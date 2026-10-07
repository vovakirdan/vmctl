"""Portable client checks; this directory is safe on Windows, Linux and macOS."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vmctl.cli import app
from vmctl.client_config import (
    ConnectionConfig,
    default_client_directory,
    initialize_client_config,
    load_client_config,
)
from vmctl.services.init_config import initialize_config


def test_client_init_only_creates_client_file_and_preserves_edits(tmp_path: Path) -> None:
    cli = CliRunner()
    args = ["--config-dir", str(tmp_path), "config", "init"]
    result = cli.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert sorted(path.name for path in tmp_path.iterdir()) == ["client.toml"]
    assert load_client_config(tmp_path).connection.host == "pve"
    path = tmp_path / "client.toml"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    custom = path.read_text().replace('host = "pve"', 'host = "another-host"')
    path.write_text(custom)
    assert cli.invoke(app, args).exit_code == 0
    assert path.read_text() == custom


def test_client_atomic_init_under_race(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(initialize_client_config, [tmp_path, tmp_path]))
    assert sum(len(result.created) for result in results) == 1
    assert sum(len(result.kept) for result in results) == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["client.toml"]


def test_server_init_does_not_install_workstation_settings(tmp_path: Path) -> None:
    initialize_config(tmp_path)
    assert not (tmp_path / "client.toml").exists()
    assert (tmp_path / "templates.toml").exists()


def test_missing_client_config_is_actionable(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["--config-dir", str(tmp_path), "presets"])
    assert result.exit_code == 1
    assert "client.toml" in result.output and "config init" in result.output
    assert "templates.toml" not in result.output


@pytest.mark.parametrize("host", ["-oProxyCommand=id", "pve;id", "pve\nroot", "pve $(id)"])
def test_invalid_connection_host_rejected(host: str) -> None:
    with pytest.raises(ValueError):
        ConnectionConfig(host=host)


@pytest.mark.parametrize("field", ["worker", "config_dir"])
def test_remote_paths_use_posix_rules_on_any_client(field: str) -> None:
    with pytest.raises(ValueError):
        ConnectionConfig.model_validate({"host": "pve", field: "C:\\server\\worker"})


def test_default_path_uses_native_platform_directory() -> None:
    path = default_client_directory()
    assert path.is_absolute()
    assert "vmctl" in path.parts
    assert path != Path("/etc/vmctl")


def test_client_cold_import_without_linux_services() -> None:
    code = """
import importlib.abc
import sys
class DenyHostImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'fcntl':
            raise ModuleNotFoundError('fcntl is unavailable')
        if fullname.startswith(('vmctl.local', 'vmctl.network', 'vmctl.proxmox', 'vmctl.services.create_vm', 'vmctl.services.delete_vm', 'vmctl.services.lifecycle')):
            raise AssertionError('Host import: ' + fullname)
sys.meta_path.insert(0, DenyHostImports())
from vmctl.cli import app
from vmctl.ssh import SSHOperations
from vmctl.tui.app import VmctlApp
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_worker_and_local_mode_reject_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    import vmctl.cli as cli
    import vmctl.worker as worker

    monkeypatch.setattr(sys, "platform", "win32")
    result = CliRunner().invoke(cli.app, ["--local", "presets"])
    assert result.exit_code == 1 and "requires Linux" in result.output
    monkeypatch.setattr(sys, "argv", ["vmctl-worker"])
    with pytest.raises(SystemExit) as failure:
        worker.main()
    assert failure.value.code == 1
