"""AI CLI provisioning stays opt-in and renders unprivileged guest operations."""

import getopt
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from vmctl.bootstrap.models import load_modules
from vmctl.bootstrap.renderer import CloudInitRenderer
from vmctl.bootstrap.resolver import resolve_modules
from vmctl.bootstrap.system import load_system_features, resolve_system_features
from vmctl.config import Configuration
from vmctl.errors import VmctlError
from vmctl.services.catalog import build_catalog
from vmctl.utils.subprocess import SubprocessRunner

AI_CLI = ("codex", "claude", "opencode", "gemini", "aider")


def rendered(
    config: Configuration,
    names: list[str],
    *,
    template: str = "ubuntu-server",
    versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    modules = resolve_modules(names, load_modules(config), config.profiles)
    data: dict[str, Any] = yaml.safe_load(
        CloudInitRenderer(config).render(
            config.template(template),
            "ai-test",
            "ssh-ed25519 key",
            modules,
            versions or {},
        )
    )
    return data


def run_script(data: dict[str, Any]) -> str:
    return next(file["content"] for file in data["write_files"] if file["path"].endswith("/run.sh"))


def test_ai_cli_group_and_minimal_shared_prerequisites(config: Configuration) -> None:
    modules = load_modules(config)
    assert all(modules[name].group == "ai-cli" for name in ("ai-tools", *AI_CLI))
    assert all(not modules[name].desktop_only for name in AI_CLI)
    assert modules["ai-tools"].dependencies == []
    assert modules["ai-tools"].implementation(config.template("ubuntu-server")).packages == [
        "ca-certificates",
        "curl",
        "git",
        "bash",
    ]
    assert all(modules[name].group == "development" for name in ("node", "python"))
    catalog = build_catalog(config)
    assert {item.name: item.group for item in catalog.modules}["codex"] == "ai-cli"


@pytest.mark.parametrize("name", ["codex", "opencode", "gemini"])
def test_npm_agents_require_node_and_use_per_user_prefix(config: Configuration, name: str) -> None:
    selected = resolve_modules([name], load_modules(config), config.profiles)
    assert [module.name for module in selected] == ["ai-tools", "base", "node", name]
    data = rendered(config, [name])
    installer = next(
        file["content"]
        for file in data["write_files"]
        if file["path"].endswith(f"development-{name}-0.sh")
    )
    assert 'npm install --global --prefix "$HOME/.local" --engine-strict' in installer
    assert "su -l -s /bin/sh -c" in installer
    assert '"$1@$2"' in installer
    assert "sudo npm" not in installer
    assert "npm config" not in installer
    assert "--userconfig=/dev/null" in installer
    assert "--registry=https://registry.npmjs.org/" in installer
    assert "--ignore-scripts" not in installer
    assert "--version" not in installer


@pytest.mark.parametrize("template", ["ubuntu-server", "rocky", "alpine"])
def test_native_claude_does_not_install_node_or_other_toolchains(
    config: Configuration, template: str
) -> None:
    selected = resolve_modules(["claude"], load_modules(config), config.profiles)
    assert [module.name for module in selected] == ["ai-tools", "claude"]
    assert load_modules(config)["claude"].default_version == "stable"
    data = rendered(config, ["claude"], template=template)
    run = run_script(data)
    assert "node-install" not in run and "development-node" not in run
    assert "development-python" not in run and "development-rust" not in run
    assert "development-claude-0.sh vmadmin stable" in run
    installer = next(
        file["content"]
        for file in data["write_files"]
        if file["path"].endswith("development-claude-0.sh")
    )
    assert "https://claude.ai/install.sh" in installer
    assert 'bash "$HOME/.vmctl/claude-install.sh" "$1"' in installer
    assert 'mv -f "$installer_temp" "$home_directory/.vmctl/claude-install.sh"' in installer
    assert "/root" not in installer
    if template == "alpine":
        assert "libgcc libstdc++ ripgrep" in run
        environment = next(
            file for file in data["write_files"] if "vmctl-claude.sh.vmctl" in file["path"]
        )
        assert environment["defer"] and environment["permissions"] == "0644"
        assert '"$(id -un)" = "vmadmin"' in environment["content"]
        assert "export USE_BUILTIN_RIPGREP=0" in environment["content"]
        assert "settings.json" not in environment["content"]
    else:
        assert not any("vmctl-claude.sh" in file["path"] for file in data["write_files"])


def test_aider_uses_uv_isolation_python312_and_pip(config: Configuration) -> None:
    selected = resolve_modules(["aider"], load_modules(config), config.profiles)
    assert [module.name for module in selected] == ["ai-tools", "base", "python", "aider"]
    data = rendered(config, ["aider"])
    installer = next(
        file["content"]
        for file in data["write_files"]
        if file["path"].endswith("development-aider-0.sh")
    )
    assert (
        '"$HOME/.local/bin/uv" tool install --force --python "$3" --with pip "$1@$2"' in installer
    )
    assert "aider-chat latest python3.12" in run_script(data)
    assert "pip install" not in installer and "--break-system-packages" not in installer


@pytest.mark.parametrize("template", ["ubuntu-server", "rocky"])
def test_composed_agents_deduplicate_shared_runtimes_and_guest_files(
    config: Configuration, template: str
) -> None:
    data = rendered(config, [*AI_CLI, "node", "python"], template=template)
    selected = resolve_modules([*AI_CLI, "node", "python"], load_modules(config), config.profiles)
    names = [module.name for module in selected]
    assert names.count("ai-tools") == names.count("node") == names.count("python") == 1
    files = data["write_files"]
    assert len({file["path"] for file in files}) == len(files)
    assert sum("vmctl-ai-tools.sh.vmctl" in file["path"] for file in files) == 1
    profile = next(file for file in files if "vmctl-ai-tools.sh.vmctl" in file["path"])
    assert profile["owner"] == "root:root" and profile["permissions"] == "0644" and profile["defer"]
    assert '"$(id -un)" = "vmadmin"' in profile["content"]
    assert 'case ":$PATH:"' in profile["content"]
    assert (
        "mv -fT -- /etc/profile.d/.vmctl-ai-tools.sh.vmctl-development-ai-tools-0 /etc/profile.d/vmctl-ai-tools.sh"
        in run_script(data)
    )


@pytest.mark.parametrize(
    ("module", "package", "version"),
    [
        ("codex", "@openai/codex", "0.128.0"),
        ("opencode", "@opencode/cli", "2.0.24"),
        ("gemini", "@google/gemini-cli", "0.28.0"),
        ("aider", "aider-chat", "0.86.2"),
    ],
)
def test_packages_versions_and_cloud_user_are_rendered_from_configuration(
    config: Configuration, module: str, package: str, version: str
) -> None:
    custom = config.model_copy(
        update={"host": config.host.model_copy(update={"username": "builder"})}
    )
    data = rendered(custom, [module], versions={module: version})
    lines = [shlex.split(line) for line in run_script(data).splitlines()]
    call = next(
        line for line in lines if len(line) > 1 and line[1].endswith(f"development-{module}-0.sh")
    )
    assert call[2:5] == ["builder", package, version]
    assert all("/home/vmadmin" not in file["content"] for file in data["write_files"])
    assert load_modules(config)[module].default_version == "latest"


@pytest.mark.parametrize("name", ["codex", "opencode", "gemini", "aider"])
def test_unvalidated_alpine_agent_combinations_fail_clearly(
    config: Configuration, name: str
) -> None:
    with pytest.raises(VmctlError, match="no implementation"):
        rendered(config, [name], template="alpine")
    catalog = build_catalog(config)
    assert "alpine" not in next(item.templates for item in catalog.modules if item.name == name)


def test_ai_bootstrap_is_opt_in_and_never_receives_credentials(config: Configuration) -> None:
    template = config.template("ubuntu-server")
    text = CloudInitRenderer(config).render(
        template,
        "clean-test",
        "ssh-ed25519 key",
        (),
        {},
        system_features=resolve_system_features(template, load_system_features(config)),
    )
    assert (
        "vmctl-ai-tools" not in text and "development-codex" not in text and "claude.ai" not in text
    )
    for profile in ("surge-dev", "full-dev"):
        assert not set(AI_CLI) & {
            module.name
            for module in resolve_modules([profile], load_modules(config), config.profiles)
        }
    ai_data = rendered(config, list(AI_CLI))
    all_text = json.dumps(ai_data)
    assert all(
        secret not in all_text
        for secret in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "auth.json")
    )
    assert all(
        flag not in all_text
        for flag in ("--dangerously-skip-permissions", "--yolo", "--no-sandbox")
    )
    assert "xrdp" not in all_text


