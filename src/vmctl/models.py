"""Domain requests and results, independent of CLI presentation."""

import re
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from typing import Literal

from pydantic import SecretStr

from vmctl.errors import VmctlError


def validate_name(name: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name) or name.endswith("-"):
        raise VmctlError(
            "Name must be a lowercase DNS label, start with a letter, and be <=63 characters"
        )
    return name


@dataclass(frozen=True)
class Resources:
    cpu: int
    memory_mib: int
    disk_gib: int


@dataclass(frozen=True)
class CreateRequest:
    name: str
    template: str
    preset: str
    cpu: int | None = None
    memory: str | None = None
    disk: str | None = None
    ip: str = "auto"
    modules: tuple[str, ...] = ()
    versions: dict[str, str] = field(default_factory=dict)
    start: bool = True
    description: str = ""
    wait_seconds: int = 0
    system_features: tuple[str, ...] = ()
    without_system: tuple[str, ...] = ()
    desktop_password_hash: SecretStr | None = field(default=None, repr=False)
    no_desktop_password: bool = False
    public_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class VM:
    vmid: int
    name: str
    status: str
    node: str
    template: bool = False
    cpu: int = 0
    memory_mib: int = 0


@dataclass(frozen=True)
class IPObservation:
    address: IPv4Address
    source: Literal["guest-agent", "dhcp-lease", "cloud-init", "neighbor"]


@dataclass(frozen=True)
class VMDetails:
    vm: VM
    config: dict[str, str]
    metadata: dict[str, str]
    ip: IPv4Address | None
    addresses: tuple[IPObservation, ...] = ()
    ip_notes: tuple[str, ...] = ()

    @property
    def display_ip(self) -> IPv4Address | None:
        return self.ip or (self.addresses[0].address if self.addresses else None)

    @property
    def ip_source(self) -> str:
        if self.ip is not None:
            return "reservation"
        return self.addresses[0].source if self.addresses else "unknown"

    @property
    def system_features(self) -> tuple[str, ...]:
        return tuple(filter(None, self.metadata.get("system_features", "").split(",")))

    @property
    def modules(self) -> tuple[str, ...]:
        return tuple(filter(None, self.metadata.get("modules", "").split(",")))

    @property
    def desktop(self) -> bool | None:
        value = self.metadata.get("desktop")
        return value == "true" if value in {"true", "false"} else None

    @property
    def rdp_enabled(self) -> bool:
        return self.desktop is True and "desktop-rdp" in self.system_features

    @property
    def rdp_address(self) -> str | None:
        return f"{self.display_ip}:3389" if self.rdp_enabled and self.display_ip else None


@dataclass(frozen=True)
class VMMetricPoint:
    timestamp: float
    cpu_percent: float | None = None
    memory_bytes: float | None = None
    memory_total_bytes: float | None = None
    network_in_bytes_per_second: float | None = None
    network_out_bytes_per_second: float | None = None
    disk_read_bytes_per_second: float | None = None
    disk_write_bytes_per_second: float | None = None


@dataclass(frozen=True)
class VMStats:
    vm: VM
    timestamp: float
    cpu_percent: float | None = None
    memory_bytes: int | None = None
    memory_total_bytes: int | None = None
    uptime_seconds: int | None = None
    pid: int | None = None
    network_in_bytes: int | None = None
    network_out_bytes: int | None = None
    disk_read_bytes: int | None = None
    disk_write_bytes: int | None = None
    history: tuple[VMMetricPoint, ...] = ()
    history_note: str = ""


@dataclass(frozen=True)
class CreatePlan:
    request: CreateRequest
    template_vmid: int
    resources: Resources
    disk_device: str
    template_disk_gib: int
    public_key: str = field(repr=False)
    user_data: str = field(repr=False)
    modules: tuple[str, ...] = ()
    system_features: tuple[str, ...] = ()
    desktop: bool = False
    requires_desktop_password: bool = False


@dataclass(frozen=True)
class CreateResult:
    vmid: int
    name: str
    template: str
    preset: str
    resources: Resources
    mac: str
    ip: IPv4Address
    username: str
    started: bool
    modules: tuple[str, ...]
    readiness: str
    system_features: tuple[str, ...] = ()
    desktop: bool = False
    rdp_enabled: bool = False
