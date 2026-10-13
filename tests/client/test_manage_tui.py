"""Management forms use fake worker previews and never touch a real VM."""

import asyncio
from dataclasses import replace
from ipaddress import IPv4Address
from threading import Event
from typing import Literal

import pytest
from test_tui import FakeOperations, app_for
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static

from vmctl.errors import UnknownOutcomeError
from vmctl.models import CloneRequest, HardwareRequest, SpiceRequest, VMDetails
from vmctl.operations import (
    ClonePreview,
    CloneResult,
    HardwarePreview,
    HardwareResult,
    Progress,
    SpicePreview,
    SpiceResult,
)
from vmctl.tui.dialogs import ConfirmScreen, TextScreen
from vmctl.tui.manage import ManageAction, ManagePreview, ManageScreen, result_text
from vmctl.utils.sizes import parse_size


class FakeManageOperations(FakeOperations):
    def __init__(self) -> None:
        super().__init__()
        self.applied: ManagePreview | None = None
        self.request_id = ""
        self.operation_error: Exception | None = None
        self.guest_status: Literal[
            "not-requested", "unavailable", "installed", "failed", "uncertain"
        ] = "installed"

    def target(self, reference: str) -> VMDetails:
        return next(row for row in self.rows if str(row.vm.vmid) == reference)

    def plan_clone(self, request: CloneRequest) -> ClonePreview:
        self.calls.append("plan_clone")
        return ClonePreview(self.target(request.reference).vm, request, "frozen-clone", False)

    def clone(self, expected: ClonePreview, *, request_id: str, progress: Progress) -> CloneResult:
        self.calls.append("clone")
        self.applied, self.request_id = expected, request_id
        if self.operation_error:
            raise self.operation_error
        progress("Copying source disks")
        vm = replace(expected.source, vmid=105, name=expected.request.name, status="stopped")
        address = IPv4Address("10.210.0.105")
        self.rows.append(VMDetails(vm, {}, {}, address))
        return CloneResult(vm, expected.source.vmid, "BC:24:11:AA:BB:CC", address, False, True)

    def plan_update(self, request: HardwareRequest) -> HardwarePreview:
        self.calls.append("plan_update")
        vm = self.target(request.reference).vm
        return HardwarePreview(
            vm,
            request,
            "frozen-hardware",
            vm.cpu,
            vm.memory_mib,
            request.cpu or vm.cpu,
            parse_size(request.memory, unit="MiB") if request.memory else vm.memory_mib,
        )

    def update(self, expected: HardwarePreview, *, progress: Progress) -> HardwareResult:
        self.calls.append("update")
        self.applied = expected
        progress("Updating CPU and memory")
        return HardwareResult(
            expected.vm, expected.new_cpu, expected.new_memory_mib, expected.request.restart
        )

    def plan_spice(self, request: SpiceRequest) -> SpicePreview:
        self.calls.append("plan_spice")
        return SpicePreview(
            self.target(request.reference).vm,
            request,
            "frozen-spice",
            "virtio",
            "virtio",
            False,
            "debian",
            True,
        )

    def spice(self, expected: SpicePreview, *, progress: Progress) -> SpiceResult:
        self.calls.append("spice")
        self.applied = expected
        progress("Checking virtual display")
        return SpiceResult(expected.vm, expected.target_vga, True, self.guest_status, guest_pid=73)


@pytest.mark.parametrize("action", ["clone", "edit", "spice"])
def test_manage_read_only_preview_and_narrow_actions(action: ManageAction) -> None:
    async def scenario() -> None:
        backend = FakeManageOperations()
        app = app_for(backend, read_only=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            for button in app.screen.query("#main-actions Button"):
                assert 0 <= button.region.y < button.region.bottom <= 23
            assert not app.query_one(f"#{action}-action", Button).disabled
            await pilot.click(f"#{action}-action")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ManageScreen)
            if action == "clone":
                screen.query_one("#clone-name", Input).value = "android-copy"
                assert not screen.query_one("#clone-start", Checkbox).value
                assert not screen.query_one("#clone-shutdown", Checkbox).value
            await pilot.pause()
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.preview is not None
            assert screen.reference == "104"
            assert screen.query_one("#manage-apply", Button).disabled
            assert "Read-only mode" in str(screen.query_one("#manage-error", Static).render())
            screen.confirm()
            screen.execute(screen.preview)
            await pilot.pause()
            assert app.screen is screen
            assert not {"clone", "update", "spice"}.intersection(backend.calls)
            for button in screen.query("#manage-buttons Button"):
                assert button.region.bottom <= 23
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is app.screen_stack[0]

    asyncio.run(scenario())


