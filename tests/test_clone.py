"""Full-clone behavior is verified only against isolated fake host operations."""

from collections.abc import Callable, Sequence
from dataclasses import replace
from ipaddress import IPv4Address

import pytest
import yaml
from conftest import FakeRunner

from vmctl.config import Configuration
from vmctl.errors import (
    CommandError,
    CreationError,
    RepeatedRequestError,
    UncertainOperationError,
    VmctlError,
)
from vmctl.models import CloneRequest
from vmctl.network.allocator import AddressAllocator
from vmctl.network.dnsmasq import DnsmasqReservations, Reservation
from vmctl.proxmox.client import ProxmoxClient
from vmctl.proxmox.parser import decode_metadata, encode_metadata
from vmctl.services.clone_vm import CloneVMService
from vmctl.services.delete_vm import DeleteVMService

TOKEN = "a" * 32
SOURCE_TOKEN = "b" * 32
SOURCE_MAC = "BC:24:11:00:02:00"


@pytest.fixture
def source(runner: FakeRunner) -> dict[str, str]:
    config = {
        "name": "android-01",
        "sata0": "local-lvm:vm-200-disk-0,size=40G",
        "net0": f"virtio={SOURCE_MAC},bridge=vmbr1,firewall=1",
        "cores": "4",
        "sockets": "1",
        "memory": "6144",
        "vga": "virtio",
        "bios": "ovmf",
        "ostype": "l26",
        "boot": "order=sata0",
        "onboot": "1",
    }
    runner.vms[200] = config
    runner.status[200] = "stopped"
    return config


@pytest.fixture
def service(config: Configuration, runner: FakeRunner) -> CloneVMService:
    return CloneVMService(
        config,
        ProxmoxClient(config, runner),
        DnsmasqReservations(config, runner),
        AddressAllocator(config, runner),
    )


def request(**kwargs: object) -> CloneRequest:
    return replace(CloneRequest("200", "android-copy"), **kwargs)


def mutations(runner: FakeRunner) -> list[tuple[str, ...]]:
    return [
        call
        for call in runner.calls
        if call[0] == "qm" and call[1] in {"clone", "set", "start", "stop", "shutdown", "destroy"}
    ]


def cloud_source(source: dict[str, str], *, managed: bool = False) -> None:
    source.update(
        ide2="local-lvm:vm-200-cloudinit,media=cdrom",
        ciuser="vmadmin",
        cipassword="secret-password-hash",
        sshkeys="ssh-ed25519 old-public-key",
        ipconfig0="ip=10.210.0.20/24,gw=10.210.0.1",
        ciupgrade="1",
    )
    if managed:
        volume = f"local:snippets/vmctl-200-{SOURCE_TOKEN}.yaml"
        source["cicustom"] = "user=" + volume
        source["description"] = encode_metadata(
            "Source user description",
            {
                "invocation": SOURCE_TOKEN,
                "snippet": volume,
                "template": "ubuntu-server",
                "preset": "normal",
                "modules": "rust,node",
                "system_features": "qemu-agent",
                "username": "vmadmin",
                "desktop": "false",
            },
        )


