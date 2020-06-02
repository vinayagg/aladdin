#!/usr/bin/env python3
"""
The aladdin build script indicated in this project's lamp.json file.

It will run when ``aladdin build`` is invoked.
"""
import collections
import contextlib
import functools
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap
import time
import typing

import coloredlogs
import jinja2
import networkx
import verboselogs
import yaml

logger = None


class Undefined:
    def __bool__(self):
        return False

    def __str__(self):
        raise NotImplementedError


UNDEFINED = Undefined()


class ConfigurationException(Exception):
    """Raised if there is an error in the component.yaml."""


class UserInfo(collections.namedtuple("UserInfo", ["create", "name", "group", "home", "sudo"])):
    def __bool__(self):
        return all(self)

    @property
    def chown(self) -> str:
        return f"{self.name}:{self.group}"


class ComponentConfig:
    """The representation of the component.yaml."""

    def __init__(self, data: dict):
        self._data = data

    def __bool__(self):
        return bool(self._data)

    def get(self, path: str, default: typing.Any = UNDEFINED) -> typing.Any:
        """
        Perform a lookup on the provided path name.

        :param path: The dot-delimited path to the config value.
        :param default: The value to return if the config value was not found.
        """
        return functools.reduce(
            lambda d, key: d.get(key, default) if isinstance(d, dict) else default,
            path.split("."),
            self._data,
        )

    @property
    def version(self) -> int:
        return self.get("meta.version", 1)

    @property
    def language_name(self) -> str:
        name = self.get("language.name")
        return name.lower() if name else UNDEFINED

    @property
    def language_version(self) -> str:
        version = self.get("language.version")
        return str(version) if version else UNDEFINED

    @property
    def image_base(self) -> str:
        return self.get("image.base")

    @property
    def image_packages(self) -> typing.List[str]:
        return self.get("image.packages")

    @property
    def image_user_info(self) -> UserInfo:
        return UserInfo(
            create=self.get("image.user.create"),
            name=self.get("image.user.name"),
            group=self.get("image.user.group"),
            home=self.get("image.user.home"),
            sudo=self.get("image.user.sudo"),
        )

    @property
    def image_workdir(self):
        return self.get("image.workdir")

    @property
    def dependencies(self) -> typing.List[str]:
        return self.get("dependencies", [])


