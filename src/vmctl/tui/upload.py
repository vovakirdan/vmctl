"""Workstation upload selection and progress; transfer policy stays in the service."""

from pathlib import Path
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING, cast

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DirectoryTree,
    Footer,
    Input,
    Label,
    ProgressBar,
    Sparkline,
    Static,
)

from vmctl.errors import VmctlError
from vmctl.models import VMDetails
from vmctl.transfer.models import (
    UploadCancelledError,
    UploadPreview,
    UploadProgress,
    UploadRequest,
    UploadResult,
    UploadUncertainError,
)
from vmctl.tui.formatting import error_text
from vmctl.tui.manage import ManageConfirmScreen

if TYPE_CHECKING:
    from vmctl.tui.app import VmctlApp


def byte_size(value: int | float) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{amount:g} B"
        amount /= 1024
    raise AssertionError("Unreachable size unit")


def duration(seconds: float) -> str:
    minutes, remaining = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{remaining:02d}" if hours else f"{minutes:d}:{remaining:02d}"


def upload_preview_text(preview: UploadPreview) -> str:
    content = (
        f"VM: {preview.vm.vmid} ({preview.vm.name})\n"
        f"Address: {preview.ip}\nUser: {preview.username}\n"
        f"Local source: {preview.source}\nGuest target: {preview.destination}\n"
        f"Regular files: {preview.file_count}\nTotal data: {byte_size(preview.total_bytes)}\n"
        f"Replace existing regular file: {'yes' if preview.request.overwrite else 'no'}\n\n"
        "Data is staged privately in the destination directory and published after transfer. "
        "Existing directories are never merged or replaced."
    )
    return content + ("\n\n" + "\n".join(preview.notes) if preview.notes else "")


def upload_result_text(result: UploadResult) -> str:
    content = (
        f"VMID: {result.vmid}\nGuest target: {result.destination}\n"
        f"Regular files: {result.file_count}\nTransferred: {byte_size(result.bytes_sent)}"
    )
    return content + ("\n\n" + "\n".join(result.notes) if result.notes else "")


class LocalSourceScreen(ModalScreen[Path | None]):
    """Choose a workstation file or directory, with a manual path for every platform."""

    def __init__(self, source: Path | None = None) -> None:
        super().__init__()
        self.source = source or Path.home()

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog large-dialog source-dialog"):
            yield Label("Choose a local file or folder", classes="dialog-title")
            yield Static("This browser reads the workstation filesystem, not the Proxmox host.")
            yield Input(str(self.source), id="source-path", placeholder="Local path")
            yield DirectoryTree(Path.home(), id="source-tree")
            with Horizontal(classes="buttons"):
                yield Button("Open folder", id="source-open")
                yield Button("Cancel", id="source-cancel")
                yield Button("Use path", id="source-use", variant="primary")

    @on(DirectoryTree.FileSelected)
    @on(DirectoryTree.DirectorySelected)
    def selected(self, event: DirectoryTree.FileSelected | DirectoryTree.DirectorySelected) -> None:
        self.query_one("#source-path", Input).value = str(event.path)

    @on(Button.Pressed)
    def button(self, event: Button.Pressed) -> None:
        if event.button.id == "source-cancel":
            self.dismiss(None)
            return
        value = self.query_one("#source-path", Input).value.strip()
        if not value:
            self.notify("Enter a local file or folder path.", severity="warning")
            return
        try:
            path = Path(value).expanduser()
        except RuntimeError:
            self.notify("Cannot resolve the home directory in this local path.", severity="error")
            return
        match event.button.id:
            case "source-open":
                self.query_one("#source-tree", DirectoryTree).path = path
            case "source-use":
                self.dismiss(path)

    def key_escape(self) -> None:
        self.dismiss(None)


