"""R1: owner-excluded oracle rows are no finding and the findings contract stays five classes.

The findings document is a cross-lane contract (``publication.py`` pins five classes and
refuses a sixth).  The oracle's not-derivable performance rows for an account the owner
excluded (``EXCLUDED_DEPRECATED_HISTORY_ONLY`` or ``EXCLUDED_IMMATERIAL``) therefore
emit no finding; ``validate_generation`` records them in the details file under
``expected_exclusions``.  Any other disposition, a capital row, and a row of an active
or unknown account stay NOT_READY.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from test_validate_generation import (
    CUTOFF,
    REPORT_GEN,
    Spec,
    _policy_fixture,
    build,
)

from acceptance.validate import checks
from acceptance.validate.generation import load_generation
from quant_engine.core.publication import evaluate_release_validation_findings

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

import check_release_validation_findings  # noqa: E402
import validate_generation  # noqa: E402

EXCLUDED = "X1"  # EXCLUDED_DEPRECATED_HISTORY_ONLY in the crosswalk fixture
PERFORMANCE = ("twr.account", "mwr.account", "flows.account.net", "flows.account.count")


def _row(metric: str, entity: str) -> dict[str, Any]:
    return {
        "metric_id": metric,
        "entity": entity,
        "window": "ITD",
        "reason": "account has no FUNDED_MEMBERSHIP interval",
    }


def _context(
    gen_dir: Path, rows: list[dict[str, Any]], discover: bool = True
) -> checks.Context:
    return checks.Context(
        gen=load_generation(gen_dir, discover=discover),
        cutoff=checks.to_date(CUTOFF) or checks.date.today(),
        cutoff_source="test",
        expected=[],
        expected_not_derivable=rows,
        repo_root=_policy_fixture(gen_dir.parent / "test-authority"),
    )


def _not_derivable(findings: list[checks.Finding]) -> list[tuple[str, str]]:
    return sorted(
        (f.entity, f.klass) for f in findings if f.check_id == "expected.not_derivable"
    )


def _add_crosswalk_row(**row: Any):
    def hook(gen_dir: Path) -> None:
        path = (
            gen_dir.parents[2]
            / "reporting"
            / "generations"
            / REPORT_GEN
            / "input"
            / "account-strategy-current-epoch-crosswalk.json"
        )
        document = json.loads(path.read_bytes())
        document["rows"].append(
            {
                "strategy_id": "delta",
                "effective_start": "2025-01-01",
                "effective_end": "2025-11-24",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                **row,
            }
        )
        path.write_bytes((json.dumps(document) + "\n").encode())

    return hook


def test_there_are_five_finding_classes() -> None:
    assert not hasattr(checks, "EXPECTED_EXCLUDED")
    assert checks.CLASSES == (
        "BLOCKER_INVARIANT",
        "BLOCKER_AUTHORITY",
        "BLOCKER_REGRESSION",
        "NOT_READY",
        "WARNING",
    )


def test_owner_excluded_performance_rows_emit_no_finding(tmp_path: Path) -> None:
    gen_dir = build(tmp_path)
    context = _context(gen_dir, [_row(m, EXCLUDED) for m in PERFORMANCE])
    assert _not_derivable(checks.run_checks(context)) == []
    exclusions = checks.expected_exclusions(context)
    assert sorted((r["metric_id"], r["entity"]) for r in exclusions) == sorted(
        (m, EXCLUDED) for m in PERFORMANCE
    )
    assert {r["disposition"] for r in exclusions} == {
        "EXCLUDED_DEPRECATED_HISTORY_ONLY"
    }
    assert all(r["reason"] for r in exclusions)


def test_capital_and_active_and_unknown_rows_stay_not_ready(tmp_path: Path) -> None:
    gen_dir = build(tmp_path)
    rows = [
        _row("capital.account.cutoff", EXCLUDED),
        _row("date.source_last_observation", EXCLUDED),
        _row("twr.account", "A1"),
        _row("twr.account", "Q7"),
    ]
    context = _context(gen_dir, rows)
    assert _not_derivable(checks.run_checks(context)) == sorted(
        [(f"{r['metric_id']}:{r['entity']}", checks.NOT_READY) for r in rows]
    )
    assert checks.expected_exclusions(context) == []


def test_an_out_of_vocabulary_exclusion_is_not_an_owner_exclusion(
    tmp_path: Path,
) -> None:
    hook = _add_crosswalk_row(
        account_id="P7", product_epoch_disposition="EXCLUDED_PROVISIONAL"
    )
    gen_dir = build(tmp_path, Spec(post_write=[hook]))
    context = _context(gen_dir, [_row("twr.account", "P7")])
    assert _not_derivable(checks.run_checks(context)) == [
        ("twr.account:P7", checks.NOT_READY)
    ]
    assert checks.expected_exclusions(context) == []


def test_the_immaterial_disposition_is_pinned_too(tmp_path: Path) -> None:
    hook = _add_crosswalk_row(
        account_id="M3", product_epoch_disposition="EXCLUDED_IMMATERIAL"
    )
    gen_dir = build(tmp_path, Spec(post_write=[hook]))
    context = _context(gen_dir, [_row("mwr.account", "M3")])
    assert _not_derivable(checks.run_checks(context)) == []
    assert [r["disposition"] for r in checks.expected_exclusions(context)] == [
        "EXCLUDED_IMMATERIAL"
    ]


def test_the_details_file_carries_the_exclusions_and_the_document_does_not(
    tmp_path: Path,
) -> None:
    gen_dir = build(tmp_path)
    baseline = tmp_path / "expected.json"
    baseline.write_text(
        json.dumps(
            {
                "cutoff": CUTOFF,
                "metrics": [],
                "not_derivable": [_row("twr.account", EXCLUDED)],
            }
        )
    )
    out, details = tmp_path / "findings.json", tmp_path / "details.json"
    validate_generation.main(
        [str(gen_dir), "--expected", str(baseline), "--cutoff", CUTOFF]
        + ["--out", str(out), "--details-out", str(details)]
    )
    document = json.loads(out.read_text())
    assert "expected_exclusions" not in document
    assert not [
        f for f in document["findings"] if f["check_id"] == "expected.not_derivable"
    ]
    exclusions = json.loads(details.read_text())["expected_exclusions"]
    assert [(r["metric_id"], r["entity"], r["disposition"]) for r in exclusions] == [
        ("twr.account", EXCLUDED, "EXCLUDED_DEPRECATED_HISTORY_ONLY")
    ]


def test_a_real_shaped_findings_document_passes_the_release_gate(
    tmp_path: Path, capsys: Any
) -> None:
    gen_dir = build(tmp_path)
    rows = [_row(m, EXCLUDED) for m in PERFORMANCE] + [
        _row("capital.reserve.cutoff", "reserve-capital")
    ]
    context = _context(gen_dir, rows)
    findings = checks.run_checks(context)
    document = checks.findings_document(
        generation_id=context.gen.generation_id,
        generation_dir=str(gen_dir),
        validator_commit="test",
        expected_baseline_sha256="a" * 64,
        findings=findings,
    )
    assert set(document["summary"]) == {
        "BLOCKER_INVARIANT",
        "BLOCKER_AUTHORITY",
        "BLOCKER_REGRESSION",
        "NOT_READY",
        "WARNING",
    }
    path = tmp_path / "validation" / "findings.json"
    path.parent.mkdir()
    path.write_text(json.dumps(document))
    may_publish, blocking, reason = evaluate_release_validation_findings(
        path, expected_generation_id=context.gen.generation_id
    )
    assert (may_publish, blocking, reason) == (True, [], None)
    code = check_release_validation_findings.main(
        ["--generation-id", context.gen.generation_id, "--findings", str(path)]
    )
    assert code == 0
