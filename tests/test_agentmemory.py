"""Shared memory credentials never enter previews and revoke before VM destruction."""

from ipaddress import IPv4Address
from unittest.mock import MagicMock, Mock

import pytest
from conftest import FakeRunner
from test_workflows_cli import services

from vmctl.agentmemory.config import AgentmemorySettings, settings
from vmctl.agentmemory.service import AgentmemoryService, generate_access
from vmctl.bootstrap.system import select_bootstrap
from vmctl.config import Configuration
from vmctl.errors import CreationError, VmctlError
from vmctl.models import CloneRequest, CreateRequest
from vmctl.proxmox.parser import decode_metadata, encode_metadata
from vmctl.services.clone_vm import CloneVMService


def definition() -> AgentmemorySettings:
    return AgentmemorySettings(
        endpoint="https://10.210.0.107",
        ca_file="memory-ca.pem",
        management_host="10.210.0.107",
        management_key_file="memory.key",
        management_known_hosts_file="memory.known_hosts",
        firewall_vmid=103,
    )


def configured(config: Configuration) -> Configuration:
    config.path("memory-ca.pem").write_text("TEST CERTIFICATE")
    return config.model_copy(
        update={"host": config.host.model_copy(update={"agentmemory": definition().model_dump()})}
    )


def test_feature_opt_in_independent_and_requires_configuration(config: Configuration) -> None:
    plain = select_bootstrap(config, CreateRequest("example", "debian", "small"))
    assert "agentmemory" not in [feature.name for feature in plain.system_features]
    request = CreateRequest("example", "debian", "small", system_features=("agentmemory",))
    with pytest.raises(VmctlError, match="unavailable"):
        select_bootstrap(config, request)
    selected = select_bootstrap(configured(config), request)
    assert "agentmemory" in [feature.name for feature in selected.system_features]
    assert not selected.modules


def test_configuration_errors_do_not_echo_secrets(config: Configuration) -> None:
    config = config.model_copy(
        update={"host": config.host.model_copy(update={"agentmemory": {"secret": "DO-NOT-PRINT"}})}
    )
    with pytest.raises(VmctlError) as failure:
        settings(config)
    assert "DO-NOT-PRINT" not in str(failure.value)


def test_distinct_credentials_and_redacted_repr() -> None:
    first = generate_access(definition(), 104, "a" * 32)
    second = generate_access(definition(), 104, "b" * 32)
    assert first.secret != second.secret
    assert first.secret not in repr(first)
    assert first.client_id != second.client_id


def test_provision_and_revoke_own_identity(config: Configuration) -> None:
    config = configured(config)
    service = AgentmemoryService(config, Mock())
    service.client = Mock()
    service.firewall = Mock()
    access = generate_access(definition(), 104, "a" * 32)
    service.provision(access, IPv4Address("10.210.0.108"))
    service.firewall.grant.assert_called_once_with(IPv4Address("10.210.0.108"), access.client_id)
    service.revoke(access.client_id, "a" * 32, rollback=True)
    assert service.client.request.call_args.kwargs["secret"] == access.secret
    with pytest.raises(VmctlError, match="ownership"):
        service.revoke(access.client_id, "b" * 32)


