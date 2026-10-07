"""Strict TOML configuration with injectable filesystem paths."""

import re
import shlex
import tomllib
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from vmctl.errors import VmctlError
from vmctl.models import CreateRequest, Resources
from vmctl.utils.sizes import parse_size


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Preset(StrictModel):
    cpu: int = Field(gt=0, le=512)
    memory_mib: int = Field(gt=0)
    disk_gib: int = Field(gt=0)


class Template(StrictModel):
    vmid: int = Field(ge=9000, le=9099)
    family: Literal["debian", "rhel", "alpine"]
    distro: str
    release: str
    desktop: bool = False
    system_features: list[str] | None = None
    disk_device: str | None = None


class NetworkConfig(StrictModel):
    bridge: str = "vmbr1"
    subnet: str = "10.210.0.0/24"
    gateway: str = "10.210.0.1"
    pool_start: str = "10.210.0.100"
    pool_end: str = "10.210.0.199"
    reservations: str = "/etc/dnsmasq.d/vmctl-hosts.conf"
    leases: str = "/var/lib/misc/dnsmasq.leases"
    dnsmasq_config: str = "/etc/dnsmasq.conf"
    dnsmasq_conf_dirs: list[str] = Field(default_factory=list)
    service: str = "dnsmasq"
    probe: bool = True

    @model_validator(mode="after")
    def validate_network(self) -> "NetworkConfig":
        for directory in self.dnsmasq_conf_dirs:
            if not directory.split(",")[0].strip() or "\x00" in directory or "\n" in directory:
                raise ValueError("dnsmasq_conf_dirs must contain valid conf-dir specifications")
        subnet = IPv4Network(self.subnet)
        start, end, gateway = map(IPv4Address, (self.pool_start, self.pool_end, self.gateway))
        if start > end or any(ip not in subnet for ip in (start, end, gateway)):
            raise ValueError("Pool and gateway must belong to the configured subnet")
        if (
            start <= subnet.network_address
            or end >= subnet.broadcast_address
            or start <= gateway <= end
            or gateway in {subnet.network_address, subnet.broadcast_address}
        ):
            raise ValueError("Pool must exclude the gateway, network and broadcast addresses")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", self.bridge) or self.bridge == "vmbr0":
            raise ValueError("A private bridge other than vmbr0 is required")
        return self


class ProxmoxConfig(StrictModel):
    node: str | None = None
    clone_storage: str | None = None
    snippets_storage: str = "local"
    command_timeout: int = Field(default=60, gt=0)
    clone_timeout: int = Field(default=1800, gt=0)
    stop_timeout: int = Field(default=120, gt=0)


class BootstrapConfig(StrictModel):
    modules_dir: str = "bootstrap/modules"
    scripts_dir: str = "bootstrap/scripts"
    system_features_dir: str = "bootstrap/system"
    # Accepted for upgrades; legacy integration modules are excluded from development.
    system_module: str | None = None


class HostConfig(StrictModel):
    username: str = "vmadmin"
    ssh_key: str = "/root/.ssh/id_ed25519.pub"
    lock_file: str = "/run/lock/vmctl.lock"
    lock_timeout: int = Field(default=30, gt=0)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    proxmox: ProxmoxConfig = Field(default_factory=ProxmoxConfig)
    bootstrap: BootstrapConfig = Field(default_factory=BootstrapConfig)
    binaries: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_username(self) -> "HostConfig":
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", self.username) or self.username == "root":
            raise ValueError("username must be a non-root Linux account name")
        return self


class Configuration(StrictModel):
    root: Path
    host: HostConfig
    templates: dict[str, Template]
    presets: dict[str, Preset]
    profiles: dict[str, list[str]]

    def path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.root / path

    def template(self, name: str) -> Template:
        try:
            return self.templates[name]
        except KeyError as exc:
            raise VmctlError(f"Unknown template: {name}") from exc

    def resources(self, request: CreateRequest) -> Resources:
        try:
            preset = self.presets[request.preset]
        except KeyError as exc:
            raise VmctlError(f"Unknown preset: {request.preset}") from exc
        cpu = request.cpu if request.cpu is not None else preset.cpu
        memory = (
            parse_size(request.memory, unit="MiB")
            if request.memory is not None
            else preset.memory_mib
        )
        disk = parse_size(request.disk, unit="GiB") if request.disk is not None else preset.disk_gib
        if not 1 <= cpu <= 512:
            raise VmctlError("CPU count must be between 1 and 512")
        return Resources(cpu, memory, disk)


def read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except FileNotFoundError as exc:
        hint = ""
        if path.name in {"config.toml", "templates.toml", "presets.toml", "profiles.toml"}:
            hint = f". Install server defaults with: vmctl --local --config-dir {shlex.quote(str(path.parent))} config init"
        raise VmctlError(f"Cannot load {path}: {exc}{hint}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VmctlError(f"Cannot load {path}: {exc}") from exc


def load_config(root: Path) -> Configuration:
    root = root.resolve()
    try:
        templates = read_toml(root / "templates.toml")
        presets = read_toml(root / "presets.toml")
        profiles = read_toml(root / "profiles.toml")
        if (
            set(templates) != {"templates"}
            or set(presets) != {"presets"}
            or set(profiles) != {"profiles"}
        ):
            raise VmctlError(
                "Expected only [templates], [presets] and [profiles] in their respective files"
            )
        config = Configuration.model_validate(
            {
                "root": root,
                "host": read_toml(root / "config.toml"),
                "templates": templates["templates"],
                "presets": presets["presets"],
                "profiles": profiles["profiles"],
            }
        )
        if not config.templates or not config.presets:
            raise VmctlError("At least one template and preset are required")
        if len({t.vmid for t in config.templates.values()}) != len(config.templates):
            raise VmctlError("Template VMIDs must be unique")
        return config
    except (ValidationError, ValueError) as exc:
        raise VmctlError(f"Invalid configuration: {exc}") from exc
