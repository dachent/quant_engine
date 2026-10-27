"""R2: derivation A and derivation B agree on one synthetic world, and so does the manifest.

SYNTHETIC: the world is the toy tree of ``test_derive_b_sleeves`` (invented accounts,
executions and values).  Both derivations run as the release runs them (command line,
``--sleeve-inputs ... --sleeves-only``); ``sleeve_compare`` lists every difference, and
the epoch rows the production manifest engine (``analyze_epoch``) builds from the same
NAV series and legs are compared with what the derivations declare.
"""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from acceptance.validate import sleeve_compare
from test_derive_b_sleeves import write_sleeve_inputs, write_toy_tree

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPOSITORY_ROOT / "tools"


def _run(argv: list[str]) -> dict[str, Any]:
    done = subprocess.run(
        [sys.executable, "-I", *argv],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPOSITORY_ROOT,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(Path(argv[argv.index("--out") + 1]).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def derivations(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("toy")
    selection = write_sleeve_inputs(root, write_toy_tree(root))
    common = [
        "--qe-root",
        str(root),
        "--sleeve-inputs",
        str(selection),
        "--sleeves-only",
    ]
    out_a, out_b = root / "a.json", root / "b.json"
    return {
        "a": _run(
            [str(REPOSITORY_ROOT / "acceptance" / "derive_a"), *common]
            + ["--out", str(out_a)]
        ),
        "b": _run(
            [str(REPOSITORY_ROOT / "acceptance" / "derive_b" / "derive.py"), *common]
            + ["--out", str(out_b)]
        ),
    }


def test_the_two_derivations_declare_the_same_epochs(
    derivations: dict[str, Any],
) -> None:
    epochs = derivations["a"]["sleeve_derivation"]["epochs"]
    assert epochs, "the toy world must derive at least one epoch"
    assert sleeve_compare.compare_derivations(derivations["a"], derivations["b"]) == []


def test_both_derivations_agree_with_the_manifest_rows_built_from_their_series(
    derivations: dict[str, Any],
) -> None:
    sys.path.insert(0, str(TOOLS))
    try:
        import account_strategy_manifest as manifest
    finally:
        sys.path.remove(str(TOOLS))
    compared = 0
    for label in ("a", "b"):
        document = derivations[label]
        rows: list[dict[str, Any]] = []
        for epoch in document["sleeve_derivation"]["epochs"]:
            legs_in: dict[Any, Decimal] = {}
            legs_out: dict[Any, Decimal] = {}
            for number, leg in enumerate(epoch["legs"]):
                key = (leg["event_day"], leg["recognition_day"], f"leg{number}")
                target = legs_in if leg["class"] == "INTERNAL_IN" else legs_out
                target[key] = Decimal(leg["amount"])
            result = manifest.analyze_epoch(
                {day: Decimal(value) for day, value in epoch["nav"].items()},
                {},
                {},
                legs_in,
                legs_out,
                None,
                epoch["effective_start"],
                epoch["effective_end"],
                derived_sleeve=True,
            )
            row = {
                "account_id": epoch["account_id"],
                "strategy_id": epoch["strategy_id"],
                "effective_start": epoch["effective_start"],
                "effective_end": epoch["effective_end"],
                "opening_capital": epoch["opening"],
                "net_capital": epoch["net_capital"],
                "current_value": epoch["closing"],
            }
            if result is None:
                # the manifest builds no performance for an epoch of fewer than
                # three NAV observations: there is nothing to compare for it
                assert len(epoch["nav"]) < 3
                rows.append(row)
                continue
            assert "unavailable_status" not in result
            compared += 1
            rows.append(
                {
                    **row,
                    "opening_capital": str(result["opening_capital"]),
                    "net_capital": str(result["net_capital"]),
                    "current_value": str(result["current_value"]),
                }
            )
            wanted = {
                row["metric_id"]: row["value"]
                for row in document["metrics"]
                if row["entity"] == epoch["account_id"]
            }
            twr = result["observation_linked_return"]
            assert abs(Decimal(wanted["twr.account"]) - twr) < Decimal("1e-8")
            assert int(wanted["flows.account.count"]) == result["flow_count"]
        parents = {
            epoch["account_id"].rsplit("-", 1)[0]
            for epoch in document["sleeve_derivation"]["epochs"]
        }
        rows.extend(
            {
                **rows[0],
                "account_id": f"{parent}-undeclared",
            }
            for parent in sorted(parents)
        )
        found = sleeve_compare.compare_with_manifest(document, rows)
        assert [item.where for item in found] == [
            f"manifest {parent}-undeclared" for parent in sorted(parents)
        ]
    assert compared >= 2
