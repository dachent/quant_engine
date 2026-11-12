"""#884 G2: each row fails on the product fact it exists for; the receipt gates the stage that needs it."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import panel_payload_size as payload  # noqa: E402
import release_driver as driver  # noqa: E402
import release_preflight as pf  # noqa: E402
import release_preflight_g2 as g2  # noqa: E402
from test_release_driver import _graph, world  # noqa: E402,F401

VALIDATOR = ["{python}", "-c", "pass", "{run}/stages/panel/candidates/{candidate_id}"]


@pytest.fixture
def gworld(world: dict[str, Any]) -> dict[str, Any]:  # noqa: F811
    cand = world["run"] / "stages" / "panel" / "candidates" / "C1"
    (cand / "data").mkdir(parents=True)
    budget = json.loads(payload.BUDGET_PATH.read_bytes())
    data = {name: [0] for name in budget["blocks"]}  # every listed block, tiny
    (cand / "data" / "dashboard_data.json").write_text(
        json.dumps(data), encoding="utf-8"
    )
    world["cand"] = cand
    world["budget"] = budget
    world["graph"] = _graph(
        world,
        [
            {
                "id": "g2",
                "builtin": "g2",
                "validator_argv": VALIDATOR,
            }
        ],
        derive={"candidate_id": {"single_dir": "{run}/stages/panel/candidates"}},
    )
    return world


def _run(w: dict[str, Any]) -> driver.Run:
    return driver.load_run(w["config"], w["graph"])


def _rows(w: dict[str, Any], rc: int = 0) -> dict[str, str]:
    receipt = g2.run_g2(_run(w), g2.Env(lambda argv: rc))
    return {r["row"]: r["status"] for r in receipt["rows"]}


def _only_fails(rows: dict[str, str], row: str) -> None:
    assert rows[row] == "FAIL", rows
    assert {k for k, v in rows.items() if v == "FAIL"} == {row}, rows


def test_a_healthy_candidate_passes_every_row(gworld: dict[str, Any]) -> None:
    assert set(_rows(gworld).values()) == {"PASS"}


def test_refusal_17_a_block_over_its_budget_fails_only_the_payload_row(
    gworld: dict[str, Any],
) -> None:
    name, limit = next(iter(gworld["budget"]["blocks"].items()))
    data = json.loads((gworld["cand"] / "data" / "dashboard_data.json").read_bytes())
    data[name] = "x" * (limit + 1)
    (gworld["cand"] / "data" / "dashboard_data.json").write_text(
        json.dumps(data), encoding="utf-8"
    )
    _only_fails(_rows(gworld), "g2:payload-size")


def test_refusal_12_a_validator_blocker_fails_only_the_findings_row(
    gworld: dict[str, Any],
) -> None:
    _only_fails(_rows(gworld, rc=1), "g2:validator-findings")
    # the validator could not judge (exit 2) is a FAIL too, never a pass
    _only_fails(_rows(gworld, rc=2), "g2:validator-findings")


def test_a_missing_candidate_fails_the_candidate_row(gworld: dict[str, Any]) -> None:
    (gworld["cand"] / "data" / "dashboard_data.json").unlink()
    assert _rows(gworld)["g2:candidate"] == "FAIL"


def test_the_validator_command_comes_from_the_stage_graph(
    gworld: dict[str, Any],
) -> None:
    argv = g2.validator_argv(_run(gworld))
    assert argv[-1].endswith("C1")
    gworld["graph"] = _graph(gworld, [{"id": "g2", "builtin": "g2"}])
    with pytest.raises(driver.Refusal, match="validator_argv"):
        g2.validator_argv(_run(gworld))


def test_the_receipt_gates_a_stage_and_is_bound_to_the_candidate(
    gworld: dict[str, Any],
) -> None:
    run = _run(gworld)
    with pytest.raises(driver.Refusal, match="no receipt"):
        pf.verify_receipt(run, "G2")
    pf.write_receipt(run, g2.run_g2(run, g2.Env(lambda argv: 0)))
    pf.verify_receipt(run, "G2")
    receipt = json.loads(pf.receipt_path(run, "G2").read_bytes())
    receipt["candidate_id"] = "OTHER"
    pf.write_receipt(run, receipt)
    with pytest.raises(driver.Refusal, match="another panel candidate"):
        pf.verify_receipt(run, "G2")
    # a G0 receipt never opens a G2 gate
    pf.write_receipt(
        run,
        {**g2.run_g2(run, g2.Env(lambda argv: 0)), "kind": "release-preflight-g0-v1"},
    )
    with pytest.raises(driver.Refusal, match="malformed"):
        pf.verify_receipt(run, "G2")


def test_the_builtin_stage_refuses_on_fail_and_records_the_receipt(
    gworld: dict[str, Any],
) -> None:
    run = _run(gworld)
    with pytest.raises(driver.Refusal, match="G2 FAIL"):
        g2.builtin_g2(run, Path("x"), g2.Env(lambda argv: 1))
    assert json.loads(pf.receipt_path(run, "G2").read_bytes())["status"] == "FAIL"


def test_the_real_stage_graph_wires_g2_before_qa_and_names_the_rehearsal() -> None:
    graph = json.loads(
        (REPO_ROOT / "ops/mvp2-release/release-stages.json").read_bytes()
    )
    stages = {s["id"]: s for s in graph["stages"]}
    assert stages["g2"]["requires"] == ["attended"] and stages["g2"]["builtin"] == "g2"
    assert stages["qa-browser"]["needs_gate"] == ["g2"] and stages["qa-browser"][
        "requires"
    ] == ["g2"]
    assert "--rehearsal-out" in stages["post-commit-verify"]["argv"]


def test_every_g2_coverage_entry_is_a_row_with_a_named_kill_test() -> None:
    rows = dict(g2.ROWS)
    source = Path(__file__).read_text(encoding="utf-8")
    for cause, where in pf.COVERAGE.items():
        if where.startswith("G2:"):
            assert where[3:] in rows, (cause, where)
            assert f"def test_refusal_{cause}_" in source, cause
