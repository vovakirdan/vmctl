"""Portable shell completion tests without SSH, host commands or profile writes."""

from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path

import pytest
import typer
from rich.ansi import AnsiDecoder
from typer.core import TyperCommand as Command
from typer.testing import CliRunner

from vmctl import cli, completion
from vmctl.client_config import initialize_client_config
from vmctl.config import Preset, Template
from vmctl.errors import VmctlError
from vmctl.models import VM, VMDetails
from vmctl.operations import Catalog, CatalogItem


@dataclass
class CompletionBackend:
    calls: list[str] = field(default_factory=list)

    def catalog(self) -> Catalog:
        self.calls.append("catalog")
        return Catalog(
            {
                "ubuntu-server": Template(
                    vmid=9000, family="debian", distro="ubuntu", release="noble"
                ),
                "ubuntu-desktop": Template(
                    vmid=9001, family="debian", distro="ubuntu", release="noble", desktop=True
                ),
                "alpine": Template(vmid=9020, family="alpine", distro="alpine", release="unknown"),
            },
            {"small": Preset(cpu=2, memory_mib=2048, disk_gib=20)},
            (
                CatalogItem("rust", "Rust toolchain", ("ubuntu-server", "ubuntu-desktop")),
                CatalogItem(
                    "node", "Node\nJavaScript runtime", ("ubuntu-server", "ubuntu-desktop")
                ),
            ),
            (CatalogItem("surge-dev", "rust, node", ("ubuntu-server",)),),
            (
                CatalogItem(
                    "qemu-agent", "Guest integration", ("ubuntu-server", "ubuntu-desktop", "alpine")
                ),
                CatalogItem("desktop-rdp", "Remote desktop", ("ubuntu-desktop",)),
            ),
            IPv4Address("10.210.0.100"),
            IPv4Address("10.210.0.199"),
        )

    def list(self) -> list[VMDetails]:
        self.calls.append("list")
        return [VMDetails(VM(104, "work", "running", "pve"), {}, {}, None)]


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> CompletionBackend:
    fake = CompletionBackend()

    def operations(ctx: typer.Context, *, completion: bool = False) -> CompletionBackend:
        assert ctx.obj is None if completion else isinstance(ctx.obj, cli.CLISettings)
        return fake

    monkeypatch.setattr(cli, "operations", operations)
    monkeypatch.setattr(
        cli.typer, "prompt", lambda *a, **kw: pytest.fail("Completion must not prompt")
    )
    return fake


def bash(words: str) -> dict[str, str]:
    return {
        "_VMCTL_COMPLETE": "complete_bash",
        "COMP_WORDS": words,
        "COMP_CWORD": str(len(words.split()) - 1),
    }


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "powershell", "pwsh"])
def test_dynamic_templates_in_shell_protocols(backend: CompletionBackend, shell: str) -> None:
    words = "vmctl create testvm ubu"
    env = (
        bash(words)
        if shell == "bash"
        else {
            "_VMCTL_COMPLETE": f"complete_{shell}",
            "_TYPER_COMPLETE_ARGS": words,
            "_TYPER_COMPLETE_FISH_ACTION": "get-args",
            "_TYPER_COMPLETE_WORD_TO_COMPLETE": "ubu",
        }
    )
    result = CliRunner().invoke(cli.app, [], prog_name="vmctl", env=env)
    assert result.exit_code == 0, result.output
    assert "ubuntu-server" in result.output and "ubuntu-desktop" in result.output
    assert "alpine" not in result.output and result.stderr == ""
    assert backend.calls == ["catalog"]


@pytest.mark.parametrize(
    "words,expected,operation",
    [
        ("vmctl create testvm ubuntu-server sm", "small", "catalog"),
        ("vmctl create testvm ubuntu-server small --with rust,no", "rust,node", "catalog"),
        ("vmctl create testvm ubuntu-server small --with=rust,no", "rust,node", "catalog"),
        ("vmctl create testvm ubuntu-server small --with sur", "surge-dev", "catalog"),
        ("vmctl create testvm ubuntu-desktop small --with-system des", "desktop-rdp", "catalog"),
        ("vmctl create testvm ubuntu-desktop small --without-system des", "desktop-rdp", "catalog"),
        ("vmctl create testvm ubuntu-server small --ip 10.210.0.19", "10.210.0.199", "catalog"),
        ("vmctl create testvm ubuntu-server small --cpu 2", "2", "catalog"),
        ("vmctl create testvm ubuntu-server small --memory 2", "2G", "catalog"),
        ("vmctl create testvm ubuntu-server small --disk 2", "20G", "catalog"),
        ("vmctl info wo", "work", "list"),
        ("vmctl delete 10", "104", "list"),
        ("vmctl bootstrap --template ubu", "ubuntu-server", "catalog"),
    ],
)
def test_argument_and_option_values(
    backend: CompletionBackend, words: str, expected: str, operation: str
) -> None:
    result = CliRunner().invoke(cli.app, [], prog_name="vmctl", env=bash(words))
    assert result.exit_code == 0, result.output
    assert expected in result.output and result.stderr == ""
    assert backend.calls == [operation]