class BuildInfo(
    collections.namedtuple(
        "BuildInfo",
        [
            "project",
            "to_publish",
            "component_graph",
            "component",
            "config",
            "tag_hash",
            "default_language_version",
            "poetry_version",
        ],
    )
):
    """
    A wrapper around the component config and some other high-level info to make parameterizing
    the build process a bit simpler. The build functions should use this rather than directly
    accessing the config.
    """

    def component_is_poetry_project(self, component=None) -> bool:
        """
        Return whether the component directory appears to be a python poetry project.

        A poetry project is defined by having two files: pyproject.toml and poetry.lock.

        :param component: The component to check, defaults to the current component.
        :returns: Whether the required files are present.
        """
        component_path = pathlib.Path("components") / (component or self.component)
        pyproject_path = component_path / "pyproject.toml"
        lock_path = component_path / "poetry.lock"
        return pyproject_path.exists() and lock_path.exists()

    def component_packages(self, component=None) -> typing.List[str]:
        """
        Provide a list of apt packages required for building python dependencies.

        This packages will only be installed in the builder image used to build the specified
        component. For instance if a "commands" component depends on a "shared" component and the
        "commands" component requires some packages, they will only be installed in the
        builder-commands multi-stage builder image, not the build-shared image.

        :param component: The component to check for package dependencies, defaults to the current
                          component.
        :returns: The list of packages to be installed with apt-get.
        """
        component_config = self.config if not component else read_component_config(component)
        return component_config.image_packages or []

    @property
    def language_name(self) -> str:
        return self.config.language_name

    @property
    def language_version(self) -> str:
        return self.config.language_version or self.default_language_version

    @property
    def tag(self) -> str:
        return f"{self.project}-{self.component}:{self.tag_hash}"

    @property
    def editor_tag(self) -> str:
        return f"{self.project}-{self.component}:editor"

    @property
    def dev(self) -> bool:
        return self.tag_hash == "local"

    @property
    def base_image(self) -> str:
        return (
            self.config.image_base
            or f"python:{'.'.join(self.language_version.split('.', 2)[:2])}-slim"
        )

    @property
    def builder_image(self):
        return f"python:{'.'.join(self.language_version.split('.', 2)[:2])}-slim"

    @property
    def workdir(self):
        return self.config.image_workdir or (
            "/code" if self.config.image_base is UNDEFINED else None
        )

    @property
    def user_info(self) -> UserInfo:
        if self.config.image_base is not UNDEFINED and not self.config.image_user_info.name:
            raise ConfigurationException(
                "Must provide at least the user.name if not using the default image"
            )

        name = "aladdin-user"
        return UserInfo(
            create=self.config.image_user_info.create or self.config.image_base is UNDEFINED,
            name=self.config.image_user_info.name or name,
            group=self.config.image_user_info.group or self.config.image_user_info.name or name,
            home=(
                self.config.image_user_info.home
                or f"/home/{self.config.image_user_info.name or name}"
            ),
            sudo=(
                self.dev
                if self.config.image_user_info.sudo is UNDEFINED
                else self.config.image_user_info.sudo
            ),
        )

    @property
    def dependencies(self) -> typing.Tuple[str]:
        """
        The topologically sorted list of dependencies required for this component.

        This will include the complete hierarchy of dependencies for this component, so it is only
        necessary to enumerate a component's direct dependencies in the component.yaml file.
        """
        dependencies = networkx.algorithms.dag.ancestors(self.component_graph, self.component)
        return tuple(
            networkx.algorithms.dag.topological_sort(self.component_graph.subgraph(dependencies))
        )

    @property
    def components(self) -> typing.Tuple[str]:
        """
        The topologically sorted list of dependencies required for this component followed by this
        component itself.
        """
        return self.dependencies + (self.component,)

    @property
    def dockerfile(self) -> str:
        path = pathlib.Path("components") / self.component / "Dockerfile"
        return path if path.exists() else None

    @property
    def dockerfile_content(self) -> str:
        with open(self.dockerfile) as dockerfile:
            return dockerfile.read()


def main():
    """Kick off the build process with data gathered from the system and environment."""

    # Install some nice logging tools
    global logger

    verboselogs.install()
    coloredlogs.install(
        level=logging.DEBUG,
        fmt="%(levelname)-8s %(message)s",
        level_styles=dict(
            spam=dict(color="green", faint=True),
            debug=dict(color="black", bold=True),
            verbose=dict(color="blue"),
            info=dict(color="white"),
            notice=dict(color="magenta"),
            warning=dict(color="yellow"),
            success=dict(color="green", bold=True),
            error=dict(color="red"),
            critical=dict(color="red", bold=True),
        ),
        field_styles=dict(
            asctime=dict(color="green"),
            hostname=dict(color="magenta"),
            levelname=dict(color="white"),
            name=dict(color="white", bold=True),
            programname=dict(color="cyan"),
            username=dict(color="yellow"),
        ),
    )

    # This will be a VerboseLogger
    logger = logging.getLogger(__name__)

    # Provide the lamp.json file data to the build process
    with open("lamp.json") as lamp_file:
        lamp = json.load(lamp_file)

    # Let's get to it!
    build_components(lamp=lamp, tag_hash=os.getenv("HASH", "local"), components=sys.argv[1:])


