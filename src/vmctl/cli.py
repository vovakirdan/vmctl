"""Typer presentation; domain services can be reused by other frontends."""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

from vmctl import completion
from vmctl.client_config import (
    default_client_directory,
    initialize_client_config,
)
from vmctl.errors import VmctlError
from vmctl.frontend import FrontendSettings, build_operations, load_public_key, target_label
from vmctl.models import CreateRequest
from vmctl.operations import LifecycleAction, Operations
from vmctl.services.init_config import initialize_config
from vmctl.utils.passwords import hash_desktop_password

if TYPE_CHECKING:
    from vmctl.config import Configuration
    from vmctl.network.allocator import AddressAllocator
    from vmctl.network.dnsmasq import DnsmasqReservations
    from vmctl.proxmox.client import ProxmoxClient

app = typer.Typer(
    no_args_is_help=True,
    help="Manage Proxmox VMs through SSH or directly on the host.",
    pretty_exceptions_show_locals=False,
)
config_app = typer.Typer(
    help="Initialize and validate configuration.", pretty_exceptions_show_locals=False
)
app.add_typer(config_app, name="config")
console = Console(markup=False)
errors = Console(stderr=True, markup=False)


CLISettings = FrontendSettings


@contextmanager
def present_errors() -> Iterator[None]:
    try:
        yield
    except (VmctlError, OSError, UnicodeError) as exc:
        errors.print(f"Error: {exc}")
        raise typer.Exit(1) from exc


@app.callback()
def main(
    ctx: typer.Context,
    config_dir: Annotated[
        Path | None,
        typer.Option(help="Client configuration directory; server directory with --local."),
    ] = None,
    local: Annotated[
        bool, typer.Option("--local", help="Execute directly on this Linux Proxmox host.")
    ] = False,
    verbose: Annotated[bool, typer.Option(help="Enable safe command debug logging.")] = False,
) -> None:
    if local and sys.platform != "linux":
        errors.print("Error: --local requires Linux")
        raise typer.Exit(1)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING, format="%(levelname)s: %(message)s"
    )
    ctx.obj = CLISettings(
        config_dir or (Path("/etc/vmctl") if local else default_client_directory()), local
    )


def dependencies(
    config: Configuration,
) -> tuple[ProxmoxClient, DnsmasqReservations, AddressAllocator]:
    # Keep adapter construction lazy so workstation imports remain portable.
    from vmctl.local import dependencies as host_dependencies

    return host_dependencies(config)


def settings_for(ctx: typer.Context) -> CLISettings:
    if isinstance(ctx.obj, CLISettings):
        return ctx.obj
    # Shell completion parses options without invoking the root callback.
    params = ctx.find_root().params
    local = bool(params.get("local"))
    directory = params.get("config_dir")
    return CLISettings(
        Path(directory)
        if directory
        else (Path("/etc/vmctl") if local else default_client_directory()),
        local,
    )


def operations(ctx: typer.Context, *, completion: bool = False) -> Operations:
    return build_operations(settings_for(ctx), completion=completion, local_factory=dependencies)


def public_key(ctx: typer.Context, path: Path | None) -> str | None:
    return load_public_key(settings_for(ctx), path)


