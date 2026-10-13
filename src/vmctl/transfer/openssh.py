"""Binary SFTP over native OpenSSH pipes, including native Windows clients."""

import errno
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import PurePosixPath
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import BinaryIO, cast

from paramiko import Channel, SFTPClient

from vmctl.errors import VmctlError
from vmctl.transfer.backend import RemoteAttributes, TransferSession
from vmctl.transfer.models import UploadCancelledError

ProcessFactory = Callable[[Sequence[str]], subprocess.Popen[bytes]]


def _start_process(arguments: Sequence[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        list(arguments),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


@dataclass
class _Write:
    data: bytes
    done: Event = field(default_factory=Event)
    error: OSError | None = None


class PipeSocket:
    """Socket-like bounded pipe I/O; select() cannot handle Windows pipe handles."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        timeout: float = 30,
        cancel: Event | None = None,
    ) -> None:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise ValueError("OpenSSH requires binary stdin, stdout, and stderr pipes")
        self.process, self.timeout, self.cancel = process, timeout, cancel
        self._closed = Event()
        self._input: Queue[bytes | None] = Queue(maxsize=8)
        self._writes: Queue[_Write] = Queue(maxsize=1)
        self._pending = bytearray()
        self._diagnostics = bytearray()
        self._threads = [
            Thread(target=self._read, args=(process.stdout,), daemon=True),
            Thread(target=self._drain_errors, args=(process.stderr,), daemon=True),
            Thread(target=self._write, args=(process.stdin,), daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def get_name(self) -> str:
        return "vmctl-upload"

    def _read(self, stream: BinaryIO) -> None:
        try:
            while not self._closed.is_set():
                data = stream.read(65536)
                while not self._closed.is_set():
                    try:
                        self._input.put(data or None, timeout=0.1)
                        break
                    except Full:
                        continue
                if not data:
                    return
        except (OSError, ValueError):
            if not self._closed.is_set():
                try:
                    self._input.put(None, timeout=0.1)
                except Full:
                    pass

    def _drain_errors(self, stream: BinaryIO) -> None:
        try:
            while not self._closed.is_set():
                data = stream.read(4096)
                if not data:
                    return
                self._diagnostics.extend(data[: max(0, 4096 - len(self._diagnostics))])
        except (OSError, ValueError):
            return

    def _write(self, stream: BinaryIO) -> None:
        while not self._closed.is_set():
            try:
                request = self._writes.get(timeout=0.1)
            except Empty:
                continue
            try:
                remaining = memoryview(request.data)
                while remaining:
                    count = stream.write(remaining)
                    if not count:
                        raise OSError("OpenSSH input pipe closed")
                    remaining = remaining[count:]
                stream.flush()
            except (OSError, ValueError) as exc:
                request.error = OSError("OpenSSH input pipe closed")
                request.error.__cause__ = exc
            finally:
                request.done.set()

    def _check(self, deadline: float) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise UploadCancelledError("Upload cancelled before publication")
        if self._closed.is_set():
            raise EOFError("OpenSSH SFTP channel closed")
        if time.monotonic() >= deadline:
            raise TimeoutError("OpenSSH SFTP operation timed out")

    def send(self, data: bytes) -> int:
        deadline = time.monotonic() + self.timeout
        request = _Write(data[:65536])
        while True:
            self._check(deadline)
            try:
                self._writes.put(request, timeout=0.1)
                break
            except Full:
                continue
        while not request.done.wait(0.1):
            self._check(deadline)
        if request.error:
            raise request.error
        return len(request.data)

    def recv(self, count: int) -> bytes:
        if count <= 0:
            return b""
        deadline = time.monotonic() + self.timeout
        while not self._pending:
            self._check(deadline)
            try:
                data = self._input.get(timeout=0.1)
            except Empty:
                continue
            if data is None:
                return b""
            self._pending.extend(data)
        data = bytes(self._pending[:count])
        del self._pending[:count]
        return data

    def failure_message(self) -> str:
        diagnostics = bytes(self._diagnostics).lower()
        if (
            b"host key verification failed" in diagnostics
            or b"host identification has changed" in diagnostics
        ):
            return (
                "Guest SSH host key is unknown or changed. Verify the guest host key and "
                "connect once with ssh before uploading; strict host-key checking is enabled"
            )
        if b"permission denied" in diagnostics:
            return "Guest SSH authentication failed; configure the local SSH key or agent"
        return "Cannot open guest SFTP; check SSH access and the guest SFTP subsystem"

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with suppress(OSError, subprocess.TimeoutExpired):
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                with suppress(OSError, ValueError):
                    stream.close()
        for thread in self._threads:
            thread.join(timeout=1)


class _QuietSFTPClient(SFTPClient):
    def _log(self, level: int, msg: str | list[str], *args: object) -> None:
        # Library debug messages may contain untrusted paths; never log file payloads.
        return


class OpenSSHSession:
    def __init__(self, client: SFTPClient) -> None:
        self.client = client

    def normalize(self, path: str) -> PurePosixPath:
        return PurePosixPath(self.client.normalize(path))

    def lstat(self, path: PurePosixPath) -> RemoteAttributes | None:
        try:
            attrs = self.client.lstat(str(path))
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return None
            raise
        if attrs.st_mode is None or attrs.st_size is None:
            raise VmctlError("Guest SFTP returned incomplete file attributes")
        return RemoteAttributes(attrs.st_mode, attrs.st_size, int(attrs.st_mtime or 0))

    def mkdir(self, path: PurePosixPath, mode: int = 0o700) -> None:
        self.client.mkdir(str(path), mode)

    def open_write(self, path: PurePosixPath) -> AbstractContextManager[BinaryIO]:
        return cast(AbstractContextManager[BinaryIO], self.client.open(str(path), "wx", bufsize=0))

    def chmod(self, path: PurePosixPath, mode: int) -> None:
        self.client.chmod(str(path), mode)

    def remove(self, path: PurePosixPath) -> None:
        self.client.remove(str(path))

    def rmdir(self, path: PurePosixPath) -> None:
        self.client.rmdir(str(path))

    def rename(self, source: PurePosixPath, destination: PurePosixPath) -> None:
        self.client.rename(str(source), str(destination))

    def posix_rename(self, source: PurePosixPath, destination: PurePosixPath) -> None:
        self.client.posix_rename(str(source), str(destination))


class OpenSSHBackend:
    def __init__(
        self,
        *,
        executable: str = "ssh",
        connect_timeout: int = 10,
        io_timeout: float = 30,
        process_factory: ProcessFactory = _start_process,
    ) -> None:
        self.executable, self.connect_timeout = executable, connect_timeout
        self.io_timeout, self.process_factory = io_timeout, process_factory

    @contextmanager
    def session(
        self, ip: IPv4Address, username: str, *, cancel: Event | None = None
    ) -> Iterator[TransferSession]:
        arguments = [
            self.executable,
            "-T",
            "-s",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ForwardX11=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            f"{username}@{ip}",
            "sftp",
        ]
        try:
            process = self.process_factory(arguments)
        except OSError as exc:
            raise VmctlError("Cannot start OpenSSH; install the native ssh client") from exc
        socket = PipeSocket(process, timeout=self.io_timeout, cancel=cancel)
        try:
            try:
                client = _QuietSFTPClient(cast(Channel, socket))
            except UploadCancelledError:
                raise
            except Exception as exc:
                socket.close()
                raise VmctlError(socket.failure_message()) from exc
            try:
                yield OpenSSHSession(client)
            finally:
                socket.close()
        finally:
            socket.close()
