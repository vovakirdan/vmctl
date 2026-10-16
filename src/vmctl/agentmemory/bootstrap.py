"""Guest-only remote memory configuration; no server or local fallback store."""

import json
import re
from typing import Any

import yaml

from vmctl.agentmemory.config import AgentmemorySettings
from vmctl.agentmemory.service import MemoryAccess
from vmctl.bootstrap.models import script_path
from vmctl.config import Configuration
from vmctl.errors import VmctlError


def inject_access(
    data: str,
    access: MemoryAccess,
    settings: AgentmemorySettings,
    username: str,
    ca_certificate: str,
    config: Configuration,
) -> str:
    """Deliver credentials as deferred private files and wire agents after installation."""
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username) or username == "root":
        raise VmctlError("Agentmemory requires a validated non-root cloud-init username")
    if access.endpoint != settings.endpoint or not ca_certificate.strip():
        raise VmctlError("Agentmemory access endpoint or CA certificate is invalid")
    document: dict[str, Any] = yaml.safe_load(data)
    root = f"/home/{username}/.local/share/vmctl/agentmemory"
    private_config = {"endpoint": access.endpoint, "secret": access.secret}
    launcher = """#!/usr/bin/python3
import json
import os
import re
import sys
from pathlib import Path
root = Path(__file__).resolve().parent
try:
    config = json.loads((root / "access.json").read_text())
    node = (root / "node-path").read_text().strip()
    environment = os.environ.copy()
    environment.update({
        "AGENTMEMORY_URL": config["endpoint"],
        "AGENTMEMORY_SECRET": config["secret"],
        "NODE_EXTRA_CA_CERTS": str(root / "ca.pem"),
        "AGENTMEMORY_AUTO_COMPRESS": "false",
        "AGENTMEMORY_INJECT_CONTEXT": "false",
        "AGENTMEMORY_CAPTURE_SPOOL": "true",
        "AGENTMEMORY_CAPTURE_SPOOL_DIR": str(root / "capture-spool"),
        "AGENTMEMORY_CAPTURE_SPOOL_MAX_BYTES": "10485760",
        "AGENTMEMORY_CAPTURE_SPOOL_MAX_AGE_HOURS": "24",
    })
    plugin = root / "client/node_modules/@agentmemory/agentmemory/plugin"
    if sys.argv[1:] == ["mcp"]:
        script = plugin / "scripts/plugin-bridge.mjs"
    elif len(sys.argv) == 3 and sys.argv[1] == "hook" and re.fullmatch(r"[a-z-]+", sys.argv[2]):
        script = plugin / "scripts" / (sys.argv[2] + ".mjs")
    else:
        raise ValueError("Invalid invocation")
    if not script.is_file():
        raise ValueError("Missing client assets")
    os.execve(node, [node, str(script)], environment)
except (OSError, ValueError, KeyError):
    print("Agentmemory client configuration is unavailable or invalid.", file=sys.stderr)
    raise SystemExit(1)
"""
    contents = (
        ("access.json", json.dumps(private_config) + "\n", "0600"),
        ("ca.pem", ca_certificate, "0600"),
        ("run", launcher, "0700"),
        ("configure.py", script_path(config, "agentmemory-configure.py").read_text(), "0600"),
        ("opencode.ts", script_path(config, "agentmemory-opencode.ts").read_text(), "0600"),
    )
    preparation: list[list[str]] = [
        ["chown", f"{username}:{username}", root],
        ["chmod", "0700", root],
    ]
    for name, content, permissions in contents:
        pending = f"{root}/.{name}.vmctl-pending"
        document["write_files"].append(
            {
                "path": pending,
                "content": content,
                "owner": f"{username}:{username}",
                "permissions": permissions,
                "defer": True,
            }
        )
        preparation.append(["mv", "-fT", "--", pending, f"{root}/{name}"])
    document["runcmd"] = (
        preparation
        + document["runcmd"]
        + [
            [
                "runuser",
                "-u",
                username,
                "--",
                "env",
                f"HOME=/home/{username}",
                f"PATH=/home/{username}/.local/bin:/usr/local/bin:/usr/bin:/bin",
                "python3",
                f"{root}/configure.py",
            ]
        ]
    )
    return "#cloud-config\n" + yaml.safe_dump(document, sort_keys=False)
