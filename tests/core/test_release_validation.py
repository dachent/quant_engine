from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "release_validation_tool", ROOT / "tools" / "release_validation.py"
)
assert _SPEC is not None and _SPEC.loader is not None
rv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rv)

RID = "e" * 64
ACCT = "account-0123456789abcdef"


def _run_root(tmp: Path) -> Path:
    run = tmp / "run"
    panel = run / "stages" / "panel" / "generations" / "pg"
    (panel / "data").mkdir(parents=True, exist_ok=True)
    (panel / "data" / "dashboard_data.json").write_text(
        json.dumps(
            {
                "strategies": [{"id": ACCT}],
                "capital": {"series": list(range(100))},
                "secret_block": 1,
            }
        ),
        encoding="utf-8",
    )
    report = run / "stages" / "report" / "generations" / "rg"
    report.mkdir(parents=True, exist_ok=True)
    (report / "portfolio-report-data.json").write_text(
        json.dumps({"twr_mwr": {"x": 1}}), encoding="utf-8"
    )
    for stage, gid in (("panel", "pg"), ("report", "rg")):
        (run / "stages" / stage / "CURRENT.json").write_text(
            json.dumps({"generation_id": gid}), encoding="utf-8"
        )
    return run


def _checklist(tmp: Path) -> Path:
    path = tmp / "CHECKLIST.md"
    path.write_text("check", encoding="utf-8")
    return path


def _packet(tmp: Path, **kw: object) -> dict[str, object]:
    return rv.build_packet(
        run_root=_run_root(tmp), out=tmp / "packet", release_id=RID, code_commit="c" * 40,
        cutoff="2026-09-17", checklist=_checklist(tmp), **kw,
    )  # fmt: skip


def test_packet_masks_accounts_shrinks_series_and_takes_only_listed_sections(
    tmp_path: Path,
) -> None:
    info = _packet(tmp_path)
    packet = tmp_path / "packet"
    assert info["panel_generation"] == "pg"
    assert "acct-89abcdef" in (packet / "panel_strategies.json").read_text()
    assert ACCT not in "".join(p.read_text() for p in packet.rglob("*") if p.is_file())
    assert "_series_len" in (packet / "panel_capital.json").read_text()
    assert not (packet / "panel_secret_block.json").exists()
    assert RID in (packet / "PACKET.md").read_text()


def test_context_files_that_look_like_ledgers_are_refused(tmp_path: Path) -> None:
    bad = tmp_path / "provider-ledger.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(rv.ValidationError, match="context file refused"):
        _packet(tmp_path, context=[bad])
    odd = tmp_path / "data.csv"
    odd.write_text("a", encoding="utf-8")
    with pytest.raises(rv.ValidationError, match="context file refused"):
        _packet(tmp_path, context=[odd])


def test_seat_dirs_must_be_fresh(tmp_path: Path) -> None:
    _packet(tmp_path)
    seats = {"kimi": {"argv": ["x"]}}
    rv.make_seats(tmp_path / "packet", tmp_path / "seats", seats, RID)
    assert (tmp_path / "seats" / "seat-kimi" / "packet" / "PACKET.md").is_file()
    assert RID in (tmp_path / "seats" / "seat-kimi" / "PROMPT.md").read_text()
    with pytest.raises(rv.ValidationError, match="not fresh"):
        rv.make_seats(tmp_path / "packet", tmp_path / "seats", seats, RID)


def test_run_waits_for_prepared_then_runs_seats_and_reads_verdicts(
    tmp_path: Path,
) -> None:
    prepared = tmp_path / "prepared.json"
    prepared.write_text(
        json.dumps({"release_id": RID, "spec_sha256": "f" * 64}), encoding="utf-8"
    )
    writer = "import sys,pathlib;p=pathlib.Path('out/review.md');p.write_text('x\\n'+sys.argv[1]+'\\n')"
    config = tmp_path / "seats.json"
    config.write_text(
        json.dumps(
            {
                "good": {
                    "argv": [sys.executable, "-c", writer, f"VERDICT: PASS {RID}"]
                },
                "nolast": {"argv": [sys.executable, "-c", writer, "no verdict"]},
                "mine": {"mode": "manual"},
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        prepared_file=prepared, run_root=_run_root(tmp_path), seats_root=tmp_path / "seats", checklist=_checklist(tmp_path),
        code_commit="c" * 40, cutoff="2026-09-17", findings_details=None, context=[], seats_config=config,
        wait_timeout=5, poll=1, seat_timeout=60,
    )  # fmt: skip
    summary = rv.run(args)
    by = {row["seat"]: row for row in summary["seats"]}
    assert by["good"]["verdict"].startswith("VERDICT: PASS")
    assert by["nolast"]["verdict"] == "NO_VERDICT_LINE"
    assert by["mine"]["verdict"] == "PENDING_MANUAL"
    assert (tmp_path / "seats" / "validation-summary.json").is_file()


def test_run_times_out_when_the_candidate_is_never_prepared(tmp_path: Path) -> None:
    with pytest.raises(rv.ValidationError, match="not PREPARED"):
        rv.wait_prepared(tmp_path / "missing.json", timeout=0, poll=1)


def test_prepared_file_with_other_fields_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "prepared.json"
    path.write_text(json.dumps({"release_id": RID}), encoding="utf-8")
    with pytest.raises(rv.ValidationError, match="unexpected fields"):
        rv.wait_prepared(path, timeout=1, poll=1)
