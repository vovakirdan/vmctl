"""Data-defined modules; custom modules require no Python registration."""

from pathlib import Path, PurePosixPath
from typing import ClassVar

from pydantic import Field, ValidationError, model_validator

from vmctl.config import Configuration, StrictModel, Template, read_toml
from vmctl.errors import VmctlError
from vmctl.models import validate_name


class Script(StrictModel):
    file: str
    args: list[str] = Field(default_factory=list)


class GuestFile(StrictModel):
    path: str
    content: str
    owner: str = "root:root"
    permissions: str = "0600"

    @model_validator(mode="after")
    def validate_path(self) -> "GuestFile":
        path = PurePosixPath(self.path)
        if not path.is_absolute() or ".." in path.parts or not path.name:
            raise ValueError("Guest file path must be absolute and contain no parent traversal")
        return self


class Implementation(StrictModel):
    packages: list[str] = Field(default_factory=list)
    commands: list[list[str]] = Field(default_factory=list)
    scripts: list[Script] = Field(default_factory=list)
    files: list[GuestFile] = Field(default_factory=list)


class Definition(StrictModel):
    category: ClassVar[str] = "Bootstrap definition"
    name: str
    description: str = ""
    dependencies: list[str] = Field(default_factory=list)
    implementations: dict[str, Implementation]

    def implementation(self, template: Template) -> Implementation:
        for key in (f"{template.distro}:{template.release}", template.distro, template.family):
            if key in self.implementations:
                return self.implementations[key]
        raise VmctlError(
            f"{self.category} {self.name!r} has no implementation for {template.distro} {template.release}"
        )


class Module(Definition):
    category: ClassVar[str] = "Module"
    default_version: str = ""


def script_path(config: Configuration, relative: str) -> Path:
    root = config.path(config.host.bootstrap.scripts_dir).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise VmctlError(f"Bootstrap script must be a file inside {root}: {relative}")
    return path


def load_definitions[DefinitionType: Definition](
    config: Configuration, directory: Path, model: type[DefinitionType]
) -> dict[str, DefinitionType]:
    if not directory.is_dir():
        raise VmctlError(f"Bootstrap directory does not exist: {directory}. Run vmctl config init")
    result: dict[str, DefinitionType] = {}
    for path in sorted(directory.glob("*.toml")):
        if model is Module and path.stem in {"system", config.host.bootstrap.system_module}:
            continue
        try:
            module = model.model_validate(read_toml(path))
        except ValidationError as exc:
            raise VmctlError(f"Invalid bootstrap definition {path}: {exc}") from exc
        validate_name(module.name)
        if module.name != path.stem:
            raise VmctlError(f"Bootstrap name must match filename: {path}")
        if module.name in result or (model is Module and module.name in config.profiles):
            raise VmctlError(f"Duplicate bootstrap name: {module.name}")
        for implementation in module.implementations.values():
            for command in implementation.commands:
                if not command or not command[0].strip():
                    raise VmctlError(f"Module {module.name} has an empty command")
            for script in implementation.scripts:
                script_path(config, script.file).read_text(encoding="utf-8")
        result[module.name] = module
    return result


def load_modules(config: Configuration) -> dict[str, Module]:
    return load_definitions(config, config.path(config.host.bootstrap.modules_dir), Module)
