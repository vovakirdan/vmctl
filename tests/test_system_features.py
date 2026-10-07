"""System capabilities stay independent of opt-in development and terminal input."""

import logging
from dataclasses import replace

import pytest
import yaml
from conftest import FakeRunner
from passlib.hash import sha512_crypt
from pydantic import SecretStr
from typer.testing import CliRunner

from vmctl import cli
from vmctl.bootstrap.models import load_modules
from vmctl.bootstrap.renderer import CloudInitRenderer
from vmctl.bootstrap.system import (
    load_system_features,
    resolve_system_features,
    select_bootstrap,
    validate_system_features,
)
from vmctl.config import Configuration
from vmctl.errors import CreationError, VmctlError
from vmctl.models import CreateRequest
from vmctl.network.allocator import AddressAllocator
from vmctl.network.dnsmasq import DnsmasqReservations
from vmctl.proxmox.client import ProxmoxClient
from vmctl.services.create_vm import CreateVMService
from vmctl.services.queries import info_vm
from vmctl.utils.passwords import hash_desktop_password


@pytest.fixture
def creator(config: Configuration, runner: FakeRunner) -> CreateVMService:
    return CreateVMService(
        config,
        ProxmoxClient(config, runner),
        DnsmasqReservations(config, runner),
        AddressAllocator(config, runner),
        CloudInitRenderer(config),
    )


@pytest.fixture
def desktop_hash() -> SecretStr:
    return hash_desktop_password("test-only-desktop-password")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("ubuntu-desktop", ("qemu-agent", "desktop-rdp")),
        ("ubuntu-server", ("qemu-agent",)),
    ],
)
def test_capability_defaults(config: Configuration, name: str, expected: tuple[str, ...]) -> None:
    template = config.template(name).model_copy(update={"system_features": None})
    selected = resolve_system_features(template, load_system_features(config))
    assert tuple(f.name for f in selected) == expected


def test_feature_disable_and_dependency_deduplication(config: Configuration) -> None:
    features = load_system_features(config)
    template = config.template("ubuntu-desktop")
    selected = resolve_system_features(template, features, ["desktop-rdp", "qemu-agent"])
    assert tuple(f.name for f in selected) == ("qemu-agent", "desktop-rdp")
    assert tuple(
        f.name for f in resolve_system_features(template, features, disabled=["desktop-rdp"])
    ) == ("qemu-agent",)
    with pytest.raises(VmctlError, match="required by a dependency"):
        resolve_system_features(template, features, disabled=["qemu-agent"])
    with pytest.raises(VmctlError, match="both requested and disabled"):
        resolve_system_features(template, features, ["desktop-rdp"], ["desktop-rdp"])
    with pytest.raises(VmctlError, match="Unknown disabled system feature"):
        resolve_system_features(template, features, disabled=["missing"])


def test_explicit_template_override_and_independent_development(config: Configuration) -> None:
    template = config.template("ubuntu-desktop").model_copy(update={"system_features": []})
    assert resolve_system_features(template, load_system_features(config)) == ()
    selected = select_bootstrap(
        config, CreateRequest("work", "ubuntu-desktop", "normal", modules=("rust", "node"))
    )
    assert tuple(f.name for f in selected.system_features) == ("qemu-agent", "desktop-rdp")
    assert tuple(m.name for m in selected.modules) == ("base", "rust", "node")
    assert not select_bootstrap(config, CreateRequest("server", "ubuntu-server", "small")).modules
    assert not {"system", "qemu-agent", "desktop-rdp"} & load_modules(config).keys()
    with pytest.raises(VmctlError, match="Unknown bootstrap"):
        select_bootstrap(
            config, CreateRequest("work", "ubuntu-desktop", "normal", modules=("desktop-rdp",))
        )


def test_system_cycles_and_cross_category_dependencies(config: Configuration) -> None:
    features = load_system_features(config)
    features["qemu-agent"] = features["qemu-agent"].model_copy(
        update={"dependencies": ["desktop-rdp"]}
    )
    with pytest.raises(VmctlError, match="System feature dependency cycle"):
        resolve_system_features(config.template("ubuntu-desktop"), features)
    features["qemu-agent"] = features["qemu-agent"].model_copy(update={"dependencies": ["base"]})
    with pytest.raises(VmctlError, match="Unknown system feature"):
        resolve_system_features(config.template("ubuntu-server"), features)


