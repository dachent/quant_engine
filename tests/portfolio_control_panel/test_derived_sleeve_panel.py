"""The panel over a manifest that carries derived sleeves (#567, stage 5)."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location(
    "portfolio_control_panel_derived_sleeves",
    ROOT / "tools" / "portfolio_control_panel.py",
)
assert SPEC is not None and SPEC.loader is not None
panel = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = panel
SPEC.loader.exec_module(panel)

CONTRACT = ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
PARENT = "account-ibkr"
EPOCH_COUNTS = {
    "p123-ai-factor": 1,
    "qsi": 2,
    "tt-portfolio": 6,
    "p123-eu": 1,
    "grizzlybears-vtma": 1,
    "robuxio-crypto": 1,
    "volatis-strategy": 1,
}
NESTED = ("p123-conn-one", "p123-conn-two", "quantbase-retired")


def _key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["account_id"],
        row["strategy_id"],
        row["effective_start"],
        row["effective_end"],
    )


def _baseline_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for strategy, count in EPOCH_COUNTS.items():
        for index in range(count):
            rows.append(
                {
                    "account_id": (
                        PARENT
                        if strategy == "p123-ai-factor"
                        else f"{strategy}-{index}"
                    ),
                    "strategy_id": strategy,
                    "effective_start": "2026-01-22",
                    "effective_end": None,
                }
            )
    return rows


def _crosswalk(rows: list[dict[str, Any]]) -> dict[str, Any]:
    crosswalk_rows = [
        {
            **row,
            "account_label": f"Account {index}",
            "assignment_id": f"assignment-{index}",
            "last_four": None,
            "owner_reason": "OWNER_CONFIRMED_CURRENT_PRODUCT_EPOCH",
            "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
            "provider": "TEST",
        }
        for index, row in enumerate(rows)
    ]
    crosswalk_rows.extend(
        [
            {
                "account_id": "account-composer-history",
                "account_label": "Composer history",
                "assignment_id": "assignment-composer-history",
                "effective_end": None,
                "effective_start": "2025-09-22",
                "last_four": None,
                "owner_reason": "COMPOSER_OWNER_EXCLUDED_IMMATERIAL",
                "product_epoch_disposition": "EXCLUDED_IMMATERIAL",
                "provider": "COMPOSER",
                "strategy_id": "composer-holy-grail",
            },
            {
                "account_id": "account-echo-history",
                "account_label": "Alpaca echo history",
                "assignment_id": "assignment-echo-history",
                "effective_end": "2025-11-24",
                "effective_start": "2025-09-27",
                "last_four": "6903",
                "owner_reason": "ECHOTRADE_DEPRECATED_EFFECTIVE_2025_11_25",
                "product_epoch_disposition": "EXCLUDED_DEPRECATED_HISTORY_ONLY",
                "provider": "ALPACA",
                "strategy_id": "echotrade-growth-pivots",
            },
        ]
    )
    return {
        "artifact_id": "account-strategy-current-epoch-crosswalk",
        "as_of_date": "2026-08-16",
        "calculation_cutoff_date": "2026-08-13",
        "disposition_counts": {
            "EXCLUDED_DEPRECATED_HISTORY_ONLY": 1,
            "EXCLUDED_IMMATERIAL": 1,
            "INCLUDED_PRODUCT_EPOCH": 13,
        },
        "rows": crosswalk_rows,
        "schema_version": 1,
        "selected_baseline": {
            "artifact_id": "normalized/portfolio-baseline.json",
            "as_of_date": "2026-08-16",
            "generation_id": "a" * 64,
            "schema_name": "portfolio-baseline-catalog",
            "schema_version": "1",
            "sha256": "b" * 64,
            "size_bytes": 1,
        },
    }


def _derived_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Manifest rows and performance rows after the parent became a residual."""

    manifest = [row for row in _baseline_rows() if row["account_id"] != PARENT]
    current = {
        "account_id": f"{PARENT}-s2",
        "strategy_id": "p123-ai-factor",
        "effective_start": "2026-01-30",
        "effective_end": None,
    }
    retained = [
        {
            "account_id": f"{PARENT}-s1",
            "strategy_id": NESTED[0],
            "effective_start": "2026-01-26",
            "effective_end": "2026-06-23",
        },
        {
            "account_id": f"{PARENT}-s3",
            "strategy_id": NESTED[1],
            "effective_start": "2026-06-22",
            "effective_end": "2026-07-22",
        },
    ]
    performance = [dict(row) for row in (*manifest, current)]
    return [*manifest, current, *retained], performance


def _controls() -> dict[str, dict[str, Any]]:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    template = next(
        row
        for row in contract["strategies"]
        if row["strategy_id"] == "echotrade-growth-pivots"
    )
    # Only the pinned nine: the shipped contract also names the retired records.
    contract["strategies"] = [
        row
        for row in contract["strategies"]
        if row["strategy_id"] in panel.CURRENT_PRODUCT_STRATEGY_IDS
    ]
    for strategy_id in NESTED:
        row = copy.deepcopy(template)
        row["strategy_id"] = strategy_id
        contract["strategies"].append(row)
    result: dict[str, dict[str, Any]] = panel._financial_mvp_product_controls(
        json.dumps(contract).encode("utf-8")
    )
    return result


