"""Portable OpenSSH transport and the remote implementation of Operations."""

import json
import logging
import queue
import shlex
import subprocess
import threading
import time
import uuid
from typing import Protocol, TypeVar

from pydantic import TypeAdapter, ValidationError

from vmctl.client_config import ConnectionConfig
from vmctl.config import Preset, Template
from vmctl.errors import ProtocolError, RepeatedRequestError, UnknownOutcomeError, VmctlError
from vmctl.models import CreateRequest, CreateResult, VMDetails
from vmctl.operations import Catalog, CreatePreview, DeletePreview, Progress, ValidationSummary
from vmctl.protocol import (
    MAX_MESSAGE_BYTES,
    CreateMessage,
    CreateParameters,
    DeleteMessage,
    DeleteParameters,
    Event,
    QueryMessage,
    ReferenceMessage,
    ReferenceParameters,
    Request,
    encode_request,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")


class Transport(Protocol):
    def exchange(self, request: Request, progress: Progress | None = None) -> Event: ...


def ssh_arguments(config: ConnectionConfig) -> list[str]:
    remote = [config.worker, "--config-dir", config.config_dir]
    if config.sudo:
        remote = ["sudo", "-n", "--", *remote]
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        f"ConnectTimeout={config.connect_timeout}",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        config.host,
        shlex.join(remote),
    ]


