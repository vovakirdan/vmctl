"""The only host subprocess boundary; generated arguments never use a shell."""

import logging
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, TextIO

from vmctl.errors import CommandError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str = ""
    returncode: int = 0


class Runner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        timeout: int = 60,
        sensitive: Sequence[str] = (),
        stream: Callable[[str], None] | None = None,
        check: bool = True,
    ) -> CommandResult: ...


def redact(text: str, sensitive: Sequence[str]) -> str:
    for secret in sensitive:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


class SubprocessRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        timeout: int = 60,
        sensitive: Sequence[str] = (),
        stream: Callable[[str], None] | None = None,
        check: bool = True,
    ) -> CommandResult:
        logger.debug("Running: %s", redact(shlex.join(args), sensitive))
        try:
            with subprocess.Popen(
                list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            ) as process:
                assert process.stdout is not None and process.stderr is not None
                try:
                    if stream is None:
                        try:
                            stdout, stderr = process.communicate(timeout=timeout)
                        except subprocess.TimeoutExpired as exc:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.communicate()
                            raise CommandError(
                                args[0],
                                f"timed out after {timeout}s; outcome may be uncertain",
                                timed_out=True,
                            ) from exc
                    else:
                        stdout, stderr = self._stream(process, stream, timeout, sensitive, args[0])
                except BaseException:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    raise
                if check and process.returncode:
                    detail = redact((stderr or stdout)[-2000:].strip(), sensitive)
                    raise CommandError(args[0], f"exit {process.returncode}: {detail}")
                return CommandResult(stdout, stderr, process.returncode)
        except OSError as exc:
            raise CommandError(args[0], str(exc)) from exc

    def _stream(
        self,
        process: subprocess.Popen[str],
        callback: Callable[[str], None],
        timeout: int,
        sensitive: Sequence[str],
        executable: str,
    ) -> tuple[str, str]:
        events: queue.Queue[tuple[int, str | None]] = queue.Queue()

        def reader(pipe: TextIO, channel: int) -> None:
            try:
                for line in pipe:
                    events.put((channel, line))
            finally:
                events.put((channel, None))

        assert process.stdout is not None and process.stderr is not None
        threads = [
            threading.Thread(target=reader, args=(pipe, i), daemon=True)
            for i, pipe in enumerate((process.stdout, process.stderr))
        ]
        for thread in threads:
            thread.start()
        chunks: list[list[str]] = [[], []]
        closed = 0
        deadline = time.monotonic() + timeout
        try:
            while closed < 2:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                channel, line = events.get(timeout=remaining)
                if line is None:
                    closed += 1
                else:
                    chunks[channel].append(line)
                    callback(redact(line.rstrip(), sensitive))
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except (queue.Empty, subprocess.TimeoutExpired) as exc:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise CommandError(
                executable, f"timed out after {timeout}s; outcome may be uncertain", timed_out=True
            ) from exc
        finally:
            for thread in threads:
                thread.join(timeout=1)
        return "".join(chunks[0]), "".join(chunks[1])
