"""Actual local pipe/process tests with a fake worker; no network connections."""

import json
import logging
import shlex
import sys
from dataclasses import replace
from typing import Any, NoReturn

import pytest
from pydantic import SecretStr

from vmctl.client_config import ConnectionConfig
from vmctl.errors import UnknownOutcomeError, VmctlError
from vmctl.models import CreateRequest
from vmctl.protocol import CreateMessage, CreateParameters, QueryMessage, encode_request
from vmctl.ssh import SSHTransport, ssh_arguments

HASH = "$6$rounds=500000$testsalt$" + "A" * 86


def create_message() -> CreateMessage:
    return CreateMessage(
        request_id="a" * 32,
        operation="create",
        parameters=CreateParameters(
            name="work",
            template="ubuntu-desktop",
            preset="normal",
            public_key="ssh-ed25519 PUBLIC",
            desktop_password_hash=SecretStr(HASH),
        ),
    )


def fake_worker(monkeypatch: pytest.MonkeyPatch, body: str, timeout: int = 3) -> SSHTransport:
    import vmctl.ssh as ssh

    script = (
        """
import json, sys, time
request = json.load(sys.stdin)
rid = request['request_id']
def event(kind, **kw):
    print(json.dumps(dict(protocol_version=1, request_id=rid, kind=kind, **kw)), flush=True)
"""
        + body
    )
    monkeypatch.setattr(ssh, "ssh_arguments", lambda _: [sys.executable, "-u", "-c", script])
    return SSHTransport(ConnectionConfig(host="mock", operation_timeout=timeout))


def test_arguments_quote_only_fixed_worker_command() -> None:
    config = ConnectionConfig(
        host="pve", worker="/opt/my tool/worker;$(id)", config_dir="/etc/my config", sudo=True
    )
    args = ssh_arguments(config)
    assert args[0:2] == ["ssh", "-T"]
    assert "BatchMode=yes" in args and "StrictHostKeyChecking=yes" in args
    assert "ForwardAgent=no" in args
    assert args[-2] == "pve"
    assert shlex.split(args[-1]) == [
        "sudo",
        "-n",
        "--",
        config.worker,
        "--config-dir",
        config.config_dir,
    ]
    assert "work" not in args and HASH not in repr(args)


def test_only_explicit_wire_serializer_unwraps_hash() -> None:
    message = create_message()
    assert HASH not in repr(message)
    assert HASH not in message.model_dump_json()
    body = json.loads(encode_request(message))
    assert body["parameters"]["desktop_password_hash"] == HASH
    assert "ssh_key" not in body["parameters"]


