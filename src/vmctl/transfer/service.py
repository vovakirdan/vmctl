"""Reusable upload selection, validation, staging, and atomic publication."""

import errno
import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from threading import Event
from uuid import uuid4

from vmctl.errors import VmctlError
from vmctl.models import VMDetails
from vmctl.operations import Operations
from vmctl.transfer.backend import RemoteAttributes, TransferBackend, TransferSession
from vmctl.transfer.models import (
    UploadCancelledError,
    UploadPreview,
    UploadProgress,
    UploadRequest,
    UploadResult,
    UploadUncertainError,
)
from vmctl.transfer.openssh import OpenSSHBackend


@dataclass(frozen=True)
class _Entry:
    relative: str
    mode: int
    size: int
    mtime_ns: int
    device: int
    inode: int

    @property
    def directory(self) -> bool:
        return stat.S_ISDIR(self.mode)


def _safe_text(value: str, label: str) -> None:
    if not value or any(unicodedata.category(char).startswith("C") for char in value):
        raise VmctlError(f"{label} must not be empty or contain control characters")


def _entry(path: Path, relative: str) -> _Entry:
    _safe_text(path.name, "Source filename")
    info = path.lstat()
    if path.is_junction() or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise VmctlError(
            "Upload sources must contain only regular files and directories; no symlinks or junctions"
        )
    return _Entry(relative, info.st_mode, info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino)


def _manifest(source: Path) -> tuple[_Entry, ...]:
    try:
        root = _entry(source, ".")
        entries = [root]
        if root.directory:
            pending = [source]
            while pending:
                directory = pending.pop()
                for child in sorted(directory.iterdir(), key=lambda item: item.name):
                    entry = _entry(child, child.relative_to(source).as_posix())
                    entries.append(entry)
                    if entry.directory:
                        pending.append(child)
        return (root, *sorted(entries[1:], key=lambda entry: entry.relative))
    except OSError as exc:
        raise VmctlError("Cannot inspect the selected upload source") from exc


