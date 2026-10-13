"""Portable upload acceptance using memory SFTP and a fake Proxmox frontend."""

import errno
import io
import logging
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from ipaddress import IPv4Address
from pathlib import Path, PurePosixPath
from threading import Event
from typing import BinaryIO, cast

import pytest

from vmctl.errors import VmctlError
from vmctl.models import VM, VMDetails, VMStats
from vmctl.operations import Operations
from vmctl.transfer.backend import RemoteAttributes, TransferSession
from vmctl.transfer.models import (
    UploadCancelledError,
    UploadProgress,
    UploadRequest,
    UploadUncertainError,
)
from vmctl.transfer.service import UploadService


class FakeGuest:
    def __init__(self) -> None:
        self.details = VMDetails(
            VM(104, "upload-test", "running", "pve", cpu=2, memory_mib=2048),
            {"ciuser": "vmadmin", "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr1"},
            {},
            IPv4Address("10.210.0.104"),
        )
        self.status = "running"

    def info(self, reference: str) -> VMDetails:
        return self.details

    def stats(self, reference: str, *, history: bool = False) -> VMStats:
        return VMStats(replace(self.details.vm, status=self.status), timestamp=0)


class MemorySession:
    def __init__(self) -> None:
        self.nodes = {
            PurePosixPath("/"): RemoteAttributes(stat.S_IFDIR | 0o755, 0),
            PurePosixPath("/home"): RemoteAttributes(stat.S_IFDIR | 0o755, 0),
            PurePosixPath("/home/vmadmin"): RemoteAttributes(stat.S_IFDIR | 0o700, 0),
        }
        self.contents: dict[PurePosixPath, bytes] = {}
        self.calls: list[tuple[str, PurePosixPath]] = []
        self.rename_error: Exception | None = None
        self.rename_reply_lost = False
        self.write_error: Exception | None = None
        self.before_rename: Callable[[], None] = lambda: None

    def normalize(self, path: str) -> PurePosixPath:
        return PurePosixPath("/home/vmadmin") if path == "." else PurePosixPath(path)

    def lstat(self, path: PurePosixPath) -> RemoteAttributes | None:
        return self.nodes.get(path)

    def mkdir(self, path: PurePosixPath, mode: int = 0o700) -> None:
        if path in self.nodes:
            raise OSError(errno.EEXIST, "Already exists")
        if path.parent not in self.nodes or not stat.S_ISDIR(self.nodes[path.parent].mode):
            raise OSError(errno.ENOENT, "Parent directory missing")
        self.calls.append(("mkdir", path))
        self.nodes[path] = RemoteAttributes(stat.S_IFDIR | mode, 0)

    @contextmanager
    def open_write(self, path: PurePosixPath) -> Iterator[BinaryIO]:
        if path in self.nodes:
            raise OSError(errno.EEXIST, "Already exists")
        if path.parent not in self.nodes or not stat.S_ISDIR(self.nodes[path.parent].mode):
            raise OSError(errno.ENOENT, "Parent directory missing")
        self.calls.append(("write", path))
        self.nodes[path] = RemoteAttributes(stat.S_IFREG | 0o600, 0)
        stream = io.BytesIO()
        try:
            if self.write_error:
                raise self.write_error
            yield stream
        finally:
            data = stream.getvalue()
            self.contents[path] = data
            self.nodes[path] = replace(self.nodes[path], size=len(data))
            stream.close()

    def chmod(self, path: PurePosixPath, mode: int) -> None:
        self.nodes[path] = replace(self.nodes[path], mode=stat.S_IFMT(self.nodes[path].mode) | mode)

    def remove(self, path: PurePosixPath) -> None:
        self.calls.append(("remove", path))
        del self.nodes[path]
        self.contents.pop(path, None)

    def rmdir(self, path: PurePosixPath) -> None:
        if any(node != path and node.is_relative_to(path) for node in self.nodes):
            raise OSError(errno.ENOTEMPTY, "Not empty")
        self.calls.append(("rmdir", path))
        del self.nodes[path]

    def _move(self, source: PurePosixPath, destination: PurePosixPath, overwrite: bool) -> None:
        self.before_rename()
        if self.rename_error:
            raise self.rename_error
        if destination in self.nodes and not overwrite:
            raise OSError(errno.EEXIST, "Already exists")
        if overwrite:
            self.nodes.pop(destination, None)
            self.contents.pop(destination, None)
        for path in list(self.nodes):
            if path == source or path.is_relative_to(source):
                target = destination / path.relative_to(source)
                self.nodes[target] = self.nodes.pop(path)
                if path in self.contents:
                    self.contents[target] = self.contents.pop(path)
        if self.rename_reply_lost:
            raise EOFError("Lost publication response")

    def rename(self, source: PurePosixPath, destination: PurePosixPath) -> None:
        self.calls.append(("rename", destination))
        self._move(source, destination, False)

    def posix_rename(self, source: PurePosixPath, destination: PurePosixPath) -> None:
        self.calls.append(("posix_rename", destination))
        self._move(source, destination, True)


