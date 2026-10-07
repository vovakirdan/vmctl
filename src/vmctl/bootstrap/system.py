"""Editable system capabilities and frontend-independent bootstrap selection."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import Field

from vmctl.bootstrap.models import (
    Definition,
    Implementation,
    Module,
    load_definitions,
    load_modules,
)
from vmctl.bootstrap.resolver import resolve_definitions, resolve_modules, validate_definitions
from vmctl.config import Configuration, Template
from vmctl.errors import VmctlError
from vmctl.models import CreateRequest


class SystemFeature(Definition):
    category: ClassVar[str] = "System feature"
    supported_families: list[Literal["debian", "rhel", "alpine"]] = Field(min_length=1)
    auto_apply: Literal["always", "desktop", "never"] = "never"

    def implementation(self, template: Template) -> Implementation:
        if template.family not in self.supported_families:
            raise VmctlError(
                f"System feature {self.name!r} does not support family {template.family!r}"
            )
        return super().implementation(template)


@dataclass(frozen=True)
class BootstrapSelection:
    system_features: tuple[SystemFeature, ...]
    modules: tuple[Module, ...]
    desktop: bool
    requires_desktop_password: bool

    @property
    def rdp_enabled(self) -> bool:
        return self.desktop and any(f.name == "desktop-rdp" for f in self.system_features)


def load_system_features(config: Configuration) -> dict[str, SystemFeature]:
    return load_definitions(
        config, config.path(config.host.bootstrap.system_features_dir), SystemFeature
    )


def resolve_system_features(
    template: Template,
    features: dict[str, SystemFeature],
    requested: Sequence[str] = (),
    disabled: Sequence[str] = (),
) -> tuple[SystemFeature, ...]:
    unknown = set(disabled) - features.keys()
    if unknown:
        raise VmctlError(f"Unknown disabled system feature: {', '.join(sorted(unknown))}")
    if set(requested) & set(disabled):
        raise VmctlError("A system feature cannot be both requested and disabled")
    defaults = template.system_features
    if defaults is None:
        defaults = [
            f.name
            for f in features.values()
            if f.auto_apply == "always" or (f.auto_apply == "desktop" and template.desktop)
        ]
    selected = resolve_definitions(
        [name for name in (*defaults, *requested) if name not in disabled],
        features,
        {},
        category="System feature",
    )
    for feature in selected:
        if feature.name in disabled:
            raise VmctlError(
                f"Disabled system feature {feature.name!r} is required by a dependency"
            )
        if feature.name == "desktop-rdp" and not template.desktop:
            raise VmctlError("System feature 'desktop-rdp' requires a desktop template")
        feature.implementation(template)
    return selected


def select_bootstrap(config: Configuration, request: CreateRequest) -> BootstrapSelection:
    template = config.template(request.template)
    features = load_system_features(config)
    resolve_definitions(tuple(features), features, {}, category="System feature")
    selected = resolve_system_features(
        template, features, request.system_features, request.without_system
    )
    modules = load_modules(config)
    if features.keys() & (modules.keys() | config.profiles.keys()):
        raise VmctlError(
            "System features and development modules/profiles must have distinct names"
        )
    validate_definitions(modules, config.profiles)
    development = resolve_modules(request.modules, modules, config.profiles)
    for module in development:
        module.implementation(template)
    if request.desktop_password_hash is not None and (
        not template.desktop or request.no_desktop_password
    ):
        raise VmctlError(
            "A desktop password requires a desktop template without --no-desktop-password"
        )
    return BootstrapSelection(
        selected,
        development,
        template.desktop,
        template.desktop and not request.no_desktop_password,
    )


def validate_system_features(config: Configuration) -> dict[str, SystemFeature]:
    features = load_system_features(config)
    resolve_definitions(tuple(features), features, {}, category="System feature")
    for template in config.templates.values():
        resolve_system_features(template, features)
    return features
