"""Frontend contract and public DTOs. No guest credentials or cloud-init data."""

from collections.abc import Callable
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from typing import Literal, Protocol

from vmctl.config import Preset, Template
from vmctl.models import VM, CreateRequest, CreateResult, ModuleGroup, Resources, VMDetails, VMStats

Progress = Callable[[str], None]
LifecycleAction = Literal["start", "shutdown", "reboot"]


@dataclass(frozen=True)
class ActionPreview:
    vm: VM
    fingerprint: str
    action: LifecycleAction


@dataclass(frozen=True)
class ActionResult:
    vm: VM
    action: LifecycleAction


@dataclass(frozen=True)
class CreatePreview:
    template_vmid: int
    resources: Resources
    disk_device: str
    template_disk_gib: int
    bridge: str
    username: str
    modules: tuple[str, ...]
    system_features: tuple[str, ...]
    desktop: bool
    requires_desktop_password: bool


@dataclass(frozen=True)
class DeletePreview:
    vm: VM
    fingerprint: str


@dataclass(frozen=True)
class ValidationSummary:
    templates: int
    presets: int
    system_features: int
    modules: int
    profiles: int


@dataclass(frozen=True)
class CatalogItem:
    name: str
    description: str
    templates: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    members: tuple[str, ...] = ()
    group: ModuleGroup = "development"


@dataclass(frozen=True)
class Catalog:
    templates: dict[str, Template]
    presets: dict[str, Preset]
    modules: tuple[CatalogItem, ...]
    profiles: tuple[CatalogItem, ...]
    system_features: tuple[CatalogItem, ...]
    pool_start: IPv4Address
    pool_end: IPv4Address
    system_defaults: dict[str, tuple[str, ...]] = field(default_factory=dict)


class Operations(Protocol):
    def plan_create(self, request: CreateRequest) -> CreatePreview: ...
    def create(
        self, request: CreateRequest, *, request_id: str, progress: Progress
    ) -> CreateResult: ...
    def plan_delete(self, reference: str) -> DeletePreview: ...
    def delete(self, expected: DeletePreview) -> int: ...
    def plan_action(self, reference: str, action: LifecycleAction) -> ActionPreview: ...
    def action(self, expected: ActionPreview) -> ActionResult: ...
    def list(self) -> list[VMDetails]: ...
    def info(self, reference: str) -> VMDetails: ...
    def stats(self, reference: str, *, history: bool = False) -> VMStats: ...
    def catalog(self) -> Catalog: ...
    def templates(self) -> dict[str, Template]: ...
    def presets(self) -> dict[str, Preset]: ...
    def validate_config(self) -> ValidationSummary: ...