def test_editable_feature_without_python_changes(config: Configuration) -> None:
    directory = config.path(config.host.bootstrap.system_features_dir)
    (directory / "timesync.toml").write_text("""
name = "timesync"
supported_families = ["debian"]
dependencies = ["qemu-agent"]
[implementations.debian]
packages = ["chrony"]
""")
    validate_system_features(config)
    selected = select_bootstrap(
        config, CreateRequest("server", "ubuntu-server", "small", system_features=("timesync",))
    )
    assert tuple(f.name for f in selected.system_features) == ("qemu-agent", "timesync")
    assert not selected.modules


@pytest.mark.parametrize("template", ["rocky", "alpine"])
def test_unsupported_desktop_os_fails_before_clone(
    config: Configuration, runner: FakeRunner, template: str
) -> None:
    desktop = config.template(template).model_copy(
        update={"desktop": True, "system_features": ["desktop-rdp"]}
    )
    config = config.model_copy(update={"templates": {**config.templates, template: desktop}})
    service = CreateVMService(
        config,
        ProxmoxClient(config, runner),
        DnsmasqReservations(config, runner),
        AddressAllocator(config, runner),
        CloudInitRenderer(config),
    )
    with pytest.raises(VmctlError, match="desktop-rdp.*does not support family"):
        service.plan(CreateRequest("work", template, "normal", no_desktop_password=True))
    assert not runner.calls


def test_desktop_rdp_rejects_server_template(config: Configuration) -> None:
    with pytest.raises(VmctlError, match="requires a desktop template"):
        select_bootstrap(
            config,
            CreateRequest("server", "ubuntu-server", "small", system_features=("desktop-rdp",)),
        )


@pytest.mark.parametrize("username", ["vmadmin", "alice"])
def test_flashback_session_and_static_agent_start(config: Configuration, username: str) -> None:
    config = config.model_copy(
        update={"host": config.host.model_copy(update={"username": username})}
    )
    selection = select_bootstrap(config, CreateRequest("work", "ubuntu-desktop", "normal"))
    data = yaml.safe_load(
        CloudInitRenderer(config).render(
            config.template("ubuntu-desktop"),
            "work",
            "public key",
            (),
            {},
            system_features=selection.system_features,
        )
    )
    session = next(f for f in data["write_files"] if "gnome-flashback-metacity" in f["content"])
    assert session["path"].startswith(f"/home/{username}/")
    assert session["owner"] == f"{username}:{username}"
    assert session["permissions"] == "0600" and session["defer"] is True
    assert session["content"] == "exec gnome-session --session=gnome-flashback-metacity\n"
    operations = data["write_files"][-1]["content"]
    assert f"mv -fT -- {session['path']} /home/{username}/.xsession" in operations
    assert operations.count("apt-get install -y --no-install-recommends qemu-guest-agent") == 1
    assert "systemctl start qemu-guest-agent" in operations
    assert "systemctl enable xrdp" in operations
    assert "systemctl restart xrdp-sesman xrdp" in operations
    assert "usermod -a -G ssl-cert xrdp" in operations
    assert "systemctl enable qemu-guest-agent" not in operations
    assert "gnome-session-x11@" not in operations and "WaylandEnable" not in operations
    assert data["users"][0]["homedir"] == f"/home/{username}"


def test_service_requires_password_and_hash_is_applied_to_existing_user(
    creator: CreateVMService, runner: FakeRunner, desktop_hash: SecretStr
) -> None:
    request = CreateRequest("work", "ubuntu-desktop", "normal")
    with pytest.raises(VmctlError, match="Desktop password is required"):
        creator.create(request)
    assert not any(call[:2] == ("qm", "clone") for call in runner.calls)
    result = creator.create(replace(request, desktop_password_hash=desktop_hash, start=False))
    snippet = next((runner.root / "snippets").glob("*.yaml"))
    assert snippet.stat().st_mode & 0o777 == 0o600
    data = yaml.safe_load(snippet.read_text())
    assert data["users"][0]["hashed_passwd"] == desktop_hash.get_secret_value()
    assert data["users"][0]["lock_passwd"] is False
    assert data["ssh_pwauth"] is False and data["chpasswd"]["expire"] is False
    assert result.rdp_enabled and not result.started
    details = info_vm(creator.client, creator.reservations, "work")
    assert details.desktop and details.rdp_enabled
    assert details.system_features == ("qemu-agent", "desktop-rdp")
    assert details.rdp_address == "10.210.0.100:3389"
    assert details.metadata["username"] == "vmadmin"


