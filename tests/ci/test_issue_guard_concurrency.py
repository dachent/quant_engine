"""Pull-request validation must not share a concurrency slot with issue reconciliation.

GitHub keeps one pending run per concurrency group and cancels the older pending run when a newer
one queues, even with `cancel-in-progress: false`. With one repository-wide group, an issue edit or
the hourly schedule cancelled a pull request's required `validate` check (#472). The expression is
GitHub's, so it cannot be evaluated here; the test pins the two properties that matter.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "issue-guard.yml"
)


def _concurrency() -> dict[str, str]:
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    return workflow["concurrency"]


def test_pull_request_runs_get_a_group_of_their_own() -> None:
    group = _concurrency()["group"]
    assert "github.event_name == 'pull_request'" in group
    assert "github.event.pull_request.number" in group


def test_reconciliation_stays_serialised_repository_wide() -> None:
    concurrency = _concurrency()
    assert concurrency["group"].rstrip("}").rstrip().endswith("|| github.repository")
    assert concurrency["cancel-in-progress"] == "false"
