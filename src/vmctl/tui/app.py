"""Workstation VM dashboard using the typed operations adapter."""

from collections.abc import Callable
from datetime import datetime
from ipaddress import IPv4Address
from pathlib import Path

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, Select, Static

from vmctl.errors import UncertainOperationError, UnknownOutcomeError
from vmctl.models import CreateResult, VMDetails
from vmctl.operations import ActionPreview, Catalog, DeletePreview, LifecycleAction, Operations
from vmctl.tui.create import CreateScreen
from vmctl.tui.details import VMDetailsScreen
from vmctl.tui.dialogs import ConfirmScreen, TextScreen
from vmctl.tui.formatting import details_text, error_text, result_text
from vmctl.tui.table import VMTable
from vmctl.utils.clipboard import copy_native


class DashboardScreen(Screen[None]):
    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 110, "narrow")


class VmctlApp(App[None]):
    TITLE = "vmctl"
    CSS_PATH = "vmctl.tcss"
    BINDINGS = [
        Binding("ctrl+n", "new", "New VM"),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("/", "search", "Search"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        backend: Operations,
        *,
        target: str,
        public_key: Callable[[Path | None], str | None],
        read_only: bool = False,
    ) -> None:
        super().__init__()
        self.backend, self.target, self.public_key, self.read_only = (
            backend,
            target,
            public_key,
            read_only,
        )
        self.catalog_data: Catalog | None = None
        self.inventory: list[VMDetails] = []
        self.selected_id: int | None = None
        self.busy = False
        self.last_success: datetime | None = None
        self.stale = False

    def get_default_screen(self) -> Screen[None]:
        return DashboardScreen()

    def _fatal_error(self) -> None:
        # Textual's default traceback includes locals, which may contain passwords.
        self.panic(
            "Unexpected interface error. No operation was retried. Check VM state before continuing."
        )

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            f"Target: {self.target}" + (" | READ ONLY" if self.read_only else ""),
            id="target",
            markup=False,
        )
        yield Static("Connecting to worker...", id="connection", markup=False)
        yield Input(placeholder="Search by name, VMID, template or IP", id="search")
        with Horizontal(id="dashboard"):
            yield VMTable(id="vm-table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="details-panel"):
                yield Static("Select a VM to see its details.", id="details", markup=False)
        with Grid(id="main-actions"):
            yield Button("New VM", id="new", variant="primary")
            yield Button("Refresh", id="refresh")
            yield Button("Details", id="details-button")
            yield Button("Start", id="start-action")
            yield Button("Shutdown", id="shutdown-action")
            yield Button("Reboot", id="reboot-action")
            yield Button("Delete", id="delete-action", variant="error")
            yield Button("Quit", id="quit-button")
        yield Footer()

    def on_mount(self) -> None:
        table: DataTable[str] = self.query_one("#vm-table", DataTable)
        table.add_columns("VMID", "Name", "Status", "Template", "CPU", "RAM", "IP")
        table.tooltip = "Click a VM to view its metrics. Click an IP address to copy it."
        table.focus()
        self.resize_layout()
        self.action_refresh()
        self.set_interval(15, self.periodic_refresh)

    def on_resize(self) -> None:
        if self.screen_stack and self.screen_stack[0].is_mounted:
            self.resize_layout()

    def resize_layout(self) -> None:
        self.screen_stack[0].set_class(self.size.width < 110, "narrow")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"search", "quit"} and isinstance(self.focused, (Input, Select)):
            return False
        if action in {"new", "refresh", "search"} and len(self.screen_stack) > 1:
            return False
        return super().check_action(action, parameters)

    def connection_status(self, message: str) -> None:
        self.screen_stack[0].query_one("#connection", Static).update(message)

    def submit[Result](
        self,
        label: str,
        operation: Callable[[], Result],
        success: Callable[[Result], None],
        failure: Callable[[Exception], None] | None = None,
    ) -> None:
        if self.busy:
            self.notify("An operation is already running.", severity="warning")
            return
        self.busy = True
        self.connection_status(label + "...")
        disabled = [
            (widget, widget.disabled)
            for widget in self.screen.query("Button, Input, Checkbox, Select, DataTable")
            if isinstance(widget, (Button, Input, Checkbox, Select, DataTable))
        ]
        for widget, _ in disabled:
            widget.disabled = True

        def finish(result: Result | None, error: Exception | None) -> None:
            self.busy = False
            for widget, was_disabled in disabled:
                if widget.is_mounted:
                    widget.disabled = was_disabled
            if error is not None:
                if isinstance(error, (UnknownOutcomeError, UncertainOperationError)):
                    self.stale = True
                self.connection_status("Error: " + error_text(error))
                if failure is not None:
                    failure(error)
                else:
                    self.notify(error_text(error), severity="error", timeout=15)
            else:
                # The operation completed; its declared result may itself be None.
                from typing import cast

                success(cast(Result, result))
            self.update_actions()

        def run() -> None:
            try:
                result = operation()
            except Exception as error:
                self.call_from_thread(finish, None, error)
            else:
                self.call_from_thread(finish, result, None)

        self.run_worker(run, thread=True, exit_on_error=False, name=label)

    def periodic_refresh(self) -> None:
        if not self.busy and len(self.screen_stack) == 1:
            self.action_refresh()

    def action_refresh(self) -> None:
        if self.busy:
            return

        def load() -> tuple[Catalog, list[VMDetails]]:
            return self.backend.catalog(), self.backend.list()

        self.submit("Refreshing VMs", load, self.refreshed, self.refresh_failed)

    def refreshed(self, loaded: tuple[Catalog, list[VMDetails]]) -> None:
        self.catalog_data, self.inventory = loaded
        self.last_success, self.stale = datetime.now(), False
        self.connection_status(
            f"Connected | Last update {self.last_success:%H:%M:%S} | {len(self.inventory)} VMs"
        )
        self.render_rows()

    def refresh_failed(self, error: Exception) -> None:
        self.stale = True
        last = (
            f" | Last successful update {self.last_success:%H:%M:%S}"
            if self.last_success
            else " | No inventory loaded"
        )
        self.connection_status("STALE: " + error_text(error) + last)
        self.update_actions()

    @on(Input.Changed, "#search")
    def search_changed(self) -> None:
        self.render_rows()

    def render_rows(self) -> None:
        main = self.screen_stack[0]
        table: DataTable[str] = main.query_one("#vm-table", DataTable)
        query = main.query_one("#search", Input).value.casefold().strip()
        previous = self.selected_id
        rows = [
            details
            for details in self.inventory
            if query
            in " ".join(
                (
                    str(details.vm.vmid),
                    details.vm.name,
                    details.metadata.get("template", ""),
                    str(details.display_ip or ""),
                )
            ).casefold()
        ]
        table.clear()
        for details in rows:
            vm = details.vm
            table.add_row(
                str(vm.vmid),
                vm.name,
                vm.status,
                details.metadata.get("template", "unknown"),
                str(vm.cpu),
                f"{vm.memory_mib / 1024:g}G",
                str(details.display_ip or "-"),
                key=str(vm.vmid),
            )
        target_row = next(
            (index for index, details in enumerate(rows) if details.vm.vmid == previous), 0
        )
        self.selected_id = rows[target_row].vm.vmid if rows else None
        if rows:
            table.move_cursor(row=target_row)
        self.show_details()
        self.update_actions()

    @on(DataTable.RowHighlighted, "#vm-table")
    def selected(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is not None:
            self.selected_id = int(event.row_key.value)
        self.show_details()
        self.update_actions()

    def current(self) -> VMDetails | None:
        return next(
            (details for details in self.inventory if details.vm.vmid == self.selected_id), None
        )

    def show_details(self) -> None:
        current = self.current()
        self.screen_stack[0].query_one("#details", Static).update(
            details_text(current) if current else "Select a VM to see its details."
        )

    def update_actions(self) -> None:
        if not self.screen_stack or not self.screen_stack[0].is_mounted:
            return
        current = self.current()
        main = self.screen_stack[0]
        main.query_one("#new", Button).disabled = self.busy or self.catalog_data is None
        main.query_one("#refresh", Button).disabled = self.busy
        main.query_one("#details-button", Button).disabled = self.busy or current is None
        for action in ("start", "shutdown", "reboot", "delete"):
            disabled = (
                self.read_only
                or self.busy
                or self.stale
                or current is None
                or bool(current and (current.vm.template or 9000 <= current.vm.vmid <= 9099))
            )
            if current is not None and action == "start":
                disabled = disabled or current.vm.status != "stopped"
            if current is not None and action in {"shutdown", "reboot"}:
                disabled = disabled or current.vm.status != "running"
            main.query_one(f"#{action}-action", Button).disabled = disabled

    @on(DataTable.RowSelected, "#vm-table")
    def open_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is not None:
            self.selected_id = int(event.row_key.value)
            self.open_details()

    @on(VMTable.Clicked)
    def clicked_cell(self, event: VMTable.Clicked) -> None:
        self.selected_id = event.vmid
        self.show_details()
        self.update_actions()
        if event.column == 6:
            try:
                address = IPv4Address(event.value)
            except ValueError:
                return
            self.copy_ip(str(address))
        else:
            self.open_details()

    def copy_ip(self, address: str) -> None:
        """Native clipboard writes are bounded and never block the UI thread."""

        def delivered(native: bool) -> None:
            if native:
                self.notify(f"Copied {address} to clipboard.")
            else:
                self.copy_to_clipboard(address)
                self.notify(
                    "Clipboard request sent; terminal clipboard permission/support is required.",
                    severity="warning",
                    timeout=8,
                )

        def run() -> None:
            native = copy_native(address)
            if self.is_running:
                self.call_from_thread(delivered, native)

        self.run_worker(run, thread=True, exit_on_error=False, name="Copy VM address")

    def open_details(self) -> None:
        current = self.current()
        if current is not None and not self.busy:

            def loaded(details: VMDetails) -> None:
                self.push_screen(VMDetailsScreen(details))

            self.submit(
                "Loading VM details",
                lambda: self.backend.info(str(current.vm.vmid)),
                loaded,
            )

    @on(Button.Pressed)
    async def button(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "new":
                self.action_new()
            case "refresh":
                self.action_refresh()
            case "details-button":
                self.open_details()
            case "start-action" | "shutdown-action" | "reboot-action":
                action = {
                    "start-action": "start",
                    "shutdown-action": "shutdown",
                    "reboot-action": "reboot",
                }[event.button.id]
                from typing import cast

                self.plan_action(cast(LifecycleAction, action))
            case "delete-action":
                self.plan_delete()
            case "quit-button":
                await self.action_quit()

    def action_search(self) -> None:
        self.screen_stack[0].query_one("#search", Input).focus()

    def action_new(self) -> None:
        if self.busy or self.catalog_data is None:
            return
        if not self.catalog_data.templates or not self.catalog_data.presets:
            self.notify("Configure at least one template and preset.", severity="error")
            return
        self.push_screen(CreateScreen(self.catalog_data), self.created)

    def created(self, result: CreateResult | None) -> None:
        if result is not None:
            self.selected_id = result.vmid
            self.action_refresh()
            self.push_screen(TextScreen("Creation result", result_text(result)))

    def plan_action(self, action: LifecycleAction) -> None:
        current = self.current()
        if current is None or self.read_only or self.busy or self.stale:
            return
        self.submit(
            f"Checking {action} target",
            lambda: self.backend.plan_action(str(current.vm.vmid), action),
            self.confirm_action,
        )

    def confirm_action(self, preview: ActionPreview) -> None:
        explanation = (
            "Graceful guest shutdown; no forced stop is used."
            if preview.action == "shutdown"
            else "The guest will be rebooted gracefully."
            if preview.action == "reboot"
            else "Boot the selected VM."
        )

        def confirmed(answer: bool | None) -> None:
            if answer and not self.read_only:
                self.submit(
                    preview.action.capitalize(),
                    lambda: self.backend.action(preview),
                    lambda _: self.action_refresh(),
                )

        self.push_screen(
            ConfirmScreen(
                preview.action.capitalize() + " VM",
                f"VMID: {preview.vm.vmid}\nName: {preview.vm.name}\nStatus: {preview.vm.status}\n\n{explanation}",
            ),
            confirmed,
        )

    def plan_delete(self) -> None:
        current = self.current()
        if current is None or self.read_only or self.busy or self.stale:
            return
        self.submit(
            "Checking deletion target",
            lambda: self.backend.plan_delete(str(current.vm.vmid)),
            self.confirm_delete,
        )

    def confirm_delete(self, preview: DeletePreview) -> None:
        def confirmed(answer: bool | None) -> None:
            if answer and not self.read_only:
                self.submit(
                    "Deleting VM",
                    lambda: self.backend.delete(preview),
                    lambda _: self.action_refresh(),
                )

        self.push_screen(
            ConfirmScreen(
                "Delete VM",
                f"Permanently destroy VM {preview.vm.vmid} ({preview.vm.name}) and release its DHCP reservation?\n\nThe worker verifies this exact target again before deletion.",
                danger=True,
            ),
            confirmed,
        )

    async def action_quit(self) -> None:
        if self.busy:
            self.notify(
                "An operation is running. Wait for its result before closing.", severity="warning"
            )
        else:
            self.exit()
