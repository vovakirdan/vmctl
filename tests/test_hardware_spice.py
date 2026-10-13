"""Resource and SPICE plans, compensation and guest execution use fake commands only."""

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FakeRunner

from vmctl.config import Configuration, load_spice_settings
from vmctl.errors import CommandError, UncertainOperationError, VmctlError
from vmctl.models import HardwareRequest, SpiceRequest
from vmctl.proxmox.client import ProxmoxClient
from vmctl.services.hardware import HardwareService
from vmctl.services.spice import SpiceService, spice_display
from vmctl.utils.subprocess import CommandResult


class HardwareRunner(FakeRunner):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.vms[104] = {
            "name": "work",
            "cores": "2",
            "sockets": "2",
            "memory": "4096",
            "vga": "std",
            "agent": "enabled=1",
            "net0": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr1",
            "scsi0": "local-lvm:vm-104-disk-0,size=40G",
        }
        self.status[104] = "stopped"
        self.pending_data: object = []
        self.os_data: object = {"result": {"id": "ubuntu"}}
        self.poll_data: object = {"exited": True, "exitcode": 0}
        self.guest_count = 0

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: int = 60,
        sensitive: Sequence[str] = (),
        stream: Callable[[str], None] | None = None,
        check: bool = True,
    ) -> CommandResult:
        if args[0] == "pvesh" and args[2].endswith(("/pending", "/agent/get-osinfo")):
            self.calls.append(tuple(args))
            return CommandResult(
                json.dumps(self.pending_data if args[2].endswith("/pending") else self.os_data)
            )
        if args[:3] == ["qm", "guest", "exec"]:
            self.calls.append(tuple(args))
            self.guest_count += 1
            return CommandResult(json.dumps({"pid": self.guest_count + 100}))
        if args[:3] == ["qm", "guest", "exec-status"]:
            self.calls.append(tuple(args))
            return CommandResult(json.dumps(self.poll_data))
        return super().run(args, timeout=timeout, sensitive=sensitive, stream=stream, check=check)


@pytest.fixture
def boundary(runner: FakeRunner) -> HardwareRunner:
    return HardwareRunner(runner.root)


@pytest.fixture
def hardware(config: Configuration, boundary: HardwareRunner) -> HardwareService:
    return HardwareService(config, ProxmoxClient(config, boundary))


@pytest.fixture
def spice(config: Configuration, boundary: HardwareRunner) -> SpiceService:
    return SpiceService(config, ProxmoxClient(config, boundary))


def mutations(boundary: HardwareRunner) -> list[tuple[str, ...]]:
    return [
        call
        for call in boundary.calls
        if call[:2]
        in {("qm", "set"), ("qm", "start"), ("qm", "shutdown"), ("qm", "stop"), ("qm", "reboot")}
        or call[:3] == ("qm", "guest", "exec")
    ]


def test_cpu_total_preserves_sockets_and_other_fields(
    hardware: HardwareService, boundary: HardwareRunner
) -> None:
    original = dict(boundary.vms[104])
    plan = hardware.plan(HardwareRequest("work", cpu=8))
    assert plan.cpu == 4 and plan.new_cpu == 8 and not mutations(boundary)
    result = hardware.update(plan, progress=lambda _: None)
    assert result.cpu == 8 and result.vm.cpu == 8 and not result.restarted
    assert boundary.vms[104] == original | {"cores": "4"}
    assert mutations(boundary) == [("qm", "set", "104", "--cores", "4")]


def test_memory_override_and_restart_restores_power(
    hardware: HardwareService, boundary: HardwareRunner
) -> None:
    boundary.status[104] = "running"
    plan = hardware.plan(HardwareRequest("104", memory="8G", restart=True))
    result = hardware.update(plan, progress=lambda _: None)
    assert result.memory_mib == 8192 and result.restarted and result.vm.status == "running"
    calls = mutations(boundary)
    assert calls[0][1] == "shutdown" and "--forceStop" in calls[0] and calls[0][-1] == "0"
    assert calls[1] == ("qm", "set", "104", "--memory", "8192")
    assert calls[2] == ("qm", "start", "104")


