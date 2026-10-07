"""Editable Go/Python development definitions; no guest installer is executed."""

import shlex
from typing import Any

import pytest
import yaml

from vmctl.bootstrap.models import load_modules
from vmctl.bootstrap.renderer import CloudInitRenderer
from vmctl.bootstrap.resolver import resolve_modules
from vmctl.bootstrap.system import load_system_features, resolve_system_features
from vmctl.config import Configuration
from vmctl.errors import VmctlError


def rendered(config: Configuration, template: str, names: list[str]) -> dict[str, Any]:
    modules = resolve_modules(names, load_modules(config), config.profiles)
    data: dict[str, Any] = yaml.safe_load(
        CloudInitRenderer(config).render(
            config.template(template), "dev-test", "ssh-ed25519 key", modules, {}
        )
    )
    return data


def installed_packages(data: dict[str, Any]) -> list[str]:
    script = next(
        file["content"] for file in data["write_files"] if file["path"].endswith("/run.sh")
    )
    packages: list[str] = []
    for line in script.splitlines():
        words = shlex.split(line)
        if len(words) > 1 and words[:2] in (
            ["apt-get", "install"],
            ["dnf", "install"],
            ["apk", "add"],
        ):
            packages.extend(word for word in words[2:] if not word.startswith("-"))
    return packages


@pytest.mark.parametrize(
    ("template", "package"),
    [
        ("ubuntu-server", "golang-go"),
        ("rocky", "golang"),
        ("alpine", "go"),
    ],
)
def test_go_uses_distribution_packages(config: Configuration, template: str, package: str) -> None:
    data = rendered(config, template, ["go"])
    packages = installed_packages(data)
    assert package in packages
    assert not (set(packages) & {"golang-go", "golang", "go"}) - {package}
    assert load_modules(config)["go"].default_version == "system"
    assert [
        module.name for module in resolve_modules(["go"], load_modules(config), config.profiles)
    ] == ["base", "go"]
    assert all("uv-install" not in file["content"] for file in data["write_files"])


@pytest.mark.parametrize(
    ("template", "pip_package", "venv_package"),
    [
        ("ubuntu-server", "python3-pip", "python3-venv"),
        ("rocky", "python3-pip", None),
        ("alpine", "py3-pip", None),
    ],
)
def test_python_provides_pip_venv_and_uv(
    config: Configuration, template: str, pip_package: str, venv_package: str | None
) -> None:
    data = rendered(config, template, ["python"])
    packages = installed_packages(data)
    assert {"python3", pip_package, "ca-certificates"}.issubset(packages)
    if venv_package:
        assert venv_package in packages
    else:
        assert "python3-venv" not in packages
    run = next(file["content"] for file in data["write_files"] if file["path"].endswith("/run.sh"))
    assert "python3 -m pip --version" in run
    installer = next(
        file for file in data["write_files"] if file["path"].endswith("development-python-0.sh")
    )
    assert installer["owner"] == "root:root"
    assert installer["permissions"] == "0700"
    assert "https://astral.sh/uv/install.sh" in installer["content"]
    assert 'UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1' in installer["content"]
    assert 'su -l -s /bin/sh "$username"' in installer["content"]
    assert "--break-system-packages" not in run + installer["content"]
    assert "pip install" not in run + installer["content"]
    assert load_modules(config)["python"].default_version == "system"


@pytest.mark.parametrize("template", ["ubuntu-server", "rocky", "alpine"])
def test_uv_targets_configured_cloud_user(config: Configuration, template: str) -> None:
    configured = config.model_copy(
        update={"host": config.host.model_copy(update={"username": "builder"})}
    )
    data = rendered(configured, template, ["python"])
    assert data["users"][0]["name"] == "builder"
    assert data["users"][0]["homedir"] == "/home/builder"
    run = next(file["content"] for file in data["write_files"] if file["path"].endswith("/run.sh"))
    assert "sh /var/lib/vmctl/bootstrap/development-python-0.sh builder" in run
    installer = next(
        file["content"]
        for file in data["write_files"]
        if file["path"].endswith("development-python-0.sh")
    )
    assert "/home/vmadmin" not in installer and "/root/.local/bin" not in installer
    assert 'home_directory=$(getent passwd "$username"' in installer
    profile = next(file for file in data["write_files"] if "vmctl-python.sh.vmctl" in file["path"])
    assert profile["owner"] == "root:root"
    assert profile["permissions"] == "0644"
    assert profile["defer"]
    assert '"$(id -un)" = "builder"' in profile["content"]
    assert 'case ":$PATH:"' in profile["content"]
    assert 'export PATH="$HOME/.local/bin:$PATH"' in profile["content"]
    assert (
        "mv -fT -- /etc/profile.d/.vmctl-python.sh.vmctl-development-python-0 /etc/profile.d/vmctl-python.sh"
        in run
    )
    assert ".profile" not in installer


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("surge-dev", ["base", "rust", "node", "go", "python", "llvm", "cmake"]),
        ("full-dev", ["base", "rust", "node", "go", "python", "docker", "llvm", "cmake"]),
    ],
)
def test_profiles_are_real_compositions_with_shared_dependency_dedup(
    config: Configuration, profile: str, expected: list[str]
) -> None:
    resolved = resolve_modules([profile, "go", "python"], load_modules(config), config.profiles)
    assert [module.name for module in resolved] == expected
    data = rendered(config, "ubuntu-server", [profile, "go", "python"])
    packages = installed_packages(data)
    assert packages.count("build-essential") == 1
    assert packages.count("golang-go") == 1
    assert packages.count("python3-pip") == 1
    assert (
        sum(file["path"].endswith("development-python-0.sh") for file in data["write_files"]) == 1
    )


@pytest.mark.parametrize("template", ["ubuntu-server", "rocky", "alpine"])
def test_clean_server_does_not_receive_go_python_or_uv(
    config: Configuration, template: str
) -> None:
    selected = config.template(template)
    text = CloudInitRenderer(config).render(
        selected,
        "compatibility-test",
        "ssh-ed25519 key",
        (),
        {},
        system_features=resolve_system_features(selected, load_system_features(config)),
    )
    data = yaml.safe_load(text)
    packages = installed_packages(data)
    assert "qemu-guest-agent" in packages
    assert not {
        "golang-go",
        "golang",
        "go",
        "python3",
        "python3-pip",
        "python3-venv",
        "py3-pip",
    } & set(packages)
    assert "astral.sh" not in text
    assert "development-python" not in text
    assert "development-go" not in text


@pytest.mark.parametrize(("module", "version"), [("go", "1.24.5"), ("python", "3.12")])
def test_distribution_module_version_override_is_rejected(
    config: Configuration, module: str, version: str
) -> None:
    selected = resolve_modules([module], load_modules(config), config.profiles)
    with pytest.raises(VmctlError, match="[Vv]ersion|override"):
        CloudInitRenderer(config).render(
            config.template("ubuntu-server"), "test", "ssh-ed25519 key", selected, {module: version}
        )
