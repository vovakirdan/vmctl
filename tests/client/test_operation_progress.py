"""Operation progress reports real stages without inventing completion percentages."""

import asyncio
from typing import Literal

import pytest
from test_tui import FakeOperations, app_for
from textual.widgets import Label, ProgressBar, Static

from vmctl.tui.dialogs import ProgressScreen
from vmctl.tui.manage import ManageProgressScreen


@pytest.mark.parametrize("kind", ["create", "manage"])
def test_operation_stage_elapsed_and_dismissal(kind: Literal["create", "manage"]) -> None:
    async def scenario() -> None:
        app = app_for(FakeOperations())
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            screen = ProgressScreen() if kind == "create" else ManageProgressScreen("Copying VM")
            app.push_screen(screen)
            await pilot.pause()
            bar = screen.query_one("#operation-bar", ProgressBar)
            assert bar.total is None
            assert not bar.show_percentage and not bar.show_eta
            assert bar.gradient is not None
            assert screen.elapsed_timer is not None
            screen.started_at -= 65
            screen.update_elapsed()
            assert "Elapsed: 1m" in str(screen.query_one("#operation-time", Static).render())
            screen.append("[red]Configuring guest tools")
            assert "[red]Configuring guest tools" in str(
                screen.query_one("#operation-stage", Static).render()
            )
            if kind == "manage":
                assert "Copying VM" in str(screen.query_one(Label).render())
            screen.dismiss(None)
            await pilot.pause()
            assert screen not in app.screen_stack

    asyncio.run(scenario())