@pytest.mark.parametrize(
    "requested",
    [
        HardwareRequest("104"),
        HardwareRequest("104", cpu=0),
        HardwareRequest("104", cpu=513),
        HardwareRequest("104", cpu=3),
        HardwareRequest("104", memory="0G"),
        HardwareRequest("104", memory="1; touch /tmp/no"),
    ],
)
def test_invalid_resources_never_mutate(
    hardware: HardwareService, boundary: HardwareRunner, requested: HardwareRequest
) -> None:
    with pytest.raises(VmctlError):
        hardware.plan(requested)
    assert not mutations(boundary)


@pytest.mark.parametrize(
    "setting", [{"numa": "1"}, {"numa0": "cpus=0-3"}, {"vcpus": "4"}, {"balloon": "16384"}]
)
def test_unsupported_topology_is_rejected(
    hardware: HardwareService, boundary: HardwareRunner, setting: dict[str, str]
) -> None:
    boundary.vms[104].update(setting)
    with pytest.raises(VmctlError):
        hardware.plan(HardwareRequest("104", cpu=8, memory="8G"))
    assert not mutations(boundary)


def test_running_edit_requires_explicit_restart(
    hardware: HardwareService, boundary: HardwareRunner
) -> None:
    boundary.status[104] = "running"
    with pytest.raises(VmctlError, match="--restart"):
        hardware.plan(HardwareRequest("104", cpu=8))
    assert not mutations(boundary)


def test_noop_does_not_restart_running_vm(
    hardware: HardwareService, boundary: HardwareRunner
) -> None:
    boundary.status[104] = "running"
    result = hardware.update(
        hardware.plan(HardwareRequest("104", cpu=4, memory="4G")), progress=lambda _: None
    )
    assert not result.restarted and not mutations(boundary)


@pytest.mark.parametrize("change", ["cores", "name", "status", "request"])
def test_preview_binds_configuration_power_and_request(
    hardware: HardwareService, boundary: HardwareRunner, change: str
) -> None:
    plan = hardware.plan(HardwareRequest("104", cpu=8))
    if change == "status":
        boundary.status[104] = "running"
    elif change == "request":
        plan = replace(plan, request=replace(plan.request, cpu=16))
    else:
        boundary.vms[104][change] = "3" if change == "cores" else "renamed"
    with pytest.raises(VmctlError):
        hardware.update(plan, progress=lambda _: None)
    assert not mutations(boundary)


@pytest.mark.parametrize("service_name", ["hardware", "spice"])
@pytest.mark.parametrize(
    "guard", ["template", "reserved", "lock", "pending", "invalid-pending", "paused"]
)
def test_edit_guards(
    config: Configuration, boundary: HardwareRunner, service_name: str, guard: str
) -> None:
    reference = "104"
    if guard == "reserved":
        boundary.vms[9005] = dict(boundary.vms[104])
        boundary.status[9005] = "stopped"
        reference = "9005"
    elif guard == "pending":
        boundary.pending_data = [{"key": "memory", "value": "4096", "pending": "8192"}]
    elif guard == "invalid-pending":
        boundary.pending_data = {}
    elif guard == "paused":
        boundary.status[104] = "paused"
    else:
        boundary.vms[104][guard] = "1"
    client = ProxmoxClient(config, boundary)
    with pytest.raises(VmctlError):
        if service_name == "hardware":
            HardwareService(config, client).plan(HardwareRequest(reference, cpu=8))
        else:
            SpiceService(config, client).plan(SpiceRequest(reference))
    assert not mutations(boundary)


def test_definite_config_failure_restores_original_running_state(
    hardware: HardwareService, boundary: HardwareRunner
) -> None:
    boundary.status[104] = "running"
    original = dict(boundary.vms[104])
    plan = hardware.plan(HardwareRequest("104", memory="8G", restart=True))
    boundary.failure = lambda args: args[:2] == ["qm", "set"]
    with pytest.raises(VmctlError, match="Original configuration and power state restored"):
        hardware.update(plan, progress=lambda _: None)
    assert boundary.vms[104] == original and boundary.status[104] == "running"
    assert not any(call[1] == "stop" for call in mutations(boundary))


