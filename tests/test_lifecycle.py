"""VM lifecycle guards at a mocked Proxmox command boundary."""

import io
import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FakeRunner

from vmctl.config import Configuration
from vmctl.errors import CommandError, UncertainOperationError, VmctlError
from vmctl.local import LocalOperations
from vmctl.network.allocator import AddressAllocator
from vmctl.network.dnsmasq import DnsmasqReservations
from vmctl.operations import LifecycleAction, Progress
from vmctl.protocol import ActionMessage, ActionParameters, Event, Request, encode_request
from vmctl.proxmox.client import ProxmoxClient
from vmctl.services.lifecycle import LifecycleService
from vmctl.ssh import SSHOperations
from vmctl.utils.subprocess import CommandResult
from vmctl.worker import serve


class LifecycleRunner(FakeRunner):
    outer_timeout: bool = False

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: int = 60,
        sensitive: Sequence[str] = (),
        stream: Callable[[str], None] | None = None,
        check: bool = True,
    ) -> CommandResult:
        if args[:2] in (["qm", "status"], ["qm", "shutdown"], ["qm", "reboot"]):
            self.calls.append(tuple(args))
            if self.failure and self.failure(args):
                raise CommandError(
                    "qm", "Injected graceful shutdown timeout", timed_out=self.outer_timeout
                )
            vmid = int(args[2])
            if args[1] == "status":
                return CommandResult("status: " + self.status[vmid] + "\n")
            self.status[vmid] = "stopped" if args[1] == "shutdown" else "running"
            return CommandResult("")
        return super().run(args, timeout=timeout, sensitive=sensitive, stream=stream, check=check)


@pytest.fixture
def lifecycle_runner(runner: FakeRunner) -> LifecycleRunner:
    boundary = LifecycleRunner(runner.root)
    boundary.vms[104] = {"name": "work", "cores": "2", "memory": "2048"}
    boundary.status[104] = "stopped"
    return boundary


@pytest.fixture
def service(config: Configuration, lifecycle_runner: LifecycleRunner) -> LifecycleService:
    return LifecycleService(config, ProxmoxClient(config, lifecycle_runner))


@pytest.mark.parametrize("action", ["start", "shutdown", "reboot"])
def test_guarded_lifecycle_actions(
    service: LifecycleService, lifecycle_runner: LifecycleRunner, action: LifecycleAction
) -> None:
    lifecycle_runner.status[104] = "stopped" if action == "start" else "running"
    plan = service.plan("work", action)
    assert not any(call[:2] == ("qm", action) for call in lifecycle_runner.calls)
    result = service.action(plan)
    assert result.vm.vmid == 104 and result.action == action
    assert result.vm.status == ("stopped" if action == "shutdown" else "running")
    assert len([call for call in lifecycle_runner.calls if call[:2] == ("qm", action)]) == 1
    assert not any(call[:2] in {("qm", "stop"), ("qm", "reset")} for call in lifecycle_runner.calls)


@pytest.mark.parametrize(
    ("action", "status"),
    [("start", "running"), ("shutdown", "stopped"), ("reboot", "stopped"), ("shutdown", "paused")],
)
def test_status_mismatch_is_rejected(
    service: LifecycleService,
    lifecycle_runner: LifecycleRunner,
    action: LifecycleAction,
    status: str,
) -> None:
    lifecycle_runner.status[104] = status
    with pytest.raises(VmctlError, match="Cannot .* expected"):
        service.plan("104", action)
    assert not any(call[:2] == ("qm", action) for call in lifecycle_runner.calls)


@pytest.mark.parametrize("change", ["name", "cores", "status", "lock", "action"])
def test_confirmation_is_rechecked(
    service: LifecycleService, lifecycle_runner: LifecycleRunner, change: str
) -> None:
    lifecycle_runner.status[104] = "running"
    plan = service.plan("104", "shutdown")
    if change == "status":
        lifecycle_runner.status[104] = "stopped"
    elif change == "action":
        plan = replace(plan, action="reboot")
    else:
        lifecycle_runner.vms[104][change] = "16" if change == "cores" else "changed"
    with pytest.raises(VmctlError):
        service.action(plan)
    assert not any(
        call[:2] in {("qm", "shutdown"), ("qm", "reboot")} for call in lifecycle_runner.calls
    )


