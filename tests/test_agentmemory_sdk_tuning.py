"""Only the reviewed SDK connection is changed, atomically and idempotently."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "vmctl_sdk_tuning", Path(__file__).parents[1] / "ops/agentmemory/tune_sdk.py"
    )
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_tuning_is_bounded_preserves_other_code_and_is_idempotent(tmp_path: Path) -> None:
    value = module()
    (tmp_path / "package.json").write_text(json.dumps({"version": "0.22.1"}))
    (tmp_path / "dist").mkdir()
    entry = tmp_path / "dist/index.mjs"
    entry.write_text("// Before\n" + value.ORIGINAL + "\n// After\n")
    entry.chmod(0o644)
    assert value.tune_sdk(tmp_path) is True
    assert entry.read_text() == "// Before\n" + value.TUNED + "\n// After\n"
    assert "maxPayload: 256 * 1024 * 1024" in entry.read_text()
    assert entry.stat().st_mode & 0o777 == 0o644
    assert value.tune_sdk(tmp_path) is False
    assert not list(entry.parent.glob(".vmctl-sdk-*"))


@pytest.mark.parametrize("version,source", [("0.23.0", "original"), ("0.22.1", "unreviewed")])
def test_unknown_sdk_is_untouched(tmp_path: Path, version: str, source: str) -> None:
    value = module()
    (tmp_path / "package.json").write_text(json.dumps({"version": version}))
    (tmp_path / "dist").mkdir()
    entry = tmp_path / "dist/index.mjs"
    original = value.ORIGINAL if source == "original" else "changed upstream implementation"
    entry.write_text(original)
    with pytest.raises(ValueError):
        value.tune_sdk(tmp_path)
    assert entry.read_text() == original
