from __future__ import annotations

import ast
from datetime import date
import json
from pathlib import Path
import shutil

import pytest

import quant_engine.ibkr.capture.verify as verify_module
from ibkr_capture_fixtures import (
    ACCOUNT_ONE,
    ACCOUNT_TWO,
    Harness,
    activity_xml,
    make_plan,
    security_xml,
    send_ok,
    weekday_range,
)
from quant_engine.ibkr.capture import (
    NormalizationError,
    StagingError,
    VerificationError,
    normalize_run,
    verify_run,
)
from quant_engine.ibkr.capture._util import account_label

START, END = date(2026, 10, 5), date(2026, 10, 9)
DAYS = weekday_range(START, END)


def _staged(tmp_path: Path, body: bytes, **plan_args: object) -> tuple[Path, Harness]:
    plan = make_plan(**plan_args)  # type: ignore[arg-type]
    harness = Harness(tmp_path, [send_ok(), body], plan=plan)
    (summary,) = harness.run()
    assert summary.status == "STAGED"
    (run_dir,) = harness.run_dirs()
    return run_dir, harness


def test_normalize_then_verify_round_trip(tmp_path: Path) -> None:
    body = activity_xml((ACCOUNT_ONE, ACCOUNT_TWO), DAYS)
    run_dir, _ = _staged(tmp_path, body)
    output = tmp_path / "normalized.json"
    document = normalize_run(run_dir, output)
    assert document["coverage"]["status"] == "COMPLETE"
    assert document["coverage"]["statement_count"] == 10
    assert document["coverage"]["first_date"] == "2026-10-05"
    first = document["statements"][0]
    assert first["nav"] == "1000.50" and first["counts"]["positions"] == 2
    report = verify_run(run_dir, output)
    assert report.status == "VERIFIED" and report.statement_count == 10
    assert report.checks == (
        "manifest",
        "file-hashes",
        "plan",
        "attempts",
        "recomputed",
    )


def test_normalized_output_is_masked_and_restricted(tmp_path: Path) -> None:
    run_dir, _ = _staged(tmp_path, activity_xml((ACCOUNT_ONE,), DAYS))
    output = tmp_path / "normalized.json"
    normalize_run(run_dir, output)
    text = output.read_text(encoding="utf-8")
    assert ACCOUNT_ONE not in text
    assert account_label(ACCOUNT_ONE) in text
    assert "1.25" not in text  # no cash amounts
    assert "exec-" not in text  # no execution detail


def test_normalized_output_is_create_only(tmp_path: Path) -> None:
    run_dir, _ = _staged(tmp_path, activity_xml((ACCOUNT_ONE,), DAYS))
    output = tmp_path / "normalized.json"
    normalize_run(run_dir, output)
    with pytest.raises(StagingError):
        normalize_run(run_dir, output)


def test_missing_weekdays_are_reported_and_known_holidays_are_not(
    tmp_path: Path,
) -> None:
    short = [d for d in DAYS if d != date(2026, 10, 7)]
    run_dir, _ = _staged(tmp_path, activity_xml((ACCOUNT_ONE,), short))
    document = normalize_run(run_dir, tmp_path / "a.json")
    label = account_label(ACCOUNT_ONE)
    assert document["coverage"]["status"] == "GAPS_PRESENT"
    assert document["coverage"]["weekday_gaps"] == {label: ["2026-10-07"]}
    assert verify_run(run_dir, tmp_path / "a.json").coverage_status == "GAPS_PRESENT"
    sub = tmp_path / "second"
    sub.mkdir()
    run_dir2, _ = _staged(
        sub, activity_xml((ACCOUNT_ONE,), short), non_trading=(date(2026, 10, 7),)
    )
    assert normalize_run(run_dir2, sub / "b.json")["coverage"]["status"] == "COMPLETE"


def test_an_expected_account_with_no_statement_is_a_gap(tmp_path: Path) -> None:
    labels = tuple(sorted({account_label(ACCOUNT_ONE), account_label(ACCOUNT_TWO)}))
    run_dir, _ = _staged(tmp_path, activity_xml((ACCOUNT_ONE,), DAYS), labels=labels)
    document = normalize_run(run_dir, tmp_path / "n.json")
    assert list(document["coverage"]["weekday_gaps"]) == [account_label(ACCOUNT_TWO)]


def test_unexpected_account_scope_is_refused(tmp_path: Path) -> None:
    only_one = (account_label(ACCOUNT_ONE),)
    run_dir, _ = _staged(
        tmp_path, activity_xml((ACCOUNT_ONE, ACCOUNT_TWO), DAYS), labels=only_one
    )
    with pytest.raises(NormalizationError, match="unexpected account scope"):
        normalize_run(run_dir, tmp_path / "n.json")


def test_statements_outside_the_window_are_refused(tmp_path: Path) -> None:
    run_dir, _ = _staged(
        tmp_path,
        activity_xml(
            (ACCOUNT_ONE,), weekday_range(date(2026, 10, 5), date(2026, 10, 12))
        ),
    )
    with pytest.raises(NormalizationError, match="outside the requested window"):
        normalize_run(run_dir, tmp_path / "n.json")


