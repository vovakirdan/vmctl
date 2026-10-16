"""Validated, worker-only fileshare configuration."""

import re
import stat
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, model_validator

from vmctl.config import Configuration, StrictModel
from vmctl.errors import VmctlError
from vmctl.utils.ssh_keys import validate_public_key


class FileshareSettings(StrictModel):
    endpoint: str
    sftp_host: str
    sftp_port: int = Field(default=2022, ge=1, le=65535)
    host_public_key: str
    ca_file: str
    api_key_file: str
    shared_directory: str = "/srv/files/shared"
    firewall_vmid: int = Field(ge=100, lt=9000)
    firewall_ipset: str = "vmctl-files-clients"
    timeout_seconds: int = Field(default=15, ge=1, le=120)

    @model_validator(mode="after")
    def validate_settings(self) -> "FileshareSettings":
        endpoint = urlsplit(self.endpoint)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path not in {"", "/"}
        ):
            raise ValueError("Fileshare endpoint must be an HTTPS origin without credentials")
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", self.sftp_host):
            raise ValueError("Invalid SFTP hostname")
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", self.firewall_ipset):
            raise ValueError("Invalid fileshare firewall IP-set name")
        if len([line for line in self.host_public_key.splitlines() if line.strip()]) != 1:
            raise ValueError("Fileshare host_public_key must contain exactly one public key")
        validate_public_key(self.host_public_key)
        path = PurePosixPath(self.shared_directory)
        if not path.is_absolute() or ".." in path.parts or str(path) == "/":
            raise ValueError("Fileshare shared_directory must be a dedicated absolute path")
        return self


def settings(config: Configuration) -> FileshareSettings:
    if config.host.fileshare is None:
        raise VmctlError(
            "System feature 'fileshare' is unavailable: configure [fileshare] on the worker"
        )
    # Stored as a mapping in HostConfig to keep configuration imports acyclic.
    try:
        return FileshareSettings.model_validate(config.host.fileshare)
    except (ValidationError, VmctlError):
        raise VmctlError(
            "Invalid fileshare configuration; check worker [fileshare] settings"
        ) from None


def read_api_key(config: Configuration, value: FileshareSettings) -> str:
    path = config.path(value.api_key_file)
    try:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o077:
            raise VmctlError("Fileshare API key must be a regular owner-only file (0600)")
        if details.st_uid != 0:
            raise VmctlError("Fileshare API key must be owned by root")
        key = path.read_text().strip()
        if not key or any(c.isspace() for c in key):
            raise VmctlError("Invalid fileshare API key file")
        return key
    except OSError as exc:
        raise VmctlError("Cannot read fileshare API key file") from exc
