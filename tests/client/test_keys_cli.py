"""Public-key commands stay local until an explicit guest export is requested."""

import base64
import struct
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from test_remote_cli import FrontendBackend
from typer.testing import CliRunner

from vmctl import cli, completion
from vmctl.client_config import initialize_client_config
from vmctl.frontend import FrontendSettings, key_registry
from vmctl.keys import PublicKeyRegistryStore
from vmctl.models import VM, CreateRequest, KeyExportRequest
from vmctl.operations import CreatePreview, KeyExportPreview, KeyExportResult, Progress


def public_key(seed: int) -> str:
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes([seed]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " test\n"


@pytest.fixture
def registry(tmp_path: Path) -> tuple[list[str], PublicKeyRegistryStore]:
    initialize_client_config(tmp_path)
    path = tmp_path / "client.toml"
    path.write_text(path.read_text().replace('"~/.ssh/id_ed25519.pub"', '"primary.pub"'))
    (tmp_path / "primary.pub").write_text(public_key(1))
    store = key_registry(FrontendSettings(tmp_path))
    store.add("iphone", public_key(2), description="Personal phone", default=True)
    return ["--config-dir", str(tmp_path)], store


@dataclass
class ExportBackend:
    calls: list[str] = field(default_factory=list)

    def plan_key_export(self, request: KeyExportRequest) -> KeyExportPreview:
        self.calls.append("plan")
        return KeyExportPreview(VM(103, "memory-vm", "running", "pve"), request, "f" * 64)

    def export_keys(self, expected: KeyExportPreview, *, progress: Progress) -> KeyExportResult:
        self.calls.append("export")
        return KeyExportResult(expected.vm, "vmadmin", 1, 0, ())


@pytest.mark.parametrize(
    "extra,calls", [(["--dry-run"], ["plan"]), (["--yes"], ["plan", "export"]), ([], ["plan"])]
)
def test_export_preview_confirmation_and_apply(
    registry: tuple[list[str], PublicKeyRegistryStore],
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
    calls: list[str],
) -> None:
    prefix, _ = registry
    backend = ExportBackend()
    monkeypatch.setattr(cli, "operations", lambda _: backend)
    result = CliRunner().invoke(
        cli.app, [*prefix, "keys", "export", "memory-vm", "iphone", *extra], input="n\n"
    )
    assert backend.calls == calls
    if extra:
        assert result.exit_code == 0, result.output
    else:
        assert result.exit_code != 0


def test_registry_commands_are_offline_and_private_keys_are_rejected(
    registry: tuple[list[str], PublicKeyRegistryStore],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prefix, store = registry
    monkeypatch.setattr(
        cli, "operations", lambda _: pytest.fail("Registry commands must be offline")
    )
    listed = CliRunner().invoke(cli.app, [*prefix, "keys", "list"])
    assert listed.exit_code == 0 and "iphone" in listed.output
    shown = CliRunner().invoke(cli.app, [*prefix, "keys", "show", "iphone"])
    assert shown.exit_code == 0 and shown.output == public_key(2)
    private = tmp_path / "private"
    private.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nDO_NOT_PRINT_ME\n")
    failed = CliRunner().invoke(cli.app, [*prefix, "keys", "add", "bad", str(private)])
    assert failed.exit_code != 0 and "DO_NOT_PRINT_ME" not in failed.output
    assert tuple(store.load().keys) == ("iphone",)
    unknown = CliRunner().invoke(cli.app, [*prefix, "keys", "export", "memory-vm", "unknown"])
    assert unknown.exit_code != 0 and "Unknown" in unknown.output


@pytest.mark.parametrize(
    "options,expected", [([], True), (["--key", "iphone"], True), (["--no-extra-keys"], False)]
)
def test_create_named_keys_preserve_primary(
    registry: tuple[list[str], PublicKeyRegistryStore],
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
    expected: bool,
) -> None:
    class Backend(FrontendBackend):
        def plan_create(self, request: CreateRequest) -> CreatePreview:
            assert request.public_key is not None
            assert public_key(1).strip() in request.public_key
            assert (public_key(2).strip() in request.public_key) is expected
            return super().plan_create(request)

    prefix, _ = registry
    monkeypatch.setattr(cli, "operations", lambda _: Backend(desktop=False))
    result = CliRunner().invoke(
        cli.app, [*prefix, "create", "work", "ubuntu-server", "small", "--dry-run", *options]
    )
    assert result.exit_code == 0, result.output


def test_key_name_completion_does_not_connect(
    registry: tuple[list[str], PublicKeyRegistryStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer import Context
    from typer.core import TyperCommand

    prefix, _ = registry
    ctx = Context(TyperCommand("create"), obj=cli.CLISettings(Path(prefix[1])))
    monkeypatch.setattr(
        cli, "operations", lambda *_args, **_kwargs: pytest.fail("No completion SSH")
    )
    assert completion.ssh_keys(ctx, "iph") == [("iphone", "Personal phone")]
