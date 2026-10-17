"""Typer presentation; domain services can be reused by other frontends."""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.progress import (
    Progress as ConsoleProgress,
)
from rich.table import Table

from vmctl import completion
from vmctl.client_config import (
    default_client_directory,
    initialize_client_config,
)
from vmctl.errors import VmctlError
from vmctl.frontend import (
    FrontendSettings,
    build_operations,
    build_upload_service,
    key_registry,
    load_public_key,
    target_label,
)
from vmctl.models import CloneRequest, CreateRequest, HardwareRequest, SpiceRequest
from vmctl.operations import LifecycleAction, Operations
from vmctl.services.init_config import initialize_config
from vmctl.utils.passwords import hash_desktop_password

if TYPE_CHECKING:
    from vmctl.config import Configuration
    from vmctl.network.allocator import AddressAllocator
    from vmctl.network.dnsmasq import DnsmasqReservations
    from vmctl.proxmox.client import ProxmoxClient
    from vmctl.transfer.service import UploadService

app = typer.Typer(
    no_args_is_help=True,
    help="Manage Proxmox VMs through SSH or directly on the host.",
    pretty_exceptions_show_locals=False,
)
config_app = typer.Typer(
    help="Initialize and validate configuration.", pretty_exceptions_show_locals=False
)
app.add_typer(config_app, name="config")
keys_app = typer.Typer(
    help="Register public SSH keys and add them to selected guests through the worker.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
app.add_typer(keys_app, name="keys")
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


def upload_service(ctx: typer.Context, backend: Operations) -> UploadService:
    return build_upload_service(settings_for(ctx), backend)


def public_key(
    ctx: typer.Context, path: Path | None, *, names: list[str] | None = None
) -> str | None:
    if names is None:
        return load_public_key(settings_for(ctx), path)
    return load_public_key(settings_for(ctx), path, names=names)


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
    key: Annotated[
        list[str] | None,
        typer.Option(
            "--key",
            help="Add a registered public key alongside the primary --ssh-key/client key. Repeat for multiple names. Replaces named defaults for this creation.",
            autocompletion=completion.ssh_keys,
        ),
    ] = None,
    no_extra_keys: Annotated[
        bool,
        typer.Option(
            help="Use only the primary public-key file, omitting registered defaults. Cannot combine with --key."
        ),
    ] = False,
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
            help="Add guest system features, comma-separated, independently of development --with. Template defaults include qemu-agent and, for desktops, desktop-rdp. Opt-in fileshare configures vmctl-files (rclone) access: read shared files, write/delete only in this VM's directory; it requires worker fileshare configuration and does not mount a filesystem. Opt-in agentmemory connects supported installed AI agents to shared memory with individual revocable access; it requires worker agentmemory configuration, and does not install AI agents.",
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
        if no_extra_keys and key:
            raise VmctlError("--key and --no-extra-keys cannot be combined")
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
            public_key=(
                public_key(ctx, ssh_key)
                if key is None and not no_extra_keys
                else public_key(ctx, ssh_key, names=[] if no_extra_keys else key)
            ),
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
            if "fileshare" in plan.system_features:
                console.print(
                    f"Fileshare: {plan.fileshare_endpoint or 'configured SFTP endpoint'}; "
                    "vmctl-files (rclone; no mount)"
                )
                console.print(
                    "Fileshare access: read shared files; write/delete only in a new directory "
                    "identified by VM name, VMID and creation ID. No credentials created by dry-run."
                )
            if "agentmemory" in plan.system_features:
                console.print(
                    f"Shared memory: {plan.agentmemory_endpoint or 'configured HTTPS endpoint'}"
                )
                console.print(
                    "Agent adapters requested: " + (", ".join(plan.agentmemory_adapters) or "none")
                )
                console.print(
                    "Shared read/write access; dry-run creates no credentials or records."
                )
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
        if result.fileshare_endpoint:
            console.print(f"\nFileshare: {result.fileshare_endpoint}")
            console.print(f"  Account: {result.fileshare_account}")
            console.print(f"  Own directory: files:{result.fileshare_directory}")
            console.print(
                "  Access: read shared files; write/delete only in own directory (default)."
            )
            console.print("  Client: vmctl-files (rclone; no mount)")
        if result.agentmemory_endpoint:
            console.print(f"\nShared memory: {result.agentmemory_endpoint}")
            console.print(f"  Client ID: {result.agentmemory_client_id}")
            console.print(
                "  Agent adapters requested: " + (", ".join(result.agentmemory_adapters) or "none")
            )
            console.print(
                "  Access: shared read/write; bootstrap completion has not been verified."
            )
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


@app.command(
    help="Full-clone an existing VM, including non-cloud guests. Copies disks and login state; the new VM stays stopped by default."
)
def clone(
    ctx: typer.Context,
    reference: VMReference,
    name: Annotated[str, typer.Argument(help="Unique name for the duplicate VM.")],
    ip: Annotated[
        str,
        typer.Option(
            help="auto or a free IPv4 in the managed pool.", autocompletion=completion.addresses
        ),
    ] = "auto",
    start: Annotated[
        bool,
        typer.Option(
            "--start/--no-start", help="Start the duplicate after cloning; default is stopped."
        ),
    ] = False,
    shutdown_source: Annotated[
        bool,
        typer.Option(
            help="Temporarily shut down a running source gracefully and restore its running state after copying."
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(help="Inspect the clone plan without changing the source or creating a VM."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", "-y", help="Skip confirmation of copying guest data and any source downtime."
        ),
    ] = False,
) -> None:
    with present_errors():
        backend = operations(ctx)
        preview = backend.plan_clone(CloneRequest(reference, name, ip, start, shutdown_source))
        console.print(f"Full clone: VM {preview.source.vmid} ({preview.source.name}) -> {name}")
        console.print(f"VMID: allocated by Proxmox; IP: {ip}; start duplicate: {start}")
        for note in preview.notes:
            console.print(note)
        if dry_run:
            console.print("Dry run: no VM, reservation or source state changed")
            return
        if not yes:
            typer.confirm(
                f"Duplicate VM {preview.source.vmid} ({preview.source.name}), including its data and login state?",
                abort=True,
            )
        result = backend.clone(preview, request_id=uuid.uuid4().hex, progress=console.print)
        console.print(f"VM cloned successfully: {result.vm.vmid} ({result.vm.name})")
        console.print(f"Status: {result.vm.status}; MAC: {result.mac}; IP: {result.ip}")
        for note in result.notes:
            console.print(note)


@app.command(
    help="Edit an existing VM's total vCPUs and RAM. Running VMs require --restart when resources change; only graceful shutdown is used."
)
def update(
    ctx: typer.Context,
    reference: VMReference,
    cpu: Annotated[
        int | None,
        typer.Option(
            min=1,
            max=512,
            help="Total vCPUs, preserving the VM's socket count.",
            autocompletion=completion.cpu,
        ),
    ] = None,
    memory: Annotated[
        str | None,
        typer.Option(help="RAM size: bare MiB or 16G / 16384M.", autocompletion=completion.memory),
    ] = None,
    restart: Annotated[
        bool,
        typer.Option(
            help="Allow graceful shutdown and start when a running VM needs a hardware change."
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option(help="Show old/new resource values without changing the VM.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip resource change and downtime confirmation.")
    ] = False,
) -> None:
    with present_errors():
        backend = operations(ctx)
        preview = backend.plan_update(HardwareRequest(reference, cpu, memory, restart))
        console.print(
            f"VM {preview.vm.vmid} ({preview.vm.name}): CPU {preview.cpu} -> {preview.new_cpu} vCPU; memory {preview.memory_mib} -> {preview.new_memory_mib} MiB"
        )
        for note in preview.notes:
            console.print(note)
        if dry_run:
            console.print("Dry run: resources and power state unchanged")
            return
        if not yes:
            typer.confirm(
                f"Apply these resources to VM {preview.vm.vmid} ({preview.vm.name})?", abort=True
            )
        result = backend.update(preview, progress=console.print)
        console.print(
            f"VM resources updated: CPU {result.cpu} vCPU; memory {result.memory_mib} MiB; status {result.vm.status}"
        )
        for note in result.notes:
            console.print(note)


@app.command(
    help="Prepare SPICE display and optionally install Linux guest integration through the QEMU guest agent. Compatible VirtIO/QXL displays are preserved."
)
def spice(
    ctx: typer.Context,
    reference: VMReference,
    restart: Annotated[
        bool,
        typer.Option(
            help="Allow graceful shutdown/start if the display must change on a running VM."
        ),
    ] = False,
    guest_tools: Annotated[
        str,
        typer.Option(
            help="auto installs only through an available supported Linux agent; skip changes display only; install requires guest support."
        ),
    ] = "auto",
    dry_run: Annotated[
        bool,
        typer.Option(
            help="Inspect display/guest eligibility without stopping, configuring or executing guest commands."
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip display/guest package and downtime confirmation."),
    ] = False,
) -> None:
    with present_errors():
        if guest_tools not in {"auto", "skip", "install"}:
            raise VmctlError("--guest-tools must be auto, skip or install")
        from typing import Literal, cast

        backend = operations(ctx)
        preview = backend.plan_spice(
            SpiceRequest(reference, restart, cast(Literal["auto", "skip", "install"], guest_tools))
        )
        console.print(
            f"VM {preview.vm.vmid} ({preview.vm.name}): display {preview.current_vga} -> {preview.target_vga}"
        )
        console.print(
            f"Guest tools eligible: {preview.guest_tools_possible}; family: {preview.guest_family or 'unknown'}"
        )
        for note in preview.notes:
            console.print(note)
        if dry_run:
            console.print("Dry run: no display, power or guest package changes")
            return
        if not yes:
            typer.confirm(
                f"Prepare SPICE for VM {preview.vm.vmid} ({preview.vm.name})?", abort=True
            )
        result = backend.spice(preview, progress=console.print)
        console.print(
            f"SPICE display enabled: {result.host_enabled}; display: {result.vga}; guest tools: {result.guest_tools_status}"
        )
        if result.guest_pid is not None:
            console.print(f"Guest process PID: {result.guest_pid}")
        for note in result.notes:
            console.print(note)
        if result.guest_tools_status in {"failed", "uncertain"}:
            raise typer.Exit(1)


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
            help="Browse VMs and preview creation, clone, edit, SPICE and uploads; disable applying changes and file transfers."
        ),
    ] = False,
) -> None:
    with present_errors():
        from vmctl.tui.app import VmctlApp

        settings = settings_for(ctx)
        backend = operations(ctx)
        VmctlApp(
            backend,
            target=target_label(settings),
            public_key=lambda path: load_public_key(settings, path),
            read_only=read_only,
            uploader=upload_service(ctx, backend),
            key_store=key_registry(settings),
        ).run()


@app.command(
    help="Upload a local file or folder directly to a running VM over SSH/SFTP. The selected basename is appended to --to; private keys and file contents stay off the Proxmox worker."
)
def upload(
    ctx: typer.Context,
    reference: VMReference,
    source: Annotated[
        Path,
        typer.Argument(
            exists=True,
            readable=True,
            help="Local file or directory; symlinks and special files are rejected.",
        ),
    ],
    destination: Annotated[
        str,
        typer.Option(
            "--to",
            help="Existing guest directory, e.g. /home/vmadmin or ~. The source name is retained.",
        ),
    ],
    username: Annotated[
        str | None, typer.Option("--user", help="Guest SSH user; default is VM metadata/config.")
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option(
            help="Allow atomic replacement of one existing regular file; never merge folders."
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            help="Inspect VM, local source and guest destination through read-only SFTP; send no files."
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation of the frozen transfer target.")
    ] = False,
) -> None:
    from vmctl.transfer.models import UploadCancelledError, UploadProgress, UploadRequest

    with present_errors():
        service = upload_service(ctx, operations(ctx))
        preview = service.plan(UploadRequest(reference, source, destination, username, overwrite))
        console.print(
            f"VM: {preview.vm.vmid} ({preview.vm.name}); SSH: {preview.username}@{preview.ip}"
        )
        console.print(f"Source: {preview.source}\nDestination: {preview.destination}")
        console.print(
            f"Files: {preview.file_count}; bytes: {preview.total_bytes}; overwrite: {overwrite}"
        )
        for note in preview.notes:
            console.print(note)
        if dry_run:
            console.print("Dry run: no guest files or VM configuration changed")
            return
        if not yes:
            typer.confirm(f"Upload to VM {preview.vm.vmid} at {preview.destination}?", abort=True)
        cancelled = Event()
        with ConsoleProgress(
            TextColumn("{task.description}", markup=False),
            BarColumn(),
            TaskProgressColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as display:
            task = display.add_task("Uploading", total=preview.total_bytes or None)

            def progressed(event: UploadProgress) -> None:
                display.update(task, completed=event.sent_bytes, description=event.current_path)

            try:
                result = service.upload(preview, progress=progressed, cancel=cancelled)
            except UploadCancelledError as error:
                errors.print(str(error))
                raise typer.Exit(130) from None
            except KeyboardInterrupt:
                cancelled.set()
                raise typer.Exit(130) from None
        console.print(
            f"Upload completed: {result.file_count} files, {result.bytes_sent} bytes -> {result.destination}"
        )
        for note in result.notes:
            console.print(note)


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
        table.add_row(
            "SPICE display", "enabled (configuration)" if details.spice_enabled else "disabled"
        )
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
        if "fileshare" in details.system_features:
            table.add_row("Fileshare", details.metadata.get("fileshare_endpoint", "unknown"))
            table.add_row("Fileshare account", details.metadata.get("fileshare_account", "unknown"))
            table.add_row(
                "Fileshare own directory",
                "files:" + details.metadata.get("fileshare_directory", "unknown"),
            )
            table.add_row(
                "Fileshare access",
                "read shared; write/delete own directory (default; server policy may differ)",
            )
        if "agentmemory" in details.system_features:
            table.add_row("Shared memory", details.metadata.get("agentmemory_endpoint", "unknown"))
            table.add_row(
                "Memory client ID", details.metadata.get("agentmemory_client_id", "unknown")
            )
            table.add_row(
                "Agent adapters requested",
                details.metadata.get("agentmemory_adapters", "") or "none",
            )
            table.add_row("Memory access", "shared read/write; guest setup not verified")
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


@keys_app.command(
    "list", help="List registered public keys, fingerprints and creation defaults. Runs locally."
)
def keys_list(ctx: typer.Context) -> None:
    from vmctl.keys import key_fingerprint

    with present_errors():
        registry = key_registry(settings_for(ctx)).load()
        table = Table("Name")
        table.add_column("Fingerprint", overflow="fold")
        table.add_column("Default")
        table.add_column("Description")
        for name, entry in sorted(registry.keys.items()):
            table.add_row(
                name,
                key_fingerprint(entry.public_key),
                "yes" if name in registry.default_keys else "no",
                entry.description,
            )
        console.print(table)
        if not registry.keys:
            console.print("No registered keys. Add one with: vmctl keys add NAME PUBLIC_KEY_FILE")


@keys_app.command(
    "add",
    help="Register a named public-key file atomically. Private keys are rejected; no VM is changed.",
)
def keys_add(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Registry name, for example iphone or workstation.")],
    path: Annotated[Path, typer.Argument(help="Public-key file on this computer.")],
    description: Annotated[str, typer.Option(help="Description displayed in key selection.")] = "",
    default: Annotated[
        bool,
        typer.Option(
            "--default",
            help="Also include this key in new VMs by default, alongside the primary key.",
        ),
    ] = False,
) -> None:
    from vmctl.keys import key_fingerprint
    from vmctl.utils.ssh_keys import read_public_key

    with present_errors():
        content = read_public_key(path.expanduser())
        key_registry(settings_for(ctx)).add(name, content, description=description, default=default)
        console.print(f"Registered public key {name}: {key_fingerprint(content)}")
        if default:
            console.print("Included in future VM creation defaults; existing VMs are unchanged")


@keys_app.command("show", help="Print a registered public key. No private-key material is stored.")
def keys_show(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(autocompletion=completion.ssh_keys)],
) -> None:
    with present_errors():
        console.print(key_registry(settings_for(ctx)).resolve([name]), end="", soft_wrap=True)


@keys_app.command(
    "remove",
    help="Remove a local registry entry and its default selection. Does not revoke existing VM access.",
)
def keys_remove(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(autocompletion=completion.ssh_keys)],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip registry removal confirmation.")
    ] = False,
) -> None:
    with present_errors():
        store = key_registry(settings_for(ctx))
        store.resolve([name])
        if not yes:
            typer.confirm(
                f"Remove {name} from the local registry? Existing VM access stays unchanged",
                abort=True,
            )
        store.remove(name)
        console.print("Removed registry entry only; existing VM access is unchanged")


