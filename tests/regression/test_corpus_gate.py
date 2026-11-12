"""#911: the corpus registry is consistent and the release gate bites."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import check_regression_corpus as gate  # noqa: E402

REGISTRY = json.loads(gate.REGISTRY.read_text(encoding="utf-8"))


def test_live_registry_is_consistent() -> None:
    assert gate.check_structure(REGISTRY) == []


def test_every_defect_named_in_the_ticket_has_a_slot() -> None:
    named = {
        "atyr_reverse_split_alias",
        "ibkr_total_cash_loan_dropped",
        "d8_publication_check_never_passes",
        "robuxio_gross_net_splice",
        "grizzlybears_3x_replica_scale",
        "matrixport_gap_days",
        "validator_fixture_values_no_builder_emits",
        "scratch_path_sidecar_read",
    }
    assert named <= set(REGISTRY["cases"])


def test_covered_case_with_missing_test_is_refused() -> None:
    bad = copy.deepcopy(REGISTRY)
    bad["cases"]["ci_reports_every_failure"]["test"] = (
        "tests/regression/test_ci_reports_every_failure.py::test_nope"
    )
    assert any("not a test function" in f for f in gate.check_structure(bad))


def test_covered_case_without_kill_statement_is_refused() -> None:
    bad = copy.deepcopy(REGISTRY)
    bad["cases"]["ci_reports_every_failure"]["kill"] = ""
    assert any("fails on pre-fix" in f for f in gate.check_structure(bad))


def test_ticket_citing_unknown_case_is_refused() -> None:
    bad = copy.deepcopy(REGISTRY)
    bad["tickets"]["894"]["cases"] = ["no_such_case"]
    assert any("unknown case" in f for f in gate.check_structure(bad))


def test_release_gate_reports_every_open_case_and_uncited_ticket() -> None:
    found = gate.check_release(REGISTRY, ["910", "894", "999"])
    text = "\n".join(found)
    assert "atyr_reverse_split_alias: open case" in text
    assert "matrixport_gap_days: open case" in text  # all of them, not the first
    assert "#894: cited case ibkr_total_cash_loan_dropped is not covered" in text
    assert "#999: closed ticket cites no regression case" in text
    assert "#910" not in text


def test_release_gate_passes_when_everything_is_covered() -> None:
    good = copy.deepcopy(REGISTRY)
    for case in good["cases"].values():
        case.update(
            status="covered",
            kill="x",
            test="tests/regression/test_ci_reports_every_failure.py::test_pytest_has_no_maxfail",
        )
    assert gate.check_release(good, ["894", "910"]) == []
