"""Destroy first; release DHCP only after VM destruction is confirmed."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from vmctl.config import Configuration
from vmctl.errors import PartialDeleteError, VmctlError
from vmctl.models import VM
from vmctl.network.dnsmasq import DnsmasqReservations
from vmctl.proxmox.client import ProxmoxClient
from vmctl.proxmox.parser import decode_metadata, network_mac
from vmctl.utils.atomic import atomic_remove, host_lock


@dataclass(frozen=True)
class DeletePlan:
    vm: VM
    mac: str | None
    snippet: Path | None
    fingerprint: str


class DeleteVMService:
    def __init__(
        self, config: Configuration, client: ProxmoxClient, reservations: DnsmasqReservations
    ) -> None:
        self.config, self.client, self.reservations = config, client, reservations

    def plan(self, reference: str) -> DeletePlan:
        vm = self.client.resolve(reference)
        config = self.client.vm_config(vm.vmid)
        if vm.template or config.get("template") == "1" or 9000 <= vm.vmid <= 9099:
            raise VmctlError("Templates and VMIDs 9000-9099 cannot be deleted by vmctl")
        if "lock" in config:
            raise VmctlError("VM is locked; resolve the active Proxmox task first")
        mac = network_mac(config["net0"]) if "net0" in config else None
        # Parse before destruction; never overwrite an invalid managed file.
        self.reservations.read()
        metadata = decode_metadata(config.get("description", ""))
        snippet = None
        token = metadata.get("invocation", "")
        volume = metadata.get("snippet", "")
        if re.fullmatch(r"[0-9a-f]{32}", token):
            filename = f"vmctl-{vm.vmid}-{token}.yaml"
            storage, _, relative = volume.partition(":snippets/")
            if re.fullmatch(r"[a-zA-Z0-9_-]+", storage) and relative == filename:
                snippet, _ = self.client.snippet_path(filename, storage=storage)
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        return DeletePlan(vm, mac, snippet, fingerprint)

    def delete(self, reference: str, *, expected: DeletePlan) -> int:
        with host_lock(self.config.path(self.config.host.lock_file), self.config.host.lock_timeout):
            plan = self.plan(reference)
            if (
                plan.vm.vmid != expected.vm.vmid
                or plan.vm.name != expected.vm.name
                or plan.mac != expected.mac
                or plan.snippet != expected.snippet
                or plan.fingerprint != expected.fingerprint
            ):
                raise VmctlError("VM changed since confirmation; inspect it and retry")
            if plan.mac and any(entry.mac == plan.mac for entry in self.reservations.read()):
                self.reservations.preflight()
            if plan.vm.status != "stopped":
                self.client.stop(plan.vm.vmid)
            self.client.destroy(plan.vm.vmid)
            if any(vm.vmid == plan.vm.vmid for vm in self.client.inventory()):
                raise VmctlError("VM destruction could not be confirmed; reservation retained")
            failures: list[str] = []
            if plan.mac:
                try:
                    self.reservations.remove_mac(plan.mac)
                except (VmctlError, OSError) as exc:
                    failures.append(f"Reservation cleanup failed: {exc}")
            if plan.snippet and plan.snippet.exists():
                try:
                    atomic_remove(plan.snippet)
                except (VmctlError, OSError) as exc:
                    failures.append(f"Snippet cleanup failed: {exc}")
            if failures:
                raise PartialDeleteError(f"VM {plan.vm.vmid} was destroyed. " + "; ".join(failures))
            return plan.vm.vmid
