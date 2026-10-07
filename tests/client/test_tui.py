"""Portable headless UI checks using a fake worker, never real VM execution."""

import asyncio
from dataclasses import replace
from ipaddress import IPv4Address
from pathlib import Path

import pytest
from textual.widgets import Button, Checkbox, DataTable, Input, Static

from vmctl.config import Preset, Template
from vmctl.errors import UnknownOutcomeError, VmctlError
from vmctl.models import VM, CreateRequest, CreateResult, Resources, VMDetails
from vmctl.operations import (
    ActionPreview,
    ActionResult,
    Catalog,
    CatalogItem,
    CreatePreview,
    DeletePreview,
    LifecycleAction,
    Progress,
    ValidationSummary,
)
from vmctl.tui.app import VmctlApp
from vmctl.tui.create import CreateScreen
from vmctl.tui.dialogs import ConfirmScreen, PasswordScreen, TextScreen
from vmctl.tui.features import FeatureChoices
from vmctl.tui.formatting import details_text, result_text
from vmctl.tui.selection import FeatureSelection


def catalog() -> Catalog:
    return Catalog(
        templates={
            "ubuntu-server": Template(vmid=9000, family="debian", distro="ubuntu", release="24.04"),
            "ubuntu-desktop": Template(
                vmid=9001, family="debian", distro="ubuntu", release="25.10", desktop=True
            ),
            "alpine": Template(vmid=9020, family="alpine", distro="alpine", release="3.22"),
        },
        presets={
            "small": Preset(cpu=2, memory_mib=2048, disk_gib=20),
            "normal": Preset(cpu=4, memory_mib=8192, disk_gib=40),
        },
        modules=(
            CatalogItem(
                "base",
                "Basic development utilities.",
                ("ubuntu-server", "ubuntu-desktop", "alpine"),
            ),
            CatalogItem(
                "rust", "Rust compiler and Cargo.", ("ubuntu-server", "ubuntu-desktop"), ("base",)
            ),
            CatalogItem("node", "Node.js and npm.", ("ubuntu-server", "ubuntu-desktop"), ("base",)),
        ),
        profiles=(
            CatalogItem(
                "surge-dev",
                "Rust and Node development tools.",
                ("ubuntu-server", "ubuntu-desktop"),
                members=("rust", "node"),
            ),
        ),
        system_features=(
            CatalogItem(
                "qemu-agent",
                "QEMU guest integration.",
                ("ubuntu-server", "ubuntu-desktop", "alpine"),
            ),
            CatalogItem(
                "desktop-rdp",
                "Remote desktop through XRDP and GNOME Flashback.",
                ("ubuntu-desktop",),
                ("qemu-agent",),
            ),
        ),
        pool_start=IPv4Address("10.210.0.100"),
        pool_end=IPv4Address("10.210.0.199"),
        system_defaults={
            "ubuntu-server": ("qemu-agent",),
            "ubuntu-desktop": ("qemu-agent", "desktop-rdp"),
            "alpine": ("qemu-agent",),
        },
    )