class MemoryBackend:
    def __init__(self) -> None:
        self.remote = MemorySession()
        self.connections: list[tuple[IPv4Address, str]] = []

    @contextmanager
    def session(
        self,
        ip: IPv4Address,
        username: str,
        *,
        cancel: Event | None = None,
    ) -> Iterator[TransferSession]:
        self.connections.append((ip, username))
        yield self.remote


@pytest.fixture
def setup(tmp_path: Path) -> tuple[FakeGuest, MemoryBackend, UploadService, Path]:
    guest, backend = FakeGuest(), MemoryBackend()
    service = UploadService(cast(Operations, guest), backend=backend)
    source = tmp_path / "hello world.txt"
    source.write_bytes(b"a private payload\n")
    return guest, backend, service, source


def test_file_upload_is_staged_and_published_once(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
) -> None:
    _, backend, service, source = setup
    preview = service.plan(UploadRequest("104", source))
    assert preview.destination == PurePosixPath("/home/vmadmin/hello world.txt")
    assert preview.file_count == 1 and preview.total_bytes == source.stat().st_size
    assert not backend.remote.calls

    def target_absent() -> None:
        assert preview.destination not in backend.remote.nodes

    backend.remote.before_rename = target_absent
    updates: list[UploadProgress] = []
    result = service.upload(preview, progress=updates.append)
    assert result.destination == preview.destination
    assert result.file_count == 1 and result.bytes_sent == preview.total_bytes
    assert backend.remote.contents[preview.destination] == source.read_bytes()
    assert updates[-1].completed_files == 1 and updates[-1].sent_bytes == preview.total_bytes
    assert sum(name == "rename" for name, _ in backend.remote.calls) == 1
    assert not any(".vmctl-upload-" in str(path) for path in backend.remote.nodes)


def test_published_file_permissions_stay_private(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
) -> None:
    _, backend, service, source = setup
    source.chmod(0o777)
    preview = service.plan(UploadRequest("104", source))
    service.upload(preview, progress=lambda _: None)
    mode = stat.S_IMODE(backend.remote.nodes[preview.destination].mode)
    assert mode & 0o077 == 0
    assert mode & stat.S_IRUSR
    assert bool(mode & stat.S_IXUSR) == bool(source.stat().st_mode & stat.S_IXUSR)


def test_directory_upload_preserves_tree_and_empty_files(tmp_path: Path) -> None:
    guest, backend = FakeGuest(), MemoryBackend()
    service = UploadService(cast(Operations, guest), backend=backend)
    source = tmp_path / "project"
    (source / "nested" / "empty-dir").mkdir(parents=True)
    (source / "nested" / "zero.txt").write_bytes(b"")
    (source / "main.py").write_bytes(b"print('hello')\n")
    preview = service.plan(UploadRequest("104", source))
    updates: list[UploadProgress] = []
    result = service.upload(preview, progress=updates.append)
    assert result.file_count == 2 and result.bytes_sent == len(b"print('hello')\n")
    assert preview.destination / "nested/empty-dir" in backend.remote.nodes
    assert backend.remote.contents[preview.destination / "nested/zero.txt"] == b""
    assert updates[-1].completed_files == 2


