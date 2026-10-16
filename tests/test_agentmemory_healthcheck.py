"""The timer recovers failed children without starting a deliberately stopped server."""

import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, Mock
from urllib.error import URLError

import pytest


def module() -> ModuleType:
    path = Path(__file__).parents[1] / "ops/agentmemory/healthcheck.py"
    specification = importlib.util.spec_from_file_location("vmctl_memory_healthcheck", path)
    assert specification is not None and specification.loader is not None
    result = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(result)
    return result


def test_stopped_service_is_never_started() -> None:
    check = module()
    check.active = Mock(return_value=False)
    check.read_secret = Mock(side_effect=AssertionError("must not inspect secrets"))
    check.subprocess = Mock()
    assert check.main() == 0
    check.subprocess.run.assert_not_called()


def test_healthy_or_transient_failure_needs_no_restart() -> None:
    check = module()
    check.active = Mock(return_value=True)
    check.read_secret = Mock(return_value="PRIVATE")
    check.healthy = Mock(side_effect=[False, True])
    check.time = Mock()
    check.subprocess = Mock()
    assert check.main() == 0
    assert check.healthy.call_count == 2
    check.subprocess.run.assert_not_called()


def test_three_failures_request_only_active_service_restart(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = module()
    check.active = Mock(return_value=True)
    check.read_secret = Mock(return_value="PRIVATE")
    check.healthy = Mock(return_value=False)
    check.time = Mock()
    runner = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(check.subprocess, "run", runner)
    assert check.main() == 0
    assert check.healthy.call_count == 3
    assert check.active.call_count == 2
    assert check.time.sleep.call_count == 2
    assert runner.call_args.args[0] == ["systemctl", "try-restart", "agentmemory.service"]
    assert "PRIVATE" not in str(runner.call_args)
    assert "PRIVATE" not in capsys.readouterr().out


def test_deliberate_stop_during_health_attempts_is_respected() -> None:
    check = module()
    check.active = Mock(side_effect=[True, False])
    check.read_secret = Mock(return_value="PRIVATE")
    check.healthy = Mock(return_value=False)
    check.time = Mock()
    check.subprocess = Mock()
    assert check.main() == 0
    check.subprocess.run.assert_not_called()


def test_secret_is_in_header_only_and_proxy_is_disabled() -> None:
    check = module()
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.read.return_value = b'{"status":"healthy"}'
    opener = Mock()
    opener.open.return_value = response
    check.build_opener = Mock(return_value=opener)
    check.ProxyHandler = Mock(return_value="proxy-disabled")
    assert check.healthy("PRIVATE") is True
    request = opener.open.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:3111/agentmemory/health"
    assert request.get_header("Authorization") == "Bearer PRIVATE"
    check.ProxyHandler.assert_called_once_with({})
    check.build_opener.assert_called_once_with("proxy-disabled")
    opener.open.side_effect = URLError("PRIVATE")
    assert check.healthy("PRIVATE") is False


def test_unsafe_secret_does_not_restart_loop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    check = module()
    check.active = Mock(return_value=True)
    check.SECRET_PATH = tmp_path / "secret"
    check.SECRET_PATH.write_text("PRIVATE")
    check.SECRET_PATH.chmod(0o644)
    check.ROOT_UID = os.getuid() if hasattr(os, "getuid") else 0
    check.subprocess = Mock()
    assert check.main() == 1
    check.subprocess.run.assert_not_called()
    assert "PRIVATE" not in capsys.readouterr().out


def test_restart_timeout_does_not_echo_exception(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = module()
    check.active = Mock(return_value=True)
    check.read_secret = Mock(return_value="PRIVATE")
    check.healthy = Mock(return_value=False)
    check.time = Mock()
    monkeypatch.setattr(
        check.subprocess,
        "run",
        Mock(side_effect=subprocess.TimeoutExpired("systemctl", 10, output=b"PRIVATE")),
    )
    assert check.main() == 1
    assert "PRIVATE" not in capsys.readouterr().out


def test_units_have_bounded_periodic_checks_without_auto_start_dependencies() -> None:
    root = Path(__file__).parents[1] / "ops/agentmemory"
    service = (root / "vmctl-agentmemory-healthcheck.service").read_text()
    timer = (root / "vmctl-agentmemory-healthcheck.timer").read_text()
    assert "Type=oneshot" in service and "TimeoutStartSec=45" in service
    assert "Wants=agentmemory" not in service and "Requires=agentmemory" not in service
    assert "Restart=" not in service
    assert "OnBootSec=90s" in timer and "OnUnitActiveSec=30s" in timer
