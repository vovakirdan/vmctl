"""Read-only choices for CLI completion and future frontend feature selectors."""

from collections.abc import Callable
from ipaddress import IPv4Address

from vmctl.bootstrap.models import load_modules
from vmctl.bootstrap.resolver import resolve_modules, validate_definitions
from vmctl.bootstrap.system import resolve_system_features, validate_system_features
from vmctl.config import Configuration, Template
from vmctl.errors import VmctlError
from vmctl.operations import Catalog, CatalogItem


def supported_templates(
    config: Configuration, check: Callable[[Template], None]
) -> tuple[str, ...]:
    supported: list[str] = []
    for name, template in sorted(config.templates.items()):
        try:
            check(template)
        except VmctlError:
            continue
        supported.append(name)
    return tuple(supported)


def build_catalog(config: Configuration) -> Catalog:
    modules = load_modules(config)
    validate_definitions(modules, config.profiles)
    features = validate_system_features(config)
    if features.keys() & (modules.keys() | config.profiles.keys()):
        raise VmctlError(
            "System features and development modules/profiles must have distinct names"
        )

    def development_support(name: str) -> tuple[str, ...]:
        resolved = resolve_modules((name,), modules, config.profiles)

        def check(template: Template) -> None:
            for module in resolved:
                module.implementation(template)

        return supported_templates(config, check)

    def feature_support(name: str) -> tuple[str, ...]:
        def check(template: Template) -> None:
            # Check this feature and its dependencies independently of template defaults.
            resolve_system_features(
                template.model_copy(update={"system_features": []}), features, (name,)
            )

        return supported_templates(config, check)

    return Catalog(
        templates=config.templates,
        presets=config.presets,
        modules=tuple(
            CatalogItem(
                name, module.description, development_support(name), tuple(module.dependencies)
            )
            for name, module in sorted(modules.items())
        ),
        profiles=tuple(
            CatalogItem(
                name, ", ".join(composition), development_support(name), members=tuple(composition)
            )
            for name, composition in sorted(config.profiles.items())
        ),
        system_features=tuple(
            CatalogItem(
                name, feature.description, feature_support(name), tuple(feature.dependencies)
            )
            for name, feature in sorted(features.items())
        ),
        pool_start=IPv4Address(config.host.network.pool_start),
        pool_end=IPv4Address(config.host.network.pool_end),
        system_defaults={
            name: tuple(feature.name for feature in resolve_system_features(template, features))
            for name, template in config.templates.items()
        },
    )
