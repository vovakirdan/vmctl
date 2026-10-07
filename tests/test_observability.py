"""VM address and metric sources are isolated, read-only and tolerant of missing data."""

import io
import json
from collections.abc import Callable, Sequence
from ipaddress import IPv4Address
from pathlib import Path

import pytest
from conftest import FakeRunner

from vmctl.config import Configuration
from vmctl.errors import CommandError, VmctlError
from vmctl.local import LocalOperations
from vmctl.models import VM, IPObservation, VMDetails
from vmctl.network.allocator import AddressAllocator
from vmctl.network.discovery import (
    AddressSnapshot,
    agent_enabled,
    discover_addresses,
    lease_observations,
    neighbor_observations,
    network_bindings,
)
from vmctl.network.dnsmasq import DnsmasqReservations, Reservation, render_reservations
from vmctl.protocol import Event, Request, encode_request
from vmctl.proxmox.client import ProxmoxClient
from vmctl.services.metrics import parse_current, parse_history, vm_stats
from vmctl.services.queries import info_vm, list_vms
from vmctl.ssh import SSHOperations
from vmctl.utils.subprocess import CommandResult
from vmctl.worker import serve

MAC = "BC:24:11:AA:BB:CC"
VM_RECORD = VM(104, "existing", "running", "test-node", cpu=2, memory_mib=2048)


class ObservabilityRunner(FakeRunner):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.current: object = {
            "status": "running",
            "vmid": 104,
            "cpu": 0.125,
            "mem": 1024,
            "maxmem": 2048,
            "netin": 1000,
            "netout": 2000,
            "diskread": 3000,
            "diskwrite": 4000,
            "uptime": 60,
            "pid": 1234,
        }
        self.history: object = [{"time": 100, "cpu": 0.25, "netin": 42.5}]
        self.guest: object = {"result": []}
        self.neighbors: object = []
        self.cached_cpu: object = 0.125
        self.timeouts: list[int] = []
        self.vms[104] = {
            "name": "existing",
            "net0": f"virtio={MAC},bridge=vmbr1",
            "agent": "1",
            "memory": "2048",
            "cores": "2",
        }
        self.status[104] = "running"

    def inventory(self) -> list[dict[str, object]]:
        return [row | {"cpu": self.cached_cpu} for row in super().inventory()]

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: int = 60,
        sensitive: Sequence[str] = (),
        stream: Callable[[str], None] | None = None,
        check: bool = True,
    ) -> CommandResult:
        if args[0] == "pvesh" and args[2] == "/cluster/resources" and timeout == 5:
            self.timeouts.append(timeout)
        if args[0] == "ip":
            self.calls.append(tuple(args))
            return CommandResult(json.dumps(self.neighbors))
        if args[0] == "pvesh" and args[2].startswith("/nodes/test-node/qemu/"):
            self.calls.append(tuple(args))
            self.timeouts.append(timeout)
            if args[2].endswith("/status/current"):
                return CommandResult(json.dumps(self.current))
            if args[2].endswith("/rrddata"):
                if self.history is None:
                    raise CommandError("pvesh", "History is unavailable")
                return CommandResult(json.dumps(self.history))
            if args[2].endswith("/agent/network-get-interfaces"):
                if self.guest is None:
                    raise CommandError("pvesh", "Guest agent is unavailable")
                return CommandResult(json.dumps(self.guest))
        return super().run(args, timeout=timeout, sensitive=sensitive, stream=stream, check=check)


@pytest.fixture
def observed_runner(tmp_path: Path) -> ObservabilityRunner:
    return ObservabilityRunner(tmp_path / "storage")


