"""The panel carries a stale implementation row as NOT_READY (ruling N66-R, option b).

The report replaces a row whose contract was built from another provider ledger with
a closed NOT_READY object.  The panel publishes that object, reports the layer as not
ready (never as the other contract kind's status, never as a legacy comparison),
labels the pre-policy AI Factor contract with its dates, and its verifier checks the
object's shape.  Rows come from the real report producer.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date
import html
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, TOOLS_ROOT / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


panel = _load("portfolio_control_panel_stale_rows", "portfolio_control_panel.py")
import portfolio_report as reporter  # noqa: E402

GRIZZLY = "grizzlybears-vtma"
TT = "tt-portfolio"
AI = "p123-ai-factor"
NAMES = {GRIZZLY: "GrizzlyBears VTMA", TT: "TT Portfolio", AI: "AI Factor [P123]"}
OLD_LEDGER = "23768b65" + "0" * 56
GOVERNED_LEDGER = "8946efa9" + "0" * 56
STALE = "NOT_READY_STALE_CONTRACT_LEDGER_VINTAGE"
POLICY = {"policy_id": "PROVIDER_ADMIT_OR_DECLARE_V1", "effective_start": "2026-08-14"}


def _stale(
    kind: str,
    strategy_id: str,
    *,
    as_of: str,
    start: str,
    end: str,
    governed: float,
    figure: float,
    count: int,
) -> dict[str, Any]:
    """A stale object exactly as the report producer builds it."""

    return reporter._stale_implementation_row(
        {
            "contract_kind": kind,
            "contract_kind_text": kind.lower().replace("_", " "),
            "contract_id": "a" * 64,
            "contract_sha256": "b" * 64,
            "contract_as_of_date": as_of,
            "contract_ledger_sha256": OLD_LEDGER,
            "governed_ledger_sha256": GOVERNED_LEDGER,
        },
        strategy_id,
        start=start,
        end=end,
        governed_figure=governed,
        governed_count=count,
        figure=figure,
    )


def _bridge(model: float, account: float) -> dict[str, Any]:
    gap = (account - model) * 100.0
    return {
        "account_basis": "BASELINE_ACCOUNT_LIVE",
        "account_return": account,
        "categories": [{"effect_pp": gap}],
        "gap_pp": gap,
        "model_basis": "EXACT_WORKBOOK_MODEL_RETURNS",
        "model_return": model,
        "status": "DIAGNOSTIC_CAUSAL_BRIDGE",
    }


def _attribution(model: float, account: float) -> dict[str, Any]:
    return {
        "bridge": _bridge(model, account),
        "observed_end": "2026-07-30",
        "observed_start": "2026-03-31",
    }


def _reconciliation(account: float) -> dict[str, Any]:
    return {
        "return_reconciliation": {
            "account_basis": "BASELINE_ACCOUNT_LIVE",
            "account_cumulative_return": account,
            "model_basis": "PROVIDER_LIVE_MODEL_RETURNS",
            "observed_end": "2026-07-24",
            "observed_start": "2026-01-22",
            "status": "DIAGNOSTIC_PROVIDER_LIVE_COMPARISON",
        }
    }


def _strategies() -> list[dict[str, Any]]:
    """Grizzly: both stale.  TT: reconciliation stale, attribution accepted.
    AI Factor: both accepted."""

    grizzly_rec = _stale(
        "RECONCILIATION",
        GRIZZLY,
        as_of="2026-08-03",
        start="2026-01-05",
        end="2026-07-17",
        governed=0.0027196,
        figure=0.0003508,
        count=194,
    )
    grizzly_attr = _stale(
        "RETURN_ATTRIBUTION",
        GRIZZLY,
        as_of="2026-08-09",
        start="2026-01-05",
        end="2026-07-17",
        governed=0.0027196,
        figure=0.0003508,
        count=194,
    )
    tt_rec = _stale(
        "RECONCILIATION",
        TT,
        as_of="2026-08-03",
        start="2025-11-25",
        end="2026-07-30",
        governed=0.0035765,
        figure=0.0039681,
        count=248,
    )
    rows = {
        GRIZZLY: (grizzly_rec, grizzly_attr, STALE),
        TT: (
            tt_rec,
            _attribution(0.1347, 0.0882),
            "PARTIAL_CAUSAL_ATTRIBUTION_RECONCILIATION_NOT_READY",
        ),
        AI: (
            _reconciliation(-0.0446),
            _attribution(0.1501, -0.0446),
            "ACCEPTED_ACCOUNT_LIVE",
        ),
    }
    return [
        {
            "strategy_id": strategy_id,
            "strategy_name": NAMES[strategy_id],
            "implementation_reconciliation": rec,
            "implementation_return_attribution": attr,
            "implementation_reconciliation_status": status,
        }
        for strategy_id, (rec, attr, status) in rows.items()
    ]


def _references() -> dict[str, dict[str, Any]]:
    """The contract references exactly as the report producer builds their extras."""

    out: dict[str, dict[str, Any]] = {}
    for key, as_of, stale_ids in (
        ("implementation_reconciliation_contract", "2026-08-03", [GRIZZLY, TT]),
        ("implementation_return_attribution_contract", "2026-08-09", [GRIZZLY]),
    ):
        rows = {item["strategy_id"]: {} for item in _strategies()}
        out[key] = {
            "as_of_date": as_of,
            **reporter._implementation_reference_extras(
                rows,
                {strategy_id: {} for strategy_id in stale_ids},
                contract_as_of=date.fromisoformat(as_of),
                provider_return_policy=POLICY,
                model_comparison_status=lambda row: "DIAGNOSTIC",
            ),
        }
    return out


def _underwriting(
    strategies: list[dict[str, Any]] | None = None,
    references: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    result, _hash = panel._strategy_underwriting(
        strategies if strategies is not None else _strategies(),
        {"modes": {}},
        {
            "economic_driver_cluster_budget": {"clusters": []},
            "correlation_one": {"components": [], "loss": "0.2"},
        },
        {"series": []},
        "2026-09-17",
        implementation_contracts=(
            references if references is not None else _references()
        ),
    )
    return result


def _layers(underwriting: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        row["strategy_id"]: row["evidence_layers"]["implementation_reconciliation"]
        for row in underwriting["rows"]
    }


def test_a_stale_kind_makes_the_layer_not_ready_and_never_the_other_kinds_status() -> (
    None
):
    layers = _layers(_underwriting())

    assert layers[GRIZZLY]["comparison_status"] == STALE
    # TT's attribution is accepted and has a bridge status, but its reconciliation is
    # stale, so the layer is not ready: the bridge status is not borrowed.
    assert layers[TT]["comparison_status"] == STALE
    assert layers[TT]["detail"]["return_attribution"]["bridge"]["status"] == (
        "DIAGNOSTIC_CAUSAL_BRIDGE"
    )
    assert layers[AI]["comparison_status"] == "DIAGNOSTIC_PROVIDER_LIVE_COMPARISON"
    assert layers[GRIZZLY]["status"] == STALE
    assert layers[TT]["status"] == (
        "PARTIAL_CAUSAL_ATTRIBUTION_RECONCILIATION_NOT_READY"
    )


def test_the_detail_carries_each_stale_object_under_its_own_key() -> None:
    strategies = _strategies()
    layers = _layers(_underwriting(strategies))
    by_id = {row["strategy_id"]: row for row in strategies}

    assert set(layers[GRIZZLY]["detail"]) == {
        "stale_reconciliation",
        "stale_return_attribution",
    }
    assert set(layers[TT]["detail"]) == {"stale_reconciliation", "return_attribution"}
    assert set(layers[AI]["detail"]) == {"return_reconciliation", "return_attribution"}
    assert (
        layers[GRIZZLY]["detail"]["stale_reconciliation"]
        == by_id[GRIZZLY]["implementation_reconciliation"]
    )
    assert (
        layers[GRIZZLY]["detail"]["stale_return_attribution"]
        == by_id[GRIZZLY]["implementation_return_attribution"]
    )
    governed = layers[GRIZZLY]["detail"]["stale_reconciliation"]
    assert governed["governed_account_return"] == 0.0027196
    assert governed["governed_account_basis"] == "BASELINE_ACCOUNT_LIVE"


def test_the_superseded_figure_is_never_read_as_a_return() -> None:
    underwriting = _underwriting()
    layers = _layers(underwriting)
    grizzly = layers[GRIZZLY]

    text = json.dumps(grizzly, sort_keys=True)
    assert text.count("0.0003508") == 2  # one labelled copy per stale object
    for key in ("stale_reconciliation", "stale_return_attribution"):
        stale = grizzly["detail"][key]
        assert stale["superseded_contract_account_basis"] == (
            "SUPERSEDED_LEDGER_VINTAGE_NOT_AN_ACCOUNT_RETURN"
        )
        assert stale["superseded_contract_account_return"] == 0.0003508
    # No derived field of the layer or of the gap carries it.
    assert "0.0003508" not in json.dumps(
        [row["model_account_gap"] for row in underwriting["rows"]]
    )
    assert grizzly["comparison_status"] == STALE


def test_the_contract_context_labels_the_pre_policy_ai_contract_with_its_dates() -> (
    None
):
    layers = _layers(_underwriting())
    ai = layers[AI]["contracts"]

    assert ai["provider_return_policy"] == POLICY
    assert ai["reconciliation"] == {
        "as_of_date": "2026-08-03",
        "pre_policy": True,
        "stale": False,
    }
    assert ai["return_attribution"] == {
        "as_of_date": "2026-08-09",
        "pre_policy": True,
        "stale": False,
    }
    assert layers[GRIZZLY]["contracts"]["reconciliation"]["stale"] is True
    assert layers[GRIZZLY]["contracts"]["return_attribution"]["stale"] is True
    assert layers[TT]["contracts"]["return_attribution"]["stale"] is False
    assert layers[TT]["contracts"]["reconciliation"]["pre_policy"] is False


def test_the_contract_context_is_present_and_empty_without_a_contract_reference() -> (
    None
):
    layers = _layers(_underwriting(references={}))

    for layer in layers.values():
        assert layer["contracts"] == {
            "provider_return_policy": None,
            "reconciliation": None,
            "return_attribution": None,
        }


def test_a_missing_return_reconciliation_is_not_ready_not_legacy() -> None:
    strategies = _strategies()
    ai = next(row for row in strategies if row["strategy_id"] == AI)
    ai["implementation_reconciliation"] = None
    ai["implementation_return_attribution"] = _attribution(0.1501, -0.0446)

    layer = _layers(_underwriting(strategies))[AI]

    assert layer["comparison_status"] == "NOT_READY_NO_RETURN_RECONCILIATION"
    assert layer["detail"]["return_attribution"]["bridge"]["status"] == (
        "DIAGNOSTIC_CAUSAL_BRIDGE"
    )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda row: row.update({"return_reconciliation": {"status": "X"}}),
        lambda row: row.pop("repair_ref"),
        lambda row: row.update({"contract_kind": "RETURN_ATTRIBUTION"}),
        lambda row: row.update(
            {"governed_provider_ledger_sha256": row["contract_provider_ledger_sha256"]}
        ),
        lambda row: row.update({"governed_account_basis": "SOMETHING_ELSE"}),
        lambda row: row.update({"governed_account_return": float("nan")}),
        lambda row: row.update({"strategy_id": "someone-else"}),
    ),
    ids=(
        "foreign-key",
        "missing-key",
        "wrong-kind",
        "same-ledger",
        "wrong-basis",
        "non-finite",
        "wrong-strategy",
    ),
)
def test_a_malformed_stale_object_fails_the_build(mutation: Any) -> None:
    strategies = _strategies()
    grizzly = next(row for row in strategies if row["strategy_id"] == GRIZZLY)
    mutation(grizzly["implementation_reconciliation"])

    with pytest.raises(panel.ControlPanelValidationError, match="stale"):
        _underwriting(strategies)


def test_the_verifier_checks_the_stale_set_and_the_exact_perimeter() -> None:
    """F8: the underwriting check passes with the stale attribution set and fails
    without it."""

    rows = _underwriting()["rows"]
    findings = panel._check_implementation_layers(rows)

    assert findings["stale_shapes_valid"] is True
    assert findings["gap_reconciles"] is True
    assert findings["causal_attribution_reconciles"] is True
    assert findings["stale_attribution_strategy_ids"] == [GRIZZLY]
    assert findings["causal_attribution_strategy_ids"] == [AI, TT]
    assert findings["stale_reconciliation_strategy_ids"] == [GRIZZLY, TT]
    assert findings["implementation_strategy_ids"] == [GRIZZLY, AI, TT]

    removed = deepcopy(rows)
    layer = next(row for row in removed if row["strategy_id"] == GRIZZLY)
    del layer["evidence_layers"]["implementation_reconciliation"]["detail"][
        "stale_return_attribution"
    ]
    assert panel._check_implementation_layers(removed)[
        "implementation_strategy_ids"
    ] == [
        AI,
        TT,
    ]


def test_the_verifier_refuses_a_stale_object_that_republishes_a_model_figure() -> None:
    rows = deepcopy(_underwriting()["rows"])
    grizzly = next(row for row in rows if row["strategy_id"] == GRIZZLY)
    detail = grizzly["evidence_layers"]["implementation_reconciliation"]["detail"]
    detail["stale_reconciliation"]["model_return"] = 0.34

    assert panel._check_implementation_layers(rows)["stale_shapes_valid"] is False


def test_the_verifier_refuses_a_stale_layer_that_reports_a_comparison_status() -> None:
    rows = deepcopy(_underwriting()["rows"])
    tt = next(row for row in rows if row["strategy_id"] == TT)
    tt["evidence_layers"]["implementation_reconciliation"]["comparison_status"] = (
        "DIAGNOSTIC_CAUSAL_BRIDGE"
    )

    assert panel._check_implementation_layers(rows)["stale_shapes_valid"] is False


# ---- app.js:1103 rendering seam: real producer `contracts`, not literal fields ----
#
# Astra finding at app.js:1103: the pre-policy exclusion disclosure read
# detail.pre_policy_exclusion / detail.return_attribution.contract_as_of_date, fields
# the producer never emits.  The producer instead publishes, per contract kind, under
# evidence_layers.implementation_reconciliation.contracts[kind] = {as_of_date, stale,
# pre_policy}, plus one shared contracts.provider_return_policy = {policy_id,
# effective_start} (see test_the_contract_context_labels_the_pre_policy_ai_contract_
# with_its_dates above, and _implementation_contract_context in
# tools/portfolio_control_panel.py).  This test renders the real producer's AI Factor
# row (pre-policy reconciliation contract) through app.js and asserts the dated
# exclusion disclosure, proving app.js consumes the producer's actual shape.


def test_real_producer_contracts_render_the_dated_pre_policy_exclusion_disclosure(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("the shell tests need node")

    underwriting = _underwriting()
    ai_row = next(row for row in underwriting["rows"] if row["strategy_id"] == AI)
    contracts = ai_row["evidence_layers"]["implementation_reconciliation"]["contracts"]
    # Sanity: the real producer marks the AI Factor reconciliation contract
    # pre-policy, with the dates this test expects app.js to render.
    assert contracts["reconciliation"]["pre_policy"] is True
    assert contracts["reconciliation"]["as_of_date"] == "2026-08-03"
    assert contracts["provider_return_policy"] == POLICY

    payload = {"strategy_underwriting": underwriting}
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    harness = Path(__file__).with_name("shell_harness.mjs")
    completed = subprocess.run(
        [
            node,
            str(harness),
            str(REPOSITORY_ROOT / "apps" / "portfolio-control-panel" / "app.js"),
            str(payload_path),
            "renderUnderwriting",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["errors"] == [], result["errors"]
    cell = re.sub(
        r"\s+",
        " ",
        html.unescape(
            re.sub(r"<[^>]+>", " ", result["dom"]["#underwriting-table"]["innerHTML"])
        ),
    ).strip()

    assert (
        "contract as of Aug 3, 2026, before the PROVIDER_ADMIT_OR_DECLARE_V1 "
        "exclusion effective Aug 14, 2026"
    ) in cell
