"""On-demand Linux executor. stdout is exclusively versioned JSON Lines."""

import argparse
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from pydantic import TypeAdapter, ValidationError

from vmctl import __version__
from vmctl.errors import RepeatedRequestError, VmctlError
from vmctl.models import VM
from vmctl.operations import DeletePreview, Operations
from vmctl.protocol import (
    MAX_MESSAGE_BYTES,
    REQUEST_ADAPTER,
    CreateMessage,
    DeleteMessage,
    Event,
    QueryMessage,
    ReferenceMessage,
    Request,
)

BackendFactory = Callable[[], Operations]


def dispatch(request: Request, backend: Operations, progress: Callable[[str], None]) -> object:
    if isinstance(request, CreateMessage):
        domain = request.parameters.to_domain()
        if request.operation == "plan_create":
            return backend.plan_create(domain)
        return backend.create(domain, request_id=request.request_id, progress=progress)
    if isinstance(request, ReferenceMessage):
        if request.operation == "info":
            return backend.info(request.parameters.reference)
        return backend.plan_delete(request.parameters.reference)
    if isinstance(request, DeleteMessage):
        params = request.parameters
        return backend.delete(
            DeletePreview(VM(params.vmid, params.name, "unknown", ""), params.fingerprint)
        )
    if isinstance(request, QueryMessage):
        match request.operation:
            case "catalog":
                return backend.catalog()
            case "list":
                return backend.list()
            case "templates":
                return backend.templates()
            case "presets":
                return backend.presets()
            case "validate_config":
                return backend.validate_config()
    raise VmctlError("Unsupported operation")


def serve(raw: str, output: TextIO, factory: BackendFactory) -> int:
    """Validate before constructing the host backend; injectable for mock-only tests."""
    connected = True
    request_id = "0" * 32
    secret = ""
    decoded: object = None
    try:
        decoded = json.loads(raw)
        if (
            isinstance(decoded, dict)
            and isinstance(decoded.get("request_id"), str)
            and re.fullmatch(r"[0-9a-f]{32}", decoded["request_id"])
        ):
            request_id = decoded["request_id"]
    except (ValueError, RecursionError):
        pass

    def emit(event: Event) -> None:
        nonlocal connected
        if not connected:
            return
        try:
            output.write(event.model_dump_json() + "\n")
            output.flush()
        except (BrokenPipeError, OSError):
            # A disconnected frontend must not trigger compensation after a successful clone.
            connected = False

    emit(Event(request_id=request_id, kind="hello", data={"worker_version": __version__}))
    try:
        if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError("Oversized request")
        if (
            not isinstance(decoded, dict)
            or "protocol_version" not in decoded
            or "parameters" not in decoded
        ):
            raise ValueError("Incomplete envelope")
        request = REQUEST_ADAPTER.validate_json(raw)
    except (ValueError, ValidationError, RecursionError):
        emit(
            Event(
                request_id=request_id,
                kind="error",
                code="invalid_request",
                message="Invalid request or incompatible protocol; no operation was executed",
            )
        )
        return 1
    if isinstance(request, CreateMessage) and request.parameters.desktop_password_hash:
        secret = request.parameters.desktop_password_hash.get_secret_value()

    def safe(text: str) -> str:
        return text.replace(secret, "<redacted>") if secret else text

    def progress(message: str) -> None:
        allocated = re.fullmatch(r"Allocated VMID: (\d+)", message)
        emit(
            Event(
                request_id=request_id,
                kind="progress",
                message=safe(message),
                vmid=int(allocated[1]) if allocated else None,
            )
        )

    try:
        result = dispatch(request, factory(), progress)
        # Serialize only operation DTOs. Never serialize the internal CreatePlan.
        data = TypeAdapter(type(result)).dump_python(result, mode="json")
        emit(Event(request_id=request_id, kind="result", data=data))
        return 0
    except RepeatedRequestError as exc:
        emit(
            Event(
                request_id=request_id,
                kind="error",
                code="repeated_request",
                message=safe(str(exc)),
                vmid=exc.vmid,
            )
        )
    except (VmctlError, OSError) as exc:
        emit(
            Event(
                request_id=request_id, kind="error", code="operation_failed", message=safe(str(exc))
            )
        )
    except Exception:
        # Do not dump tracebacks, request bodies or arbitrary adapter exceptions to the wire.
        emit(
            Event(
                request_id=request_id,
                kind="error",
                code="internal_error",
                message="Worker failed unexpectedly; inspect server state before retrying",
            )
        )
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Execute one vmctl JSON request on a Proxmox host."
    )
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/vmctl"))
    args = parser.parse_args()
    if sys.platform != "linux":
        print("vmctl-worker requires Linux", file=sys.stderr)
        raise SystemExit(1)
    from vmctl.local import LocalOperations

    try:
        raw = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1).decode("utf-8")
    except UnicodeError:
        raw = ""
    raise SystemExit(serve(raw, sys.stdout, lambda: LocalOperations(args.config_dir)))


if __name__ == "__main__":
    main()