def test_lease_validity_and_exact_mac_association(config: Configuration) -> None:
    text = f"""200 {MAC.lower()} 10.210.0.20 wrong-name *
0 {MAC} 10.210.0.21 existing *
100 {MAC} 10.210.0.22 existing *
99 {MAC} 10.210.0.23 existing *
-1 {MAC} 10.210.0.24 existing *
200 BC:24:11:11:22:33 10.210.0.25 existing *
200 {MAC} 192.168.1.20 existing *
200 {MAC} 10.210.0.1 existing *
200 bad 10.210.0.26 existing *
bad {MAC} 10.210.0.27 existing *
broken
"""
    result = lease_observations(text, config, now=100)
    assert result[MAC] == (IPv4Address("10.210.0.20"), IPv4Address("10.210.0.21"))
    assert result["BC:24:11:11:22:33"] == (IPv4Address("10.210.0.25"),)


def test_neighbor_bindings_exclude_foreign_bridge_and_invalid_entries(
    config: Configuration,
) -> None:
    rows = [
        {"dst": "10.210.0.20", "lladdr": MAC.lower(), "dev": "vmbr1", "state": ["STALE"]},
        {"dst": "10.210.0.21", "lladdr": MAC, "dev": "vmbr0", "state": ["REACHABLE"]},
        {"dst": "10.210.0.22", "lladdr": MAC, "state": ["FAILED"]},
        {"dst": "10.210.0.23", "lladdr": MAC, "state": ["INCOMPLETE"]},
        {"dst": "10.210.0.24", "lladdr": MAC, "state": [{}]},
        {"dst": "10.210.0.25", "state": ["REACHABLE"]},
        {"dst": "192.168.1.20", "lladdr": MAC, "state": ["REACHABLE"]},
        None,
    ]
    assert neighbor_observations(json.dumps(rows), config) == {MAC: (IPv4Address("10.210.0.20"),)}
    with pytest.raises(ValueError):
        neighbor_observations("{}", config)


def test_all_private_nics_are_matched_without_names_or_mac_guesses() -> None:
    assert network_bindings(
        {
            "net0": f"virtio={MAC},bridge=vmbr0",
            "net1": "e1000=BC:24:11:11:22:33,bridge=vmbr1",
            "net2": "virtio,bridge=vmbr1",
            "net3": "virtio=bad,bridge=vmbr1",
        },
        "vmbr1",
    ) == {"1": "BC:24:11:11:22:33"}
    assert agent_enabled({"agent": "enabled=1,fstrim_cloned_disks=1"})
    assert not agent_enabled({"agent": "0"})
    assert not agent_enabled({})


def test_address_source_priority_and_deduplication(
    config: Configuration, observed_runner: ObservabilityRunner
) -> None:
    observed_runner.guest = {
        "result": [
            {
                "hardware-address": MAC.lower(),
                "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "10.210.0.20"},
                    {"ip-address-type": "ipv4", "ip-address": "127.0.0.1"},
                    {"ip-address-type": "ipv6", "ip-address": "::1"},
                ],
            },
            {
                "hardware-address": "BC:24:11:11:22:33",
                "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "10.210.0.99"},
                ],
            },
        ]
    }
    snapshot = AddressSnapshot(
        {MAC: (IPv4Address("10.210.0.20"), IPv4Address("10.210.0.21"))},
        {MAC: (IPv4Address("10.210.0.21"), IPv4Address("10.210.0.23"))},
    )
    addresses, notes = discover_addresses(
        ProxmoxClient(config, observed_runner),
        VM_RECORD,
        observed_runner.vms[104] | {"ipconfig0": "ip=10.210.0.22/24,gw=10.210.0.1"},
        snapshot,
    )
    assert addresses == (
        IPObservation(IPv4Address("10.210.0.20"), "guest-agent"),
        IPObservation(IPv4Address("10.210.0.21"), "dhcp-lease"),
        IPObservation(IPv4Address("10.210.0.22"), "cloud-init"),
        IPObservation(IPv4Address("10.210.0.23"), "neighbor"),
    )
    assert not notes
    assert observed_runner.timeouts == [3]


@pytest.mark.parametrize("status,enabled", [("stopped", "1"), ("running", "0")])
def test_guest_queries_require_running_enabled_agent(
    config: Configuration, observed_runner: ObservabilityRunner, status: str, enabled: str
) -> None:
    vm = VM(104, "existing", status, "test-node")
    discover_addresses(
        ProxmoxClient(config, observed_runner),
        vm,
        observed_runner.vms[104] | {"agent": enabled},
        AddressSnapshot({}, {}),
    )
    assert not observed_runner.calls


