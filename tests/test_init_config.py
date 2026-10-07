"""Configuration installation works without Proxmox and preserves administrator edits."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from rich.ansi import AnsiDecoder
from typer.testing import CliRunner

from vmctl.bootstrap.models import load_modules
from vmctl.bootstrap.resolver import validate_definitions
from vmctl.bootstrap.system import validate_system_features
from vmctl.cli import app
from vmctl.config import load_config
from vmctl.errors import VmctlError
from vmctl.services.init_config import initialize_config
from vmctl.utils.atomic import atomic_create


def test_init_installs_complete_configuration(tmp_path: Path) -> None:
    root = tmp_path / "config"
    result = initialize_config(root)
    config = load_config(root)
    modules = load_modules(config)
    validate_definitions(modules, config.profiles)
    features = validate_system_features(config)
    assert len(config.templates) == 5
    assert len(config.presets) == 4
    assert "system" not in modules and "rust" in modules
    assert set(features) == {"qemu-agent", "desktop-rdp"}
    assert (root / "bootstrap/scripts/rust-install.sh").is_file()
    assert result.created and not result.kept
    assert all(path.stat().st_mode & 0o777 == 0o644 for path in result.created)
    assert not list(root.rglob(".*"))


def test_init_preserves_edits_and_restores_only_missing_files(tmp_path: Path) -> None:
    root = tmp_path / "config"
    first = initialize_config(root)
    preset = root / "presets.toml"
    customized = preset.read_text().replace("cpu = 2", "cpu = 3")
    preset.write_text(customized)
    missing = root / "templates.toml"
    missing.unlink()
    extra = root / "local-notes.txt"
    extra.write_text("Keep this file")
    result = initialize_config(root)
    assert result.created == (missing,)
    assert len(result.kept) == len(first.created) - 1
    assert preset.read_text() == customized
    assert extra.read_text() == "Keep this file"
    assert load_config(root).presets["small"].cpu == 3


def test_simultaneous_initialization_never_overwrites(tmp_path: Path) -> None:
    root = tmp_path / "config"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(initialize_config, [root, root]))
    created = [path for result in results for path in result.created]
    assert len(created) == len(set(created))
    assert len(created) == len([path for path in root.rglob("*") if path.is_file()])
    load_config(root)


def test_atomic_create_keeps_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "file"
    assert atomic_create(path, b"first")
    assert not atomic_create(path, b"second")
    assert path.read_bytes() == b"first"
    assert sorted(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("relative", ["", "bootstrap", "bootstrap/modules", "templates.toml"])
def test_init_rejects_symlinks_before_writing(tmp_path: Path, relative: str) -> None:
    root = tmp_path / "config"
    target = tmp_path / "target"
    target.mkdir()
    symlink = root / relative if relative else root
    symlink.parent.mkdir(parents=True, exist_ok=True)
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(VmctlError, match="not a regular"):
        initialize_config(root)
    assert not list(target.iterdir())
    assert not (root / "config.toml").exists()


def test_cli_init_works_before_config_exists(tmp_path: Path) -> None:
    root = tmp_path / "config"
    runner = CliRunner()
    result = runner.invoke(app, ["--local", "--config-dir", str(root), "config", "init"])
    assert result.exit_code == 0, result.output
    assert "Configuration initialized" in result.output
    result = runner.invoke(app, ["--local", "--config-dir", str(root), "presets"])
    assert result.exit_code == 0, result.output
    assert "small" in result.output and "heavy" in result.output
    result = runner.invoke(app, ["--local", "--config-dir", str(root), "config", "init"])
    assert result.exit_code == 0, result.output
    output = "\n".join(line.plain for line in AnsiDecoder().decode(result.output))
    assert "Created 0 files" in " ".join(output.split())


def test_missing_config_error_explains_initialization(tmp_path: Path) -> None:
    root = tmp_path / "config"
    result = CliRunner().invoke(app, ["--local", "--config-dir", str(root), "presets"])
    assert result.exit_code == 1
    assert "config init" in " ".join(result.output.split())
    assert not root.exists()
