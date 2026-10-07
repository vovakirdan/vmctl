"""Typed local qm/pvesh/pvesm operations."""

import re
import socket
from collections.abc import Callable, Mapping
from pathlib import Path

from vmctl.config import Configuration
from vmctl.errors import VmctlError
from vmctl.models import VM
from vmctl.proxmox.parser import parse_config, parse_json, parse_vms
from vmctl.utils.subprocess import CommandResult, Runner


class ProxmoxClient:
    def __init__(self, config: Configuration, runner: Runner) -> None:
        self.config = config
        self.runner = runner
        self.node = config.host.proxmox.node or socket.gethostname().split(".")[0]

    def command(
        self,
        binary: str,
        *args: str,
        timeout: int | None = None,
        sensitive: tuple[str, ...] = (),
        stream: Callable[[str], None] | None = None,
    ) -> CommandResult:
        return self.runner.run(
            [self.config.host.binaries.get(binary, binary), *args],
            timeout=timeout or self.config.host.proxmox.command_timeout,
            sensitive=sensitive,
            stream=stream,
        )

    def inventory(self) -> list[VM]:
        return parse_vms(
            self.command(
                "pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"
            ).stdout
        )

    def list_vms(self) -> list[VM]:
        return [vm for vm in self.inventory() if vm.node == self.node]

    def resolve(self, reference: str) -> VM:
        matches = [
            vm
            for vm in self.list_vms()
            if (vm.vmid == int(reference) if reference.isdecimal() else vm.name == reference)
        ]
        if not matches:
            raise VmctlError(f"No local VM matches {reference!r}")
        if len(matches) > 1:
            raise VmctlError(f"Ambiguous VM name {reference!r}; use a VMID")
        return matches[0]

    def vm_config(self, vmid: int) -> dict[str, str]:
        return parse_config(self.command("qm", "config", str(vmid)).stdout)

    def status(self, vmid: int) -> str:
        status = parse_config(self.command("qm", "status", str(vmid)).stdout).get("status", "")
        if not re.fullmatch(r"[a-z][a-z0-9-]*", status):
            raise VmctlError("Proxmox returned an invalid VM status")
        return status

    def next_id(self) -> int:
        args = ["get", "/cluster/nextid", "--output-format", "json"]
        value = parse_json(self.command("pvesh", *args).stdout)
        try:
            vmid = int(value)
        except (ValueError, TypeError) as exc:
            raise VmctlError("Proxmox returned an invalid next VMID") from exc
        if 9000 <= vmid <= 9099:
            occupied = {vm.vmid for vm in self.inventory()}
            candidate = 9099 + 1
            while candidate in occupied:
                candidate += 1
            # With --vmid Proxmox validates a candidate; it must still approve the ID.
            value = parse_json(self.command("pvesh", *args, "--vmid", str(candidate)).stdout)
            try:
                vmid = int(value)
            except (ValueError, TypeError) as exc:
                raise VmctlError("Proxmox returned an invalid next VMID") from exc
        if vmid < 100 or 9000 <= vmid <= 9099:
            raise VmctlError("Proxmox returned a reserved or invalid VMID")
        if any(vm.vmid == vmid for vm in self.inventory()):
            raise VmctlError(f"VMID {vmid} is already in use; retry the operation")
        return vmid

    def clone(
        self,
        template: int,
        vmid: int,
        name: str,
        description: str,
        stream: Callable[[str], None] | None = None,
    ) -> None:
        args = [
            "clone",
            str(template),
            str(vmid),
            "--full",
            "1",
            "--name",
            name,
            "--description",
            description,
        ]
        if self.config.host.proxmox.clone_storage:
            args += ["--storage", self.config.host.proxmox.clone_storage]
        self.command("qm", *args, timeout=self.config.host.proxmox.clone_timeout, stream=stream)

    def configure(
        self, vmid: int, options: Mapping[str, str], *, delete: tuple[str, ...] = ()
    ) -> None:
        args = ["set", str(vmid)]
        for key, value in options.items():
            args += [f"--{key}", value]
        if delete:
            args += ["--delete", ",".join(delete)]
        self.command("qm", *args)

    def resize(self, vmid: int, device: str, size_gib: int) -> None:
        self.command("qm", "disk", "resize", str(vmid), device, f"{size_gib}G")

    def start(self, vmid: int) -> None:
        self.command("qm", "start", str(vmid))

    def stop(self, vmid: int) -> None:
        self.command("qm", "stop", str(vmid), timeout=self.config.host.proxmox.stop_timeout)

    def shutdown(self, vmid: int) -> None:
        timeout = self.config.host.proxmox.stop_timeout
        self.command(
            "qm",
            "shutdown",
            str(vmid),
            "--timeout",
            str(timeout),
            "--forceStop",
            "0",
            timeout=timeout + self.config.host.proxmox.command_timeout,
        )

    def reboot(self, vmid: int) -> None:
        timeout = self.config.host.proxmox.stop_timeout
        # Proxmox's graceful reboot fails on timeout; it does not force a stop.
        self.command(
            "qm",
            "reboot",
            str(vmid),
            "--timeout",
            str(timeout),
            timeout=timeout + self.config.host.proxmox.command_timeout,
        )

    def destroy(self, vmid: int) -> None:
        self.command("qm", "destroy", str(vmid), timeout=self.config.host.proxmox.clone_timeout)

    def refresh_cloudinit(self, vmid: int) -> None:
        self.command("qm", "cloudinit", "update", str(vmid))

    def snippet_path(self, filename: str, *, storage: str | None = None) -> tuple[Path, str]:
        storage = storage or self.config.host.proxmox.snippets_storage
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", storage):
            raise VmctlError("Invalid snippets storage ID")
        data = parse_json(
            self.command("pvesh", "get", f"/storage/{storage}", "--output-format", "json").stdout
        )
        if not isinstance(data, dict) or "snippets" not in str(data.get("content", "")).split(","):
            raise VmctlError(
                f"Storage {storage!r} must have snippets enabled for custom cloud-init. "
                f"On Proxmox, open Datacenter > Storage > {storage} > Edit > Content, "
                "add Snippets and keep all currently selected content types. "
                "Alternatively, set [proxmox].snippets_storage in the server config.toml "
                "to an available directory storage with snippets enabled. "
                "Then retry create --dry-run. vmctl will not change storage configuration"
            )
        if data.get("type") != "dir":
            raise VmctlError("MVP snippets storage must be a directory storage")
        if data.get("disable") or (
            data.get("nodes") and self.node not in str(data["nodes"]).split(",")
        ):
            raise VmctlError(f"Snippets storage {storage!r} is not available on this node")
        status = parse_json(
            self.command(
                "pvesh",
                "get",
                f"/nodes/{self.node}/storage/{storage}/status",
                "--output-format",
                "json",
            ).stdout
        )
        if not isinstance(status, dict) or not status.get("active"):
            raise VmctlError(f"Snippets storage {storage!r} is not active")
        volume = f"{storage}:snippets/{filename}"
        path = Path(self.command("pvesm", "path", volume).stdout.strip())
        if not path.is_absolute() or path.name != filename:
            raise VmctlError("pvesm returned an invalid snippet path")
        return path, volume
