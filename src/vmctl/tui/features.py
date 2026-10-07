"""Described system and optional module groups loaded from the worker catalog."""

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Checkbox, Label, Static

from vmctl.errors import VmctlError
from vmctl.operations import CatalogItem
from vmctl.tui.selection import FeatureSelection


class FeatureChoices(Vertical):
    class Changed(Message):
        pass

    def __init__(self, selection: FeatureSelection) -> None:
        super().__init__(id="feature-choices")
        self.selection = selection

    def compose(self) -> ComposeResult:
        yield Label("System features", classes="section-title")
        yield Static("Guest integration and desktop access. Defaults follow the selected template.")
        for item in self.selection.catalog.system_features:
            yield from self.choice(item, "system")
        for group, title, description in (
            (
                "development",
                "Development modules",
                "Optional tools. Leave unchecked for clean compatibility-test VMs.",
            ),
            (
                "ai-cli",
                "AI CLI agents",
                "Optional terminal agents. Sign in inside the guest after installation.",
            ),
            (
                "ai-desktop",
                "AI desktop apps",
                "Optional GUI apps for supported desktop templates. Sign in inside the guest.",
            ),
        ):
            items = [item for item in self.selection.catalog.modules if item.group == group]
            if items:
                yield Label(title, id=f"group-{group}", classes="section-title")
                yield Static(description)
                for item in items:
                    yield from self.choice(item, "dev")
        if self.selection.catalog.profiles:
            yield Label("Profiles", id="bootstrap-profiles", classes="section-title")
            yield Static("Compositions of optional modules; dependencies are deduplicated.")
            for item in self.selection.catalog.profiles:
                yield from self.choice(item, "profile")
        yield Static("", id="features-error", markup=False)

    def choice(self, item: CatalogItem, prefix: str) -> ComposeResult:
        yield Checkbox(item.name, id=f"{prefix}-{item.name}")
        yield Static(
            item.description or "No description configured.",
            classes="choice-description",
            markup=False,
        )
        yield Static("", id=f"note-{prefix}-{item.name}", classes="choice-note", markup=False)

    def on_mount(self) -> None:
        self.update_choices()

    def update_choices(self) -> None:
        for system, prefix, items in (
            (True, "system", self.selection.catalog.system_features),
            (False, "dev", self.selection.catalog.modules),
            (False, "profile", self.selection.catalog.profiles),
        ):
            names = set(self.selection.selected_names(system=system))
            requested = self.selection.system if system else self.selection.development
            required = self.selection.required_names(system=system)
            for item in items:
                supported = self.selection.template in item.templates
                checked = item.name in names or item.name in requested
                checkbox = self.query_one(f"#{prefix}-{item.name}", Checkbox)
                with checkbox.prevent(Checkbox.Changed):
                    checkbox.value = checked
                checkbox.disabled = not supported or item.name in required
                notes = []
                if not supported:
                    notes.append(f"Unavailable for {self.selection.template}.")
                elif item.name in required:
                    notes.append("Required by another selected feature/module.")
                dependencies = item.members if prefix == "profile" else item.dependencies
                if dependencies:
                    notes.append("Includes: " + ", ".join(dependencies))
                note = " ".join(notes)
                checkbox.tooltip = note or item.description
                self.query_one(f"#note-{prefix}-{item.name}", Static).update(note)

    @on(Checkbox.Changed)
    def changed(self, event: Checkbox.Changed) -> None:
        identifier = event.checkbox.id or ""
        if "-" not in identifier:
            return
        prefix, name = identifier.split("-", 1)
        if prefix not in {"system", "dev", "profile"}:
            return
        event.stop()
        try:
            self.selection.toggle(name, event.value, system=prefix == "system")
            self.query_one("#features-error", Static).update("")
            self.update_choices()
        except VmctlError as error:
            self.query_one("#features-error", Static).update(str(error))
        self.post_message(self.Changed())
