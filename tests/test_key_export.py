"""Guest enrollment safety, idempotency and typed SSH-worker integration."""

import base64
import json
import logging
import os
import shlex
import struct
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from conftest import FakeRunner
from pydantic import TypeAdapter

from vmctl.config import Configuration
from vmctl.errors import UncertainOperationError, VmctlError
from vmctl.models import KeyExportRequest
from vmctl.operations import KeyExportPreview, KeyExportResult
from vmctl.protocol import (
    MUTATING_OPERATIONS,
    REQUEST_ADAPTER,
    KeyExportExpected,
    KeyExportMessage,
    KeyExportParameters,
    PlanKeyExportMessage,
    encode_request,
)
from vmctl.proxmox.client import ProxmoxClient
from vmctl.services.ssh_keys import (
    GUEST_MERGE_SCRIPT,
    KeyExportService,
    guest_merge_arguments,
    key_fingerprints,
)
from vmctl.utils.subprocess import CommandResult
from vmctl.worker import dispatch


def public_key(value: int = 1, comment: str = "phone") -> str:
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes([value]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " " + comment + "\n"


class EnrollmentRunner(FakeRunner):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.vms[104] = {"name": "work", "agent": "1"}
        self.status[104] = "running"
        self.os_data: object = {"result": {"id": "debian"}}
        self.result: object = {"exited": True, "exitcode": 0, "out-data": "VMCTL_KEYS 1 0\n"}

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: int = 60,
        sensitive: Sequence[str] = (),
        stream: Callable[[str], None] | None = None,
        check: bool = True,
    ) -> CommandResult:
        if args[0] == "pvesh" and args[2].endswith("/agent/get-osinfo"):
            self.calls.append(tuple(args))
            return CommandResult(json.dumps(self.os_data))
        if args[:3] == ["qm", "guest", "exec-status"]:
            self.calls.append(tuple(args))
            return CommandResult(json.dumps(self.result))
        return super().run(args, timeout=timeout, sensitive=sensitive, stream=stream, check=check)


@pytest.fixture
def enrollment(
    config: Configuration, runner: FakeRunner
) -> tuple[KeyExportService, EnrollmentRunner]:
    boundary = EnrollmentRunner(runner.root)
    return KeyExportService(config, ProxmoxClient(config, boundary)), boundary


def test_plan_is_read_only_and_normalizes(
    enrollment: tuple[KeyExportService, EnrollmentRunner],
) -> None:
    service, runner = enrollment
    plan = service.plan(KeyExportRequest("work", public_key() + public_key(comment="duplicate")))
    assert plan.request.username == "vmadmin"
    assert plan.request.public_key == public_key()
    assert not any(call[:2] == ("qm", "guest") for call in runner.calls)
    assert plan.vm.vmid == 104


@pytest.mark.parametrize("username", ["root", "-root", "bob;id", "../vmadmin", "", "a\nroot"])
def test_rejects_unsafe_username(
    enrollment: tuple[KeyExportService, EnrollmentRunner], username: str
) -> None:
    service, _ = enrollment
    with pytest.raises(VmctlError, match="non-root"):
        service.plan(KeyExportRequest("work", public_key(), username))


@pytest.mark.parametrize("issue", ["stopped", "template", "agent", "unsupported", "private"])
def test_requires_running_supported_guest(
    enrollment: tuple[KeyExportService, EnrollmentRunner], issue: str
) -> None:
    service, runner = enrollment
    key = public_key()
    if issue == "stopped":
        runner.status[104] = "stopped"
    elif issue == "template":
        runner.vms[104]["template"] = "1"
    elif issue == "agent":
        runner.vms[104].pop("agent")
    elif issue == "unsupported":
        runner.os_data = {"result": {"id": "mswindows"}}
    else:
        key = "-----BEGIN OPENSSH PRIVATE KEY-----"
    with pytest.raises(VmctlError):
        service.plan(KeyExportRequest("work", key))
    assert not any(call[:2] == ("qm", "guest") for call in runner.calls)


def test_export_rechecks_identity(enrollment: tuple[KeyExportService, EnrollmentRunner]) -> None:
    service, runner = enrollment
    plan = service.plan(KeyExportRequest("work", public_key()))
    runner.vms[104]["name"] = "changed"
    with pytest.raises(VmctlError):
        service.export(plan, progress=lambda _: None)
    assert not any(call[:2] == ("qm", "guest") for call in runner.calls)


