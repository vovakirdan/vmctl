"""Workstation settings, independent of Proxmox and Linux-only adapters."""

import re
import tomllib
from pathlib import Path, PurePosixPath

from platformdirs import user_config_path
from pydantic import Field, ValidationError, model_validator

from vmctl.config import StrictModel
from vmctl.errors import VmctlError
from vmctl.services.init_config import InitConfigResult, bundled_defaults
from vmctl.utils.atomic import atomic_create


class ConnectionConfig(StrictModel):
    host: str
    worker: str = "/opt/vmctl/bin/vmctl-worker"
    config_dir: str = "/etc/vmctl"
    sudo: bool = False
    connect_timeout: int = Field(default=10, gt=0, le=300)
    operation_timeout: int = Field(default=3600, gt=0)

    @model_validator(mode="after")
    def validate_connection(self) -> "ConnectionConfig":
        if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.@:-]*", self.host):
            raise ValueError("host must be an OpenSSH alias or user@host without whitespace")
        for value in (self.worker, self.config_dir):
            if not PurePosixPath(value).is_absolute() or "\x00" in value or "\n" in value:
                raise ValueError("worker and config_dir must be absolute remote POSIX paths")
        return self


class ClientSettings(StrictModel):
    ssh_key: str = "~/.ssh/id_ed25519.pub"
    completion_timeout: int = Field(default=3, gt=0, le=30)


class ClientConfiguration(StrictModel):
    connection: ConnectionConfig
    client: ClientSettings = Field(default_factory=ClientSettings)


def default_client_directory() -> Path:
    return user_config_path("vmctl", appauthor=False, roaming=True)


def load_client_config(directory: Path) -> ClientConfiguration:
    path = directory.expanduser() / "client.toml"
    try:
        with path.open("rb") as source:
            return ClientConfiguration.model_validate(tomllib.load(source))
    except FileNotFoundError as exc:
        raise VmctlError(f"Client configuration missing: {path}. Run vmctl config init") from exc
    except (OSError, ValueError, ValidationError) as exc:
        # Do not echo arbitrary configuration contents in validation failures.
        raise VmctlError(f"Cannot load client configuration: {path}") from exc


def initialize_client_config(directory: Path) -> InitConfigResult:
    directory = directory.expanduser().absolute()
    path = directory / "client.toml"
    for parent in (directory, *directory.parents):
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise VmctlError(f"Configuration directory is not a regular directory: {parent}")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise VmctlError(f"Configuration destination is not a regular file: {path}")
    created = atomic_create(
        path, bundled_defaults().joinpath("client.toml").read_bytes(), mode=0o600
    )
    return InitConfigResult(directory, (path,) if created else (), () if created else (path,))