class UploadProgressScreen(ModalScreen[None]):
    """Display actual transferred bytes and cooperative cancellation without fake progress."""

    def __init__(self, preview: UploadPreview, cancel: Event) -> None:
        super().__init__()
        self.preview, self.cancel = preview, cancel
        self.started = monotonic()
        self.latest: UploadProgress | None = None
        self.samples: list[float] = []
        self.last_sample_time = self.started
        self.last_sample_bytes = 0

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog large-dialog upload-progress-dialog"):
            yield Label(
                f"Upload to {self.preview.vm.vmid} ({self.preview.vm.name})",
                classes="dialog-title",
            )
            with VerticalScroll(id="upload-progress-content"):
                yield Static(
                    str(self.preview.destination), markup=False, id="upload-progress-target"
                )
                yield Static("Connecting and rechecking the confirmed target...", id="upload-stage")
                yield Static("", id="upload-current", markup=False)
                yield ProgressBar(
                    total=None,
                    show_eta=False,
                    show_percentage=True,
                    id="upload-progress-bar",
                )
                yield Static(
                    f"0 / {byte_size(self.preview.total_bytes)} | "
                    f"0 / {self.preview.file_count} files",
                    id="upload-counts",
                    markup=False,
                )
                yield Static(
                    "Throughput: waiting | ETA: waiting | Elapsed: 0:00", id="upload-speed"
                )
                yield Label("Throughput history")
                yield Sparkline([], id="upload-speed-graph")
                yield Static(
                    "Cancel requests cleanup of staged data before publication. "
                    "Wait for the final result; a lost publication reply requires inspection "
                    "before retrying.",
                    id="upload-cancel-note",
                )
            with Horizontal(classes="buttons"):
                yield Button("Cancel transfer", id="upload-cancel", variant="warning")

    def on_mount(self) -> None:
        self.set_interval(0.5, self.refresh_speed)

    def append(self, progress: UploadProgress) -> None:
        self.latest = progress
        current_time = monotonic()
        interval = current_time - self.last_sample_time
        if interval >= 0.1:
            self.samples.append(max(0, progress.sent_bytes - self.last_sample_bytes) / interval)
            self.samples = self.samples[-60:]
            self.last_sample_time, self.last_sample_bytes = current_time, progress.sent_bytes
            self.query_one("#upload-speed-graph", Sparkline).data = self.samples
        self.query_one("#upload-current", Static).update(progress.current_path)
        complete = (
            progress.sent_bytes == progress.total_bytes
            and progress.completed_files == progress.total_files
        )
        self.query_one("#upload-stage", Static).update(
            "Transfer complete; verifying and publishing staged data..."
            if complete
            else "Sending staged data..."
        )
        bar = self.query_one("#upload-progress-bar", ProgressBar)
        if complete or progress.total_bytes == 0:
            # Publication has no byte-based progress and may still fail.
            bar.update(total=None)
        else:
            bar.update(total=progress.total_bytes, progress=progress.sent_bytes)
        self.query_one("#upload-counts", Static).update(
            f"{byte_size(progress.sent_bytes)} / {byte_size(progress.total_bytes)} | "
            f"{progress.completed_files} / {progress.total_files} files"
        )
        self.refresh_speed()

    def refresh_speed(self) -> None:
        elapsed = max(0, monotonic() - self.started)
        progress = self.latest
        rate = progress.sent_bytes / elapsed if progress is not None and elapsed > 0 else 0
        eta = (
            duration((progress.total_bytes - progress.sent_bytes) / rate)
            if progress is not None and rate > 0 and progress.sent_bytes < progress.total_bytes
            else "waiting"
            if progress is None or rate == 0
            else "verifying"
        )
        speed = f"{byte_size(rate)}/s" if rate > 0 else "waiting"
        self.query_one("#upload-speed", Static).update(
            f"Throughput: {speed} | ETA: {eta} | Elapsed: {duration(elapsed)}"
        )

    @on(Button.Pressed, "#upload-cancel")
    def request_cancel(self) -> None:
        self.cancel.set()
        self.query_one("#upload-cancel", Button).disabled = True
        self.query_one("#upload-cancel-note", Static).update(
            "Cancel requested. Waiting for transfer cleanup and the final result..."
        )

    def key_escape(self) -> None:
        self.request_cancel()


