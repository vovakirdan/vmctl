"""Target confirmation, masked credentials and operation progress screens."""

from pydantic import SecretStr
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog, Static

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
    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog large-dialog"):
            yield Label("Creating VM", classes="dialog-title")
            yield Static(
                "The server may still be working after a connection failure. Inspect VM state before retrying an unknown outcome."
            )
            yield RichLog(id="progress-log", markup=False, highlight=False, wrap=True)

    def append(self, message: str) -> None:
        self.query_one("#progress-log", RichLog).write(message)