@pytest.mark.parametrize("vmid", [9000, 9005, 104])
def test_templates_and_reserved_vmids_are_protected(
    service: LifecycleService, lifecycle_runner: LifecycleRunner, vmid: int
) -> None:
    lifecycle_runner.vms[vmid] = {"name": "protected"}
    lifecycle_runner.status[vmid] = "stopped"
    if vmid == 104:
        lifecycle_runner.vms[vmid]["template"] = "1"
    with pytest.raises(VmctlError, match="Templates and VMIDs"):
        service.plan(str(vmid), "start")
    assert not any(call[:2] == ("qm", "start") for call in lifecycle_runner.calls)


def test_graceful_timeout_never_falls_back_to_stop(
    service: LifecycleService, lifecycle_runner: LifecycleRunner
) -> None:
    lifecycle_runner.status[104] = "running"
    plan = service.plan("104", "shutdown")
    lifecycle_runner.failure = lambda args: args[:2] == ["qm", "shutdown"]
    with pytest.raises(CommandError, match="timeout"):
        service.action(plan)
    assert lifecycle_runner.status[104] == "running"
    assert not any(call[:2] == ("qm", "stop") for call in lifecycle_runner.calls)


def test_outer_timeout_has_an_uncertain_local_outcome(
    service: LifecycleService, lifecycle_runner: LifecycleRunner
) -> None:
    lifecycle_runner.status[104] = "running"
    plan = service.plan("104", "reboot")
    lifecycle_runner.failure = lambda args: args[:2] == ["qm", "reboot"]
    lifecycle_runner.outer_timeout = True
    with pytest.raises(UncertainOperationError) as failure:
        service.action(plan)
    assert failure.value.vmid == 104
    assert "no automatic retry" in str(failure.value)
    assert not any(call[:2] == ("qm", "stop") for call in lifecycle_runner.calls)