def build_components(lamp: dict, tag_hash: str, components: typing.List[str] = None):
    """
    Build each component for the project.

    If components is empty, this will assume each directory in the components/ directory is a
    component and will build each of them.

    :param lamp: The data from the project's lamp.json file.
    :param tag_hash: The build hash provided by ``aladdin build``.
    :param components: The list of components to build, defaults to all of them.
    """
    components_path = pathlib.Path("components")
    all_components = [
        item
        for item in os.listdir(components_path)
        if os.path.isdir(components_path / item) and not item.startswith("_")
    ]

    if not components:
        if tag_hash == "local":
            # Just build everything if doing a local build
            components = all_components
        else:
            # Only build components that will be published
            prefix = f"{lamp['name']}-"
            components = {
                image[image.startswith(prefix) and len(prefix) :]
                for image in lamp.get("docker_images", [])
            }

    for component in components:
        if component not in all_components:
            raise ValueError(f"Component '{component}' does not exist")

    if not components:
        logger.info(
            "No components found for this project. Create a component directory to get started."
        )
        return

    # Check for cycles in the component dependency graph
    component_graph = validate_component_dependencies(components=components)

    # Let's build in topological order
    components = [
        component
        for component in networkx.algorithms.dag.topological_sort(component_graph)
        if component in components
    ]

    # Build each component in turn
    for component in components:
        try:
            logger.notice("Starting build for %s component", component)

            component_yaml_path = pathlib.Path("components") / component / "component.yaml"
            dockerfile_path = pathlib.Path("components") / component / "Dockerfile"
            if component_yaml_path.exists():
                build_aladdin_component(
                    lamp=lamp,
                    component=component,
                    component_graph=component_graph,
                    tag_hash=tag_hash,
                )
            elif dockerfile_path.exists():
                build_traditional_component(
                    project=lamp["name"], component=component, tag_hash=tag_hash
                )
            else:
                raise ConfigurationException(
                    "No component.yaml or Dockerfile found for '%s' component", component
                )

        except Exception:
            logger.error("Failed to build image for component: %s", component)
            raise
        else:
            logger.success("Built image for component: %s\n\n", component)
    else:
        logger.success("Built images for components: %s", ", ".join(components))


def validate_component_dependencies(components: typing.List[str]) -> networkx.DiGraph:
    """
    Confirm that the components' dependency hierarchy has no cycles.

    :returns: The component dependency graph
    """
    # Create the component dependency graph
    component_graph = networkx.DiGraph()
    component_graph.add_nodes_from(components)
    for component in components:
        config = read_component_config(component)
        component_graph.add_edges_from(
            (dependency, component) for dependency in config.dependencies
        )

    # Check the graph for cycles
    try:
        cycles = networkx.algorithms.cycles.find_cycle(component_graph)
    except networkx.exception.NetworkXNoCycle:
        return component_graph
    else:
        logger.error("Cycle(s) found in component dependency graph: %s", cycles)
        raise ConfigurationException("Cycle(s) found in component dependency graph", cycles)


def read_component_config(component: str) -> ComponentConfig:
    """
    Read the component's ``component.yaml`` file into a ``ComponentConfig`` object.

    :param component: The component's config to read.
    :returns: The config data for the component. If the component does not provide a
              ``component.yaml`` file, this returns an empty config.
    """
    try:
        with open(pathlib.Path("components") / component / "component.yaml") as file:
            return ComponentConfig(yaml.safe_load(file))
    except ConfigurationException:
        raise
    except Exception:
        return ComponentConfig({})


def build_traditional_component(project: str, component: str, tag_hash: str) -> None:
    """
    Build a standard component image.

    This will build an image based solely on the Dockerfile in the component directory. No aladdin
    boilerplate or transforms will be applied. It resulting image will be tagged as
    '{project}-{component}:{tag_hash}'

    :param project: The name of the project containing the component
    :param component: The name of the component being built.
    :param tag_hash: The hash to use as the image tag.
    """
    logger.info("Building standard image for component: %s", component)
    _docker_build(
        tags=f"{project}-{component}:{tag_hash}",
        dockerfile=pathlib.Path("components") / component / "Dockerfile",
    )


