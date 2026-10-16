"""Editable server catalogs remain read-only and honor OS compatibility."""

from pathlib import Path

from conftest import FakeRunner

from vmctl.config import Configuration
from vmctl.services.catalog import build_catalog


def test_catalog_is_read_only_and_separates_categories(
    config: Configuration, runner: FakeRunner
) -> None:
    catalog = build_catalog(config)
    assert {item.name for item in catalog.system_features} == {
        "qemu-agent",
        "desktop-rdp",
        "fileshare",
        "agentmemory",
    }
    assert "qemu-agent" not in {item.name for item in catalog.modules}
    desktop = next(item for item in catalog.system_features if item.name == "desktop-rdp")
    assert desktop.templates == ("ubuntu-desktop",)
    node = next(item for item in catalog.modules if item.name == "node")
    assert "alpine" not in node.templates
    assert str(catalog.pool_start) == "10.210.0.100" and not runner.calls
    assert next(item for item in catalog.profiles if item.name == "surge-dev").templates


def test_fileshare_requires_worker_configuration_and_is_never_an_automatic_default(
    config: Configuration,
) -> None:
    catalog = build_catalog(config)
    fileshare = next(item for item in catalog.system_features if item.name == "fileshare")
    assert fileshare.templates == ()
    assert fileshare.unavailable_reason and "Configure fileshare" in fileshare.unavailable_reason
    assert all("fileshare" not in names for names in catalog.system_defaults.values())
    configured = config.model_copy(
        update={
            "host": config.host.model_copy(
                update={
                    "fileshare": {
                        "endpoint": "https://10.210.0.110:8443",
                        "sftp_host": "10.210.0.110",
                        "firewall_vmid": 110,
                        "host_public_key": Path(config.host.ssh_key).read_text().strip(),
                        "ca_file": "/no-read-needed/cert.pem",
                        "api_key_file": "/no-read-needed/secret",
                    }
                }
            )
        }
    )
    configured_catalog = build_catalog(configured)
    configured_fileshare = next(
        item for item in configured_catalog.system_features if item.name == "fileshare"
    )
    assert "debian" in configured_fileshare.templates
    assert configured_fileshare.unavailable_reason is None
    assert all("fileshare" not in names for names in configured_catalog.system_defaults.values())


def test_catalog_reads_new_user_module(config: Configuration) -> None:
    path = config.path(config.host.bootstrap.modules_dir) / "custom.toml"
    path.write_text(
        'name = "custom"\ndescription = "User module"\ndependencies = ["base"]\n[implementations.debian]\npackages = ["jq"]\n'
    )
    catalog = build_catalog(config)
    custom = next(item for item in catalog.modules if item.name == "custom")
    assert custom.description == "User module"
    assert custom.dependencies == ("base",)
    assert "ubuntu-server" in custom.templates and "alpine" not in custom.templates


def test_catalog_supplies_editable_defaults_and_dependencies(config: Configuration) -> None:
    catalog = build_catalog(config)
    assert catalog.system_defaults["ubuntu-desktop"] == ("qemu-agent", "desktop-rdp")
    assert catalog.system_defaults["ubuntu-server"] == ("qemu-agent",)
    assert next(f for f in catalog.system_features if f.name == "desktop-rdp").dependencies == (
        "qemu-agent",
    )
    assert next(p for p in catalog.profiles if p.name == "surge-dev").members == tuple(
        config.profiles["surge-dev"]
    )
    config.templates["ubuntu-server"] = config.templates["ubuntu-server"].model_copy(
        update={"system_features": []}
    )
    assert build_catalog(config).system_defaults["ubuntu-server"] == ()


def test_agentmemory_requires_configuration_and_stays_opt_in(config: Configuration) -> None:
    catalog = build_catalog(config)
    feature = next(item for item in catalog.system_features if item.name == "agentmemory")
    assert feature.templates == ()
    assert feature.unavailable_reason and "Configure agentmemory" in feature.unavailable_reason
    assert all("agentmemory" not in names for names in catalog.system_defaults.values())
    assert "agentmemory" not in {item.name for item in catalog.modules}
    configured = config.model_copy(
        update={
            "host": config.host.model_copy(
                update={
                    "agentmemory": {
                        "endpoint": "https://10.210.0.110",
                        "ca_file": "/not-read-by-catalog/ca.crt",
                        "management_host": "10.210.0.110",
                        "management_key_file": "/not-read-by-catalog/key",
                        "management_known_hosts_file": "/not-read-by-catalog/known_hosts",
                        "firewall_vmid": 110,
                    }
                }
            )
        }
    )
    feature = next(
        item for item in build_catalog(configured).system_features if item.name == "agentmemory"
    )
    assert "debian" in feature.templates and "rocky" not in feature.templates
    assert feature.unavailable_reason is None
