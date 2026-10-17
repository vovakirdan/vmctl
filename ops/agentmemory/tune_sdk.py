"""Bound the pinned SDK's internal WebSocket responses for an existing large graph."""

import json
import os
import stat
import tempfile
from pathlib import Path

ORIGINAL = """this.ws = new WebSocket(this.address, {
\t\t\theaders: this.options?.headers,
\t\t\thandshakeTimeout: WS_HANDSHAKE_TIMEOUT_MS
\t\t});"""
TUNED = ORIGINAL.replace(
    "handshakeTimeout: WS_HANDSHAKE_TIMEOUT_MS",
    "handshakeTimeout: WS_HANDSHAKE_TIMEOUT_MS,\n\t\t\tmaxPayload: 256 * 1024 * 1024",
)


def tune_sdk(directory: Path) -> bool:
    if json.loads((directory / "package.json").read_text())["version"] != "0.22.1":
        raise ValueError("SDK version changed; review the internal payload limit before tuning")
    target = directory / "dist/index.mjs"
    details = target.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("SDK entrypoint must be a regular file")
    source = target.read_text()
    if source.count(TUNED) == 1:
        return False
    if source.count(ORIGINAL) != 1:
        raise ValueError("SDK connection implementation changed; refusing an unverified patch")
    descriptor, temporary = tempfile.mkstemp(prefix=".vmctl-sdk-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(details.st_mode))
            os.fchown(stream.fileno(), details.st_uid, details.st_gid)
            stream.write(source.replace(ORIGINAL, TUNED))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        descriptor = os.open(target.parent, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


if __name__ == "__main__":
    changed = tune_sdk(Path("/opt/agentmemory/app/node_modules/iii-sdk"))
    print("Internal SDK payload limit configured." if changed else "SDK tuning already applied.")
