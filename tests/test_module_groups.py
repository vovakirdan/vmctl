"""Editable module groups and desktop eligibility are shared domain rules."""

import pytest
from conftest import FakeRunner
from pydantic import TypeAdapter, ValidationError

from vmctl.bootstrap.models import Module
from vmctl.bootstrap.renderer import CloudInitRenderer
from vmctl.bootstrap.system import select_bootstrap
from vmctl.config import Configuration
from vmctl.errors import VmctlError
from vmctl.models import CreateRequest
from vmctl.network.allocator import AddressAllocator
from vmctl.network.dnsmasq import DnsmasqReservations
from vmctl.operations import CatalogItem
from vmctl.proxmox.client import ProxmoxClient
from vmctl.services.catalog import build_catalog
from vmctl.services.create_vm import CreateVMService


def test_legacy_module_and_wire_catalog_group_defaults(config: Configuration) -> None:
    legacy = Module.model_validate({"name": "custom", "implementations": {"debian": {}}})
    assert legacy.group == "development" and not legacy.desktop_only
    assert legacy.implementation(config.template("ubuntu-server")) is not None
    old = TypeAdapter(CatalogItem).validate_python(
        {"name": "custom", "description": "Legacy item", "templates": ["ubuntu-server"]}
    )
    assert old.group == "development"


def test_invalid_group_fails_configuration_validation() -> None:
    with pytest.raises(ValidationError, match="group"):
        Module.model_validate({"name": "custom", "group": "unrecognized", "implementations": {}})


def test_custom_desktop_module_is_checked_by_catalog_selection_and_creation(
    config: Configuration, runner: FakeRunner
) -> None:
    path = config.path(config.host.bootstrap.modules_dir) / "custom-gui.toml"
    path.write_text(
        'name = "custom-gui"\ngroup = "ai-desktop"\ndesktop_only = true\n'
        'description = "Configured GUI"\n[implementations.debian]\npackages = ["jq"]\n'
    )
    choice = next(item for item in build_catalog(config).modules if item.name == "custom-gui")
    assert choice.group == "ai-desktop" and choice.templates == ("ubuntu-desktop",)
    selected = select_bootstrap(
        config,
        CreateRequest("desktop", "ubuntu-desktop", "small", modules=("custom-gui",)),
    )
    assert selected.modules[-1].name == "custom-gui"
    service = CreateVMService(
        config,
        ProxmoxClient(config, runner),
        DnsmasqReservations(config, runner),
        AddressAllocator(config, runner),
        CloudInitRenderer(config),
    )
    with pytest.raises(VmctlError, match="requires a desktop template"):
        service.plan(CreateRequest("server", "ubuntu-server", "small", modules=("custom-gui",)))
    assert not any(call[:2] == ("qm", "clone") for call in runner.calls)


def test_group_is_metadata_not_an_implicit_selection(config: Configuration) -> None:
    path = config.path(config.host.bootstrap.modules_dir) / "custom-agent.toml"
    path.write_text(
        'name = "custom-agent"\ngroup = "ai-cli"\n[implementations.debian]\npackages = ["jq"]\n'
    )
    catalog = build_catalog(config)
    agent = next(item for item in catalog.modules if item.name == "custom-agent")
    assert agent.group == "ai-cli" and "ubuntu-server" in agent.templates
    assert select_bootstrap(config, CreateRequest("clean", "ubuntu-server", "small")).modules == ()


@pytest.mark.parametrize("version", ["--help", "-s"])
def test_leading_option_versions_are_rejected_before_rendering(
    config: Configuration, version: str
) -> None:
    module = Module.model_validate(
        {
            "name": "custom",
            "default_version": "latest",
            "implementations": {"debian": {"commands": [["printf", "%s", "{version}"]]}},
        }
    )
    with pytest.raises(VmctlError, match="Invalid version"):
        CloudInitRenderer(config).render(
            config.template("ubuntu-server"),
            "test",
            "ssh-ed25519 AAAA mock",
            (module,),
            {"custom": version},
        )