@app.command(
    help="Full-clone a template with preset resources, DHCP and cloud-init. Development and AI tools are opt-in. Use --dry-run to check prerequisites first."
)
def create(
    ctx: typer.Context,
    name: Annotated[
        str, typer.Argument(help="New VM name: lowercase DNS label, up to 63 characters.")
    ],
    template: Annotated[
        str,
        typer.Argument(
            help="Configured template name; see vmctl templates.",
            autocompletion=completion.templates,
        ),
    ],
    preset: Annotated[
        str,
        typer.Argument(
            help="Resource preset name; see vmctl presets. Options override its defaults.",
            autocompletion=completion.presets,
        ),
    ],
    cpu: Annotated[
        int | None,
        typer.Option(
            min=1, max=512, help="Override preset CPU cores (vCPUs).", autocompletion=completion.cpu
        ),
    ] = None,
    memory: Annotated[
        str | None,
        typer.Option(
            help="Override preset RAM: bare MiB, or binary sizes such as 2048M / 48G.",
            autocompletion=completion.memory,
        ),
    ] = None,
    disk: Annotated[
        str | None,
        typer.Option(
            help="Minimum disk size: bare GiB, or 160G. Only enlarges the template disk; never shrinks it.",
            autocompletion=completion.disk,
        ),
    ] = None,
    ip: Annotated[
        str,
        typer.Option(
            help="auto allocates a free DHCP reservation, or specify an IPv4 address in the managed pool. Availability is checked at creation.",
            autocompletion=completion.addresses,
        ),
    ] = "auto",
    ssh_key: Annotated[
        Path | None,
        typer.Option(
            help="OpenSSH public-key file on this computer, overriding client.ssh_key. Never supply a private key."
        ),
    ] = None,
    with_modules: Annotated[
        str,
        typer.Option(
            "--with",
            help="Optional development or AI modules and profile aliases, comma-separated: rust,node,codex or surge-dev,ai-cli. Dependencies such as Node are included once. Desktop apps require a compatible desktop template. See vmctl bootstrap.",
            autocompletion=completion.development,
        ),
    ] = "",
    with_system: Annotated[
        str,
        typer.Option(
            help="Add guest system features, comma-separated, independently of --with. Template defaults already include qemu-agent and, for desktops, desktop-rdp.",
            autocompletion=completion.system,
        ),
    ] = "",
    without_system: Annotated[
        str,
        typer.Option(
            help="Disable automatic system features, e.g. desktop-rdp. Disabling a feature still required by another feature is an error.",
            autocompletion=completion.without_system,
        ),
    ] = "",
    no_desktop_password: Annotated[
        bool,
        typer.Option(
            "--no-desktop-password",
            help="Skip the desktop password prompt; another login setup is required.",
        ),
    ] = False,
    start: Annotated[
        bool,
        typer.Option(
            "--start/--no-start",
            help="Start after provisioning (default). --no-start leaves the VM powered off with cloud-init ready for its first boot.",
        ),
    ] = True,
    description: Annotated[
        str, typer.Option(help="Human-readable description saved in the Proxmox VM configuration.")
    ] = "",
    wait: Annotated[
        int,
        typer.Option(
            min=0,
            help="Wait up to this many seconds for an SSH banner from the host; requires --start. Does not verify bootstrap completion.",
        ),
    ] = 0,
    dry_run: Annotated[
        bool,
        typer.Option(
            help="Check live host/template/storage/network prerequisites and print a plan. Creates no VM or reservation and never prompts for a desktop password."
        ),
    ] = False,
) -> None:
    with present_errors():
        backend = operations(ctx)
        modules = tuple(item.strip() for item in with_modules.split(",") if item.strip())
        request = CreateRequest(
            name,
            template,
            preset,
            cpu,
            memory,
            disk,
            ip,
            modules=modules,
            start=start,
            description=description,
            wait_seconds=wait,
            system_features=tuple(item.strip() for item in with_system.split(",") if item.strip()),
            without_system=tuple(
                item.strip() for item in without_system.split(",") if item.strip()
            ),
            no_desktop_password=no_desktop_password,
            public_key=public_key(ctx, ssh_key),
        )
        plan = backend.plan_create(request)
        if dry_run:
            console.print("Dry run: no changes will be made")
            console.print(
                f"Full clone: {plan.template_vmid} -> VMID allocated by Proxmox at execution"
            )
            console.print(
                f"Name: {name}; CPU: {plan.resources.cpu}; memory: {plan.resources.memory_mib} MiB"
            )
            console.print(
                f"Disk: {plan.disk_device}, at least {max(plan.resources.disk_gib, plan.template_disk_gib)} GiB"
            )
            console.print(
                f"Network: {plan.bridge}; MAC: generated by Proxmox; IP: {ip} (checked again at execution)"
            )
            console.print(f"System features: {', '.join(plan.system_features) or 'none'}")
            console.print(f"Optional modules: {', '.join(plan.modules) or 'none'}; start: {start}")
            if plan.desktop:
                console.print(
                    "Desktop password: interactive setup required"
                    if plan.requires_desktop_password
                    else "Desktop password: skipped"
                )
            return
        if plan.requires_desktop_password:
            password_hash = hash_desktop_password(
                typer.prompt(
                    f"Desktop password for {plan.username}",
                    hide_input=True,
                    confirmation_prompt="Confirm password",
                    show_default=False,
                )
            )
            request = replace(request, desktop_password_hash=password_hash)
        elif plan.desktop:
            errors.print(
                "Warning: GUI/RDP login may not be usable without another authentication setup."
            )
        request_id = uuid.uuid4().hex
        console.print(f"Request ID: {request_id}")
        result = backend.create(request, request_id=request_id, progress=console.print)
        console.print("VM created successfully\n")
        for label, value in (
            ("VMID", str(result.vmid)),
            ("Name", result.name),
            ("Template", result.template),
            ("Preset", result.preset),
            ("CPU", f"{result.resources.cpu} vCPU"),
            ("Memory", f"{result.resources.memory_mib / 1024:g} GiB"),
            ("Disk", f"{result.resources.disk_gib} GiB"),
            ("MAC", result.mac),
            ("IP", str(result.ip)),
            ("Started", str(result.started)),
            ("Readiness", result.readiness),
        ):
            console.print(f"{label + ':':12}{value}")
        console.print(f"\nSSH:\n  ssh {result.username}@{result.ip}")
        console.print(f"\nSystem features: {', '.join(result.system_features) or 'none'}")
        console.print(f"Optional modules: {', '.join(result.modules) or 'none'}")
        if result.rdp_enabled:
            console.print(f"\nRDP:\n  {result.ip}:3389\n  user: {result.username}")
        console.print("Bootstrap execution inside the guest has not been verified.")