def _cancelled(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise UploadCancelledError("Upload cancelled before publication")


def _remote_destination(session: TransferSession, request: UploadRequest) -> PurePosixPath:
    value = request.destination_directory
    _safe_text(value, "Guest destination")
    if value == "~" or value.startswith("~/"):
        destination = session.normalize(".") / value[2:] if value != "~" else session.normalize(".")
    else:
        destination = PurePosixPath(value)
    if (
        not destination.is_absolute()
        or ".." in destination.parts
        or str(destination).startswith("//")
    ):
        raise VmctlError("Guest destination must be an absolute POSIX directory or ~/path")
    destination = session.normalize(str(destination))
    _safe_text(str(destination), "Resolved guest destination")
    if (
        not destination.is_absolute()
        or ".." in destination.parts
        or str(destination).startswith("//")
    ):
        raise VmctlError("Guest SFTP returned an invalid destination directory")
    for path in (*reversed(destination.parents), destination):
        attrs = session.lstat(path)
        if attrs is None or not stat.S_ISDIR(attrs.mode):
            raise VmctlError(
                "Guest destination must be an existing directory without symlink parents"
            )
    return destination


def _check_target(
    session: TransferSession,
    target: PurePosixPath,
    request: UploadRequest,
    directory: bool,
) -> RemoteAttributes | None:
    attrs = session.lstat(target)
    if attrs is not None and (not request.overwrite or directory or not stat.S_ISREG(attrs.mode)):
        raise VmctlError(
            "Guest target already exists. Only regular files can be replaced with --overwrite; "
            "directories are never merged or deleted"
        )
    if request.overwrite and directory:
        raise VmctlError("--overwrite is supported only for a selected regular file")
    return attrs


def _fingerprint(
    details: VMDetails,
    username: str,
    source: Path,
    entries: tuple[_Entry, ...],
    destination: PurePosixPath,
    attrs: RemoteAttributes | None,
    request: UploadRequest,
) -> str:
    payload = {
        "vm": asdict(details.vm),
        "config": details.config,
        "metadata": details.metadata,
        "ip": str(details.display_ip),
        "username": username,
        "source": str(source),
        "entries": [asdict(entry) for entry in entries],
        "destination": str(destination),
        "target": asdict(attrs) if attrs is not None else None,
        "overwrite": request.overwrite,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class UploadService:
    def __init__(
        self,
        operations: Operations,
        *,
        default_username: str = "vmadmin",
        backend: TransferBackend | None = None,
    ) -> None:
        self.operations, self.default_username = operations, default_username
        self.backend = backend or OpenSSHBackend()

    def _vm(self, reference: str) -> VMDetails:
        details = self.operations.info(reference)
        current = self.operations.stats(str(details.vm.vmid)).vm
        if (current.vmid, current.node, current.name) != (
            details.vm.vmid,
            details.vm.node,
            details.vm.name,
        ):
            raise VmctlError("VM identity changed; refresh the inventory and preview again")
        if current.template or 9000 <= current.vmid <= 9099 or current.status != "running":
            raise VmctlError("File upload requires a running, non-template VM")
        if (
            details.display_ip is None
            or details.display_ip.is_unspecified
            or details.display_ip.is_multicast
        ):
            raise VmctlError("VM has no usable IPv4 address; refresh its network information")
        return replace(details, vm=replace(details.vm, status=current.status))

    def _username(self, details: VMDetails, request: UploadRequest) -> str:
        username = (
            request.username
            or details.metadata.get("username")
            or details.config.get("ciuser")
            or self.default_username
        )
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?", username):
            raise VmctlError("Guest SSH username must be a valid Linux account name")
        return username

    def plan(self, request: UploadRequest) -> UploadPreview:
        details = self._vm(request.reference)
        username = self._username(details, request)
        source = request.source.expanduser().absolute()
        _safe_text(str(source), "Source path")
        if source.name in {"", ".", ".."}:
            raise VmctlError("Select a named file or directory, not a filesystem root")
        try:
            _entry(source, ".")
            source = source.parent.resolve(strict=True) / source.name
        except OSError as exc:
            raise VmctlError("Cannot inspect the selected upload source") from exc
        entries = _manifest(source)
        assert details.display_ip is not None
        try:
            with self.backend.session(details.display_ip, username) as session:
                destination = _remote_destination(session, request) / source.name
                attrs = _check_target(session, destination, request, entries[0].directory)
        except VmctlError:
            raise
        except Exception as exc:
            raise VmctlError(
                "Cannot inspect guest destination; check SFTP access and permissions"
            ) from exc
        files = [entry for entry in entries if not entry.directory]
        notes = (
            "Files go directly from this computer to the guest over SSH/SFTP.",
            "The selected basename is appended to the destination directory; directories are not merged.",
            "Upload is staged privately and published with one rename; guest account permissions apply.",
        )
        return UploadPreview(
            details.vm,
            details.display_ip,
            username,
            source,
            destination,
            len(files),
            sum(entry.size for entry in files),
            _fingerprint(details, username, source, entries, destination, attrs, request),
            request,
            notes,
        )

    def upload(
        self,
        expected: UploadPreview,
        *,
        progress: Callable[[UploadProgress], None],
        cancel: Event | None = None,
    ) -> UploadResult:
        _cancelled(cancel)
        fresh = self.plan(expected.request)
        if fresh != expected:
            raise VmctlError("VM, source, or guest target changed; preview the upload again")
        entries = _manifest(expected.source)
        stage = expected.destination.parent / f".vmctl-upload-{uuid4().hex}"
        staged_target = stage / expected.source.name
        created_files: list[PurePosixPath] = []
        created_dirs: list[PurePosixPath] = []
        publication = False
        result: UploadResult | None = None
        identity: VMDetails | None = None
        sent, completed = 0, 0
        try:
            with self.backend.session(expected.ip, expected.username, cancel=cancel) as session:
                parent = _remote_destination(session, expected.request)
                if parent != expected.destination.parent:
                    raise VmctlError("Guest destination changed; preview the upload again")
                initial = _check_target(
                    session, expected.destination, expected.request, entries[0].directory
                )
                identity = self._recheck(expected, entries, initial)
                _cancelled(cancel)
                session.mkdir(stage)
                created_dirs.append(stage)
                progress(
                    UploadProgress(
                        "Preparing upload",
                        sent,
                        expected.total_bytes,
                        completed,
                        expected.file_count,
                    )
                )
                for entry in entries:
                    _cancelled(cancel)
                    remote = (
                        staged_target if entry.relative == "." else staged_target / entry.relative
                    )
                    local = (
                        expected.source
                        if entry.relative == "."
                        else expected.source / entry.relative
                    )
                    if entry.directory:
                        session.mkdir(remote)
                        created_dirs.append(remote)
                        continue
                    created_files.append(remote)
                    sent = self._copy_file(
                        session, local, remote, entry, expected, sent, completed, progress, cancel
                    )
                    completed += 1
                    progress(
                        UploadProgress(
                            entry.relative,
                            sent,
                            expected.total_bytes,
                            completed,
                            expected.file_count,
                        )
                    )
                _cancelled(cancel)
                for entry in reversed(entries):
                    remote = (
                        staged_target if entry.relative == "." else staged_target / entry.relative
                    )
                    session.chmod(remote, stat.S_IMODE(entry.mode) & 0o777)
                attrs = _check_target(
                    session, expected.destination, expected.request, entries[0].directory
                )
                if attrs != initial or _remote_destination(session, expected.request) != parent:
                    raise VmctlError("Guest target changed during upload; nothing was published")
                self._recheck(expected, _manifest(expected.source), attrs)
                _cancelled(cancel)
                publication = True
                if expected.request.overwrite and attrs is not None:
                    session.posix_rename(staged_target, expected.destination)
                else:
                    session.rename(staged_target, expected.destination)
                result = UploadResult(expected.vm.vmid, expected.destination, completed, sent)
                publication = False
                created_files.clear()
                created_dirs = [stage]
                notes: tuple[str, ...] = ()
                try:
                    session.rmdir(stage)
                    created_dirs.clear()
                except Exception:
                    notes = (f"Upload succeeded; empty staging directory may remain: {stage}",)
                result = replace(result, notes=notes)
                return result
        except BaseException as exc:
            if result is not None:
                return replace(
                    result,
                    notes=(
                        *result.notes,
                        "Upload succeeded; final connection or staging cleanup did not complete. "
                        f"An empty staging directory may remain: {stage}",
                    ),
                )
            if publication and not (
                isinstance(exc, OSError)
                and exc.errno
                in {
                    errno.EACCES,
                    errno.EPERM,
                    errno.EEXIST,
                    errno.ENOTEMPTY,
                    errno.ENOENT,
                    errno.EOPNOTSUPP,
                }
            ):
                raise UploadUncertainError(expected.vm.vmid, expected.destination) from exc
            retained = self._cleanup(expected, created_files, created_dirs, identity)
            detail = f" Staging may remain at {stage}." if retained else ""
            if isinstance(exc, UploadCancelledError) or isinstance(exc, KeyboardInterrupt):
                raise UploadCancelledError(f"Upload cancelled before publication.{detail}") from exc
            if isinstance(exc, VmctlError):
                raise VmctlError(f"{exc}{detail}") from exc
            raise VmctlError(
                f"Upload failed before publication; the final target was preserved.{detail}"
            ) from exc

    def _recheck(
        self,
        expected: UploadPreview,
        entries: tuple[_Entry, ...],
        attrs: RemoteAttributes | None,
    ) -> VMDetails:
        details = self._vm(str(expected.vm.vmid))
        if (
            _fingerprint(
                details,
                self._username(details, expected.request),
                expected.source,
                entries,
                expected.destination,
                attrs,
                expected.request,
            )
            != expected.fingerprint
        ):
            raise VmctlError("VM, source, or guest target changed; nothing was published")
        return details

    def _copy_file(
        self,
        session: TransferSession,
        local: Path,
        remote: PurePosixPath,
        entry: _Entry,
        expected: UploadPreview,
        sent: int,
        completed: int,
        progress: Callable[[UploadProgress], None],
        cancel: Event | None,
    ) -> int:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(local, flags), "rb") as source, session.open_write(remote) as target:
            if _entry(local, entry.relative) != entry:
                raise VmctlError("Source file changed; nothing was published")
            before = source.fileno()
            info = os.fstat(before)
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                entry.device,
                entry.inode,
                entry.size,
                entry.mtime_ns,
            ):
                raise VmctlError("Source file changed while opening it; nothing was published")
            count = 0
            while chunk := source.read(65536):
                _cancelled(cancel)
                target.write(chunk)
                count += len(chunk)
                sent += len(chunk)
                if count > entry.size:
                    raise VmctlError("Source file grew during upload; nothing was published")
                progress(
                    UploadProgress(
                        entry.relative, sent, expected.total_bytes, completed, expected.file_count
                    )
                )
            if count != entry.size or _entry(local, entry.relative) != entry:
                raise VmctlError("Source file changed during upload; nothing was published")
        attrs = session.lstat(remote)
        if attrs is None or not stat.S_ISREG(attrs.mode) or attrs.size != entry.size:
            raise VmctlError("Guest staging file size could not be verified; nothing was published")
        return sent

    def _cleanup(
        self,
        expected: UploadPreview,
        files: list[PurePosixPath],
        directories: list[PurePosixPath],
        identity: VMDetails | None,
    ) -> bool:
        if not files and not directories:
            return False
        try:
            details = self._vm(str(expected.vm.vmid))
            if identity is None or (
                details.vm.name != expected.vm.name
                or details.vm.node != expected.vm.node
                or details.display_ip != expected.ip
                or details.config != identity.config
                or details.metadata != identity.metadata
            ):
                return True
            with self.backend.session(expected.ip, expected.username) as session:
                for path in (
                    *reversed(expected.destination.parent.parents),
                    expected.destination.parent,
                    *directories,
                ):
                    attrs = session.lstat(path)
                    if attrs is None or not stat.S_ISDIR(attrs.mode):
                        return True
                for path in directories:
                    attrs = session.lstat(path)
                    if attrs is not None:
                        if not stat.S_ISDIR(attrs.mode):
                            return True
                        session.chmod(path, 0o700)
                for path in reversed(files):
                    attrs = session.lstat(path)
                    if attrs is not None:
                        if not stat.S_ISREG(attrs.mode):
                            return True
                        session.remove(path)
                for path in reversed(directories):
                    attrs = session.lstat(path)
                    if attrs is not None:
                        if not stat.S_ISDIR(attrs.mode):
                            return True
                        session.rmdir(path)
            return False
        except Exception:
            return True
