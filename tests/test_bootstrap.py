from pathlib import Path

import pytest
import yaml

from vmctl.bootstrap.models import Module, load_modules
from vmctl.bootstrap.renderer import CloudInitRenderer
from vmctl.bootstrap.resolver import resolve_modules, validate_definitions
from vmctl.bootstrap.system import load_system_features, resolve_system_features
from vmctl.config import Configuration
from vmctl.errors import VmctlError


def module(name: str, dependencies: list[str] | None = None) -> Module:
    return Module(name=name, dependencies=dependencies or [], implementations={})


def test_dependency_resolution() -> None:
    modules = {
        "base": module("base"),
        "rust": module("rust", ["base"]),
        "node": module("node", ["base"]),
    }
    profiles = {"dev": ["rust", "node"], "nested": ["dev", "rust"]}
    assert [m.name for m in resolve_modules(["nested", "node"], modules, profiles)] == [
        "base",
        "rust",
        "node",
    ]
    with pytest.raises(VmctlError, match="Unknown"):
        resolve_modules(["missing"], modules, profiles)


def test_cycles() -> None:
    modules = {"a": module("a", ["b"]), "b": module("b", ["a"])}
    with pytest.raises(VmctlError, match="a -> b -> a"):
        resolve_modules(["a"], modules, {})
    with pytest.raises(VmctlError, match="cycle"):
        resolve_modules(["profile"], {}, {"profile": ["other"], "other": ["profile"]})


def test_builtin_definitions_and_no_automatic_toolchains(config: Configuration) -> None:
    modules = load_modules(config)
    validate_definitions(modules, config.profiles)
    template = config.template("debian")
    resolved = resolve_system_features(template, load_system_features(config))
    text = CloudInitRenderer(config).render(
        template, "legacy-test", "ssh-ed25519 key", (), {}, system_features=resolved
    )
    data = yaml.safe_load(text)
    assert not data["package_upgrade"]
    assert data["users"][0]["name"] == "vmadmin"
    assert data["users"][0]["lock_passwd"]
    assert "rustup" not in text and "nodejs" not in text and "build-essential" not in text
    assert "qemu-guest-agent" in text


@pytest.mark.parametrize(
    ("template", "manager"), [("ubuntu-server", "apt-get"), ("rocky", "dnf"), ("alpine", "apk")]
)
def test_os_specific_rendering(config: Configuration, template: str, manager: str) -> None:
    modules = resolve_modules(["rust"], load_modules(config), config.profiles)
    features = resolve_system_features(config.template(template), load_system_features(config))
    text = CloudInitRenderer(config).render(
        config.template(template),
        "test",
        "ssh-ed25519 key",
        modules,
        {"rust": "1.82.0"},
        system_features=features,
    )
    assert manager in text and "1.82.0" in text
    data = yaml.safe_load(text)
    rust_file = next(file for file in data["write_files"] if file["path"].endswith("rust-0.sh"))
    assert "https://sh.rustup.rs" in rust_file["content"]


def test_unsupported_combination(config: Configuration) -> None:
    modules = resolve_modules(["node"], load_modules(config), config.profiles)
    with pytest.raises(VmctlError, match="no implementation"):
        CloudInitRenderer(config).render(config.template("alpine"), "test", "key", modules, {})


def test_new_module_without_python_changes(config: Configuration) -> None:
    (config.root / "bootstrap/modules/custom.toml").write_text("""
name = "custom"
dependencies = ["base"]
default_version = "2.0"
[implementations.debian]
packages = ["jq"]
commands = [["printf", "%s", "{version}"]]
scripts = [{ file = "custom.sh", args = ["{username}", "{version}"] }]
""")
    (config.root / "bootstrap/scripts/custom.sh").write_text('echo "$1 $2"\n')
    modules = load_modules(config)
    resolved = resolve_modules(["custom"], modules, config.profiles)
    text = CloudInitRenderer(config).render(config.template("debian"), "test", "key", resolved, {})
    assert "jq" in text and "2.0" in text and "custom-0.sh" in text
    assert 'echo "$1 $2"' in text


def test_script_path_escape(config: Configuration, tmp_path: Path) -> None:
    (tmp_path / "outside.sh").write_text("echo bad")
    module_path = config.root / "bootstrap/modules/escape.toml"
    module_path.write_text(
        f'name = "escape"\n[implementations.debian]\nscripts = [{{file = "{tmp_path / "outside.sh"}"}}]\n'
    )
    with pytest.raises(VmctlError, match="inside"):
        load_modules(config)


def test_version_injection_rejected(config: Configuration) -> None:
    modules = resolve_modules(["rust"], load_modules(config), {})
    with pytest.raises(VmctlError, match="Invalid version"):
        CloudInitRenderer(config).render(
            config.template("debian"), "test", "key", modules, {"rust": "stable;id"}
        )


def test_command_arguments_are_shell_quoted(config: Configuration) -> None:
    custom = Module.model_validate(
        {
            "name": "custom",
            "implementations": {"debian": {"commands": [["echo", "$(touch /host)"]]}},
        }
    )
    text = CloudInitRenderer(config).render(config.template("debian"), "test", "key", [custom], {})
    content = yaml.safe_load(text)["write_files"][-1]["content"]
    assert "echo '$(touch /host)'" in content
