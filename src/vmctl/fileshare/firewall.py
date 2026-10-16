"""Ownership-checked per-client Proxmox firewall IP-set entries."""

import json
from ipaddress import IPv4Address, IPv4Network
from typing import Protocol

from vmctl.errors import VmctlError
from vmctl.proxmox.client import ProxmoxClient


class FirewallSettings(Protocol):
    @property
    def firewall_vmid(self) -> int: ...

    @property
    def firewall_ipset(self) -> str: ...


class FileshareFirewall:
    def __init__(self, client: ProxmoxClient, settings: FirewallSettings) -> None:
        self.client = client
        self.path = (
            f"/nodes/{client.node}/qemu/{settings.firewall_vmid}"
            f"/firewall/ipset/{settings.firewall_ipset}"
        )

    def entries(self) -> list[dict[str, object]]:
        try:
            rows = json.loads(
                self.client.command("pvesh", "get", self.path, "--output-format", "json").stdout
            )
        except ValueError as exc:
            raise VmctlError("Invalid fileshare firewall IP-set response") from exc
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise VmctlError("Invalid fileshare firewall IP-set response")
        return rows

    def grant(self, address: IPv4Address, account: str) -> None:
        for row in self.entries():
            try:
                network = IPv4Network(str(row.get("cidr", "")), strict=False)
            except ValueError:
                raise VmctlError("Invalid fileshare firewall IP-set network") from None
            if address in network:
                if row.get("comment") == account and not row.get("nomatch"):
                    return
                raise VmctlError("Fileshare firewall address overlaps an existing entry")
        self.client.command(
            "pvesh",
            "create",
            self.path,
            "--cidr",
            str(address),
            "--comment",
            account,
            "--nomatch",
            "0",
        )

    def revoke(self, account: str) -> None:
        for row in self.entries():
            if row.get("comment") == account:
                try:
                    address = IPv4Network(str(row.get("cidr", "")), strict=False)
                except ValueError:
                    raise VmctlError("Invalid owned fileshare firewall IP-set network") from None
                if address.prefixlen != 32:
                    raise VmctlError("Refusing to remove a non-host fileshare firewall entry")
                self.client.command(
                    "pvesh", "delete", self.path + "/" + str(address.network_address)
                )
