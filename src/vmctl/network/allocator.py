"""Allocate from live reservations, leases and optional ARP conflict probes."""

import json
from collections.abc import Callable, Iterable, Iterator
from ipaddress import IPv4Address
from pathlib import Path

from vmctl.config import Configuration
from vmctl.errors import VmctlError
from vmctl.utils.subprocess import Runner


def lease_addresses(path: Path) -> set[IPv4Address]:
    if not path.exists():
        return set()
    addresses: set[IPv4Address] = set()
    for line in path.read_text().splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 5:
            raise VmctlError(f"Malformed dnsmasq lease entry in {path}")
        try:
            addresses.add(IPv4Address(fields[2]))
        except ValueError as exc:
            raise VmctlError(f"Malformed lease IP in {path}") from exc
    return addresses


def allocate_ip(
    start: IPv4Address,
    end: IPv4Address,
    occupied: Iterable[IPv4Address],
    *,
    requested: str = "auto",
    responds: Callable[[IPv4Address], bool] | None = None,
) -> IPv4Address:
    used = set(occupied)
    candidates: Iterator[IPv4Address]
    if requested == "auto":
        candidates = (IPv4Address(value) for value in range(int(start), int(end) + 1))
    else:
        try:
            address = IPv4Address(requested)
        except ValueError as exc:
            raise VmctlError(f"Invalid IPv4 address: {requested}") from exc
        if not start <= address <= end:
            raise VmctlError("Requested IP must belong to the managed pool")
        candidates = iter((address,))
    for address in candidates:
        if address not in used and (responds is None or not responds(address)):
            return address
    raise VmctlError("No available IP address in the requested pool")


class AddressAllocator:
    def __init__(self, config: Configuration, runner: Runner) -> None:
        self.config, self.runner = config, runner

    def neighbors(self) -> set[IPv4Address]:
        binary = self.config.host.binaries.get("ip", "ip")
        result = self.runner.run(
            [binary, "-j", "neigh", "show", "dev", self.config.host.network.bridge]
        )
        try:
            data = json.loads(result.stdout)
            if not isinstance(data, list):
                raise ValueError("Expected an array")
            return {
                IPv4Address(entry["dst"])
                for entry in data
                if ":" not in entry.get("dst", "")
                and not set(entry.get("state", [])).intersection({"FAILED", "INCOMPLETE"})
            }
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise VmctlError("Cannot parse the host neighbor table") from exc

    def responds(self, address: IPv4Address) -> bool:
        binary = self.config.host.binaries.get("arping", "arping")
        result = self.runner.run(
            [
                binary,
                "-D",
                "-I",
                self.config.host.network.bridge,
                "-c",
                "2",
                "-w",
                "2",
                str(address),
            ],
            timeout=5,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise VmctlError(f"ARP probe failed for {address}: exit {result.returncode}")
        return result.returncode == 1

    def select(self, reserved: Iterable[IPv4Address], requested: str = "auto") -> IPv4Address:
        network = self.config.host.network
        occupied = (
            set(reserved) | lease_addresses(self.config.path(network.leases)) | self.neighbors()
        )
        return allocate_ip(
            IPv4Address(network.pool_start),
            IPv4Address(network.pool_end),
            occupied,
            requested=requested,
            responds=self.responds if network.probe else None,
        )
