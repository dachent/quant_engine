"""#911: the corpus registry is consistent and the release gate bites."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from typing import Any

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
    opened = [k for k, v in REGISTRY["cases"].items() if v["status"] == "open"]
    assert len(opened) >= 2
    for case_id in opened:  # all of them, not the first
        assert f"{case_id}: open case" in text
    assert "#894: cited case ibkr_total_cash_loan_dropped is not covered" in text
    assert "#999: closed ticket cites no regression case" in text
    assert "#910" not in text


def _covered_tree(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    good = copy.deepcopy(REGISTRY)
    folder = tmp_path / "tests" / "regression"
    folder.mkdir(parents=True)
    for case_id, case in good["cases"].items():
        (folder / f"test_{case_id}.py").write_text(
            f"def test_{case_id}():\n    assert True\n", encoding="utf-8"
        )
        case.update(
            status="covered",
            kill="x",
            test=f"tests/regression/test_{case_id}.py::test_{case_id}",
        )
    return good, tmp_path


def test_release_gate_passes_when_everything_is_covered(tmp_path: Path) -> None:
    good, root = _covered_tree(tmp_path)
    assert gate.check_release(good, ["894", "910"], root) == []


def test_two_cases_may_not_share_one_test(tmp_path: Path) -> None:
    good, root = _covered_tree(tmp_path)
    good["cases"]["matrixport_gap_days"]["test"] = good["cases"][
        "atyr_reverse_split_alias"
    ]["test"]
    assert any("shares test" in f for f in gate.check_release(good, ["894"], root))


def test_nested_function_and_path_escape_are_not_tests(tmp_path: Path) -> None:
    good, root = _covered_tree(tmp_path)
    folder = root / "tests" / "regression"
    (folder / "test_matrixport_gap_days.py").write_text(
        "def helper():\n    def test_matrixport_gap_days():\n        pass\n",
        encoding="utf-8",
    )
    (root / "outside.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    good["cases"]["scratch_path_sidecar_read"]["test"] = (
        "tests/regression/../../outside.py::test_x"
    )
    found = "\n".join(gate.check_structure(good, root))
    assert "matrixport_gap_days: 'test_matrixport_gap_days' is not a test" in found
    assert "scratch_path_sidecar_read: test file" in found


def test_release_gate_needs_a_closed_ticket_list(tmp_path: Path) -> None:
    good, root = _covered_tree(tmp_path)
    assert any("--closed" in f for f in gate.check_release(good, [], root))
