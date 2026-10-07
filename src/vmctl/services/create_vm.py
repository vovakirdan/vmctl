"""Preflight, creation and conservative compensation of invocation-owned resources."""

import socket
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from vmctl.bootstrap.renderer import BootstrapExecutor
from vmctl.bootstrap.system import select_bootstrap
from vmctl.config import Configuration
from vmctl.errors import CreationError, RepeatedRequestError, VmctlError
from vmctl.models import CreatePlan, CreateRequest, CreateResult, validate_name
from vmctl.network.allocator import AddressAllocator
from vmctl.network.dnsmasq import DnsmasqReservations, Reservation
from vmctl.proxmox.client import ProxmoxClient
from vmctl.proxmox.parser import (
    decode_metadata,
    disk_gib_exact,
    encode_metadata,
    network_mac,
    select_disk,
)
from vmctl.utils.atomic import atomic_remove, atomic_write, host_lock
from vmctl.utils.ssh_keys import read_public_key, validate_public_key


def wait_for_ssh(address: str, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                (address, 22), timeout=min(2, max(0.01, deadline - time.monotonic()))
            ) as connection:
                connection.settimeout(min(2, max(0.01, deadline - time.monotonic())))
                if connection.recv(256).startswith(b"SSH-"):
                    return True
        except OSError:
            pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1, remaining))
    return False