@app.command(
    help="Stop and permanently destroy a VM and its disks, then remove its managed DHCP reservation and cloud-init snippet. Templates are protected; ordinary unmanaged VMs can also be deleted."
)
def delete(
    ctx: typer.Context,
    reference: Annotated[
        str,
        typer.Argument(
            help="Existing VM name or VMID; see vmctl list.", autocompletion=completion.references
        ),
    ],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip deletion confirmation.")] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            help="Resolve the VM and show the deletion plan without stopping or destroying it."
        ),
    ] = False,
) -> None:
    with present_errors():
        backend = operations(ctx)
        plan = backend.plan_delete(reference)
        if dry_run:
            console.print(
                f"Dry run: stop and destroy VM {plan.vm.vmid} ({plan.vm.name}); then clean its managed reservation and snippet"
            )
            return
        if not yes:
            typer.confirm(
                f"Permanently destroy VM {plan.vm.vmid} ({plan.vm.name}) and its disks?", abort=True
            )
        vmid = backend.delete(plan)
        console.print(f"VM {vmid} deleted successfully")


VMReference = Annotated[
    str,
    typer.Argument(help="Existing VM name or VMID.", autocompletion=completion.references),
]


def lifecycle_command(
    ctx: typer.Context, reference: str, action: LifecycleAction, *, yes: bool, dry_run: bool
) -> None:
    with present_errors():
        backend = operations(ctx)
        plan = backend.plan_action(reference, action)
        target = f"VM {plan.vm.vmid} ({plan.vm.name})"
        if dry_run:
            console.print(f"Dry run: {action} {target}; current status: {plan.vm.status}")
            return
        if not yes:
            typer.confirm(f"{action.capitalize()} {target}?", abort=True)
        result = backend.action(plan)
        console.print(f"{action.capitalize()} completed for {target}; status: {result.vm.status}")


@app.command(help="Start a stopped VM. Templates and locked VMs are protected.")
def start(
    ctx: typer.Context,
    reference: VMReference,
    dry_run: Annotated[
        bool, typer.Option(help="Check the VM and show the action without starting it.")
    ] = False,
) -> None:
    lifecycle_command(ctx, reference, "start", yes=True, dry_run=dry_run)


@app.command(
    help="Gracefully shut down a running VM using ACPI or the guest agent. Uses the configured stop_timeout (default 120 seconds); never falls back to a forced stop."
)
def shutdown(
    ctx: typer.Context,
    reference: VMReference,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip shutdown confirmation.")] = False,
    dry_run: Annotated[
        bool, typer.Option(help="Check the VM and show the action without shutting it down.")
    ] = False,
) -> None:
    lifecycle_command(ctx, reference, "shutdown", yes=yes, dry_run=dry_run)


