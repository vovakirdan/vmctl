"""Editable server catalogs remain read-only and honor OS compatibility."""

from conftest import FakeRunner

from vmctl.config import Configuration
from vmctl.services.catalog import build_catalog


def test_catalog_is_read_only_and_separates_categories(
    config: Configuration, runner: FakeRunner
) -> None:
    catalog = build_catalog(config)
    assert {item.name for item in catalog.system_features} == {"qemu-agent", "desktop-rdp"}
    assert "qemu-agent" not in {item.name for item in catalog.modules}
    desktop = next(item for item in catalog.system_features if item.name == "desktop-rdp")
    assert desktop.templates == ("ubuntu-desktop",)
    node = next(item for item in catalog.modules if item.name == "node")
    assert "alpine" not in node.templates
    assert str(catalog.pool_start) == "10.210.0.100" and not runner.calls
    assert next(item for item in catalog.profiles if item.name == "surge-dev").templates


def test_catalog_reads_new_user_module(config: Configuration) -> None:
    path = config.path(config.host.bootstrap.modules_dir) / "custom.toml"
    path.write_text(
        'name = "custom"\ndescription = "User module"\ndependencies = ["base"]\n[implementations.debian]\npackages = ["jq"]\n'
    )
    catalog = build_catalog(config)
    custom = next(item for item in catalog.modules if item.name == "custom")
    assert custom.description == "User module"
    assert "ubuntu-server" in custom.templates and "alpine" not in custom.templates