def test_export_preserves_vm_state_and_returns_safe_result(
    enrollment: tuple[KeyExportService, EnrollmentRunner], caplog: pytest.LogCaptureFixture
) -> None:
    service, runner = enrollment
    with caplog.at_level(logging.DEBUG):
        result = service.export(
            service.plan(KeyExportRequest("work", public_key())), progress=lambda _: None
        )
    assert result.added == 1 and result.unchanged == 0
    assert result.fingerprints == key_fingerprints(public_key())
    assert runner.status[104] == "running"
    assert not any(
        call[:2] in {("qm", "set"), ("qm", "stop"), ("qm", "reboot")} for call in runner.calls
    )
    assert "PRIVATE KEY" not in caplog.text


@pytest.mark.parametrize(
    "result",
    [
        {"exited": True, "exitcode": 0, "out-data": "secrets"},
        {"exited": 3},
        {"exited": True, "exitcode": 0, "out-data": "VMCTL_KEYS 99 1\n"},
    ],
)
def test_invalid_guest_result_is_uncertain(
    enrollment: tuple[KeyExportService, EnrollmentRunner], result: object
) -> None:
    service, runner = enrollment
    runner.result = result
    with pytest.raises(UncertainOperationError) as error:
        service.export(
            service.plan(KeyExportRequest("work", public_key())), progress=lambda _: None
        )
    assert error.value.vmid == 104
    assert "secrets" not in str(error.value)


@pytest.mark.parametrize(
    "code, message",
    [(41, "non-root"), (42, "unsafe"), (43, "locked"), (44, "changed"), (1, "withheld")],
)
def test_guest_failures_are_clear_and_do_not_expose_output(
    enrollment: tuple[KeyExportService, EnrollmentRunner], code: int, message: str
) -> None:
    service, runner = enrollment
    runner.result = {"exited": True, "exitcode": code, "err-data": "SECRET"}
    with pytest.raises(VmctlError, match=message) as error:
        service.export(
            service.plan(KeyExportRequest("work", public_key())), progress=lambda _: None
        )
    assert "SECRET" not in str(error.value)


def run_merge(home: Path, keys: str) -> subprocess.CompletedProcess[str]:
    """Run only the merge in a temporary home, replacing identity discovery for this test."""
    script = GUEST_MERGE_SCRIPT.replace('[ "$uid" -ne 0 ] || exit 41', ":")
    script = script.replace(
        "home=$(awk -F: -v user=\"$(id -un)\" '$1 == user {print $6}' /etc/passwd)",
        "home=" + shlex.quote(str(home)),
    )
    script = script.replace("VMCTL_PUBLIC_KEYS", shlex.quote(keys))
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=False)


