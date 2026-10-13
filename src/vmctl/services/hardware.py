"""Guarded CPU/RAM edits and shared stopped-VM configuration changes."""

import hashlib
import json
import re
from dataclasses import asdict, replace

from vmctl.config import Configuration
from vmctl.errors import CommandError, UncertainOperationError, VmctlError
from vmctl.models import VM, HardwareRequest
from vmctl.operations import HardwarePreview, HardwareResult, Progress
from vmctl.proxmox.client import ProxmoxClient
from vmctl.utils.atomic import host_lock
from vmctl.utils.sizes import parse_size


def inspect_vm(client: ProxmoxClient, reference: str) -> tuple[VM, dict[str, str]]:
    """Read direct power state and refuse configuration that cannot be safely edited."""
    vm = client.resolve(reference)
    config = client.vm_config(vm.vmid)
    if vm.template or config.get("template") == "1" or 9000 <= vm.vmid <= 9099:
        raise VmctlError("Templates and VMIDs 9000-9099 cannot be edited by vmctl")
    if "lock" in config:
        raise VmctlError("VM is locked; resolve the active Proxmox task first")
    pending = client.pending(vm.vmid)
    if not isinstance(pending, list) or any(not isinstance(entry, dict) for entry in pending):
        raise VmctlError("Proxmox returned invalid pending configuration data")
    if any("pending" in entry or entry.get("delete") for entry in pending):
        raise VmctlError("VM has pending configuration changes; apply or revert them first")
    status = client.status(vm.vmid)
    if status not in {"running", "stopped"}:
        raise VmctlError(f"VM status {status!r} cannot be safely edited")
    return replace(vm, name=config.get("name", vm.name), status=status), config


def confirmation_fingerprint(vm: VM, config: dict[str, str], request: object) -> str:
    return hashlib.sha256(
        json.dumps([asdict(vm), config, request], sort_keys=True).encode()
    ).hexdigest()


def positive_setting(config: dict[str, str], key: str, default: int) -> int:
    value = config.get(key, str(default))
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise VmctlError(f"VM has invalid {key} configuration")
    return int(value)


def change_stopped_config(
    client: ProxmoxClient,
    vm: VM,
    before: dict[str, str],
    changes: dict[str, str],
    *,
    operation: str,
    progress: Progress,
) -> tuple[VM, bool]:
    """Restore only known fields after definite failure; never compensate uncertain actions."""
    if not changes:
        return vm, False
    was_running = vm.status == "running"
    if was_running:
        progress("Shutting down VM gracefully")
        try:
            client.shutdown(vm.vmid)
        except CommandError as exc:
            if exc.timed_out:
                raise UncertainOperationError(vm.vmid, operation + " shutdown") from exc
            raise
    try:
        if client.status(vm.vmid) != "stopped":
            raise VmctlError("VM did not stop; no configuration was changed")
        # The host lock protects vmctl operations, not changes from another administrator.
        checked, current = inspect_vm(client, str(vm.vmid))
        if checked.status != "stopped" or current != before:
            raise VmctlError("VM changed while stopping; inspect it before retrying")
    except VmctlError as exc:
        raise VmctlError(f"{exc}; VM was not restarted automatically") from exc

    progress("Updating VM configuration")
    verification_unknown = False
    try:
        client.configure(vm.vmid, changes)
        try:
            actual = client.vm_config(vm.vmid)
        except VmctlError:
            verification_unknown = True
            raise
        if any(actual.get(key) != value for key, value in changes.items()):
            raise VmctlError("Proxmox did not apply the requested configuration")
        if {key: value for key, value in actual.items() if key not in changes} != {
            key: value for key, value in before.items() if key not in changes
        }:
            raise VmctlError("Other VM configuration changed during the operation")
        try:
            stopped = client.status(vm.vmid) == "stopped"
        except VmctlError:
            verification_unknown = True
            raise
        if not stopped:
            raise VmctlError(
                "VM started during configuration; no further power action was attempted"
            )
        if was_running:
            progress("Restoring the previous running state")
            client.start(vm.vmid)
        try:
            status = client.status(vm.vmid)
        except VmctlError:
            verification_unknown = True
            raise
        if status != ("running" if was_running else "stopped"):
            raise VmctlError("VM power state changed unexpectedly after configuration")
        return replace(vm, status=status), was_running
    except CommandError as exc:
        if exc.timed_out:
            raise UncertainOperationError(vm.vmid, operation) from exc
        failure: VmctlError = exc
    except VmctlError as exc:
        failure = exc

    if verification_unknown:
        raise VmctlError(
            f"{operation.capitalize()} was submitted, but its result cannot be verified. "
            "Inspect VM state before retrying; no rollback or power action was attempted"
        ) from failure

    try:
        checked, actual = inspect_vm(client, str(vm.vmid))
        state = checked.status
        outside = {key: value for key, value in actual.items() if key not in changes}
        original_outside = {key: value for key, value in before.items() if key not in changes}
        if (
            state != "stopped"
            or outside != original_outside
            or any(
                actual.get(key) not in {before.get(key), value} for key, value in changes.items()
            )
        ):
            raise VmctlError("VM state is not safe for automatic configuration rollback")
        restore = {
            key: before[key] for key in changes if key in before and actual.get(key) != before[key]
        }
        delete = tuple(key for key in changes if key not in before and key in actual)
        if restore or delete:
            client.configure(vm.vmid, restore, delete=delete)
        if client.vm_config(vm.vmid) != before or client.status(vm.vmid) != "stopped":
            raise VmctlError("Original configuration could not be verified")
        if was_running:
            client.start(vm.vmid)
            if client.status(vm.vmid) != "running":
                raise VmctlError("Previous running state could not be verified")
        message = "Original configuration and power state restored"
    except CommandError as exc:
        if exc.timed_out:
            raise UncertainOperationError(vm.vmid, operation + " rollback") from exc
        message = "Automatic rollback failed; inspect VM state before retrying"
    except VmctlError:
        message = "Automatic rollback was not safe; inspect VM state before retrying"
    raise VmctlError(f"{failure}. {message}") from failure


