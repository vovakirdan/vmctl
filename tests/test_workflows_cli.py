from collections.abc import Sequence
from pathlib import Path

import pytest
from conftest import FakeRunner
from typer.testing import CliRunner

from vmctl import cli
from vmctl.bootstrap.renderer import CloudInitRenderer
from vmctl.config import Configuration
from vmctl.errors import CreationError, PartialDeleteError, VmctlError
from vmctl.models import CreateRequest
from vmctl.network.allocator import AddressAllocator
from vmctl.network.dnsmasq import DnsmasqReservations
from vmctl.proxmox.client import ProxmoxClient
from vmctl.proxmox.parser import decode_metadata
from vmctl.services.create_vm import CreateVMService, read_public_key
from vmctl.services.delete_vm import DeleteVMService
from vmctl.services.queries import info_vm, list_vms
from vmctl.utils.subprocess import CommandResult


def services(
    config: Configuration, runner: FakeRunner
) -> tuple[CreateVMService, DeleteVMService, ProxmoxClient, DnsmasqReservations]:
    client = ProxmoxClient(config, runner)
    reservations = DnsmasqReservations(config, runner)
    creator = CreateVMService(
        config, client, reservations, AddressAllocator(config, runner), CloudInitRenderer(config)
    )
    return creator, DeleteVMService(config, client, reservations), client, reservations


def test_create_full_workflow(config: Configuration, runner: FakeRunner) -> None:
    creator, _, client, store = services(config, runner)
    result = creator.create(
        CreateRequest("surge-dev", "ubuntu-server", "large", modules=("surge-dev",))
    )
    assert result.vmid == 104 and str(result.ip) == "10.210.0.100"
    assert runner.status[104] == "running"
    vm = runner.vms[104]
    assert vm["cores"] == "8" and vm["sockets"] == "1" and vm["memory"] == "16384"
    assert "size=80G" in vm["scsi0"] and "bridge=vmbr1" in vm["net0"]
    assert vm["ciuser"] == "vmadmin" and vm["ipconfig0"] == "ip=dhcp" and vm["ciupgrade"] == "0"
    assert store.read()[0].mac == result.mac
    assert decode_metadata(vm["description"])["template"] == "ubuntu-server"
    assert "node" in result.modules
    assert info_vm(client, store, "surge-dev").ip == result.ip
    assert list_vms(client, store)[0].vm.vmid == 104
    clone_command = next(call for call in runner.calls if call[:2] == ("qm", "clone"))
    assert "--full" in clone_command
    key_command = next(call for call in runner.calls if "--sshkeys" in call)
    assert not Path(key_command[key_command.index("--sshkeys") + 1]).exists()
    assert not any("PRIVATE KEY" in " ".join(call) for call in runner.calls)