def test_comma_deduplication_and_template_compatibility(backend: CompletionBackend) -> None:
    ctx = typer.Context(Command("create"))
    ctx.params = {"template": "alpine"}
    assert completion.development(ctx, "no") == []
    assert completion.system(ctx, "des") == []
    assert completion.without_system(ctx, "des") == [("desktop-rdp", "Remote desktop")]
    ctx.params = {"template": "ubuntu-server"}
    assert completion.development(ctx, "rust,r") == []
    assert completion.development(ctx, "rust,no") == [("rust,node", "Node JavaScript runtime")]


def test_globals_resolved_without_root_callback(tmp_path: Path) -> None:
    root = typer.Context(Command("vmctl"))
    root.params = {"config_dir": str(tmp_path), "local": True}
    ctx = typer.Context(Command("create"), parent=root)
    assert cli.settings_for(ctx) == cli.CLISettings(tmp_path, True)
    root.params = {"local": False}
    assert not cli.settings_for(ctx).local
    assert cli.settings_for(ctx).directory != Path("/etc/vmctl")


def test_completion_ssh_timeout_and_configurable_alias(tmp_path: Path) -> None:
    initialize_client_config(tmp_path)
    path = tmp_path / "client.toml"
    path.write_text(path.read_text().replace('host = "pve"', 'host = "pxmx"'))
    ctx = typer.Context(Command("vmctl"))
    ctx.params = {"config_dir": str(tmp_path), "local": False}
    transport = cli.operations(ctx, completion=True).transport
    assert transport.config.host == "pxmx"
    assert transport.config.connect_timeout == 3 and transport.config.operation_timeout == 3
    assert cli.operations(ctx).transport.config.operation_timeout == 3600


def test_offline_dynamic_completion_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(*args: object, **kwargs: object) -> None:
        raise VmctlError("SSH connection timed out")

    monkeypatch.setattr(cli, "operations", offline)
    runner = CliRunner()
    for words in ("vmctl create testvm ubu", "vmctl info wo"):
        result = runner.invoke(cli.app, [], prog_name="vmctl", env=bash(words))
        assert result.exit_code == 0 and not result.output.strip() and not result.stderr
    result = runner.invoke(
        cli.app, [], prog_name="vmctl", env=bash("vmctl create testvm ubuntu-server small --with-s")
    )
    assert "--with-system" in result.output


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "powershell", "pwsh"])
def test_emitting_completion_script_requires_no_configuration(shell: str) -> None:
    result = CliRunner().invoke(
        cli.app, [], prog_name="vmctl", env={"_VMCTL_COMPLETE": f"source_{shell}"}
    )
    assert result.exit_code == 0 and "vmctl" in result.output
    if shell == "bash":
        # macOS ships Bash 3.2; Typer still emits the script with a version warning.
        assert result.stderr in (
            "",
            "Shell completion is not supported for Bash versions older than 4.4.\n",
            "Couldn't detect Bash version, shell completion is not supported.\n",
        )
    else:
        assert not result.stderr


def test_bootstrap_discovery_and_help(backend: CompletionBackend) -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["bootstrap", "--template", "alpine"])
    assert result.exit_code == 0, result.output
    assert "qemu-agent" in result.output and "desktop-rdp" not in result.output
    assert "rust" not in result.output and "node" not in result.output
    result = runner.invoke(cli.app, ["bootstrap", "--template", "invalid"])
    assert result.exit_code == 1 and "Unknown template" in result.output
    result = runner.invoke(cli.app, ["create", "--help"], terminal_width=140)
    output = "\n".join(line.plain for line in AnsiDecoder().decode(result.output))
    for text in (
        "--start",
        "--no-start",
        "powered",
        "--with-system",
        "--without-system",
        "Dependencies",
        "shrinks",
        "--dry-run",
        "--wait",
    ):
        assert text in output
