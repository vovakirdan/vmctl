"""Install bundled examples without replacing any administrator-managed files."""

from collections.abc import Iterator
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

import vmctl
from vmctl.errors import VmctlError
from vmctl.utils.atomic import atomic_create


@dataclass(frozen=True)
class InitConfigResult:
    directory: Path
    created: tuple[Path, ...]
    kept: tuple[Path, ...]


def bundled_defaults() -> Traversable:
    bundled = files("vmctl").joinpath("_defaults")
    if bundled.is_dir():
        return bundled
    # Editable installs use the checkout's single source of example configuration.
    assert vmctl.__file__ is not None
    checkout = Path(vmctl.__file__).resolve().parents[2] / "config"
    if checkout.is_dir() and (checkout.parent / "pyproject.toml").is_file():
        return checkout
    raise VmctlError("Bundled configuration is missing; reinstall vmctl from a complete package")


def default_files(root: Traversable, relative: Path = Path()) -> Iterator[tuple[Path, bytes]]:
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name.startswith(".") or (relative == Path() and entry.name == "client.toml"):
            continue
        destination = relative / entry.name
        if entry.is_dir():
            yield from default_files(entry, destination)
        elif entry.is_file():
            yield destination, entry.read_bytes()


def initialize_config(directory: Path) -> InitConfigResult:
    directory = directory.expanduser().absolute()
    defaults = tuple(default_files(bundled_defaults()))
    if not defaults:
        raise VmctlError("Bundled configuration is empty; reinstall vmctl")
    # Check the whole tree before writing, including existing parent directories.
    for relative, _ in defaults:
        destination = directory / relative
        for parent in (directory, *destination.parents):
            if parent == directory or parent.is_relative_to(directory):
                if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                    raise VmctlError(
                        f"Configuration directory is not a regular directory: {parent}"
                    )
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise VmctlError(f"Configuration destination is not a regular file: {destination}")
    created: list[Path] = []
    kept: list[Path] = []
    for relative, data in defaults:
        path = directory / relative
        if atomic_create(path, data):
            created.append(path)
        else:
            kept.append(path)
    return InitConfigResult(directory, tuple(created), tuple(kept))