class FakeOperations:
    def __init__(self) -> None:
        self.data = catalog()
        self.rows = [
            VMDetails(
                VM(104, "server", "running", "pve", cpu=2, memory_mib=2048),
                {"ciuser": "vmadmin"},
                {"template": "ubuntu-server", "desktop": "false", "system_features": "qemu-agent"},
                IPv4Address("10.210.0.104"),
            )
        ]
        self.calls: list[str] = []
        self.created: CreateRequest | None = None
        self.action_preview: ActionPreview | None = None
        self.deleted_preview: DeletePreview | None = None
        self.list_error: Exception | None = None
        self.create_error: Exception | None = None

    def catalog(self) -> Catalog:
        return self.data

    def list(self) -> list[VMDetails]:
        self.calls.append("list")
        if self.list_error:
            raise self.list_error
        return self.rows

    def info(self, reference: str) -> VMDetails:
        self.calls.append("info")
        return next(row for row in self.rows if str(row.vm.vmid) == reference)

    def plan_create(self, request: CreateRequest) -> CreatePreview:
        self.calls.append("plan_create")
        desktop = self.data.templates[request.template].desktop
        return CreatePreview(
            self.data.templates[request.template].vmid,
            Resources(request.cpu or 4, 8192, 40),
            "scsi0",
            10,
            "vmbr1",
            "customuser",
            tuple(
                item.name
                for item in FeatureSelection(
                    self.data, request.template, development=set(request.modules)
                ).resolve(system=False)
            ),
            request.system_features,
            desktop,
            desktop and not request.no_desktop_password,
        )

    def create(
        self, request: CreateRequest, *, request_id: str, progress: Progress
    ) -> CreateResult:
        self.calls.append("create")
        self.created = request
        if self.create_error:
            raise self.create_error
        progress("Cloning template")
        return CreateResult(
            105,
            request.name,
            request.template,
            request.preset,
            Resources(4, 8192, 40),
            "BC:24:11:AA:BB:CC",
            IPv4Address("10.210.0.105"),
            "customuser",
            request.start,
            request.modules,
            "not waited",
            request.system_features,
            self.data.templates[request.template].desktop,
            "desktop-rdp" in request.system_features,
        )

    def plan_delete(self, reference: str) -> DeletePreview:
        self.calls.append("plan_delete")
        return DeletePreview(self.rows[0].vm, "frozen-delete")

    def delete(self, expected: DeletePreview) -> int:
        self.calls.append("delete")
        self.deleted_preview = expected
        self.rows = []
        return expected.vm.vmid

    def plan_action(self, reference: str, action: LifecycleAction) -> ActionPreview:
        self.calls.append("plan_action")
        return ActionPreview(self.rows[0].vm, "frozen-action", action)

    def action(self, expected: ActionPreview) -> ActionResult:
        self.calls.append("action")
        self.action_preview = expected
        return ActionResult(expected.vm, expected.action)

    def templates(self) -> dict[str, Template]:
        return self.data.templates

    def presets(self) -> dict[str, Preset]:
        return self.data.presets

    def validate_config(self) -> ValidationSummary:
        return ValidationSummary(3, 2, 2, 3, 1)


def app_for(backend: FakeOperations, *, read_only: bool = False) -> VmctlApp:
    def public_key(path: Path | None) -> str:
        return "ssh-ed25519 AAAA mock"

    return VmctlApp(backend, target="pxmx", public_key=public_key, read_only=read_only)


def test_selection_defaults_dependencies_and_independence() -> None:
    selection = FeatureSelection(catalog(), "ubuntu-server")
    selection.set_template("ubuntu-server")
    assert selection.selected_names(system=True) == ("qemu-agent",)
    assert selection.selected_names(system=False) == ()
    selection.set_template("ubuntu-desktop")
    assert selection.selected_names(system=True) == ("qemu-agent", "desktop-rdp")
    assert "qemu-agent" in selection.required_names(system=True)
    selection.toggle("desktop-rdp", False, system=True)
    assert selection.without_system() == ("desktop-rdp",)
    assert "qemu-agent" not in selection.required_names(system=True)
    selection.toggle("surge-dev", True, system=False)
    assert selection.selected_names(system=False) == ("base", "rust", "node")
    assert selection.selected_names(system=True) == ("qemu-agent",)
    selection.set_template("alpine")
    assert selection.selected_names(system=False) == ()


def test_rdp_information_matches_enabled_desktop() -> None:
    backend = FakeOperations()
    assert "RDP:" not in details_text(backend.rows[0])
    desktop = replace(
        backend.rows[0], metadata={"desktop": "true", "system_features": "qemu-agent,desktop-rdp"}
    )
    assert "RDP: 10.210.0.104:3389" in details_text(desktop)
    assert "RDP:" not in details_text(
        replace(desktop, metadata={"desktop": "true", "system_features": "qemu-agent"})
    )
    result = backend.create(
        CreateRequest("test", "ubuntu-server", "normal"),
        request_id="unused",
        progress=lambda _: None,
    )
    assert "RDP:" not in result_text(result)
    assert "RDP: 10.210.0.105:3389" in result_text(replace(result, desktop=True, rdp_enabled=True))