class CreateVMService:
    def __init__(
        self,
        config: Configuration,
        client: ProxmoxClient,
        reservations: DnsmasqReservations,
        allocator: AddressAllocator,
        bootstrap: BootstrapExecutor,
    ) -> None:
        self.config, self.client = config, client
        self.reservations, self.allocator, self.bootstrap = reservations, allocator, bootstrap

    def plan(self, request: CreateRequest) -> CreatePlan:
        validate_name(request.name)
        if request.wait_seconds < 0 or (request.wait_seconds and not request.start):
            raise VmctlError("--wait requires --start and a nonnegative timeout")
        template = self.config.template(request.template)
        resources = self.config.resources(request)
        public_key = (
            validate_public_key(request.public_key)
            if request.public_key is not None
            else read_public_key(self.config.path(self.config.host.ssh_key))
        )
        selection = select_bootstrap(self.config, request)
        data = self.bootstrap.render(
            template,
            request.name,
            public_key,
            selection.modules,
            request.versions,
            system_features=selection.system_features,
            desktop_password_hash=request.desktop_password_hash,
        )
        inventory = self.client.inventory()
        if any(vm.name == request.name for vm in inventory):
            raise VmctlError(f"VM name {request.name!r} already exists")
        source = next((vm for vm in inventory if vm.vmid == template.vmid), None)
        if source is None or source.node != self.client.node or not source.template:
            raise VmctlError(
                f"Template {template.vmid} must exist on the local node and be a template"
            )
        source_config = self.client.vm_config(template.vmid)
        if not any(
            "cloudinit" in value
            for key, value in source_config.items()
            if key.startswith(("ide", "sata", "scsi"))
        ):
            raise VmctlError("Template requires an existing cloud-init drive")
        if "lock" in source_config:
            raise VmctlError("Template is locked")
        if any(key in source_config for key in ("args", "hookscript")) or any(
            key.startswith("hostpci") for key in source_config
        ):
            raise VmctlError(
                "Templates with custom QEMU args, hooks or PCI passthrough require manual review"
            )
        disk = select_disk(source_config, template.disk_device)
        disk_size = disk_gib_exact(source_config, disk)
        self.client.snippet_path("vmctl-preflight.yaml")
        self.reservations.preflight()
        # Do not allocate or retain a reservation during a read-only preflight.
        self.allocator.select([entry.ip for entry in self.reservations.read()], request.ip)
        encode_metadata(request.description, {})
        return CreatePlan(
            request,
            template.vmid,
            resources,
            disk,
            int(disk_size.to_integral_value(rounding="ROUND_CEILING")),
            public_key,
            data,
            tuple(module.name for module in selection.modules),
            tuple(feature.name for feature in selection.system_features),
            selection.desktop,
            selection.requires_desktop_password,
        )

    def create(
        self,
        request: CreateRequest,
        *,
        progress: Callable[[str], None] | None = None,
        request_id: str | None = None,
    ) -> CreateResult:
        token = request_id or uuid.uuid4().hex
        if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
            raise VmctlError("Invalid creation request ID")
        with host_lock(self.config.path(self.config.host.lock_file), self.config.host.lock_timeout):
            for existing in self.client.inventory():
                if existing.node == self.client.node and not existing.template:
                    metadata = decode_metadata(
                        self.client.vm_config(existing.vmid).get("description", "")
                    )
                    if metadata.get("invocation") == token:
                        raise RepeatedRequestError(token, existing.vmid)
            plan = self.plan(request)
            if plan.requires_desktop_password and request.desktop_password_hash is None:
                raise VmctlError(
                    "Desktop password is required; provide a hash through the password helper or use --no-desktop-password"
                )
            vmid = self.client.next_id()
            snippet, volume = self.client.snippet_path(f"vmctl-{vmid}-{token}.yaml")
            metadata = {
                "invocation": token,
                "template": request.template,
                "preset": request.preset,
                "snippet": volume,
                "system_features": ",".join(plan.system_features),
                "modules": ",".join(plan.modules),
                "desktop": "true" if plan.desktop else "false",
                "username": self.config.host.username,
            }
            description = encode_metadata(request.description, metadata)
            if progress is not None:
                progress(f"Allocated VMID: {vmid}")
            cloned = False
            mac: str | None = None
            reservation_attempted = False
            snippet_written = False
            try:
                self.client.clone(plan.template_vmid, vmid, request.name, description, progress)
                cloned = True
                config = self.client.vm_config(vmid)
                if decode_metadata(config.get("description", "")).get("invocation") != token:
                    raise VmctlError("Clone ownership could not be verified")
                inherited = tuple(
                    key
                    for key in config
                    if (key.startswith("net") and key != "net0")
                    or key.startswith("ipconfig")
                    or key in {"cicustom", "cipassword", "sshkeys"}
                )
                self.client.configure(
                    vmid,
                    {
                        "cores": str(plan.resources.cpu),
                        "sockets": "1",
                        "memory": str(plan.resources.memory_mib),
                        "balloon": "0",
                        "description": description,
                        "net0": f"virtio,bridge={self.config.host.network.bridge}",
                        "onboot": "0",
                    },
                    delete=inherited,
                )
                config = self.client.vm_config(vmid)
                size = disk_gib_exact(config, plan.disk_device)
                if size < Decimal(plan.resources.disk_gib):
                    self.client.resize(vmid, plan.disk_device, plan.resources.disk_gib)
                actual = max(
                    plan.resources.disk_gib, int(size.to_integral_value(rounding="ROUND_CEILING"))
                )
                mac = network_mac(config.get("net0", ""))
                if any(entry.mac == mac for entry in self.reservations.read()):
                    raise VmctlError("Generated MAC conflicts with an existing reservation")
                address = self.allocator.select(
                    [entry.ip for entry in self.reservations.read()], request.ip
                )
                reservation_attempted = True
                self.reservations.add(Reservation(mac, address, request.name, vmid))
                # Treat an atomic-write error after replace as a possibly created snippet.
                if snippet.exists() or snippet.is_symlink():
                    raise VmctlError(f"Snippet already exists: {snippet}")
                snippet_written = True
                atomic_write(snippet, plan.user_data.encode(), mode=0o600)
                # qm's CLI expects a public-key filename, unlike the API's sshkeys property.
                with tempfile.TemporaryDirectory(prefix="vmctl-key-") as key_directory:
                    key_file = Path(key_directory) / "public.pub"
                    atomic_write(key_file, plan.public_key.encode())
                    self.client.configure(
                        vmid,
                        {
                            "ciuser": self.config.host.username,
                            "sshkeys": str(key_file),
                            "ipconfig0": "ip=dhcp",
                            "ciupgrade": "0",
                            "agent": "enabled=1"
                            if "qemu-agent" in plan.system_features
                            else "enabled=0",
                            "cicustom": f"user={volume}",
                        },
                    )
                self.client.refresh_cloudinit(vmid)
                if request.start:
                    self.client.start(vmid)
            except (VmctlError, OSError, KeyboardInterrupt) as exc:
                failures = self._rollback(
                    vmid,
                    token,
                    cloned,
                    mac if reservation_attempted else None,
                    snippet if snippet_written else None,
                )
                details = (
                    "; ".join(failures) if failures else "Invocation-owned resources cleaned up"
                )
                raise CreationError(
                    f"VM {vmid} creation failed: {exc or 'Interrupted'}. {details}"
                ) from exc
        # Waiting does not hold the mutation lock and does not trigger destructive compensation.
        readiness = "not checked"
        if request.wait_seconds:
            readiness = (
                "SSH available"
                if wait_for_ssh(str(address), request.wait_seconds)
                else "SSH readiness timed out; VM retained"
            )
        return CreateResult(
            vmid,
            request.name,
            request.template,
            request.preset,
            replace(plan.resources, disk_gib=actual),
            mac,
            address,
            self.config.host.username,
            request.start,
            plan.modules,
            readiness,
            plan.system_features,
            plan.desktop,
            plan.desktop and "desktop-rdp" in plan.system_features,
        )

    def _rollback(
        self, vmid: int, token: str, cloned: bool, mac: str | None, snippet: Path | None
    ) -> list[str]:
        if not cloned:
            return [
                f"Clone outcome is uncertain; inspect VMID {vmid} and Proxmox tasks before cleanup"
            ]
        try:
            config = self.client.vm_config(vmid)
            if decode_metadata(config.get("description", "")).get("invocation") != token:
                return [f"VM {vmid} retained: invocation ownership could not be verified"]
            vm = self.client.resolve(str(vmid))
            if vm.status != "stopped":
                self.client.stop(vmid)
            self.client.destroy(vmid)
            if any(vm.vmid == vmid for vm in self.client.inventory()):
                raise VmctlError("VM still appears in Proxmox inventory")
        except (VmctlError, OSError) as exc:
            return [
                f"VM {vmid} and its reservation/snippet retained: cleanup could not be confirmed: {exc}"
            ]
        failures: list[str] = []
        if mac is not None:
            try:
                self.reservations.remove_mac(mac)
            except (VmctlError, OSError) as exc:
                failures.append(f"Reservation for {mac} needs cleanup: {exc}")
        if snippet is not None:
            try:
                atomic_remove(snippet)
            except (VmctlError, OSError) as exc:
                failures.append(f"Snippet {snippet} needs cleanup: {exc}")
        return failures
