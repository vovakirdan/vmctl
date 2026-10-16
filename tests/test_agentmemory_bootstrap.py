"""Private guest bootstrap and non-destructive agent configuration merges."""

import importlib.util
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest
import yaml

from vmctl.agentmemory.bootstrap import inject_access
from vmctl.agentmemory.config import AgentmemorySettings
from vmctl.agentmemory.service import MemoryAccess
from vmctl.bootstrap.renderer import CloudInitRenderer
from vmctl.bootstrap.system import load_system_features, resolve_system_features
from vmctl.config import Configuration
from vmctl.errors import VmctlError


def settings() -> AgentmemorySettings:
    return AgentmemorySettings(
        endpoint="https://10.210.0.106",
        ca_file="/ca.pem",
        management_host="10.210.0.106",
        management_key_file="/key",
        management_known_hosts_file="/known",
        firewall_vmid=106,
    )


def configure_module() -> ModuleType:
    script = Path(__file__).parents[1] / "config/bootstrap/scripts/agentmemory-configure.py"
    spec = importlib.util.spec_from_file_location("guest_memory_configure", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_assets(home: Path) -> Path:
    root = home / ".local/share/vmctl/agentmemory"
    plugin = root / "client/node_modules/@agentmemory/agentmemory/plugin"
    (plugin / "hooks").mkdir(parents=True)
    manifest = {
        "hooks": {
            "PostToolUse": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'node "${CLAUDE_PLUGIN_ROOT}/scripts/post-tool-use.mjs"',
                        }
                    ]
                }
            ]
        }
    }
    for name in ("hooks.json", "hooks.codex.json"):
        (plugin / "hooks" / name).write_text(json.dumps(manifest))
    (plugin / "skills/remember").mkdir(parents=True)
    (plugin / "skills/remember/SKILL.md").write_text("Memory instruction")
    (root / "opencode.ts").write_text("export default async () => ({});\n")
    return root


def test_guest_access_is_private_and_configured_after_all_modules(config: Configuration) -> None:
    feature = load_system_features(config)["agentmemory"]
    rendered = CloudInitRenderer(config).render(
        config.template("debian"),
        "test",
        "ssh-ed25519 test",
        [],
        {},
        system_features=[feature],
    )
    access = MemoryAccess("client", settings().endpoint, "owner", "never-log-this-secret")
    document = yaml.safe_load(
        inject_access(
            rendered,
            access,
            settings(),
            "vmadmin",
            "TEST CA",
            config,
        )
    )
    secret_file = next(item for item in document["write_files"] if "access.json" in item["path"])
    assert secret_file["permissions"] == "0600"
    assert secret_file["owner"] == "vmadmin:vmadmin"
    assert secret_file["defer"] is True
    assert json.loads(secret_file["content"])["secret"] == access.secret
    assert access.secret not in str(document["runcmd"])
    assert access.secret not in repr(access)
    assert document["runcmd"][-1][-1].endswith("configure.py")
    launcher = next(
        item["content"]
        for item in document["write_files"]
        if item["path"].endswith(".run.vmctl-pending")
    )
    assert "plugin-bridge.mjs" in launcher
    assert "standalone.mjs" not in launcher
    assert '"AGENTMEMORY_INJECT_CONTEXT": "false"' in launcher
    assert '"AGENTMEMORY_CAPTURE_SPOOL_MAX_BYTES": "10485760"' in launcher


def test_feature_is_opt_in_and_has_no_development_node_dependency(config: Configuration) -> None:
    features = load_system_features(config)
    template = config.template("debian")
    assert "agentmemory" not in {
        value.name for value in resolve_system_features(template, features)
    }
    selected = resolve_system_features(template, features, ["agentmemory"])
    assert features["agentmemory"].dependencies == []
    assert {value.name for value in selected} == {"qemu-agent", "agentmemory"}
    with pytest.raises(VmctlError, match="does not support"):
        resolve_system_features(config.template("alpine"), features, ["agentmemory"])


