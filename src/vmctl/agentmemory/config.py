"""Worker-only configuration for the shared memory gateway."""

import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, model_validator

from vmctl.config import Configuration, StrictModel
from vmctl.errors import VmctlError


class AgentmemorySettings(StrictModel):
    endpoint: str
    ca_file: str
    management_host: str
    management_port: int = Field(default=22, ge=1, le=65535)
    management_user: str = "vmctl-memory-admin"
    management_key_file: str
    management_known_hosts_file: str
    firewall_vmid: int = Field(ge=100, lt=9000)
    firewall_ipset: str = "vmctl-memory-clients"
    timeout_seconds: int = Field(default=15, ge=1, le=120)

    @model_validator(mode="after")
    def validate_settings(self) -> "AgentmemorySettings":
        url = urlsplit(self.endpoint)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("Agentmemory endpoint must be an HTTPS origin without credentials")
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", self.management_host):
            raise ValueError("Invalid agentmemory management hostname")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", self.management_user):
            raise ValueError("Invalid agentmemory management username")
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", self.firewall_ipset):
            raise ValueError("Invalid agentmemory firewall IP-set name")
        return self


def settings(config: Configuration) -> AgentmemorySettings:
    if config.host.agentmemory is None:
        raise VmctlError(
            "System feature 'agentmemory' is unavailable: configure [agentmemory] on the worker"
        )
    try:
        return AgentmemorySettings.model_validate(config.host.agentmemory)
    except (ValidationError, VmctlError):
        raise VmctlError(
            "Invalid agentmemory configuration; check worker [agentmemory] settings"
        ) from None


def private_file(path: Path) -> None:
    try:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o077 or details.st_uid != 0:
            raise VmctlError(
                "Agentmemory management key must be a root-owned regular owner-only file"
            )
    except OSError:
        raise VmctlError("Cannot inspect agentmemory management key") from None
