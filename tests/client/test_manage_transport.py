"""Portable typed management transport without a Proxmox host or guest execution."""

import io
import json
from ipaddress import IPv4Address

import pytest
from pydantic import ValidationError

from vmctl.models import VM, CloneRequest, HardwareRequest, SpiceRequest
from vmctl.operations import (
    ClonePreview,
    CloneResult,
    HardwarePreview,
    HardwareResult,
    Progress,
    SpicePreview,
    SpiceResult,
)
from vmctl.protocol import MUTATING_OPERATIONS, REQUEST_ADAPTER, Event, Request, encode_request
from vmctl.ssh import SSHOperations
from vmctl.worker import serve


class ManagementBackend:
    def __init__(self) -> None:
        self.vm = VM(300, "android-01", "stopped", "test-node", cpu=4, memory_mib=6144)
        self.calls: list[str] = []

    def plan_clone(self, request: CloneRequest) -> ClonePreview:
        self.calls.append("plan_clone")
        return ClonePreview(self.vm, request, "a" * 64, False, ("Guest state is copied",))

    def clone(self, expected: ClonePreview, *, request_id: str, progress: Progress) -> CloneResult:
        self.calls.append("clone")
        assert expected.fingerprint == "a" * 64
        progress("Allocated VMID: 104")
        return CloneResult(
            VM(104, expected.request.name, "stopped", "test-node"),
            300,
            "BC:24:11:01:02:03",
            IPv4Address("10.210.0.105"),
            False,
            True,
        )

    def plan_update(self, request: HardwareRequest) -> HardwarePreview:
        self.calls.append("plan_update")
        return HardwarePreview(self.vm, request, "b" * 64, 4, 6144, request.cpu or 4, 8192)

    def update(self, expected: HardwarePreview, *, progress: Progress) -> HardwareResult:
        self.calls.append("update")
        assert expected.fingerprint == "b" * 64
        return HardwareResult(self.vm, expected.new_cpu, expected.new_memory_mib, False)

    def plan_spice(self, request: SpiceRequest) -> SpicePreview:
        self.calls.append("plan_spice")
        return SpicePreview(self.vm, request, "c" * 64, "virtio", "virtio", False, None, False)

    def spice(self, expected: SpicePreview, *, progress: Progress) -> SpiceResult:
        self.calls.append("spice")
        assert expected.fingerprint == "c" * 64
        return SpiceResult(self.vm, "virtio", True, "unavailable", notes=("Guest step is manual",))


class WorkerTransport:
    def __init__(self, backend: ManagementBackend) -> None:
        self.backend = backend
        self.seen: list[str] = []

    def exchange(self, request: Request, progress: Progress | None = None) -> Event:
        self.seen.append(request.operation)
        output = io.StringIO()
        assert serve(encode_request(request).decode(), output, lambda: self.backend) == 0
        events = [Event.model_validate_json(line) for line in output.getvalue().splitlines()]
        for event in events:
            if event.kind == "progress" and progress:
                progress(event.message)
        assert all(event.request_id == request.request_id for event in events)
        return events[-1]


def test_management_worker_roundtrip_preserves_frozen_requests() -> None:
    backend = ManagementBackend()
    transport = WorkerTransport(backend)
    remote = SSHOperations(transport)
    clone = remote.plan_clone(CloneRequest("300", "android-copy", shutdown_source=True))
    assert clone.source.vmid == 300 and clone.request.shutdown_source
    messages: list[str] = []
    result = remote.clone(clone, request_id="d" * 32, progress=messages.append)
    assert result.vm.vmid == 104 and str(result.ip) == "10.210.0.105"
    assert messages == ["Allocated VMID: 104"]
    resources = remote.plan_update(HardwareRequest("300", cpu=8, memory="8G"))
    assert resources.request.cpu == 8
    assert remote.update(resources, progress=messages.append).cpu == 8
    spice = remote.plan_spice(SpiceRequest("300", guest_tools="skip"))
    assert spice.request.guest_tools == "skip"
    assert remote.spice(spice, progress=messages.append).guest_tools_status == "unavailable"
    assert transport.seen == ["plan_clone", "clone", "plan_update", "update", "plan_spice", "spice"]
    assert {"clone", "update", "spice"} <= MUTATING_OPERATIONS
    assert not {"plan_clone", "plan_update", "plan_spice"} & MUTATING_OPERATIONS


@pytest.mark.parametrize(
    "operation,parameters",
    [
        ("plan_update", {"reference": "300", "cpu": 0}),
        ("plan_update", {"reference": "300", "cpu": True}),
        ("plan_spice", {"reference": "300", "guest_tools": "arbitrary"}),
        ("plan_clone", {"reference": "300", "name": "x", "unknown": 1}),
    ],
)
def test_management_protocol_rejects_invalid_fields(
    operation: str, parameters: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        REQUEST_ADAPTER.validate_json(
            json.dumps(
                {
                    "protocol_version": 1,
                    "request_id": "a" * 32,
                    "operation": operation,
                    "parameters": parameters,
                }
            )
        )