def test_shell_merge_preserves_bytes_options_and_deduplicates(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    directory = home / ".ssh"
    directory.mkdir(mode=0o700)
    existing = directory / "authorized_keys"
    original = (
        "# comment with key: "
        + public_key(3)
        + 'command="echo hello world",no-port-forwarding '
        + public_key(1)
    ).encode()
    existing.write_bytes(original)
    keys = public_key(1, "new comment") + public_key(2)
    first = run_merge(home, keys)
    assert first.returncode == 0, first.stderr
    assert first.stdout == "VMCTL_KEYS 1 1\n"
    assert existing.read_bytes() == original + public_key(2).encode()
    assert existing.stat().st_mode & 0o777 == 0o600
    second = run_merge(home, keys)
    assert second.returncode == 0, second.stderr
    assert second.stdout == "VMCTL_KEYS 0 2\n"
    assert existing.read_bytes() == original + public_key(2).encode()


def test_shell_merge_empty_file_and_missing_newline(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    assert run_merge(home, public_key()).stdout == "VMCTL_KEYS 1 0\n"
    existing = home / ".ssh" / "authorized_keys"
    existing.write_bytes(b"# retained without newline")
    assert run_merge(home, public_key()).returncode == 0
    assert existing.read_bytes() == b"# retained without newline\n" + public_key().encode()


@pytest.mark.parametrize(
    "unsafe", ["home-link", "directory-link", "file-link", "hardlink", "world-writable", "lock"]
)
def test_shell_merge_rejects_unsafe_paths(tmp_path: Path, unsafe: str) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    sentinel = other / "sentinel"
    sentinel.write_text("do not touch")
    directory = home / ".ssh"
    if unsafe == "home-link":
        home.rmdir()
        home.symlink_to(other, target_is_directory=True)
    elif unsafe == "directory-link":
        directory.symlink_to(other, target_is_directory=True)
    else:
        directory.mkdir(mode=0o700)
        if unsafe == "file-link":
            (directory / "authorized_keys").symlink_to(sentinel)
        elif unsafe == "hardlink":
            os.link(sentinel, directory / "authorized_keys")
        elif unsafe == "world-writable":
            directory.chmod(0o777)
        else:
            (directory / ".vmctl-keys.lock").mkdir()
    result = run_merge(home, public_key())
    assert result.returncode in {42, 43}
    assert sentinel.read_text() == "do not touch"


def test_key_like_quoted_option_and_comment_do_not_count_as_installed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    directory = home / ".ssh"
    directory.mkdir(mode=0o700)
    original = (
        "# " + public_key(1) + 'command="echo ' + public_key(1).strip() + '" ' + public_key(2)
    )
    (directory / "authorized_keys").write_text(original)
    result = run_merge(home, public_key(1))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "VMCTL_KEYS 1 0\n"
    assert (directory / "authorized_keys").read_text() == original + public_key(1)


def test_guest_arguments_drop_privileges_and_quote_public_comments() -> None:
    args = guest_merge_arguments("vmadmin", public_key(comment="$(touch /never-created); ' quote"))
    assert args[:2] == ("/bin/sh", "-c")
    assert "exec su -s /bin/sh -c" in args[2]
    assert "vmadmin" in args[2]


def test_wire_requests_round_trip_and_worker_dispatch(
    enrollment: tuple[KeyExportService, EnrollmentRunner],
) -> None:
    service, _ = enrollment
    request = KeyExportRequest("work", public_key())
    plan = service.plan(request)
    message = PlanKeyExportMessage(
        request_id="a" * 32,
        parameters=KeyExportParameters(**TypeAdapter(KeyExportRequest).dump_python(request)),
    )
    assert REQUEST_ADAPTER.validate_json(encode_request(message)) == message
    export = KeyExportMessage(request_id="b" * 32, parameters=KeyExportExpected(expected=plan))
    assert REQUEST_ADAPTER.validate_json(encode_request(export)) == export
    assert "export_keys" in MUTATING_OPERATIONS

    class Backend:
        def plan_key_export(self, request: KeyExportRequest) -> KeyExportPreview:
            return service.plan(request)

        def export_keys(
            self, expected: KeyExportPreview, *, progress: Callable[[str], None]
        ) -> KeyExportResult:
            return service.export(expected, progress=progress)

    backend = Backend()
    assert dispatch(message, backend, lambda _: None) == plan  # type: ignore[arg-type]
    result = dispatch(export, backend, lambda _: None)  # type: ignore[arg-type]
    assert isinstance(result, KeyExportResult)


def test_unchanged_key_repairs_permissions_atomically(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    directory = home / ".ssh"
    directory.mkdir(mode=0o700)
    existing = directory / "authorized_keys"
    existing.write_text(public_key())
    existing.chmod(0o644)
    inode = existing.stat().st_ino
    result = run_merge(home, public_key())
    assert result.returncode == 0, result.stderr
    assert result.stdout == "VMCTL_KEYS 0 1\n"
    assert existing.stat().st_mode & 0o777 == 0o600
    assert existing.stat().st_ino != inode
    assert existing.read_text() == public_key()


def test_remote_operations_and_invalid_mutation_result(
    enrollment: tuple[KeyExportService, EnrollmentRunner],
) -> None:
    from vmctl.errors import UnknownOutcomeError
    from vmctl.protocol import Event, Request
    from vmctl.ssh import SSHOperations

    service, _ = enrollment

    class Transport:
        invalid = False

        def exchange(
            self, request: Request, progress: Callable[[str], None] | None = None
        ) -> Event:
            if self.invalid:
                data = {}
            else:
                if isinstance(request, PlanKeyExportMessage):
                    result = service.plan(request.parameters.to_domain())
                else:
                    assert isinstance(request, KeyExportMessage)
                    result = service.export(request.parameters.expected, progress=lambda _: None)
                data = TypeAdapter(type(result)).dump_python(result, mode="json")
            return Event(request_id=request.request_id, kind="result", data=data)

    transport = Transport()
    remote = SSHOperations(transport)
    plan = remote.plan_key_export(KeyExportRequest("work", public_key()))
    result = remote.export_keys(plan, progress=lambda _: None)
    assert result.added == 1 and result.vm.vmid == 104
    transport.invalid = True
    with pytest.raises(UnknownOutcomeError) as error:
        remote.export_keys(plan, progress=lambda _: None)
    assert error.value.vmid == 104


def test_existing_restricted_key_comment_quotes_cannot_loosen_restrictions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    directory = home / ".ssh"
    directory.mkdir(mode=0o700)
    original = 'from="10.200.0.3",restrict ' + public_key(
        comment='a comment with an unmatched " quote'
    )
    existing = directory / "authorized_keys"
    existing.write_text(original)
    result = run_merge(home, public_key())
    assert result.returncode == 0, result.stderr
    assert result.stdout == "VMCTL_KEYS 0 1\n"
    assert existing.read_text() == original