@app.command(
    help="Gracefully reboot a running VM through Proxmox. Uses the configured stop_timeout (default 120 seconds); never forces a reset. Guest readiness is not verified."
)
def reboot(
    ctx: typer.Context,
    reference: VMReference,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip reboot confirmation.")] = False,
    dry_run: Annotated[
        bool, typer.Option(help="Check the VM and show the action without rebooting it.")
    ] = False,
) -> None:
    lifecycle_command(ctx, reference, "reboot", yes=yes, dry_run=dry_run)


@app.command(
    help="Open the Textual VM dashboard and creation form using the same connection settings as the CLI. Separate system features, development tools, AI CLI agents and compatible desktop apps."
)
def tui(
    ctx: typer.Context,
    read_only: Annotated[
        bool,
        typer.Option(
            help="Browse VMs and preview the creation form; disable create, delete and lifecycle operations."
        ),
    ] = False,
) -> None:
    with present_errors():
        from vmctl.tui.app import VmctlApp

        settings = settings_for(ctx)
        VmctlApp(
            operations(ctx),
            target=target_label(settings),
            public_key=lambda path: load_public_key(settings, path),
            read_only=read_only,
        ).run()


@app.command("list", help="List VMs with resources and reserved or detected IPv4 addresses.")
def list_command(ctx: typer.Context) -> None:
    with present_errors():
        backend = operations(ctx)
        table = Table("VMID", "Name", "Status", "Template / OS", "CPU", "RAM", "IP", "IP source")
        # An ellipsized IP cannot be copied or used to connect to the VM.
        table.columns[6].min_width = 15
        table.columns[6].no_wrap = True
        table.columns[7].min_width = 11
        table.columns[7].no_wrap = True
        for details in backend.list():
            vm = details.vm
            cpu = int(details.config.get("cores", "1")) * int(details.config.get("sockets", "1"))
            memory = int(details.config.get("memory", "0"))
            table.add_row(
                str(vm.vmid),
                vm.name,
                vm.status,
                details.metadata.get("template", "unknown"),
                str(cpu),
                f"{memory / 1024:g} GiB",
                str(details.display_ip) if details.display_ip else "unknown",
                details.ip_source,
            )
        console.print(table)


@app.command(
    help="Show VM configuration, reserved/detected IP with its source, system features, optional development/AI modules and desktop/RDP provisioning metadata. Credentials are redacted; guest readiness is not probed."
)
def info(
    ctx: typer.Context,
    reference: Annotated[
        str, typer.Argument(help="Existing VM name or VMID.", autocompletion=completion.references)
    ],
) -> None:
    with present_errors():
        backend = operations(ctx)
        details = backend.info(reference)
        table = Table("Property", "Value")
        table.add_row("VMID", str(details.vm.vmid))
        table.add_row("Status", details.vm.status)
        table.add_row("Node", details.vm.node)
        table.add_row("Managed IP", str(details.ip) if details.ip else "unknown")
        table.add_row("IP", str(details.display_ip) if details.display_ip else "unknown")
        table.add_row("IP source", details.ip_source)
        for address in details.addresses:
            table.add_row("Observed IP", f"{address.address} ({address.source})")
        for note in details.ip_notes:
            table.add_row("IP discovery", note)
        table.add_row(
            "System features",
            ", ".join(details.system_features)
            or ("none" if "system_features" in details.metadata else "unknown"),
        )
        table.add_row(
            "Optional modules",
            ", ".join(details.modules) or ("none" if "modules" in details.metadata else "unknown"),
        )
        table.add_row(
            "Desktop",
            "yes" if details.desktop else ("no" if details.desktop is False else "unknown"),
        )
        if details.desktop:
            table.add_row(
                "RDP", "enabled (provisioning requested)" if details.rdp_enabled else "disabled"
            )
            if details.rdp_enabled:
                table.add_row("RDP address", details.rdp_address or "unknown")
                table.add_row("RDP user", details.metadata.get("username", "unknown"))
        for key, value in sorted(details.config.items()):
            table.add_row(key, "<redacted>" if key in {"cipassword", "sshkeys"} else value)
        for key, value in sorted(details.metadata.items()):
            table.add_row(f"vmctl.{key}", value)
        console.print(table)


