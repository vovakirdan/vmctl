"""End-to-end protocol orchestration with a fake Proxmox command boundary."""

import io
import json
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import FakeRunner
from pydantic import SecretStr

from vmctl.config import Configuration
from vmctl.local import LocalOperations
from vmctl.models import CreateRequest
from vmctl.operations import Progress
from vmctl.protocol import (
    CreateMessage,
    CreateParameters,
    DeleteMessage,
    DeleteParameters,
    Event,
    QueryMessage,
    ReferenceMessage,
    ReferenceParameters,
    Request,
    encode_request,
)
from vmctl.ssh import SSHOperations
from vmctl.utils.ssh_keys import read_public_key
from vmctl.utils.subprocess import CommandResult
from vmctl.worker import serve


@pytest.fixture
def backend(config: Configuration, runner: FakeRunner) -> LocalOperations:
    from vmctl.network.allocator import AddressAllocator
    from vmctl.network.dnsmasq import DnsmasqReservations
    from vmctl.proxmox.client import ProxmoxClient

    return LocalOperations(
        config.root,
        factory=lambda cfg: (
            ProxmoxClient(cfg, runner),
            DnsmasqReservations(cfg, runner),
            AddressAllocator(cfg, runner),
        ),
    )


def exchange(request: Request, backend: LocalOperations) -> list[Event]:
    output = io.StringIO()
    serve(encode_request(request).decode(), output, lambda: backend)
    return [Event.model_validate_json(line) for line in output.getvalue().splitlines()]


def request_for(
    config: Configuration, *, desktop: bool = False, request_id: str = "a" * 32
) -> CreateMessage:
    return CreateMessage(
        request_id=request_id,
        operation="create",
        parameters=CreateParameters(
            name="work",
            template="ubuntu-desktop" if desktop else "ubuntu-server",
            preset="normal",
            public_key=read_public_key(config.path(config.host.ssh_key)),
            no_desktop_password=desktop,
        ),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"operation": "arbitrary_command"},
        {"protocol_version": 2},
        {"request_id": "invalid"},
        {"parameters": {"shell": "id"}},
    ],
)
def test_invalid_requests_rejected_before_host_construction(change: dict[str, object]) -> None:
    called: list[bool] = []
    raw = {
        "protocol_version": 1,
        "request_id": "a" * 32,
        "operation": "list",
        "parameters": {},
    } | change
    output = io.StringIO()

    def factory() -> LocalOperations:
        called.append(True)
        raise AssertionError("No backend allowed")

    assert serve(json.dumps(raw), output, factory) == 1
    assert called == []
    assert "no operation was executed" in output.getvalue()


def test_plan_is_public_and_key_contents_are_used(
    config: Configuration, backend: LocalOperations, runner: FakeRunner
) -> None:
    request = request_for(config)
    config.path(config.host.ssh_key).unlink()
    preview = exchange(request.model_copy(update={"operation": "plan_create"}), backend)[-1]
    assert preview.kind == "result" and preview.data["username"] == "vmadmin"
    assert not (
        {"public_key", "user_data", "request", "desktop_password_hash"} & preview.data.keys()
    )
    assert not any(call[:2] == ("qm", "clone") for call in runner.calls)
    result = exchange(request, backend)[-1]
    assert result.kind == "result" and result.data["vmid"] == 104
    assert runner.vms[104]["sshkeys"] == request.parameters.public_key.strip()


def test_repeated_request_does_not_create_or_destroy_again(
    config: Configuration, backend: LocalOperations, runner: FakeRunner
) -> None:
    request = request_for(config)
    assert exchange(request, backend)[-1].kind == "result"
    repeated = exchange(request, backend)[-1]
    assert repeated.kind == "error" and repeated.code == "repeated_request" and repeated.vmid == 104
    assert len([call for call in runner.calls if call[:2] == ("qm", "clone")]) == 1
    assert not any(call[:2] == ("qm", "destroy") for call in runner.calls)


def test_confirmation_fingerprint_is_rechecked(
    config: Configuration, backend: LocalOperations, runner: FakeRunner
) -> None:
    assert exchange(request_for(config), backend)[-1].kind == "result"
    plan = backend.plan_delete("work")
    runner.vms[104]["cores"] = "16"
    rejected = exchange(
        DeleteMessage(request_id="b" * 32, parameters=DeleteParameters.from_preview(plan)), backend
    )[-1]
    assert rejected.kind == "error" and "changed since confirmation" in rejected.message
    assert 104 in runner.vms


def test_info_redacts_credentials_before_transfer(
    config: Configuration, backend: LocalOperations, runner: FakeRunner
) -> None:
    exchange(request_for(config), backend)
    runner.vms[104]["cipassword"] = "private-password"
    reply = exchange(
        ReferenceMessage(
            request_id="b" * 32, operation="info", parameters=ReferenceParameters(reference="104")
        ),
        backend,
    )[-1]
    text = reply.model_dump_json()
    assert "private-password" not in text
    assert request_for(config).parameters.public_key.strip() not in text
    assert reply.data["config"]["cipassword"] == "<redacted>"


