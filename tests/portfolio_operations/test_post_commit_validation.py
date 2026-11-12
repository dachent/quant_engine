"""Post-commit validator re-run comparison (#896): kill tests for each refusal."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import post_commit_validation as pcv  # noqa: E402


def _doc(rows: list[tuple[str, str, str]], generation: str = "g" * 64) -> dict:
    return {
        "generation_id": generation,
        "findings": [{"check_id": c, "entity": e, "class": k} for c, e, k in rows],
    }


NR = ("completeness.required_outputs", "hwm_summary", "NOT_READY")


def test_equal_row_sets_pass() -> None:
    assert pcv.compare(_doc([NR]), _doc([NR])) == []


def test_a_blocker_that_only_exists_after_the_commit_is_refused() -> None:
    """The D8 case: pre-commit clean, post-commit BLOCKER_AUTHORITY."""

    blocker = ("identity.publication_state", "generation", "BLOCKER_AUTHORITY")
    problems = pcv.compare(_doc([NR]), _doc([NR, blocker]))
    assert any("post-commit blocker" in p for p in problems)
    assert any("appeared" in p for p in problems)


def test_a_row_that_vanishes_is_refused() -> None:
    assert any("vanished" in p for p in pcv.compare(_doc([NR]), _doc([])))


def test_a_different_generation_is_refused() -> None:
    assert pcv.compare(_doc([], "a" * 64), _doc([], "b" * 64))


def test_the_receipt_records_both_digests_and_the_exit_code(tmp_path: Path) -> None:
    pre, post = tmp_path / "pre.json", tmp_path / "post.json"
    pre.write_text(json.dumps(_doc([NR])), encoding="utf-8")
    post.write_text(json.dumps(_doc([NR])), encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    assert (
        pcv.main(["--pre", str(pre), "--post", str(post), "--receipt", str(receipt)])
        == 0
    )
    body = json.loads(receipt.read_bytes())
    assert body["status"] == "PASS" and len(body["pre_commit_findings_sha256"]) == 64
    post.write_text(json.dumps(_doc([])), encoding="utf-8")
    assert pcv.main(["--pre", str(pre), "--post", str(post)]) == 1


def test_a_malformed_document_fails_closed(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(pcv.PostCommitValidationError):
        pcv.check(bad, bad)
    assert pcv.main(["--pre", str(bad), "--post", str(bad)]) == 1
