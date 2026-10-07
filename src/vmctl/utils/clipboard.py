"""Bounded workstation clipboard delivery; content is sent only through stdin."""

import os
import platform
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ClipboardCommand:
    arguments: tuple[str, ...]
    encoding: str = "utf-8"


class ClipboardWriter(Protocol):
    def __call__(self, command: ClipboardCommand, text: str) -> None: ...


def clipboard_command(
    *,
    system: str,
    release: str,
    environment: Mapping[str, str],
    find: Callable[[str], str | None],
) -> ClipboardCommand | None:
    """Choose a local clipboard, without assuming a display or WSL interop exists."""
    windows = system == "Windows" or (
        system == "Linux"
        and (
            "microsoft" in release.casefold()
            or bool(environment.get("WSL_DISTRO_NAME") or environment.get("WSL_INTEROP"))
        )
    )
    if windows:
        executable = find("clip.exe") or (find("clip") if system == "Windows" else None)
        return ClipboardCommand((executable,), "utf-16-le") if executable else None
    if system == "Darwin":
        executable = find("pbcopy")
        return ClipboardCommand((executable,)) if executable else None
    if environment.get("WAYLAND_DISPLAY") and (executable := find("wl-copy")):
        return ClipboardCommand((executable,))
    if environment.get("DISPLAY"):
        if executable := find("xclip"):
            return ClipboardCommand((executable, "-selection", "clipboard"))
        if executable := find("xsel"):
            return ClipboardCommand((executable, "--clipboard", "--input"))
    return None


def write_clipboard(command: ClipboardCommand, text: str) -> None:
    """Keep the clipboard-specific process boundary isolated from host execution."""
    with subprocess.Popen(
        command.arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as process:
        try:
            process.communicate(text.encode(command.encoding), timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise OSError("Clipboard command timed out") from None
        if process.returncode:
            raise OSError("Clipboard command failed")


def copy_native(text: str, *, writer: ClipboardWriter = write_clipboard) -> bool:
    command = clipboard_command(
        system=platform.system(),
        release=platform.release(),
        environment=os.environ,
        find=shutil.which,
    )
    if command is None:
        return False
    try:
        writer(command, text)
    except OSError:
        return False
    return True
