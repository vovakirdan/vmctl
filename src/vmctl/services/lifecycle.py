"""Confirmed, guarded VM power operations independent of the frontend."""

import hashlib
import json
from dataclasses import replace

from vmctl.config import Configuration
from vmctl.errors import CommandError, UncertainOperationError, VmctlError
from vmctl.operations import ActionPreview, ActionResult, LifecycleAction
from vmctl.proxmox.client import ProxmoxClient
from vmctl.utils.atomic import host_lock


class LifecycleService:
    def __init__(self, config: Configuration, client: ProxmoxClient) -> None:
        self.config, self.client = config, client

    def plan(self, reference: str, action: LifecycleAction) -> ActionPreview:
        if action not in {"start", "shutdown", "reboot"}:
            raise VmctlError("Unsupported VM lifecycle action")
        vm = self.client.resolve(reference)
        config = self.client.vm_config(vm.vmid)
        if vm.template or config.get("template") == "1" or 9000 <= vm.vmid <= 9099:
            raise VmctlError("Templates and VMIDs 9000-9099 cannot be controlled by vmctl")
        if "lock" in config:
            raise VmctlError("VM is locked; resolve the active Proxmox task first")
        # Cluster inventory can lag after a power action; read QEMU directly.
        vm = replace(vm, name=config.get("name", vm.name), status=self.client.status(vm.vmid))
        required = "stopped" if action == "start" else "running"
        if vm.status != required:
            raise VmctlError(
                f"Cannot {action} VM {vm.vmid}: status is {vm.status!r}; expected {required!r}"
            )
        # Bind the confirmation to both configuration and observed power state.
        identity = (vm.vmid, vm.name, vm.status, vm.node, action)
        fingerprint = hashlib.sha256(
            json.dumps([identity, config], sort_keys=True).encode()
        ).hexdigest()
        return ActionPreview(vm, fingerprint, action)

    def action(self, expected: ActionPreview) -> ActionResult:
        with host_lock(self.config.path(self.config.host.lock_file), self.config.host.lock_timeout):
            current = self.plan(str(expected.vm.vmid), expected.action)
            if (
                current.vm.vmid != expected.vm.vmid
                or current.vm.name != expected.vm.name
                or current.vm.status != expected.vm.status
                or current.fingerprint != expected.fingerprint
            ):
                raise VmctlError("VM changed since confirmation; inspect it and retry")
            try:
                match current.action:
                    case "start":
                        self.client.start(current.vm.vmid)
                    case "shutdown":
                        self.client.shutdown(current.vm.vmid)
                    case "reboot":
                        self.client.reboot(current.vm.vmid)
            except CommandError as exc:
                if exc.timed_out:
                    raise UncertainOperationError(current.vm.vmid, current.action) from exc
                raise
            # Reboot may still be restarting. Report the host's current state,
            # without promising guest readiness or retrying a submitted action.
            try:
                vm = replace(current.vm, status=self.client.status(current.vm.vmid))
            except VmctlError as exc:
                raise VmctlError(
                    f"{current.action.capitalize()} completed for VM {current.vm.vmid}, "
                    "but its current state could not be read; inspect it before retrying"
                ) from exc
            return ActionResult(vm, current.action)