def test_streamed_progress_and_terminal_result(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = fake_worker(
        monkeypatch,
        "event('hello')\nevent('progress', message='cloning', vmid=104)\nevent('result', data={'ok': True})",
    )
    progress: list[str] = []
    result = transport.exchange(create_message(), progress.append)
    assert progress == ["cloning"] and result.data == {"ok": True}


@pytest.mark.parametrize(
    "body",
    [
        "event('hello')\nevent('progress', message='Allocated VMID: 104', vmid=104)",
        "event('hello')\nevent('progress', message='Allocated VMID: 104', vmid=104)\ntime.sleep(10)",
        "event('hello')\nevent('progress', message='Allocated VMID: 104', vmid=104)\nprint('not JSON', flush=True)",
    ],
)
def test_lost_mutation_response_is_unknown(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    transport = fake_worker(monkeypatch, body, timeout=1)
    with pytest.raises(UnknownOutcomeError) as failure:
        transport.exchange(create_message())
    assert failure.value.request_id == "a" * 32 and failure.value.vmid == 104
    assert "no automatic retry" in str(failure.value)


@pytest.mark.parametrize(
    "body",
    [
        "event('result', data={})",
        "print(json.dumps(dict(request_id=rid, kind='hello')), flush=True)",
        "event('hello')\nevent('hello')",
        "event('hello')\nprint(json.dumps(dict(protocol_version=2, request_id=rid, kind='result')), flush=True)",
        "event('hello')\nprint(json.dumps(dict(protocol_version=1, request_id='b'*32, kind='result')), flush=True)",
        "event('hello')\nevent('result', data=[])\nevent('progress', message='late')",
    ],
)
def test_incompatible_or_invalid_responses_rejected(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    transport = fake_worker(monkeypatch, body)
    with pytest.raises(VmctlError):
        transport.exchange(QueryMessage(request_id="a" * 32, operation="list"))


def test_progress_errors_and_logs_redact_hash(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    transport = fake_worker(
        monkeypatch,
        "event('hello')\nsecret=request['parameters']['desktop_password_hash']\nevent('progress', message=secret)\nevent('error', message=secret, code='operation_failed')",
    )
    progress: list[str] = []
    with caplog.at_level(logging.DEBUG), pytest.raises(VmctlError) as failure:
        transport.exchange(create_message(), progress.append)
    assert progress == ["<redacted>"]
    assert HASH not in str(failure.value) + caplog.text


def test_worker_crash_reports_unknown_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = fake_worker(
        monkeypatch, "event('hello')\nevent('error', code='internal_error', message='crash')"
    )
    with pytest.raises(UnknownOutcomeError):
        transport.exchange(create_message())


def test_missing_openssh_is_clear_and_does_not_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    import vmctl.ssh as ssh

    monkeypatch.setattr(ssh, "ssh_arguments", lambda _: ["vmctl-missing-executable-for-test"])
    with pytest.raises(VmctlError, match="install ssh"):
        SSHTransport(ConnectionConfig(host="mock")).exchange(create_message())


def test_domain_path_is_never_sent() -> None:
    request = CreateRequest(
        "work",
        "ubuntu-desktop",
        "normal",
        public_key="ssh-ed25519 PUBLIC",
    )
    params = CreateParameters.from_domain(replace(request, desktop_password_hash=SecretStr(HASH)))
    assert "C:/" not in params.model_dump_json()
    assert not hasattr(params.to_domain(), "ssh_key")


def test_ctrl_c_during_mutation_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    import vmctl.ssh as ssh

    transport = fake_worker(monkeypatch, "event('hello')\ntime.sleep(10)")

    def interrupted(self: object, *args: object, **kwargs: object) -> NoReturn:
        raise KeyboardInterrupt()

    monkeypatch.setattr(ssh.queue.Queue, "get", interrupted)
    with pytest.raises(UnknownOutcomeError) as failure:
        transport.exchange(create_message())
    assert failure.value.request_id == "a" * 32


def test_request_submission_uses_pipe_and_no_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    import vmctl.ssh as ssh

    transport = fake_worker(monkeypatch, "event('hello')\nevent('result', data={})")
    actual = ssh.subprocess.Popen
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def capture(args: list[str], **kwargs: Any) -> ssh.subprocess.Popen[bytes]:
        calls.append((args, kwargs))
        return actual(args, **kwargs)

    monkeypatch.setattr(ssh.subprocess, "Popen", capture)
    transport.exchange(create_message())
    assert len(calls) == 1
    assert HASH not in repr(calls)
    assert calls[0][1]["stdin"] == ssh.subprocess.PIPE
    assert not calls[0][1].get("shell", False)


def test_repeated_request_returns_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from vmctl.errors import RepeatedRequestError

    transport = fake_worker(
        monkeypatch,
        "event('hello')\nevent('error', code='repeated_request', vmid=104, message='duplicate')",
    )
    with pytest.raises(RepeatedRequestError) as failure:
        transport.exchange(create_message())
    assert failure.value.vmid == 104 and failure.value.request_id == "a" * 32


def test_invalid_domain_request_does_not_dump_credentials() -> None:
    request = CreateRequest(
        "work", "ubuntu-desktop", "normal", desktop_password_hash=SecretStr(HASH)
    )
    with pytest.raises(VmctlError) as failure:
        CreateParameters.from_domain(request)
    assert HASH not in str(failure.value)
    assert "public-key contents" in str(failure.value)
