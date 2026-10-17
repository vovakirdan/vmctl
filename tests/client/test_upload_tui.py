"""Workstation upload screens are exercised with fake transfers and real widgets."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from ipaddress import IPv4Address
from pathlib import Path, PurePosixPath
from threading import Event

import pytest
from test_tui import FakeOperations, app_for
from textual.pilot import Pilot
from textual.widgets import (
    Button,
    Checkbox,
    DirectoryTree,
    Input,
    LoadingIndicator,
    ProgressBar,
    Static,
)

from vmctl.errors import VmctlError
from vmctl.transfer.models import (
    UploadCancelledError,
    UploadPreview,
    UploadProgress,
    UploadRequest,
    UploadResult,
    UploadUncertainError,
)
from vmctl.tui.dialogs import ConfirmScreen, TextScreen
from vmctl.tui.upload import LocalSourceScreen, UploadProgressScreen, UploadScreen


class FakeUploader:
    def __init__(self, backend: FakeOperations) -> None:
        self.backend = backend
        self.planned: list[UploadRequest] = []
        self.applied: list[UploadPreview] = []
        self.error: Exception | None = None
        self.cancel_gate = False
        self.started = Event()
        self.plan_gate: Event | None = None
        self.plan_started = Event()
        self.wrong_target = False

    def plan(self, request: UploadRequest) -> UploadPreview:
        self.planned.append(request)
        self.plan_started.set()
        if self.plan_gate is not None:
            assert self.plan_gate.wait(timeout=10)
        vm = next(row.vm for row in self.backend.rows if str(row.vm.vmid) == request.reference)
        if self.wrong_target:
            vm = replace(vm, vmid=999)
        return UploadPreview(
            vm=vm,
            ip=IPv4Address("10.210.0.104"),
            username=request.username or "vmadmin",
            source=request.source,
            destination=PurePosixPath("/home/vmadmin") / request.source.name,
            file_count=2,
            total_bytes=4096,
            fingerprint="exact-transfer",
            request=request,
        )

    def upload(
        self,
        expected: UploadPreview,
        *,
        progress: Callable[[UploadProgress], None],
        cancel: Event | None = None,
    ) -> UploadResult:
        self.applied.append(expected)
        progress(UploadProgress("project/readme.txt", 1024, 4096, 1, 2))
        self.started.set()
        if self.cancel_gate:
            assert cancel is not None
            assert cancel.wait(timeout=10)
            raise UploadCancelledError("Upload cancelled. Staged data was removed.")
        if self.error is not None:
            raise self.error
        progress(UploadProgress("project/image.png", 4096, 4096, 2, 2))
        return UploadResult(expected.vm.vmid, expected.destination, 2, 4096)


async def preview_upload(pilot: Pilot[None], screen: UploadScreen, source: Path) -> UploadPreview:
    """Dispatch input edits before the real preview button captures its generation."""
    screen.query_one("#upload-source", Input).value = str(source)
    await pilot.pause()
    assert await pilot.click("#upload-plan")
    await screen.vmctl.workers.wait_for_complete()
    await pilot.pause()
    assert screen.preview is not None, (
        f"busy={screen.vmctl.busy}, stale={screen.vmctl.stale}, "
        f"error={screen.query_one('#upload-error', Static).render()}, "
        f"preview={screen.query_one('#upload-preview', Static).render()}"
    )
    return screen.preview


def test_upload_read_only_preview_and_narrow_actions(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        app = app_for(backend, read_only=True)
        uploader = FakeUploader(backend)
        app.uploader = uploader
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            actions = list(app.screen.query("#main-actions Button"))
            assert len(actions) == 13
            assert all(0 <= button.region.y < button.region.bottom <= 23 for button in actions)
            assert not app.query_one("#upload-action", Button).disabled
            await pilot.click("#upload-action")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UploadScreen)
            assert screen.reference == "104"
            assert screen.query_one("#upload-user", Input).value == "vmadmin"
            assert screen.query_one("#upload-destination", Input).value == "~"
            assert not screen.query_one("#upload-overwrite", Checkbox).value
            await preview_upload(pilot, screen, tmp_path / "project")
            assert "Guest target: /home/vmadmin/project" in str(
                screen.query_one("#upload-preview", Static).render()
            )
            assert screen.query_one("#upload-send", Button).disabled
            assert "Read-only mode" in str(screen.query_one("#upload-error", Static).render())
            screen.confirm()
            screen.execute(screen.preview)
            await pilot.pause()
            assert uploader.applied == []
            assert all(
                button.region.bottom <= 23 for button in screen.query("#upload-buttons Button")
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case", ["missing-uploader", "stopped", "no-ip", "template", "reserved", "stale"]
)
def test_upload_action_requires_running_ordinary_vm_with_ip(case: str) -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        app = app_for(backend)
        app.uploader = None if case == "missing-uploader" else FakeUploader(backend)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            target = backend.rows[0]
            if case == "stopped":
                backend.rows[0] = replace(target, vm=replace(target.vm, status="stopped"))
            elif case == "no-ip":
                backend.rows[0] = replace(target, ip=None)
            elif case == "template":
                backend.rows[0] = replace(target, vm=replace(target.vm, template=True))
            elif case == "reserved":
                backend.rows[0] = replace(target, vm=replace(target.vm, vmid=9000))
                app.selected_id = 9000
            elif case == "stale":
                app.stale = True
            app.update_actions()
            assert app.query_one("#upload-action", Button).disabled
            app.open_upload()
            assert app.screen is app.screen_stack[0]

    asyncio.run(scenario())


def test_upload_confirmation_freezes_vmid_and_preview(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        backend.rows.append(replace(backend.rows[0], vm=replace(backend.rows[0].vm, vmid=200)))
        app = app_for(backend)
        uploader = FakeUploader(backend)
        app.uploader = uploader
        async with app.run_test(size=(110, 36)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_upload()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UploadScreen)
            screen.query_one("#upload-user", Input).value = "other-user"
            screen.query_one("#upload-overwrite", Checkbox).value = True
            preview = await preview_upload(pilot, screen, tmp_path / "project")
            screen.confirm()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            assert "VM: 104" in app.screen.message_text
            assert "User: other-user" in app.screen.message_text
            assert "Replace existing regular file: yes" in app.screen.message_text
            await pilot.click("#cancel")
            await pilot.pause()
            assert uploader.applied == []
            screen.confirm()
            await pilot.pause()
            app.selected_id = 200
            await pilot.click("#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert uploader.applied == [preview]
            assert uploader.applied[0].request.reference == "104"
            assert isinstance(app.screen, TextScreen)
            assert "VMID: 104" in app.screen.content
            assert "Transferred: 4.0 KiB" in app.screen.content

    asyncio.run(scenario())


def test_editing_source_during_confirmation_invalidates_transfer(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        app = app_for(backend)
        uploader = FakeUploader(backend)
        app.uploader = uploader
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_upload()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UploadScreen)
            await preview_upload(pilot, screen, tmp_path / "one")
            screen.confirm()
            await pilot.pause()
            screen.query_one("#upload-source", Input).value = str(tmp_path / "two")
            await pilot.pause()
            await pilot.click("#confirm")
            await pilot.pause()
            assert screen.preview is None
            assert uploader.applied == []

    asyncio.run(scenario())


@pytest.mark.parametrize("uncertain", [False, True])
def test_upload_failure_invalidates_preview_without_retry(tmp_path: Path, uncertain: bool) -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        app = app_for(backend)
        uploader = FakeUploader(backend)
        uploader.error = (
            UploadUncertainError(104, PurePosixPath("/home/vmadmin/project"))
            if uncertain
            else VmctlError("Transfer failed before publication; staged data removed.")
        )
        app.uploader = uploader
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_upload()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UploadScreen)
            await preview_upload(pilot, screen, tmp_path / "project")
            screen.execute(screen.preview)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen is screen
            assert screen.preview is None
            assert app.stale is uncertain
            assert len(uploader.applied) == 1
            if uncertain:
                assert "Inspect /home/vmadmin/project" in str(
                    screen.query_one("#upload-error", Static).render()
                )
                screen.plan()
                assert len(uploader.planned) == 1

    asyncio.run(scenario())


def test_upload_cancel_stays_clickable_while_transfer_is_busy(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        app = app_for(backend)
        uploader = FakeUploader(backend)
        uploader.cancel_gate = True
        app.uploader = uploader
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_upload()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UploadScreen)
            await preview_upload(pilot, screen, tmp_path / "project")
            screen.execute(screen.preview)
            await pilot.pause()
            assert uploader.started.is_set()
            assert app.busy
            assert app.screen_stack[0].query_one("#connection-loading", LoadingIndicator).display
            progress = app.screen
            assert isinstance(progress, UploadProgressScreen)
            button = progress.query_one("#upload-cancel", Button)
            assert not button.disabled
            assert 0 < button.region.y < button.region.bottom <= 24
            assert "1.0 KiB / 4.0 KiB" in str(progress.query_one("#upload-counts", Static).render())
            await pilot.click("#upload-cancel")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen is screen
            assert not app.busy
            assert (
                not app.screen_stack[0].query_one("#connection-loading", LoadingIndicator).display
            )
            assert not app.stale
            assert screen.preview is None
            assert "cancelled" in str(screen.query_one("#upload-error", Static).render())
            assert len(uploader.applied) == 1

    asyncio.run(scenario())


def test_progress_shows_actual_bytes_throughput_and_publication_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [100.0]
    monkeypatch.setattr("vmctl.tui.upload.monotonic", lambda: clock[0])

    async def scenario() -> None:
        backend = FakeOperations()
        uploader = FakeUploader(backend)
        preview = uploader.plan(UploadRequest("104", tmp_path / "project"))
        app = app_for(backend)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            progress = UploadProgressScreen(preview, Event())
            app.push_screen(progress)
            await pilot.pause()
            assert progress.query_one("#upload-progress-bar", ProgressBar).total is None
            clock[0] = 102.0
            progress.append(UploadProgress("project/readme.txt", 1024, 4096, 1, 2))
            bar = progress.query_one("#upload-progress-bar", ProgressBar)
            assert bar.total == 4096
            assert bar.progress == 1024
            assert progress.samples == [512.0]
            text = str(progress.query_one("#upload-speed", Static).render())
            assert "512 B/s" in text
            assert "ETA: 0:06" in text
            assert "Elapsed: 0:02" in text
            clock[0] = 104.0
            progress.append(UploadProgress("project/image.png", 4096, 4096, 2, 2))
            assert bar.total is None
            assert "publishing" in str(progress.query_one("#upload-stage", Static).render())
            assert "ETA: verifying" in str(progress.query_one("#upload-speed", Static).render())
            progress.append(UploadProgress("empty", 0, 0, 0, 0))
            assert bar.total is None
            assert "0 B / 0 B" in str(progress.query_one("#upload-counts", Static).render())

    asyncio.run(scenario())


def test_local_chooser_selects_files_and_directories(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        app = app_for(backend)
        app.uploader = FakeUploader(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_upload()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UploadScreen)
            for selected in (tmp_path / "folder", tmp_path / "file.txt"):
                screen.browse()
                await pilot.pause()
                chooser = app.screen
                assert isinstance(chooser, LocalSourceScreen)
                tree = chooser.query_one("#source-tree", DirectoryTree)
                event = (
                    DirectoryTree.DirectorySelected(tree.root, selected)
                    if selected.name == "folder"
                    else DirectoryTree.FileSelected(tree.root, selected)
                )
                chooser.selected(event)
                assert chooser.query_one("#source-path", Input).value == str(selected)
                await pilot.click("#source-use")
                await pilot.pause()
                assert screen.query_one("#upload-source", Input).value == str(selected)

    asyncio.run(scenario())


def test_upload_validation_and_wrong_preview_target_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        app = app_for(backend)
        uploader = FakeUploader(backend)
        uploader.wrong_target = True
        app.uploader = uploader
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_upload()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UploadScreen)
            screen.plan()
            assert uploader.planned == []
            assert "Choose a local source" in str(
                screen.query_one("#upload-error", Static).render()
            )
            screen.query_one("#upload-source", Input).value = str(tmp_path / "project")
            screen.query_one("#upload-destination", Input).value = ""
            await pilot.pause()
            screen.plan()
            assert uploader.planned == []
            screen.query_one("#upload-destination", Input).value = "~"
            await pilot.pause()
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.preview is None
            assert "different VM" in str(screen.query_one("#upload-error", Static).render())
            assert uploader.applied == []

    asyncio.run(scenario())


def test_editing_source_while_preview_runs_discards_late_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        app = app_for(backend)
        uploader = FakeUploader(backend)
        uploader.plan_gate = Event()
        app.uploader = uploader
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_upload()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UploadScreen)
            screen.query_one("#upload-source", Input).value = str(tmp_path / "one")
            await pilot.pause()
            screen.plan()
            await pilot.pause()
            assert uploader.plan_started.is_set()
            assert app.busy
            assert screen.query_one("#upload-plan", Button).disabled
            screen.query_one("#upload-source", Input).value = str(tmp_path / "two")
            await pilot.pause()
            uploader.plan_gate.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.preview is None
            assert screen.query_one("#upload-send", Button).disabled
            assert uploader.applied == []

    asyncio.run(scenario())


def test_unresolvable_local_home_is_actionable_and_chooser_still_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_expanduser = Path.expanduser

    def expanduser(path: Path) -> Path:
        # Windows expands ~user syntactically, even if that account does not exist.
        if path.parts and path.parts[0] == "~vmctl-user-that-does-not-exist":
            raise RuntimeError("Home directory is unavailable")
        return original_expanduser(path)

    monkeypatch.setattr(Path, "expanduser", expanduser)

    async def scenario() -> None:
        backend = FakeOperations()
        app = app_for(backend)
        uploader = FakeUploader(backend)
        app.uploader = uploader
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_upload()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UploadScreen)
            source = "~vmctl-user-that-does-not-exist/file"
            screen.query_one("#upload-source", Input).value = source
            await pilot.pause()
            screen.plan()
            assert uploader.planned == []
            assert "Cannot resolve" in str(screen.query_one("#upload-error", Static).render())
            screen.browse()
            await pilot.pause()
            chooser = app.screen
            assert isinstance(chooser, LocalSourceScreen)
            chooser.query_one("#source-path", Input).value = source
            await pilot.pause()
            await pilot.click("#source-cancel")
            await pilot.pause()
            assert app.screen is screen
            assert uploader.applied == []

    asyncio.run(scenario())
