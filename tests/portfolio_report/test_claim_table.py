"""Tests of the claim table, its runner and its sentence scanner.

Synthetic payloads only; no real data.  An env-gated test runs the table over a
real panel and report candidate (QE_CLAIM_TABLE_PANEL_DIR and
QE_CLAIM_TABLE_REPORT_DIR); it is skipped when they are unset.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

import claim_table
from claim_table import ROWS, Payload, Row, Verdict
import claim_table_fixtures
import claim_table_runner
import claim_table_scan

REPO = Path(__file__).resolve().parents[2]
CHECKABLE = [row for row in ROWS if not row.unverifiable]


def _prepared(row: Row) -> Payload:
    payload = copy.deepcopy(claim_table_fixtures.base_payload())
    if row.pass_fixture is not None:
        row.pass_fixture(payload)
    return payload


@pytest.mark.parametrize("row", CHECKABLE, ids=lambda r: r.id)
def test_row_passes_on_the_synthetic_payload(row: Row) -> None:
    verdict, evidence = claim_table.evaluate(row, _prepared(row))
    assert verdict is Verdict.PASS, evidence


@pytest.mark.parametrize("row", CHECKABLE, ids=lambda r: r.id)
def test_row_fails_when_the_claim_is_false(row: Row) -> None:
    assert row.break_fixture is not None, f"{row.id} has no break_fixture"
    payload = _prepared(row)
    row.break_fixture(payload)
    verdict, evidence = claim_table.evaluate(row, payload)
    assert verdict is Verdict.FAIL, evidence


@pytest.mark.parametrize("row", [r for r in ROWS if r.unverifiable], ids=lambda r: r.id)
def test_unverifiable_rows_skip_with_a_reason(row: Row) -> None:
    verdict, evidence = claim_table.evaluate(row, claim_table_fixtures.base_payload())
    assert verdict is Verdict.SKIP and "UNVERIFIABLE" in evidence


def _u02() -> Row:
    return next(row for row in ROWS if row.id == "U02")


def test_u02_skips_without_a_bundle_and_runs_with_one() -> None:
    row = _u02()
    verdict, evidence = claim_table.evaluate(row, claim_table_fixtures.base_payload())
    assert (
        verdict is Verdict.SKIP
        and "UNVERIFIABLE" in evidence
        and "--bundle" in evidence
    )
    verdict, evidence = claim_table.evaluate(row, _prepared(row))
    assert verdict is Verdict.PASS and "1 panel/ artifacts" in evidence


@pytest.mark.parametrize("tamper", ["status", "bytes", "missing", "no_panel"])
def test_u02_fails_on_a_tampered_or_absent_bundle(tamper: str) -> None:
    payload = _prepared(_u02())
    bundle = payload.bundle
    assert bundle is not None
    if tamper == "status":
        bundle["verification"] = "PENDING"
    elif tamper == "bytes":
        bundle["on_disk"]["panel/index.html"]["size_bytes"] = 6
    elif tamper == "missing":
        bundle["on_disk"] = {}
    else:
        bundle["artifacts"] = bundle["artifacts"][1:]
    verdict, evidence = claim_table.evaluate(_u02(), payload)
    assert verdict is Verdict.FAIL, evidence


def test_load_bundle_hashes_the_stored_panel_files(tmp_path: Path) -> None:
    import hashlib

    (tmp_path / "panel").mkdir()
    data = b"<html>panel</html>"
    (tmp_path / "panel" / "index.html").write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    manifest = {
        "verification": "VERIFIED",
        "artifacts": [
            {"artifact_id": "panel/index.html", "sha256": sha, "size_bytes": len(data)},
            {"artifact_id": "panel/gone.js", "sha256": sha, "size_bytes": 1},
            {"artifact_id": "evidence/x.json", "sha256": sha, "size_bytes": 1},
        ],
    }
    import json

    (tmp_path / "generation.json").write_bytes(json.dumps(manifest).encode())
    bundle = claim_table_runner.load_bundle(tmp_path)
    assert set(bundle["on_disk"]) == {"panel/index.html"}
    payload = Payload(panel={}, report={}, bundle=bundle)
    verdict, evidence = claim_table.evaluate(_u02(), payload)
    assert verdict is Verdict.FAIL and "panel/gone.js" in evidence
    (tmp_path / "panel" / "gone.js").write_bytes(b"x")
    manifest["artifacts"][1]["sha256"] = hashlib.sha256(b"x").hexdigest()
    (tmp_path / "generation.json").write_bytes(json.dumps(manifest).encode())
    payload = Payload(
        panel={}, report={}, bundle=claim_table_runner.load_bundle(tmp_path)
    )
    verdict, _ = claim_table.evaluate(_u02(), payload)
    assert verdict is Verdict.PASS


def test_a_predicate_that_raises_is_a_failure() -> None:
    verdict, evidence = claim_table.evaluate(CHECKABLE[0], Payload(panel={}, report={}))
    assert verdict in (Verdict.SKIP, Verdict.FAIL)
    boom = Row("X", "count", "v", "f", ("n",), "c", lambda p: 1 / 0, None)  # type: ignore[arg-type,return-value]
    verdict, evidence = claim_table.evaluate(boom, Payload(panel={}, report={}))
    assert verdict is Verdict.FAIL and "ZeroDivisionError" in evidence


def test_every_row_names_a_known_class_and_a_file() -> None:
    classes = {"universal", "count", "basis", "epoch", "coverage", "status"}
    for row in ROWS:
        assert row.klass in classes, row.id
        assert (REPO / row.file).is_file(), row.id
        assert row.needles and row.claim, row.id


def test_every_sentence_is_located_in_the_shipped_sources() -> None:
    # A reworded sentence leaves its row STALE: re-state the claim, not the needle.
    stale = []
    for row in ROWS:
        text = (REPO / row.file).read_bytes().decode("utf-8")
        if claim_table_runner.locate(row, text).missing:
            stale.append(row.id)
    assert not stale, f"sentence no longer found for rows {stale}"


def test_masks_ids_to_the_last_eight_hex_digits() -> None:
    masked = claim_table_runner.mask(
        "account-1b44c987969b9e47 and sha 0123456789abcdef0123456789abcdef"
    )
    assert "1b44c987969b9e47" not in masked and "969b9e47" in masked
    assert "89abcdef" in masked and "0123456789abcdef0123" not in masked
    assert claim_table_runner.mask("deadbeef") == "deadbeef"


# --- scanner and runner on synthetic sources --------------------------------

SYNTH_JS = """
// Every comment sentence is ignored by the scanner of the renderer text.
const note = "Every strategy has a window in this view.";
const quiet = "Nothing to see in this short sentence here.";
const dyn = `All ${rows.length} epochs are listed here with no filter.`;
throw new Error("Every field must be present for the renderer to continue.");
const tpl = `<p>Each epoch is complete.</p><p>The panel never fills a gap.</p>`;
"""
SYNTH_HTML = """<html><body><p>Only accepted accounts remain in this table of rows.</p>
<script>var s = "Every script string is ignored by the html scanner here.";</script></body></html>"""
SYNTH_PY = '''"""Every docstring sentence is skipped by the scanner of builders."""
MSG = "All covered strategies are listed in this builder sentence."
def f(x):
    raise ValueError("Every raised message is skipped by the scanner of builders.")
'''


def _sentences(file: str, source: str) -> list[str]:
    return [s.text for s in claim_table_scan.scan_text(file, source)]


def test_scanner_finds_claim_sentences_and_skips_the_rest() -> None:
    js = _sentences("a.js", SYNTH_JS)
    assert "Every strategy has a window in this view." in js
    assert "All {} epochs are listed here with no filter." in js
    assert "Each epoch is complete." not in js  # fewer than five words
    assert "The panel never fills a gap." in js
    assert not any("Every field must be present" in s for s in js)
    assert not any("comment sentence" in s for s in js)
    assert not any("short sentence" in s for s in js)  # no claim pattern
    html = _sentences("a.html", SYNTH_HTML)
    assert html == ["Only accepted accounts remain in this table of rows."]
    py = _sentences("a.py", SYNTH_PY)
    assert py == ["All covered strategies are listed in this builder sentence."]


def _repo(tmp_path: Path, js: str) -> Path:
    target = tmp_path / "apps" / "portfolio-control-panel"
    target.mkdir(parents=True)
    (target / "app.js").write_bytes(js.encode("utf-8"))
    return tmp_path


def _row(needle: str) -> Row:
    return Row(
        "R1",
        "universal",
        "v",
        claim_table.PANEL_JS,
        (needle,),
        "c",
        lambda p: claim_table.ok("fine"),
        None,
    )


def test_a_covered_sentence_is_not_uncovered_and_a_reworded_one_is_stale(
    tmp_path: Path,
) -> None:
    source = 'const a = "Every strategy has a window in this view.";\nconst b = "No row is hidden from the user in this table.";\n'
    repo = _repo(tmp_path, source)
    sentences = claim_table_scan.scan_text(
        "apps/portfolio-control-panel/app.js", source
    )
    located = [claim_table_runner.locate(_row("Every strategy has a window"), source)]
    left, unused = claim_table_runner.uncovered_sentences(sentences, located, [])
    assert [s.text for s in left] == ["No row is hidden from the user in this table."]
    assert unused == []
    # reword the covered sentence: the row no longer locates it
    reworded = source.replace("Every strategy", "Any strategy")
    outcome = claim_table_runner.run(
        Payload(panel={}, report={}),
        _repo(tmp_path / "r", reworded),
        [],
        rows=(_row("Every strategy has a window"),),
        scan=True,
    )
    assert outcome["results"][0][2] == "STALE"
    assert any("Any strategy" in s.text for s in outcome["uncovered"])
    assert repo.is_dir()


def test_allowlist_needs_a_justification_and_silences_a_sentence(
    tmp_path: Path,
) -> None:
    source = 'const b = "No row is hidden from the user in this table.";\n'
    sentences = claim_table_scan.scan_text(
        "apps/portfolio-control-panel/app.js", source
    )
    entry = {
        "file": "apps/portfolio-control-panel/app.js",
        "pattern": "^No row is hidden",
        "justification": "layout statement, not a payload claim",
    }
    left, unused = claim_table_runner.uncovered_sentences(sentences, [], [entry])
    assert left == [] and unused == []
    _left, unused = claim_table_runner.uncovered_sentences([], [], [entry])
    assert unused and "No row is hidden" in unused[0]
    bad = tmp_path / "allow.json"
    bad.write_bytes(
        b'{"entries": [{"file": "a", "pattern": "b", "justification": " "}]}'
    )
    with pytest.raises(SystemExit):
        claim_table_runner.load_allowlist(bad)


def test_shipped_allowlist_entries_all_have_justifications() -> None:
    entries = claim_table_runner.load_allowlist(claim_table_runner.DEFAULT_ALLOWLIST)
    assert all(entry["justification"].strip() for entry in entries)


def test_render_reports_failures_and_exit_condition(tmp_path: Path) -> None:
    source = 'const a = "Every strategy has a window in this view.";\n'
    repo = _repo(tmp_path, source)
    failing = Row(
        "F1",
        "universal",
        "v",
        claim_table.PANEL_JS,
        ("Every strategy has a window",),
        "c",
        lambda p: claim_table.bad("account 1b44c987969b9e47 is wrong"),
        None,
    )
    outcome = claim_table_runner.run(
        Payload(panel={}, report={}), repo, [], rows=(failing,), scan=True
    )
    text, failed, uncovered = claim_table_runner.render(outcome, 10)
    assert failed and uncovered == 0
    assert "FAIL  F1" in text and "…969b9e47" in text and "1b44c987969b9e47" not in text


# --- real payloads (env-gated) ----------------------------------------------

_PANEL = os.environ.get("QE_CLAIM_TABLE_PANEL_DIR")
_REPORT = os.environ.get("QE_CLAIM_TABLE_REPORT_DIR")


@pytest.mark.skipif(
    not (_PANEL and _REPORT), reason="QE_CLAIM_TABLE_PANEL_DIR/REPORT_DIR unset"
)
def test_claim_table_over_the_real_candidate_payloads() -> None:
    assert _PANEL and _REPORT
    payload = claim_table_runner.load_payload(Path(_PANEL), Path(_REPORT))
    allow = claim_table_runner.load_allowlist(claim_table_runner.DEFAULT_ALLOWLIST)
    outcome = claim_table_runner.run(payload, REPO, allow)
    text, failed, _uncovered = claim_table_runner.render(outcome, 400)
    assert not failed, text


def test_baseline_ratchet_flags_only_new_or_reworded_sentences(tmp_path: Path) -> None:
    old = 'const a = "No row is hidden from the user in this table.";\n'
    repo = _repo(tmp_path / "a", old)
    sentences = claim_table_scan.scan_repo(repo)
    path = tmp_path / "baseline.json"
    claim_table_runner.write_baseline(path, sentences)
    baseline = claim_table_runner.load_baseline(path)
    payload = Payload(panel={}, report={})
    same = claim_table_runner.run(payload, repo, [], rows=(), baseline=baseline)
    assert same["uncovered"] == [] and len(same["baselined"]) == 1
    reworded = old.replace("No row", "Any row")
    again = claim_table_runner.run(
        payload, _repo(tmp_path / "b", reworded), [], rows=(), baseline=baseline
    )
    assert [s.text for s in again["uncovered"]] == [
        "Any row is hidden from the user in this table."
    ]
    assert again["stale_baseline"] == 1
    text, failed, new = claim_table_runner.render(again, 10)
    assert new == 1 and "UNCOVERED apps/portfolio-control-panel/app.js" in text
    assert not failed


def test_shipped_baseline_has_no_sentence_with_a_row() -> None:
    # Debt is only sentences without a row: a sentence that gained a row must leave the baseline.
    baseline = claim_table_runner.load_baseline(claim_table_runner.DEFAULT_BASELINE)
    outcome = claim_table_runner.run(
        Payload(panel={}, report={}),
        REPO,
        claim_table_runner.load_allowlist(claim_table_runner.DEFAULT_ALLOWLIST),
        baseline=baseline,
    )
    assert outcome["uncovered"] == [], (
        "new claim sentence without a row or allowlist entry"
    )
    assert outcome["stale_baseline"] == 0, (
        "baseline entry for a sentence that is gone or covered"
    )