def test_dashboard_wide_narrow_search_stale_and_selection() -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        app = app_for(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.selected_id == 104
            assert app.query_one("#vm-table", DataTable).row_count == 1
            assert not app.screen.has_class("narrow")
            backend.data = replace(
                backend.data, presets={"custom": Preset(cpu=8, memory_mib=8192, disk_gib=80)}
            )
            app.action_refresh()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.catalog_data is backend.data
            assert "custom" in app.catalog_data.presets
            assert app.selected_id == 104
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert app.screen.has_class("narrow")
            app.query_one("#search", Input).value = "nothing"
            await pilot.pause()
            assert app.query_one("#vm-table", DataTable).row_count == 0
            app.query_one("#search", Input).value = "server"
            await pilot.pause()
            assert app.selected_id == 104
            backend.list_error = VmctlError("SSH unavailable")
            app.action_refresh()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.stale
            assert app.query_one("#vm-table", DataTable).row_count == 1
            assert app.query_one("#delete-action", Button).disabled
            assert "STALE" in str(app.query_one("#connection", Static).render())

    asyncio.run(scenario())


def test_creation_form_descriptions_dependencies_read_only_preview() -> None:
    async def scenario() -> None:
        app = app_for(FakeOperations(), read_only=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_new()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CreateScreen)
            assert not screen.query_one("#dev-rust", Checkbox).value
            assert screen.query_one("#system-qemu-agent", Checkbox).value
            assert screen.query_one("#system-desktop-rdp", Checkbox).disabled
            assert "Rust compiler" in " ".join(
                str(widget.render()) for widget in screen.query(Static)
            )
            screen.query_one("#dev-rust", Checkbox).value = True
            await pilot.pause()
            assert screen.query_one("#dev-base", Checkbox).value
            assert screen.query_one("#dev-base", Checkbox).disabled
            screen.query_one("#vm-name", Input).value = "test-vm"
            await pilot.pause()
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.preview is not None
            assert screen.query_one("#create", Button).disabled
            screen.confirm_create()
            assert app.screen is screen
            assert "create" not in app.backend.calls

    asyncio.run(scenario())


def test_lifecycle_and_delete_confirm_exact_preview() -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        app = app_for(backend)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.plan_action("shutdown")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            assert "action" not in backend.calls
            await pilot.click("#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert backend.action_preview is not None
            assert backend.action_preview.fingerprint == "frozen-action"
            app.plan_delete()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.click("#cancel")
            await pilot.pause()
            assert "delete" not in backend.calls
            app.plan_delete()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.click("#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert backend.deleted_preview is not None
            assert backend.deleted_preview.fingerprint == "frozen-delete"

    asyncio.run(scenario())


def test_desktop_password_masked_confirmed_not_logged(capsys: pytest.CaptureFixture[str]) -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        app = app_for(backend)
        async with app.run_test(size=(100, 36)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_new()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CreateScreen)
            screen.template_name = "ubuntu-desktop"
            screen.selection.set_template("ubuntu-desktop")
            screen.query_one(FeatureChoices).update_choices()
            screen.query_one("#vm-name", Input).value = "desktop-test"
            await pilot.pause()
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            screen.confirm_create()
            await pilot.pause()
            await pilot.click("#confirm")
            await pilot.pause()
            assert isinstance(app.screen, PasswordScreen)
            assert app.screen.username == "customuser"
            password = app.screen.query_one("#desktop-password", Input)
            confirmation = app.screen.query_one("#desktop-confirm", Input)
            assert password.password and confirmation.password
            password.value = "unique-password-never-output"
            confirmation.value = "different"
            await pilot.click("#continue")
            await pilot.pause()
            assert password.value == confirmation.value == ""
            assert backend.created is None
            await pilot.pause(0.3)
            password.value = confirmation.value = "unique-password-never-output"
            await pilot.click("#continue")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert backend.created is not None
            assert backend.created.desktop_password_hash is not None
            assert password.value == confirmation.value == ""
            assert "unique-password-never-output" not in repr(backend.created)
            assert isinstance(app.screen, TextScreen)
            assert "unique-password-never-output" not in app.screen.content

    asyncio.run(scenario())
    captured = capsys.readouterr()
    assert "unique-password-never-output" not in captured.out + captured.err


def test_server_create_skips_password_and_unknown_outcome_not_retried() -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        backend.create_error = UnknownOutcomeError("a" * 32, 105)
        app = app_for(backend)
        async with app.run_test(size=(100, 36)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_new()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CreateScreen)
            screen.query_one("#vm-name", Input).value = "server-test"
            await pilot.pause()
            screen.plan()
            await app.workers.wait_for_complete()
            await pilot.pause()
            screen.confirm_create()
            await pilot.pause()
            await pilot.click("#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen is screen
            assert backend.calls.count("create") == 1
            assert screen.preview is None
            assert screen.query_one("#create", Button).disabled
            assert "outcome is unknown" in str(screen.query_one("#create-error", Static).render())

    asyncio.run(scenario())


def test_unsupported_selection_does_not_poison_state() -> None:
    import pytest

    selection = FeatureSelection(catalog(), "alpine")
    selection.set_template("alpine")
    with pytest.raises(VmctlError, match="not supported"):
        selection.toggle("rust", True, system=False)
    assert selection.development == set()


def test_preflight_changed_settings_discards_old_preview() -> None:
    from threading import Event

    async def scenario() -> None:
        backend = FakeOperations()
        entered, release = Event(), Event()
        original = backend.plan_create

        def slow(request: CreateRequest) -> CreatePreview:
            entered.set()
            assert release.wait(5)
            return original(request)

        backend.plan_create = slow
        app = app_for(backend)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_new()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CreateScreen)
            screen.query_one("#vm-name", Input).value = "before"
            await pilot.pause()
            screen.plan()
            try:
                await asyncio.to_thread(entered.wait, 2)
                assert screen.query_one("#vm-name", Input).disabled
                screen.query_one("#vm-name", Input).value = "after"
                await pilot.pause()
            finally:
                release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.preview is None
            assert screen.query_one("#create", Button).disabled
            assert not screen.query_one("#vm-name", Input).disabled

    asyncio.run(scenario())


def test_read_only_lifecycle_and_delete_never_execute() -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        app = app_for(backend, read_only=True)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.plan_action("shutdown")
            app.plan_delete()
            await pilot.pause()
            assert "plan_action" not in backend.calls
            assert "plan_delete" not in backend.calls
            for name in ("start", "shutdown", "reboot", "delete"):
                assert app.query_one(f"#{name}-action", Button).disabled

    asyncio.run(scenario())


def test_busy_operation_prevents_quit_and_duplicate_actions() -> None:
    from threading import Event

    async def scenario() -> None:
        backend = FakeOperations()
        app = app_for(backend)
        entered, release = Event(), Event()
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            def slow() -> int:
                entered.set()
                assert release.wait(5)
                return 1

            app.submit("Slow action", slow, lambda _: None)
            try:
                await asyncio.to_thread(entered.wait, 2)
                await app.action_quit()
                app.plan_action("shutdown")
                assert app.is_running
                assert "plan_action" not in backend.calls
            finally:
                release.set()
            await app.workers.wait_for_complete()

    asyncio.run(scenario())


def test_fatal_error_never_renders_exception_locals(capsys: pytest.CaptureFixture[str]) -> None:
    import pytest

    async def scenario() -> None:
        app = app_for(FakeOperations())
        with pytest.raises(RuntimeError, match="Unexpected crash"):
            async with app.run_test() as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()

                def crash() -> None:
                    password = "fatal-password-never-render"
                    assert password
                    raise RuntimeError("Unexpected crash")

                app.call_later(crash)
                await pilot.pause()

    asyncio.run(scenario())
    captured = capsys.readouterr()
    assert "fatal-password-never-render" not in captured.out + captured.err
    assert "Unexpected interface error" in captured.err