def test_clone_confirmation_keeps_original_target_after_selection_change() -> None:
    async def scenario() -> None:
        backend = FakeManageOperations()
        backend.rows.append(replace(backend.rows[0], vm=replace(backend.rows[0].vm, vmid=200)))
        app = app_for(backend)
        async with app.run_test(size=(110, 36)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_manage("clone")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ManageScreen)
            screen.query_one("#clone-name", Input).value = "android-copy"
            await pilot.pause()
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            preview = screen.preview
            assert isinstance(preview, ClonePreview)
            screen.confirm()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            assert "Source: 104" in app.screen.message_text
            app.selected_id = 200
            await pilot.click("#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert backend.applied is preview
            assert backend.applied.request.reference == "104"
            assert not backend.applied.request.start
            assert len(backend.request_id) == 32
            assert isinstance(app.screen, TextScreen)
            assert "Guest data, accounts and credentials were copied" in app.screen.content

    asyncio.run(scenario())


def test_resource_validation_and_edit_during_confirmation_discards_preview() -> None:
    async def scenario() -> None:
        backend = FakeManageOperations()
        app = app_for(backend)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_manage("edit")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ManageScreen)
            screen.query_one("#edit-cpu", Input).value = "0"
            await pilot.pause()
            screen.plan()
            assert "plan_update" not in backend.calls
            assert "positive integer" in str(screen.query_one("#manage-error", Static).render())
            screen.query_one("#edit-cpu", Input).value = "8"
            screen.query_one("#edit-memory", Input).value = "bad"
            await pilot.pause()
            screen.plan()
            assert "plan_update" not in backend.calls
            screen.query_one("#edit-memory", Input).value = "16G"
            screen.query_one("#edit-restart", Checkbox).value = True
            await pilot.pause()
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(screen.preview, HardwarePreview)
            assert screen.preview.new_cpu == 8
            assert screen.preview.new_memory_mib == 16384
            screen.confirm()
            await pilot.pause()
            screen.query_one("#edit-memory", Input).value = "32G"
            await pilot.pause()
            await pilot.click("#confirm")
            await pilot.pause()
            assert "update" not in backend.calls
            assert screen.preview is None
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            screen.confirm()
            await pilot.pause()
            await pilot.click("#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, TextScreen)
            assert "Memory: 32768 MiB" in app.screen.content
            assert "Restarted: yes" in app.screen.content

    asyncio.run(scenario())


@pytest.mark.parametrize("guest_status", ["failed", "uncertain"])
def test_spice_reports_partial_guest_result_and_preserves_supported_display(
    guest_status: Literal["failed", "uncertain"],
) -> None:
    async def scenario() -> None:
        backend = FakeManageOperations()
        backend.guest_status = guest_status
        app = app_for(backend)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_manage("spice")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ManageScreen)
            screen.query_one("#spice-tools", Select).value = "install"
            await pilot.pause()
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(screen.preview, SpicePreview)
            assert screen.preview.current_vga == screen.preview.target_vga == "virtio"
            screen.confirm()
            await pilot.pause()
            await pilot.click("#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, TextScreen)
            assert "SPICE display configured: yes" in app.screen.content
            assert f"Guest tools: {guest_status}" in app.screen.content
            assert "Guest process ID: 73" in app.screen.content
            assert backend.calls.count("spice") == 1
            if guest_status == "uncertain":
                assert "may still be running" in app.screen.content
            else:
                assert "manual attention" in app.screen.content

    asyncio.run(scenario())


def test_busy_preview_prevents_back_and_discards_changed_fields() -> None:
    async def scenario() -> None:
        backend = FakeManageOperations()
        entered, release = Event(), Event()
        original = backend.plan_update

        def slow(request: HardwareRequest) -> HardwarePreview:
            entered.set()
            assert release.wait(5)
            return original(request)

        backend.plan_update = slow
        app = app_for(backend)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_manage("edit")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ManageScreen)
            screen.plan()
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                assert screen.query_one("#edit-cpu", Input).disabled
                screen.key_escape()
                await app.action_quit()
                assert app.screen is screen and app.is_running
                screen.query_one("#edit-memory", Input).value = "16G"
                await pilot.pause()
            finally:
                release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.preview is None
            assert screen.query_one("#manage-apply", Button).disabled
            assert not screen.query_one("#edit-cpu", Input).disabled

    asyncio.run(scenario())


