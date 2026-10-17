"""Gateway authorizes both key and source and rolls back failed reloads."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from vmctl.agentmemory import gateway

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only server helper")

TOKEN = "a" * 32
CLIENT = "vmctl-104-" + TOKEN
SECRET = "S" * 64
BACKEND = "B" * 64


@pytest.fixture(autouse=True)
def root_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "ROOT_UID", os.getuid())


def settings(tmp_path: Path) -> dict[str, str]:
    secret = tmp_path / "backend"
    secret.write_text(BACKEND)
    secret.chmod(0o600)
    return {
        "endpoint_host": "10.210.0.107",
        "backend_secret_file": str(secret),
        "clients_file": str(tmp_path / "clients.json"),
        "caddy_file": str(tmp_path / "Caddyfile"),
    }


def test_authorization_requires_both_source_and_key(tmp_path: Path) -> None:
    value = settings(tmp_path)
    clients = {CLIENT: {"owner": "vmctl:" + TOKEN, "address": "10.210.0.108", "secret": SECRET}}
    rendered = gateway.render(value, clients, BACKEND).decode()
    assert "remote_ip 10.210.0.108/32" in rendered
    assert f'header Authorization "Bearer {SECRET}"' in rendered
    assert f'header_up Authorization "Bearer {BACKEND}"' in rendered
    assert 'respond "Unauthorized" 401' in rendered
    assert "log" not in rendered
    assert "127.0.0.1:3111" in rendered
    assert "stream_timeout" not in rendered
    assert "request_body" not in rendered


def test_empty_registry_denies_everyone(tmp_path: Path) -> None:
    rendered = gateway.render(settings(tmp_path), {}, BACKEND).decode()
    assert "reverse_proxy" not in rendered
    assert 'respond "Unauthorized" 401' in rendered


def test_grant_is_owned_idempotent_and_revoke_retains_backend_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = settings(tmp_path)
    monkeypatch.setattr(gateway, "backend_ready", Mock())
    apply = Mock()
    monkeypatch.setattr(gateway, "apply", apply)
    request = {
        "operation": "grant",
        "client_id": CLIENT,
        "owner": "vmctl:" + TOKEN,
        "address": "10.210.0.108",
        "secret": SECRET,
    }
    gateway.process(value, request)
    clients = apply.call_args.args[1]
    assert clients[CLIENT]["secret"] == SECRET
    state = Path(value["clients_file"])
    state.write_text(json.dumps(clients))
    state.chmod(0o600)
    gateway.process(value, request)
    with pytest.raises(gateway.GatewayError, match="replace"):
        gateway.process(value, {**request, "secret": "N" * 64})
    with pytest.raises(gateway.GatewayError, match="Rollback"):
        gateway.process(
            value,
            {
                "operation": "revoke",
                "client_id": CLIENT,
                "owner": "vmctl:" + TOKEN,
                "secret": "N" * 64,
            },
        )
    gateway.process(value, {"operation": "revoke", "client_id": CLIENT, "owner": "vmctl:" + TOKEN})
    assert apply.call_args.args[1] == {}
    assert Path(value["backend_secret_file"]).read_text() == BACKEND


def test_revoke_works_when_backend_is_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = settings(tmp_path)
    health = Mock(side_effect=AssertionError("revoke must not require backend"))
    monkeypatch.setattr(gateway, "backend_ready", health)
    monkeypatch.setattr(gateway, "apply", Mock())
    gateway.process(value, {"operation": "revoke", "client_id": CLIENT, "owner": "vmctl:" + TOKEN})
    health.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("client_id", "other"),
        ("owner", "vmctl:" + "b" * 32),
        ("secret", "secret\nunsafe"),
        ("address", "10.210.0.108\n}"),
    ],
)
def test_untrusted_request_values_cannot_generate_caddy_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    monkeypatch.setattr(gateway, "backend_ready", Mock())
    apply = Mock()
    monkeypatch.setattr(gateway, "apply", apply)
    request = {
        "operation": "grant",
        "client_id": CLIENT,
        "owner": "vmctl:" + TOKEN,
        "address": "10.210.0.108",
        "secret": SECRET,
    }
    with pytest.raises((gateway.GatewayError, ValueError)):
        gateway.process(settings(tmp_path), {**request, field: value})
    apply.assert_not_called()


def test_failed_reload_restores_both_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = settings(tmp_path)
    state, caddy = Path(value["clients_file"]), Path(value["caddy_file"])
    state.write_bytes(b"{}")
    caddy.write_bytes(b"original")
    monkeypatch.setattr("grp.getgrnam", lambda _: Mock(gr_gid=os.getgid()))
    commands = Mock(side_effect=[None, gateway.GatewayError("reload failed"), None])
    monkeypatch.setattr(gateway, "run", commands)
    with pytest.raises(gateway.GatewayError, match="inspect server"):
        gateway.apply(value, {}, BACKEND)
    assert state.read_bytes() == b"{}" and caddy.read_bytes() == b"original"
    assert state.stat().st_mode & 0o777 == 0o600
    assert caddy.stat().st_mode & 0o777 == 0o640
    assert len(commands.call_args_list) == 3
    assert SECRET not in str(commands.call_args_list)


def test_invalid_candidate_does_not_replace_live_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = settings(tmp_path)
    state, caddy = Path(value["clients_file"]), Path(value["caddy_file"])
    state.write_bytes(b"{}")
    caddy.write_bytes(b"original")
    monkeypatch.setattr("grp.getgrnam", lambda _: Mock(gr_gid=os.getgid()))
    monkeypatch.setattr(gateway, "run", Mock(side_effect=gateway.GatewayError("invalid")))
    with pytest.raises(gateway.GatewayError):
        gateway.apply(value, {}, BACKEND)
    assert state.read_bytes() == b"{}" and caddy.read_bytes() == b"original"


def test_reload_timeout_restores_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = settings(tmp_path)
    state, caddy = Path(value["clients_file"]), Path(value["caddy_file"])
    state.write_bytes(b"{}")
    caddy.write_bytes(b"original")
    monkeypatch.setattr("grp.getgrnam", lambda _: Mock(gr_gid=os.getgid()))
    commands = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0),
            subprocess.TimeoutExpired("systemctl", 30, output=b"PRIVATE-OUTPUT"),
            subprocess.CompletedProcess([], 0),
        ]
    )
    monkeypatch.setattr(gateway.subprocess, "run", commands)
    with pytest.raises(gateway.GatewayError) as failure:
        gateway.apply(value, {}, BACKEND)
    assert state.read_bytes() == b"{}" and caddy.read_bytes() == b"original"
    assert commands.call_count == 3
    assert "PRIVATE-OUTPUT" not in str(failure.value)


def test_workstation_has_its_own_identity_and_pinned_source(tmp_path: Path) -> None:
    client_id = "workstation-zov-wsl-" + TOKEN
    gateway.validate_identity(client_id, "workstation:" + TOKEN)
    rendered = gateway.render(
        settings(tmp_path),
        {client_id: {"owner": "workstation:" + TOKEN, "address": "10.200.0.2", "secret": SECRET}},
        BACKEND,
    ).decode()
    assert "remote_ip 10.200.0.2/32" in rendered
    assert f'header Authorization "Bearer {SECRET}"' in rendered
    with pytest.raises(gateway.GatewayError):
        gateway.validate_identity(client_id, "vmctl:" + TOKEN)
    with pytest.raises(gateway.GatewayError):
        gateway.validate_identity(CLIENT, "workstation:" + TOKEN)


@pytest.mark.parametrize("name", ["../escape", "a\nreverse_proxy", "", "x" * 33, "HOST"])
def test_invalid_workstation_name_is_rejected(name: str) -> None:
    with pytest.raises(gateway.GatewayError):
        gateway.validate_identity("workstation-" + name + "-" + TOKEN, "workstation:" + TOKEN)
