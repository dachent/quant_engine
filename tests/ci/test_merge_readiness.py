from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "merge_readiness.py"
WORKFLOW_PATH = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "merge-readiness.yml"
)
SPEC = importlib.util.spec_from_file_location("merge_readiness_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
readiness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(readiness)


def test_workflow_grants_read_access_for_commit_status_census() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

    assert workflow["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
        "checks": "read",
        "statuses": "read",
    }


def _run(
    name: str,
    conclusion: str = "success",
    *,
    status: str = "completed",
    app_id: int = 15368,
) -> dict[str, object]:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "app": {"id": app_id},
    }


def _status(
    context: str, state: str = "success", **fields: object
) -> dict[str, object]:
    return {"context": context, "state": state, **fields}


def test_census_paginates_without_protected_branch_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    first_page = [_run("quality")] + [_run(f"optional-{index}") for index in range(99)]

    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        calls.append(endpoint)
        if "/check-runs?" in endpoint and endpoint.endswith("&page=1"):
            return json.dumps({"check_runs": first_page})
        if "/check-runs?" in endpoint and endpoint.endswith("&page=2"):
            return json.dumps({"check_runs": [_run("security")]})
        if "/statuses?" in endpoint:
            return json.dumps([])
        raise AssertionError(endpoint)

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    assert readiness._check_states("a" * 40) == ([], [])
    assert any("/check-runs?" in call and call.endswith("&page=2") for call in calls)
    assert any("/statuses?" in call for call in calls)
    assert not any("protection" in call for call in calls)


def test_newest_check_run_controls_duplicate_context_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = _run("quality", "failure")
    older.update({"completed_at": "2026-08-24T12:00:00Z", "id": 200})
    newer = _run("quality")
    newer.update({"completed_at": "2026-08-25T12:00:00Z", "id": 100})

    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        if "/check-runs?" in endpoint and endpoint.endswith("&page=1"):
            return json.dumps({"check_runs": [older] * 100})
        if "/check-runs?" in endpoint and endpoint.endswith("&page=2"):
            return json.dumps({"check_runs": [newer]})
        if "/statuses?" in endpoint:
            return json.dumps([])
        raise AssertionError(endpoint)

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    assert readiness._check_states("b" * 40) == ([], [])


def test_newest_commit_status_is_censused_and_can_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = _status("legacy", updated_at="2026-08-24T12:00:00Z", id=200)
    newer = _status("legacy", "failure", updated_at="2026-08-25T12:00:00Z", id=100)
    first_page = [older] + [_status(f"filler-{index}") for index in range(99)]

    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        if "/check-runs?" in endpoint:
            return json.dumps({"check_runs": []})
        if "/statuses?" in endpoint and endpoint.endswith("&page=1"):
            return json.dumps(first_page)
        if "/statuses?" in endpoint and endpoint.endswith("&page=2"):
            return json.dumps([newer])
        raise AssertionError(endpoint)

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    assert readiness._check_states("c" * 40) == (["legacy"], [])


@pytest.mark.parametrize("conclusion", ("skipped", "neutral"))
def test_optional_nonfailing_conclusions_do_not_block(
    monkeypatch: pytest.MonkeyPatch, conclusion: str
) -> None:
    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        if "/check-runs?" in endpoint:
            return json.dumps({"check_runs": [_run("optional", conclusion)]})
        if "/statuses?" in endpoint:
            return json.dumps([])
        raise AssertionError(endpoint)

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    assert readiness._check_states("d" * 40) == ([], [])


@pytest.mark.parametrize(
    "conclusion", ("action_required", "cancelled", "failure", "stale", "timed_out")
)
def test_completed_failure_conclusions_block(
    monkeypatch: pytest.MonkeyPatch, conclusion: str
) -> None:
    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        if "/check-runs?" in endpoint:
            return json.dumps({"check_runs": [_run("quality", conclusion)]})
        if "/statuses?" in endpoint:
            return json.dumps([])
        raise AssertionError(endpoint)

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    assert readiness._check_states("e" * 40) == (["quality"], [])


@pytest.mark.parametrize("status", ("queued", "in_progress"))
def test_nonterminal_check_runs_are_pending(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        if "/check-runs?" in endpoint:
            return json.dumps({"check_runs": [_run("quality", status=status)]})
        if "/statuses?" in endpoint:
            return json.dumps([])
        raise AssertionError(endpoint)

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    assert readiness._check_states("f" * 40) == ([], ["quality"])


def test_self_check_is_excluded_from_its_own_pending_census(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        if "/check-runs?" in endpoint:
            return json.dumps(
                {"check_runs": [_run(readiness.SELF_CHECK_NAME, status="in_progress")]}
            )
        if "/statuses?" in endpoint:
            return json.dumps([_status(readiness.SELF_CHECK_NAME, "pending")])
        raise AssertionError(endpoint)

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    assert readiness._check_states("g" * 40) == ([], [])


def test_merge_state_still_blocks_when_ci_census_is_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        readiness,
        "_gh",
        lambda *args: json.dumps(
            {"state": "OPEN", "headRefOid": "h" * 40, "mergeStateStatus": "BLOCKED"}
        ),
    )
    monkeypatch.setattr(readiness, "_unresolved_thread_count", lambda number: 0)
    monkeypatch.setattr(readiness, "_check_states", lambda head: ([], []))
    monkeypatch.setattr(sys, "argv", ["merge_readiness.py", "383"])

    assert readiness.main() == 1
    assert "mergeStateStatus=BLOCKED" in capsys.readouterr().out
