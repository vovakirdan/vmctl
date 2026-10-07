"""One VM creation form with template defaults and optional advanced settings."""

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from pydantic import SecretStr
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Collapsible, Footer, Input, Label, Select, Static

from vmctl.errors import UncertainOperationError, UnknownOutcomeError, VmctlError
from vmctl.models import CreateRequest, CreateResult, validate_name
from vmctl.operations import Catalog, CreatePreview
from vmctl.tui.dialogs import ConfirmScreen, PasswordScreen, ProgressScreen
from vmctl.tui.features import FeatureChoices
from vmctl.tui.formatting import error_text, preview_text
from vmctl.tui.selection import FeatureSelection

if TYPE_CHECKING:
    from vmctl.tui.app import VmctlApp


class CreateScreen(Screen[CreateResult | None]):
    def __init__(self, catalog: Catalog) -> None:
        super().__init__()
        self.catalog = catalog
        self.template_name = next(iter(catalog.templates))
        self.preset_name = "normal" if "normal" in catalog.presets else next(iter(catalog.presets))
        self.selection = FeatureSelection(catalog, self.template_name)
        self.selection.set_template(self.template_name)
        self.generation = 0
        self.preview: CreatePreview | None = None
        self.preview_request: CreateRequest | None = None

    @property
    def vmctl(self) -> "VmctlApp":
        return cast("VmctlApp", self.app)

    def compose(self) -> ComposeResult:
        yield Label("Create VM", id="create-title", classes="section-title")
        with VerticalScroll(id="create-scroll"):
            yield Label("Basic settings", classes="section-title")
            yield Label("VM name")
            yield Input(id="vm-name", placeholder="workstation", max_length=63)
            yield Label("Template")
            yield Select(
                ((name, name) for name in self.catalog.templates),
                value=self.template_name,
                allow_blank=False,
                id="template",
            )
            yield Static("", id="template-note", markup=False)
            yield Label("Resource preset")
            yield Select(
                ((name, name) for name in self.catalog.presets),
                value=self.preset_name,
                allow_blank=False,
                id="preset",
            )
            yield Label("Resources", classes="section-title")
            yield Static(
                "Preset values are editable defaults. Disks are only enlarged; an existing larger template disk is retained."
            )
            with Horizontal(id="resources"):
                with Vertical():
                    yield Label("CPU (vCPU)")
                    yield Input(id="cpu", type="integer")
                with Vertical():
                    yield Label("Memory (e.g. 8G)")
                    yield Input(id="memory")
                with Vertical():
                    yield Label("Disk (e.g. 40G)")
                    yield Input(id="disk")
            yield FeatureChoices(self.selection)
            with Collapsible(title="Advanced settings", id="advanced"):
                yield Label(
                    f"IPv4 address (auto or {self.catalog.pool_start} - {self.catalog.pool_end})"
                )
                yield Input("auto", id="ip")
                yield Label("SSH public key path on this computer (blank uses client default)")
                yield Input(id="ssh-key", placeholder="~/.ssh/id_ed25519.pub")
                yield Label("Description")
                yield Input(id="description")
                yield Checkbox("Start after creation", value=True, id="start")
                yield Static(
                    "Boot the cloned VM after configuration. Uncheck to leave it stopped.",
                    classes="choice-description",
                )
                yield Label("Wait for SSH (seconds, 0 skips waiting)")
                yield Input("0", type="integer", id="wait")
                yield Checkbox("Skip desktop password", id="no-password")
                yield Static(
                    "GUI/RDP login may be unavailable without another authentication setup. Server templates never prompt for a desktop password.",
                    classes="choice-description",
                )
            yield Label("Server preview", classes="section-title")
            yield Static(
                "Preview validates the host configuration without creating a VM.",
                id="create-preview",
                markup=False,
            )
            yield Static("", id="create-error", markup=False)
        with Horizontal(classes="buttons", id="create-buttons"):
            yield Button("Back", id="back")
            yield Button("Preview", id="preview", variant="primary")
            yield Button("Create", id="create", variant="success", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self.apply_preset()
        self.template_note()
        self.query_one("#vm-name", Input).focus()

    def template_note(self) -> None:
        template = self.catalog.templates[self.template_name]
        self.query_one("#template-note", Static).update(
            f"{template.distro} {template.release} | {'Desktop' if template.desktop else 'Server'} | Template VMID {template.vmid}"
        )

    def apply_preset(self) -> None:
        preset = self.catalog.presets[self.preset_name]
        self.query_one("#cpu", Input).value = str(preset.cpu)
        self.query_one("#memory", Input).value = f"{preset.memory_mib}M"
        self.query_one("#disk", Input).value = f"{preset.disk_gib}G"
        self.invalidate()

    def invalidate(self) -> None:
        self.generation += 1
        self.preview = None
        self.preview_request = None
        if self.is_mounted:
            self.query_one("#create", Button).disabled = True
            self.query_one("#create-preview", Static).update(
                "Settings changed. Preview to validate the server plan."
            )

    @on(Select.Changed, "#template")
    def change_template(self, event: Select.Changed) -> None:
        if not isinstance(event.value, str):
            return
        self.template_name = event.value
        self.selection.set_template(event.value)
        self.query_one(FeatureChoices).update_choices()
        self.template_note()
        self.invalidate()

    @on(Select.Changed, "#preset")
    def change_preset(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.preset_name = event.value
            self.apply_preset()

    @on(Input.Changed)
    @on(FeatureChoices.Changed)
    @on(Checkbox.Changed)
    def edited(self) -> None:
        self.invalidate()

    def request(self) -> CreateRequest:
        def value(identifier: str) -> str:
            return self.query_one(f"#{identifier}", Input).value.strip()

        try:
            cpu, wait = int(value("cpu")), int(value("wait"))
        except ValueError as error:
            raise VmctlError("CPU and wait timeout must be integers.") from error
        if cpu <= 0 or wait < 0:
            raise VmctlError("CPU must be positive and wait timeout cannot be negative.")
        key_path = Path(value("ssh-key")) if value("ssh-key") else None
        return CreateRequest(
            name=validate_name(value("vm-name")),
            template=self.template_name,
            preset=self.preset_name,
            cpu=cpu,
            memory=value("memory"),
            disk=value("disk"),
            ip=value("ip"),
            modules=tuple(sorted(self.selection.development)),
            system_features=self.selection.selected_names(system=True),
            without_system=self.selection.without_system(),
            start=self.query_one("#start", Checkbox).value,
            description=value("description"),
            wait_seconds=wait,
            no_desktop_password=self.query_one("#no-password", Checkbox).value,
            public_key=self.vmctl.public_key(key_path),
        )

    def show_error(self, error: Exception) -> None:
        self.query_one("#create-error", Static).update(error_text(error))

    @on(Button.Pressed)
    def button(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "back":
                if not self.vmctl.busy:
                    self.dismiss(None)
            case "preview":
                self.plan()
            case "create":
                self.confirm_create()

    def plan(self) -> None:
        if self.vmctl.busy:
            return
        try:
            request = self.request()
        except VmctlError as error:
            self.show_error(error)
            return
        self.query_one("#create-error", Static).update("")
        generation = self.generation
        self.vmctl.submit(
            "Checking creation plan",
            lambda: self.vmctl.backend.plan_create(request),
            lambda preview: self.planned(request, preview, generation),
            self.show_error,
        )

    def planned(self, request: CreateRequest, preview: CreatePreview, generation: int) -> None:
        if generation != self.generation:
            return
        self.preview_request, self.preview = request, preview
        self.query_one("#create-preview", Static).update(preview_text(preview))
        self.query_one("#create", Button).disabled = self.vmctl.read_only
        if self.vmctl.read_only:
            self.query_one("#create-error", Static).update(
                "Read-only mode: this plan can be inspected, but a VM cannot be created."
            )

    def confirm_create(self) -> None:
        if (
            self.vmctl.busy
            or self.vmctl.read_only
            or self.preview is None
            or self.preview_request is None
        ):
            return
        request, preview = self.preview_request, self.preview

        def confirmed(answer: bool | None) -> None:
            if not answer:
                return
            if preview.requires_desktop_password:
                self.app.push_screen(
                    PasswordScreen(preview.username),
                    lambda hashed: self.password_ready(request, hashed),
                )
            else:
                self.execute(request)

        self.app.push_screen(
            ConfirmScreen(
                "Create VM",
                f"Create {request.name} from {request.template}?\n\n{preview_text(preview)}",
            ),
            confirmed,
        )

    def password_ready(self, request: CreateRequest, hashed: SecretStr | None) -> None:
        if hashed is not None:
            self.execute(replace(request, desktop_password_hash=hashed))

    def execute(self, request: CreateRequest) -> None:
        if self.vmctl.busy or self.vmctl.read_only:
            return
        request_id = uuid4().hex
        progress_screen = ProgressScreen()
        self.app.push_screen(progress_screen)

        def progress(message: str) -> None:
            self.app.call_from_thread(progress_screen.append, message)

        def success(result: CreateResult) -> None:
            progress_screen.dismiss(None)
            self.dismiss(result)

        def failed(error: Exception) -> None:
            progress_screen.dismiss(None)
            self.show_error(error)
            if isinstance(error, (UnknownOutcomeError, UncertainOperationError)):
                self.invalidate()
            self.vmctl.notify(f"Creation request: {request_id}", severity="warning")

        self.vmctl.submit(
            "Creating VM",
            lambda: self.vmctl.backend.create(request, request_id=request_id, progress=progress),
            success,
            failed,
        )

    def key_escape(self) -> None:
        if not self.vmctl.busy:
            self.dismiss(None)