def test_the_controls_accept_nested_history_only_strategies_beyond_the_nine() -> None:
    controls = _controls()

    assert len(controls) == 9 + len(NESTED)
    assert panel._history_only_strategy_ids(controls) == frozenset(NESTED)
    # the shipped contract names exactly the six retired history-only records
    # beyond the pinned perimeter
    assert panel._history_only_strategy_ids(
        panel._financial_mvp_product_controls()
    ) == frozenset(
        {
            "p123-conn-1911998-andreas-sc-ai-mq-live",
            "p123-conn-1951348-higher-beta-earnings",
            "p123-conn-1956961-ai-largecap-ls",
            "p123-conn-1958970-high-octane",
            "quantbase-crisis-factor-investing",
            "quantbase-single-index-leveraged-in-out",
        }
    )


@pytest.mark.parametrize(
    "edit",
    [
        lambda row: row.update(product_requirement="REQUIRED"),
        lambda row: row.update(lifecycle_status="ACTIVE"),
    ],
    ids=["required", "active"],
)
def test_a_nested_control_must_be_a_deprecated_exclusion(edit: Any) -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    row = copy.deepcopy(
        next(
            item
            for item in contract["strategies"]
            if item["strategy_id"] == "echotrade-growth-pivots"
        )
    )
    row["strategy_id"] = "nested-strategy"
    edit(row)
    contract["strategies"].append(row)

    with pytest.raises(
        panel.ControlPanelValidationError,
        match="nested history-only strategy control differs",
    ):
        panel._financial_mvp_product_controls(json.dumps(contract).encode("utf-8"))


def test_the_product_surface_is_thirteen_rows_with_the_sleeve_for_its_parent() -> None:
    controls = _controls()
    manifest, performance = _derived_rows()
    crosswalk = _crosswalk(_baseline_rows())

    product, retained, removed = panel._split_manifest_epoch_rows(
        manifest, panel._current_epoch_crosswalk_included_tuples(crosswalk), controls
    )

    assert len(product) == 13
    assert [row["strategy_id"] for row in retained] == [NESTED[0], NESTED[1]]
    assert removed == {(PARENT, "p123-ai-factor", "2026-01-22", None)}

    identities = panel._validate_current_product_epoch_surface(
        {"schema_version": 10, "rows": manifest},
        performance,
        controls,
        as_of_date="2026-08-13",
        current_epoch_crosswalk=crosswalk,
    )
    assert (f"{PARENT}-s2", "p123-ai-factor") in identities
    assert (PARENT, "p123-ai-factor") not in identities


def _surface(manifest: Any, performance: Any) -> Any:
    return panel._validate_current_product_epoch_surface(
        {"schema_version": 10, "rows": manifest},
        performance,
        _controls(),
        as_of_date="2026-08-13",
        current_epoch_crosswalk=_crosswalk(_baseline_rows()),
    )


def test_a_dropped_epoch_must_belong_to_the_parent_of_derived_rows() -> None:
    manifest, performance = _derived_rows()
    # a baseline epoch vanishes and nothing derives from its account
    manifest = [row for row in manifest if row["account_id"] != "qsi-0"]
    performance = [row for row in performance if row["account_id"] != "qsi-0"]

    with pytest.raises(panel.ControlPanelValidationError, match="identities differ"):
        _surface(manifest, performance)


def test_a_derived_row_must_hang_on_a_current_account_with_a_known_strategy() -> None:
    manifest, performance = _derived_rows()
    stranger = {
        "account_id": "account-unknown-s1",
        "strategy_id": NESTED[2],
        "effective_start": "2026-01-26",
        "effective_end": None,
    }
    with pytest.raises(panel.ControlPanelValidationError, match="identities differ"):
        _surface([*manifest, stranger], performance)
    unknown_strategy = {**stranger, "account_id": f"{PARENT}-s4", "strategy_id": "who"}
    with pytest.raises(panel.ControlPanelValidationError, match="identities differ"):
        _surface([*manifest, unknown_strategy], performance)


def test_the_performance_surface_must_equal_the_product_rows() -> None:
    manifest, performance = _derived_rows()
    # the residual parent is still reported as if it were a strategy epoch
    parent = dict(next(row for row in _baseline_rows() if row["account_id"] == PARENT))
    performance = [
        row for row in performance if row["account_id"] != f"{PARENT}-s2"
    ] + [parent]

    with pytest.raises(panel.ControlPanelValidationError, match="identities differ"):
        _surface(manifest, performance)


def test_the_browser_manifest_admits_derived_rows_on_current_accounts_only() -> None:
    controls = _controls()
    manifest, _performance = _derived_rows()
    accounts = {row["account_id"] for row in _baseline_rows()}

    panel._validate_current_product_account_manifest(
        {"rows": manifest}, controls, accounts
    )

    outside = {
        "account_id": "account-elsewhere-s1",
        "strategy_id": NESTED[0],
        "effective_start": "2026-01-26",
        "effective_end": None,
    }
    with pytest.raises(
        panel.ControlPanelValidationError, match="outside the product boundary"
    ):
        panel._validate_current_product_account_manifest(
            {"rows": [*manifest, outside]}, controls, accounts
        )
    # a derived row of an excluded owner strategy is no history-only sleeve
    echo = {
        "account_id": f"{PARENT}-s9",
        "strategy_id": "echotrade-growth-pivots",
        "effective_start": "2026-01-26",
        "effective_end": None,
    }
    with pytest.raises(
        panel.ControlPanelValidationError, match="outside the product boundary"
    ):
        panel._validate_current_product_account_manifest(
            {"rows": [*manifest, echo]}, controls, accounts
        )
