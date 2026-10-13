"""SPICE display configuration and optional QEMU-agent guest integration."""

import time
from dataclasses import asdict
from typing import Literal

from vmctl.config import Configuration, SpiceSettings, load_spice_settings
from vmctl.errors import CommandError, VmctlError
from vmctl.models import SpiceRequest
from vmctl.network.discovery import agent_enabled
from vmctl.operations import Progress, SpicePreview, SpiceResult
from vmctl.proxmox.client import ProxmoxClient
from vmctl.proxmox.parser import parse_properties
from vmctl.services.hardware import (
    change_stopped_config,
    confirmation_fingerprint,
    inspect_vm,
)
from vmctl.utils.atomic import host_lock


def spice_display(value: str) -> bool:
    properties = parse_properties(value)
    kind = properties.get("type", properties.get("value", "std"))
    return kind in {"qxl", "qxl2", "qxl3", "qxl4"} or kind.startswith("virtio")


def guest_family(data: object, settings: SpiceSettings) -> str | None:
    if not isinstance(data, dict):
        return None
    result = data.get("result", data)
    if not isinstance(result, dict) or not isinstance(result.get("id"), str):
        return None
    return next(
        (
            family
            for family, definition in settings.families.items()
            if result["id"] in definition.os_ids
        ),
        None,
    )


class SpiceService:
    def __init__(self, config: Configuration, client: ProxmoxClient) -> None:
        self.config, self.client = config, client

    def plan(self, request: SpiceRequest) -> SpicePreview:
        if request.guest_tools not in {"auto", "skip", "install"}:
            raise VmctlError("Guest tools mode must be auto, skip or install")
        settings = load_spice_settings(self.config)
        vm, config = inspect_vm(self.client, request.reference)
        current_vga = config.get("vga", "std")
        target_vga = current_vga if spice_display(current_vga) else "qxl"
        needs_shutdown = target_vga != current_vga and vm.status == "running"
        if needs_shutdown and not request.restart:
            raise VmctlError("Changing a running VM display requires --restart and causes downtime")
        family: str | None = None
        notes = ["Display configuration alone does not verify guest SPICE readiness"]
        if request.guest_tools == "skip":
            notes.append("Guest integration was not requested")
        elif vm.status != "running" or not agent_enabled(config):
            notes.append("Guest integration requires a running VM with an enabled QEMU guest agent")
        else:
            try:
                family = guest_family(
                    self.client.get_os_info(vm.vmid, timeout=settings.agent_timeout_seconds),
                    settings,
                )
            except VmctlError:
                notes.append("QEMU guest agent OS information is unavailable")
            if family is None:
                notes.append(
                    "Guest integration is unavailable for this OS; install supported tools manually"
                )
        possible = family is not None
        if request.guest_tools == "install" and not possible:
            raise VmctlError(
                "Guest tools installation requires an available QEMU agent and supported Linux OS"
            )
        identity = {
            "request": asdict(request),
            "spice_settings": settings.model_dump(),
            "guest_family": family,
        }
        return SpicePreview(
            vm,
            request,
            confirmation_fingerprint(vm, config, identity),
            current_vga,
            target_vga,
            needs_shutdown,
            family,
            possible,
            tuple(notes),
        )

    def apply(self, expected: SpicePreview, *, progress: Progress) -> SpiceResult:
        with host_lock(self.config.path(self.config.host.lock_file), self.config.host.lock_timeout):
            current = self.plan(expected.request)
            if current != expected:
                raise VmctlError(
                    "VM or SPICE settings changed since confirmation; inspect and retry"
                )
            settings = load_spice_settings(self.config)
            before = self.client.vm_config(current.vm.vmid)
            identity = {
                "request": asdict(current.request),
                "spice_settings": settings.model_dump(),
                "guest_family": current.guest_family,
            }
            if confirmation_fingerprint(current.vm, before, identity) != current.fingerprint:
                raise VmctlError("VM changed during confirmation recheck; inspect and retry")
            changes = (
                {"vga": current.target_vga} if current.target_vga != current.current_vga else {}
            )
            vm, restarted = change_stopped_config(
                self.client,
                current.vm,
                before,
                changes,
                operation="SPICE setup",
                progress=progress,
            )
            if current.request.guest_tools == "skip":
                return SpiceResult(
                    vm, current.target_vga, True, "not-requested", notes=current.notes
                )
            if not current.guest_tools_possible or current.guest_family is None:
                return SpiceResult(vm, current.target_vga, True, "unavailable", notes=current.notes)
            progress("Installing guest SPICE integration through the QEMU guest agent")
            status, pid, note = self._install(
                vm.vmid, current.guest_family, settings, wait_for_agent=restarted
            )
            return SpiceResult(vm, current.target_vga, True, status, pid, (*current.notes, note))

    def _install(
        self,
        vmid: int,
        family: str,
        settings: SpiceSettings,
        *,
        wait_for_agent: bool = False,
    ) -> tuple[Literal["installed", "failed", "uncertain"], int | None, str]:
        deadline = time.monotonic() + settings.timeout_seconds
        if wait_for_agent:
            while time.monotonic() < deadline:
                try:
                    info = self.client.get_os_info(vmid, timeout=settings.agent_timeout_seconds)
                except VmctlError:
                    time.sleep(
                        min(settings.poll_interval_seconds, max(0, deadline - time.monotonic()))
                    )
                    continue
                if guest_family(info, settings) != family:
                    return (
                        "failed",
                        None,
                        "Guest OS changed after restart; no integration command was submitted",
                    )
                break
            else:
                return (
                    "failed",
                    None,
                    "QEMU guest agent did not become available after restart; no integration command was submitted",
                )
        for arguments in settings.families[family].commands:
            if time.monotonic() >= deadline:
                return (
                    "failed",
                    None,
                    "Guest integration deadline expired before command submission",
                )
            try:
                pid = self.client.guest_exec(
                    vmid, tuple(arguments), timeout=settings.agent_timeout_seconds
                )
            except CommandError:
                return (
                    "uncertain",
                    None,
                    "Guest command submission could not be confirmed; it may still run. Inspect the guest before retrying",
                )
            except VmctlError:
                return (
                    "uncertain",
                    None,
                    "Guest command submission returned no valid PID; inspect the guest before retrying",
                )
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return (
                        "uncertain",
                        pid,
                        f"Guest process {pid} may still run after the polling timeout; inspect it before retrying",
                    )
                try:
                    state = self.client.guest_exec_status(
                        vmid,
                        pid,
                        timeout=min(settings.agent_timeout_seconds, max(1, int(remaining))),
                    )
                except VmctlError:
                    return (
                        "uncertain",
                        pid,
                        f"Guest process {pid} status is unavailable; inspect it before retrying",
                    )
                if not isinstance(state, dict) or type(state.get("exited")) not in {int, bool}:
                    return (
                        "uncertain",
                        pid,
                        f"Guest process {pid} returned invalid status; inspect it before retrying",
                    )
                if state["exited"] not in {0, 1}:
                    return (
                        "uncertain",
                        pid,
                        f"Guest process {pid} returned invalid exit state; inspect it before retrying",
                    )
                if state["exited"]:
                    if type(state.get("exitcode")) is int and state["exitcode"] == 0:
                        break
                    return "failed", pid, f"Guest process {pid} failed; command output was withheld"
                time.sleep(min(settings.poll_interval_seconds, max(0, deadline - time.monotonic())))
        return (
            "installed",
            None,
            "Guest integration commands completed; desktop session readiness was not probed",
        )
