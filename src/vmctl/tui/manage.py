"""Small management forms sharing guarded worker previews and confirmations."""

from ipaddress import IPv4Address
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Input, Label, Select, Static

from vmctl.errors import VmctlError
from vmctl.models import CloneRequest, HardwareRequest, SpiceRequest, VMDetails, validate_name
from vmctl.operations import (
    ClonePreview,
    CloneResult,
    HardwarePreview,
    HardwareResult,
    SpicePreview,
    SpiceResult,
)
from vmctl.tui.dialogs import ConfirmScreen, ProgressScreen
from vmctl.tui.formatting import error_text
from vmctl.utils.sizes import parse_size

if TYPE_CHECKING:
    from vmctl.tui.app import VmctlApp

type ManageAction = Literal["clone", "edit", "spice"]
type ManageRequest = CloneRequest | HardwareRequest | SpiceRequest
type ManagePreview = ClonePreview | HardwarePreview | SpicePreview
type ManageResult = CloneResult | HardwareResult | SpiceResult


def preview_text(preview: ManagePreview) -> str:
    if isinstance(preview, ClonePreview):
        content = (
            f"Source: {preview.source.vmid} ({preview.source.name})\n"
            f"New name: {preview.request.name}\nIP: {preview.request.ip}\n"
            f"Start clone: {'yes' if preview.request.start else 'no'}\n"
            f"Temporary source shutdown: {'yes' if preview.request.shutdown_source else 'no'}\n"
            f"Cloud-init: {'network/hostname refresh only' if preview.cloud_init else 'not present'}\n"
            "Full copy: installed software, data, accounts and credentials are copied. "
            "A new MAC/IP reservation cannot replace a static IP configured inside the guest."
        )
    elif isinstance(preview, HardwarePreview):
        content = (
            f"VM: {preview.vm.vmid} ({preview.vm.name})\n"
            f"CPU: {preview.cpu} -> {preview.new_cpu} vCPU\n"
            f"Memory: {preview.memory_mib} -> {preview.new_memory_mib} MiB\n"
            f"Allow graceful shutdown/restart: {'yes' if preview.request.restart else 'no'}"
        )
    else:
        content = (
            f"VM: {preview.vm.vmid} ({preview.vm.name})\n"
            f"Display: {preview.current_vga} -> {preview.target_vga}\n"
            f"Shutdown needed: {'yes' if preview.needs_shutdown else 'no'}\n"
            f"Guest tools: {preview.request.guest_tools}\n"
            f"Detected guest family: {preview.guest_family or 'unknown'}\n"
            f"Guest installation available: {'yes' if preview.guest_tools_possible else 'no'}\n"
            "Display configuration does not prove guest integration or a working SPICE connection."
        )
    return content + ("\n\n" + "\n".join(preview.notes) if preview.notes else "")


def result_text(result: ManageResult) -> str:
    content = f"VMID: {result.vm.vmid}\nName: {result.vm.name}\nStatus: {result.vm.status}\n"
    if isinstance(result, CloneResult):
        content += (
            f"Cloned from VMID: {result.source_vmid}\nMAC: {result.mac}\nIP: {result.ip}\n"
            f"Source restored: {'yes' if result.source_restored else 'no'}\n"
            "Guest data, accounts and credentials were copied. Review guest identity before use."
        )
    elif isinstance(result, HardwareResult):
        content += (
            f"CPU: {result.cpu} vCPU\nMemory: {result.memory_mib} MiB\n"
            f"Restarted: {'yes' if result.restarted else 'no'}"
        )
    else:
        content += (
            f"Display: {result.vga}\nSPICE display configured: {'yes' if result.host_enabled else 'no'}\n"
            f"Guest tools: {result.guest_tools_status}"
        )
        if result.guest_pid is not None:
            content += f"\nGuest process ID: {result.guest_pid}"
        if result.guest_tools_status == "uncertain":
            content += "\nThe guest package job may still be running. Inspect it before retrying."
        elif result.guest_tools_status in {"unavailable", "failed"}:
            content += (
                "\nGuest integration needs manual attention; SPICE readiness was not verified."
            )
    return content + ("\n\n" + "\n".join(result.notes) if result.notes else "")


class ManageProgressScreen(ProgressScreen):
    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label

    def on_mount(self) -> None:
        self.query_one(Label).update(self.label)