def test_dryrun_does_not_generate_credentials(
    config: Configuration, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configured(config)
    creator, _, _, _ = services(config, runner)
    service = Mock()
    monkeypatch.setattr("vmctl.services.create_vm.AgentmemoryService", Mock(return_value=service))
    generator = Mock(side_effect=AssertionError("preview generated secret"))
    monkeypatch.setattr("vmctl.services.create_vm.generate_memory_access", generator)
    plan = creator.plan(
        CreateRequest("example", "debian", "small", system_features=("agentmemory",))
    )
    assert "agentmemory" in plan.system_features
    generator.assert_not_called()
    service.preflight.assert_called_once()
    service.provision.assert_not_called()
    assert not any(call[:2] == ("qm", "clone") for call in runner.calls)


def test_unavailable_backend_stops_before_clone(
    config: Configuration, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configured(config)
    creator, _, _, _ = services(config, runner)
    service = Mock()
    service.preflight.side_effect = VmctlError("Memory backend unavailable")
    monkeypatch.setattr("vmctl.services.create_vm.AgentmemoryService", Mock(return_value=service))
    with pytest.raises(VmctlError, match="unavailable"):
        creator.create(
            CreateRequest("example", "debian", "small", system_features=("agentmemory",))
        )
    assert not any(call[:2] == ("qm", "clone") for call in runner.calls)


def test_combined_features_revoke_both_on_memory_failure(
    config: Configuration, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_fileshare import (
        configured as fileshare_configured,
    )
    from test_fileshare import (
        definition as fileshare_definition,
    )

    config = configured(fileshare_configured(config))
    creator, _, _, _ = services(config, runner)
    fileshare, memory = Mock(), Mock()
    fileshare.settings = fileshare_definition(config)
    memory.settings = definition()
    memory.provision.side_effect = VmctlError("memory grant failed")
    monkeypatch.setattr("vmctl.services.create_vm.FileshareService", Mock(return_value=fileshare))
    monkeypatch.setattr("vmctl.services.create_vm.AgentmemoryService", Mock(return_value=memory))
    with pytest.raises(CreationError, match="memory grant failed"):
        creator.create(
            CreateRequest(
                "example", "debian", "small", system_features=("fileshare", "agentmemory")
            )
        )
    fileshare.revoke.assert_called_once()
    memory.revoke.assert_called_once()
    assert 104 not in runner.vms


def test_revoke_failure_retains_running_vm(
    config: Configuration, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator, delete, _, _ = services(config, runner)
    result = creator.create(CreateRequest("example", "debian", "small"))
    metadata = decode_metadata(runner.vms[result.vmid]["description"])
    metadata["agentmemory_client_id"] = f"vmctl-{result.vmid}-{metadata['invocation']}"
    runner.vms[result.vmid]["description"] = encode_metadata("", metadata)
    memory = Mock()
    memory.revoke.side_effect = VmctlError("gateway unavailable")
    monkeypatch.setattr("vmctl.services.delete_vm.AgentmemoryService", Mock(return_value=memory))
    with pytest.raises(VmctlError, match="gateway unavailable"):
        delete.delete(str(result.vmid), expected=delete.plan(str(result.vmid)))
    assert runner.status[result.vmid] == "running"
    assert result.vmid in runner.vms


def test_clone_blocks_inherited_memory_secret(config: Configuration, runner: FakeRunner) -> None:
    creator, _, client, reservations = services(config, runner)
    result = creator.create(CreateRequest("example", "debian", "small", start=False))
    metadata = decode_metadata(runner.vms[result.vmid]["description"])
    metadata["system_features"] += ",agentmemory"
    runner.vms[result.vmid]["description"] = encode_metadata("", metadata)
    clone = CloneVMService(config, client, reservations, creator.allocator)
    with pytest.raises(VmctlError, match="private access key"):
        clone.plan(CloneRequest(str(result.vmid), "copy"))


def test_ssh_gateway_sends_credentials_only_on_stdin(
    config: Configuration, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import json

    from vmctl.agentmemory.client import GatewayClient

    ssh = Mock()
    stdin, stdout, stderr = Mock(), Mock(), Mock()
    stdout.read.return_value = b'{"ok":true}'
    stdout.channel.recv_exit_status.return_value = 0
    ssh.exec_command.return_value = stdin, stdout, stderr
    monkeypatch.setattr("vmctl.agentmemory.client.private_file", Mock())
    monkeypatch.setattr("vmctl.agentmemory.client.paramiko.SSHClient", Mock(return_value=ssh))
    client = GatewayClient(config, definition())
    caplog.set_level("DEBUG")
    client.request("grant", secret="DO-NOT-LOG")
    ssh.exec_command.assert_called_once_with("vmctl-memory-gateway", timeout=15)
    assert json.loads(stdin.write.call_args.args[0])["secret"] == "DO-NOT-LOG"
    assert "DO-NOT-LOG" not in str(ssh.connect.call_args)
    assert "DO-NOT-LOG" not in caplog.text
    assert ssh.connect.call_args.kwargs["allow_agent"] is False
    assert ssh.connect.call_args.kwargs["look_for_keys"] is False
    ssh.load_host_keys.assert_called_once()
    ssh.set_missing_host_key_policy.assert_called_once()


def test_gateway_failure_does_not_echo_response_or_credentials(
    config: Configuration, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vmctl.agentmemory.client import GatewayClient

    ssh = Mock()
    stdout = Mock()
    stdout.read.return_value = b'{"error":"PRIVATE-RESPONSE"}'
    stdout.channel.recv_exit_status.return_value = 1
    ssh.exec_command.return_value = Mock(), stdout, Mock()
    monkeypatch.setattr("vmctl.agentmemory.client.private_file", Mock())
    monkeypatch.setattr("vmctl.agentmemory.client.paramiko.SSHClient", Mock(return_value=ssh))
    with pytest.raises(VmctlError) as failure:
        GatewayClient(config, definition()).request("grant", secret="DO-NOT-PRINT")
    assert "DO-NOT-PRINT" not in str(failure.value)
    assert "PRIVATE-RESPONSE" not in str(failure.value)
    ssh.close.assert_called_once()


def test_preflight_checks_tls_and_denies_unauthenticated_requests(
    config: Configuration, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.error import HTTPError

    service = AgentmemoryService(configured(config), Mock())
    service.client, service.firewall = Mock(), Mock()
    monkeypatch.setattr("vmctl.agentmemory.service.ssl.create_default_context", Mock())
    request = Mock(side_effect=HTTPError(service.settings.endpoint, 401, "Unauthorized", {}, None))
    monkeypatch.setattr("vmctl.agentmemory.service.urlopen", request)
    service.preflight()
    assert request.call_args.args[0].endswith("/agentmemory/health")
    assert "context" in request.call_args.kwargs
    service.client.request.assert_called_once_with("preflight")
    service.firewall.grant.assert_not_called()
    request.side_effect = None
    request.return_value = MagicMock()
    with pytest.raises(VmctlError, match="must deny"):
        service.preflight()