@app.command(
    help="Read one VM's CPU, memory, uptime and cumulative network/disk counters from Proxmox. Use tui for live rate graphs. No guest credentials or VM changes."
)
def stats(ctx: typer.Context, reference: VMReference) -> None:
    with present_errors():
        result = operations(ctx).stats(reference)
        table = Table("Property", "Value", title=f"VM {result.vm.vmid} · {result.vm.name}")
        table.add_row("Status", result.vm.status)
        table.add_row(
            "CPU (cached VM sample)",
            f"{result.cpu_percent:.1f}%" if result.cpu_percent is not None else "unavailable",
        )
        for label, amount in (
            ("Memory (host-reported)", result.memory_bytes),
            ("Memory limit", result.memory_total_bytes),
            ("Network received (total)", result.network_in_bytes),
            ("Network transmitted (total)", result.network_out_bytes),
            ("Disk read (total)", result.disk_read_bytes),
            ("Disk written (total)", result.disk_write_bytes),
        ):
            table.add_row(label, f"{amount:,} bytes" if amount is not None else "unavailable")
        table.add_row(
            "Uptime",
            f"{result.uptime_seconds}s" if result.uptime_seconds is not None else "unavailable",
        )
        console.print(table)


@app.command(help="Show configured resource presets.")
def presets(ctx: typer.Context) -> None:
    with present_errors():
        table = Table("Preset", "CPU", "Memory", "Disk")
        for name, preset in operations(ctx).presets().items():
            table.add_row(
                name, str(preset.cpu), f"{preset.memory_mib} MiB", f"{preset.disk_gib} GiB"
            )
        console.print(table)


@app.command(help="Show configured VM templates.")
def templates(ctx: typer.Context) -> None:
    with present_errors():
        table = Table("Template", "VMID", "Distribution", "Release", "Desktop")
        for name, template in operations(ctx).templates().items():
            table.add_row(
                name, str(template.vmid), template.distro, template.release, str(template.desktop)
            )
        console.print(table)


@app.command(
    help="Show separate system features, development tools, AI CLI agents, desktop apps and profiles, with supported templates. Reads server definitions without running guest scripts."
)
def bootstrap(
    ctx: typer.Context,
    template: Annotated[
        str | None,
        typer.Option(
            help="Show only choices compatible with this configured template.",
            autocompletion=completion.templates,
        ),
    ] = None,
) -> None:
    with present_errors():
        catalog = operations(ctx).catalog()
        if template is not None and template not in catalog.templates:
            raise VmctlError(f"Unknown template: {template}")
        for title, items in (
            ("System features (--with-system / --without-system)", catalog.system_features),
            (
                "Development modules (--with)",
                tuple(item for item in catalog.modules if item.group == "development"),
            ),
            (
                "AI CLI agents (--with)",
                tuple(item for item in catalog.modules if item.group == "ai-cli"),
            ),
            (
                "AI desktop apps (--with)",
                tuple(item for item in catalog.modules if item.group == "ai-desktop"),
            ),
            ("Bootstrap profiles (--with)", catalog.profiles),
        ):
            table = Table("Name", "Description / composition", "Supported templates", title=title)
            for item in items:
                if template is None or template in item.templates:
                    table.add_row(item.name, item.description, ", ".join(item.templates) or "none")
            console.print(table)


@config_app.command("init", help="Install bundled defaults without overwriting existing files.")
def init(ctx: typer.Context) -> None:
    with present_errors():
        settings: CLISettings = ctx.obj
        result = (
            initialize_config(settings.directory)
            if settings.local
            else initialize_client_config(settings.directory)
        )
        console.print(f"Configuration initialized in {result.directory}")
        console.print(
            f"Created {len(result.created)} files; kept {len(result.kept)} existing files"
        )


@config_app.command(
    "validate",
    help="Validate client settings, worker protocol and server TOML/bootstrap definitions. Does not check live template disks or snippets storage; use create --dry-run for those.",
)
def validate(ctx: typer.Context) -> None:
    with present_errors():
        summary = operations(ctx).validate_config()
        console.print(
            f"Configuration valid: {summary.templates} templates, {summary.presets} presets, {summary.system_features} system features, {summary.modules} optional modules, {summary.profiles} profiles"
        )


if __name__ == "__main__":
    app()
