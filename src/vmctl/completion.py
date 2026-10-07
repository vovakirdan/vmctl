"""Read-only shell suggestions, independent of provisioning and interactive prompts."""

from collections.abc import Callable, Iterable
from ipaddress import IPv4Address

import typer

from vmctl.errors import VmctlError
from vmctl.operations import Catalog, CatalogItem

type Suggestions = list[tuple[str, str]]


def read_catalog(ctx: typer.Context) -> Catalog:
    # Lazy import avoids a cycle and keeps workstation imports Linux-independent.
    from vmctl.cli import operations

    return operations(ctx, completion=True).catalog()


def quietly(callback: Callable[[], Suggestions]) -> Suggestions:
    try:
        return callback()
    except (VmctlError, OSError, ValueError):
        # Offline completion must not print errors or interfere with shell input.
        return []


def matches(choices: Iterable[tuple[str, str]], incomplete: str) -> Suggestions:
    return sorted(
        (value, " ".join(help_text.split()))
        for value, help_text in choices
        if value.startswith(incomplete)
    )


def templates(ctx: typer.Context, incomplete: str) -> Suggestions:
    def suggest() -> Suggestions:
        return matches(
            (
                (name, f"{template.distro} {template.release}; VMID {template.vmid}")
                for name, template in read_catalog(ctx).templates.items()
            ),
            incomplete,
        )

    return quietly(suggest)


def presets(ctx: typer.Context, incomplete: str) -> Suggestions:
    def suggest() -> Suggestions:
        return matches(
            (
                (
                    name,
                    f"{preset.cpu} vCPU; {preset.memory_mib} MiB RAM; {preset.disk_gib} GiB disk",
                )
                for name, preset in read_catalog(ctx).presets.items()
            ),
            incomplete,
        )

    return quietly(suggest)


def comma_choices(
    ctx: typer.Context, incomplete: str, items: Iterable[CatalogItem], *, compatible: bool = True
) -> Suggestions:
    prefix, separator, partial = incomplete.rpartition(",")
    selected = {name.strip() for name in prefix.split(",")} if separator else set()
    template = ctx.params.get("template")
    choices = (
        (item.name, item.description)
        for item in items
        if item.name not in selected
        and (not compatible or not template or template in item.templates)
    )
    # Return the entire comma-separated word; shells do not split it at commas.
    return [
        ((prefix + separator if separator else "") + value, help_text)
        for value, help_text in matches(choices, partial if separator else incomplete)
    ]


def development(ctx: typer.Context, incomplete: str) -> Suggestions:
    def suggest() -> Suggestions:
        catalog = read_catalog(ctx)
        return comma_choices(ctx, incomplete, (*catalog.modules, *catalog.profiles))

    return quietly(suggest)


def system(ctx: typer.Context, incomplete: str) -> Suggestions:
    return quietly(lambda: comma_choices(ctx, incomplete, read_catalog(ctx).system_features))


def without_system(ctx: typer.Context, incomplete: str) -> Suggestions:
    return quietly(
        lambda: comma_choices(ctx, incomplete, read_catalog(ctx).system_features, compatible=False)
    )


def references(ctx: typer.Context, incomplete: str) -> Suggestions:
    from vmctl.cli import operations

    def suggest() -> Suggestions:
        choices: dict[str, str] = {}
        for details in operations(ctx, completion=True).list():
            vm = details.vm
            choices[vm.name] = f"VMID {vm.vmid}; {vm.status}; node {vm.node}"
            choices[str(vm.vmid)] = f"{vm.name}; {vm.status}; node {vm.node}"
        return matches(choices.items(), incomplete)

    return quietly(suggest)


def addresses(ctx: typer.Context, incomplete: str) -> Suggestions:
    def suggest() -> Suggestions:
        catalog = read_catalog(ctx)
        # Bound suggestions for unusually large pools; creation validates availability.
        start, end = int(catalog.pool_start), int(catalog.pool_end)
        return matches(
            [("auto", "Allocate an available pool address at creation")]
            + [
                (str(IPv4Address(address)), "Pool candidate; availability checked at creation")
                for address in range(start, min(end + 1, start + 256))
            ],
            incomplete,
        )

    return quietly(suggest)


def resources(ctx: typer.Context, incomplete: str, kind: str) -> Suggestions:
    def suggest() -> Suggestions:
        choices: dict[str, str] = {}
        for name, preset in read_catalog(ctx).presets.items():
            if kind == "cpu":
                value = str(preset.cpu)
            elif kind == "memory":
                value = (
                    f"{preset.memory_mib // 1024}G"
                    if preset.memory_mib % 1024 == 0
                    else f"{preset.memory_mib}M"
                )
            else:
                value = f"{preset.disk_gib}G"
            choices[value] = f"Configured {name} preset"
        return matches(choices.items(), incomplete)

    return quietly(suggest)


def cpu(ctx: typer.Context, incomplete: str) -> Suggestions:
    return resources(ctx, incomplete, "cpu")


def memory(ctx: typer.Context, incomplete: str) -> Suggestions:
    return resources(ctx, incomplete, "memory")


def disk(ctx: typer.Context, incomplete: str) -> Suggestions:
    return resources(ctx, incomplete, "disk")
