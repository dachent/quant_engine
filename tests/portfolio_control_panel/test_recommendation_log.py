"""The recommendation log: append-only, hash-chained, folded for the panel."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import recommendation_log as rl  # noqa: E402


def _rec(**over: object) -> dict:
    row = {
        "kind": "RECOMMENDATION",
        "recorded_on": "2026-11-17",
        "run_id": "run-2026-11-17",
        "pack_id": "PACK-0123456789ab",
        "policy_version": "1",
        "date": "2026-11-17",
        "type": "REBALANCE",
        "subject": "TT Portfolio above its band",
        "recommendation": "Sell part of TT when the zone is normal.",
        "rationale": "Weight is above target plus band; the zone currently holds rebalances back.",
        "evidence": [{"fact_id": "strategy.tt-portfolio.weight", "value": "23.80"}],
        "confidence": "MEDIUM",
        "rule_ref": "targets and bands, policy version 1",
        "would_change_if": "Drawdown returns under 10%.",
        "review_on": "2026-11-24",
        "model": "analyst-seat-test",
    }
    row.update(over)
    return row


def _log(tmp_path: Path) -> Path:
    return tmp_path / "log.jsonl"


def test_repo_log_when_present_verifies_and_is_lf_and_when_absent_reads_empty() -> None:
    # The real log is retained outside Git (the boundary forbids .jsonl); a missing file is an empty log.
    if rl.DEFAULT_LOG.exists():
        assert b"\r" not in rl.DEFAULT_LOG.read_bytes()
        rl.load_log(rl.DEFAULT_LOG)
    else:
        assert rl.load_log(rl.DEFAULT_LOG) == []


def test_append_builds_a_verifiable_chain(tmp_path: Path) -> None:
    path = _log(tmp_path)
    first = rl.append_row(_rec(), path)
    assert (
        first["row_id"] == 1
        and first["rec_id"] == "R-0001"
        and first["prev_sha256"] == rl.GENESIS
    )
    second = rl.append_row(
        {
            "kind": "OWNER_RESPONSE",
            "rec_id": "R-0001",
            "recorded_on": "2026-11-18",
            "response": "ACCEPT",
            "date": "2026-11-18",
        },
        path,
    )
    assert second["prev_sha256"] == rl.line_sha256(
        path.read_bytes().splitlines()[0] + b"\n"
    )
    assert len(rl.load_log(path)) == 2
    assert (
        rl.append_row(
            _rec(date="2026-11-24", recorded_on="2026-11-24", review_on="2026-12-01"),
            path,
        )["rec_id"]
        == "R-0002"
    )


def test_editing_removing_or_reordering_a_line_breaks_the_chain(tmp_path: Path) -> None:
    path = _log(tmp_path)
    rl.append_row(_rec(), path)
    rl.append_row(
        {
            "kind": "OWNER_RESPONSE",
            "rec_id": "R-0001",
            "recorded_on": "2026-11-18",
            "response": "REJECT",
            "date": "2026-11-18",
            "reason": "x",
        },
        path,
    )
    rl.append_row(
        {
            "kind": "STATUS_CHANGE",
            "rec_id": "R-0001",
            "recorded_on": "2026-11-19",
            "status": "CLOSED",
            "date": "2026-11-19",
        },
        path,
    )
    original = path.read_bytes()
    lines = original.splitlines(keepends=True)
    path.write_bytes(original.replace(b'"REJECT"', b'"ACCEPT"'))
    with pytest.raises(rl.LogError, match="chain is broken"):
        rl.load_log(path)
    path.write_bytes(lines[0] + lines[2])
    with pytest.raises(rl.LogError):
        rl.load_log(path)
    path.write_bytes(lines[0] + lines[2] + lines[1])
    with pytest.raises(rl.LogError):
        rl.load_log(path)
    path.write_bytes(original)
    assert len(rl.load_log(path)) == 3


def test_a_bad_append_leaves_the_file_unchanged(tmp_path: Path) -> None:
    path = _log(tmp_path)
    rl.append_row(_rec(), path)
    before = path.read_bytes()
    with pytest.raises(rl.LogError):
        rl.append_row(
            {
                "kind": "OWNER_RESPONSE",
                "rec_id": "R-0009",
                "recorded_on": "2026-11-18",
                "response": "ACCEPT",
                "date": "2026-11-18",
            },
            path,
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (_rec(evidence=[]), "non-empty|short|minItems"),
        (_rec(confidence="CERTAIN"), "CERTAIN"),
        (_rec(pack_id="pack-1"), "does not match"),
        (_rec(type="TRADE"), "TRADE"),
        (_rec(review_on="2026-11-10"), "review_on"),
    ],
)
def test_malformed_recommendations_are_refused(
    tmp_path: Path, row: dict, message: str
) -> None:
    with pytest.raises(rl.LogError, match=message):
        rl.append_row(row, _log(tmp_path))


def test_no_action_needs_no_evidence_other_types_do(tmp_path: Path) -> None:
    path = _log(tmp_path)
    rl.append_row(_rec(type="NO_ACTION", evidence=[]), path)
    with pytest.raises(rl.LogError):
        rl.append_row(_rec(evidence=[]), path)


def test_fields_from_another_kind_and_orphans_are_refused(tmp_path: Path) -> None:
    path = _log(tmp_path)
    rl.append_row(_rec(), path)
    with pytest.raises(rl.LogError, match="do not belong"):
        rl.append_row(
            {
                "kind": "OWNER_RESPONSE",
                "rec_id": "R-0001",
                "recorded_on": "2026-11-18",
                "response": "ACCEPT",
                "date": "2026-11-18",
                "recommendation": "rewrite",
            },
            path,
        )
    with pytest.raises(rl.LogError, match="no RECOMMENDATION"):
        rl.append_row(
            {
                "kind": "EXECUTION",
                "rec_id": "R-0007",
                "recorded_on": "2026-11-18",
                "executed": True,
                "date": "2026-11-18",
            },
            path,
        )
    with pytest.raises(rl.LogError, match="already recommended|expected"):
        rl.append_row(_rec(rec_id="R-0001"), path)
    with pytest.raises(rl.LogError, match="recorded before|goes backwards"):
        rl.append_row(
            {
                "kind": "OWNER_RESPONSE",
                "rec_id": "R-0001",
                "recorded_on": "2026-11-16",
                "response": "ACCEPT",
                "date": "2026-11-16",
            },
            path,
        )


def test_crlf_and_missing_newline_are_refused(tmp_path: Path) -> None:
    path = _log(tmp_path)
    rl.append_row(_rec(), path)
    good = path.read_bytes()
    path.write_bytes(good.replace(b"\n", b"\r\n"))
    with pytest.raises(rl.LogError, match="carriage"):
        rl.load_log(path)
    path.write_bytes(good[:-1])
    with pytest.raises(rl.LogError, match="newline"):
        rl.load_log(path)


def test_fold_tracks_response_execution_checks_and_overdue(tmp_path: Path) -> None:
    path = _log(tmp_path)
    rl.append_row(_rec(), path)
    rl.append_row(
        {
            "kind": "OWNER_RESPONSE",
            "rec_id": "R-0001",
            "recorded_on": "2026-11-18",
            "response": "MODIFY",
            "date": "2026-11-18",
            "modified_text": "half the size",
        },
        path,
    )
    rl.append_row(
        {
            "kind": "EXECUTION",
            "rec_id": "R-0001",
            "recorded_on": "2026-11-19",
            "executed": True,
            "date": "2026-11-19",
            "what": "sold TT",
            "amount_usd": "8500",
        },
        path,
    )
    rl.append_row(
        {
            "kind": "OUTCOME_CHECK",
            "rec_id": "R-0001",
            "recorded_on": "2026-12-15",
            "horizon_weeks": 4,
            "date": "2026-12-15",
            "result": "gap closed",
        },
        path,
    )
    record = rl.fold(rl.load_log(path), today="2027-03-01")[0]
    assert (
        record["owner_response"]["response"] == "MODIFY"
        and record["execution"]["amount_usd"] == "8500"
    )
    assert [d["done"] for d in record["outcome_due"]] == [True, False, False]
    assert [d["due"] for d in record["outcome_due"]] == [
        "2026-12-15",
        "2027-02-16",
        "2027-05-18",
    ]
    assert (
        record["overdue_checks"] == [13]
        and record["review_overdue"] is True
        and record["status"] == "OPEN"
    )
    assert record["policy_version"] == "1" and record["pack_id"] == "PACK-0123456789ab"


def test_panel_block_states(tmp_path: Path) -> None:
    path = _log(tmp_path)
    assert rl.panel_block(path)["status"] == "EMPTY"
    rl.append_row(_rec(), path)
    block = rl.panel_block(path, today="2026-11-20")
    assert (
        block["status"] == "VERIFIED"
        and block["rows"] == 1
        and len(block["records"]) == 1
    )
    rl.append_row(
        {
            "kind": "OWNER_RESPONSE",
            "rec_id": "R-0001",
            "recorded_on": "2026-11-18",
            "response": "ACCEPT",
            "date": "2026-11-18",
        },
        path,
    )
    path.write_bytes(path.read_bytes().replace(b"REBALANCE", b"LIMIT_ACTION"))
    broken = rl.panel_block(path)
    assert (
        broken["status"] == "LOG_BROKEN"
        and broken["records"] == []
        and "chain" in broken["reason"]
    )


def test_the_last_line_is_pinned_by_the_head_digest(tmp_path: Path) -> None:
    path = _log(tmp_path)
    rl.append_row(_rec(), path)
    head = rl.panel_block(path)["head_sha256"]
    rl.load_log(path, expected_head=head)
    path.write_bytes(path.read_bytes().replace(b"MEDIUM", b"HIGH"))
    rl.load_log(path)  # the chain alone cannot see an edit to the last line
    with pytest.raises(rl.LogError, match="head"):
        rl.load_log(path, expected_head=head)
    assert rl.panel_block(path, expected_head=head)["status"] == "LOG_BROKEN"


def test_cli_verify_and_append(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _log(tmp_path)
    row_file = tmp_path / "row.json"
    row_file.write_text(json.dumps(_rec()), encoding="utf-8")
    assert rl.main(["append", "--row-json", str(row_file), "--log", str(path)]) == 0
    assert rl.main(["verify", "--log", str(path)]) == 0
    assert "1 rows verified" in capsys.readouterr().out
    head = rl.panel_block(path)["head_sha256"]
    assert rl.main(["verify", "--log", str(path), "--head", head]) == 0
    path.write_bytes(path.read_bytes().replace(b"MEDIUM", b"HIGH"))
    assert rl.main(["verify", "--log", str(path), "--head", head]) == 1
