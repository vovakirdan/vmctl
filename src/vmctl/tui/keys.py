"""Public-key selection and confirmed guest export, independent of SSH credentials."""

from typing import TYPE_CHECKING, cast

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from vmctl.errors import VmctlError
from vmctl.keys import PublicKeyRegistryStore, key_fingerprint, merge_public_keys
from vmctl.models import KeyExportRequest, VMDetails
from vmctl.operations import KeyExportPreview, KeyExportResult
from vmctl.tui.formatting import error_text
from vmctl.tui.manage import ManageConfirmScreen, ManageProgressScreen

if TYPE_CHECKING:
    from vmctl.tui.app import VmctlApp


def export_preview_text(preview: KeyExportPreview) -> str:
    fingerprints = _fingerprints(preview.request.public_key)
    content = (
        f"VM: {preview.vm.vmid} ({preview.vm.name})\n"
        f"Guest user: {preview.request.username or 'configured default'}\n"
        "Add these public keys to authorized_keys:\n"
        + "\n".join(fingerprints)
        + "\n\nExisting keys and access are preserved. No private keys are transferred."
    )
    return content + ("\n\n" + "\n".join(preview.notes) if preview.notes else "")


def _fingerprints(public_keys: str) -> tuple[str, ...]:
    return tuple(key_fingerprint(line) for line in public_keys.splitlines() if line.strip())


def export_result_text(result: KeyExportResult) -> str:
    return (
        f"VM: {result.vm.vmid} ({result.vm.name})\nUser: {result.username}\n"
        f"Added: {result.added}\nAlready present: {result.unchanged}\n"
        + "\n".join(result.fingerprints)
    )


class KeyExportScreen(ModalScreen[KeyExportResult | None]):
    def __init__(self, target: VMDetails, store: PublicKeyRegistryStore) -> None:
        super().__init__()
        self.target, self.store = target, store
        self.registry = store.load()
        self.names = tuple(sorted(self.registry.keys))
        self.preview: KeyExportPreview | None = None
        self.generation = 0

    @property
    def vmctl(self) -> "VmctlApp":
        return cast("VmctlApp", self.app)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog large-dialog keys-dialog"):
            yield Label("Export SSH public keys", classes="dialog-title")
            with VerticalScroll(id="keys-scroll"):
                yield Static(f"VM: {self.target.vm.vmid} ({self.target.vm.name})", markup=False)
                yield Static(
                    "Add public keys while preserving existing access. Requires a running "
                    "Linux guest with a working QEMU guest agent. No existing SSH login is needed."
                )
                if not self.names:
                    yield Static(
                        "No saved public keys. Add one on the workstation with: "
                        "vmctl keys add NAME PATH --default",
                        id="keys-empty",
                    )
                for index, name in enumerate(self.names):
                    key = self.registry.keys[name]
                    yield Checkbox(
                        name, value=name in self.registry.default_keys, id=f"key-choice-{index}"
                    )
                    yield Static(
                        f"{key.description}\n{key_fingerprint(key.public_key)}",
                        classes="choice-description",
                        markup=False,
                    )
                yield Label("Guest user (blank uses configured default)")
                yield Input(
                    self.target.metadata.get("username") or self.target.config.get("ciuser", ""),
                    id="keys-user",
                    placeholder="Configured cloud-init user",
                )
                yield Static(
                    "Preview to check the exact target and selected keys.",
                    id="keys-preview",
                    markup=False,
                )
                yield Static("", id="keys-error", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Back", id="keys-back")
                yield Button("Preview", id="keys-plan", variant="primary", disabled=not self.names)
                yield Button("Export", id="keys-apply", variant="success", disabled=True)

    def invalidate(self) -> None:
        self.generation += 1
        self.preview = None
        if self.is_mounted:
            self.query_one("#keys-apply", Button).disabled = True
            self.query_one("#keys-preview", Static).update("Selection changed. Preview again.")

    @on(Checkbox.Changed)
    @on(Input.Changed)
    def edited(self) -> None:
        self.invalidate()

    def request(self) -> KeyExportRequest:
        names = [
            name
            for index, name in enumerate(self.names)
            if self.query_one(f"#key-choice-{index}", Checkbox).value
        ]
        if not names:
            raise VmctlError("Select at least one public key.")
        return KeyExportRequest(
            str(self.target.vm.vmid),
            merge_public_keys(*(self.registry.keys[name].public_key for name in names)),
            self.query_one("#keys-user", Input).value.strip() or None,
        )

    def show_error(self, error: Exception) -> None:
        self.query_one("#keys-error", Static).update(error_text(error))

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
        self.query_one("#keys-error", Static).update("")

        def operation() -> KeyExportPreview:
            preview = self.vmctl.backend.plan_key_export(request)
            if (
                preview.vm.vmid != self.target.vm.vmid
                or preview.request.reference != request.reference
                or _fingerprints(preview.request.public_key) != _fingerprints(request.public_key)
                or (request.username is not None and preview.request.username != request.username)
            ):
                raise VmctlError(
                    "Worker preview changed the selected VM, user or public keys. No keys were exported."
                )
            return preview

        def planned(preview: KeyExportPreview) -> None:
            if not self.is_mounted or generation != self.generation:
                return
            self.preview = preview
            self.query_one("#keys-preview", Static).update(export_preview_text(preview))
            self.query_one("#keys-apply", Button).disabled = self.vmctl.read_only
            if self.vmctl.read_only:
                self.query_one("#keys-error", Static).update(
                    "Read-only mode: preview only; keys cannot be exported."
                )

        self.vmctl.submit("Checking SSH key export", operation, planned, self.show_error)

    def confirm(self) -> None:
        if self.vmctl.busy or self.vmctl.stale or self.vmctl.read_only or self.preview is None:
            return
        preview, generation = self.preview, self.generation

        def confirmed(answer: bool | None) -> None:
            if answer and self.preview is preview and self.generation == generation:
                self.execute(preview)

        self.app.push_screen(
            ManageConfirmScreen("Export SSH public keys", export_preview_text(preview)), confirmed
        )

    def execute(self, preview: KeyExportPreview) -> None:
        if (
            self.vmctl.busy
            or self.vmctl.stale
            or self.vmctl.read_only
            or self.preview is not preview
        ):
            return
        progress_screen = ManageProgressScreen("Exporting SSH public keys")
        self.app.push_screen(progress_screen)

        def progress(message: str) -> None:
            self.app.call_from_thread(progress_screen.append, message)

        def operation() -> KeyExportResult:
            return self.vmctl.backend.export_keys(preview, progress=progress)

        def success(result: KeyExportResult) -> None:
            progress_screen.dismiss(None)
            self.dismiss(result)

        def failed(error: Exception) -> None:
            progress_screen.dismiss(None)
            self.invalidate()
            self.show_error(error)

        self.vmctl.submit("Exporting SSH public keys", operation, success, failed)

    @on(Button.Pressed)
    def button(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "keys-back":
                self.key_escape()
            case "keys-plan":
                self.plan()
            case "keys-apply":
                self.confirm()

    def key_escape(self) -> None:
        if not self.vmctl.busy:
            self.dismiss(None)
