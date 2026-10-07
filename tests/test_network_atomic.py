from ipaddress import IPv4Address
from pathlib import Path

import pytest
from conftest import FakeRunner

from vmctl.config import Configuration
from vmctl.errors import VmctlError
from vmctl.network.allocator import AddressAllocator, allocate_ip, lease_addresses
from vmctl.network.dnsmasq import (
    WARNING,
    DnsmasqReservations,
    Reservation,
    parse_reservations,
    render_reservations,
)
from vmctl.utils.atomic import atomic_write, host_lock


def reservation(last: int = 105) -> Reservation:
    return Reservation("BC:24:11:AA:BB:CC", IPv4Address(f"10.210.0.{last}"), "surge-dev", 104)


def test_reservation_round_trip() -> None:
    entry = reservation()
    text = render_reservations([entry])
    assert text.startswith(WARNING)
    assert "dhcp-host=BC:24:11:AA:BB:CC,10.210.0.105,surge-dev,infinite" in text
    assert parse_reservations(text) == [entry]
    assert (
        parse_reservations(
            WARNING + "\ndhcp-host=BC:24:11:AA:BB:CC,10.210.0.105,surge-dev,infinite\n"
        )[0].vmid
        is None
    )


@pytest.mark.parametrize(
    "text", ["manually managed", WARNING + "\nbroken", WARNING + "\n# vmctl-vmid=104\n"]
)
def test_malformed_reservation_file(text: str) -> None:
    with pytest.raises(VmctlError):
        parse_reservations(text)
    with pytest.raises(VmctlError, match="Duplicate"):
        render_reservations([reservation(), reservation()])


def test_allocation() -> None:
    start, end = IPv4Address("10.210.0.100"), IPv4Address("10.210.0.103")
    assert allocate_ip(
        start, end, [start], responds=lambda ip: ip == IPv4Address("10.210.0.101")
    ) == IPv4Address("10.210.0.102")
    with pytest.raises(VmctlError, match="pool"):
        allocate_ip(start, end, [], requested="10.210.0.20")
    with pytest.raises(VmctlError, match="No available"):
        allocate_ip(start, end, [start], requested=str(start))
    with pytest.raises(VmctlError, match="Invalid IPv4"):
        allocate_ip(start, end, [], requested="bad")


def test_leases_are_conservative(config: Configuration, runner: FakeRunner) -> None:
    leases = config.path(config.host.network.leases)
    leases.write_text("0 bc:24:11:00:00:01 10.210.0.100 existing *\n")
    assert IPv4Address("10.210.0.100") in lease_addresses(leases)
    allocator = AddressAllocator(config, runner)
    assert allocator.select([]) == IPv4Address("10.210.0.101")


def test_arp_probe_interpretation(config: Configuration, runner: FakeRunner) -> None:
    allocator = AddressAllocator(config, runner)
    address = IPv4Address("10.210.0.100")
    assert not allocator.responds(address)
    runner.arp_codes[str(address)] = 1
    assert allocator.responds(address)
    runner.arp_codes[str(address)] = 2
    with pytest.raises(VmctlError, match="probe failed"):
        allocator.responds(address)


def test_dnsmasq_update_and_mac_release(config: Configuration, runner: FakeRunner) -> None:
    store = DnsmasqReservations(config, runner)
    store.preflight()
    store.add(reservation())
    assert store.read() == [reservation()]
    store.remove_mac("bc:24:11:aa:bb:cc")
    assert store.read() == []
    assert ("systemctl", "restart", "dnsmasq") in runner.calls
    assert all(not file.name.startswith(".") for file in store.path.parent.iterdir())


@pytest.mark.parametrize(
    "failure_stage", ["temp-validation", "live-validation", "restart", "active"]
)
def test_dnsmasq_rolls_back(config: Configuration, runner: FakeRunner, failure_stage: str) -> None:
    store = DnsmasqReservations(config, runner)
    store.add(reservation())
    before = store.path.read_bytes()
    second = Reservation("BC:24:11:00:00:02", IPv4Address("10.210.0.106"), "other", 105)
    if failure_stage == "temp-validation":
        runner.failure = lambda args: args[0] == "dnsmasq" and "/.vmctl-hosts.conf." in args[-1]
    elif failure_stage == "live-validation":
        runner.failure = lambda args: args[0] == "dnsmasq" and args[-1].endswith("dnsmasq.conf")
    elif failure_stage == "restart":
        runner.failure = lambda args: args[:2] == ["systemctl", "restart"]
    else:
        runner.failure = lambda args: args[:2] == ["systemctl", "is-active"]
    with pytest.raises(
        VmctlError, match="retained" if failure_stage == "temp-validation" else "restored"
    ):
        store.add(second)
    assert store.path.read_bytes() == before


