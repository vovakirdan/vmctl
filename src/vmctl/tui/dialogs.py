"""Target confirmation, masked credentials and operation progress screens."""

import time

from pydantic import SecretStr
from textual import on
from textual.app import ComposeResult
from textual.color import Gradient
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, Input, Label, ProgressBar, RichLog, Static

from vmctl.errors import VmctlError
from vmctl.utils.passwords import hash_desktop_password


class ConfirmScreen(ModalScreen[bool]):
    def __init__(self, title: str, message: str, *, danger: bool = False) -> None:
        super().__init__()
        self.title_text, self.message_text, self.danger = title, message, danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Static(self.message_text, markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Confirm", id="confirm", variant="error" if self.danger else "primary")

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def key_escape(self) -> None:
        self.dismiss(False)


class PasswordScreen(ModalScreen[SecretStr | None]):
    def __init__(self, username: str) -> None:
        super().__init__()
        self.username = username

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"Desktop password for {self.username}", classes="dialog-title")
            yield Static(
                "A unique GUI/RDP password is sent as a hash through SSH stdin. It is never printed or saved in client configuration."
            )
            yield Input(
                password=True, id="desktop-password", placeholder="Password", max_length=4096
            )
            yield Input(
                password=True, id="desktop-confirm", placeholder="Confirm password", max_length=4096
            )
            yield Static("", id="password-error", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Continue", id="continue", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#desktop-password", Input).focus()

    def on_unmount(self) -> None:
        for field in self.query(Input):
            if field.password:
                field.value = ""

    def clear_passwords(self) -> None:
        self.query_one("#desktop-password", Input).value = ""
        self.query_one("#desktop-confirm", Input).value = ""

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.clear_passwords()
            self.dismiss(None)
            return
        password_input = self.query_one("#desktop-password", Input)
        confirmation = self.query_one("#desktop-confirm", Input)
        if password_input.value != confirmation.value:
            self.query_one("#password-error", Static).update("Passwords do not match.")
            self.clear_passwords()
            return
        try:
            hashed = hash_desktop_password(password_input.value)
        except VmctlError as error:
            self.query_one("#password-error", Static).update(str(error))
            self.clear_passwords()
            return
        self.clear_passwords()
        self.dismiss(hashed)

    def key_escape(self) -> None:
        self.clear_passwords()
        self.dismiss(None)


class TextScreen(ModalScreen[None]):
    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self.title_text, self.content = title, content

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog large-dialog"):
            yield Label(self.title_text, classes="dialog-title")
            with VerticalScroll():
                yield Static(self.content, markup=False)
            yield Button("Close", id="close")

    @on(Button.Pressed, "#close")
    def close(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class ProgressScreen(ModalScreen[None]):
    def __init__(self) -> None:
        super().__init__()
        self.started_at = time.monotonic()
        self.elapsed_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog large-dialog"):
            yield Label("Creating VM", classes="dialog-title")
            yield Static(
                "The server may still be working after a connection failure. Inspect VM state before retrying an unknown outcome."
            )
            yield ProgressBar(
                total=None,
                show_percentage=False,
                show_eta=False,
                gradient=Gradient.from_colors("#22c1c3", "#4f86f7", "#aa6df5"),
                id="operation-bar",
            )
            yield Static("Waiting for worker progress...", id="operation-stage", markup=False)
            yield Static("Elapsed: 0s | Duration is not known in advance", id="operation-time")
            yield RichLog(id="progress-log", markup=False, highlight=False, wrap=True)

    def on_mount(self) -> None:
        self.elapsed_timer = self.set_interval(0.5, self.update_elapsed)

    def on_unmount(self) -> None:
        if self.elapsed_timer is not None:
            self.elapsed_timer.stop()

    def update_elapsed(self) -> None:
        elapsed = int(time.monotonic() - self.started_at)
        minutes, seconds = divmod(elapsed, 60)
        label = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
        self.query_one("#operation-time", Static).update(
            f"Elapsed: {label} | Duration is not known in advance"
        )

    def append(self, message: str) -> None:
        if message.strip():
            self.query_one("#operation-stage", Static).update(message.splitlines()[-1][:200])
        self.query_one("#progress-log", RichLog).write(message)