class ManageConfirmScreen(ConfirmScreen):
    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog large-dialog"):
            yield Label(self.title_text, classes="dialog-title")
            with VerticalScroll():
                yield Static(self.message_text, markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Confirm", id="confirm", variant="primary")


class ManageScreen(Screen[ManageResult | None]):
    def __init__(self, action: ManageAction, target: VMDetails) -> None:
        super().__init__()
        self.action, self.target = action, target
        self.reference = str(target.vm.vmid)
        self.generation = 0
        self.preview: ManagePreview | None = None

    @property
    def vmctl(self) -> "VmctlApp":
        return cast("VmctlApp", self.app)

    @property
    def label(self) -> str:
        return {"clone": "Clone VM", "edit": "Edit resources", "spice": "Configure SPICE"}[
            self.action
        ]

    def compose(self) -> ComposeResult:
        yield Label(self.label, id="manage-title", classes="section-title")
        with VerticalScroll(id="manage-scroll"):
            yield Static(
                f"Target: {self.target.vm.vmid} ({self.target.vm.name}) | {self.target.vm.status}",
                id="manage-target",
                markup=False,
            )
            if self.action == "clone":
                yield Static(
                    "Full clone copies guest data, accounts and existing credentials. "
                    "The duplicate receives a fresh VMID/MAC and private DHCP reservation; "
                    "static guest networking and other guest identities may need manual changes."
                )
                yield Label("New VM name")
                yield Input(id="clone-name", placeholder="android-copy", max_length=63)
                yield Label(
                    "Private IPv4 reservation (auto or an address from the configured pool)"
                )
                yield Input("auto", id="clone-ip")
                yield Checkbox("Start the duplicate", id="clone-start")
                yield Static("Leave unchecked to review the copy before booting it.")
                yield Checkbox("Allow temporary source shutdown", id="clone-shutdown")
                yield Static(
                    "A running source is shut down gracefully, copied and started again. "
                    "Its services are unavailable during copying. No forced stop is used."
                )
            elif self.action == "edit":
                yield Static(
                    "Edit CPU and RAM only. A running VM requires explicit permission for "
                    "graceful shutdown and restart; there is no forced-stop fallback."
                )
                yield Label("CPU (total vCPU)")
                yield Input(str(self.target.vm.cpu), type="integer", id="edit-cpu")
                yield Label("Memory (e.g. 8G or 8192M)")
                yield Input(f"{self.target.vm.memory_mib}M", id="edit-memory")
                yield Checkbox("Allow graceful shutdown and restart", id="edit-restart")
                yield Static(
                    "A previously stopped VM stays stopped. Running services face downtime."
                )
            else:
                yield Static(
                    "Configure a SPICE-capable virtual display. Existing supported displays "
                    "are preserved. Changing the display of a running VM requires downtime."
                )
                yield Label("Guest integration tools")
                yield Select(
                    (
                        ("Auto: install when supported", "auto"),
                        ("Skip: display configuration only", "skip"),
                        ("Install: require guest support", "install"),
                    ),
                    value="auto",
                    allow_blank=False,
                    id="spice-tools",
                )
                yield Static(
                    "Guest tools are installed through a running QEMU guest agent on supported "
                    "Linux guests. Android or a missing agent requires manual guest integration. "
                    "No public ports or firewall rules are changed."
                )
                yield Checkbox("Allow graceful shutdown and restart", id="spice-restart")
                yield Static("A stopped VM is not automatically booted to install guest packages.")
            yield Label("Server preview", classes="section-title")
            yield Static(
                "Preview validates this exact VM without changing it.",
                id="manage-preview",
                markup=False,
            )
            yield Static("", id="manage-error", markup=False)
        with Horizontal(classes="buttons", id="manage-buttons"):
            yield Button("Back", id="manage-back")
            yield Button("Preview", id="manage-plan", variant="primary")
            yield Button("Apply", id="manage-apply", variant="success", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        identifier = {"clone": "clone-name", "edit": "edit-cpu", "spice": "spice-tools"}[
            self.action
        ]
        self.query_one(f"#{identifier}").focus()

    def invalidate(self) -> None:
        self.generation += 1
        self.preview = None
        if self.is_mounted:
            self.query_one("#manage-apply", Button).disabled = True
            self.query_one("#manage-preview", Static).update(
                "Settings changed. Preview to validate the server plan."
            )

    @on(Input.Changed)
    @on(Select.Changed)
    @on(Checkbox.Changed)
    def edited(self) -> None:
        self.invalidate()

    def request(self) -> ManageRequest:
        def value(identifier: str) -> str:
            return self.query_one(f"#{identifier}", Input).value.strip()

        if self.action == "clone":
            address = value("clone-ip")
            if address != "auto":
                try:
                    IPv4Address(address)
                except ValueError as error:
                    raise VmctlError("IP must be auto or a valid IPv4 address.") from error
            return CloneRequest(
                self.reference,
                validate_name(value("clone-name")),
                ip=address,
                start=self.query_one("#clone-start", Checkbox).value,
                shutdown_source=self.query_one("#clone-shutdown", Checkbox).value,
            )
        if self.action == "edit":
            try:
                cpu = int(value("edit-cpu"))
            except ValueError as error:
                raise VmctlError("CPU must be a positive integer.") from error
            if cpu <= 0:
                raise VmctlError("CPU must be a positive integer.")
            memory = value("edit-memory")
            parse_size(memory, unit="MiB")
            return HardwareRequest(
                self.reference,
                cpu=cpu,
                memory=memory,
                restart=self.query_one("#edit-restart", Checkbox).value,
            )
        tools = self.query_one("#spice-tools", Select).value
        if tools not in {"auto", "skip", "install"}:
            raise VmctlError("Select auto, skip or install for guest integration tools.")
        return SpiceRequest(
            self.reference,
            restart=self.query_one("#spice-restart", Checkbox).value,
            guest_tools=cast(Literal["auto", "skip", "install"], tools),
        )

    def show_error(self, error: Exception) -> None:
        self.query_one("#manage-error", Static).update(error_text(error))

    def plan(self) -> None:
        if self.vmctl.busy or self.vmctl.stale:
            return
        self.invalidate()
        try:
            request = self.request()
        except VmctlError as error:
            self.show_error(error)
            return
        generation = self.generation
        self.query_one("#manage-error", Static).update("")

        def operation() -> ManagePreview:
            preview: ManagePreview
            if isinstance(request, CloneRequest):
                preview = self.vmctl.backend.plan_clone(request)
                vmid = preview.source.vmid
            elif isinstance(request, HardwareRequest):
                preview = self.vmctl.backend.plan_update(request)
                vmid = preview.vm.vmid
            else:
                preview = self.vmctl.backend.plan_spice(request)
                vmid = preview.vm.vmid
            if str(vmid) != self.reference:
                raise VmctlError(
                    "The worker preview returned a different VM. No changes were made."
                )
            return preview

        def planned(preview: ManagePreview) -> None:
            if not self.is_mounted or generation != self.generation:
                return
            self.preview = preview
            self.query_one("#manage-preview", Static).update(preview_text(preview))
            self.query_one("#manage-apply", Button).disabled = self.vmctl.read_only
            if self.vmctl.read_only:
                self.query_one("#manage-error", Static).update(
                    "Read-only mode: this plan can be inspected, but changes cannot be applied."
                )

        self.vmctl.submit("Checking " + self.label.lower(), operation, planned, self.show_error)

    def confirm(self) -> None:
        if self.vmctl.busy or self.vmctl.read_only or self.vmctl.stale or self.preview is None:
            return
        preview, generation = self.preview, self.generation

        def confirmed(answer: bool | None) -> None:
            if answer and self.preview is preview and self.generation == generation:
                self.execute(preview)

        self.app.push_screen(ManageConfirmScreen(self.label, preview_text(preview)), confirmed)

    def execute(self, preview: ManagePreview) -> None:
        if (
            self.vmctl.busy
            or self.vmctl.read_only
            or self.vmctl.stale
            or self.preview is not preview
        ):
            return
        progress_screen = ManageProgressScreen(self.label)
        self.app.push_screen(progress_screen)
        request_id = uuid4().hex

        def progress(message: str) -> None:
            self.app.call_from_thread(progress_screen.append, message)

        def operation() -> ManageResult:
            if isinstance(preview, ClonePreview):
                return self.vmctl.backend.clone(preview, request_id=request_id, progress=progress)
            if isinstance(preview, HardwarePreview):
                return self.vmctl.backend.update(preview, progress=progress)
            return self.vmctl.backend.spice(preview, progress=progress)

        def success(result: ManageResult) -> None:
            progress_screen.dismiss(None)
            self.dismiss(result)

        def failed(error: Exception) -> None:
            progress_screen.dismiss(None)
            self.invalidate()
            self.show_error(error)
            if isinstance(preview, ClonePreview):
                self.vmctl.notify(f"Clone request: {request_id}", severity="warning")

        self.vmctl.submit(self.label, operation, success, failed)

    @on(Button.Pressed)
    def button(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "manage-back":
                self.key_escape()
            case "manage-plan":
                self.plan()
            case "manage-apply":
                self.confirm()

    def key_escape(self) -> None:
        if not self.vmctl.busy:
            self.dismiss(None)