def build_aladdin_component(
    lamp: dict,
    component: str,
    component_graph: networkx.DiGraph,
    tag_hash: str,
    default_python_version: str = "3.8",
    default_poetry_version: str = "1.0.5",
) -> None:
    """
    Build a component that has defined a component.yaml file.

    :param lamp: The data from the project's lamp.json file.
    ;param component: The name of the component to build.
    ;param component_graph: The component dependency graph.
    :param tag_hash: The build hash provided by ``aladdin build``.
    :param default_python_version: The python version to use for the base image if not provided in
                                   the component's ``component.yaml`` file, defaults to ``"3.8"``.
    :param default_poetry_version: The version of poetry to install in the component images,
                                   defaults to ``"1.0.5"``.
    """
    # Read the component.yaml file
    component_config = read_component_config(component)

    # We currently assume every component is python.
    # This assumption could conceivably be configured in the lamp.json file for
    # language-homogenous projects.
    language = component_config.language_name or "python"

    if language == "python":
        build_info = BuildInfo(
            project=lamp["name"],
            to_publish=lamp["docker_images"],
            component_graph=component_graph,
            component=component,
            config=component_config,
            tag_hash=tag_hash,
            # TODO: Handle these in a better manner when adding support for other languages
            default_language_version=default_python_version,
            poetry_version=default_poetry_version,
        )

        language_version = build_info.language_version
        if not language_version.startswith("3"):
            raise ValueError(
                f"Unsupported python version for {component} component: {language_version}"
            )

        # We only support python 3 components at the moment
        build_python_component(build_info)

        if tag_hash == "local":
            # Build the editor image for facilitating poetry package updates
            build_python_editor_image(build_info)
    else:
        raise ValueError(
            f"Unsupported language for {component} component: {language}:{language_version}"
        )


def build_python_component(build_info: BuildInfo) -> None:
    """
    Build the component.

    This builds the component image according to the ``component.yaml`` configuration. It begins by
    building or tagging a base image and then adding to it things like the poetry tool and any
    component dependency assets.

    :param build_info: The build info populated from the config and command line arguments.
    """
    logger.info("Building aladdin image for python component: %s", build_info.component)

    env = jinja2.Environment(
        loader=jinja2.PackageLoader("build_components", "templates/python"), trim_blocks=True
    )

    template = env.get_template("Dockerfile.j2")

    with build_context(
        component=build_info.component,
        dockerfile=template.render(build_info=build_info),
        copy_dockerfile=build_info.dev,
    ):
        _docker_build(tags=build_info.tag)


@contextlib.contextmanager
def build_context(component: str, dockerfile: str, copy_dockerfile: bool) -> typing.Generator:
    """
    A context manager that writes the contents to components/Dockerfile

    This writes the provided contents to components/Dockerfile. It then deletes the file upon exit.

    :param component: The component to build.
    :param contents: The Dockerfile contents to use within the contents.
    :param copy_dockerfile: Whether to write the generated Dockerfile to the component directory
                            for debuging purposes.
    """
    components_path = pathlib.Path("components")
    pip_conf_path = components_path / "pip.conf"
    poetry_toml_path = components_path / "poetry.toml"
    dockerfile_path = components_path / "Dockerfile"
    try:
        # In addition to the generated Dockerfile, we provide these files in the build context
        # so that the Dockerfile can COPY these artifacts into the image. These are boilerplate
        # files that we don't want to burden the aladdin client project with including.
        with open(pip_conf_path, "w") as outfile:
            outfile.write(
                textwrap.dedent(
                    """
                    # This is a dynamically generated file created by build-components for the
                    # purpose of building the component containers.
                    # It is copied into our docker images to globally configure pip

                    [global]
                    # Install packages under the user directory
                    user = true
                    # Disable the cache dir
                    no-cache-dir = false

                    [install]
                    # Disable the .local warning
                    no-warn-script-location = false
                    """
                )
            )

        with open(poetry_toml_path, "w") as outfile:
            outfile.write(
                textwrap.dedent(
                    """
                    # This is a dynamically generated file created by build-components for the
                    # purpose of building the component containers.
                    # It is copied into our docker images to globally configure poetry

                    [virtualenvs]
                    # We're in a docker container, there's no need for virtualenvs
                    # One should still specify "ENV PIP_USER yes" to let poetry know
                    # install packages as --user so they show up in ~/.local
                    create = false
                    """
                )
            )

        with open(dockerfile_path, "w") as outfile:
            outfile.write(dockerfile)

        if copy_dockerfile:
            with contextlib.suppress():
                shutil.copyfile(
                    dockerfile_path, pathlib.Path("components") / component / "build.dockerfile"
                )

        yield
    finally:
        with contextlib.suppress():
            dockerfile_path.unlink()

        with contextlib.suppress():
            pip_conf_path.unlink()

        with contextlib.suppress():
            poetry_toml_path.unlink()