def test_agent_configuration_is_merged_and_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_assets(home)
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(
        'model = "custom"\n[mcp_servers.other]\ncommand = "other"\n'
    )
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text('{"mcpServers":{"other":{"command":"other"}}}')
    (home / ".claude/settings.json").write_text('{"permissions":{"allow":["Read"]}}')
    (home / ".config/opencode").mkdir(parents=True)
    (home / ".config/opencode/opencode.json").write_text(
        '{"model":"custom","mcp":{"other":{"type":"remote"}}}'
    )
    module = configure_module()
    with patch.object(module.shutil, "which", return_value=None):
        module.configure(home)
        first = {
            path.relative_to(home): path.read_bytes() for path in home.rglob("*") if path.is_file()
        }
        module.configure(home)
    second = {
        path.relative_to(home): path.read_bytes() for path in home.rglob("*") if path.is_file()
    }
    assert first == second
    codex = tomllib.loads((home / ".codex/config.toml").read_text())
    assert codex["model"] == "custom"
    assert codex["mcp_servers"]["other"]["command"] == "other"
    assert codex["mcp_servers"]["agentmemory"]["args"] == ["mcp"]
    claude = json.loads((home / ".claude/settings.json").read_text())
    assert claude["permissions"] == {"allow": ["Read"]}
    assert len(claude["hooks"]["PostToolUse"]) == 1
    assert "trust" not in codex
    assert "other" in json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert (home / ".config/opencode/plugins/vmctl-agentmemory.ts").exists()
    assert (home / ".agents/skills/remember/SKILL.md").exists()