def test_discovery_failure_keeps_inventory_and_reservation_authority(
    config: Configuration, observed_runner: ObservabilityRunner
) -> None:
    observed_runner.guest = None
    config.path(config.host.network.leases).write_text(f"0 {MAC} 10.210.0.20 existing *\n")
    reservations = DnsmasqReservations(config, observed_runner)
    original = render_reservations([Reservation(MAC, IPv4Address("10.210.0.100"), "existing", 104)])
    reservations.path.write_text(original)
    details = info_vm(ProxmoxClient(config, observed_runner), reservations, "existing")
    assert details.ip == details.display_ip == IPv4Address("10.210.0.100")
    assert details.ip_source == "reservation"
    assert details.addresses == (IPObservation(IPv4Address("10.210.0.20"), "dhcp-lease"),)
    assert "Guest-agent addresses are unavailable" in details.ip_notes[0]
    assert reservations.path.read_text() == original
    assert not any(call[0] in {"arping", "systemctl", "dnsmasq"} for call in observed_runner.calls)


def test_inventory_batches_neighbor_and_lease_reads(
    config: Configuration, observed_runner: ObservabilityRunner
) -> None:
    observed_runner.vms[105] = {
        "name": "second",
        "net1": "virtio=BC:24:11:11:22:33,bridge=vmbr1",
        "agent": "0",
    }
    config.path(config.host.network.leases).write_text(
        f"0 {MAC} 10.210.0.20 other-name *\n0 BC:24:11:11:22:33 10.210.0.21 second *\n"
    )
    details = list_vms(
        ProxmoxClient(config, observed_runner), DnsmasqReservations(config, observed_runner)
    )
    assert [item.display_ip for item in details] == [
        IPv4Address("10.210.0.20"),
        IPv4Address("10.210.0.21"),
    ]
    assert all(item.ip is None and item.ip_source == "dhcp-lease" for item in details)
    assert sum(call[0] == "ip" for call in observed_runner.calls) == 1


def test_redaction_preserves_observed_addresses_and_notes() -> None:
    details = VMDetails(
        VM_RECORD,
        {"cipassword": "private", "sshkeys": "key"},
        {},
        None,
        (IPObservation(IPv4Address("10.210.0.20"), "dhcp-lease"),),
        ("note",),
    )
    safe = LocalOperations.safe_details(details)
    assert safe.config == {"cipassword": "<redacted>", "sshkeys": "<redacted>"}
    assert safe.addresses == details.addresses and safe.ip_notes == details.ip_notes


def test_current_metrics_are_vm_scoped_and_counters_remain_cumulative() -> None:
    result = parse_current(
        VM_RECORD,
        {
            "status": "running",
            "vmid": 104,
            "cpu": 0.25,
            "mem": 1024,
            "maxmem": 2048,
            "netin": 3000,
            "netout": 4000,
            "diskread": 5000,
            "diskwrite": 6000,
            "uptime": 60,
            "pid": 1234,
        },
        timestamp=100,
    )
    assert result.vm.vmid == 104 and result.cpu_percent == 25
    assert result.network_in_bytes == 3000 and result.disk_write_bytes == 6000
    assert result.timestamp == 100 and result.uptime_seconds == 60 and result.pid == 1234


@pytest.mark.parametrize("invalid", [None, "5", True, -1, float("nan"), float("inf"), 1.5])
def test_bad_missing_values_stay_unavailable(invalid: object) -> None:
    result = parse_current(
        VM_RECORD,
        {
            "status": "running",
            "cpu": invalid,
            "mem": invalid,
            "maxmem": invalid,
            "netin": invalid,
            "netout": invalid,
            "diskread": invalid,
            "diskwrite": invalid,
            "uptime": invalid,
            "pid": invalid,
        },
        timestamp=100,
    )
    assert result.cpu_percent is None and result.memory_bytes is None
    assert result.network_in_bytes is None and result.disk_read_bytes is None
    assert result.uptime_seconds is None and result.pid is None


