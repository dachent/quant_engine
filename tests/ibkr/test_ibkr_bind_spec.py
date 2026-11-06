"""bind-spec with the preflight: a bound spec is one the generation builder accepts."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

from quant_engine.ibkr.capture import CaptureRequest, RequestPlan
from quant_engine.ibkr.capture._util import account_label
from quant_engine.ibkr.capture.bind import BindError, bind_spec
from quant_engine.ibkr.generation import build_generation

from test_ibkr_generation import _refreshed_spec, _xml


def _staged_run(root: Path, day: date, nav: str) -> Path:
    """A STAGED one-day run directory shaped like a capture run (the bytes are the generation test's)."""

    run_dir = root / f"flex-{day.isoformat()}"
    run_dir.mkdir()
    (run_dir / "statement.xml").write_text(
        _xml("SYNTH-RAW", day, nav), encoding="utf-8"
    )
    plan = RequestPlan(
        date(2026, 2, 1),
        (CaptureRequest("activity-history", day, day, True),),
        (account_label("SYNTH-RAW"),),
    )
    (run_dir / "capture-manifest.json").write_text(
        json.dumps(
            {
                "status": "STAGED",
                "run_id": run_dir.name,
                "plan": plan.to_document(),
                "request_index": 0,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def test_a_bound_spec_passes_the_generation_builders_own_checks(tmp_path: Path) -> None:
    base = _refreshed_spec(tmp_path)
    spec = json.loads(base.read_text(encoding="utf-8"))
    root = Path(spec["roots"]["legacy"])
    run_dir = _staged_run(root, date(2026, 1, 9), "108")
    out = tmp_path / "bound.json"
    result = bind_spec(
        base_spec=base,
        runs=[run_dir],
        root_id="legacy",
        as_of=date(2026, 1, 9),
        out=out,
        verify=False,
        preflight=True,
    )
    assert result["preflight"] == "PASSED" and result["added_sources"] == 1
    catalog, _, _ = build_generation(out)
    assert max(
        date.fromisoformat(row["as_of_date"]) for row in catalog["nav_observations"]
    ) == date(2026, 1, 9)


def test_a_bound_spec_the_builder_refuses_is_not_written(tmp_path: Path) -> None:
    base = _refreshed_spec(tmp_path)
    spec = json.loads(base.read_text(encoding="utf-8"))
    root = Path(spec["roots"]["legacy"])
    # a different NAV for a day the retained refresh already covers is a conflicting overlap
    run_dir = _staged_run(root, date(2026, 1, 8), "999")
    out = tmp_path / "refused.json"
    with pytest.raises(BindError, match="generation builder refused the spec"):
        bind_spec(
            base_spec=base,
            runs=[run_dir],
            root_id="legacy",
            as_of=date(2026, 1, 9),
            out=out,
            verify=False,
            preflight=True,
        )
    assert not out.exists()
