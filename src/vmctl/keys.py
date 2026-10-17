"""Editable named public keys for guest access; private keys are never stored."""

import base64
import hashlib
import json
import os
import re
import time
import tomllib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from pydantic import Field, ValidationError, field_validator, model_validator

from vmctl.config import StrictModel
from vmctl.errors import VmctlError
from vmctl.utils.atomic import atomic_write
from vmctl.utils.ssh_keys import validate_public_key

_MAX_BYTES = 262144
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")


def _validate_name(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise VmctlError("SSH key names must contain 1-64 letters, numbers, underscores or hyphens")
    return name


def _single_key(text: str) -> str:
    if len(text.encode("utf-8")) > 16384 or any(
        ord(char) < 32 and char not in "\n\r\t" for char in text
    ):
        raise VmctlError("Invalid SSH public key")
    key = validate_public_key(text)
    if len(key.splitlines()) != 1:
        raise VmctlError("Each named entry must contain exactly one SSH public key")
    return key.strip()


def _identity(text: str) -> tuple[str, bytes]:
    algorithm, encoded, *_ = _single_key(text).split()
    return algorithm, base64.b64decode(encoded)


def key_fingerprint(public_key: str) -> str:
    """Return the standard OpenSSH SHA256 fingerprint without its comment."""
    digest = hashlib.sha256(_identity(public_key)[1]).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def merge_public_keys(*texts: str) -> str:
    """Preserve first-seen comments while deduplicating identities."""
    seen: set[tuple[str, bytes]] = set()
    lines: list[str] = []
    for text in texts:
        for line in validate_public_key(text).splitlines():
            identity = _identity(line)
            if identity not in seen:
                lines.append(line)
                seen.add(identity)
    return "\n".join(lines) + ("\n" if lines else "")


class NamedPublicKey(StrictModel):
    public_key: str
    description: str = Field(default="", max_length=1024)

    @field_validator("public_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        return _single_key(value)


class KeyRegistry(StrictModel):
    default_keys: list[str] = Field(default_factory=list, max_length=128)
    keys: dict[str, NamedPublicKey] = Field(default_factory=dict, max_length=128)

    @model_validator(mode="after")
    def validate_registry(self) -> "KeyRegistry":
        for name in (*self.keys, *self.default_keys):
            _validate_name(name)
        if any(name not in self.keys for name in self.default_keys):
            raise VmctlError("SSH key defaults reference an unknown named key")
        return self


@contextmanager
def _registry_lock(path: Path) -> Iterator[None]:
    """Serialize read/modify/replace on Linux, macOS and Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise VmctlError("SSH key registry lock must not be a symlink")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    locked = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        deadline = time.monotonic() + 10
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except (BlockingIOError, PermissionError) as exc:
                if time.monotonic() >= deadline:
                    raise VmctlError("Another operation holds the SSH key registry lock") from exc
                time.sleep(0.05)
        yield
    finally:
        if locked and os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        os.close(fd)


class PublicKeyRegistryStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()

    def load(self) -> KeyRegistry:
        try:
            if self.path.is_symlink():
                raise VmctlError("SSH key registry must not be a symlink")
            with self.path.open("rb") as source:
                data = source.read(_MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                raise VmctlError("SSH key registry exceeds the 256 KiB limit")
            return KeyRegistry.model_validate(tomllib.loads(data.decode("utf-8")))
        except FileNotFoundError:
            return KeyRegistry()
        except (OSError, ValueError, ValidationError) as exc:
            raise VmctlError(f"Cannot load SSH key registry: {self.path}") from exc

    def _save(self, registry: KeyRegistry) -> None:
        lines = [
            "# vmctl public SSH keys. Never store private keys here.",
            "default_keys = " + json.dumps(registry.default_keys),
        ]
        for name, key in registry.keys.items():
            lines.extend(
                [
                    "",
                    f"[keys.{json.dumps(name)}]",
                    "public_key = " + json.dumps(key.public_key, ensure_ascii=False),
                    "description = " + json.dumps(key.description, ensure_ascii=False),
                ]
            )
        data = ("\n".join(lines) + "\n").encode("utf-8")
        if len(data) > _MAX_BYTES:
            raise VmctlError("SSH key registry exceeds the 256 KiB limit")
        atomic_write(self.path, data, mode=0o600)

    def add(
        self, name: str, public_key: str, *, description: str = "", default: bool = False
    ) -> None:
        _validate_name(name)
        try:
            key = NamedPublicKey(public_key=public_key, description=description)
            with _registry_lock(self.path.with_name(self.path.name + ".lock")):
                registry = self.load()
                existing = registry.keys.get(name)
                if existing is not None and _identity(existing.public_key) != _identity(
                    key.public_key
                ):
                    raise VmctlError("SSH key name already exists with a different public key")
                keys = {**registry.keys, name: key}
                defaults = list(registry.default_keys)
                if default and name not in defaults:
                    defaults.append(name)
                self._save(KeyRegistry(default_keys=defaults, keys=keys))
        except (OSError, ValueError, ValidationError) as exc:
            raise VmctlError(f"Cannot save SSH key registry: {self.path}") from exc

    def remove(self, name: str) -> None:
        _validate_name(name)
        try:
            with _registry_lock(self.path.with_name(self.path.name + ".lock")):
                registry = self.load()
                if name not in registry.keys:
                    raise VmctlError(f"Unknown SSH key: {name}")
                self._save(
                    KeyRegistry(
                        default_keys=[item for item in registry.default_keys if item != name],
                        keys={item: key for item, key in registry.keys.items() if item != name},
                    )
                )
        except OSError as exc:
            raise VmctlError(f"Cannot save SSH key registry: {self.path}") from exc

    def resolve(self, names: Sequence[str] | None = None) -> str:
        registry = self.load()
        selected = registry.default_keys if names is None else names
        if not selected:
            raise VmctlError("No SSH keys selected; add a named key or configure default_keys")
        for name in selected:
            _validate_name(name)
            if name not in registry.keys:
                raise VmctlError(f"Unknown SSH key: {name}")
        return merge_public_keys(*(registry.keys[name].public_key for name in selected))
