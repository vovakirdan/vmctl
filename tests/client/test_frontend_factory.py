"""Shared frontend configuration stays portable and independent of Typer."""

import base64
import struct
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vmctl import cli
from vmctl.client_config import initialize_client_config
from vmctl.frontend import FrontendSettings, build_operations, load_public_key, target_label
from vmctl.ssh import SSHOperations


def test_adapter_uses_configured_alias_and_completion_timeout(tmp_path: Path) -> None:
    initialize_client_config(tmp_path)
    config = tmp_path / "client.toml"
    config.write_text(config.read_text().replace('host = "pve"', 'host = "pxmx"'))
    settings = FrontendSettings(tmp_path)
    backend = build_operations(settings, completion=True)
    assert isinstance(backend, SSHOperations)
    assert backend.transport.config.host == "pxmx"
    assert backend.transport.config.operation_timeout == 3
    assert target_label(settings) == "pxmx"


def test_key_on_workstation_is_resolved_relative_to_client_directory(tmp_path: Path) -> None:
    initialize_client_config(tmp_path)
    config = tmp_path / "client.toml"
    config.write_text(config.read_text().replace('"~/.ssh/id_ed25519.pub"', '"workstation.pub"'))
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes(range(32))
    key = "ssh-ed25519 " + base64.b64encode(blob).decode() + " workstation"
    (tmp_path / "workstation.pub").write_text(key + "\n")
    assert load_public_key(FrontendSettings(tmp_path)) == key + "\n"
    assert load_public_key(FrontendSettings(tmp_path, local=True)) is None


def test_tui_launcher_reuses_client_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vmctl.tui import app as tui_app

    initialize_client_config(tmp_path)
    config = tmp_path / "client.toml"
    config.write_text(config.read_text().replace('host = "pve"', 'host = "pxmx"'))
    backend = object()
    launches: list[tuple[object, str, bool]] = []

    class FakeApp:
        def __init__(
            self,
            operations: object,
            *,
            target: str,
            public_key: Callable[[Path | None], str | None],
            read_only: bool,
        ) -> None:
            self.arguments = (operations, target, read_only)

        def run(self) -> None:
            launches.append(self.arguments)

    monkeypatch.setattr(cli, "operations", lambda _: backend)
    monkeypatch.setattr(tui_app, "VmctlApp", FakeApp)
    result = CliRunner().invoke(cli.app, ["--config-dir", str(tmp_path), "tui", "--read-only"])
    assert result.exit_code == 0, result.output
    assert launches == [(backend, "pxmx", True)]
