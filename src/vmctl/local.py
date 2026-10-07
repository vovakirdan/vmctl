"""Linux worker adapter around existing host services."""

import sys
from collections.abc import Callable
from pathlib import Path

from vmctl.bootstrap.models import load_modules
from vmctl.bootstrap.renderer import CloudInitRenderer
from vmctl.bootstrap.resolver import validate_definitions
from vmctl.bootstrap.system import validate_system_features
from vmctl.config import Configuration, Preset, Template, load_config
from vmctl.errors import VmctlError
from vmctl.models import CreateRequest, CreateResult, VMDetails, VMStats
from vmctl.network.allocator import AddressAllocator
from vmctl.network.dnsmasq import DnsmasqReservations
from vmctl.operations import (
    ActionPreview,
    ActionResult,
    Catalog,
    CreatePreview,
    DeletePreview,
    LifecycleAction,
    Progress,
    ValidationSummary,
)
from vmctl.proxmox.client import ProxmoxClient
from vmctl.services.catalog import build_catalog
from vmctl.services.create_vm import CreateVMService
from vmctl.services.delete_vm import DeleteVMService
from vmctl.services.lifecycle import LifecycleService
from vmctl.services.metrics import vm_stats
from vmctl.services.queries import info_vm, list_vms
from vmctl.utils.subprocess import SubprocessRunner

Dependencies = Callable[
    [Configuration], tuple[ProxmoxClient, DnsmasqReservations, AddressAllocator]
]


def dependencies(
    config: Configuration,
) -> tuple[ProxmoxClient, DnsmasqReservations, AddressAllocator]:
    runner = SubprocessRunner()
    return (
        ProxmoxClient(config, runner),
        DnsmasqReservations(config, runner),
        AddressAllocator(config, runner),
    )


class LocalOperations:
    def __init__(self, directory: Path, factory: Dependencies = dependencies) -> None:
        if sys.platform != "linux":
            raise VmctlError("Direct Proxmox execution requires Linux")
        self.config = load_config(directory)
        self.client, self.reservations, allocator = factory(self.config)
        self.creator = CreateVMService(
            self.config, self.client, self.reservations, allocator, CloudInitRenderer(self.config)
        )
        self.deleter = DeleteVMService(self.config, self.client, self.reservations)
        self.lifecycle = LifecycleService(self.config, self.client)

    def plan_create(self, request: CreateRequest) -> CreatePreview:
        plan = self.creator.plan(request)
        return CreatePreview(
            plan.template_vmid,
            plan.resources,
            plan.disk_device,
            plan.template_disk_gib,
            self.config.host.network.bridge,
            self.config.host.username,
            plan.modules,
            plan.system_features,
            plan.desktop,
            plan.requires_desktop_password,
        )

    def create(
        self, request: CreateRequest, *, request_id: str, progress: Progress
    ) -> CreateResult:
        return self.creator.create(request, request_id=request_id, progress=progress)

    def plan_delete(self, reference: str) -> DeletePreview:
        plan = self.deleter.plan(reference)
        return DeletePreview(plan.vm, plan.fingerprint)

    def delete(self, expected: DeletePreview) -> int:
        current = self.deleter.plan(str(expected.vm.vmid))
        if current.vm.name != expected.vm.name or current.fingerprint != expected.fingerprint:
            raise VmctlError("VM changed since confirmation; inspect it and retry")
        # The service checks the same plan again while holding the host lock.
        return self.deleter.delete(str(expected.vm.vmid), expected=current)

    def plan_action(self, reference: str, action: LifecycleAction) -> ActionPreview:
        return self.lifecycle.plan(reference, action)

    def action(self, expected: ActionPreview) -> ActionResult:
        return self.lifecycle.action(expected)

    @staticmethod
    def safe_details(details: VMDetails) -> VMDetails:
        config = {
            k: "<redacted>" if k in {"cipassword", "sshkeys"} else v
            for k, v in details.config.items()
        }
        return VMDetails(
            details.vm, config, details.metadata, details.ip, details.addresses, details.ip_notes
        )

    def list(self) -> list[VMDetails]:
        return [self.safe_details(item) for item in list_vms(self.client, self.reservations)]

    def info(self, reference: str) -> VMDetails:
        return self.safe_details(info_vm(self.client, self.reservations, reference))

    def stats(self, reference: str, *, history: bool = False) -> VMStats:
        return vm_stats(self.client, reference, history=history)

    def catalog(self) -> Catalog:
        return build_catalog(self.config)

    def templates(self) -> dict[str, Template]:
        return self.config.templates

    def presets(self) -> dict[str, Preset]:
        return self.config.presets

    def validate_config(self) -> ValidationSummary:
        modules = load_modules(self.config)
        validate_definitions(modules, self.config.profiles)
        features = validate_system_features(self.config)
        if features.keys() & (modules.keys() | self.config.profiles.keys()):
            raise VmctlError(
                "System features and development modules/profiles must have distinct names"
            )
        return ValidationSummary(
            len(self.config.templates),
            len(self.config.presets),
            len(features),
            len(modules),
            len(self.config.profiles),
        )
