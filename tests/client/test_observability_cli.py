"""Read-only CLI presentation of observed addresses and per-VM statistics."""

from ipaddress import IPv4Address

import pytest
from typer.testing import CliRunner

from vmctl import cli
from vmctl.models import VM, IPObservation, VMDetails, VMStats


class ObservabilityBackend:
    def __init__(self, *, stopped: bool = False) -> None:
        self.vm = VM(200, "existing-vm", "stopped" if stopped else "running", "pve")
        self.calls: list[str] = []

    def list(self) -> list[VMDetails]:
        return [self.info("200")]

    def info(self, reference: str) -> VMDetails:
        self.calls.append(reference)
        return VMDetails(
            self.vm,
            {"cores": "2", "memory": "2048", "cipassword": "secret-password"},
            {},
            None,
            addresses=(IPObservation(IPv4Address("10.210.0.20"), "dhcp-lease"),),
        )

    def stats(self, reference: str, *, history: bool = False) -> VMStats:
        self.calls.append(reference)
        assert history is False
        if self.vm.status == "stopped":
            return VMStats(self.vm, timestamp=100)
        return VMStats(
            self.vm,
            timestamp=100,
            cpu_percent=17.5,
            memory_bytes=1024,
            memory_total_bytes=2048,
            network_in_bytes=5000,
            network_out_bytes=0,
            disk_read_bytes=6000,
            disk_write_bytes=7000,
            uptime_seconds=80,
        )


def test_cli_list_info_show_observed_ip_without_claiming_a_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = ObservabilityBackend()
    monkeypatch.setattr(cli, "operations", lambda _: backend)
    for args in (["list"], ["info", "200"]):
        result = CliRunner().invoke(cli.app, args)
        assert result.exit_code == 0, result.output
        assert "10.210.0.20" in result.output
        assert "dhcp-lease" in result.output
        assert "secret-password" not in result.output
    assert "Managed IP" in result.output and "unknown" in result.output


@pytest.mark.parametrize("stopped", [False, True])
def test_cli_stats_totals_and_unavailable_values(
    monkeypatch: pytest.MonkeyPatch, stopped: bool
) -> None:
    backend = ObservabilityBackend(stopped=stopped)
    monkeypatch.setattr(cli, "operations", lambda _: backend)
    result = CliRunner().invoke(cli.app, ["stats", "existing-vm"])
    assert result.exit_code == 0, result.output
    assert backend.calls == ["existing-vm"]
    assert "host-reported" in result.output and "(total)" in result.output
    assert "bytes/s" not in result.output
    if stopped:
        assert "stopped" in result.output and "unavailable" in result.output
        assert "0.0%" not in result.output
    else:
        assert "17.5%" in result.output and "5,000 bytes" in result.output
        assert "0 bytes" in result.output and "80s" in result.output