class SSHTransport:
    def __init__(self, config: ConnectionConfig) -> None:
        self.config = config

    def exchange(self, request: Request, progress: Progress | None = None) -> Event:
        payload = encode_request(request)
        arguments = ssh_arguments(self.config)
        logger.debug("SSH operation %s request %s", request.operation, request.request_id)
        try:
            process = subprocess.Popen(
                arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError as exc:
            raise VmctlError("Cannot start OpenSSH; install ssh and check PATH") from exc
        assert (
            process.stdin is not None and process.stdout is not None and process.stderr is not None
        )
        stdin, stdout, stderr_pipe = process.stdin, process.stdout, process.stderr
        messages: queue.Queue[tuple[str, bytes]] = queue.Queue()
        stderr = bytearray()
        vmid: int | None = None
        mutating = request.operation in {"create", "delete"}
        if isinstance(request, DeleteMessage):
            vmid = request.parameters.vmid
        terminal: Event | None = None
        hello = False
        secret = (
            request.parameters.desktop_password_hash.get_secret_value()
            if isinstance(request, CreateMessage) and request.parameters.desktop_password_hash
            else ""
        )

        def read_stdout() -> None:
            try:
                with stdout:
                    while line := stdout.readline(MAX_MESSAGE_BYTES + 1):
                        messages.put(("stdout", line))
            except (OSError, ValueError):
                pass
            finally:
                messages.put(("eof", b""))

        def read_stderr() -> None:
            try:
                with stderr_pipe:
                    while chunk := stderr_pipe.read(4096):
                        if len(stderr) < 16384:
                            stderr.extend(chunk[: 16384 - len(stderr)])
            except (OSError, ValueError):
                pass

        def write_request() -> None:
            try:
                with stdin:
                    stdin.write(payload)
            except (OSError, ValueError):
                messages.put(("write_failed", b""))

        threads = [
            threading.Thread(target=function, daemon=True)
            for function in (read_stdout, read_stderr, write_request)
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + self.config.operation_timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProtocolError("SSH operation timed out")
                try:
                    kind, raw = messages.get(timeout=remaining)
                except queue.Empty as exc:
                    raise ProtocolError("SSH operation timed out") from exc
                if kind == "eof":
                    break
                if kind == "write_failed":
                    raise ProtocolError("SSH request delivery failed")
                if len(raw) > MAX_MESSAGE_BYTES:
                    raise ProtocolError("Worker message exceeds protocol size limit")
                try:
                    envelope = json.loads(raw)
                    if not isinstance(envelope, dict) or "protocol_version" not in envelope:
                        raise ValueError("Worker protocol version is missing")
                    event = Event.model_validate_json(raw)
                except (ValidationError, ValueError) as exc:
                    raise ProtocolError(
                        "Invalid JSON response or incompatible worker protocol"
                    ) from exc
                if event.request_id != request.request_id:
                    raise ProtocolError("Worker response request ID does not match")
                if terminal is not None:
                    raise ProtocolError("Worker sent messages after a terminal response")
                if not hello:
                    if event.kind != "hello":
                        raise ProtocolError("Worker handshake missing")
                    hello = True
                elif event.kind == "hello":
                    raise ProtocolError("Duplicate worker handshake")
                elif event.kind == "progress":
                    if event.vmid is not None:
                        vmid = event.vmid
                    if progress is not None:
                        progress(
                            event.message.replace(secret, "<redacted>") if secret else event.message
                        )
                else:
                    terminal = event
            returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if terminal is None:
                detail = stderr.decode("utf-8", errors="replace").strip()
                if secret:
                    detail = detail.replace(secret, "<redacted>")
                raise ProtocolError(f"Worker did not return a final response. {detail[:2000]}")
            if terminal.kind == "error":
                if terminal.code == "repeated_request" and terminal.vmid is not None:
                    raise RepeatedRequestError(request.request_id, terminal.vmid)
                if terminal.code == "internal_error" and mutating:
                    raise UnknownOutcomeError(request.request_id, terminal.vmid or vmid)
                message = (
                    terminal.message.replace(secret, "<redacted>") if secret else terminal.message
                )
                raise VmctlError(message or "Worker rejected the operation")
            if returncode != 0:
                raise ProtocolError("SSH exited unsuccessfully after the worker response")
            return terminal.model_copy(update={"vmid": terminal.vmid or vmid})
        except (ProtocolError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            if mutating:
                raise UnknownOutcomeError(request.request_id, vmid) from exc
            if isinstance(exc, KeyboardInterrupt):
                raise VmctlError("SSH operation interrupted") from exc
            raise VmctlError(str(exc)) from exc
        finally:
            # Stop only the local SSH process. Never issue a compensating remote request.
            if process.poll() is None:
                process.kill()
            process.wait()
            # Pipe readers own their handles. Closing a buffered pipe from this
            # thread can deadlock if a proxy process still holds its other end.
            for thread in threads:
                thread.join(timeout=0.1)


class SSHOperations:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def _read(
        self, request: Request, adapter: TypeAdapter[T], progress: Progress | None = None
    ) -> T:
        event = self.transport.exchange(request, progress)
        if event.kind != "result":
            raise ProtocolError("Expected a worker result")
        try:
            return adapter.validate_json(json.dumps(event.data))
        except ValidationError as exc:
            if request.operation in {"create", "delete"}:
                raise UnknownOutcomeError(request.request_id, event.vmid) from exc
            raise ProtocolError("Invalid worker result schema") from exc

    def plan_create(self, request: CreateRequest) -> CreatePreview:
        message = CreateMessage(
            request_id=uuid.uuid4().hex,
            operation="plan_create",
            parameters=CreateParameters.from_domain(request),
        )
        return self._read(message, TypeAdapter(CreatePreview))

    def create(
        self, request: CreateRequest, *, request_id: str, progress: Progress
    ) -> CreateResult:
        message = CreateMessage(
            request_id=request_id,
            operation="create",
            parameters=CreateParameters.from_domain(request),
        )
        return self._read(message, TypeAdapter(CreateResult), progress)

    def plan_delete(self, reference: str) -> DeletePreview:
        return self._read(
            ReferenceMessage(
                request_id=uuid.uuid4().hex,
                operation="plan_delete",
                parameters=ReferenceParameters(reference=reference),
            ),
            TypeAdapter(DeletePreview),
        )

    def delete(self, expected: DeletePreview) -> int:
        return self._read(
            DeleteMessage(
                request_id=uuid.uuid4().hex, parameters=DeleteParameters.from_preview(expected)
            ),
            TypeAdapter(int),
        )

    def list(self) -> list[VMDetails]:
        return self._read(
            QueryMessage(request_id=uuid.uuid4().hex, operation="list"),
            TypeAdapter(list[VMDetails]),
        )

    def info(self, reference: str) -> VMDetails:
        return self._read(
            ReferenceMessage(
                request_id=uuid.uuid4().hex,
                operation="info",
                parameters=ReferenceParameters(reference=reference),
            ),
            TypeAdapter(VMDetails),
        )

    def catalog(self) -> Catalog:
        return self._read(
            QueryMessage(request_id=uuid.uuid4().hex, operation="catalog"), TypeAdapter(Catalog)
        )

    def templates(self) -> dict[str, Template]:
        return self._read(
            QueryMessage(request_id=uuid.uuid4().hex, operation="templates"),
            TypeAdapter(dict[str, Template]),
        )

    def presets(self) -> dict[str, Preset]:
        return self._read(
            QueryMessage(request_id=uuid.uuid4().hex, operation="presets"),
            TypeAdapter(dict[str, Preset]),
        )

    def validate_config(self) -> ValidationSummary:
        return self._read(
            QueryMessage(request_id=uuid.uuid4().hex, operation="validate_config"),
            TypeAdapter(ValidationSummary),
        )
