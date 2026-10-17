"""Shared adapter and public-key setup for workstation frontends."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vmctl.client_config import load_client_config
from vmctl.errors import VmctlError
from vmctl.keys import PublicKeyRegistryStore, merge_public_keys
from vmctl.operations import Operations
from vmctl.utils.ssh_keys import read_public_key

if TYPE_CHECKING:
    from vmctl.local import Dependencies
    from vmctl.transfer.service import UploadService


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


def key_registry(settings: FrontendSettings) -> PublicKeyRegistryStore:
    if settings.local:
        from vmctl.config import load_config

        filename = load_config(settings.directory).host.ssh_keys_file
    else:
        filename = load_client_config(settings.directory).client.ssh_keys_file
    path = Path(filename).expanduser()
    if not path.is_absolute():
        path = settings.directory / path
    return PublicKeyRegistryStore(path)


def load_public_key(
    settings: FrontendSettings, path: Path | None = None, *, names: Sequence[str] | None = None
) -> str | None:
    # Preserve the local worker's deferred key loading when no registry is needed.
    if settings.local and not (settings.directory / "config.toml").exists():
        if names is not None:
            raise VmctlError("Host configuration is required to select named SSH keys")
        return read_public_key(path.expanduser()) if path is not None else None
    store = key_registry(settings)
    selected = store.load().default_keys if names is None else names
    if settings.local and path is None and not selected:
        return None
    if path is None:
        if settings.local:
            from vmctl.config import load_config

            filename = load_config(settings.directory).host.ssh_key
        else:
            filename = load_client_config(settings.directory).client.ssh_key
        path = Path(filename).expanduser()
        if not path.is_absolute():
            path = settings.directory / path
    primary = read_public_key(path.expanduser())
    return merge_public_keys(primary, store.resolve(selected)) if selected else primary


def target_label(settings: FrontendSettings) -> str:
    return (
        "Local Proxmox host"
        if settings.local
        else load_client_config(settings.directory).connection.host
    )


def build_upload_service(settings: FrontendSettings, backend: Operations) -> "UploadService":
    from vmctl.transfer.service import UploadService

    if settings.local:
        from vmctl.config import load_config

        username = load_config(settings.directory).host.username
    else:
        username = load_client_config(settings.directory).client.guest_username
    return UploadService(backend, default_username=username)
