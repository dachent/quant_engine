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


def test_execute_commit_forwards_commit_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = object()
    authorization = tmp_path / "owner-publication-authorization.json"
    observed: dict[str, object] = {}
    monkeypatch.setattr(data_pipeline, "load_pipeline_spec", lambda _path: spec)

    def execute(received: object, **kwargs: object) -> dict[str, object]:
        observed["spec"] = received
        observed.update(kwargs)
        return {"release_state": "COMMITTED"}

    monkeypatch.setattr(data_pipeline, "execute_pipeline", execute)
    monkeypatch.setattr(data_pipeline, "is_replica_spec", lambda _s: True)

    return_code = data_pipeline.main(
        [
            "execute",
            "--spec",
            str(tmp_path / "spec.json"),
            "--commit",
            "--commit-authorization",
            str(authorization),
        ]
    )

    assert return_code == 0
    assert observed == {
        "allow_external_read": False,
        "commit": True,
        "commit_authorization": authorization,
        "spec": spec,
    }


def test_real_roots_commit_without_replica_receipt_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(data_pipeline, "load_pipeline_spec", lambda _path: object())
    monkeypatch.setattr(data_pipeline, "is_replica_spec", lambda _s: False)
    called: list[object] = []
    monkeypatch.setattr(
        data_pipeline, "execute_pipeline", lambda *a, **k: called.append(a)
    )

    return_code = data_pipeline.main(
        ["execute", "--spec", str(tmp_path / "s.json"), "--commit"]
    )

    assert return_code == 1
    assert called == []
    assert "requires --replica-receipt" in capsys.readouterr().err


def test_real_roots_commit_verifies_the_replica_receipt_then_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = object()
    receipt = tmp_path / "receipt.json"
    seen: list[tuple[object, object]] = []
    monkeypatch.setattr(data_pipeline, "load_pipeline_spec", lambda _path: spec)
    monkeypatch.setattr(data_pipeline, "is_replica_spec", lambda _s: False)
    monkeypatch.setattr(
        data_pipeline,
        "verify_replica_receipt",
        lambda path, s: seen.append((path, s)) or {},
    )
    monkeypatch.setattr(
        data_pipeline,
        "execute_pipeline",
        lambda *a, **k: {"release_state": "COMMITTED"},
    )

    return_code = data_pipeline.main(
        [
            "execute",
            "--spec",
            str(tmp_path / "s.json"),
            "--commit",
            "--replica-receipt",
            str(receipt),
        ]
    )

    assert return_code == 0
    assert seen == [(receipt, spec)]


def test_dry_run_needs_no_replica_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(data_pipeline, "load_pipeline_spec", lambda _path: object())
    monkeypatch.setattr(
        data_pipeline, "execute_pipeline", lambda *a, **k: {"release_state": "PREPARED"}
    )
    assert data_pipeline.main(["execute", "--spec", str(tmp_path / "s.json")]) == 0
