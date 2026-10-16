"""Guarded full clones of existing guests without replaying their bootstrap."""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, replace
from ipaddress import IPv4Address, IPv4Interface
from pathlib import Path

import yaml

from vmctl.config import Configuration
from vmctl.errors import (
    CommandError,
    CreationError,
    RepeatedRequestError,
    UncertainOperationError,
    VmctlError,
)
from vmctl.models import VM, CloneRequest, validate_name
from vmctl.network.allocator import AddressAllocator
from vmctl.network.dnsmasq import DnsmasqReservations, Reservation
from vmctl.operations import ClonePreview, CloneResult
from vmctl.proxmox.client import ProxmoxClient
from vmctl.proxmox.parser import decode_metadata, encode_metadata, network_mac, parse_properties
from vmctl.utils.atomic import atomic_create, atomic_remove, host_lock


class CloneVMService:
    def __init__(
        self,
        config: Configuration,
        client: ProxmoxClient,
        reservations: DnsmasqReservations,
        allocator: AddressAllocator,
    ) -> None:
        self.config, self.client = config, client
        self.reservations, self.allocator = reservations, allocator

    def plan(self, request: CloneRequest) -> ClonePreview:
        validate_name(request.name)
        vm = self.client.resolve(request.reference)
        config = self.client.vm_config(vm.vmid)
        if vm.template or config.get("template") == "1" or 9000 <= vm.vmid <= 9099:
            raise VmctlError(
                "Clone requires an existing VM; use create for configured cloud templates"
            )
        metadata = decode_metadata(config.get("description", ""))
        if metadata.get("fileshare_account") or "fileshare" in metadata.get(
            "system_features", ""
        ).split(","):
            raise VmctlError(
                "Cloning a fileshare-enabled VM would copy its private access key; create a fresh VM instead"
            )
        if any(existing.name == request.name for existing in self.client.inventory()):
            raise VmctlError(f"VM name {request.name!r} already exists")
        if "lock" in config:
            raise VmctlError("VM is locked; resolve the active Proxmox task first")
        pending = self.client.pending(vm.vmid)
        if not isinstance(pending, list) or any(not isinstance(row, dict) for row in pending):
            raise VmctlError("Cannot verify pending VM configuration")
        if any("pending" in row or row.get("delete") for row in pending):
            raise VmctlError("VM has pending configuration changes; apply or revert them first")
        self._validate_hardware(config)
        vm = replace(vm, name=config.get("name", vm.name), status=self.client.status(vm.vmid))
        if vm.status not in {"running", "stopped"}:
            raise VmctlError(f"Cannot clone VM {vm.vmid} while its status is {vm.status!r}")
        if vm.status == "running" and not request.shutdown_source:
            raise VmctlError(
                "Source VM is running; shut it down or explicitly allow shutdown_source"
            )
        cloud_init = self._cloud_init(vm.vmid, config)
        if cloud_init:
            self.client.snippet_path("vmctl-clone-preflight.yaml")
        self.reservations.preflight()
        self.allocator.select(self._occupied(config), request.ip)
        fingerprint = self._fingerprint(vm, request, config)
        notes = [
            "The full clone copies installed software, accounts, credentials and guest identity files.",
            "The clone receives a new VMID, MAC and DHCP reservation on the private bridge.",
            "Guest-configured static IPs and application identities require manual adjustment before starting.",
        ]
        if vm.status == "running":
            notes.append("The source will be shut down gracefully and restarted after copying.")
        if cloud_init:
            notes.append(
                "Fresh cloud-init data sets only hostname and DHCP; source bootstrap is not replayed."
            )
        return ClonePreview(vm, request, fingerprint, cloud_init, tuple(notes))

    @staticmethod
    def _fingerprint(vm: VM, request: CloneRequest, config: dict[str, str]) -> str:
        return hashlib.sha256(
            json.dumps([asdict(vm), asdict(request), config], sort_keys=True).encode()
        ).hexdigest()

    def _occupied(self, source_config: dict[str, str]) -> list[IPv4Address]:
        addresses = [entry.ip for entry in self.reservations.read()]
        address = parse_properties(source_config.get("ipconfig0", "")).get("ip", "")
        if address not in {"", "dhcp", "manual"}:
            try:
                addresses.append(IPv4Interface(address).ip)
            except ValueError as exc:
                raise VmctlError("Source has an invalid cloud-init IPv4 address") from exc
        return addresses

    def _validate_hardware(self, config: dict[str, str]) -> None:
        unsafe = {"args", "hookscript", "ivshmem", "vmstate"}
        if any(
            key in unsafe
            or key.startswith(("hostpci", "usb", "virtiofs", "parallel", "unused"))
            or (key.startswith("serial") and value != "socket")
            for key, value in config.items()
        ):
            raise VmctlError(
                "VM has passthrough, external resources or hooks requiring manual review"
            )
        networks = [key for key in config if re.fullmatch(r"net[0-9]+", key)]
        if networks != ["net0"]:
            raise VmctlError("Clone supports exactly one private network interface, net0")
        network = parse_properties(config["net0"])
        if network.get("bridge") != self.config.host.network.bridge:
            raise VmctlError("Source net0 must use the configured private bridge")
        if network.get("tag") or network.get("trunks"):
            raise VmctlError("Tagged or trunk network interfaces require manual cloning")
        network_mac(config["net0"])
        disks = [
            (key, parse_properties(value))
            for key, value in config.items()
            if re.fullmatch(r"(?:(?:scsi|virtio|sata|ide)[0-9]+|efidisk0|tpmstate0)", key)
        ]
        if not disks:
            raise VmctlError("VM has no cloneable storage disks")
        for key, properties in disks:
            volume = properties.get("value", "")
            if volume == "none" and properties.get("media") == "cdrom":
                continue
            if not re.fullmatch(r"[a-zA-Z0-9_-]+:[a-zA-Z0-9_./-]+", volume) or ".." in volume:
                raise VmctlError(f"Disk {key} is external or has an unsupported storage volume")
            if properties.get("shared") == "1":
                raise VmctlError(f"Shared disk {key} requires manual cloning")

    def _cloud_init(self, vmid: int, config: dict[str, str]) -> bool:
        cloud_init = any(
            "cloudinit" in value
            for key, value in config.items()
            if re.fullmatch(r"(?:scsi|virtio|sata|ide)[0-9]+", key)
        )
        custom = config.get("cicustom", "")
        if custom:
            properties = parse_properties(custom)
            metadata = decode_metadata(config.get("description", ""))
            token, volume = metadata.get("invocation", ""), metadata.get("snippet", "")
            storage, _, filename = volume.partition(":snippets/")
            if (
                not cloud_init
                or set(properties) != {"user"}
                or properties["user"] != volume
                or custom != f"user={volume}"
                or not re.fullmatch(r"[0-9a-f]{32}", token)
                or not re.fullmatch(r"[a-zA-Z0-9_-]+", storage)
                or filename != f"vmctl-{vmid}-{token}.yaml"
            ):
                raise VmctlError(
                    "Custom cloud-init must be vmctl-owned user-data only; foreign payloads cannot be cloned safely"
                )
        return cloud_init

    @staticmethod
    def _verify_owned_disks(vmid: int, config: dict[str, str]) -> None:
        for key, value in config.items():
            if not re.fullmatch(r"(?:(?:scsi|virtio|sata|ide)[0-9]+|efidisk0|tpmstate0)", key):
                continue
            properties = parse_properties(value)
            if properties.get("media") == "cdrom" and "cloudinit" not in properties.get(
                "value", ""
            ):
                continue
            _, separator, volume = properties.get("value", "").partition(":")
            if not separator or not re.fullmatch(rf"(?:{vmid}/)?vm-{vmid}-[a-zA-Z0-9_.-]+", volume):
                raise VmctlError(f"Disk {key} ownership could not be verified for clone VM {vmid}")

    def clone(
        self,
        expected: ClonePreview,
        *,
        request_id: str,
        progress: Callable[[str], None] | None = None,
    ) -> CloneResult:
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise VmctlError("Invalid clone request ID")
        with host_lock(self.config.path(self.config.host.lock_file), self.config.host.lock_timeout):
            self._check_request_id(request_id)
            current = self.plan(expected.request)
            if current != expected:
                raise VmctlError("Source VM changed since confirmation; inspect it and retry")
            source_config = self.client.vm_config(current.source.vmid)
            if (
                self._fingerprint(current.source, current.request, source_config)
                != current.fingerprint
            ):
                raise VmctlError("Source VM changed since confirmation; inspect it and retry")
            vmid = self.client.next_id()
            if progress:
                progress(f"Allocated VMID: {vmid}")
            snippet: Path | None = None
            volume = ""
            if current.cloud_init:
                snippet, volume = self.client.snippet_path(f"vmctl-{vmid}-{request_id}.yaml")
                if snippet.exists() or snippet.is_symlink():
                    raise VmctlError(f"Snippet already exists: {snippet}")
            metadata = {
                key: value
                for key, value in decode_metadata(source_config.get("description", "")).items()
                if key
                in {"template", "preset", "system_features", "modules", "desktop", "username"}
            }
            metadata.update(invocation=request_id, clone_of=str(current.source.vmid))
            if volume:
                metadata["snippet"] = volume
            description = encode_metadata(f"Clone of VM {current.source.vmid}", metadata)
            cloned = False
            clone_submitted = False
            phase = "source shutdown"
            reservation_mac: str | None = None
            snippet_attempted = False
            source_stopped = False
            source_restored = current.source.status == "stopped"
            notes: list[str] = []
            try:
                if current.source.status == "running":
                    self.client.shutdown(current.source.vmid)
                    source_stopped = True
                    try:
                        stopped = self.client.status(current.source.vmid) == "stopped"
                    except VmctlError as exc:
                        raise UncertainOperationError(
                            current.source.vmid, "source state verification after shutdown"
                        ) from exc
                    if not stopped:
                        raise VmctlError("Source did not stop; no clone was submitted")
                if self.client.vm_config(current.source.vmid) != source_config:
                    raise VmctlError("Source configuration changed before cloning")
                if self.client.status(current.source.vmid) != "stopped":
                    raise VmctlError("Source is no longer stopped; no clone was submitted")
                if progress:
                    progress(f"Full clone: VM {current.source.vmid} -> {vmid}")
                phase = "full clone"
                clone_submitted = True
                self.client.clone(
                    current.source.vmid, vmid, current.request.name, description, progress
                )
                cloned = True
                phase = "clone configuration"
                clone_config = self.client.vm_config(vmid)
                if (
                    decode_metadata(clone_config.get("description", "")).get("invocation")
                    != request_id
                ):
                    raise VmctlError("Clone ownership could not be verified")
                self._validate_hardware(clone_config)
                self._verify_owned_disks(vmid, clone_config)
                if self.client.status(vmid) != "stopped":
                    raise VmctlError("Clone is no longer stopped; no configuration was applied")
                mac = network_mac(clone_config.get("net0", ""))
                if mac == network_mac(source_config["net0"]) or any(
                    entry.mac == mac for entry in self.reservations.read()
                ):
                    raise VmctlError(
                        "Generated clone MAC conflicts with the source or an existing reservation"
                    )
                options = {"onboot": "0"}
                inherited = tuple(
                    key
                    for key in clone_config
                    if key.startswith("ipconfig") or key in {"cipassword", "sshkeys", "ciuser"}
                )
                if current.cloud_init:
                    options.update(ipconfig0="ip=dhcp", ciupgrade="0", cicustom=f"user={volume}")
                    inherited = tuple(key for key in inherited if key != "ipconfig0")
                self.client.configure(vmid, options, delete=inherited)
                address = self.allocator.select(self._occupied(source_config), current.request.ip)
                reservation_mac = mac
                self.reservations.add(Reservation(mac, address, current.request.name, vmid))
                if snippet is not None:
                    snippet_attempted = True
                    data = "#cloud-config\n" + yaml.safe_dump(
                        {
                            "hostname": current.request.name,
                            "preserve_hostname": False,
                            "manage_etc_hosts": True,
                            "package_update": False,
                            "package_upgrade": False,
                        },
                        sort_keys=False,
                    )
                    if not atomic_create(snippet, data.encode(), mode=0o600):
                        snippet_attempted = False
                        raise VmctlError(f"Snippet already exists: {snippet}")
                    phase = "clone cloud-init refresh"
                    self.client.refresh_cloudinit(vmid)
                if source_stopped:
                    phase = "source restart"
                    source_restored, note = self._restore_source(current, source_config)
                    if note:
                        notes.append(note)
                    source_stopped = False
                phase = "clone verification"
                actual = self.client.vm_config(vmid)
                self._validate_hardware(actual)
                self._verify_owned_disks(vmid, actual)
                if (
                    decode_metadata(actual.get("description", "")).get("invocation") != request_id
                    or "lock" in actual
                    or network_mac(actual.get("net0", "")) != mac
                    or any(actual.get(key) != value for key, value in options.items())
                    or any(key in actual for key in inherited)
                    or self.client.status(vmid) != "stopped"
                ):
                    raise VmctlError(
                        "Clone changed during provisioning; inspect it before starting"
                    )
                if current.request.start:
                    phase = "clone start"
                    self.client.start(vmid)
            except (VmctlError, OSError, KeyboardInterrupt) as exc:
                uncertain = isinstance(exc, (UncertainOperationError, KeyboardInterrupt)) or (
                    isinstance(exc, CommandError) and exc.timed_out
                )
                if uncertain or (clone_submitted and not cloned):
                    details = [
                        f"Clone outcome may be uncertain; inspect VMID {vmid} and Proxmox tasks before cleanup"
                    ]
                    if source_stopped:
                        details.append(
                            f"Source VM {current.source.vmid} was left stopped while the clone task may continue"
                        )
                else:
                    details = (
                        self._rollback(
                            vmid,
                            current.request.name,
                            request_id,
                            cloned,
                            reservation_mac,
                            snippet if snippet_attempted else None,
                        )
                        if cloned
                        else []
                    )
                    if source_stopped:
                        _, note = self._restore_source(current, source_config)
                        if note:
                            details.append(note)
                details.extend(notes)
                if uncertain:
                    if isinstance(exc, UncertainOperationError):
                        raise
                    target = current.source.vmid if phase.startswith("source ") else vmid
                    if source_stopped:
                        phase += (
                            f"; source VM {current.source.vmid} power state needs inspection"
                            if phase == "source restart"
                            else f"; source VM {current.source.vmid} remains stopped until its clone task is inspected"
                        )
                    raise UncertainOperationError(target, phase) from exc
                raise CreationError(
                    f"VM {vmid} clone failed: {exc or 'Interrupted'}. "
                    + (
                        "; ".join(details)
                        if details
                        else "Invocation-owned clone resources cleaned up"
                    )
                ) from exc
            vm = replace(self.client.resolve(str(vmid)), status=self.client.status(vmid))
            return CloneResult(
                vm,
                current.source.vmid,
                mac,
                address,
                current.cloud_init,
                source_restored,
                tuple(notes),
            )

    def _check_request_id(self, token: str) -> None:
        for vm in self.client.list_vms():
            if (
                decode_metadata(self.client.vm_config(vm.vmid).get("description", "")).get(
                    "invocation"
                )
                == token
            ):
                raise RepeatedRequestError(token, vm.vmid)

    def _restore_source(self, plan: ClonePreview, config: dict[str, str]) -> tuple[bool, str]:
        try:
            if self.client.vm_config(plan.source.vmid) != config:
                raise VmctlError("Source configuration changed")
            if self.client.status(plan.source.vmid) != "stopped":
                raise VmctlError("Source is no longer definitely stopped")
            self.client.start(plan.source.vmid)
            if self.client.status(plan.source.vmid) != "running":
                raise VmctlError("Source restart could not be confirmed")
        except (VmctlError, OSError) as exc:
            return (
                False,
                f"Source VM {plan.source.vmid} restart could not be confirmed: {exc}; inspect it before retrying",
            )
        return True, ""

    def _rollback(
        self, vmid: int, name: str, token: str, cloned: bool, mac: str | None, snippet: Path | None
    ) -> list[str]:
        if not cloned:
            return [f"Clone outcome is uncertain; inspect VMID {vmid} before cleanup"]
        try:
            config = self.client.vm_config(vmid)
            if decode_metadata(config.get("description", "")).get("invocation") != token:
                raise VmctlError("Invocation ownership could not be verified")
            if "lock" in config or self.client.status(vmid) != "stopped":
                raise VmctlError("Clone is not definitely stopped and unlocked")
            self._verify_owned_disks(vmid, config)
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
                matches = [entry for entry in self.reservations.read() if entry.mac == mac]
                if any(entry.vmid != vmid or entry.name != name for entry in matches):
                    raise VmctlError(
                        "Reservation ownership could not be verified; unrelated reservation retained"
                    )
                if matches:
                    self.reservations.remove_mac(mac)
            except (VmctlError, OSError) as exc:
                failures.append(f"Reservation for {mac} needs cleanup: {exc}")
        if snippet is not None:
            try:
                atomic_remove(snippet)
            except (VmctlError, OSError) as exc:
                failures.append(f"Snippet {snippet} needs cleanup: {exc}")
        return failures
