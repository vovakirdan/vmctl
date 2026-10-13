"""Typed upload requests, previews, progress, and actionable outcomes."""

from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path, PurePosixPath

from vmctl.errors import VmctlError
from vmctl.models import VM


@dataclass(frozen=True)
class UploadRequest:
    reference: str
    source: Path
    destination_directory: str = "~"
    username: str | None = None
    overwrite: bool = False


@dataclass(frozen=True)
class UploadPreview:
    vm: VM
    ip: IPv4Address
    username: str
    source: Path
    destination: PurePosixPath
    file_count: int
    total_bytes: int
    fingerprint: str
    request: UploadRequest
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class UploadProgress:
    current_path: str
    sent_bytes: int
    total_bytes: int
    completed_files: int
    total_files: int


@dataclass(frozen=True)
class UploadResult:
    vmid: int
    destination: PurePosixPath
    file_count: int
    bytes_sent: int
    notes: tuple[str, ...] = ()


class UploadCancelledError(VmctlError):
    """The user cancelled before the final target was published."""


class UploadUncertainError(VmctlError):
    """Publication was submitted but its response could not be verified."""

    def __init__(self, vmid: int, destination: PurePosixPath) -> None:
        self.vmid = vmid
        self.destination = destination
        super().__init__(
            f"Upload publication for VM {vmid} is uncertain. Inspect {destination} "
            "before retrying; no automatic retry or target cleanup was performed"
        )