def test_missing_metrics_do_not_turn_into_zero() -> None:
    result = parse_current(VM_RECORD, {"status": "running"}, timestamp=100)
    assert result.cpu_percent is None and result.memory_bytes is None
    assert result.network_in_bytes is None and result.history == ()
    zeros = parse_current(
        VM_RECORD, {"status": "running", "cpu": 0, "mem": 0, "netin": 0}, timestamp=100
    )
    assert zeros.cpu_percent == zeros.memory_bytes == zeros.network_in_bytes == 0


def test_stopped_vm_updates_inventory_status_and_has_no_current_activity() -> None:
    result = parse_current(
        VM_RECORD,
        {
            "status": "stopped",
            "cpu": 0.5,
            "mem": 500,
            "netin": 5000,
            "maxmem": 2048,
        },
        timestamp=100,
    )
    assert result.vm.status == "stopped" and result.memory_total_bytes == 2048
    assert (
        result.cpu_percent is None
        and result.memory_bytes is None
        and result.network_in_bytes is None
    )


@pytest.mark.parametrize("data", [[], {}, {"status": None}, {"status": "running", "vmid": 105}])
def test_invalid_status_and_other_vm_metrics_fail_clearly(data: object) -> None:
    with pytest.raises(VmctlError):
        parse_current(VM_RECORD, data, timestamp=100)


def test_history_rates_are_not_differentiated_and_unknown_samples_are_gaps() -> None:
    result = parse_history(
        [
            {
                "time": 200,
                "cpu": 0.25,
                "mem": 123.5,
                "maxmem": 2048,
                "netin": 300.5,
                "diskwrite": 42.5,
            },
            {"time": 100, "cpu": None, "mem": -1, "netin": float("nan"), "diskread": "5"},
            {"time": "50", "cpu": 0.5},
            None,
        ]
    )
    assert [point.timestamp for point in result] == [100, 200]
    assert result[0].cpu_percent is None and result[0].network_in_bytes_per_second is None
    assert result[1].cpu_percent == 25 and result[1].network_in_bytes_per_second == 300.5
    assert result[1].disk_write_bytes_per_second == 42.5 and result[1].memory_bytes == 123.5
    with pytest.raises(VmctlError):
        parse_history({})


def test_stats_commands_are_bounded_local_and_read_only(
    config: Configuration, observed_runner: ObservabilityRunner
) -> None:
    result = vm_stats(ProxmoxClient(config, observed_runner), "existing", history=True)
    assert result.vm.vmid == 104 and result.cpu_percent == 12.5
    assert result.history[0].network_in_bytes_per_second == 42.5
    assert "minute averages" in result.history_note
    assert observed_runner.timeouts == [5, 5, 5]
    assert all(call[:2] == ("pvesh", "get") for call in observed_runner.calls)
    assert "--timeframe" in observed_runner.calls[-1] and "AVERAGE" in observed_runner.calls[-1]
    with pytest.raises(VmctlError, match="No local VM"):
        vm_stats(ProxmoxClient(config, observed_runner), "missing")


def test_current_cpu_placeholder_is_replaced_by_pvestatd_sample(
    config: Configuration, observed_runner: ObservabilityRunner
) -> None:
    observed_runner.current = {
        "status": "running",
        "vmid": 104,
        "cpu": 0,
        "mem": 1024,
        "maxmem": 2048,
        "netin": 5000,
    }
    observed_runner.cached_cpu = 0.075
    result = vm_stats(ProxmoxClient(config, observed_runner), "104")
    assert result.cpu_percent == 7.5
    assert result.memory_bytes == 1024 and result.network_in_bytes == 5000