def test_native_plugin_does_not_duplicate_capture(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_assets(home)
    (home / ".claude").mkdir()
    original = {"enabledPlugins": {"agentmemory@agentmemory": True}, "hooks": {"Stop": []}}
    (home / ".claude/settings.json").write_text(json.dumps(original))
    module = configure_module()
    with patch.object(module.shutil, "which", return_value=None):
        module.configure(home)
    assert json.loads((home / ".claude/settings.json").read_text()) == original


def test_no_agents_means_no_agent_configuration(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_assets(home)
    module = configure_module()
    with patch.object(module.shutil, "which", return_value=None):
        module.configure(home)
    assert not (home / ".codex").exists()
    assert not (home / ".claude.json").exists()


def test_malformed_configuration_is_preserved(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_assets(home)
    (home / ".claude").mkdir()
    path = home / ".claude.json"
    path.write_text("not JSON")
    module = configure_module()
    with patch.object(module.shutil, "which", return_value=None), pytest.raises(ValueError):
        module.configure(home)
    assert path.read_text() == "not JSON"


def test_installer_reuses_runtime_and_does_not_install_global_node() -> None:
    script = Path(__file__).parents[1] / "config/bootstrap/scripts/agentmemory-install.sh"
    contents = script.read_text()
    assert "Number(process.versions.node.split" in contents
    assert '--prefix "$root/client"' in contents
    assert "--omit=optional --ignore-scripts" in contents
    assert "root/runtime" in contents
    assert "ln -sf" not in contents
    subprocess.run(["sh", "-n", str(script)], check=True)


def test_no_server_fallback_when_remote_is_down(config: Configuration, tmp_path: Path) -> None:
    data = "#cloud-config\nwrite_files: []\nruncmd: []\n"
    access = MemoryAccess("client", settings().endpoint, "owner", "secret")
    document = yaml.safe_load(inject_access(data, access, settings(), "vmadmin", "CA", config))
    launcher = next(
        item["content"]
        for item in document["write_files"]
        if item["path"].endswith(".run.vmctl-pending")
    )
    root = tmp_path / "memory"
    scripts = root / "client/node_modules/@agentmemory/agentmemory/plugin/scripts"
    scripts.mkdir(parents=True)
    (scripts / "plugin-bridge.mjs").write_text(
        "process.stderr.write('remote unavailable'); process.exit(1);"
    )
    (root / "access.json").write_text(
        json.dumps({"endpoint": settings().endpoint, "secret": "secret"})
    )
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable for guest launcher smoke test")
    (root / "node-path").write_text(node)
    path = root / "run"
    path.write_text(launcher)
    result = subprocess.run(["python3", str(path), "mcp"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "secret" not in result.stderr
    assert not (root / "standalone.json").exists()


def test_distribution_node_uses_distribution_npm_without_global_install(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable")
    home = tmp_path / "home"
    home.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    distro_node = tmp_path / "usr/bin/node"
    distro_node.parent.mkdir(parents=True)
    distro_node.write_text("#!/bin/sh\nexec " + node + ' "$@"\n')
    distro_node.chmod(0o755)
    npm = tmp_path / "usr/share/nodejs/npm/bin/npm-cli.js"
    npm.parent.mkdir(parents=True)
    recorded = tmp_path / "npm-arguments.json"
    npm.write_text(
        "require('fs').writeFileSync(process.env.RECORDED, JSON.stringify(process.argv.slice(2)));\n"
    )
    commands = {
        "getent": '#!/bin/sh\nprintf "guest:x:1000:1000::%s:/bin/sh\\n" "$GUEST_HOME"\n',
        "su": '#!/bin/sh\ncase "$*" in *"command -v npm"*) printf "%s\\n" "$DISTRO_NPM" ;; *) printf "%s\\n" "$DISTRO_NODE" ;; esac\n',
        "install": '#!/bin/sh\nfor arg do destination=$arg; done\nmkdir -p "$destination"\n',
        "chown": "#!/bin/sh\nexit 0\n",
        "runuser": '#!/bin/sh\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n',
    }
    for name, content in commands.items():
        path = binaries / name
        path.write_text(content)
        path.chmod(0o755)
    script = Path(__file__).parents[1] / "config/bootstrap/scripts/agentmemory-install.sh"
    environment = os.environ | {
        "PATH": str(binaries) + ":" + os.environ["PATH"],
        "GUEST_HOME": str(home),
        "DISTRO_NODE": str(distro_node),
        "DISTRO_NPM": str(npm),
        "RECORDED": str(recorded),
    }
    subprocess.run(["sh", str(script), "guest", "0.9.30", "24.21.0"], env=environment, check=True)
    arguments = json.loads(recorded.read_text())
    assert arguments[:3] == [
        "install",
        "--prefix",
        str(home / ".local/share/vmctl/agentmemory/client"),
    ]
    assert "--global" not in arguments
    assert (home / ".local/share/vmctl/agentmemory/node-path").read_text().strip() == str(
        distro_node
    )
    assert not (home / ".local/share/vmctl/agentmemory/runtime").exists()


def test_opencode_v2_registers_capture_without_context_injection(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable")
    major = int(
        subprocess.check_output([node, "-p", "process.versions.node.split('.')[0]"], text=True)
    )
    if major < 23:
        pytest.skip("Node TypeScript stripping unavailable")
    home = tmp_path / "home"
    root = home / ".local/share/vmctl/agentmemory"
    root.mkdir(parents=True)
    recorded = tmp_path / "captured.jsonl"
    wrapper = root / "run"
    wrapper.write_text(
        "#!/usr/bin/python3\nimport json, os, sys\n"
        "with open(os.environ['RECORDED'], 'a') as stream:\n"
        "    stream.write(json.dumps({'name': sys.argv[2], 'payload': json.load(sys.stdin)}) + '\\n')\n"
    )
    wrapper.chmod(0o700)
    source = Path(__file__).parents[1] / "config/bootstrap/scripts/agentmemory-opencode.ts"
    smoke = tmp_path / "smoke.mjs"
    smoke.write_text(
        "import plugin from " + json.dumps(source.as_uri()) + ";\n"
        "const toolHooks = {}; const sessionHooks = {};\n"
        "const cleanup = await plugin.setup({location:{directory:'/project'},"
        "tool:{hook: async (name, callback)=>{toolHooks[name]=callback}},"
        "session:{hook: async (name,callback)=>{sessionHooks[name]=callback}},"
        "event:{subscribe:async function*(){}}});\n"
        "if (sessionHooks.context || sessionHooks.compaction) throw Error('Context injection enabled');\n"
        "await sessionHooks.prompt({sessionID:'test',prompt:{text:'remember test'}});\n"
        "await toolHooks['execute.after']({sessionID:'test',tool:'read',input:{file:'test.py'},"
        "result:{content:'value'}});\ncleanup();\n"
    )
    result = subprocess.run(
        [node, "--experimental-strip-types", str(smoke)],
        env=os.environ | {"HOME": str(home), "RECORDED": str(recorded)},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in recorded.read_text().splitlines()]
    assert [event["name"] for event in events] == ["prompt-submit", "post-tool-use"]
    assert events[0]["payload"]["prompt"] == "remember test"
    assert events[1]["payload"]["tool_response"] == "value"


@pytest.mark.parametrize("inline", [False, True])
def test_codex_roundtrip_merge_handles_quoted_and_inline_mcp(tmp_path: Path, inline: bool) -> None:
    home = tmp_path / "home"
    fake_assets(home)
    (home / ".codex").mkdir()
    path = home / ".codex/config.toml"
    original = (
        '# Preserve my comment\nmodel = "custom"\n'
        'mcp_servers = {agentmemory = {command = "old"}, other = {command = "keep"}}\n'
        if inline
        else '# Preserve my comment\nmodel = "custom"\n["mcp_servers"."agentmemory"]\n'
        'command = "old"\n["mcp_servers"."other"]\ncommand = "keep"\n'
    )
    path.write_text(original)
    module = configure_module()
    with patch.object(module.shutil, "which", return_value=None):
        module.configure(home)
    value = tomllib.loads(path.read_text())
    assert value["mcp_servers"]["other"]["command"] == "keep"
    assert value["mcp_servers"]["agentmemory"]["args"] == ["mcp"]
    assert "# Preserve my comment" in path.read_text()
    assert value["model"] == "custom"


def test_disabled_native_codex_plugin_does_not_disable_managed_capture(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_assets(home)
    (home / ".codex").mkdir()
    path = home / ".codex/config.toml"
    path.write_text('[plugins."agentmemory@memory"]\nenabled = false\n')
    module = configure_module()
    with patch.object(module.shutil, "which", return_value=None):
        module.configure(home)
    hooks = json.loads((home / ".codex/hooks.json").read_text())
    assert len(hooks["hooks"]["PostToolUse"]) == 1
    assert tomllib.loads(path.read_text())["plugins"]["agentmemory@memory"]["enabled"] is False


def test_enabling_native_plugins_removes_managed_duplicate_capture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    fake_assets(home)
    for folder in (".codex", ".claude", ".config/opencode"):
        (home / folder).mkdir(parents=True)
    module = configure_module()
    with patch.object(module.shutil, "which", return_value=None):
        module.configure(home)
        (home / ".codex/config.toml").write_text(
            (home / ".codex/config.toml").read_text()
            + '\n[plugins."agentmemory@memory"]\nenabled = true\n'
        )
        path = home / ".claude/settings.json"
        value = json.loads(path.read_text())
        value["enabledPlugins"] = {"agentmemory@memory": True}
        path.write_text(json.dumps(value))
        path = home / ".config/opencode/opencode.json"
        value = json.loads(path.read_text())
        value["plugin"] = ["agentmemory-native"]
        path.write_text(json.dumps(value))
        module.configure(home)
    for folder, filename in ((".codex", "hooks.json"), (".claude", "settings.json")):
        value = json.loads((home / folder / filename).read_text())
        assert value["hooks"]["PostToolUse"] == []
    assert not (home / ".config/opencode/plugins/vmctl-agentmemory.ts").exists()
    assert "requires remote endpoint/key integration review" in capsys.readouterr().out


def test_memory_installer_runs_after_development_node(config: Configuration) -> None:
    from vmctl.bootstrap.models import load_modules

    modules = load_modules(config)
    rendered = CloudInitRenderer(config).render(
        config.template("debian"),
        "test",
        "ssh-ed25519 test",
        [modules["node"], modules["codex"]],
        {},
        system_features=[load_system_features(config)["agentmemory"]],
    )
    document = yaml.safe_load(rendered)
    script = next(
        item["content"] for item in document["write_files"] if item["path"].endswith("/run.sh")
    )
    assert script.index("development-node-0.sh") < script.index("system-agentmemory-0.sh")
    assert script.index("development-codex-0.sh") < script.index("system-agentmemory-0.sh")


def test_opencode_v2_schema_preserves_existing_legacy_servers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_assets(home)
    directory = home / ".config/opencode"
    directory.mkdir(parents=True)
    path = directory / "opencode.json"
    path.write_text(
        json.dumps(
            {
                "model": "keep",
                "mcp": {
                    "legacy": {"type": "local", "command": ["keep"], "enabled": False},
                    "timeout": {"startup": 30000},
                    "servers": {"native": {"type": "remote", "url": "https://example.test"}},
                },
            }
        )
    )
    module = configure_module()
    with patch.object(module.shutil, "which", return_value=None):
        module.configure(home)
    value = json.loads(path.read_text())
    assert value["model"] == "keep"
    assert value["mcp"]["servers"]["agentmemory"]["disabled"] is False
    assert value["mcp"]["servers"]["legacy"]["disabled"] is True
    assert value["mcp"]["servers"]["legacy"]["command"] == ["keep"]
    assert value["mcp"]["servers"]["native"]["url"] == "https://example.test"
    assert value["mcp"]["timeout"] == {"startup": 30000}
    assert "legacy" not in value["mcp"]


def test_opencode_v1_installed_binary_keeps_flat_schema(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_assets(home)
    module = configure_module()
    with (
        patch.object(
            module.shutil,
            "which",
            side_effect=lambda value: "/fake/opencode" if value == "opencode" else None,
        ),
        patch.object(module.subprocess, "check_output", return_value="1.2.0\n"),
    ):
        module.configure(home)
    value = json.loads((home / ".config/opencode/opencode.json").read_text())
    assert value["mcp"]["agentmemory"]["enabled"] is True
    assert "servers" not in value["mcp"]
