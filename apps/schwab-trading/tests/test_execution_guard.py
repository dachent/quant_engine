from __future__ import annotations

import json
from pathlib import Path

import pytest

from execution_guard import SubmissionDisabledError
from schwab_client import SchwabClient
from storage import get_app_paths
import tasks


def _unexpected(*args: object, **kwargs: object) -> object:
    raise AssertionError("submission guard did not run first")


def test_place_orders_task_fails_before_local_or_external_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tasks, "_load_profile", _unexpected)
    monkeypatch.setattr(tasks, "SchwabClient", _unexpected)
    monkeypatch.setattr(tasks, "init_db", _unexpected)

    with pytest.raises(SubmissionDisabledError, match="separate approved Broker Core"):
        tasks.TASKS["place_orders"]({})


def test_client_mutation_methods_fail_before_client_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SchwabClient.__new__(SchwabClient)
    monkeypatch.setattr(client, "ensure_client", _unexpected)

    with pytest.raises(SubmissionDisabledError):
        client.place_order("account-hash", {"orderType": "MARKET"})
    with pytest.raises(SubmissionDisabledError):
        client.cancel_order("account-hash", "order-id")


def test_runner_returns_structured_disabled_result_without_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request = tmp_path / "request.json"
    result = tmp_path / "result.json"
    request.write_text(
        json.dumps({"task_name": "place_orders", "args": {}}), encoding="utf-8"
    )
    monkeypatch.setenv("QUANT_ENGINE_SCHWAB_HOME", str(tmp_path / "runtime"))

    import runner

    monkeypatch.setattr(
        "sys.argv",
        [
            "runner.py",
            "--task",
            "place_orders",
            "--request-file",
            str(request),
            "--result-file",
            str(result),
        ],
    )

    assert runner.main() == 5
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["error"]["type"] == "SubmissionDisabledError"
    assert payload["output"] is None
    assert not (tmp_path / "runtime" / "state" / "app.db").exists()


def test_default_runtime_root_is_outside_the_worktree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QUANT_ENGINE_SCHWAB_HOME", raising=False)
    paths = get_app_paths()
    application_root = Path(__file__).resolve().parents[1]

    assert paths.root.name == "runtime"
    assert paths.root.parent.name == "dataSchwab"
    assert application_root not in paths.root.parents