@keys_app.command(
    "export",
    help="Add selected public keys to a running Linux VM through its QEMU guest agent. Preserves existing keys; no guest SSH password or private key is needed.",
)
def keys_export(
    ctx: typer.Context,
    reference: Annotated[
        str, typer.Argument(help="VM name or VMID.", autocompletion=completion.references)
    ],
    names: Annotated[
        list[str],
        typer.Argument(
            help="One or more registered key names.", autocompletion=completion.ssh_keys
        ),
    ],
    user: Annotated[
        str | None,
        typer.Option(
            help="Existing non-root guest account; defaults to the configured guest user."
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option(help="Check VM/guest-agent eligibility without changing guest files.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip granting SSH access confirmation.")
    ] = False,
) -> None:
    from vmctl.models import KeyExportRequest

    with present_errors():
        content = key_registry(settings_for(ctx)).resolve(names)
        backend = operations(ctx)
        preview = backend.plan_key_export(KeyExportRequest(reference, content, user))
        console.print(
            f"VM {preview.vm.vmid} ({preview.vm.name}): add public keys {', '.join(names)}"
        )
        console.print(f"Guest account: {preview.request.username or 'configured guest user'}")
        for note in preview.notes:
            console.print(note)
        if dry_run:
            console.print("Dry run: no guest files or SSH access changed")
            return
        if not yes:
            typer.confirm(
                f"Grant SSH access on {preview.vm.name} using these public keys?", abort=True
            )
        result = backend.export_keys(preview, progress=console.print)
        console.print(
            f"Public keys exported to {result.username}@{result.vm.name}: {result.added} added, {result.unchanged} already authorized"
        )


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