def test_password_hash_only_goes_to_protected_snippet(
    config: Configuration, backend: LocalOperations, runner: FakeRunner
) -> None:
    hashed = "$6$rounds=500000$testsalt$" + "A" * 86
    request = request_for(config, desktop=True)
    request = request.model_copy(
        update={
            "parameters": request.parameters.model_copy(
                update={"desktop_password_hash": SecretStr(hashed), "no_desktop_password": False}
            )
        }
    )
    events = exchange(request, backend)
    assert events[-1].kind == "result"
    assert hashed not in "\n".join(event.model_dump_json() for event in events)
    assert hashed not in repr(runner.calls)
    snippet = next((runner.root / "snippets").glob("vmctl-*.yaml"))
    assert hashed in snippet.read_text()
    assert snippet.stat().st_mode & 0o777 == 0o600


def test_concurrent_workers_share_lock_and_reject_duplicate(
    config: Configuration,
    backend: LocalOperations,
    runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()
    original = runner.run

    def blocked(
        args: Sequence[str],
        *,
        timeout: int = 60,
        sensitive: Sequence[str] = (),
        stream: Callable[[str], None] | None = None,
        check: bool = True,
    ) -> CommandResult:
        if args[:2] == ["qm", "clone"]:
            entered.set()
            assert release.wait(3)
        return original(args, timeout=timeout, sensitive=sensitive, stream=stream, check=check)

    monkeypatch.setattr(runner, "run", blocked)
    request = request_for(config)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(exchange, request, backend)
        assert entered.wait(3)
        second = pool.submit(exchange, request, backend)
        release.set()
        outcomes = [first.result(timeout=5)[-1], second.result(timeout=5)[-1]]
    assert [event.kind for event in outcomes] == ["result", "error"]
    assert outcomes[1].code == "repeated_request"


def test_broken_response_pipe_does_not_trigger_rollback(
    config: Configuration, backend: LocalOperations, runner: FakeRunner
) -> None:
    class Disconnected(io.StringIO):
        def write(self, text: str) -> int:
            raise BrokenPipeError()

    assert serve(encode_request(request_for(config)).decode(), Disconnected(), lambda: backend) == 0
    assert 104 in runner.vms


def test_frontend_roundtrip_uses_same_typed_contract(
    config: Configuration, backend: LocalOperations
) -> None:
    class InProcessTransport:
        def exchange(self, request: Request, progress: Progress | None = None) -> Event:
            events = exchange(request, backend)
            for event in events:
                if event.kind == "progress" and progress:
                    progress(event.message)
            assert events[-1].kind == "result", events[-1].message
            return events[-1]

    frontend = SSHOperations(InProcessTransport())
    request = CreateRequest(
        "work",
        "ubuntu-desktop",
        "small",
        public_key=read_public_key(config.path(config.host.ssh_key)),
        no_desktop_password=True,
    )
    preview = frontend.plan_create(request)
    assert preview.system_features == ("qemu-agent", "desktop-rdp")
    result = frontend.create(request, request_id="a" * 32, progress=lambda _: None)
    assert result.rdp_enabled and result.ip.exploded == "10.210.0.100"
    info = frontend.info("104")
    assert info.rdp_address == "10.210.0.100:3389"
    assert frontend.templates()["ubuntu-desktop"].desktop
    catalog = frontend.catalog()
    assert catalog.templates["ubuntu-desktop"].desktop
    assert catalog.presets["small"].cpu == 2
    assert any(item.name == "desktop-rdp" for item in catalog.system_features)
    assert str(catalog.pool_end) == "10.210.0.199"
    assert frontend.presets()["small"].cpu == 2
    assert frontend.validate_config().templates == 5
    assert len(frontend.list()) == 1
    assert frontend.delete(frontend.plan_delete("work")) == 104


@pytest.mark.parametrize("missing", ["protocol_version", "parameters"])
def test_required_envelope_fields_are_not_defaulted(missing: str) -> None:
    raw = json.loads(encode_request(QueryMessage(request_id="a" * 32, operation="list")))
    del raw[missing]
    output = io.StringIO()
    assert (
        serve(json.dumps(raw), output, lambda: pytest.fail("Do not construct host services")) == 1
    )
    assert "invalid_request" in output.getvalue()


@pytest.mark.parametrize("version", [True, "1", 0, 2])
def test_protocol_version_is_a_strict_integer(version: object) -> None:
    raw = json.loads(encode_request(QueryMessage(request_id="a" * 32, operation="list")))
    raw["protocol_version"] = version
    output = io.StringIO()
    assert (
        serve(json.dumps(raw), output, lambda: pytest.fail("Do not construct host services")) == 1
    )


def test_workstation_paths_are_rejected_as_parameters(config: Configuration) -> None:
    raw = json.loads(encode_request(request_for(config)))
    raw["parameters"]["ssh_key"] = "C:/Users/user/private.pem"
    output = io.StringIO()
    assert (
        serve(json.dumps(raw), output, lambda: pytest.fail("Do not construct host services")) == 1
    )
    assert "C:/Users" not in output.getvalue()


def test_worker_domain_errors_do_not_echo_request_hash(config: Configuration) -> None:
    secret = "$6$rounds=500000$testsalt$" + "A" * 86
    request = request_for(config)
    request = request.model_copy(
        update={
            "parameters": request.parameters.model_copy(
                update={"desktop_password_hash": SecretStr(secret)}
            )
        }
    )
    from vmctl.errors import VmctlError

    def failed_backend() -> LocalOperations:
        raise VmctlError("Injected error " + secret)

    output = io.StringIO()
    assert serve(encode_request(request).decode(), output, failed_backend) == 1
    assert secret not in output.getvalue()
    assert "<redacted>" in output.getvalue()
