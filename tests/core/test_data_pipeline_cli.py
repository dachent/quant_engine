from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "data_pipeline.py"
SPEC = importlib.util.spec_from_file_location("data_pipeline_cli", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
data_pipeline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(data_pipeline)


@pytest.mark.parametrize("command", ["plan", "status"])
def test_cli_returns_nonzero_for_redacted_required_not_ready_status(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = {
        "financial_mvp": {
            "blocking_reasons": ["SOURCE:required-source"],
            "ready_for_execution": False,
        },
        "pipeline_id": "financial-mvp-test",
    }
    monkeypatch.setattr(data_pipeline, "load_pipeline_spec", lambda _path: object())
    monkeypatch.setattr(data_pipeline, "plan_pipeline", lambda _spec: result)
    monkeypatch.setattr(data_pipeline, "pipeline_status", lambda _spec: result)

    return_code = data_pipeline.main([command, "--spec", "synthetic.json"])

    assert return_code == 1
    assert '"ready_for_execution":false' in capsys.readouterr().out