def test_duplicate_account_days_and_bad_shapes_are_refused(tmp_path: Path) -> None:
    run_dir, _ = _staged(tmp_path, activity_xml((ACCOUNT_ONE,), DAYS + DAYS[:1]))
    with pytest.raises(NormalizationError, match="more than once"):
        normalize_run(run_dir, tmp_path / "n.json")
    sub = tmp_path / "s"
    sub.mkdir()
    broken = activity_xml((ACCOUNT_ONE,), DAYS).replace(b"<Transfers />", b"", 1)
    run_dir2, _ = _staged(sub, broken)
    with pytest.raises(NormalizationError, match="sections missing"):
        normalize_run(run_dir2, sub / "n.json")


def test_refused_or_modified_runs_cannot_be_normalized(tmp_path: Path) -> None:
    run_dir, _ = _staged(tmp_path, activity_xml((ACCOUNT_ONE,), DAYS))
    (run_dir / "statement.xml").write_bytes(b"<tampered/>")
    with pytest.raises(NormalizationError, match="manifest hash"):
        normalize_run(run_dir, tmp_path / "n.json")
    (run_dir / "extra.txt").write_text("x")
    with pytest.raises(NormalizationError, match="differs from its manifest"):
        normalize_run(run_dir, tmp_path / "m.json")


def test_verifier_detects_tampering_everywhere(tmp_path: Path) -> None:
    run_dir, _ = _staged(tmp_path, activity_xml((ACCOUNT_ONE,), DAYS))
    output = tmp_path / "normalized.json"
    normalize_run(run_dir, output)
    verify_run(run_dir, output)

    changed = json.loads(output.read_text())
    changed["statements"][0]["nav"] = "1.00"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(VerificationError) as differs:
        verify_run(run_dir, bad)
    assert differs.value.reason == "normalized-differs"

    with pytest.raises(VerificationError) as missing:
        verify_run(run_dir, tmp_path / "absent.json")
    assert missing.value.reason == "normalized-missing"

    copy = tmp_path / "copy"
    shutil.copytree(run_dir, copy)
    (copy / "statement.xml").write_bytes(b"<tampered/>")
    with pytest.raises(VerificationError) as hashed:
        verify_run(copy, output)
    assert hashed.value.reason == "file-hash"

    other = tmp_path / "other"
    shutil.copytree(run_dir, other)
    (other / "extra.txt").write_text("x")
    with pytest.raises(VerificationError) as extra:
        verify_run(other, output)
    assert extra.value.reason == "file-set"

    third = tmp_path / "third"
    shutil.copytree(run_dir, third)
    manifest = json.loads((third / "capture-manifest.json").read_text())
    manifest["status"] = "REFUSED"
    (third / "capture-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(VerificationError) as status:
        verify_run(third, output)
    assert status.value.reason == "manifest-not-staged"


def test_verifier_catches_a_normalizer_that_miscounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quant_engine.ibkr.capture.normalize as normalizer

    run_dir, _ = _staged(tmp_path, activity_xml((ACCOUNT_ONE,), DAYS))
    original = normalizer._activity_statements

    def skewed(path: Path, artifact_id: str) -> list[dict[str, object]]:
        rows = original(path, artifact_id)
        rows[0]["counts"]["trades"] += 1  # type: ignore[index,operator]
        return rows

    monkeypatch.setattr(normalizer, "_activity_statements", skewed)
    output = tmp_path / "skewed.json"
    normalize_run(run_dir, output)
    with pytest.raises(VerificationError) as error:
        verify_run(run_dir, output)
    assert error.value.reason == "normalized-differs"


def test_verifier_shares_no_parser_with_the_normalizer() -> None:
    tree = ast.parse(Path(verify_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(
                (node.module or "") + "|" + ",".join(a.name for a in node.names)
            )
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    flat = " ".join(sorted(imported))
    assert "normalize" not in flat
    assert "securities" not in flat
    assert "flex" not in flat


def test_security_reference_run_normalizes_symbols_and_missing_rows(
    tmp_path: Path,
) -> None:
    plan = make_plan(query="security-reference")
    body = security_xml((ACCOUNT_ONE,), START, END)
    harness = Harness(tmp_path, [send_ok(), body], plan=plan)
    (summary,) = harness.run()
    assert summary.status == "STAGED"
    (run_dir,) = harness.run_dirs()
    output = tmp_path / "sec.json"
    document = normalize_run(run_dir, output)
    assert document["kind"] == "security-reference"
    keys = {row["key"]: row for row in document["securities"]}
    assert len(keys) == 2
    missing = [r for r in document["securities"] if not r["symbols"]]
    assert len(missing) == 1 and missing[0]["rows_missing_symbol"] == 2
    assert verify_run(run_dir, output).status == "VERIFIED"
