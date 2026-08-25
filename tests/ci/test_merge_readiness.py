from __future__ import annotations

import json
import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "merge_readiness.py"
SPEC = importlib.util.spec_from_file_location("merge_readiness_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
readiness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(readiness)


def _run(
    name: str, conclusion: str = "success", *, app_id: int = 15368
) -> dict[str, object]:
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "app": {"id": app_id},
    }


def test_check_census_paginates_and_requires_every_protected_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    first_page = [_run("quality")] + [_run(f"optional-{index}") for index in range(99)]
    second_page = [_run("security")]

    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        calls.append(endpoint)
        if endpoint.endswith("required_status_checks"):
            return json.dumps({"contexts": ["quality", "security"]})
        if endpoint.endswith("&page=1"):
            return json.dumps({"check_runs": first_page})
        if endpoint.endswith("&page=2"):
            return json.dumps({"check_runs": second_page})
        raise AssertionError(endpoint)

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    failing, pending, missing = readiness._check_states("a" * 40)

    assert (failing, pending, missing) == ([], [], [])
    assert any(call.endswith("&page=2") for call in calls)


def test_check_census_deduplicates_required_context_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    older_quality = _run("quality", "failure")
    older_quality.update({"created_at": "2026-08-24T12:00:00Z", "id": 100})
    first_page = [older_quality] + [
        _run(f"optional-{index}") for index in range(99)
    ]
    newer_quality = _run("quality")
    newer_quality.update({"created_at": "2026-08-25T12:00:00Z", "id": 101})
    second_page = [newer_quality, _run("security")]

    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        calls.append(endpoint)
        if endpoint.endswith("required_status_checks"):
            return json.dumps({"contexts": ["quality", "security"]})
        if endpoint.endswith("&page=1"):
            return json.dumps({"check_runs": first_page})
        if endpoint.endswith("&page=2"):
            return json.dumps({"check_runs": second_page})
        raise AssertionError(endpoint)

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    failing, pending, missing = readiness._check_states("a" * 40)

    assert (failing, pending, missing) == ([], [], [])
    assert any(call.endswith("&page=2") for call in calls)


def test_newest_check_run_controls_duplicate_context_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older_quality = _run("quality", "failure")
    older_quality.update(
        {
            "completed_at": "2026-08-24T12:00:00Z",
            "created_at": "2026-08-24T11:00:00Z",
            "id": 200,
        }
    )
    newer_quality = _run("quality")
    newer_quality.update(
        {
            "completed_at": "2026-08-25T12:00:00Z",
            "created_at": "2026-08-25T11:00:00Z",
            "id": 100,
        }
    )

    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        if endpoint.endswith("required_status_checks"):
            return json.dumps({"contexts": ["quality"]})
        if endpoint.endswith("&page=1"):
            return json.dumps({"check_runs": [older_quality] * 100})
        if endpoint.endswith("&page=2"):
            return json.dumps({"check_runs": [newer_quality]})
        raise AssertionError(endpoint)

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    assert readiness._check_states("b" * 40) == ([], [], [])


@pytest.mark.parametrize("conclusion", ("skipped", "neutral", "cancelled"))
def test_non_success_required_conclusion_never_satisfies_readiness(
    monkeypatch: pytest.MonkeyPatch, conclusion: str
) -> None:
    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        if endpoint.endswith("required_status_checks"):
            return json.dumps({"contexts": ["quality"]})
        return json.dumps({"check_runs": [_run("quality", conclusion)]})

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    failing, pending, missing = readiness._check_states("b" * 40)

    assert failing == ["quality"]
    assert pending == []
    assert missing == []


def test_missing_protected_context_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        if endpoint.endswith("required_status_checks"):
            return json.dumps({"contexts": ["quality", "issue-guard"]})
        if "/statuses?" in endpoint:
            return json.dumps([])
        return json.dumps({"check_runs": [_run("quality")]})

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    assert readiness._check_states("c" * 40) == ([], [], ["issue-guard"])


def test_latest_check_run_controls_duplicate_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        if endpoint.endswith("required_status_checks"):
            return json.dumps({"contexts": ["quality"]})
        return json.dumps({"check_runs": [_run("quality"), _run("quality", "failure")]})

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    assert readiness._check_states("d" * 40) == ([], [], [])


def test_required_check_is_bound_to_protected_app_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        if endpoint.endswith("required_status_checks"):
            return json.dumps(
                {
                    "contexts": ["quality"],
                    "checks": [{"context": "quality", "app_id": 15368}],
                }
            )
        return json.dumps({"check_runs": [_run("quality", app_id=999)]})

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    assert readiness._check_states("e" * 40) == (
        [],
        [],
        ["quality@app:15368"],
    )


def test_legacy_required_context_accepts_successful_commit_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gh(*args: str) -> str:
        endpoint = args[-1]
        if endpoint.endswith("required_status_checks"):
            return json.dumps({"contexts": ["legacy"]})
        if "/check-runs?" in endpoint:
            return json.dumps({"check_runs": []})
        if "/statuses?" in endpoint:
            return json.dumps([{"context": "legacy", "state": "success"}])
        raise AssertionError(endpoint)

    monkeypatch.setattr(readiness, "_gh", fake_gh)

    assert readiness._check_states("f" * 40) == ([], [], [])