@pytest.mark.parametrize("status", ["stopped", "paused", "unknown"])
def test_direct_status_must_be_running(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
    status: str,
) -> None:
    guest, backend, service, source = setup
    guest.status = status
    with pytest.raises(VmctlError, match="running"):
        service.plan(UploadRequest("104", source))
    assert not backend.connections


@pytest.mark.parametrize(
    "destination", ["relative", "~other", "/tmp/../home", "/tmp\nvalue", "//tmp"]
)
def test_bad_destination_is_rejected(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
    destination: str,
) -> None:
    _, backend, service, source = setup
    with pytest.raises(VmctlError):
        service.plan(UploadRequest("104", source, destination))
    assert not backend.remote.calls


@pytest.mark.parametrize("username", ["root;bad", "-bad", "user@other", "abc\n"])
def test_bad_username_is_rejected(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
    username: str,
) -> None:
    _, backend, service, source = setup
    with pytest.raises(VmctlError, match="username"):
        service.plan(UploadRequest("104", source, username=username))
    assert not backend.connections


def test_username_override_and_metadata_precedence(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
) -> None:
    guest, _, service, source = setup
    guest.details = replace(guest.details, metadata={"username": "customuser"})
    assert service.plan(UploadRequest("104", source)).username == "customuser"
    assert service.plan(UploadRequest("104", source, username="otheruser")).username == "otheruser"


@pytest.mark.parametrize("drift", ["source", "ip", "status", "config", "target"])
def test_frozen_preview_rejects_changes_before_any_write(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
    drift: str,
) -> None:
    guest, backend, service, source = setup
    preview = service.plan(UploadRequest("104", source))
    if drift == "source":
        source.write_bytes(b"changed")
    elif drift == "ip":
        guest.details = replace(guest.details, ip=IPv4Address("10.210.0.105"))
    elif drift == "status":
        guest.status = "stopped"
    elif drift == "config":
        guest.details = replace(guest.details, config={**guest.details.config, "memory": "8192"})
    else:
        backend.remote.nodes[preview.destination] = RemoteAttributes(stat.S_IFREG | 0o600, 4)
    with pytest.raises(VmctlError):
        service.upload(preview, progress=lambda _: None)
    assert not backend.remote.calls


def test_symlink_inside_directory_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    target = tmp_path / "original"
    target.write_bytes(b"private")
    try:
        (root / "link").symlink_to(target)
    except OSError:
        pytest.skip("This workstation cannot create symlinks")
    backend = MemoryBackend()
    with pytest.raises(VmctlError, match="symlinks"):
        UploadService(cast(Operations, FakeGuest()), backend=backend).plan(
            UploadRequest("104", root)
        )
    assert not backend.connections


