"""Native-platform pipe tests run a harmless local Python child, never SSH."""

import subprocess
import sys
from collections.abc import Sequence
from ipaddress import IPv4Address
from threading import Event

import pytest

from vmctl.errors import VmctlError
from vmctl.transfer.models import UploadCancelledError
from vmctl.transfer.openssh import OpenSSHBackend, PipeSocket


def child(code: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


def test_pipe_binary_receive_keeps_short_read_remainder_and_reaps_child() -> None:
    process = child(
        "import sys; data=sys.stdin.buffer.read(4); "
        "sys.stdout.buffer.write(data+b'\xff\x00'); sys.stdout.buffer.flush()".replace(
            "\xff", "\\xff"
        ).replace("\x00", "\\x00")
    )
    socket = PipeSocket(process, timeout=3)
    try:
        assert socket.send(b"ping") == 4
        assert socket.recv(3) == b"pin"
        assert socket.recv(3) == b"g\xff\x00"
        assert socket.recv(1) == b""
    finally:
        socket.close()
    assert process.poll() is not None
    assert all(not thread.is_alive() for thread in socket._threads)


def test_pipe_receive_timeout_is_bounded_and_closes_child() -> None:
    process = child("import time; time.sleep(30)")
    socket = PipeSocket(process, timeout=0.1)
    try:
        with pytest.raises(TimeoutError):
            socket.recv(1)
    finally:
        socket.close()
    assert process.poll() is not None


def test_pipe_close_reaps_threads_if_process_exits_during_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = child("import time; time.sleep(30)")
    socket = PipeSocket(process, timeout=3)
    terminate = process.terminate

    def already_exited() -> None:
        terminate()
        process.wait(timeout=3)
        raise ProcessLookupError("Process already exited")

    monkeypatch.setattr(process, "terminate", already_exited)
    socket.close()
    assert process.poll() is not None
    assert all(not thread.is_alive() for thread in socket._threads)
    assert all(
        stream is not None and stream.closed
        for stream in (process.stdin, process.stdout, process.stderr)
    )


def test_pipe_write_timeout_is_bounded_on_full_stdin() -> None:
    process = child("import time; time.sleep(30)")
    socket = PipeSocket(process, timeout=0.1)
    try:
        with pytest.raises(TimeoutError):
            for _ in range(32):
                socket.send(b"x" * 65536)
    finally:
        socket.close()
    assert process.poll() is not None
    assert all(not thread.is_alive() for thread in socket._threads)


def test_pipe_cancellation_interrupts_wait_without_printing_payload() -> None:
    process = child("import time; time.sleep(30)")
    cancel = Event()
    socket = PipeSocket(process, timeout=3, cancel=cancel)
    cancel.set()
    try:
        with pytest.raises(UploadCancelledError):
            socket.recv(1)
    finally:
        socket.close()
    assert process.poll() is not None


def test_backend_negotiates_real_sftp_over_binary_pipe_with_secure_native_arguments() -> None:
    seen: list[str] = []
    processes: list[subprocess.Popen[bytes]] = []

    def factory(arguments: Sequence[str]) -> subprocess.Popen[bytes]:
        seen.extend(arguments)
        process = child(
            "import sys,struct,time; "
            "size=struct.unpack('>I',sys.stdin.buffer.read(4))[0]; "
            "sys.stdin.buffer.read(size); "
            "sys.stdout.buffer.write(struct.pack('>IBI',5,2,3)); "
            "sys.stdout.buffer.flush(); time.sleep(30)"
        )
        processes.append(process)
        return process

    backend = OpenSSHBackend(process_factory=factory, io_timeout=3)
    with backend.session(IPv4Address("10.210.0.104"), "vmadmin"):
        assert seen[-2:] == ["vmadmin@10.210.0.104", "sftp"]
        for value in (
            "BatchMode=yes",
            "StrictHostKeyChecking=yes",
            "ForwardAgent=no",
            "ForwardX11=no",
            "ClearAllForwardings=yes",
            "PermitLocalCommand=no",
            "ControlMaster=no",
            "ControlPath=none",
        ):
            assert value in seen
    assert processes[0].poll() is not None


def test_backend_failed_negotiation_cleans_process_and_sanitizes_diagnostics() -> None:
    processes: list[subprocess.Popen[bytes]] = []

    def factory(arguments: Sequence[str]) -> subprocess.Popen[bytes]:
        process = child(
            "import sys; sys.stderr.write('Host key verification failed. private-secret-marker'); "
            "sys.stderr.flush()"
        )
        processes.append(process)
        return process

    backend = OpenSSHBackend(process_factory=factory, io_timeout=3)
    with (
        pytest.raises(VmctlError) as failure,
        backend.session(IPv4Address("10.210.0.104"), "vmadmin"),
    ):
        pytest.fail("Negotiation must fail")
    assert "host key" in str(failure.value).lower()
    assert "private-secret-marker" not in str(failure.value)
    assert processes[0].poll() is not None


def test_backend_missing_native_client_has_actionable_error() -> None:
    def missing(arguments: Sequence[str]) -> subprocess.Popen[bytes]:
        raise FileNotFoundError("ssh")

    with (
        pytest.raises(VmctlError, match="native ssh"),
        OpenSSHBackend(process_factory=missing).session(
            IPv4Address("10.210.0.104"),
            "vmadmin",
        ),
    ):
        pytest.fail("Missing executable must fail")