def test_partial_config_failure_rolls_back_only_known_fields(
    hardware: HardwareService, boundary: HardwareRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = dict(boundary.vms[104])
    plan = hardware.plan(HardwareRequest("104", cpu=8, memory="8G"))
    configure = hardware.client.configure
    called = False

    def partial(vmid: int, options: dict[str, str], *, delete: tuple[str, ...] = ()) -> None:
        nonlocal called
        if not called:
            called = True
            configure(vmid, {"cores": options["cores"]})
            raise CommandError("qm", "Failed after setting cores")
        configure(vmid, options, delete=delete)

    monkeypatch.setattr(hardware.client, "configure", partial)
    with pytest.raises(VmctlError, match="restored"):
        hardware.update(plan, progress=lambda _: None)
    assert boundary.vms[104] == original
    assert mutations(boundary)[-1] == ("qm", "set", "104", "--cores", "2")


@pytest.mark.parametrize("stage", ["shutdown", "configure", "start"])
def test_command_timeout_never_compensates_or_retries(
    hardware: HardwareService, boundary: HardwareRunner, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    boundary.status[104] = "running"
    plan = hardware.plan(HardwareRequest("104", memory="8G", restart=True))
    method = getattr(hardware.client, stage)

    def timeout(*args: object, **kwargs: object) -> None:
        method(*args, **kwargs)
        raise CommandError("qm", "Timed out", timed_out=True)

    monkeypatch.setattr(hardware.client, stage, timeout)
    with pytest.raises(UncertainOperationError):
        hardware.update(plan, progress=lambda _: None)
    assert len([call for call in mutations(boundary) if call[1] == "set"]) <= 1
    assert len([call for call in mutations(boundary) if call[1] == "start"]) <= 1
    assert not any(call[1] in {"stop", "reboot"} for call in mutations(boundary))


def test_unknown_post_set_read_never_rolls_back(
    hardware: HardwareService, boundary: HardwareRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = hardware.plan(HardwareRequest("104", memory="8G"))
    vm_config = hardware.client.vm_config

    def unreadable(vmid: int) -> dict[str, str]:
        if any(call[:2] == ("qm", "set") for call in boundary.calls):
            raise VmctlError("Config unreadable")
        return vm_config(vmid)

    monkeypatch.setattr(hardware.client, "vm_config", unreadable)
    with pytest.raises(VmctlError, match="no rollback or power action"):
        hardware.update(plan, progress=lambda _: None)
    assert mutations(boundary) == [("qm", "set", "104", "--memory", "8192")]


def test_recheck_and_mutations_share_host_lock(
    hardware: HardwareService, boundary: HardwareRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vmctl.services.hardware as module

    plan = hardware.plan(HardwareRequest("104", cpu=8))
    held = False
    observed: list[bool] = []
    run = boundary.run

    @contextmanager
    def locked(path: Path, timeout: int) -> Iterator[None]:
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    def checked_run(args: Sequence[str], **kwargs: object) -> CommandResult:
        observed.append(held)
        return run(args, **kwargs)

    monkeypatch.setattr(module, "host_lock", locked)
    monkeypatch.setattr(boundary, "run", checked_run)
    hardware.update(plan, progress=lambda _: None)
    assert observed and all(observed) and not held


def test_external_change_during_shutdown_prevents_set_and_restart(
    hardware: HardwareService, boundary: HardwareRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary.status[104] = "running"
    plan = hardware.plan(HardwareRequest("104", cpu=8, restart=True))
    shutdown = hardware.client.shutdown

    def changed(vmid: int) -> None:
        shutdown(vmid)
        boundary.vms[vmid]["name"] = "externally-renamed"

    monkeypatch.setattr(hardware.client, "shutdown", changed)
    with pytest.raises(VmctlError, match="changed while stopping"):
        hardware.update(plan, progress=lambda _: None)
    assert [call[1] for call in mutations(boundary)] == ["shutdown"]


def test_unknown_fields_are_never_overwritten_during_rollback(
    hardware: HardwareService, boundary: HardwareRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = hardware.plan(HardwareRequest("104", cpu=8))
    configure = hardware.client.configure

    def changed(vmid: int, options: dict[str, str], *, delete: tuple[str, ...] = ()) -> None:
        configure(vmid, options, delete=delete)
        boundary.vms[vmid]["cores"] = "3"
        raise CommandError("qm", "Failed with a conflicting change")

    monkeypatch.setattr(hardware.client, "configure", changed)
    with pytest.raises(VmctlError, match="rollback was not safe"):
        hardware.update(plan, progress=lambda _: None)
    assert boundary.vms[104]["cores"] == "3"
    assert len(mutations(boundary)) == 1


def test_spice_failure_restores_absent_display_setting(
    spice: SpiceService, boundary: HardwareRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary.vms[104].pop("vga")
    plan = spice.plan(SpiceRequest("104", guest_tools="skip"))
    configure = spice.client.configure
    called = False

    def partially_set(vmid: int, options: dict[str, str], *, delete: tuple[str, ...] = ()) -> None:
        nonlocal called
        configure(vmid, options, delete=delete)
        if not called:
            called = True
            raise CommandError("qm", "Failed after configuration")

    monkeypatch.setattr(spice.client, "configure", partially_set)
    with pytest.raises(VmctlError, match="restored"):
        spice.apply(plan, progress=lambda _: None)
    assert "vga" not in boundary.vms[104]
    assert mutations(boundary)[-1] == ("qm", "set", "104", "--delete", "vga")


@pytest.mark.parametrize(
    "value",
    ["qxl", "qxl2", "qxl3", "qxl4", "virtio", "virtio-gl", "virtio2", "type=virtio,memory=128"],
)
def test_existing_spice_display_preserved(
    spice: SpiceService, boundary: HardwareRunner, value: str
) -> None:
    boundary.vms[104]["vga"] = value
    boundary.status[104] = "running"
    plan = spice.plan(SpiceRequest("104", guest_tools="skip"))
    result = spice.apply(plan, progress=lambda _: None)
    assert spice_display(value) and result.vga == value and not mutations(boundary)


def test_qxl_fallback_and_restart_spice(spice: SpiceService, boundary: HardwareRunner) -> None:
    boundary.status[104] = "running"
    with pytest.raises(VmctlError, match="--restart"):
        spice.plan(SpiceRequest("104"))
    result = spice.apply(
        spice.plan(SpiceRequest("104", restart=True, guest_tools="skip")), progress=lambda _: None
    )
    assert result.host_enabled and result.vga == "qxl" and result.vm.status == "running"
    assert [call[1] for call in mutations(boundary)] == ["shutdown", "set", "start"]


@pytest.mark.parametrize(
    "os_id,family",
    [("ubuntu", "debian"), ("debian", "debian"), ("rocky", "rhel"), ("alpine", "alpine")],
)
def test_guest_commands_are_os_aware_and_editable(
    spice: SpiceService, boundary: HardwareRunner, os_id: str, family: str
) -> None:
    boundary.status[104] = "running"
    boundary.vms[104]["vga"] = "qxl"
    boundary.os_data = {"result": {"id": os_id}}
    plan = spice.plan(SpiceRequest("104", guest_tools="install"))
    assert plan.guest_family == family and not mutations(boundary)
    result = spice.apply(plan, progress=lambda _: None)
    assert result.guest_tools_status == "installed"
    settings = load_spice_settings(spice.config)
    calls = [call for call in boundary.calls if call[:3] == ("qm", "guest", "exec")]
    assert [call[7:] for call in calls] == [
        tuple(command) for command in settings.families[family].commands
    ]
    assert all("--synchronous" in call and "--" in call for call in calls)


@pytest.mark.parametrize("reason", ["android", "windows", "no-agent", "stopped", "missing-os"])
def test_unavailable_guest_tools_are_manual_and_explicit_install_fails(
    spice: SpiceService, boundary: HardwareRunner, reason: str
) -> None:
    boundary.status[104] = "running"
    boundary.vms[104]["vga"] = "virtio"
    if reason == "no-agent":
        boundary.vms[104].pop("agent")
    elif reason == "stopped":
        boundary.status[104] = "stopped"
    else:
        boundary.os_data = {"result": {"id": reason}} if reason != "missing-os" else {}
    result = spice.apply(spice.plan(SpiceRequest("104")), progress=lambda _: None)
    assert (
        result.host_enabled
        and result.guest_tools_status == "unavailable"
        and not mutations(boundary)
    )
    with pytest.raises(VmctlError, match="supported Linux OS"):
        spice.plan(SpiceRequest("104", guest_tools="install"))


def test_skip_never_runs_guest_commands(spice: SpiceService, boundary: HardwareRunner) -> None:
    boundary.status[104] = "running"
    boundary.vms[104]["vga"] = "virtio"
    result = spice.apply(
        spice.plan(SpiceRequest("104", guest_tools="skip")), progress=lambda _: None
    )
    assert result.guest_tools_status == "not-requested"
    assert not any("get-osinfo" in call[2] for call in boundary.calls if call[0] == "pvesh")
    assert not mutations(boundary)


@pytest.mark.parametrize(
    "state", [{"exited": True, "exitcode": 1, "err-data": "secret"}, {"exited": True, "signal": 9}]
)
def test_guest_failure_stops_followup_commands_and_hides_output(
    spice: SpiceService, boundary: HardwareRunner, caplog: pytest.LogCaptureFixture, state: object
) -> None:
    boundary.status[104] = "running"
    boundary.vms[104]["vga"] = "qxl"
    boundary.poll_data = state
    with caplog.at_level(logging.DEBUG):
        result = spice.apply(spice.plan(SpiceRequest("104")), progress=lambda _: None)
    assert result.guest_tools_status == "failed" and boundary.guest_count == 1
    assert "secret" not in str(result) + caplog.text


def test_guest_timeout_returns_pid_without_second_submission_or_power_action(
    spice: SpiceService, boundary: HardwareRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vmctl.services.spice as module

    boundary.status[104] = "running"
    boundary.vms[104]["vga"] = "qxl"
    boundary.poll_data = {"exited": False}
    clock = 0.0

    def now() -> float:
        return clock

    def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    monkeypatch.setattr(module.time, "monotonic", now)
    monkeypatch.setattr(module.time, "sleep", sleep)
    result = spice.apply(spice.plan(SpiceRequest("104")), progress=lambda _: None)
    assert result.guest_tools_status == "uncertain" and result.guest_pid == 101
    assert boundary.guest_count == 1 and all(
        call[:3] == ("qm", "guest", "exec") for call in mutations(boundary)
    )


def test_restart_waits_for_agent_without_resubmitting_guest_jobs(
    spice: SpiceService, boundary: HardwareRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vmctl.services.spice as module

    boundary.status[104] = "running"
    plan = spice.plan(SpiceRequest("104", restart=True))
    read = spice.client.get_os_info
    reads = 0
    waits: list[float] = []

    def delayed(vmid: int, *, timeout: int = 10) -> object:
        nonlocal reads
        reads += 1
        if reads in {2, 3}:
            raise VmctlError("Guest agent is still booting")
        return read(vmid, timeout=timeout)

    monkeypatch.setattr(spice.client, "get_os_info", delayed)
    monkeypatch.setattr(module.time, "sleep", waits.append)
    result = spice.apply(plan, progress=lambda _: None)
    assert result.guest_tools_status == "installed" and len(waits) == 2
    assert boundary.guest_count == 2
    assert [call[1] for call in mutations(boundary)[:3]] == ["shutdown", "set", "start"]


@pytest.mark.parametrize("stage", ["submission", "poll"])
@pytest.mark.parametrize("timed_out", [True, False])
def test_guest_unknown_outcomes_never_retry(
    spice: SpiceService,
    boundary: HardwareRunner,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    timed_out: bool,
) -> None:
    boundary.status[104] = "running"
    boundary.vms[104]["vga"] = "qxl"
    plan = spice.plan(SpiceRequest("104"))
    method = spice.client.guest_exec if stage == "submission" else spice.client.guest_exec_status

    def timeout(*args: object, **kwargs: object) -> object:
        method(*args, **kwargs)
        raise CommandError("qm", "Guest response lost", timed_out=timed_out)

    monkeypatch.setattr(
        spice.client, "guest_exec" if stage == "submission" else "guest_exec_status", timeout
    )
    result = spice.apply(plan, progress=lambda _: None)
    assert result.guest_tools_status == "uncertain" and boundary.guest_count == 1
    assert result.guest_pid == (None if stage == "submission" else 101)


@pytest.mark.parametrize("state", [[], {}, {"exited": "true"}, {"exited": 2}])
def test_invalid_guest_status_is_uncertain_without_followup(
    spice: SpiceService, boundary: HardwareRunner, state: object
) -> None:
    boundary.status[104] = "running"
    boundary.vms[104]["vga"] = "qxl"
    boundary.poll_data = state
    result = spice.apply(spice.plan(SpiceRequest("104")), progress=lambda _: None)
    assert result.guest_tools_status == "uncertain" and result.guest_pid == 101
    assert boundary.guest_count == 1


def test_guest_submission_payload_and_failure_output_are_redacted(
    spice: SpiceService, boundary: HardwareRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vmctl.utils.subprocess import redact

    seen: list[tuple[Sequence[str], Sequence[str]]] = []

    def failure(
        args: Sequence[str],
        *,
        timeout: int = 60,
        sensitive: Sequence[str] = (),
        stream: Callable[[str], None] | None = None,
        check: bool = True,
    ) -> CommandResult:
        seen.append((args, sensitive))
        return CommandResult("private-command-output", "private-error-output", 1)

    monkeypatch.setattr(boundary, "run", failure)
    with pytest.raises(CommandError) as error:
        spice.client.guest_exec(104, ("/bin/sh", "-c", "private-script-payload"))
    assert "private" not in str(error.value)
    args, sensitive = seen[0]
    assert "private-script-payload" not in redact(" ".join(args), sensitive)


def test_settings_edit_invalidates_spice_preview(
    spice: SpiceService, boundary: HardwareRunner
) -> None:
    plan = spice.plan(SpiceRequest("104", guest_tools="skip"))
    path = spice.config.path(spice.config.host.spice_file)
    path.write_text(path.read_text().replace("timeout_seconds = 300", "timeout_seconds = 301"))
    with pytest.raises(VmctlError, match="changed since confirmation"):
        spice.apply(plan, progress=lambda _: None)
    assert not mutations(boundary)


def test_spice_config_injectable_and_invalid_commands_rejected(
    config: Configuration, tmp_path: Path
) -> None:
    path = tmp_path / "custom-spice.toml"
    path.write_text(
        '[families.custom]\nos_ids = ["custom-linux"]\ncommands = [["custom-install", "integration"]]\n'
    )
    changed = config.model_copy(
        update={"host": config.host.model_copy(update={"spice_file": str(path)})}
    )
    assert load_spice_settings(changed).families["custom"].commands == [
        ["custom-install", "integration"]
    ]
    path.write_text('[families.custom]\nos_ids = ["custom-linux"]\ncommands = [[]]\n')
    with pytest.raises(VmctlError, match="Invalid SPICE configuration"):
        load_spice_settings(changed)


def test_invalid_spice_config_never_exposes_command_input(
    config: Configuration, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "private-spice.toml"
    path.write_text(
        '[families.custom]\nos_ids = ["custom-linux"]\ncommands = "private-installer-token"\n'
    )
    changed = config.model_copy(
        update={"host": config.host.model_copy(update={"spice_file": str(path)})}
    )
    with pytest.raises(VmctlError) as error:
        load_spice_settings(changed)
    assert "Invalid SPICE configuration" in str(error.value)
    assert "private-installer-token" not in str(error.value) + caplog.text