def test_dnsmasq_restores_absence(config: Configuration, runner: FakeRunner) -> None:
    store = DnsmasqReservations(config, runner)
    runner.failure = lambda args: args[:2] == ["systemctl", "restart"]
    with pytest.raises(VmctlError, match="restored"):
        store.add(reservation())
    assert not store.path.exists()


def test_dnsmasq_reports_failed_restoration(config: Configuration, runner: FakeRunner) -> None:
    store = DnsmasqReservations(config, runner)
    runner.failure = lambda args: args[:2] == ["systemctl", "restart"]
    runner.fail_once = False
    with pytest.raises(VmctlError, match="restoration also failed"):
        store.add(reservation())


def test_missing_include_rejected(config: Configuration, runner: FakeRunner) -> None:
    config.path(config.host.network.dnsmasq_config).write_text("interface=vmbr1\n")
    with pytest.raises(VmctlError, match="must include"):
        DnsmasqReservations(config, runner).preflight()


def test_debian_service_conf_dirs_are_included_and_validated(
    config: Configuration, runner: FakeRunner
) -> None:
    root = config.path(config.host.network.dnsmasq_config)
    root.write_text("interface=vmbr1\n")
    directory = config.path(config.host.network.reservations).parent
    spec = f"{directory},.dpkg-dist,.dpkg-old,.dpkg-new"
    network = config.host.network.model_copy(update={"dnsmasq_conf_dirs": [spec]})
    config = config.model_copy(update={"host": config.host.model_copy(update={"network": network})})
    store = DnsmasqReservations(config, runner)
    store.preflight()
    store.add(reservation())
    full_tests = [call for call in runner.calls if f"--conf-file={root}" in call]
    assert len(full_tests) == 2
    assert all(f"--conf-dir={spec}" in call for call in full_tests)
    assert root.read_text() == "interface=vmbr1\n"


def test_extra_conf_dir_filters_still_reject_excluded_reservations(
    config: Configuration, runner: FakeRunner
) -> None:
    config.path(config.host.network.dnsmasq_config).write_text("interface=vmbr1\n")
    directory = config.path(config.host.network.reservations).parent
    network = config.host.network.model_copy(update={"dnsmasq_conf_dirs": [f"{directory},.conf"]})
    config = config.model_copy(update={"host": config.host.model_copy(update={"network": network})})
    with pytest.raises(VmctlError, match="dnsmasq_conf_dirs"):
        DnsmasqReservations(config, runner).preflight()


def test_atomic_validation_keeps_old_file(tmp_path: Path) -> None:
    path = tmp_path / "atomic"
    path.write_bytes(b"old")

    def reject(candidate: Path) -> None:
        assert candidate.read_bytes() == b"new"
        raise VmctlError("bad candidate")

    with pytest.raises(VmctlError):
        atomic_write(path, b"new", validate=reject)
    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_symlink_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("old")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(VmctlError, match="symlink"):
        atomic_write(link, b"new")
    assert target.read_text() == "old"


def test_lock_excludes_second_writer(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    with host_lock(path, 1):
        with pytest.raises(VmctlError, match="host lock"):
            with host_lock(path, 1):
                pytest.fail("Second lock unexpectedly acquired")


def test_candidate_rejection_does_not_restart_service(
    config: Configuration, runner: FakeRunner
) -> None:
    store = DnsmasqReservations(config, runner)
    runner.failure = lambda args: args[0] == "dnsmasq"
    with pytest.raises(VmctlError, match="candidate rejected"):
        store.add(reservation())
    assert not store.path.exists()
    assert not any(call[0] == "systemctl" for call in runner.calls)


def test_include_filters_do_not_follow_ignored_files(
    config: Configuration, runner: FakeRunner, tmp_path: Path
) -> None:
    root = config.path(config.host.network.dnsmasq_config)
    excluded = tmp_path / "excluded"
    excluded.mkdir()
    root.write_text(f"conf-dir={excluded},*.conf\n")
    (excluded / "ignored.backup").write_text(
        f"conf-dir={config.path(config.host.network.reservations).parent}\n"
    )
    with pytest.raises(VmctlError, match="must include"):
        DnsmasqReservations(config, runner).preflight()
