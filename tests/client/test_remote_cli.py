"""Frontend interactions against a portable fake of the operations contract."""

import base64
import struct
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from vmctl import cli
from vmctl.config import Preset, Template
from vmctl.errors import VmctlError
from vmctl.models import VM, CreateRequest, CreateResult, Resources, VMDetails
from vmctl.operations import CreatePreview, DeletePreview, Progress, ValidationSummary


@dataclass
class FrontendBackend:
    desktop: bool = True
    requests: list[CreateRequest] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)

    def plan_create(self, request: CreateRequest) -> CreatePreview:
        return CreatePreview(
            9001 if self.desktop else 9000,
            Resources(4, 8192, 40),
            "scsi0",
            10,
            "vmbr1",
            "deskuser",
            (),
            ("qemu-agent", "desktop-rdp") if self.desktop else ("qemu-agent",),
            self.desktop,
            self.desktop and not request.no_desktop_password,
        )

    def create(
        self, request: CreateRequest, *, request_id: str, progress: Progress
    ) -> CreateResult:
        self.requests.append(request)
        self.ids.append(request_id)
        progress("Clone complete")
        return CreateResult(
            104,
            request.name,
            request.template,
            request.preset,
            Resources(4, 8192, 40),
            "BC:24:11:AA:BB:CC",
            IPv4Address("10.210.0.100"),
            "deskuser",
            True,
            (),
            "not checked",
            ("qemu-agent", "desktop-rdp") if self.desktop else ("qemu-agent",),
            self.desktop,
            self.desktop,
        )

    def plan_delete(self, reference: str) -> DeletePreview:
        return DeletePreview(VM(104, "work", "running", "pve"), "f" * 64)

    def delete(self, expected: DeletePreview) -> int:
        return expected.vm.vmid

    def list(self) -> list[VMDetails]:
        return [self.info("104")]

    def info(self, reference: str) -> VMDetails:
        return VMDetails(
            VM(104, "work", "running", "pve"),
            {},
            {
                "desktop": "true" if self.desktop else "false",
                "system_features": "qemu-agent,desktop-rdp" if self.desktop else "qemu-agent",
                "username": "deskuser",
            },
            IPv4Address("10.210.0.100"),
        )

    def templates(self) -> dict[str, Template]:
        return {}

    def presets(self) -> dict[str, Preset]:
        return {}

    def validate_config(self) -> ValidationSummary:
        return ValidationSummary(5, 4, 2, 6, 1)


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], FrontendBackend]:
    from vmctl.client_config import initialize_client_config

    initialize_client_config(tmp_path)
    key = tmp_path / "workstation.pub"
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes(range(32))
    key.write_text("ssh-ed25519 " + base64.b64encode(blob).decode() + " workstation\n")
    backend = FrontendBackend()
    monkeypatch.setattr(cli, "operations", lambda _: backend)
    return [
        "--config-dir",
        str(tmp_path),
        "create",
        "work",
        "ubuntu-desktop",
        "normal",
        "--ssh-key",
        str(key),
    ], backend


@pytest.mark.parametrize(
    "desktop,options,prompted",
    [
        (True, [], True),
        (False, [], False),
        (True, ["--dry-run"], False),
        (True, ["--no-desktop-password"], False),
        (True, ["--no-start"], True),
    ],
)
def test_local_password_prompt_respects_server_plan(
    setup, monkeypatch: pytest.MonkeyPatch, desktop: bool, options: list[str], prompted: bool
) -> None:
    args, backend = setup
    backend.desktop = desktop
    prompts: list[dict[str, Any]] = []

    def prompt(text: str, **kwargs: Any) -> str:
        assert text == "Desktop password for deskuser"
        prompts.append(kwargs)
        return "unique-desktop-password"

    monkeypatch.setattr(cli.typer, "prompt", prompt)
    result = CliRunner().invoke(cli.app, args + options)
    assert result.exit_code == 0, result.output
    assert bool(prompts) is prompted
    assert "unique-desktop-password" not in result.output
    if prompted:
        assert prompts[0]["hide_input"] and prompts[0]["confirmation_prompt"]
        assert backend.requests[-1].desktop_password_hash is not None
        assert not hasattr(backend.requests[-1], "ssh_key")
    if "--dry-run" not in options:
        assert bool("RDP:" in result.output) is desktop
        assert len(backend.ids[0]) == 32


def test_rdp_info_only_for_desktop(setup: tuple[list[str], FrontendBackend]) -> None:
    args, backend = setup
    for desktop in (False, True):
        backend.desktop = desktop
        result = CliRunner().invoke(cli.app, args[:2] + ["info", "104"])
        assert result.exit_code == 0
        assert ("3389" in result.output) is desktop


def test_no_password_prompt_after_preflight_failure(
    setup: tuple[list[str], FrontendBackend], monkeypatch: pytest.MonkeyPatch
) -> None:
    args, backend = setup

    def fail(request: CreateRequest) -> CreatePreview:
        raise VmctlError("Unsupported desktop feature for this OS")

    monkeypatch.setattr(backend, "plan_create", fail)
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **kw: pytest.fail("Do not prompt"))
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 1 and "Unsupported" in result.output
    assert not backend.requests
