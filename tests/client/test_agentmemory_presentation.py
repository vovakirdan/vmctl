"""Shared memory choices and public presentation never carry guest credentials."""

import asyncio
from dataclasses import replace
from ipaddress import IPv4Address

import pytest
from test_remote_cli import FrontendBackend
from test_tui import FakeOperations, app_for, catalog
from textual.widgets import Checkbox, Static
from typer.testing import CliRunner

from vmctl import cli
from vmctl.models import VM, CreateRequest, CreateResult, Resources, VMDetails
from vmctl.operations import CatalogItem, CreatePreview, Progress
from vmctl.tui.features import FeatureChoices
from vmctl.tui.formatting import details_text, preview_text, result_text
from vmctl.tui.selection import FeatureSelection

ENDPOINT = "https://10.210.0.110"
CLIENT_ID = "vmctl-104-0123456789abcdef"


class MemoryBackend(FrontendBackend):
    def plan_create(self, request: CreateRequest) -> CreatePreview:
        return replace(
            super().plan_create(request),
            system_features=("qemu-agent", "agentmemory"),
            agentmemory_endpoint=ENDPOINT,
            agentmemory_adapters=("codex", "claude", "opencode"),
        )

    def create(
        self, request: CreateRequest, *, request_id: str, progress: Progress
    ) -> CreateResult:
        return replace(
            super().create(request, request_id=request_id, progress=progress),
            system_features=("qemu-agent", "agentmemory"),
            agentmemory_endpoint=ENDPOINT,
            agentmemory_client_id=CLIENT_ID,
            agentmemory_adapters=("codex", "claude", "opencode"),
        )


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_memory_output_reports_request_not_verified_guest_setup(
    monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    backend = MemoryBackend(desktop=False)
    monkeypatch.setattr(cli, "operations", lambda _: backend)
    monkeypatch.setattr(cli, "public_key", lambda *_: None)
    args = ["create", "project", "debian", "small", "--with-system", "agentmemory"]
    if dry_run:
        args.append("--dry-run")
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert ENDPOINT in result.output
    assert "codex, claude, opencode" in result.output
    if dry_run:
        assert not backend.requests and "no credentials" in result.output
    else:
        assert CLIENT_ID in result.output and "not been verified" in result.output
    assert "AGENTMEMORY_SECRET" not in result.output


def test_tui_memory_public_preview_result_details() -> None:
    backend = MemoryBackend(desktop=False)
    request = CreateRequest("project", "debian", "small")
    preview = preview_text(backend.plan_create(request))
    result = result_text(backend.create(request, request_id="test", progress=lambda _: None))
    details = details_text(
        VMDetails(
            VM(104, "project", "running", "pve"),
            {},
            {
                "system_features": "qemu-agent,agentmemory",
                "agentmemory_endpoint": ENDPOINT,
                "agentmemory_client_id": CLIENT_ID,
                "agentmemory_adapters": "codex,claude,opencode",
            },
            IPv4Address("10.210.0.104"),
        )
    )
    assert ENDPOINT in preview and "creates no credentials" in preview
    assert CLIENT_ID in result and "not been verified" in result
    assert CLIENT_ID in details and "guest setup not verified" in details
    plain = result_text(
        CreateResult(
            104,
            "plain",
            "debian",
            "small",
            Resources(2, 2048, 20),
            "BC:24:11:AA:BB:CC",
            IPv4Address("10.210.0.104"),
            "vmadmin",
            True,
            (),
            "not checked",
        )
    )
    assert "Shared memory" not in plain


def test_unconfigured_memory_checkbox_is_off_and_explains_why() -> None:
    async def scenario() -> None:
        backend = FakeOperations()
        base = catalog()
        backend.data = replace(
            base,
            system_features=base.system_features
            + (
                CatalogItem(
                    "agentmemory",
                    "Shared memory; AI agents remain optional.",
                    (),
                    unavailable_reason="Configure agentmemory on the worker before granting VM access.",
                ),
            ),
        )
        app = app_for(backend, read_only=True)
        async with app.run_test(size=(100, 36)) as pilot:
            await app.workers.wait_for_complete()
            app.action_new()
            await pilot.pause()
            choices = app.screen.query_one(FeatureChoices)
            checkbox = choices.query_one("#system-agentmemory", Checkbox)
            assert checkbox.disabled and not checkbox.value
            assert "Configure agentmemory" in str(
                choices.query_one("#note-system-agentmemory", Static).render()
            )
            assert "create" not in backend.calls

    asyncio.run(scenario())


def test_memory_selection_does_not_select_agents_or_development_modules() -> None:
    base = catalog()
    choices = replace(
        base,
        system_features=base.system_features
        + (
            CatalogItem("agentmemory", "Shared remote memory", ("ubuntu-server", "ubuntu-desktop")),
            CatalogItem("fileshare", "File exchange", ("ubuntu-server", "ubuntu-desktop")),
        ),
    )
    selection = FeatureSelection(choices, "ubuntu-server")
    selection.toggle("agentmemory", True, system=True)
    selection.toggle("fileshare", True, system=True)
    assert set(selection.selected_names(system=True)) == {"fileshare", "agentmemory"}
    assert selection.selected_names(system=False) == ()
