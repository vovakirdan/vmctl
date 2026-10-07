"""Portable lifecycle wire and uncertain-outcome behavior without a real SSH host."""

import sys
from ipaddress import IPv4Address

import pytest
from pydantic import TypeAdapter

from vmctl.client_config import ConnectionConfig
from vmctl.errors import ProtocolError, UnknownOutcomeError
from vmctl.models import VM
from vmctl.operations import ActionPreview, Catalog, LifecycleAction
from vmctl.protocol import ActionMessage, ActionParameters, Event, Request
from vmctl.ssh import SSHOperations, SSHTransport


@pytest.mark.parametrize("action", ["start", "shutdown", "reboot"])
@pytest.mark.parametrize(
    "body",
    [
        "event('hello')",
        "event('hello')\nprint('invalid JSON', flush=True)",
        "event('hello')\nevent('error', code='internal_error', message='crash')",
        "event('hello')\nevent('error', code='unknown_outcome', vmid=104, message='host timeout')",
    ],
)
def test_lost_lifecycle_reply_is_unknown(
    monkeypatch: pytest.MonkeyPatch, action: LifecycleAction, body: str
) -> None:
    import vmctl.ssh as ssh

    script = (
        """
import json, sys
request = json.load(sys.stdin)
rid = request['request_id']
def event(kind, **kw):
    print(json.dumps(dict(protocol_version=1, request_id=rid, kind=kind, **kw)), flush=True)
"""
        + body
    )
    monkeypatch.setattr(ssh, "ssh_arguments", lambda _: [sys.executable, "-u", "-c", script])
    request = ActionMessage(
        request_id="a" * 32,
        operation=action,
        parameters=ActionParameters(vmid=104, name="work", status="running", fingerprint="b" * 64),
    )
    with pytest.raises(UnknownOutcomeError) as failure:
        SSHTransport(ConnectionConfig(host="mock")).exchange(request)
    assert failure.value.vmid == 104 and failure.value.request_id == "a" * 32


@pytest.mark.parametrize("action", ["start", "shutdown", "reboot"])
def test_invalid_lifecycle_result_is_unknown(action: LifecycleAction) -> None:
    submitted: list[Request] = []

    class InvalidResult:
        def exchange(self, request: Request, progress: object = None) -> Event:
            submitted.append(request)
            return Event(request_id=request.request_id, kind="result", data={"unexpected": True})

    preview = ActionPreview(VM(104, "work", "running", "node"), "b" * 64, action)
    with pytest.raises(UnknownOutcomeError) as failure:
        SSHOperations(InvalidResult()).action(preview)
    assert failure.value.vmid == 104
    assert len(submitted) == 1


def test_older_catalog_requires_worker_upgrade() -> None:
    catalog = Catalog({}, {}, (), (), (), IPv4Address("10.210.0.100"), IPv4Address("10.210.0.199"))
    data = TypeAdapter(Catalog).dump_python(catalog, mode="json")
    data["templates"] = {
        "ubuntu-server": {
            "vmid": 9000,
            "family": "debian",
            "distro": "ubuntu",
            "release": "24.04",
        }
    }
    data.pop("system_defaults")

    class OlderWorker:
        def exchange(self, request: Request, progress: object = None) -> Event:
            return Event(request_id=request.request_id, kind="result", data=data)

    with pytest.raises(ProtocolError, match="upgrade the Proxmox"):
        SSHOperations(OlderWorker()).catalog()
