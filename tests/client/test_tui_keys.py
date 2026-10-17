"""Headless public-key selection and guarded guest export."""

import asyncio
import base64
import struct
from dataclasses import replace
from pathlib import Path

from test_tui import FakeOperations
from textual.widgets import Button, Checkbox, Input, Static

from vmctl.keys import PublicKeyRegistryStore, key_fingerprint
from vmctl.models import KeyExportRequest
from vmctl.operations import KeyExportPreview, KeyExportResult, Progress
from vmctl.tui.app import VmctlApp
from vmctl.tui.dialogs import TextScreen
from vmctl.tui.keys import KeyExportScreen
from vmctl.tui.manage import ManageConfirmScreen

_KEY_BLOB = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes(range(32))
IPHONE = "ssh-ed25519 " + base64.b64encode(_KEY_BLOB).decode() + " test-phone"


class KeyOperations(FakeOperations):
    def __init__(self) -> None:
        super().__init__()
        self.key_request: KeyExportRequest | None = None
        self.key_preview: KeyExportPreview | None = None

    def plan_key_export(self, request: KeyExportRequest) -> KeyExportPreview:
        self.calls.append("plan_key_export")
        self.key_request = request
        return KeyExportPreview(self.rows[0].vm, request, "frozen-key-export")

    def export_keys(self, expected: KeyExportPreview, *, progress: Progress) -> KeyExportResult:
        self.calls.append("export_keys")
        self.key_preview = expected
        progress("Adding selected public keys")
        return KeyExportResult(
            expected.vm, expected.request.username or "vmadmin", 1, 0, (key_fingerprint(IPHONE),)
        )


def make_app(
    path: Path, *, read_only: bool = False, empty: bool = False
) -> tuple[VmctlApp, KeyOperations]:
    store = PublicKeyRegistryStore(path)
    if not empty:
        store.add("iphone", IPHONE, description="Termius on iPhone", default=True)
    backend = KeyOperations()
    return VmctlApp(
        backend, target="pxmx", public_key=lambda _: None, key_store=store, read_only=read_only
    ), backend


def test_key_selection_preview_confirm_export(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, backend = make_app(tmp_path / "keys.toml")
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("ctrl+k")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, KeyExportScreen)
            assert screen.query_one("#key-choice-0", Checkbox).value
            assert "Termius on iPhone" in " ".join(
                str(widget.render()) for widget in screen.query(Static)
            )
            screen.query_one("#keys-user", Input).value = "customuser"
            await pilot.pause()
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.preview is not None
            assert backend.key_request is not None
            assert backend.key_request.username == "customuser"
            assert backend.key_request.public_key.strip() == IPHONE
            screen.confirm()
            await pilot.pause()
            assert isinstance(app.screen, ManageConfirmScreen)
            assert "export_keys" not in backend.calls
            await pilot.click("#cancel")
            await pilot.pause()
            assert "export_keys" not in backend.calls
            screen.confirm()
            await pilot.pause()
            await pilot.click("#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert backend.key_preview is not None
            assert backend.key_preview.fingerprint == "frozen-key-export"
            assert isinstance(app.screen, TextScreen)
            assert "Added: 1" in app.screen.content

    asyncio.run(scenario())


def test_read_only_preview_and_selection_invalidation(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, backend = make_app(tmp_path / "keys.toml", read_only=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_keys()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, KeyExportScreen)
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.preview is not None
            assert screen.query_one("#keys-apply", Button).disabled
            screen.confirm()
            assert app.screen is screen
            assert "export_keys" not in backend.calls
            screen.query_one("#key-choice-0", Checkbox).value = False
            await pilot.pause()
            assert screen.preview is None
            screen.plan()
            assert "Select at least one" in str(screen.query_one("#keys-error", Static).render())
            screen.key_escape()
            await pilot.pause()
            assert len(app.screen_stack) == 1

    asyncio.run(scenario())


def test_empty_registry_and_stopped_vm(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, backend = make_app(tmp_path / "missing.toml", empty=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_keys()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, KeyExportScreen)
            assert "vmctl keys add" in str(screen.query_one("#keys-empty", Static).render())
            assert screen.query_one("#keys-plan", Button).disabled
            screen.key_escape()
            await pilot.pause()
            backend.rows = [
                replace(backend.rows[0], vm=replace(backend.rows[0].vm, status="stopped"))
            ]
            app.action_refresh()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#keys-action", Button).disabled
            app.action_keys()
            assert len(app.screen_stack) == 1
            assert "export_keys" not in backend.calls

    asyncio.run(scenario())


def test_export_uses_displayed_key_snapshot_and_rejects_changed_target(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, backend = make_app(tmp_path / "keys.toml")
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_keys()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, KeyExportScreen)
            # Removing a registry entry after opening cannot change the key being confirmed.
            screen.store.remove("iphone")
            assert screen.request().public_key.strip() == IPHONE
            original = backend.plan_key_export

            def wrong_target(request: KeyExportRequest) -> KeyExportPreview:
                preview = original(request)
                return replace(preview, vm=replace(preview.vm, vmid=999))

            backend.plan_key_export = wrong_target
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.preview is None
            assert "changed the selected VM" in str(
                screen.query_one("#keys-error", Static).render()
            )
            assert screen.query_one("#keys-apply", Button).disabled
            screen.confirm()
            assert "export_keys" not in backend.calls

    asyncio.run(scenario())