def test_unknown_clone_outcome_invalidates_plan_without_retry() -> None:
    async def scenario() -> None:
        backend = FakeManageOperations()
        backend.operation_error = UnknownOutcomeError("a" * 32, 105)
        app = app_for(backend)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_manage("clone")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ManageScreen)
            screen.query_one("#clone-name", Input).value = "android-copy"
            await pilot.pause()
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            screen.confirm()
            await pilot.pause()
            await pilot.click("#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen is screen
            assert app.stale
            assert screen.preview is None
            assert "outcome is unknown" in str(screen.query_one("#manage-error", Static).render())
            screen.plan()
            screen.confirm()
            assert backend.calls.count("clone") == 1
            assert backend.calls.count("plan_clone") == 1

    asyncio.run(scenario())


def test_selected_row_manage_button_and_protected_target_guards() -> None:
    async def scenario() -> None:
        backend = FakeManageOperations()
        backend.rows.append(replace(backend.rows[0], vm=replace(backend.rows[0].vm, vmid=200)))
        app = app_for(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.query_one("#vm-table", DataTable).move_cursor(row=1)
            await pilot.pause()
            assert app.selected_id == 200
            await pilot.click("#edit-action")
            await pilot.pause()
            assert isinstance(app.screen, ManageScreen)
            assert app.screen.reference == "200"
            await pilot.press("escape")
            await pilot.pause()
            protected = replace(backend.rows[0], vm=replace(backend.rows[0].vm, vmid=9000))
            app.inventory = [protected]
            app.selected_id = 9000
            app.update_actions()
            for action in ("clone", "edit", "spice"):
                assert app.query_one(f"#{action}-action", Button).disabled
                app.open_manage(action)
                assert len(app.screen_stack) == 1
            app.inventory, app.selected_id, app.stale = backend.rows, 104, True
            app.update_actions()
            for action in ("clone", "edit", "spice"):
                assert app.query_one(f"#{action}-action", Button).disabled

    asyncio.run(scenario())


def test_spice_skipped_tools_are_not_reported_as_installed() -> None:
    vm = FakeManageOperations().rows[0].vm
    assert "Guest tools: not-requested" in result_text(
        SpiceResult(vm, "virtio", True, "not-requested")
    )


@pytest.mark.parametrize(
    ("name", "address"), [("Invalid name", "auto"), ("valid-name", "not-an-ip")]
)
def test_clone_invalid_fields_never_reach_worker(name: str, address: str) -> None:
    async def scenario() -> None:
        backend = FakeManageOperations()
        app = app_for(backend)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_manage("clone")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ManageScreen)
            screen.query_one("#clone-name", Input).value = name
            screen.query_one("#clone-ip", Input).value = address
            await pilot.pause()
            screen.plan()
            assert "plan_clone" not in backend.calls
            assert screen.preview is None
            assert screen.query_one("#manage-apply", Button).disabled
            assert str(screen.query_one("#manage-error", Static).render())

    asyncio.run(scenario())


def test_worker_preview_for_different_vm_is_rejected() -> None:
    async def scenario() -> None:
        backend = FakeManageOperations()
        original = backend.plan_spice

        def wrong_vm(request: SpiceRequest) -> SpicePreview:
            preview = original(request)
            return replace(preview, vm=replace(preview.vm, vmid=200))

        backend.plan_spice = wrong_vm
        app = app_for(backend)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_manage("spice")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ManageScreen)
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.preview is None
            assert "different VM" in str(screen.query_one("#manage-error", Static).render())
            screen.confirm()
            assert "spice" not in backend.calls

    asyncio.run(scenario())


def test_long_confirmation_is_scrollable_and_buttons_visible_on_small_terminal() -> None:
    async def scenario() -> None:
        backend = FakeManageOperations()
        original = backend.plan_clone

        def long_notes(request: CloneRequest) -> ClonePreview:
            return replace(
                original(request), notes=tuple(f"Guest note {index}" for index in range(30))
            )

        backend.plan_clone = long_notes
        app = app_for(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.open_manage("clone")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ManageScreen)
            screen.query_one("#clone-name", Input).value = "android-copy"
            await pilot.pause()
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            screen.confirm()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            for selector in ("#cancel", "#confirm"):
                button = app.screen.query_one(selector, Button)
                assert 0 < button.region.y < button.region.bottom <= 24
            await pilot.click("#cancel")
            await pilot.pause()
            assert app.screen is screen
            assert "clone" not in backend.calls

    asyncio.run(scenario())