def test_android_clone_preserves_hardware_and_source(
    config: Configuration, source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    original = dict(source)
    service.reservations.add(
        Reservation(SOURCE_MAC, IPv4Address("10.210.0.112"), "android-01", 200)
    )
    preview = service.plan(request())
    result = service.clone(preview, request_id=TOKEN)
    assert source == original and runner.status[200] == "stopped"
    assert result.vm.vmid == 104 and result.vm.status == "stopped"
    assert result.source_vmid == 200 and result.source_restored and not result.cloud_init
    assert result.mac == runner.mac and result.mac != SOURCE_MAC
    assert result.ip == IPv4Address("10.210.0.100")
    clone = runner.vms[104]
    for key in ("vga", "cores", "sockets", "memory", "bios", "ostype", "boot"):
        assert clone[key] == original[key]
    assert clone["sata0"] != original["sata0"] and "size=40G" in clone["sata0"]
    assert "vm-104-" in clone["sata0"]
    assert clone["net0"] == original["net0"].replace(SOURCE_MAC, result.mac)
    assert clone["onboot"] == "0"
    assert "ciuser" not in clone and "cicustom" not in clone
    assert decode_metadata(clone["description"]) == {"clone_of": "200", "invocation": TOKEN}
    assert {entry.vmid for entry in service.reservations.read()} == {200, 104}
    assert any(
        call[:4] == ("qm", "clone", "200", "104") and "--full" in call for call in runner.calls
    )
    assert not any(call[:2] in {("qm", "stop"), ("qm", "cloudinit")} for call in runner.calls)
    assert not (runner.root / "snippets").exists()


def test_clone_preview_is_read_only(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    before = dict(source)
    preview = service.plan(request(ip="10.210.0.105"))
    assert not preview.cloud_init and preview.notes
    assert not mutations(runner) and source == before
    assert not service.reservations.path.exists()
    assert not service.config.path(service.config.host.lock_file).exists()


@pytest.mark.parametrize("reference", ["200", "android-01"])
def test_clone_resolves_name_or_vmid(
    source: dict[str, str], service: CloneVMService, reference: str
) -> None:
    assert service.plan(request(reference=reference)).source.vmid == 200


@pytest.mark.parametrize("name", ["Capital", "-copy", "copy;touch", "a" * 64, "trailing-"])
def test_clone_invalid_name_rejected(
    source: dict[str, str], service: CloneVMService, name: str
) -> None:
    with pytest.raises(VmctlError, match="Name must"):
        service.plan(request(name=name))


def test_clone_name_collision_rejected(source: dict[str, str], service: CloneVMService) -> None:
    with pytest.raises(VmctlError, match="already exists"):
        service.plan(request(name="android-01"))


@pytest.mark.parametrize("address", ["10.210.0.20", "10.200.0.105", "invalid"])
def test_clone_invalid_pool_ip_rejected(
    source: dict[str, str], service: CloneVMService, address: str
) -> None:
    with pytest.raises(VmctlError, match="IP|IPv4"):
        service.plan(request(ip=address))


def test_clone_template_rejected(service: CloneVMService) -> None:
    with pytest.raises(VmctlError, match="use create"):
        service.plan(request(reference="9000"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("hookscript", "local:snippets/hook.pl"),
        ("args", "-device something"),
        ("hostpci0", "0000:01:00.0"),
        ("usb0", "host=1234:5678"),
        ("serial0", "/dev/ttyS0"),
        ("virtiofs0", "dirid=shared"),
        ("unused0", "local-lvm:vm-200-disk-1"),
        ("sata0", "/dev/sdb,size=40G"),
        ("sata0", "local-lvm:vm-200-disk-0,size=40G,shared=1"),
        ("net0", f"virtio={SOURCE_MAC},bridge=vmbr0"),
        ("net0", f"virtio={SOURCE_MAC},bridge=vmbr1,tag=10"),
        ("net1", "e1000=BC:24:11:00:02:01,bridge=vmbr1"),
        ("lock", "backup"),
    ],
)
def test_unsafe_clone_sources_fail_before_mutation(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner, field: str, value: str
) -> None:
    source[field] = value
    with pytest.raises(VmctlError):
        service.plan(request())
    assert not mutations(runner)


def test_pending_config_rejected(
    source: dict[str, str],
    service: CloneVMService,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service.client,
        "pending",
        lambda vmid: [{"key": "memory", "value": "6144", "pending": "8192"}],
    )
    with pytest.raises(VmctlError, match="pending"):
        service.plan(request())
    assert not mutations(runner)


@pytest.mark.parametrize(
    "custom",
    [
        "user=local:snippets/foreign.yaml",
        "network=local:snippets/network.yaml",
        f"user=local:snippets/vmctl-200-{SOURCE_TOKEN}.yaml,vendor=local:snippets/vendor.yaml",
        f"user=local:snippets/vmctl-201-{SOURCE_TOKEN}.yaml",
        f"user=local:snippets/foreign.yaml,user=local:snippets/vmctl-200-{SOURCE_TOKEN}.yaml",
    ],
)
def test_custom_cloudinit_requires_owned_user_only(
    source: dict[str, str], service: CloneVMService, custom: str
) -> None:
    cloud_source(source, managed=True)
    source["cicustom"] = custom
    with pytest.raises(VmctlError, match="vmctl-owned user-data only"):
        service.plan(request())


@pytest.mark.parametrize("managed", [False, True])
def test_cloud_clone_has_fresh_minimal_userdata_and_never_replays_bootstrap(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner, managed: bool
) -> None:
    cloud_source(source, managed=managed)
    original = dict(source)
    result = service.clone(service.plan(request()), request_id=TOKEN)
    clone = runner.vms[104]
    assert result.cloud_init and source == original
    assert clone["ipconfig0"] == "ip=dhcp" and clone["ciupgrade"] == "0"
    assert not {"cipassword", "sshkeys", "ciuser"} & clone.keys()
    assert clone["cicustom"] == f"user=local:snippets/vmctl-104-{TOKEN}.yaml"
    snippet = runner.root / "snippets" / f"vmctl-104-{TOKEN}.yaml"
    data = yaml.safe_load(snippet.read_text())
    assert data == {
        "hostname": "android-copy",
        "preserve_hostname": False,
        "manage_etc_hosts": True,
        "package_update": False,
        "package_upgrade": False,
    }
    assert snippet.stat().st_mode & 0o777 == 0o600
    assert (
        not {
            "users",
            "password",
            "ssh_authorized_keys",
            "packages",
            "write_files",
            "runcmd",
            "bootcmd",
        }
        & data.keys()
    )
    assert "secret" not in snippet.read_text()
    metadata = decode_metadata(clone["description"])
    assert metadata["snippet"] == f"local:snippets/vmctl-104-{TOKEN}.yaml"
    if managed:
        assert metadata["modules"] == "rust,node"
    assert any(call[:3] == ("qm", "cloudinit", "update") for call in runner.calls)


def test_delete_clone_never_removes_source_snippet(
    config: Configuration, source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    cloud_source(source, managed=True)
    source_snippet = runner.root / "snippets" / f"vmctl-200-{SOURCE_TOKEN}.yaml"
    source_snippet.parent.mkdir(parents=True)
    source_snippet.write_text("secret source payload\n")
    service.reservations.add(
        Reservation(SOURCE_MAC, IPv4Address("10.210.0.112"), "android-01", 200)
    )
    result = service.clone(service.plan(request()), request_id=TOKEN)
    deleter = DeleteVMService(config, service.client, service.reservations)
    deleter.delete(str(result.vm.vmid), expected=deleter.plan(str(result.vm.vmid)))
    assert source_snippet.read_text() == "secret source payload\n"
    assert source["cicustom"] == f"user=local:snippets/vmctl-200-{SOURCE_TOKEN}.yaml"
    assert service.reservations.read() == [
        Reservation(SOURCE_MAC, IPv4Address("10.210.0.112"), "android-01", 200)
    ]


def test_running_source_requires_explicit_shutdown(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    runner.status[200] = "running"
    with pytest.raises(VmctlError, match="explicitly allow"):
        service.plan(request())
    assert not mutations(runner)


def test_running_source_is_gracefully_stopped_and_restored(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    original = dict(source)
    runner.status[200] = "running"
    result = service.clone(
        service.plan(request(shutdown_source=True, start=True)), request_id=TOKEN
    )
    assert (
        source == original and runner.status[200] == "running" and runner.status[104] == "running"
    )
    assert result.source_restored
    calls = mutations(runner)
    assert calls[0][:3] == ("qm", "shutdown", "200") and "--forceStop" in calls[0]
    assert calls[0][calls[0].index("--forceStop") + 1] == "0"
    assert ("qm", "start", "200") in calls and not any(call[1] == "stop" for call in calls)


@pytest.mark.parametrize("field", ["cores", "net0", "description"])
def test_confirmation_binds_source_configuration(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner, field: str
) -> None:
    preview = service.plan(request())
    source[field] = (
        "8"
        if field == "cores"
        else (f"virtio={SOURCE_MAC},bridge=vmbr1,firewall=0" if field == "net0" else "Changed")
    )
    with pytest.raises(VmctlError, match="changed since confirmation"):
        service.clone(preview, request_id=TOKEN)
    assert not mutations(runner)


def test_confirmation_binds_source_direct_status(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    runner.status[200] = "running"
    preview = service.plan(request(shutdown_source=True))
    runner.status[200] = "stopped"
    with pytest.raises(VmctlError, match="changed since confirmation"):
        service.clone(preview, request_id=TOKEN)
    assert not mutations(runner)


def test_confirmation_binds_request(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    preview = service.plan(request())
    changed = replace(preview, request=request(start=True))
    with pytest.raises(VmctlError, match="changed since confirmation"):
        service.clone(changed, request_id=TOKEN)
    assert not mutations(runner)


def test_replayed_clone_request_is_rejected(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    preview = service.plan(request())
    service.clone(preview, request_id=TOKEN)
    count = len(mutations(runner))
    with pytest.raises(RepeatedRequestError) as error:
        service.clone(preview, request_id=TOKEN)
    assert error.value.vmid == 104 and len(mutations(runner)) == count


def test_invalid_request_id_has_no_mutations(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    with pytest.raises(VmctlError, match="request ID"):
        service.clone(service.plan(request()), request_id="invalid")
    assert not mutations(runner)


@pytest.mark.parametrize("stage", ["configuration", "reservation", "cloudinit", "start"])
def test_clone_failure_cleans_only_invocation_owned_clone(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner, stage: str
) -> None:
    cloud_source(source, managed=True)
    original = dict(source)
    service.reservations.add(
        Reservation(SOURCE_MAC, IPv4Address("10.210.0.112"), "android-01", 200)
    )
    preview = service.plan(request(start=True))
    failures: dict[str, Callable[[Sequence[str]], bool]] = {
        "configuration": lambda args: args[:3] == ["qm", "set", "104"],
        "reservation": lambda args: args[:2] == ["systemctl", "restart"],
        "cloudinit": lambda args: args[:3] == ["qm", "cloudinit", "update"],
        "start": lambda args: args[:3] == ["qm", "start", "104"],
    }
    runner.failure = failures[stage]
    with pytest.raises(CreationError, match="clone failed"):
        service.clone(preview, request_id=TOKEN)
    assert 104 not in runner.vms and source == original
    assert service.reservations.read() == [
        Reservation(SOURCE_MAC, IPv4Address("10.210.0.112"), "android-01", 200)
    ]
    assert not list((runner.root / "snippets").glob(f"vmctl-104-{TOKEN}.yaml"))
    assert not any(call[:3] == ("qm", "destroy", "200") for call in runner.calls)


def test_clone_timeout_retains_clone_and_does_not_restart_source(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    runner.status[200] = "running"
    preview = service.plan(request(shutdown_source=True))
    runner.clone_timeout = True
    with pytest.raises(UncertainOperationError, match="source VM 200 remains stopped"):
        service.clone(preview, request_id=TOKEN)
    assert 104 in runner.vms and runner.status[200] == "stopped"
    assert not any(
        call[:2] in {("qm", "destroy"), ("qm", "start"), ("qm", "stop")} for call in runner.calls
    )
    with pytest.raises(RepeatedRequestError):
        service.clone(preview, request_id=TOKEN)


def test_source_shutdown_timeout_does_not_clone_or_force_stop(
    source: dict[str, str],
    service: CloneVMService,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner.status[200] = "running"
    preview = service.plan(request(shutdown_source=True))

    def timeout(vmid: int) -> None:
        raise CommandError("qm", "shutdown timeout", timed_out=True)

    monkeypatch.setattr(service.client, "shutdown", timeout)
    with pytest.raises(UncertainOperationError, match="source shutdown"):
        service.clone(preview, request_id=TOKEN)
    assert 104 not in runner.vms and not mutations(runner)


def test_start_timeout_keeps_clone_reservation(
    source: dict[str, str],
    service: CloneVMService,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = service.plan(request(start=True))

    def timeout(vmid: int) -> None:
        raise CommandError("qm", "start timeout", timed_out=True)

    monkeypatch.setattr(service.client, "start", timeout)
    with pytest.raises(UncertainOperationError, match="clone start"):
        service.clone(preview, request_id=TOKEN)
    assert 104 in runner.vms and service.reservations.read()[0].vmid == 104
    assert not any(call[:2] == ("qm", "destroy") for call in runner.calls)


def test_failed_source_restore_reports_completed_clone(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    runner.status[200] = "running"
    preview = service.plan(request(shutdown_source=True))
    runner.failure = lambda args: args[:3] == ["qm", "start", "200"]
    result = service.clone(preview, request_id=TOKEN)
    assert not result.source_restored and result.vm.vmid == 104 and result.notes
    assert runner.status[200] == "stopped" and runner.status[104] == "stopped"
    assert not any(call[:2] == ("qm", "destroy") for call in runner.calls)


def test_ownership_loss_never_destroys_or_removes_foreign_resources(
    source: dict[str, str],
    service: CloneVMService,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = service.client.vm_config

    def foreign(vmid: int) -> dict[str, str]:
        config = original(vmid)
        if vmid == 104:
            config["description"] = "Foreign VM"
        return config

    monkeypatch.setattr(service.client, "vm_config", foreign)
    with pytest.raises(CreationError, match="ownership"):
        service.clone(service.plan(request()), request_id=TOKEN)
    assert 104 in runner.vms and 200 in runner.vms
    assert not any(call[:2] == ("qm", "destroy") for call in runner.calls)


def test_mac_conflict_never_removes_existing_reservation(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    service.reservations.add(Reservation(runner.mac, IPv4Address("10.210.0.105"), "existing", 105))
    with pytest.raises(CreationError, match="MAC conflicts"):
        service.clone(service.plan(request()), request_id=TOKEN)
    assert service.reservations.read()[0].vmid == 105 and 104 not in runner.vms


def test_rollback_never_removes_reservation_created_by_another_administrator(
    source: dict[str, str],
    service: CloneVMService,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_add = service.reservations.add

    def race(reservation: Reservation) -> None:
        original_add(replace(reservation, vmid=777, name="another-vm"))
        raise VmctlError("A concurrent reservation update prevented this operation")

    monkeypatch.setattr(service.reservations, "add", race)
    with pytest.raises(CreationError, match="unrelated reservation retained"):
        service.clone(service.plan(request()), request_id=TOKEN)
    assert 104 not in runner.vms and 200 in runner.vms
    assert service.reservations.read() == [
        Reservation(runner.mac, IPv4Address("10.210.0.100"), "another-vm", 777)
    ]


def test_static_source_cloudinit_ip_is_excluded_from_new_reservation(
    source: dict[str, str], service: CloneVMService
) -> None:
    cloud_source(source)
    source["ipconfig0"] = "ip=10.210.0.100/24,gw=10.210.0.1"
    result = service.clone(service.plan(request()), request_id=TOKEN)
    assert result.ip == IPv4Address("10.210.0.101")
    assert source["ipconfig0"] == "ip=10.210.0.100/24,gw=10.210.0.1"


def test_source_change_between_revalidation_and_snapshot_is_rejected(
    source: dict[str, str],
    service: CloneVMService,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = service.plan(request())
    original = service.client.vm_config
    reads = 0

    def changed(vmid: int) -> dict[str, str]:
        nonlocal reads
        config = original(vmid)
        if vmid == 200:
            reads += 1
            if reads == 3:
                config["cores"] = "8"
        return config

    monkeypatch.setattr(service.client, "vm_config", changed)
    with pytest.raises(VmctlError, match="changed since confirmation"):
        service.clone(preview, request_id=TOKEN)
    assert not mutations(runner)


def test_source_restart_after_safe_clone_failure(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    runner.status[200] = "running"
    preview = service.plan(request(shutdown_source=True))
    runner.failure = lambda args: args[:3] == ["qm", "set", "104"]
    with pytest.raises(CreationError, match="clone failed"):
        service.clone(preview, request_id=TOKEN)
    assert runner.status[200] == "running" and 104 not in runner.vms
    assert not any(call[:2] == ("qm", "stop") for call in runner.calls)


def test_failed_cleanup_retains_clone_and_its_reservation(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    preview = service.plan(request(start=True))
    runner.failure = lambda args: args[:2] in (["qm", "start"], ["qm", "destroy"])
    runner.fail_once = False
    with pytest.raises(CreationError, match="retained"):
        service.clone(preview, request_id=TOKEN)
    assert 104 in runner.vms and service.reservations.read()[0].vmid == 104


def test_clone_progress_identifies_vmid_before_submission(
    source: dict[str, str], service: CloneVMService
) -> None:
    progress: list[str] = []
    service.clone(service.plan(request()), request_id=TOKEN, progress=progress.append)
    assert progress[0] == "Allocated VMID: 104"


def test_changed_clone_network_never_starts_or_keeps_public_clone(
    source: dict[str, str],
    service: CloneVMService,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = service.client.clone

    def changed(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)
        runner.vms[104]["net0"] = f"virtio={runner.mac},bridge=vmbr0"

    monkeypatch.setattr(service.client, "clone", changed)
    with pytest.raises(CreationError, match="private bridge"):
        service.clone(service.plan(request(start=True)), request_id=TOKEN)
    assert 104 not in runner.vms and 200 in runner.vms
    assert not any(call[:2] == ("qm", "start") for call in runner.calls)


@pytest.mark.parametrize(
    "disk,foreign",
    [("sata0", False), ("sata0", True), ("efidisk0", False), ("tpmstate0", False), ("ide2", False)],
)
def test_partial_clone_referencing_unowned_disk_is_retained_without_destroy(
    source: dict[str, str],
    service: CloneVMService,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    disk: str,
    foreign: bool,
) -> None:
    if disk == "ide2":
        cloud_source(source)
    elif disk != "sata0":
        source[disk] = "local-lvm:vm-200-disk-1,size=4M"
    original = service.client.clone

    def incomplete(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)
        runner.vms[104][disk] = (
            source[disk].replace("vm-200-", "vm-999-") if foreign else source[disk]
        )

    monkeypatch.setattr(service.client, "clone", incomplete)
    with pytest.raises(CreationError, match=f"Disk {disk} ownership could not be verified"):
        service.clone(service.plan(request()), request_id=TOKEN)
    assert 104 in runner.vms and 200 in runner.vms
    assert not any(call[:2] == ("qm", "destroy") for call in runner.calls)


def test_source_started_after_preflight_is_not_cloned(
    source: dict[str, str],
    service: CloneVMService,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = service.plan(request())
    original = service.client.next_id

    def restarted() -> int:
        result = original()
        runner.status[200] = "running"
        return result

    monkeypatch.setattr(service.client, "next_id", restarted)
    with pytest.raises(CreationError, match="no longer stopped"):
        service.clone(preview, request_id=TOKEN)
    assert 104 not in runner.vms and not mutations(runner)


def test_external_destination_start_prevents_configuration_and_destruction(
    source: dict[str, str],
    service: CloneVMService,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = service.client.clone

    def started(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)
        runner.status[104] = "running"

    monkeypatch.setattr(service.client, "clone", started)
    with pytest.raises(CreationError, match="no configuration was applied"):
        service.clone(service.plan(request()), request_id=TOKEN)
    assert 104 in runner.vms and runner.status[104] == "running"
    assert not any(
        call[:2] in {("qm", "set"), ("qm", "destroy"), ("qm", "stop")} for call in runner.calls
    )


def test_unreadable_source_status_after_shutdown_requires_inspection(
    source: dict[str, str],
    service: CloneVMService,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner.status[200] = "running"
    preview = service.plan(request(shutdown_source=True))
    original = service.client.status

    def unreadable(vmid: int) -> str:
        if vmid == 200 and runner.status[200] == "stopped":
            raise CommandError("qm", "cannot read status")
        return original(vmid)

    monkeypatch.setattr(service.client, "status", unreadable)
    with pytest.raises(UncertainOperationError, match="source state verification after shutdown"):
        service.clone(preview, request_id=TOKEN)
    assert 104 not in runner.vms and runner.status[200] == "stopped"
    assert not any(
        call[:2] in {("qm", "start"), ("qm", "stop"), ("qm", "clone")} for call in runner.calls
    )


def test_immutable_iso_remains_shared_while_guest_disk_is_owned(
    source: dict[str, str], service: CloneVMService, runner: FakeRunner
) -> None:
    source["ide0"] = "local:iso/android.iso,media=cdrom"
    service.clone(service.plan(request()), request_id=TOKEN)
    assert runner.vms[104]["ide0"] == source["ide0"]
    assert runner.vms[104]["sata0"] != source["sata0"]


@pytest.mark.parametrize("phase", ["shutdown", "clone", "configure", "start"])
def test_interrupted_mutation_never_force_stops_or_destroys(
    source: dict[str, str],
    service: CloneVMService,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    if phase in {"shutdown", "clone", "configure"}:
        runner.status[200] = "running"
    preview = service.plan(
        request(start=phase == "start", shutdown_source=phase in {"shutdown", "clone", "configure"})
    )
    original_clone = service.client.clone

    def interrupted(*args: object, **kwargs: object) -> None:
        if phase == "clone":
            original_clone(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(service.client, phase, interrupted)
    with pytest.raises(UncertainOperationError):
        service.clone(preview, request_id=TOKEN)
    assert not any(call[:2] in {("qm", "destroy"), ("qm", "stop")} for call in runner.calls)
    if phase != "shutdown":
        assert 104 in runner.vms
    if phase in {"clone", "configure"}:
        assert runner.status[200] == "stopped"
        assert not any(call[:3] == ("qm", "start", "200") for call in runner.calls)