def test_direct_status_replaces_stale_cluster_inventory(
    service: LifecycleService, lifecycle_runner: LifecycleRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_inventory = lifecycle_runner.inventory

    def stale_inventory() -> list[dict[str, object]]:
        return [entry | {"status": "running"} for entry in original_inventory()]

    monkeypatch.setattr(lifecycle_runner, "inventory", stale_inventory)
    assert service.plan("104", "start").vm.status == "stopped"


def test_recheck_and_submission_share_the_host_lock(
    service: LifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vmctl.services.lifecycle as lifecycle

    plan = service.plan("104", "start")
    held = False
    observations: list[bool] = []
    original_status = service.client.status

    @contextmanager
    def locked(path: Path, timeout: int) -> Iterator[None]:
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    def status(vmid: int) -> str:
        observations.append(held)
        return original_status(vmid)

    monkeypatch.setattr(lifecycle, "host_lock", locked)
    monkeypatch.setattr(service.client, "status", status)
    service.action(plan)
    assert observations == [True, True] and not held


def test_completed_action_is_not_retried_when_status_read_fails(
    service: LifecycleService, lifecycle_runner: LifecycleRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = service.plan("104", "start")
    original_status = service.client.status
    reads = 0

    def status(vmid: int) -> str:
        nonlocal reads
        reads += 1
        if reads == 2:
            raise VmctlError("Injected status read failure")
        return original_status(vmid)

    monkeypatch.setattr(service.client, "status", status)
    with pytest.raises(VmctlError, match="completed .*inspect it before retrying"):
        service.action(plan)
    assert len([call for call in lifecycle_runner.calls if call[:2] == ("qm", "start")]) == 1


def test_reboot_result_can_report_restart_in_progress(
    service: LifecycleService, lifecycle_runner: LifecycleRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle_runner.status[104] = "running"
    plan = service.plan("104", "reboot")
    original_reboot = service.client.reboot

    def reboot(vmid: int) -> None:
        original_reboot(vmid)
        lifecycle_runner.status[vmid] = "stopped"

    monkeypatch.setattr(service.client, "reboot", reboot)
    assert service.action(plan).vm.status == "stopped"


def test_lifecycle_commands_use_bounded_configurable_timeout(config: Configuration) -> None:
    calls: list[tuple[tuple[str, ...], int]] = []

    class RecordingRunner:
        def run(
            self,
            args: Sequence[str],
            *,
            timeout: int = 60,
            sensitive: Sequence[str] = (),
            stream: Callable[[str], None] | None = None,
            check: bool = True,
        ) -> CommandResult:
            calls.append((tuple(args), timeout))
            return CommandResult("")

    configured = config.model_copy(
        update={
            "host": config.host.model_copy(
                update={"proxmox": config.host.proxmox.model_copy(update={"stop_timeout": 45})}
            )
        }
    )
    client = ProxmoxClient(configured, RecordingRunner())
    client.shutdown(104)
    client.reboot(104)
    assert calls == [
        (("qm", "shutdown", "104", "--timeout", "45", "--forceStop", "0"), 105),
        (("qm", "reboot", "104", "--timeout", "45"), 105),
    ]


def test_lifecycle_wire_roundtrip(config: Configuration, lifecycle_runner: LifecycleRunner) -> None:
    backend = LocalOperations(
        config.root,
        factory=lambda cfg: (
            ProxmoxClient(cfg, lifecycle_runner),
            DnsmasqReservations(cfg, lifecycle_runner),
            AddressAllocator(cfg, lifecycle_runner),
        ),
    )

    class InProcessTransport:
        def exchange(self, request: Request, progress: Progress | None = None) -> Event:
            output = io.StringIO()
            assert serve(encode_request(request).decode(), output, lambda: backend) == 0
            return Event.model_validate_json(output.getvalue().splitlines()[-1])

    frontend = SSHOperations(InProcessTransport())
    for action in ("start", "reboot", "shutdown"):
        plan = frontend.plan_action("104", action)
        result = frontend.action(plan)
        assert result.vm.vmid == 104 and result.action == action
    assert lifecycle_runner.status[104] == "stopped"


@pytest.mark.parametrize("outer_timeout", [False, True])
def test_worker_distinguishes_native_failure_from_outer_timeout(
    config: Configuration, lifecycle_runner: LifecycleRunner, outer_timeout: bool
) -> None:
    backend = LocalOperations(
        config.root,
        factory=lambda cfg: (
            ProxmoxClient(cfg, lifecycle_runner),
            DnsmasqReservations(cfg, lifecycle_runner),
            AddressAllocator(cfg, lifecycle_runner),
        ),
    )
    lifecycle_runner.status[104] = "running"
    plan = backend.plan_action("104", "shutdown")
    lifecycle_runner.failure = lambda args: args[:2] == ["qm", "shutdown"]
    lifecycle_runner.outer_timeout = outer_timeout
    message = ActionMessage(
        request_id="a" * 32, operation="shutdown", parameters=ActionParameters.from_preview(plan)
    )
    output = io.StringIO()
    assert serve(encode_request(message).decode(), output, lambda: backend) == 1
    event = Event.model_validate_json(output.getvalue().splitlines()[-1])
    assert event.code == ("unknown_outcome" if outer_timeout else "operation_failed")
    assert event.vmid == (104 if outer_timeout else None)


@pytest.mark.parametrize("action", ["stop", "destroy", "reset"])
def test_unallowlisted_power_operations_rejected_before_host(action: str) -> None:
    output = io.StringIO()
    raw = json.dumps(
        {
            "protocol_version": 1,
            "request_id": "a" * 32,
            "operation": action,
            "parameters": {},
        }
    )
    assert serve(raw, output, lambda: pytest.fail("Must not create backend")) == 1
    assert "invalid_request" in output.getvalue()
