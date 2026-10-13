"""CLI management previews, confirmations and partial results are portable."""

import pytest
from test_manage_transport import ManagementBackend
from typer.testing import CliRunner

from vmctl import cli
from vmctl.operations import Progress, SpicePreview, SpiceResult


@pytest.mark.parametrize(
    "arguments,expected",
    [
        (["clone", "300", "android-copy", "--dry-run"], "plan_clone"),
        (["update", "300", "--cpu", "8", "--memory", "8G", "--dry-run"], "plan_update"),
        (["spice", "300", "--guest-tools", "skip", "--dry-run"], "plan_spice"),
    ],
)
def test_cli_previews_never_mutate(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], expected: str
) -> None:
    backend = ManagementBackend()
    monkeypatch.setattr(cli, "operations", lambda _: backend)
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert backend.calls == [expected]


@pytest.mark.parametrize(
    "arguments,plan,action",
    [
        (["clone", "300", "android-copy"], "plan_clone", "clone"),
        (["update", "300", "--cpu", "8"], "plan_update", "update"),
        (["spice", "300", "--guest-tools", "skip"], "plan_spice", "spice"),
    ],
)
def test_cli_confirmation_decline_or_frozen_apply(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], plan: str, action: str
) -> None:
    backend = ManagementBackend()
    monkeypatch.setattr(cli, "operations", lambda _: backend)
    declined = CliRunner().invoke(cli.app, arguments, input="n\n")
    assert declined.exit_code != 0 and backend.calls == [plan]
    backend.calls.clear()
    applied = CliRunner().invoke(cli.app, [*arguments, "--yes"])
    assert applied.exit_code == 0, applied.output
    assert backend.calls == [plan, action]


def test_cli_spice_uncertain_is_nonzero_and_exposes_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend(ManagementBackend):
        def spice(self, expected: SpicePreview, *, progress: Progress) -> SpiceResult:
            return SpiceResult(
                self.vm, "virtio", True, "uncertain", 73, ("The guest job may still run",)
            )

    monkeypatch.setattr(cli, "operations", lambda _: Backend())
    result = CliRunner().invoke(cli.app, ["spice", "300", "--yes"])
    assert result.exit_code == 1
    assert "uncertain" in result.output and "73" in result.output
    assert "may still run" in result.output