@pytest.mark.parametrize(
    ("script", "arguments"),
    [
        ("ai-npm-install.sh", ["builder", "@openai/codex", "latest", "codex"]),
        ("ai-aider-install.sh", ["builder", "aider-chat", "latest", "python3.12"]),
        ("ai-claude-install.sh", ["builder", "stable"]),
    ],
)
@pytest.mark.parametrize("payload", ["$(touch /host-should-never-be-written)", "--help"])
def test_installers_pass_parameters_to_mock_su_without_shell_interpolation(
    config: Configuration,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: str,
    arguments: list[str],
    payload: str,
) -> None:
    # Only fake user/network/owner commands run; no installer or privileged shell executes.
    # This fake command runs; it records argv without executing an installer or user shell.
    commands = tmp_path / "fake-commands"
    commands.mkdir()
    su = commands / "su"
    su.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    su.chmod(0o755)
    if script == "ai-claude-install.sh":
        guest_directory = tmp_path / "guest-home"
        guest_directory.mkdir()
        programs = {
            "getent": f"print({str('builder:x:1000:1000::' + str(guest_directory) + ':/bin/sh')!r})",
            "install": "from pathlib import Path; import sys; Path(sys.argv[-1]).mkdir(parents=True, exist_ok=True)",
            "curl": "from pathlib import Path; import sys; Path(sys.argv[sys.argv.index('-o') + 1]).write_text('# Mock installer, never executed\\n')",
            "chown": "pass",
        }
        for name, program in programs.items():
            path = commands / name
            path.write_text(f"#!{sys.executable}\n{program}\n")
            path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{commands}{os.pathsep}{os.environ.get('PATH', '')}")
    arguments[1 if script == "ai-claude-install.sh" else 2] = payload
    result = SubprocessRunner().run(
        ["sh", str(config.root / "bootstrap/scripts" / script), *arguments]
    )
    received = json.loads(result.stdout)
    assert received[:4] == ["-l", "-s", "/bin/sh", "-c"]
    assert payload not in received[4]
    assert received[5:] == ["--", "builder", "sh", *arguments[1:]]

    options, operands = getopt.gnu_getopt(received, "ls:c:")
    assert [option for option, _ in options] == ["-l", "-s", "-c"]
    assert operands == ["builder", "sh", *arguments[1:]]
    # GNU/BusyBox su prepend -c COMMAND before these operands. Verify actual sh binding
    # with a harmless printf probe, without running su, login profiles or installers.
    bound = SubprocessRunner().run(["sh", "-c", 'printf "%s\\n" "$0" "$@"', *operands[1:]])
    assert bound.stdout.splitlines() == ["sh", *arguments[1:]]
