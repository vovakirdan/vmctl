"""Durable atomic file replacement and a bounded host-wide lock."""

import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from vmctl.errors import VmctlError


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # Windows does not support opening directories for POSIX fsync.
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(
    path: Path, data: bytes, *, mode: int = 0o600, validate: Callable[[Path], None] | None = None
) -> None:
    if path.is_symlink():
        raise VmctlError(f"Refusing to replace symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "wb") as output:
            if os.name != "nt":
                os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if validate is not None:
            validate(temporary)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_remove(path: Path) -> None:
    if path.is_symlink():
        raise VmctlError(f"Refusing to remove symlink: {path}")
    path.unlink(missing_ok=True)
    fsync_directory(path.parent)


def atomic_create(path: Path, data: bytes, *, mode: int = 0o644) -> bool:
    """Publish a complete file only if the destination is absent, even under a race."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "wb") as output:
            if os.name != "nt":
                os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        fsync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def host_lock(path: Path, timeout: int) -> Iterator[None]:
    if sys.platform != "linux":
        raise VmctlError("Direct Proxmox execution requires Linux")
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise VmctlError("Another vmctl operation holds the host lock") from exc
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)
