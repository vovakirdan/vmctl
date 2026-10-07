"""Parse Proxmox boundaries without relying on human-facing table layouts."""

import json
import re
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

from vmctl.errors import VmctlError
from vmctl.models import VM


def parse_config(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator or not re.fullmatch(r"[a-zA-Z0-9_]+", key):
            raise VmctlError(f"Invalid qm config line: {line!r}")
        if key in result:
            raise VmctlError(f"Duplicate qm config key: {key}")
        result[key] = unquote(value.strip()) if key == "description" else value.strip()
    return result


def parse_properties(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, part in enumerate(value.split(",")):
        key, sep, val = part.partition("=")
        if sep:
            result[key] = val
        elif index == 0:
            result["value"] = key
        else:
            raise VmctlError(f"Invalid Proxmox property: {part!r}")
    return result


def network_mac(value: str) -> str:
    properties = parse_properties(value)
    mac = next(
        (
            value
            for value in properties.values()
            if re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", value)
        ),
        None,
    )
    if mac is None or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac):
        raise VmctlError("Cannot determine a valid MAC address from net0")
    return mac.upper()


def parse_json(output: str) -> Any:
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise VmctlError(f"Invalid Proxmox JSON: {exc}") from exc


def parse_vms(output: str) -> list[VM]:
    data = parse_json(output)
    if not isinstance(data, list):
        raise VmctlError("Expected a VM array from pvesh")
    result: list[VM] = []
    try:
        for entry in data:
            if entry.get("type") != "qemu":
                continue
            result.append(
                VM(
                    vmid=int(entry["vmid"]),
                    name=str(entry.get("name", "")),
                    status=str(entry.get("status", "unknown")),
                    node=str(entry["node"]),
                    template=bool(int(entry.get("template", 0))),
                    cpu=int(entry.get("maxcpu", 0)),
                    memory_mib=int(entry.get("maxmem", 0)) // 1048576,
                )
            )
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise VmctlError("Invalid VM entry from pvesh") from exc
    return result


def disk_gib_exact(config: dict[str, str], device: str) -> Decimal:
    props = parse_properties(config[device])
    size = props.get("size", "")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGT])", size, re.IGNORECASE)
    if match is None:
        raise VmctlError(f"Disk {device} is missing a usable size")
    return (
        Decimal(match[1])
        * {"K": Decimal(1) / 1048576, "M": Decimal(1) / 1024, "G": Decimal(1), "T": Decimal(1024)}[
            match[2].upper()
        ]
    )


def select_disk(config: dict[str, str], configured: str | None = None) -> str:
    devices = [
        key
        for key, value in config.items()
        if re.fullmatch(r"(?:scsi|virtio|sata|ide)[0-9]+", key)
        and "cloudinit" not in value
        and "media=cdrom" not in value
    ]
    if configured is not None:
        if configured not in devices:
            raise VmctlError(f"Configured primary disk {configured!r} is not a data disk")
        return configured
    order = parse_properties(config.get("boot", "")).get("order", "").split(";")
    candidates = [device for device in order if device in devices]
    if candidates:
        return candidates[0]
    if len(devices) != 1:
        raise VmctlError("Cannot determine primary disk; set disk_device in templates.toml")
    return devices[0]


def encode_metadata(description: str, metadata: dict[str, str]) -> str:
    if "[vmctl:" in description:
        raise VmctlError("Description contains a reserved vmctl metadata marker")
    marker = "[vmctl:" + json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "]"
    return f"{description}\n\n{marker}".strip()


def decode_metadata(description: str) -> dict[str, str]:
    match = re.search(r"(?:^|\n)\[vmctl:(\{[^\n]*\})\]\s*$", description)
    if match is None:
        return {}
    try:
        value = json.loads(match[1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        return {}
    return value
