"""Read-only metrics and address DTOs use the same portable worker protocol."""

from ipaddress import IPv4Address

import pytest
from pydantic import TypeAdapter, ValidationError

from vmctl.errors import ProtocolError
from vmctl.models import VM, IPObservation, VMDetails, VMMetricPoint, VMStats
from vmctl.operations import Progress
from vmctl.protocol import (
    MUTATING_OPERATIONS,
    REQUEST_ADAPTER,
    Event,
    Request,
    StatsMessage,
    StatsParameters,
    encode_request,
)
from vmctl.ssh import SSHOperations


class ReplyTransport:
    def __init__(self, result: VMStats | VMDetails) -> None:
        self.result = result
        self.requests: list[Request] = []

    def exchange(self, request: Request, progress: Progress | None = None) -> Event:
        self.requests.append(request)
        encoded = TypeAdapter(type(self.result)).dump_python(self.result, mode="json")
        return Event(request_id=request.request_id, kind="result", data=encoded)


def test_remote_stats_request_is_typed_read_only_and_preserves_units() -> None:
    stats = VMStats(
        VM(104, "work", "running", "node"),
        100,
        cpu_percent=12.5,
        memory_bytes=1024,
        memory_total_bytes=2048,
        network_in_bytes=5000,
        disk_write_bytes=7000,
        history=(VMMetricPoint(60, network_in_bytes_per_second=42.5),),
        history_note="Minute averages",
    )
    transport = ReplyTransport(stats)
    result = SSHOperations(transport).stats("work", history=True)
    assert result == stats
    request = transport.requests[0]
    assert isinstance(request, StatsMessage)
    assert request.parameters.reference == "work" and request.parameters.history is True
    assert request.operation not in MUTATING_OPERATIONS
    assert REQUEST_ADAPTER.validate_json(encode_request(request)) == request


def test_old_inventory_dto_remains_compatible_and_observation_properties_are_local() -> None:
    old = TypeAdapter(VMDetails).validate_json(
        '{"vm":{"vmid":104,"name":"work","status":"running","node":"node"},'
        '"config":{},"metadata":{},"ip":null}'
    )
    assert old.addresses == () and old.display_ip is None and old.ip_source == "unknown"
    observed = VMDetails(
        old.vm, {}, {}, None, (IPObservation(IPv4Address("10.210.0.20"), "dhcp-lease"),)
    )
    result = SSHOperations(ReplyTransport(observed)).info("work")
    assert result.ip is None and result.display_ip == IPv4Address("10.210.0.20")
    assert result.ip_source == "dhcp-lease"


@pytest.mark.parametrize("change", [{"history": "true"}, {"reference": ""}, {"shell": "id"}])
def test_stats_request_rejects_wrong_types_and_arbitrary_commands(
    change: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        StatsParameters.model_validate({"reference": "104", "history": False} | change)


def test_invalid_stats_result_is_protocol_error_without_mutation_uncertainty() -> None:
    class InvalidReply:
        def exchange(self, request: Request, progress: Progress | None = None) -> Event:
            return Event(request_id=request.request_id, kind="result", data={"vm": "bad"})

    with pytest.raises(ProtocolError, match="Invalid worker result schema"):
        SSHOperations(InvalidReply()).stats("104")
