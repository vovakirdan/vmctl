"""Upload CLI uses frozen service previews and keeps file contents out of output."""

from collections.abc import Callable
from ipaddress import IPv4Address
from pathlib import Path, PurePosixPath
from threading import Event

import pytest
from typer.testing import CliRunner

from vmctl import cli
from vmctl.models import VM
from vmctl.transfer.models import (
    UploadCancelledError,
    UploadPreview,
    UploadProgress,
    UploadRequest,
    UploadResult,
    UploadUncertainError,
)


class FakeUploader:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.preview: UploadPreview | None = None
        self.error: BaseException | None = None
        self.cancel: Event | None = None

    def plan(self, request: UploadRequest) -> UploadPreview:
        self.calls.append("plan")
        self.preview = UploadPreview(
            VM(104, "work", "running", "fake"),
            IPv4Address("10.210.0.105"),
            request.username or "vmadmin",
            request.source,
            PurePosixPath("/home/vmadmin") / request.source.name,
            1,
            12,
            "frozen",
            request,
        )
        return self.preview

    def upload(
        self, expected: UploadPreview, *, progress: Callable[[UploadProgress], None], cancel: Event
    ) -> UploadResult:
        self.calls.append("upload")
        assert expected is self.preview
        self.cancel = cancel
        if self.error:
            raise self.error
        progress(UploadProgress(expected.source.name, 12, 12, 1, 1))
        return UploadResult(104, expected.destination, 1, 12)


@pytest.fixture
def uploader(monkeypatch: pytest.MonkeyPatch) -> FakeUploader:
    fake = FakeUploader()
    monkeypatch.setattr(cli, "operations", lambda _: object())
    monkeypatch.setattr(cli, "upload_service", lambda *_: fake)
    return fake


def source_file(tmp_path: Path) -> Path:
    path = tmp_path / "file with spaces.txt"
    path.write_text("private payload")
    return path


def test_upload_dry_run_is_read_only(uploader: FakeUploader, tmp_path: Path) -> None:
    source = source_file(tmp_path)
    result = CliRunner().invoke(cli.app, ["upload", "work", str(source), "--to", "~", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert uploader.calls == ["plan"]
    assert "Dry run" in result.output and "private payload" not in result.output


def test_upload_requires_confirmation(uploader: FakeUploader, tmp_path: Path) -> None:
    arguments = ["upload", "work", str(source_file(tmp_path)), "--to", "~"]
    result = CliRunner().invoke(cli.app, arguments, input="n\n")
    assert result.exit_code != 0 and uploader.calls == ["plan"]
    assert "104" in result.output and "file with spaces.txt" in result.output
    uploader.calls.clear()
    result = CliRunner().invoke(cli.app, arguments, input="y\n")
    assert result.exit_code == 0, result.output
    assert uploader.calls == ["plan", "upload"]
    assert "Upload completed" in result.output and "private payload" not in result.output


def test_upload_user_and_overwrite_options(uploader: FakeUploader, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli.app,
        [
            "upload",
            "work",
            str(source_file(tmp_path)),
            "--to",
            "/tmp",
            "--user",
            "alice",
            "--overwrite",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert uploader.preview is not None
    assert uploader.preview.request.username == "alice"
    assert uploader.preview.request.overwrite


def test_upload_unknown_publication_is_not_retried(uploader: FakeUploader, tmp_path: Path) -> None:
    uploader.error = UploadUncertainError(104, PurePosixPath("/home/vmadmin/file"))
    result = CliRunner().invoke(
        cli.app, ["upload", "work", str(source_file(tmp_path)), "--to", "~", "--yes"]
    )
    assert result.exit_code == 1
    assert uploader.calls == ["plan", "upload"] and "uncertain" in result.output


def test_upload_keyboard_interrupt_signals_cancellation(
    uploader: FakeUploader, tmp_path: Path
) -> None:
    uploader.error = KeyboardInterrupt()
    result = CliRunner().invoke(
        cli.app, ["upload", "work", str(source_file(tmp_path)), "--to", "~", "--yes"]
    )
    assert result.exit_code == 130
    assert uploader.cancel is not None and uploader.cancel.is_set()


def test_upload_service_cancellation_uses_interrupted_exit_code(
    uploader: FakeUploader, tmp_path: Path
) -> None:
    uploader.error = UploadCancelledError("Upload cancelled before publication")
    result = CliRunner().invoke(
        cli.app,
        ["upload", "work", str(source_file(tmp_path)), "--to", "~", "--yes"],
    )
    assert result.exit_code == 130 and "cancelled before publication" in result.output


def test_upload_missing_source_does_not_contact_vm(uploader: FakeUploader, tmp_path: Path) -> None:
    result = CliRunner().invoke(cli.app, ["upload", "work", str(tmp_path / "missing"), "--to", "~"])
    assert result.exit_code != 0 and not uploader.calls
