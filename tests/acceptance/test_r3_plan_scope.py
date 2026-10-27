"""R3 (4): every out-of-scope requirement the governed plan carries is honoured, exactly.

The decomposition plan marks a (series, source) pair out of scope either as
``NOT_APPLICABLE_METHOD_SCOPE`` or ``NOT_APPLICABLE_OWNER_APPROVED``.  A non-available cell
is excused only when the plan carries the pair with such a requirement and the cell's own
label equals it; a self-label the plan does not confirm, or one that names the other kind,
is still a gap.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from test_r2_measures import PLAN, _cell, _findings, _incomplete, _lens
from test_validate_generation import Spec

from acceptance.validate import checks

METHOD = "NOT_APPLICABLE_METHOD_SCOPE"
OWNER = "NOT_APPLICABLE_OWNER_APPROVED"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _plan() -> dict:
    plan = copy.deepcopy(PLAN)
    plan["strategies"].append(
        {
            "series_id": "S-OWN",
            "source_dispositions": {
                "us-economic": {"requirement": OWNER, "readiness": OWNER}
            },
        }
    )
    return plan


def test_an_owner_approved_cell_the_plan_confirms_is_not_a_gap(tmp_path: Path) -> None:
    spec = Spec(report=[_lens(_cell("S-OWN", "UNAVAILABLE", OWNER))])
    assert _incomplete(_findings(tmp_path, spec, _plan())) == []


def test_both_out_of_scope_kinds_together_are_not_a_gap(tmp_path: Path) -> None:
    spec = Spec(
        report=[
            _lens(
                _cell("S-OWN", "UNAVAILABLE", OWNER),
                _cell("S-NA", "UNAVAILABLE", METHOD),
            )
        ]
    )
    assert _incomplete(_findings(tmp_path, spec, _plan())) == []


def test_a_self_labelled_owner_approved_cell_the_plan_requires_is_a_gap(
    tmp_path: Path,
) -> None:
    spec = Spec(report=[_lens(_cell("S-REQ", "UNAVAILABLE", OWNER))])
    assert _incomplete(_findings(tmp_path, spec, _plan())) == [1]


def test_a_cell_naming_the_other_out_of_scope_kind_is_a_gap(tmp_path: Path) -> None:
    spec = Spec(
        report=[
            _lens(
                _cell("S-OWN", "UNAVAILABLE", METHOD),
                _cell("S-NA", "UNAVAILABLE", OWNER),
            )
        ]
    )
    assert _incomplete(_findings(tmp_path, spec, _plan())) == [2]


def test_a_label_the_plan_does_not_carry_at_all_is_a_gap(tmp_path: Path) -> None:
    spec = Spec(report=[_lens(_cell("S-OWN", "UNAVAILABLE", "NOT_APPLICABLE_OTHER"))])
    assert _incomplete(_findings(tmp_path, spec, _plan())) == [1]


def test_the_real_plan_pins_exactly_the_two_kinds_and_every_pair_is_confirmed() -> None:
    plan = json.loads(
        (
            REPOSITORY_ROOT / "docs/analytics/current-strategy-decomposition-plan.json"
        ).read_bytes()
    )
    context = checks.Context(
        gen=None,  # type: ignore[arg-type]
        cutoff=checks.date(2026, 9, 17),
        cutoff_source="test",
        repo_root=REPOSITORY_ROOT,
    )
    dispositions = checks._plan_dispositions(context)
    out_of_scope = {
        key: value["requirement"]
        for key, value in dispositions.items()
        if str(value["requirement"]).startswith("NOT_APPLICABLE")
    }
    assert set(out_of_scope.values()) == {METHOD, OWNER}
    assert (
        sorted(kind for kind in out_of_scope.values() if kind == OWNER) == [OWNER] * 48
    )
    assert len(plan["strategies"]) > 0
    for (series_id, source_key), kind in out_of_scope.items():
        cell = {
            "series_id": series_id,
            "source_key": source_key,
            "status": "UNAVAILABLE",
            "selection_status": kind,
        }
        assert checks._cell_is_explained(cell, dispositions), (series_id, source_key)
