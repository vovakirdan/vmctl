"""Shared adapter and public-key setup for workstation frontends."""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vmctl.client_config import load_client_config
from vmctl.operations import Operations
from vmctl.utils.ssh_keys import read_public_key

if TYPE_CHECKING:
    from vmctl.local import Dependencies


@dataclass(frozen=True)
class FrontendSettings:
    directory: Path
    local: bool = False


def build_operations(
    settings: FrontendSettings,
    *,
    completion: bool = False,
    local_factory: "Dependencies | None" = None,
) -> Operations:
    if settings.local:
        from vmctl.local import LocalOperations

        if local_factory is not None:
            return LocalOperations(settings.directory, factory=local_factory)
        return LocalOperations(settings.directory)
    from vmctl.ssh import SSHOperations, SSHTransport

    config = load_client_config(settings.directory)
    connection = config.connection
    if completion:
        connection = connection.model_copy(
            update={
                "connect_timeout": min(
                    connection.connect_timeout, config.client.completion_timeout
                ),
                "operation_timeout": min(
                    connection.operation_timeout, config.client.completion_timeout
                ),
            }
        )
    return SSHOperations(SSHTransport(connection))


def load_public_key(settings: FrontendSettings, path: Path | None = None) -> str | None:
    if path is not None:
        return read_public_key(path.expanduser())
    if settings.local:
        return None
    config = load_client_config(settings.directory)
    key_path = Path(config.client.ssh_key).expanduser()
    if not key_path.is_absolute():
        key_path = settings.directory / key_path
    return read_public_key(key_path)


def target_label(settings: FrontendSettings) -> str:
    return (
        "Local Proxmox host"
        if settings.local
        else load_client_config(settings.directory).connection.host
    )
