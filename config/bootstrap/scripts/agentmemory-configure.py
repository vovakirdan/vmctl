"""Merge guest-only agent integrations without touching credentials or trust settings."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import tomlkit


def atomic(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Refusing symlinked agent configuration")
    descriptor, temporary = tempfile.mkstemp(prefix=".vmctl-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError("Agent configuration must be a JSON object")
    return value


def save_json(path: Path, value: dict[str, Any]) -> None:
    atomic(path, json.dumps(value, indent=2) + "\n")


def merge_hooks(existing: dict[str, Any], manifest: dict[str, Any], wrapper: str) -> None:
    hooks = existing.setdefault("hooks", {})
    for event, groups in manifest["hooks"].items():
        current = hooks.setdefault(event, [])
        current[:] = [group for group in current if wrapper not in json.dumps(group)]
        for group in groups:
            group = json.loads(json.dumps(group))
            for hook in group["hooks"]:
                script = re.search(r"/scripts/([a-z-]+)\.mjs", hook["command"])
                if script is None:
                    raise ValueError("Unknown upstream agentmemory hook command")
                hook["command"] = shlex.join([wrapper, "hook", script.group(1)])
            current.append(group)


def remove_managed_hooks(config: dict[str, Any], wrapper: str) -> None:
    for groups in config.get("hooks", {}).values():
        groups[:] = [group for group in groups if wrapper not in json.dumps(group)]


def enabled_memory_plugin(plugins: dict[str, Any]) -> bool:
    return any(
        "agentmemory" in name
        and (enabled is True or isinstance(enabled, dict) and enabled.get("enabled") is True)
        for name, enabled in plugins.items()
    )


def native_hooks_present(config: dict[str, Any]) -> bool:
    # Native plugin capture and global capture must not run twice.
    return enabled_memory_plugin(config.get("enabledPlugins", {})) or (
        "agentmemory" in json.dumps(config.get("hooks", {}))
    )


def native_notice() -> None:
    print(
        "Existing native agentmemory capture requires remote endpoint/key integration review; "
        "managed duplicate hooks removed, MCP configured, native trust settings unchanged."
    )


def configure(home: Path) -> None:
    root = home / ".local/share/vmctl/agentmemory"
    plugin = root / "client/node_modules/@agentmemory/agentmemory/plugin"
    wrapper = str(root / "run")
    selected: list[str] = []
    for agent, executable, directory in (
        ("codex", "codex", home / ".codex"),
        ("claude", "claude", home / ".claude"),
        ("opencode", "opencode", home / ".config/opencode"),
    ):
        if shutil.which(executable) or directory.exists():
            selected.append(agent)
    if "codex" in selected:
        path = home / ".codex/config.toml"
        current = path.read_text() if path.exists() else ""
        parsed = tomlkit.parse(current)
        native = enabled_memory_plugin(parsed.get("plugins", {}))
        parsed.setdefault("mcp_servers", {})["agentmemory"] = {
            "command": wrapper,
            "args": ["mcp"],
        }
        atomic(path, tomlkit.dumps(parsed))
        hooks_path = home / ".codex/hooks.json"
        hooks = load_json(hooks_path)
        remove_managed_hooks(hooks, wrapper)
        if not native and not native_hooks_present(hooks):
            merge_hooks(hooks, load_json(plugin / "hooks/hooks.codex.json"), wrapper)
        else:
            native_notice()
        save_json(hooks_path, hooks)
        print(
            "Codex hooks require native review on first interactive launch; trust settings unchanged."
        )
    if "claude" in selected:
        path = home / ".claude.json"
        config = load_json(path)
        config.setdefault("mcpServers", {})["agentmemory"] = {"command": wrapper, "args": ["mcp"]}
        save_json(path, config)
        settings_path = home / ".claude/settings.json"
        settings = load_json(settings_path)
        remove_managed_hooks(settings, wrapper)
        if not native_hooks_present(settings):
            merge_hooks(settings, load_json(plugin / "hooks/hooks.json"), wrapper)
        else:
            native_notice()
        save_json(settings_path, settings)
    if "opencode" in selected:
        path = home / ".config/opencode/opencode.json"
        config = load_json(path)
        executable = shutil.which("opencode")
        major = 2
        if executable:
            version = subprocess.check_output([executable, "--version"], text=True, timeout=15)
            match = re.search(r"(?:v|^)([0-9]+)\.", version.strip().removeprefix("opencode "))
            if match:
                major = int(match.group(1))
        mcp = config.setdefault("mcp", {})
        if major >= 2:
            servers = mcp.setdefault("servers", {})
            # Migrate legacy entries when upgrading an existing v1 installation.
            for name in list(mcp):
                value = mcp[name]
                if name not in {"servers", "timeout"} and isinstance(value, dict):
                    if "enabled" in value:
                        value["disabled"] = not value.pop("enabled")
                    servers.setdefault(name, value)
                    del mcp[name]
            servers["agentmemory"] = {
                "type": "local",
                "command": [wrapper, "mcp"],
                "disabled": False,
            }
        else:
            mcp["agentmemory"] = {
                "type": "local",
                "command": [wrapper, "mcp"],
                "enabled": True,
            }
        save_json(path, config)
        # OpenCode loads this one local plugin; it dispatches bundled capture hooks.
        destination = home / ".config/opencode/plugins/vmctl-agentmemory.ts"
        if not any("agentmemory" in str(value) for value in config.get("plugin", [])):
            atomic(destination, (root / "opencode.ts").read_text())
        else:
            if destination.exists():
                if destination.read_text() != (root / "opencode.ts").read_text():
                    raise ValueError("Managed OpenCode plugin was modified; review before removing")
                destination.unlink()
            native_notice()
    for agent in selected:
        destination = (
            home / ".agents/skills"
            if agent == "codex"
            else home / ".claude/skills"
            if agent == "claude"
            else home / ".config/opencode/skills"
        )
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        for source in sorted((plugin / "skills").iterdir()):
            target = destination / source.name
            if source.is_dir() and not target.exists():
                # Copy into a sibling then publish atomically; never replace user skills.
                staging = Path(tempfile.mkdtemp(prefix=".vmctl-", dir=destination))
                try:
                    shutil.copytree(source, staging / source.name)
                    os.rename(staging / source.name, target)
                finally:
                    shutil.rmtree(staging)


if __name__ == "__main__":
    try:
        configure(Path.home())
    except (ValueError, OSError, TypeError, KeyError):
        print(
            "Cannot configure agentmemory: existing agent configuration is invalid or unsafe.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