def build_python_editor_image(build_info: BuildInfo) -> None:
    """
    Build a companion image to the final built image that removes the ENTRYPOINT and CMD settings.

    This image can be used for shelling into for debugging and/or running arbitrary commands in a
    mirror of the built image.

    :param build_info: The build info populated from the config and command line arguments.
    """
    logger.info("Building editor image for %s component", build_info.component)

    # Perform a "no context" docker build
    _docker_build(
        tags=build_info.editor_tag,
        dockerfile=textwrap.dedent(
            f"""
            FROM {build_info.tag}
            CMD "/bin/sh"
            ENTRYPOINT []
            """
        ).encode(),
    )


def _docker_build(
    tags: typing.Union[str, typing.List[str]],
    buildargs: dict = None,
    dockerfile: typing.Union[pathlib.Path, bytes] = None,
) -> None:
    """
    A convenience wrapper for calling out to "docker build".

    We always send the same context: the entire components/ directory.

    :param tags: The tags to be applied to the built image.
    :param buildargs: Values for ARG instructions in the dockerfile.
    :param dockerfile: The dockerfile to build against. If not provided, it's assumed that a
                       Dockerfile is present in the context directory. If it's a bytes object, it
                       will be provided to the docker build process on stdin and a "no context"
                       build will take place. Otherwise, a normal docker build will be performed
                       with the specified Dockerfile.
    """
    buildargs = buildargs or {}
    buildargs.setdefault("CACHE_BUST", str(time.time()))

    cmd = ["env", "DOCKER_BUILDKIT=1", "docker", "build"]

    for key, value in buildargs.items():
        cmd.extend(["--build-arg", f"{key}={value}"])

    tags = [tags] if isinstance(tags, str) else tags
    for tag in tags:
        cmd.extend(["--tag", tag])

    if isinstance(dockerfile, bytes):
        # If we receive the Dockerfile as content, we should pipe it to stdin.
        # This is the "no context" build.
        cmd.extend(["-"])
    else:
        # Otherwise, they can specify the path to the Dockerfile to use or let docker
        # find one in the context directory.
        if dockerfile:
            cmd.extend(["-f", dockerfile.as_posix()])
        cmd.extend(["components"])

    logger.debug("Docker build command: %s", " ".join(cmd))
    _check_call(cmd, stdin=dockerfile if isinstance(dockerfile, bytes) else None)


def _check_call(cmd: typing.List[str], stdin: bytes = None) -> None:
    """
    Make a subprocess call and indent its output to match our python logging format.

    :param cmd: The command to run.
    :param stdin: Data to send to the subprocess as its input.
    """
    if stdin is None:
        ps = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        subprocess.run(["sed", "-e", "s/^/         /"], stdin=ps.stdout, check=True)
        ps.wait()
        if ps.returncode:
            raise subprocess.CalledProcessError(ps.returncode, cmd)
    else:
        ps = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        stdout, stderr = ps.communicate(input=stdin)
        if ps.returncode:
            raise subprocess.CalledProcessError(ps.returncode, cmd)
        subprocess.run(["sed", "-e", "s/^/         /"], input=stdout, check=True)
