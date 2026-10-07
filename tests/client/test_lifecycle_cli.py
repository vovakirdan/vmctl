"""Portable lifecycle presentation without host access."""

from dataclasses import dataclass, field

import pytest
from typer.testing import CliRunner

from vmctl import cli
from vmctl.errors import VmctlError
from vmctl.models import VM
from vmctl.operations import ActionPreview, ActionResult, LifecycleAction


@dataclass
class Backend:
    actions: list[ActionPreview] = field(default_factory=list)
    planned: ActionPreview | None = None

    def plan_action(self, reference: str, action: LifecycleAction) -> ActionPreview:
        assert reference == "work"
        self.planned = ActionPreview(VM(104, "work", "running", "pve"), "f" * 64, action)
        return self.planned

    def action(self, expected: ActionPreview) -> ActionResult:
        assert expected is self.planned
        self.actions.append(expected)
        return ActionResult(expected.vm, expected.action)


@pytest.mark.parametrize("action", ["start", "shutdown", "reboot"])
def test_dry_run_never_confirms_or_mutates(action: str, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = Backend()
    monkeypatch.setattr(cli, "operations", lambda _: backend)
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **kw: pytest.fail("Unexpected prompt"))
    result = CliRunner().invoke(cli.app, [action, "work", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "104" in result.output and "work" in result.output and not backend.actions


@pytest.mark.parametrize("action", ["shutdown", "reboot"])
def test_confirmation_identifies_resolved_target(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = Backend()
    monkeypatch.setattr(cli, "operations", lambda _: backend)
    runner = CliRunner()
    result = runner.invoke(cli.app, [action, "work"], input="n\n")
    assert result.exit_code == 1 and not backend.actions
    assert "104" in result.output and "work" in result.output
    result = runner.invoke(cli.app, [action, "work"], input="y\n")
    assert result.exit_code == 0, result.output
    assert len(backend.actions) == 1


def test_start_and_yes_do_not_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = Backend()
    monkeypatch.setattr(cli, "operations", lambda _: backend)
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **kw: pytest.fail("Unexpected prompt"))
    for args in (["start", "work"], ["shutdown", "work", "--yes"]):
        result = CliRunner().invoke(cli.app, args)
        assert result.exit_code == 0, result.output
    assert len(backend.actions) == 2


def test_preflight_error_is_presented_without_action(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = Backend()

    def fail(reference: str, action: LifecycleAction) -> ActionPreview:
        raise VmctlError("VM is locked")

    monkeypatch.setattr(backend, "plan_action", fail)
    monkeypatch.setattr(cli, "operations", lambda _: backend)
    result = CliRunner().invoke(cli.app, ["start", "work"])
    assert result.exit_code == 1 and "VM is locked" in result.output
    assert not backend.actions
