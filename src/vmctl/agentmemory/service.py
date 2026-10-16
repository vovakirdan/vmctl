"""Per-invocation memory credentials, firewall access and compensation."""

import re
import secrets
import ssl
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from urllib.error import HTTPError
from urllib.request import urlopen

from vmctl.agentmemory.client import GatewayClient
from vmctl.agentmemory.config import AgentmemorySettings, settings
from vmctl.config import Configuration
from vmctl.errors import VmctlError
from vmctl.fileshare.firewall import FileshareFirewall
from vmctl.proxmox.client import ProxmoxClient


@dataclass(frozen=True)
class MemoryAccess:
    client_id: str
    endpoint: str
    owner: str
    secret: str = field(repr=False)


def generate_access(value: AgentmemorySettings, vmid: int, token: str) -> MemoryAccess:
    if not re.fullmatch(r"[0-9a-f]{32}", token) or not 100 <= vmid < 9000:
        raise VmctlError("Invalid agentmemory access ownership")
    return MemoryAccess(
        f"vmctl-{vmid}-{token}", value.endpoint, f"vmctl:{token}", secrets.token_urlsafe(48)
    )


class AgentmemoryService:
    def __init__(self, config: Configuration, proxmox: ProxmoxClient) -> None:
        self.config = config
        self.settings = settings(config)
        self.client = GatewayClient(config, self.settings)
        self.firewall = FileshareFirewall(proxmox, self.settings)
        try:
            self.ca_certificate = config.path(self.settings.ca_file).read_text()
        except OSError:
            raise VmctlError("Cannot read agentmemory TLS trust certificate") from None
        self._attempted: dict[str, str] = {}

    def preflight(self) -> None:
        self.firewall.entries()
        self.client.request("preflight")
        try:
            context = ssl.create_default_context(
                cafile=str(self.config.path(self.settings.ca_file))
            )
            with urlopen(
                self.settings.endpoint.rstrip("/") + "/agentmemory/health",
                context=context,
                timeout=self.settings.timeout_seconds,
            ):
                raise VmctlError(
                    "Agentmemory HTTPS gateway must deny requests without a client credential"
                )
        except HTTPError as exc:
            if exc.code != 401:
                raise VmctlError("Agentmemory HTTPS gateway is not ready") from None
        except (OSError, ValueError):
            raise VmctlError(
                "Cannot verify agentmemory HTTPS gateway with its pinned certificate"
            ) from None

    def provision(self, access: MemoryAccess, address: IPv4Address) -> None:
        self._attempted[access.client_id] = access.secret
        self.client.request(
            "grant",
            client_id=access.client_id,
            owner=access.owner,
            address=str(address),
            secret=access.secret,
        )
        self.firewall.grant(address, access.client_id)

    def revoke(self, client_id: str, token: str, *, rollback: bool = False) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", token) or not re.fullmatch(
            rf"vmctl-[0-9]+-{token}", client_id
        ):
            raise VmctlError("Agentmemory client ownership metadata is invalid")
        if rollback and client_id not in self._attempted:
            return
        credentials = {"secret": self._attempted[client_id]} if rollback else {}
        self.client.request("revoke", client_id=client_id, owner=f"vmctl:{token}", **credentials)
        self.firewall.revoke(client_id)