class UploadScreen(Screen[UploadResult | None]):
    def __init__(self, target: VMDetails) -> None:
        super().__init__()
        self.target = target
        self.reference = str(target.vm.vmid)
        self.preview: UploadPreview | None = None
        self.generation = 0

    @property
    def vmctl(self) -> "VmctlApp":
        return cast("VmctlApp", self.app)

    def compose(self) -> ComposeResult:
        yield Label("Upload files to VM", id="upload-title", classes="section-title")
        with VerticalScroll(id="upload-scroll"):
            yield Static(
                f"Target: {self.target.vm.vmid} ({self.target.vm.name}) | "
                f"{self.target.vm.status} | {self.target.display_ip}",
                markup=False,
                id="upload-target",
            )
            yield Static(
                "Send one local file or a folder directly over SSH/SFTP to a running VM. "
                "The folder is copied as a new directory; no rsync or guest agent is required."
            )
            yield Label("Workstation source file or folder")
            with Horizontal(id="upload-source-row"):
                yield Input(id="upload-source", placeholder="/path/to/file-or-folder")
                yield Button("Browse", id="upload-browse")
            yield Label("Guest destination directory (must already exist)")
            yield Input("~", id="upload-destination", placeholder="~ or /home/vmadmin")
            yield Static(
                "The selected file/folder name is appended to this directory. "
                "~ resolves to the SSH user's home during preview."
            )
            yield Label("Guest SSH user (blank uses configured default)")
            yield Input(
                self.target.metadata.get("username") or self.target.config.get("ciuser", ""),
                id="upload-user",
                placeholder="Configured guest user",
            )
            yield Static(
                "Uses the workstation's SSH keys/agent and known_hosts. "
                "VM passwords and private keys are never requested or printed."
            )
            yield Checkbox("Replace an existing regular file", id="upload-overwrite")
            yield Static(
                "Unchecked: fail if the target exists. Checked: allow replacement of a "
                "regular file only. Existing directories are never merged or overwritten."
            )
            yield Label("Transfer preview", classes="section-title")
            yield Static(
                "Preview reads source metadata and checks the guest target without writing data.",
                id="upload-preview",
                markup=False,
            )
            yield Static("", id="upload-error", markup=False)
        with Horizontal(classes="buttons", id="upload-buttons"):
            yield Button("Back", id="upload-back")
            yield Button("Preview", id="upload-plan", variant="primary")
            yield Button("Send", id="upload-send", variant="success", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#upload-source", Input).focus()

    def invalidate(self) -> None:
        self.generation += 1
        self.preview = None
        if self.is_mounted:
            self.query_one("#upload-send", Button).disabled = True
            self.query_one("#upload-preview", Static).update(
                "Settings changed. Preview to check the exact transfer target."
            )

    @on(Input.Changed)
    @on(Checkbox.Changed)
    def edited(self) -> None:
        self.invalidate()

    def browse(self) -> None:
        if self.vmctl.busy:
            return
        value = self.query_one("#upload-source", Input).value.strip()

        def chosen(path: Path | None) -> None:
            if path is not None:
                self.query_one("#upload-source", Input).value = str(path)

        self.app.push_screen(LocalSourceScreen(Path(value) if value else None), chosen)

    def request(self) -> UploadRequest:
        source = self.query_one("#upload-source", Input).value.strip()
        destination = self.query_one("#upload-destination", Input).value.strip()
        if not source:
            raise VmctlError("Choose a local source file or folder.")
        if not destination:
            raise VmctlError("Enter an existing guest destination directory.")
        try:
            source_path = Path(source).expanduser()
        except RuntimeError as error:
            raise VmctlError(
                "Cannot resolve the home directory in the local source path."
            ) from error
        return UploadRequest(
            reference=self.reference,
            source=source_path,
            destination_directory=destination,
            username=self.query_one("#upload-user", Input).value.strip() or None,
            overwrite=self.query_one("#upload-overwrite", Checkbox).value,
        )

    def show_error(self, error: Exception) -> None:
        self.query_one("#upload-error", Static).update(error_text(error))

    def plan(self) -> None:
        uploader = self.vmctl.uploader
        if uploader is None or self.vmctl.busy or self.vmctl.stale:
            return
        self.invalidate()
        try:
            request = self.request()
        except VmctlError as error:
            self.show_error(error)
            return
        generation = self.generation
        self.query_one("#upload-error", Static).update("")

        def operation() -> UploadPreview:
            preview = uploader.plan(request)
            if str(preview.vm.vmid) != self.reference:
                raise VmctlError("Transfer preview returned a different VM. No files were sent.")
            return preview

        def planned(preview: UploadPreview) -> None:
            if not self.is_mounted or generation != self.generation:
                return
            self.preview = preview
            self.query_one("#upload-preview", Static).update(upload_preview_text(preview))
            self.query_one("#upload-send", Button).disabled = self.vmctl.read_only
            if self.vmctl.read_only:
                self.query_one("#upload-error", Static).update(
                    "Read-only mode: preview can be inspected, but files cannot be sent."
                )

        self.vmctl.submit("Checking upload target", operation, planned, self.show_error)

    def confirm(self) -> None:
        if self.vmctl.busy or self.vmctl.read_only or self.vmctl.stale or self.preview is None:
            return
        preview, generation = self.preview, self.generation

        def confirmed(answer: bool | None) -> None:
            if answer and self.preview is preview and self.generation == generation:
                self.execute(preview)

        self.app.push_screen(
            ManageConfirmScreen("Send files to VM", upload_preview_text(preview)), confirmed
        )

    def execute(self, preview: UploadPreview) -> None:
        uploader = self.vmctl.uploader
        if (
            uploader is None
            or self.vmctl.busy
            or self.vmctl.read_only
            or self.vmctl.stale
            or self.preview is not preview
        ):
            return
        cancel = Event()
        progress_screen = UploadProgressScreen(preview, cancel)
        self.app.push_screen(progress_screen)
        last_reported = 0.0
        last_path = ""
        last_completed = -1

        def progress(value: UploadProgress) -> None:
            nonlocal last_reported, last_path, last_completed
            current = monotonic()
            if (
                current - last_reported >= 0.1
                or value.current_path != last_path
                or value.completed_files != last_completed
                or value.sent_bytes == value.total_bytes
            ):
                last_reported, last_path, last_completed = (
                    current,
                    value.current_path,
                    value.completed_files,
                )
                self.app.call_from_thread(progress_screen.append, value)

        def operation() -> UploadResult:
            return uploader.upload(preview, progress=progress, cancel=cancel)

        def success(result: UploadResult) -> None:
            progress_screen.dismiss(None)
            self.dismiss(result)

        def failed(error: Exception) -> None:
            progress_screen.dismiss(None)
            self.invalidate()
            if isinstance(error, UploadUncertainError):
                self.vmctl.stale = True
            if isinstance(error, UploadCancelledError):
                self.query_one("#upload-error", Static).update(str(error))
            else:
                self.show_error(error)

        self.vmctl.submit(
            "Uploading files",
            operation,
            success,
            failed,
            enabled_controls=("upload-cancel",),
        )

    @on(Button.Pressed)
    def button(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "upload-back":
                self.key_escape()
            case "upload-browse":
                self.browse()
            case "upload-plan":
                self.plan()
            case "upload-send":
                self.confirm()

    def key_escape(self) -> None:
        if not self.vmctl.busy:
            self.dismiss(None)
