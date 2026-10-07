"""Catalog-driven AI groups reuse the optional module dependency selection."""

import asyncio
from dataclasses import replace

from test_tui import FakeOperations, app_for, catalog
from textual.widgets import Checkbox, Label, Select, Static

from vmctl.operations import Catalog, CatalogItem
from vmctl.tui.create import CreateScreen
from vmctl.tui.features import FeatureChoices


def ai_catalog() -> Catalog:
    original = catalog()
    supported = ("ubuntu-server", "ubuntu-desktop")
    return replace(
        original,
        modules=original.modules
        + (
            CatalogItem(
                "custom-terminal-agent",
                "Editable terminal agent description.",
                supported,
                ("node",),
                group="ai-cli",
            ),
            CatalogItem(
                "other-terminal-agent",
                "Another agent sharing the Node runtime.",
                supported,
                ("node",),
                group="ai-cli",
            ),
            CatalogItem(
                "custom-gui-app",
                "Editable desktop application description.",
                ("ubuntu-desktop",),
                group="ai-desktop",
            ),
        ),
        profiles=original.profiles
        + (
            CatalogItem(
                "custom-agents",
                "A configurable AI composition.",
                supported,
                members=("custom-terminal-agent", "other-terminal-agent"),
            ),
        ),
    )


def test_optional_ai_groups_descriptions_and_compatibility_come_from_catalog() -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        backend.data = ai_catalog()
        app = app_for(backend, read_only=True)
        async with app.run_test(size=(100, 36)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_new()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CreateScreen)
            choices = screen.query_one(FeatureChoices)
            labels = [str(label.render()) for label in choices.query(Label)]
            assert labels == [
                "System features",
                "Development modules",
                "AI CLI agents",
                "AI desktop apps",
                "Profiles",
            ]
            assert str(choices.query_one("#group-ai-cli", Label).render()) == "AI CLI agents"
            assert str(choices.query_one("#group-ai-desktop", Label).render()) == "AI desktop apps"
            assert str(choices.query_one("#bootstrap-profiles", Label).render()) == "Profiles"
            assert screen.selection.development == set()
            for module in backend.data.modules:
                checkbox = choices.query_one(f"#dev-{module.name}", Checkbox)
                assert not checkbox.value
                assert checkbox.disabled == (screen.template_name not in module.templates)
            descriptions = [str(widget.render()) for widget in choices.query(".choice-description")]
            assert "Editable terminal agent description." in descriptions
            assert "Editable desktop application description." in descriptions
            assert "Unavailable for ubuntu-server." in str(
                choices.query_one("#note-dev-custom-gui-app", Static).render()
            )
            assert choices.query_one("#dev-node", Checkbox).value is False
            assert choices.query_one("#system-qemu-agent", Checkbox).value is True
            assert choices.query_one("#system-desktop-rdp", Checkbox).disabled is True
            assert "create" not in backend.calls

    asyncio.run(scenario())


def test_agents_share_automatic_node_dependency_until_last_agent_is_unchecked() -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        backend.data = ai_catalog()
        app = app_for(backend, read_only=True)
        async with app.run_test(size=(100, 36)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_new()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CreateScreen)
            choices = screen.query_one(FeatureChoices)
            first = choices.query_one("#dev-custom-terminal-agent", Checkbox)
            second = choices.query_one("#dev-other-terminal-agent", Checkbox)
            node = choices.query_one("#dev-node", Checkbox)
            first.value = True
            await pilot.pause()
            assert node.value and node.disabled
            assert "Includes: node" in str(
                choices.query_one("#note-dev-custom-terminal-agent", Static).render()
            )
            assert "Required by another selected" in str(
                choices.query_one("#note-dev-node", Static).render()
            )
            second.value = True
            await pilot.pause()
            assert screen.selection.selected_names(system=False).count("node") == 1
            first.value = False
            await pilot.pause()
            assert node.value and node.disabled
            second.value = False
            await pilot.pause()
            assert not node.value and not node.disabled
            assert screen.selection.development == set()
            assert screen.selection.selected_names(system=True) == ("qemu-agent",)

    asyncio.run(scenario())


def test_ai_profile_composes_same_modules_and_template_changes_clear_incompatible_choices() -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        backend.data = ai_catalog()
        app = app_for(backend, read_only=True)
        async with app.run_test(size=(100, 36)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_new()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CreateScreen)
            choices = screen.query_one(FeatureChoices)
            screen.query_one("#template", Select).value = "ubuntu-desktop"
            await pilot.pause()
            gui = choices.query_one("#dev-custom-gui-app", Checkbox)
            assert not gui.disabled and not gui.value
            gui.value = True
            choices.query_one("#profile-custom-agents", Checkbox).value = True
            await pilot.pause()
            names = screen.selection.selected_names(system=False)
            assert {
                "node",
                "custom-terminal-agent",
                "other-terminal-agent",
                "custom-gui-app",
            }.issubset(names)
            assert names.count("node") == 1
            assert choices.query_one("#dev-node", Checkbox).disabled
            screen.query_one("#template", Select).value = "ubuntu-server"
            await pilot.pause()
            assert gui.disabled and not gui.value
            assert "custom-gui-app" not in screen.selection.development
            assert choices.query_one("#profile-custom-agents", Checkbox).value
            screen.query_one("#template", Select).value = "alpine"
            await pilot.pause()
            assert screen.selection.development == set()
            assert choices.query_one("#dev-node", Checkbox).disabled
            assert not choices.query_one("#dev-node", Checkbox).value
            assert choices.query_one("#dev-custom-terminal-agent", Checkbox).disabled
            assert not choices.query_one("#profile-custom-agents", Checkbox).value
            screen.query_one("#template", Select).value = "ubuntu-desktop"
            await pilot.pause()
            assert screen.selection.development == set()
            assert not gui.value

    asyncio.run(scenario())


def test_legacy_catalog_defaults_to_development_without_empty_ai_sections() -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        assert all(item.group == "development" for item in backend.data.modules)
        app = app_for(backend)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_new()
            await pilot.pause()
            labels = [
                str(label.render()) for label in app.screen.query_one(FeatureChoices).query(Label)
            ]
            assert "Development modules" in labels
            assert "AI CLI agents" not in labels
            assert "AI desktop apps" not in labels

    asyncio.run(scenario())