def test_source_control_characters_fail_before_connect(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows prevents control characters in filenames")
    source = tmp_path / "unsafe\nname"
    source.write_bytes(b"payload")
    backend = MemoryBackend()
    with pytest.raises(VmctlError, match="control"):
        UploadService(cast(Operations, FakeGuest()), backend=backend).plan(
            UploadRequest("104", source)
        )
    assert not backend.connections


def test_regular_file_overwrite_uses_atomic_extension(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
) -> None:
    _, backend, service, source = setup
    target = PurePosixPath("/home/vmadmin") / source.name
    backend.remote.nodes[target] = RemoteAttributes(stat.S_IFREG | 0o640, 3, 1)
    backend.remote.contents[target] = b"old"
    with pytest.raises(VmctlError, match="already exists"):
        service.plan(UploadRequest("104", source))
    preview = service.plan(UploadRequest("104", source, overwrite=True))
    service.upload(preview, progress=lambda _: None)
    assert backend.remote.contents[target] == source.read_bytes()
    assert ("posix_rename", target) in backend.remote.calls
    assert ("remove", target) not in backend.remote.calls


@pytest.mark.parametrize("target_type", [stat.S_IFDIR, stat.S_IFLNK])
def test_overwrite_never_replaces_directories_or_symlinks(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
    target_type: int,
) -> None:
    _, backend, service, source = setup
    target = PurePosixPath("/home/vmadmin") / source.name
    backend.remote.nodes[target] = RemoteAttributes(target_type | 0o700, 0)
    with pytest.raises(VmctlError, match="already exists"):
        service.plan(UploadRequest("104", source, overwrite=True))
    assert not backend.remote.calls


def test_partial_failure_cleans_only_staging_and_preserves_target(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
) -> None:
    _, backend, service, source = setup
    target = PurePosixPath("/home/vmadmin") / source.name
    backend.remote.nodes[target] = RemoteAttributes(stat.S_IFREG | 0o600, 3)
    backend.remote.contents[target] = b"old"
    preview = service.plan(UploadRequest("104", source, overwrite=True))
    backend.remote.write_error = OSError(errno.ENOSPC, "Disk full")
    with pytest.raises(VmctlError, match="preserved"):
        service.upload(preview, progress=lambda _: None)
    assert backend.remote.contents[target] == b"old"
    assert not any(".vmctl-upload-" in str(path) for path in backend.remote.nodes)


def test_cancellation_cleans_staging_before_publication(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
) -> None:
    _, backend, service, source = setup
    source.write_bytes(b"x" * 200000)
    preview = service.plan(UploadRequest("104", source))
    cancel = Event()

    def stop(update: UploadProgress) -> None:
        if update.sent_bytes:
            cancel.set()

    with pytest.raises(UploadCancelledError):
        service.upload(preview, progress=stop, cancel=cancel)
    assert preview.destination not in backend.remote.nodes
    assert not any(".vmctl-upload-" in str(path) for path in backend.remote.nodes)


def test_source_mutation_during_copy_does_not_publish(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
) -> None:
    _, backend, service, source = setup
    preview = service.plan(UploadRequest("104", source))

    def mutate(update: UploadProgress) -> None:
        if update.sent_bytes:
            source.write_bytes(b"changed")

    with pytest.raises(VmctlError, match="changed"):
        service.upload(preview, progress=mutate)
    assert preview.destination not in backend.remote.nodes
    assert not any(".vmctl-upload-" in str(path) for path in backend.remote.nodes)


def test_vm_stops_during_copy_target_is_not_published(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
) -> None:
    guest, backend, service, source = setup
    preview = service.plan(UploadRequest("104", source))

    def stop(update: UploadProgress) -> None:
        if update.sent_bytes:
            guest.status = "stopped"

    with pytest.raises(VmctlError, match="running"):
        service.upload(preview, progress=stop)
    assert preview.destination not in backend.remote.nodes


def test_lost_publication_response_retains_result_and_never_retries(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
) -> None:
    _, backend, service, source = setup
    preview = service.plan(UploadRequest("104", source))
    backend.remote.rename_reply_lost = True
    with pytest.raises(UploadUncertainError) as failure:
        service.upload(preview, progress=lambda _: None)
    assert failure.value.vmid == 104 and failure.value.destination == preview.destination
    assert backend.remote.contents[preview.destination] == source.read_bytes()
    assert sum(name == "rename" for name, _ in backend.remote.calls) == 1
    assert not any(name == "remove" for name, _ in backend.remote.calls)


def test_known_publish_rejection_preserves_existing_file(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
) -> None:
    _, backend, service, source = setup
    target = PurePosixPath("/home/vmadmin") / source.name
    backend.remote.nodes[target] = RemoteAttributes(stat.S_IFREG | 0o600, 3)
    backend.remote.contents[target] = b"old"
    preview = service.plan(UploadRequest("104", source, overwrite=True))
    backend.remote.rename_error = OSError(errno.EOPNOTSUPP, "Extension unavailable")
    with pytest.raises(VmctlError) as failure:
        service.upload(preview, progress=lambda _: None)
    assert not isinstance(failure.value, UploadUncertainError)
    assert backend.remote.contents[target] == b"old"
    assert ("remove", target) not in backend.remote.calls


def test_file_contents_never_appear_in_logs(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, _, service, source = setup
    marker = b"private-token-never-log-9dd4"
    source.write_bytes(marker)
    with caplog.at_level(logging.DEBUG):
        service.upload(service.plan(UploadRequest("104", source)), progress=lambda _: None)
    assert marker.decode() not in caplog.text


def test_directory_with_leading_space_child_keeps_root_first(tmp_path: Path) -> None:
    source = tmp_path / "folder"
    source.mkdir()
    (source / " leading.txt").write_bytes(b"space")
    backend = MemoryBackend()
    service = UploadService(cast(Operations, FakeGuest()), backend=backend)
    preview = service.plan(UploadRequest("104", source))
    service.upload(preview, progress=lambda _: None)
    assert backend.remote.contents[preview.destination / " leading.txt"] == b"space"
    assert stat.S_ISDIR(backend.remote.nodes[preview.destination].mode)


def test_system_symlink_parent_is_canonicalized_but_selected_symlink_rejected(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "payload.txt").write_bytes(b"payload")
    link = tmp_path / "alias"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("This workstation cannot create symlinks")
    backend = MemoryBackend()
    service = UploadService(cast(Operations, FakeGuest()), backend=backend)
    preview = service.plan(UploadRequest("104", link / "payload.txt"))
    assert preview.source == real.resolve() / "payload.txt"
    service.upload(preview, progress=lambda _: None)
    with pytest.raises(VmctlError, match="symlinks"):
        service.plan(UploadRequest("104", link))


def test_cleanup_refuses_symlinked_staging_parent(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
) -> None:
    _, backend, service, source = setup
    preview = service.plan(UploadRequest("104", source))
    cancel = Event()

    def replace_parent(update: UploadProgress) -> None:
        if update.sent_bytes:
            staging = next(
                path for path in backend.remote.nodes if path.name.startswith(".vmctl-upload-")
            )
            backend.remote.nodes[staging] = RemoteAttributes(stat.S_IFLNK | 0o777, 0)
            cancel.set()

    with pytest.raises(UploadCancelledError, match="Staging may remain"):
        service.upload(preview, progress=replace_parent, cancel=cancel)
    assert not any(name == "remove" for name, _ in backend.remote.calls)
    assert preview.destination not in backend.remote.nodes


def test_cleanup_refuses_changed_vm_network_configuration(
    setup: tuple[FakeGuest, MemoryBackend, UploadService, Path],
) -> None:
    guest, backend, service, source = setup
    preview = service.plan(UploadRequest("104", source))
    cancel = Event()

    def replace_network(update: UploadProgress) -> None:
        if update.sent_bytes:
            guest.details = replace(
                guest.details, config={**guest.details.config, "net0": "changed"}
            )
            cancel.set()

    with pytest.raises(UploadCancelledError, match="Staging may remain"):
        service.upload(preview, progress=replace_network, cancel=cancel)
    assert not any(name == "remove" for name, _ in backend.remote.calls)


@pytest.mark.parametrize("nested", [False, True])
def test_windows_junction_source_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    junction = source / "junction" if nested else source
    if nested:
        junction.mkdir()
    monkeypatch.setattr(Path, "is_junction", lambda path: path == junction)
    backend = MemoryBackend()
    service = UploadService(cast(Operations, FakeGuest()), backend=backend)
    with pytest.raises(VmctlError, match="junctions"):
        service.plan(UploadRequest("104", source))
    assert not backend.connections


def test_transport_close_failure_after_confirmed_publication_returns_success(
    tmp_path: Path,
) -> None:
    class CloseFailureBackend(MemoryBackend):
        @contextmanager
        def session(
            self,
            ip: IPv4Address,
            username: str,
            *,
            cancel: Event | None = None,
        ) -> Iterator[TransferSession]:
            yield self.remote
            if any(name == "rename" for name, _ in self.remote.calls):
                raise OSError("Closing transport failed")

    backend = CloseFailureBackend()
    source = tmp_path / "source.txt"
    source.write_bytes(b"published")
    service = UploadService(cast(Operations, FakeGuest()), backend=backend)
    preview = service.plan(UploadRequest("104", source))
    result = service.upload(preview, progress=lambda _: None)
    assert backend.remote.contents[result.destination] == b"published"
    assert result.bytes_sent == len(b"published")
    assert any("Upload succeeded" in note for note in result.notes)
    assert sum(name == "rename" for name, _ in backend.remote.calls) == 1
    assert not any(name == "remove" for name, _ in backend.remote.calls)
