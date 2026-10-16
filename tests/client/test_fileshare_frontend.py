"""Opt-in fileshare presentation uses public metadata without credentials."""

import asyncio
from dataclasses import replace

import pytest
import typer
from pydantic import TypeAdapter
from test_tui import FakeOperations, app_for, catalog
from textual.widgets import Checkbox, Static
from typer.core import TyperCommand
from typer.testing import CliRunner

from vmctl import cli, completion
from vmctl.models import CreateRequest, CreateResult
from vmctl.operations import Catalog, CatalogItem, CreatePreview
from vmctl.tui.create import CreateScreen
from vmctl.tui.features import FeatureChoices
from vmctl.tui.formatting import details_text, preview_text, result_text
from vmctl.tui.selection import FeatureSelection

ENDPOINT = "sftp://10.210.0.110:2022"
OWN_DIRECTORY = "project-105-abcdef012345"
DESCRIPTION = "Read shared files; write/delete only in this VM's own directory. No mount."


def fileshare_catalog(*, configured: bool = True) -> Catalog:
    original = catalog()
    feature = CatalogItem(
        "fileshare",
        DESCRIPTION,
        tuple(original.templates) if configured else (),
        unavailable_reason=None if configured else "Configure fileshare on the worker first.",
    )
    return replace(original, system_features=(*original.system_features, feature))


def preview() -> CreatePreview:
    return replace(
        FakeOperations().plan_create(CreateRequest("project", "ubuntu-server", "small")),
        system_features=("qemu-agent", "fileshare"),
        fileshare_endpoint=ENDPOINT,
    )


def result() -> CreateResult:
    return replace(
        FakeOperations().create(
            CreateRequest("project", "ubuntu-server", "small"),
            request_id="a" * 32,
            progress=lambda _: None,
        ),
        system_features=("qemu-agent", "fileshare"),
        fileshare_endpoint=ENDPOINT,
        fileshare_account="vmctl-105-abcdef",
        fileshare_directory=OWN_DIRECTORY,
    )


def test_fileshare_is_opt_in_separate_from_development_and_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = fileshare_catalog()
    selection = FeatureSelection(data, "ubuntu-server")
    selection.set_template("ubuntu-server")
    assert "fileshare" not in selection.selected_names(system=True)
    selection.toggle("rust", True, system=False)
    assert "fileshare" not in selection.selected_names(system=True)
    selection.toggle("fileshare", True, system=True)
    assert selection.selected_names(system=False) == ("base", "rust")
    monkeypatch.setattr(completion, "read_catalog", lambda _: data)
    context = typer.Context(TyperCommand("create"))
    context.params = {"template": "ubuntu-server"}
    assert completion.system(context, "fi") == [("fileshare", DESCRIPTION)]
    assert completion.development(context, "fi") == []
    monkeypatch.setattr(completion, "read_catalog", lambda _: fileshare_catalog(configured=False))
    assert completion.system(context, "fi") == []


@pytest.mark.parametrize("configured", [True, False])
def test_checkbox_is_described_off_by_default_and_explains_missing_configuration(
    configured: bool,
) -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        backend.data = fileshare_catalog(configured=configured)
        app = app_for(backend)
        async with app.run_test(size=(100, 36)) as pilot:
            await app.workers.wait_for_complete()
            app.action_new()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CreateScreen)
            checkbox = screen.query_one("#system-fileshare", Checkbox)
            assert not checkbox.value
            assert checkbox.disabled is not configured
            descriptions = " ".join(str(widget.render()) for widget in screen.query(Static))
            assert "Read shared files" in descriptions
            if configured:
                checkbox.value = True
                await pilot.pause()
                choices = screen.query_one(FeatureChoices)
                assert "fileshare" in choices.selection.selected_names(system=True)
                assert choices.selection.selected_names(system=False) == ()
            else:
                assert "Configure fileshare on the worker first" in descriptions
                assert "fileshare" not in screen.selection.selected_names(system=True)

    asyncio.run(scenario())


def test_public_preview_result_and_metadata_survive_wire_roundtrip() -> None:
    planned = TypeAdapter(CreatePreview).validate_json(
        TypeAdapter(CreatePreview).dump_json(preview())
    )
    created = TypeAdapter(CreateResult).validate_json(TypeAdapter(CreateResult).dump_json(result()))
    text = preview_text(planned) + result_text(created)
    assert ENDPOINT in text and OWN_DIRECTORY in text
    assert "write/delete only" in text and "does not create credentials" in text
    assert "vmctl-files" in text and "PRIVATE KEY" not in text
    row = replace(
        FakeOperations().rows[0],
        metadata={
            "system_features": "qemu-agent,fileshare",
            "fileshare_endpoint": ENDPOINT,
            "fileshare_account": "vmctl-105-abcdef",
            "fileshare_directory": OWN_DIRECTORY,
        },
    )
    assert ENDPOINT in details_text(row) and OWN_DIRECTORY in details_text(row)
    assert "Fileshare:" not in details_text(FakeOperations().rows[0])


def test_cli_dry_run_shows_policy_without_provisioning(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend(FakeOperations):
        def plan_create(self, request: CreateRequest) -> CreatePreview:
            assert request.system_features == ("fileshare",)
            return preview()

    backend = Backend()
    monkeypatch.setattr(cli, "operations", lambda _: backend)
    monkeypatch.setattr(cli, "load_public_key", lambda *_: "ssh-ed25519 mock")
    command = CliRunner().invoke(
        cli.app,
        ["create", "project", "ubuntu-server", "small", "--with-system", "fileshare", "--dry-run"],
    )
    assert command.exit_code == 0, command.output
    assert ENDPOINT in command.output
    assert "read shared files" in command.output
    assert "No credentials created by dry-run" in command.output
    assert "create" not in backend.calls


def test_cli_create_and_info_display_public_fileshare_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Backend(FakeOperations):
        def create(
            self, request: CreateRequest, *, request_id: str, progress: object
        ) -> CreateResult:
            return result()

    backend = Backend()
    backend.rows[0] = replace(
        backend.rows[0],
        metadata={
            "system_features": "qemu-agent,fileshare",
            "fileshare_endpoint": ENDPOINT,
            "fileshare_account": "vmctl-105-abcdef",
            "fileshare_directory": OWN_DIRECTORY,
        },
    )
    monkeypatch.setattr(cli, "operations", lambda _: backend)
    monkeypatch.setattr(cli, "load_public_key", lambda *_: "ssh-ed25519 mock")
    for args in (
        ["create", "project", "ubuntu-server", "small", "--with-system", "fileshare"],
        ["info", "104"],
    ):
        response = CliRunner().invoke(cli.app, args)
        assert response.exit_code == 0, response.output
        assert ENDPOINT in response.output and OWN_DIRECTORY in response.output
        assert "PRIVATE KEY" not in response.output
