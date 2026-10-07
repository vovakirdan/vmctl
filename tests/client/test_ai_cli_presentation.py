"""Portable grouped CLI choices and completion use editable worker catalogs."""

from ipaddress import IPv4Address

import pytest
import typer
from typer.core import TyperCommand
from typer.testing import CliRunner

from vmctl import cli, completion
from vmctl.operations import Catalog, CatalogItem


class ChoicesBackend:
    def catalog(self) -> Catalog:
        return Catalog(
            {},
            {},
            modules=(
                CatalogItem("language", "Custom language", ("server",)),
                CatalogItem(
                    "bot", "Custom CLI bot", ("server", "desktop"), ("language",), group="ai-cli"
                ),
                CatalogItem("gui", "Custom GUI", ("desktop",), group="ai-desktop"),
            ),
            profiles=(),
            system_features=(),
            pool_start=IPv4Address("10.210.0.100"),
            pool_end=IPv4Address("10.210.0.199"),
        )


def test_cli_bootstrap_displays_editable_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "operations", lambda _: ChoicesBackend())
    result = CliRunner().invoke(cli.app, ["bootstrap"])
    assert result.exit_code == 0, result.output
    assert "AI CLI agents" in result.output and "AI desktop apps" in result.output
    assert "Custom CLI bot" in result.output and "Custom GUI" in result.output
    assert result.output.index("language") < result.output.index("bot") < result.output.index("gui")


def test_completion_honors_group_agnostic_compatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(completion, "read_catalog", lambda _: ChoicesBackend().catalog())
    context = typer.Context(TyperCommand("create"))
    context.params = {"template": "server"}
    assert completion.development(context, "b") == [("bot", "Custom CLI bot")]
    assert completion.development(context, "g") == []
    context.params = {"template": "desktop"}
    assert completion.development(context, "bot,g") == [("bot,gui", "Custom GUI")]