def test_no_password_and_rdp_disable_preserve_ssh(
    creator: CreateVMService, runner: FakeRunner
) -> None:
    result = creator.create(
        CreateRequest(
            "work",
            "ubuntu-desktop",
            "normal",
            without_system=("desktop-rdp",),
            no_desktop_password=True,
        )
    )
    assert result.desktop and not result.rdp_enabled
    assert result.system_features == ("qemu-agent",)
    data = yaml.safe_load(next((runner.root / "snippets").glob("*.yaml")).read_text())
    assert data["users"][0]["lock_passwd"] and "hashed_passwd" not in data["users"][0]
    assert "xrdp" not in str(data) and data["users"][0]["ssh_authorized_keys"]


def test_password_hash_is_salted_and_redacted(desktop_hash: SecretStr) -> None:
    second = hash_desktop_password("test-only-desktop-password")
    assert second.get_secret_value() != desktop_hash.get_secret_value()
    assert desktop_hash.get_secret_value().startswith("$6$rounds=500000$")
    assert sha512_crypt.verify("test-only-desktop-password", desktop_hash.get_secret_value())
    request = CreateRequest("work", "ubuntu-desktop", "normal", desktop_password_hash=desktop_hash)
    assert desktop_hash.get_secret_value() not in repr(request)
    assert desktop_hash.get_secret_value() not in str(desktop_hash)


@pytest.mark.parametrize("password", ["", "\0bad", "a" * 4097])
def test_invalid_password_inputs(password: str) -> None:
    with pytest.raises(VmctlError):
        hash_desktop_password(password)


def test_plaintext_hash_field_and_invalid_combinations_are_rejected(
    creator: CreateVMService, desktop_hash: SecretStr
) -> None:
    with pytest.raises(VmctlError, match="SHA-512 crypt hash"):
        creator.plan(
            CreateRequest(
                "work", "ubuntu-desktop", "normal", desktop_password_hash=SecretStr("not-a-hash")
            )
        )
    for template, disabled in [("ubuntu-server", False), ("ubuntu-desktop", True)]:
        with pytest.raises(VmctlError, match="desktop password requires"):
            creator.plan(
                CreateRequest(
                    "work",
                    template,
                    "normal",
                    desktop_password_hash=desktop_hash,
                    no_desktop_password=disabled,
                )
            )


@pytest.mark.parametrize(
    "template,options,prompt,rdp",
    [
        ("ubuntu-desktop", [], True, True),
        ("ubuntu-desktop", ["--no-start"], True, True),
        ("ubuntu-desktop", ["--no-desktop-password"], False, True),
        ("ubuntu-desktop", ["--without-system", "desktop-rdp"], True, False),
        ("ubuntu-desktop", ["--dry-run"], False, False),
        ("ubuntu-server", [], False, False),
    ],
)
def test_cli_password_prompt_and_rdp_output(
    config: Configuration,
    runner: FakeRunner,
    creator: CreateVMService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    template: str,
    options: list[str],
    prompt: bool,
    rdp: bool,
) -> None:
    monkeypatch.setattr(
        cli,
        "dependencies",
        lambda config: (creator.client, creator.reservations, creator.allocator),
    )
    password = "interactive-test-only-secret"
    with caplog.at_level(logging.DEBUG):
        result = CliRunner().invoke(
            cli.app,
            [
                "--local",
                "--config-dir",
                str(config.root),
                "create",
                "work",
                template,
                "normal",
                *options,
            ],
            input=f"{password}\n{password}\n",
        )
    assert result.exit_code == 0, result.output
    assert ("Desktop password for vmadmin:" in result.output) is prompt
    assert ("Confirm password:" in result.output) is prompt
    assert ("\nRDP:" in result.output) is rdp
    assert password not in result.output + caplog.text
    commands = str(runner.calls)
    assert password not in commands and "$6$" not in commands
    if "--no-desktop-password" in options:
        assert "GUI/RDP login may not be usable" in result.output
    if "--dry-run" in options:
        assert "desktop-rdp" in result.output
        assert not any(call[:2] == ("qm", "clone") for call in runner.calls)
        assert not config.path(config.host.lock_file).exists()
    else:
        info = CliRunner().invoke(
            cli.app, ["--local", "--config-dir", str(config.root), "info", "work"]
        )
        assert info.exit_code == 0, info.output
        assert ("RDP address" in info.output) is rdp
        assert ("RDP" in info.output) is (template == "ubuntu-desktop")
        assert "$6$" not in info.output and password not in info.output


