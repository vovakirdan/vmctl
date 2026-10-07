import pytest

from vmctl.config import Configuration, load_config
from vmctl.errors import VmctlError
from vmctl.models import CreateRequest, validate_name
from vmctl.proxmox.parser import (
    decode_metadata,
    disk_gib_exact,
    encode_metadata,
    network_mac,
    parse_config,
    parse_properties,
    parse_vms,
    select_disk,
)
from vmctl.utils.sizes import parse_size, proxmox_disk_gib


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        ("48G", "MiB", 49152),
        ("2048", "MiB", 2048),
        ("2048M", "GiB", 2),
        ("160G", "GiB", 160),
        ("20", "GiB", 20),
        ("2GiB", "MiB", 2048),
        ("32mib", "MiB", 32),
    ],
)
def test_sizes(value: str, unit: str, expected: int) -> None:
    assert parse_size(value, unit=unit) == expected


@pytest.mark.parametrize("value", ["0", "-1", "1.5G", "2T", "1G; rm -rf /", "", "1MB", " 4G"])
def test_invalid_sizes(value: str) -> None:
    with pytest.raises(VmctlError):
        parse_size(value, unit="MiB")


def test_disk_granularity() -> None:
    with pytest.raises(VmctlError, match="whole"):
        parse_size("512M", unit="GiB")
    assert proxmox_disk_gib("1.5T") == 1536
    assert disk_gib_exact({"scsi0": "local:disk,size=512M"}, "scsi0") == 0.5


def test_presets_and_overrides(config: Configuration) -> None:
    assert config.resources(CreateRequest("test", "debian", "large")).cpu == 8
    result = config.resources(CreateRequest("test", "debian", "small", 16, "48G", "160G"))
    assert (result.cpu, result.memory_mib, result.disk_gib) == (16, 49152, 160)
    with pytest.raises(VmctlError, match="Unknown preset"):
        config.resources(CreateRequest("test", "debian", "missing"))
    with pytest.raises(VmctlError, match="CPU"):
        config.resources(CreateRequest("test", "debian", "small", cpu=0))
    assert config.template("rocky").vmid == 9030
    with pytest.raises(VmctlError, match="Unknown template"):
        config.template("missing")


def test_config_reloads_and_rejects_unknown_fields(config: Configuration) -> None:
    path = config.root / "presets.toml"
    path.write_text(path.read_text().replace("cpu = 2", "cpu = 3"))
    assert load_config(config.root).presets["small"].cpu == 3
    path.write_text(path.read_text() + "\nunknown = 1\n")
    with pytest.raises(VmctlError, match="Invalid configuration"):
        load_config(config.root)


@pytest.mark.parametrize("name", ["123", "Test", "-test", "test-", "a" * 64, "a/b", "a;id"])
def test_names(name: str) -> None:
    with pytest.raises(VmctlError):
        validate_name(name)


def test_qm_parser() -> None:
    config = parse_config(
        "cores: 2\nnet0: virtio=bc:24:11:aa:bb:cc,bridge=vmbr1\ndescription: hello%0Aworld\n"
    )
    assert config["description"] == "hello\nworld"
    assert network_mac(config["net0"]) == "BC:24:11:AA:BB:CC"
    assert parse_properties("local:disk,size=20G")["size"] == "20G"
    with pytest.raises(VmctlError):
        parse_config("broken")
    with pytest.raises(VmctlError):
        parse_config("cores: 1\ncores: 2")
    with pytest.raises(VmctlError):
        network_mac("virtio=invalid")
    assert parse_vms('[{"type":"lxc"}]') == []
    with pytest.raises(VmctlError):
        parse_vms("{}")


def test_disk_selection() -> None:
    config = {"scsi0": "local:a,size=40G", "scsi1": "local:b,size=5G", "ide2": "local:cloudinit"}
    with pytest.raises(VmctlError, match="primary disk"):
        select_disk(config)
    assert select_disk({**config, "boot": "order=scsi0;ide2"}) == "scsi0"
    assert select_disk(config, "scsi1") == "scsi1"
    with pytest.raises(VmctlError):
        select_disk(config, "ide2")


def test_metadata_round_trip() -> None:
    description = encode_metadata("Human description", {"template": "debian", "invocation": "123"})
    assert description.startswith("Human description")
    assert decode_metadata(description)["template"] == "debian"
    assert decode_metadata("unmanaged") == {}
    with pytest.raises(VmctlError):
        encode_metadata("[vmctl:stuff]", {})


@pytest.mark.parametrize("gateway", ["10.210.0.0", "10.210.0.255", "10.210.0.105"])
def test_invalid_gateway(config: Configuration, gateway: str) -> None:
    path = config.root / "config.toml"
    path.write_text(path.read_text().replace('gateway = "10.210.0.1"', f'gateway = "{gateway}"'))
    with pytest.raises(VmctlError, match="Invalid configuration"):
        load_config(config.root)


def test_legacy_network_model_mac() -> None:
    assert network_mac("ne2k_pci=BC:24:11:AA:BB:CC,bridge=vmbr1") == "BC:24:11:AA:BB:CC"
