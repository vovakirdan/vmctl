"""Bounded liveness checks; CPU-heavy work must not trigger a service restart."""

import json
import stat
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

SECRET_PATH = Path("/etc/agentmemory/backend-secret")
SERVICE = "agentmemory.service"
LIVENESS_URL = "http://127.0.0.1:3111/agentmemory/livez"
ROOT_UID = 0


def active() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", SERVICE],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def read_secret() -> str:
    details = SECRET_PATH.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_uid != ROOT_UID or details.st_mode & 0o077:
        raise ValueError("Invalid backend credential permissions")
    value = SECRET_PATH.read_text().strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("Invalid backend credential")
    return value


def healthy(secret: str) -> bool:
    request = Request(LIVENESS_URL, headers={"Authorization": f"Bearer {secret}"})
    # A host proxy configuration must not redirect private backend credentials.
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=4) as response:
            if response.status != 200:
                return False
            raw = response.read(8193)
            if len(raw) > 8192:
                return False
            result = json.loads(raw)
            return (
                isinstance(result, dict)
                and result.get("status") == "ok"
                and result.get("service") == "agentmemory"
            )
    except (OSError, ValueError, HTTPError, URLError):
        return False


def main() -> int:
    if not active():
        return 0
    try:
        secret = read_secret()
    except (OSError, ValueError):
        print("Agentmemory health check skipped: backend credential is unavailable or unsafe.")
        return 1
    for attempt in range(3):
        if healthy(secret):
            return 0
        if attempt < 2:
            time.sleep(1)
    if not active():
        return 0
    print(
        "Agentmemory backend failed three health checks; requesting restart of the active service."
    )
    try:
        result = subprocess.run(
            ["systemctl", "try-restart", SERVICE],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return 0 if result.returncode == 0 else 1
    except (OSError, subprocess.SubprocessError):
        print("Agentmemory health check could not confirm the restart request.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