def test_snippets_error_explains_remediation_before_clone(
    config: Configuration, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = runner.run

    def without_snippets(args: Sequence[str], **kwargs: object) -> CommandResult:
        if args[:3] == ["pvesh", "get", "/storage/local"]:
            return CommandResult('{"type":"dir","content":"iso,backup"}')
        return run(args, **kwargs)

    monkeypatch.setattr(runner, "run", without_snippets)
    creator, _, _, _ = services(config, runner)
    with pytest.raises(VmctlError, match="keep all currently selected content types"):
        creator.plan(CreateRequest("preview", "ubuntu-server", "small"))
    assert not any(
        call[:2] == ("qm", "clone") or call[:2] == ("pvesm", "set") for call in runner.calls
    )


def test_no_start_and_no_disk_shrink(config: Configuration, runner: FakeRunner) -> None:
    runner.vms[9000]["scsi0"] = "local-lvm:large-template,size=100G"
    runner.vms[9000]["net1"] = "virtio=BC:24:11:22:22:22,bridge=vmbr0"
    runner.vms[9000]["cicustom"] = "user=local:snippets/manual.yaml"
    runner.vms[9000]["cipassword"] = "secret"
    creator, _, _, _ = services(config, runner)
    result = creator.create(CreateRequest("legacy-test", "ubuntu-server", "small", start=False))
    assert result.resources.disk_gib == 100 and not result.started
    assert not any(call[:3] == ("qm", "disk", "resize") for call in runner.calls)
    assert result.modules == ()
    assert result.system_features == ("qemu-agent",)
    assert "net1" not in runner.vms[104] and "cipassword" not in runner.vms[104]
    assert "net1" in runner.vms[9000]
    assert runner.status[104] == "stopped"


@pytest.mark.parametrize("stage", ["configuration", "resize", "reservation", "cloudinit", "start"])
def test_create_rollback(config: Configuration, runner: FakeRunner, stage: str) -> None:
    creator, _, _, store = services(config, runner)
    if stage == "configuration":
        runner.failure = lambda args: args[:2] == ["qm", "set"]
    elif stage == "resize":
        runner.failure = lambda args: args[:3] == ["qm", "disk", "resize"]
    elif stage == "reservation":
        runner.failure = lambda args: args[:2] == ["systemctl", "restart"]
    elif stage == "cloudinit":
        runner.failure = lambda args: args[:2] == ["qm", "cloudinit"]
    else:
        runner.failure = lambda args: args[:2] == ["qm", "start"]
    with pytest.raises(CreationError, match="creation failed"):
        creator.create(CreateRequest("test", "ubuntu-server", "small"))
    assert 104 not in runner.vms
    assert store.read() == []
    assert not list((runner.root / "snippets").glob("vmctl-*.yaml"))
    assert 9000 in runner.vms


def test_uncertain_clone_not_destroyed(config: Configuration, runner: FakeRunner) -> None:
    creator, _, _, _ = services(config, runner)
    runner.clone_timeout = True
    with pytest.raises(CreationError, match="Clone outcome is uncertain"):
        creator.create(CreateRequest("test", "ubuntu-server", "small"))
    assert 104 in runner.vms
    assert not any(call[:2] == ("qm", "destroy") for call in runner.calls)


def test_failed_vm_cleanup_keeps_reservation(config: Configuration, runner: FakeRunner) -> None:
    creator, _, _, store = services(config, runner)
    runner.failure = lambda args: args[:2] in (["qm", "start"], ["qm", "destroy"])
    runner.fail_once = False
    with pytest.raises(CreationError, match="retained"):
        creator.create(CreateRequest("test", "ubuntu-server", "small"))
    assert 104 in runner.vms and store.read()
    assert list((runner.root / "snippets").glob("vmctl-*.yaml"))


def test_ownership_failure_keeps_existing_vm(
    config: Configuration, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator, _, client, store = services(config, runner)
    original = client.vm_config

    def changed(vmid: int) -> dict[str, str]:
        value = original(vmid)
        if vmid == 104:
            value["description"] = "Foreign VM"
        return value

    monkeypatch.setattr(client, "vm_config", changed)
    with pytest.raises(CreationError, match="ownership"):
        creator.create(CreateRequest("test", "ubuntu-server", "small"))
    assert 104 in runner.vms and not store.read()


def test_mac_conflict_does_not_remove_existing_reservation(
    config: Configuration, runner: FakeRunner
) -> None:
    creator, _, _, store = services(config, runner)
    from ipaddress import IPv4Address

    from vmctl.network.dnsmasq import Reservation

    store.add(Reservation(runner.mac, IPv4Address("10.210.0.105"), "existing", 105))
    with pytest.raises(CreationError, match="MAC conflicts"):
        creator.create(CreateRequest("test", "ubuntu-server", "small"))
    assert store.read()[0].name == "existing"


def test_preflight_error_never_clones(config: Configuration, runner: FakeRunner) -> None:
    creator, _, _, _ = services(config, runner)
    with pytest.raises(VmctlError, match="no implementation"):
        creator.create(CreateRequest("test", "alpine", "small", modules=("node",)))
    assert not any(call[:2] == ("qm", "clone") for call in runner.calls)


def test_delete_and_release(config: Configuration, runner: FakeRunner) -> None:
    creator, deleter, _, store = services(config, runner)
    result = creator.create(CreateRequest("test", "ubuntu-server", "small"))
    plan = deleter.plan("test")
    assert deleter.delete("104", expected=plan) == result.vmid
    assert 104 not in runner.vms and not store.read()
    assert not list((runner.root / "snippets").glob("vmctl-*.yaml"))


def test_delete_unmanaged_vm_and_protect_templates(
    config: Configuration, runner: FakeRunner
) -> None:
    _, deleter, _, _ = services(config, runner)
    runner.vms[200] = {"name": "manual", "cores": "1", "memory": "1024"}
    plan = deleter.plan("manual")
    deleter.delete("200", expected=plan)
    assert 200 not in runner.vms
    with pytest.raises(VmctlError, match="Templates"):
        deleter.plan("9000")


def test_delete_failure_preserves_ip(config: Configuration, runner: FakeRunner) -> None:
    creator, deleter, _, store = services(config, runner)
    creator.create(CreateRequest("test", "ubuntu-server", "small"))
    plan = deleter.plan("104")
    runner.failure = lambda args: args[:2] == ["qm", "destroy"]
    with pytest.raises(VmctlError):
        deleter.delete("104", expected=plan)
    assert 104 in runner.vms and store.read()


def test_delete_reports_dhcp_failure_after_destroy(
    config: Configuration, runner: FakeRunner
) -> None:
    creator, deleter, _, store = services(config, runner)
    creator.create(CreateRequest("test", "ubuntu-server", "small"))
    plan = deleter.plan("104")
    runner.failure = lambda args: args[:2] == ["systemctl", "restart"]
    with pytest.raises(PartialDeleteError, match="was destroyed"):
        deleter.delete("104", expected=plan)
    assert 104 not in runner.vms and store.read()


def test_vm_resolution(config: Configuration, runner: FakeRunner) -> None:
    client = ProxmoxClient(config, runner)
    assert client.resolve("9000").name == "ubuntu-server"
    assert client.resolve("ubuntu-server").vmid == 9000
    with pytest.raises(VmctlError, match="No local"):
        client.resolve("missing")
    runner.vms[104] = {"name": "duplicate"}
    runner.vms[105] = {"name": "duplicate"}
    with pytest.raises(VmctlError, match="Ambiguous"):
        client.resolve("duplicate")
    runner.nextid = 9002
    assert client.next_id() == 9100


def test_public_key_validation(config: Configuration, tmp_path: Path) -> None:
    assert read_public_key(config.path(config.host.ssh_key)).startswith("ssh-ed25519")
    invalid = tmp_path / "invalid"
    for text in ("-----BEGIN OPENSSH PRIVATE KEY-----", "ssh-ed25519 not-base64", ""):
        invalid.write_text(text)
        with pytest.raises(VmctlError):
            read_public_key(invalid)


def test_cli_config_help_and_validation(config: Configuration) -> None:
    command = CliRunner()
    assert command.invoke(cli.app, ["--help"]).exit_code == 0
    result = command.invoke(
        cli.app, ["--local", "--config-dir", str(config.root), "config", "validate"]
    )
    assert result.exit_code == 0, result.output
    assert "Configuration valid" in result.output
    assert (
        command.invoke(cli.app, ["--local", "--config-dir", str(config.root), "presets"]).exit_code
        == 0
    )
    assert (
        command.invoke(
            cli.app, ["--local", "--config-dir", str(config.root), "templates"]
        ).exit_code
        == 0
    )


def test_cli_dry_run_and_confirmation(
    config: Configuration, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, client, store = services(config, runner)
    monkeypatch.setattr(
        cli, "dependencies", lambda config: (client, store, AddressAllocator(config, runner))
    )
    command = CliRunner()
    args = ["--local", "--config-dir", str(config.root)]
    result = command.invoke(
        cli.app, [*args, "create", "test", "ubuntu-server", "small", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert not store.path.exists() and not config.path(config.host.lock_file).exists()
    assert not any(call[:2] == ("qm", "clone") for call in runner.calls)
    runner.vms[200] = {"name": "manual"}
    result = command.invoke(cli.app, [*args, "delete", "200"], input="n\n")
    assert result.exit_code != 0 and 200 in runner.vms
    result = command.invoke(cli.app, [*args, "delete", "200", "--yes"])
    assert result.exit_code == 0, result.output
    assert 200 not in runner.vms


def test_delete_confirmation_binds_configuration(config: Configuration, runner: FakeRunner) -> None:
    _, deleter, _, _ = services(config, runner)
    runner.vms[200] = {"name": "manual", "memory": "1024"}
    plan = deleter.plan("200")
    runner.vms[200]["memory"] = "2048"
    with pytest.raises(VmctlError, match="changed since confirmation"):
        deleter.delete("200", expected=plan)
    assert 200 in runner.vms


def test_unmanaged_delete_does_not_create_reservations(
    config: Configuration, runner: FakeRunner
) -> None:
    _, deleter, _, store = services(config, runner)
    runner.vms[200] = {"name": "manual", "net0": "virtio=BC:24:11:99:99:99,bridge=vmbr1"}
    plan = deleter.plan("200")
    deleter.delete("200", expected=plan)
    assert not store.path.exists()
    assert not any(call[0] in {"dnsmasq", "systemctl"} for call in runner.calls)


def test_nextid_skips_reserved_range_and_existing_id(
    config: Configuration, runner: FakeRunner
) -> None:
    runner.nextid = 9002
    runner.vms[9100] = {"name": "already-used"}
    assert ProxmoxClient(config, runner).next_id() == 9101


def test_wait_timeout_retains_vm(
    config: Configuration, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vmctl.services import create_vm

    monkeypatch.setattr(create_vm, "wait_for_ssh", lambda address, timeout: False)
    creator, _, _, store = services(config, runner)
    result = creator.create(CreateRequest("wait-test", "ubuntu-server", "small", wait_seconds=1))
    assert "timed out" in result.readiness
    assert 104 in runner.vms and store.read()


def test_no_start_wait_rejected(config: Configuration, runner: FakeRunner) -> None:
    creator, _, _, _ = services(config, runner)
    with pytest.raises(VmctlError, match="requires --start"):
        creator.plan(CreateRequest("test", "ubuntu-server", "small", start=False, wait_seconds=1))