@pytest.mark.parametrize("cached", [None, "0.075", True, -1, 1.5, float("nan"), float("inf")])
def test_invalid_cached_cpu_never_falls_back_to_current_placeholder(
    config: Configuration, observed_runner: ObservabilityRunner, cached: object
) -> None:
    observed_runner.cached_cpu = cached
    result = vm_stats(ProxmoxClient(config, observed_runner), "104")
    assert result.cpu_percent is None
    assert result.memory_bytes == 1024 and result.network_in_bytes == 1000


@pytest.mark.parametrize(
    "change",
    [
        {"node": "foreign-node"},
        {"vmid": 105},
        {"vmid": "104"},
        {"type": "lxc"},
        {"status": "stopped"},
    ],
)
def test_cached_cpu_requires_exact_local_running_qemu_identity(
    config: Configuration,
    observed_runner: ObservabilityRunner,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, object],
) -> None:
    row: dict[str, object] = {
        "vmid": 104,
        "node": "test-node",
        "type": "qemu",
        "status": "running",
        "cpu": 0.075,
    }
    monkeypatch.setattr(observed_runner, "inventory", lambda: [row | change])
    assert ProxmoxClient(config, observed_runner).cached_cpu(104) is None
    assert observed_runner.timeouts == [5]


def test_cached_cpu_failure_preserves_other_current_metrics(
    config: Configuration, observed_runner: ObservabilityRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = ProxmoxClient(config, observed_runner)

    def unavailable(vmid: int) -> object:
        raise CommandError("pvesh", "Cached CPU unavailable")

    monkeypatch.setattr(client, "cached_cpu", unavailable)
    result = vm_stats(client, "104")
    assert result.cpu_percent is None and result.memory_bytes == 1024
    assert result.disk_write_bytes == 4000


def test_history_failure_retains_current_metrics(
    config: Configuration, observed_runner: ObservabilityRunner
) -> None:
    observed_runner.history = None
    result = vm_stats(ProxmoxClient(config, observed_runner), "104", history=True)
    assert result.cpu_percent == 12.5 and result.history == ()
    assert "unavailable" in result.history_note


def test_unrecognized_history_keeps_current_and_explains_missing_graph_data(
    config: Configuration, observed_runner: ObservabilityRunner
) -> None:
    observed_runner.history = [{"time": 100, "future_cpu_metric": 0.5}]
    result = vm_stats(ProxmoxClient(config, observed_runner), "104", history=True)
    assert result.cpu_percent == 12.5 and result.history == ()
    assert "no supported metric values" in result.history_note


def test_metric_timestamp_is_captured_after_read(
    config: Configuration, observed_runner: ObservabilityRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = ProxmoxClient(config, observed_runner)
    finished = False

    def read(vmid: int) -> object:
        nonlocal finished
        assert vmid == 104
        finished = True
        return observed_runner.current

    def now() -> float:
        assert finished
        return 123.5

    monkeypatch.setattr(client, "current_metrics", read)
    monkeypatch.setattr("vmctl.services.metrics.time.time", now)
    assert vm_stats(client, "104").timestamp == 123.5


def test_worker_frontend_roundtrip_preserves_observations_and_metrics(
    config: Configuration, observed_runner: ObservabilityRunner
) -> None:
    config.path(config.host.network.leases).write_text(f"0 {MAC} 10.210.0.20 existing *\n")
    backend = LocalOperations(
        config.root,
        factory=lambda cfg: (
            ProxmoxClient(cfg, observed_runner),
            DnsmasqReservations(cfg, observed_runner),
            AddressAllocator(cfg, observed_runner),
        ),
    )

    class InProcessTransport:
        def exchange(
            self, request: Request, progress: Callable[[str], None] | None = None
        ) -> Event:
            output = io.StringIO()
            assert serve(encode_request(request).decode(), output, lambda: backend) == 0
            return Event.model_validate_json(output.getvalue().splitlines()[-1])

    frontend = SSHOperations(InProcessTransport())
    details = frontend.info("104")
    assert details.display_ip == IPv4Address("10.210.0.20") and details.ip_source == "dhcp-lease"
    metrics = frontend.stats("104", history=True)
    assert metrics.vm.vmid == 104 and metrics.cpu_percent == 12.5 and len(metrics.history) == 1
