import logging
import sys

import pytest

from vmctl.errors import CommandError
from vmctl.utils.subprocess import SubprocessRunner


def test_arguments_do_not_use_a_shell() -> None:
    literal = "$(echo injected); id"
    result = SubprocessRunner().run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", literal]
    )
    assert result.stdout.strip() == literal


def test_redaction_and_error(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(CommandError) as failure:
            SubprocessRunner().run(
                [sys.executable, "-c", "import sys; print(sys.argv[1]); sys.exit(2)", "secret"],
                sensitive=["secret"],
            )
    assert "secret" not in str(failure.value)
    assert "secret" not in caplog.text
    assert "<redacted>" in str(failure.value)


def test_streams_both_pipes() -> None:
    lines: list[str] = []
    result = SubprocessRunner().run(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        stream=lines.append,
    )
    assert set(lines) == {"out", "err"}
    assert result.stdout.strip() == "out" and result.stderr.strip() == "err"


@pytest.mark.parametrize("stream", [False, True])
def test_timeout(stream: bool) -> None:
    with pytest.raises(CommandError) as failure:
        SubprocessRunner().run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=1,
            stream=(lambda _: None) if stream else None,
        )
    assert failure.value.timed_out


def test_unchecked_exit_status() -> None:
    result = SubprocessRunner().run([sys.executable, "-c", "raise SystemExit(1)"], check=False)
    assert result.returncode == 1
