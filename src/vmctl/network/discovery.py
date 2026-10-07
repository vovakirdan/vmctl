"""Read-only, MAC-bound address observations for the configured private network."""

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface, IPv4Network

from vmctl.config import Configuration
from vmctl.errors import VmctlError
from vmctl.models import VM, IPObservation
from vmctl.proxmox.client import ProxmoxClient
from vmctl.proxmox.parser import network_mac, parse_properties

MAC_PATTERN = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")


@dataclass(frozen=True)
class AddressSnapshot:
    leases: Mapping[str, tuple[IPv4Address, ...]]
    neighbors: Mapping[str, tuple[IPv4Address, ...]]
    notes: tuple[str, ...] = ()


def private_address(value: object, config: Configuration) -> IPv4Address | None:
    if not isinstance(value, str):
        return None
    try:
        address = IPv4Address(value)
        subnet = IPv4Network(config.host.network.subnet)
        gateway = IPv4Address(config.host.network.gateway)
    except ValueError:
        return None
    if address not in subnet or address in {
        subnet.network_address,
        subnet.broadcast_address,
        gateway,
    }:
        return None
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        return None
    return address


def lease_observations(
    text: str, config: Configuration, *, now: float
) -> dict[str, tuple[IPv4Address, ...]]:
    """Ignore expired/malformed leases; hostname is never used to associate a VM."""
    entries: dict[str, list[IPv4Address]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 5 or not MAC_PATTERN.fullmatch(parts[1]):
            continue
        try:
            expires = int(parts[0])
        except ValueError:
            continue
        if expires < 0 or (expires != 0 and expires <= now):
            continue
        address = private_address(parts[2], config)
        if address is not None:
            entries.setdefault(parts[1].upper(), []).append(address)
    return {mac: tuple(dict.fromkeys(addresses)) for mac, addresses in entries.items()}


def neighbor_observations(text: str, config: Configuration) -> dict[str, tuple[IPv4Address, ...]]:
    data: object = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Expected a neighbor array")
    entries: dict[str, list[IPv4Address]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("dev", config.host.network.bridge) != config.host.network.bridge:
            continue
        state = item.get("state", [])
        if not isinstance(state, list) or any(
            not isinstance(value, str) or value in {"FAILED", "INCOMPLETE"} for value in state
        ):
            continue
        mac = item.get("lladdr")
        if not isinstance(mac, str) or not MAC_PATTERN.fullmatch(mac):
            continue
        address = private_address(item.get("dst"), config)
        if address is not None:
            entries.setdefault(mac.upper(), []).append(address)
    return {mac: tuple(dict.fromkeys(addresses)) for mac, addresses in entries.items()}


def read_address_snapshot(client: ProxmoxClient) -> AddressSnapshot:
    """Read each shared source once per inventory request, without active probing."""
    config = client.config
    notes: list[str] = []
    leases: dict[str, tuple[IPv4Address, ...]] = {}
    try:
        text = config.path(config.host.network.leases).read_text(encoding="utf-8")
        leases = lease_observations(text, config, now=time.time())
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        notes.append("DHCP leases could not be read")
    neighbors: dict[str, tuple[IPv4Address, ...]] = {}
    try:
        output = client.command(
            "ip", "-j", "neigh", "show", "dev", config.host.network.bridge, timeout=3
        ).stdout
        neighbors = neighbor_observations(output, config)
    except (VmctlError, OSError, ValueError):
        notes.append("Private-bridge neighbor observations are unavailable")
    return AddressSnapshot(leases, neighbors, tuple(notes))


def network_bindings(config: Mapping[str, str], bridge: str) -> dict[str, str]:
    """Return network-index to MAC bindings only for the configured private bridge."""
    result: dict[str, str] = {}
    for key, value in config.items():
        if not re.fullmatch(r"net[0-9]+", key):
            continue
        try:
            if parse_properties(value).get("bridge") == bridge:
                result[key.removeprefix("net")] = network_mac(value)
        except VmctlError:
            continue
    return result


def agent_enabled(config: Mapping[str, str]) -> bool:
    try:
        values = parse_properties(config.get("agent", "0"))
    except VmctlError:
        return False
    return values.get("enabled", values.get("value", "0")) == "1"


def agent_observations(
    data: object, macs: set[str], config: Configuration
) -> tuple[IPObservation, ...]:
    if isinstance(data, dict):
        data = data.get("result")
    if not isinstance(data, list):
        raise ValueError("Expected guest-agent interfaces")
    observations: list[IPObservation] = []
    for interface in data:
        if not isinstance(interface, dict):
            continue
        mac = interface.get("hardware-address")
        if not isinstance(mac, str) or mac.upper() not in macs:
            continue
        addresses = interface.get("ip-addresses", [])
        if not isinstance(addresses, list):
            continue
        for entry in addresses:
            if not isinstance(entry, dict) or entry.get("ip-address-type") != "ipv4":
                continue
            address = private_address(entry.get("ip-address"), config)
            if address is not None:
                observations.append(IPObservation(address, "guest-agent"))
    return tuple(observations)


def discover_addresses(
    client: ProxmoxClient,
    vm: VM,
    vm_config: Mapping[str, str],
    snapshot: AddressSnapshot,
) -> tuple[tuple[IPObservation, ...], tuple[str, ...]]:
    bindings = network_bindings(vm_config, client.config.host.network.bridge)
    macs = set(bindings.values())
    result: list[IPObservation] = []
    notes = list(snapshot.notes)
    if vm.status == "running" and macs and agent_enabled(vm_config):
        try:
            result.extend(agent_observations(client.guest_interfaces(vm.vmid), macs, client.config))
        except (VmctlError, OSError, ValueError):
            notes.append("Guest-agent addresses are unavailable; using other observations")
    for mac in bindings.values():
        result.extend(
            IPObservation(address, "dhcp-lease") for address in snapshot.leases.get(mac, ())
        )
    for index in bindings:
        try:
            value = parse_properties(vm_config.get(f"ipconfig{index}", "")).get("ip", "")
            address = private_address(str(IPv4Interface(value).ip), client.config)
        except (VmctlError, ValueError):
            continue
        if address is not None:
            result.append(IPObservation(address, "cloud-init"))
    for mac in bindings.values():
        result.extend(
            IPObservation(address, "neighbor") for address in snapshot.neighbors.get(mac, ())
        )
    # Keep each address once, retaining the strongest available source in deterministic order.
    seen: set[IPv4Address] = set()
    observations: list[IPObservation] = []
    for item in result:
        if item.address not in seen:
            observations.append(item)
            seen.add(item.address)
    return tuple(observations), tuple(notes)