class HardwareService:
    def __init__(self, config: Configuration, client: ProxmoxClient) -> None:
        self.config, self.client = config, client

    def plan(self, request: HardwareRequest) -> HardwarePreview:
        if request.cpu is None and request.memory is None:
            raise VmctlError("Specify --cpu or --memory")
        if request.cpu is not None and (
            type(request.cpu) is not int or not 1 <= request.cpu <= 512
        ):
            raise VmctlError("CPU count must be between 1 and 512")
        vm, config = inspect_vm(self.client, request.reference)
        sockets = positive_setting(config, "sockets", 1)
        cpu = positive_setting(config, "cores", 1) * sockets
        memory = positive_setting(config, "memory", 512)
        new_cpu = request.cpu if request.cpu is not None else cpu
        new_memory = (
            parse_size(request.memory, unit="MiB") if request.memory is not None else memory
        )
        if new_cpu % sockets:
            raise VmctlError(f"CPU count must be divisible by the existing {sockets} sockets")
        changed = new_cpu != cpu or new_memory != memory
        if changed and (
            config.get("numa") == "1" or any(re.fullmatch(r"numa[0-9]+", key) for key in config)
        ):
            raise VmctlError("Explicit NUMA topology cannot be edited by vmctl")
        if new_cpu != cpu and "vcpus" in config:
            raise VmctlError("Explicit vcpus topology cannot be edited by vmctl")
        balloon = config.get("balloon", "0")
        if not balloon.isdecimal() or int(balloon) > new_memory:
            raise VmctlError("Requested memory is below the configured balloon minimum")
        if changed and vm.status == "running" and not request.restart:
            raise VmctlError("Changing a running VM requires --restart and causes downtime")
        vm = replace(vm, cpu=cpu, memory_mib=memory)
        notes = () if changed else ("The requested resources already match the VM configuration",)
        return HardwarePreview(
            vm,
            request,
            confirmation_fingerprint(vm, config, asdict(request)),
            cpu,
            memory,
            new_cpu,
            new_memory,
            notes,
        )

    def update(self, expected: HardwarePreview, *, progress: Progress) -> HardwareResult:
        with host_lock(self.config.path(self.config.host.lock_file), self.config.host.lock_timeout):
            current = self.plan(expected.request)
            if current != expected:
                raise VmctlError(
                    "VM or requested resources changed since confirmation; inspect and retry"
                )
            before = self.client.vm_config(current.vm.vmid)
            if (
                confirmation_fingerprint(current.vm, before, asdict(current.request))
                != current.fingerprint
            ):
                raise VmctlError("VM changed during confirmation recheck; inspect and retry")
            sockets = positive_setting(before, "sockets", 1)
            changes: dict[str, str] = {}
            if current.new_cpu != current.cpu:
                changes["cores"] = str(current.new_cpu // sockets)
            if current.new_memory_mib != current.memory_mib:
                changes["memory"] = str(current.new_memory_mib)
            vm, restarted = change_stopped_config(
                self.client,
                current.vm,
                before,
                changes,
                operation="resource update",
                progress=progress,
            )
            vm = replace(vm, cpu=current.new_cpu, memory_mib=current.new_memory_mib)
            return HardwareResult(
                vm, current.new_cpu, current.new_memory_mib, restarted, current.notes
            )