def test_password_prompt_mismatch_and_abort_do_not_clone(
    config: Configuration,
    runner: FakeRunner,
    creator: CreateVMService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "dependencies",
        lambda config: (creator.client, creator.reservations, creator.allocator),
    )
    result = CliRunner().invoke(
        cli.app,
        ["--local", "--config-dir", str(config.root), "create", "work", "ubuntu-desktop", "normal"],
        input="first-secret\nother-secret\n",
    )
    assert result.exit_code != 0
    assert "first-secret" not in result.output and "other-secret" not in result.output
    assert not any(call[:2] == ("qm", "clone") for call in runner.calls)


def test_desktop_rollback_removes_secret_snippet(
    creator: CreateVMService, runner: FakeRunner, desktop_hash: SecretStr
) -> None:
    runner.failure = lambda args: args[:2] == ["qm", "start"]
    with pytest.raises(CreationError) as error:
        creator.create(
            CreateRequest("work", "ubuntu-desktop", "normal", desktop_password_hash=desktop_hash)
        )
    assert desktop_hash.get_secret_value() not in str(error.value)
    assert not list((runner.root / "snippets").glob("*.yaml"))
    assert 104 not in runner.vms and not creator.reservations.read()


def test_explicit_no_system_features_disables_proxmox_agent(
    config: Configuration, runner: FakeRunner
) -> None:
    template = config.template("ubuntu-server").model_copy(update={"system_features": []})
    config = config.model_copy(
        update={"templates": {**config.templates, "ubuntu-server": template}}
    )
    creator = CreateVMService(
        config,
        ProxmoxClient(config, runner),
        DnsmasqReservations(config, runner),
        AddressAllocator(config, runner),
        CloudInitRenderer(config),
    )
    result = creator.create(CreateRequest("server", "ubuntu-server", "small"))
    assert not result.system_features and runner.vms[104]["agent"] == "enabled=0"


def test_legacy_system_module_is_excluded_from_development(config: Configuration) -> None:
    config = config.model_copy(
        update={
            "host": config.host.model_copy(
                update={
                    "bootstrap": config.host.bootstrap.model_copy(
                        update={"system_module": "integration"}
                    )
                }
            )
        }
    )
    (config.root / "bootstrap/modules/integration.toml").write_text(
        'name = "integration"\n[implementations.debian]\npackages = ["qemu-guest-agent"]\n'
    )
    assert "integration" not in load_modules(config)
    selection = select_bootstrap(config, CreateRequest("server", "ubuntu-server", "small"))
    assert tuple(f.name for f in selection.system_features) == ("qemu-agent",)
    assert not selection.modules


def test_category_name_collision_rejected(config: Configuration) -> None:
    (config.root / "bootstrap/modules/qemu-agent.toml").write_text(
        'name = "qemu-agent"\n[implementations.debian]\npackages = []\n'
    )
    with pytest.raises(VmctlError, match="distinct names"):
        select_bootstrap(config, CreateRequest("server", "ubuntu-server", "small"))


@pytest.mark.parametrize("path", ["relative/.xsession", "/home/vmadmin/../.xsession", "/"])
def test_invalid_guest_file_fails_configuration_validation(
    config: Configuration, path: str
) -> None:
    definition = config.path(config.host.bootstrap.system_features_dir) / "invalid.toml"
    definition.write_text(f'''name = "invalid"
supported_families = ["debian"]
[implementations.debian]
files = [{{path = "{path}", content = "test"}}]
''')
    with pytest.raises(VmctlError, match="Guest file path"):
        validate_system_features(config)


def test_duplicate_guest_target_is_rejected(config: Configuration) -> None:
    features = load_system_features(config)
    duplicate = features["desktop-rdp"].model_copy(update={"name": "duplicate"})
    with pytest.raises(VmctlError, match="Duplicate guest file target"):
        CloudInitRenderer(config).render(
            config.template("ubuntu-desktop"),
            "work",
            "key",
            (),
            {},
            system_features=[features["desktop-rdp"], duplicate],
        )


def test_info_of_preexisting_vm_keeps_features_unknown(
    creator: CreateVMService, runner: FakeRunner
) -> None:
    runner.vms[200] = {"name": "manual"}
    details = info_vm(creator.client, creator.reservations, "manual")
    assert details.desktop is None and not details.system_features and not details.modules
    assert not details.rdp_enabled and details.rdp_address is None
