"""Stable recursive dependency and profile expansion with cycle detection."""

from collections.abc import Mapping, Sequence

from vmctl.bootstrap.models import Definition, Module
from vmctl.errors import VmctlError


def resolve_definitions[DefinitionType: Definition](
    requested: Sequence[str],
    modules: Mapping[str, DefinitionType],
    profiles: Mapping[str, Sequence[str]],
    *,
    category: str = "Bootstrap",
) -> tuple[DefinitionType, ...]:
    resolved: list[DefinitionType] = []
    complete: set[str] = set()
    active: list[str] = []

    def visit(name: str) -> None:
        if name in active:
            raise VmctlError(f"{category} dependency cycle: " + " -> ".join([*active, name]))
        if name in complete:
            return
        if name not in modules and name not in profiles:
            raise VmctlError(f"Unknown {category.lower()} module or feature or profile: {name}")
        active.append(name)
        dependencies = modules[name].dependencies if name in modules else profiles[name]
        for dependency in dependencies:
            visit(dependency)
        active.pop()
        complete.add(name)
        if name in modules:
            resolved.append(modules[name])

    for name in requested:
        visit(name)
    return tuple(resolved)


def resolve_modules(
    requested: Sequence[str], modules: Mapping[str, Module], profiles: Mapping[str, Sequence[str]]
) -> tuple[Module, ...]:
    return resolve_definitions(requested, modules, profiles)


def validate_definitions(
    modules: Mapping[str, Module], profiles: Mapping[str, Sequence[str]]
) -> None:
    resolve_modules(tuple(modules) + tuple(profiles), modules, profiles)
