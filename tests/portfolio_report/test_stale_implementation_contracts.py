"""A stale implementation row is NOT_READY, not fatal (ruling rulings-qc1i-stale-contracts, N66-R).

The values keep the shape of the smoke-8 bytes: the contract pins a provider
ledger that is not the governed one, and its account figure differs from the
governed account-live series over the window it declares.  The row is replaced
by a closed NOT_READY object; every other difference stays fatal.
"""

from __future__ import annotations

from decimal import Decimal
import importlib.util
import json
from pathlib import Path
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


reporter = _load("portfolio_report_stale_test", "portfolio_report.py")
reconciliation = _load(
    "portfolio_implementation_reconciliation",
    "portfolio_implementation_reconciliation.py",
)
attribution = _load(
    "portfolio_implementation_return_attribution",
    "portfolio_implementation_return_attribution.py",
)

AI, GRIZZLY, TT = "p123-ai-factor", "grizzlybears-vtma", "tt-portfolio"
STRATEGIES = (GRIZZLY, AI, TT)
LEDGER_CONTRACT = "23768b65" + "0" * 56
LEDGER_GOVERNED = "8946efa9" + "1" * 56
DATES = ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08")
OLD = ("-0.0028962", "0.0125976854165033", "-0.0083439556684643", "-0.0332133263079033")
GOVERNED = (
    "-0.0028962",
    "0.012597685416503276790239893",
    "-0.0083439556684642926119059292",
    "-0.0309240051291309354865271400",
)
AS_OF = "2026-08-03"


def _rows(strategy_id: str, values: tuple[str, ...]) -> list[dict[str, str]]:
    return [
        {
            "date": observed,
            "evidence_layer": "ACCOUNT_LIVE",
            "return": value,
            "strategy_id": strategy_id,
        }
        for observed, value in zip(DATES, values, strict=True)
    ]


def _governed() -> dict[str, dict[str, str]]:
    return {
        strategy_id: {
            row["date"]: row["return"] for row in _rows(strategy_id, GOVERNED)
        }
        for strategy_id in STRATEGIES
    }


def _compound(values: tuple[str, ...]) -> float:
    result = Decimal(1)
    for value in values:
        result *= 1 + Decimal(value)
    return float(result - 1)


def _reconciliation_row(strategy_id: str, values: tuple[str, ...]) -> dict[str, Any]:
    row = {
        "return_reconciliation": {
            "account_cumulative_return": _compound(values),
            "account_minus_model_cumulative": -0.1,
            "model_cumulative_return": _compound(values) + 0.1,
            "observations": len(values),
            "observed_end": DATES[-1],
            "observed_start": DATES[0],
            "status": "TESTED_EXACT_COMMON_DATES",
        },
        "root_causes": [{"cause": "SYNTHETIC", "evidence": "t", "status": "CONFIRMED"}],
        "strategy_id": strategy_id,
        "verification": "VERIFIED",
    }
    return reconciliation.with_account_leg(
        row,
        _rows(strategy_id, values),
        strategy_id=strategy_id,
        accepted_as_of=reconciliation.date.fromisoformat(AS_OF),
    )


def _attribution_row(strategy_id: str, values: tuple[str, ...]) -> dict[str, Any]:
    account = _compound(values)
    row = {
        "bridge": {
            "account_return": account,
            "categories": [{"category": "R", "effect_pp": -10.0}],
            "gap_pp": -10.0,
            "model_return": account + 0.1,
            "status": "EXACT_OPERATIONAL_BRIDGE_NO_UNNAMED_RESIDUAL",
        },
        "observed_end": DATES[-1],
        "observed_start": DATES[0],
        "strategy_id": strategy_id,
        "verification": "VERIFIED_WITH_DECLARED_PROXY",
    }
    return attribution.with_account_leg(
        row,
        _rows(strategy_id, values),
        strategy_id=strategy_id,
        accepted_as_of=attribution.date.fromisoformat(AS_OF),
        model_basis="EXACT_MODEL_RETURNS",
    )


def _write(
    path: Path,
    kind: str,
    values: dict[str, tuple[str, ...]],
    *,
    ledger: str | None = LEDGER_CONTRACT,
    schema_version: int = 2,
    mutate: Any = None,
) -> None:
    builder = _reconciliation_row if kind == "rec" else _attribution_row
    strategies = []
    for strategy_id in STRATEGIES:
        row = builder(strategy_id, values[strategy_id])
        if schema_version == 1:
            row.pop("account_leg")
        strategies.append(row)
    pin: list[dict[str, Any]] = []
    if ledger is not None:
        pin = [
            {"artifact_id": "normalized/provider-ledger.json", "sha256": ledger}
            if kind == "rec"
            else {
                "filename": "provider-ledger.json",
                "sha256": ledger,
                "source_id": "provider-ledger",
            }
        ]
    payload: dict[str, Any] = {
        "as_of_date": AS_OF,
        "builder": "portfolio-implementation-reconciliation"
        if kind == "rec"
        else "portfolio-implementation-return-attribution",
        "schema_version": schema_version,
        "source_artifacts": pin,
        "strategies": strategies,
        "verification": "VERIFIED"
        if kind == "rec"
        else "VERIFIED_WITH_DECLARED_PARTIAL_CAUSAL_BOUNDARIES",
        ("reconciliation_id" if kind == "rec" else "attribution_id"): (
            "a" if kind == "rec" else "b"
        )
        * 64,
    }
    if mutate is not None:
        mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


REC_VALUES = {GRIZZLY: OLD, AI: GOVERNED, TT: OLD}
ATTR_VALUES = {GRIZZLY: OLD, AI: GOVERNED, TT: GOVERNED}


def _read_rec(path: Path, **overrides: Any) -> tuple[Any, Any]:
    arguments: dict[str, Any] = {
        "governed_account_returns": _governed(),
        "governed_provider_ledger_sha256": LEDGER_GOVERNED,
        "provider_return_policy": {
            "effective_start": "2026-08-14",
            "policy_id": "PROVIDER_ADMIT_OR_DECLARE_V1",
        },
    }
    arguments.update(overrides)
    return reporter._implementation_reconciliation_contract(
        path, portfolio_as_of="2026-09-17", **arguments
    )


def _read_attr(path: Path, **overrides: Any) -> tuple[Any, Any]:
    arguments: dict[str, Any] = {
        "governed_account_returns": _governed(),
        "governed_provider_ledger_sha256": LEDGER_GOVERNED,
        "provider_return_policy": None,
    }
    arguments.update(overrides)
    return reporter._implementation_return_attribution_contract(
        path, portfolio_as_of="2026-09-17", **arguments
    )


STALE_KEYS = {
    "status",
    "strategy_id",
    "contract_kind",
    "contract_id",
    "contract_sha256",
    "contract_as_of_date",
    "window_start",
    "window_end",
    "governed_account_return",
    "governed_account_basis",
    "governed_observations",
    "contract_provider_ledger_sha256",
    "governed_provider_ledger_sha256",
    "superseded_contract_account_return",
    "superseded_contract_account_basis",
    "reason",
    "repair_ref",
}


def test_f1_stale_rows_become_closed_not_ready_objects_and_the_rest_is_unchanged(
    tmp_path: Path,
) -> None:
    rec_path, attr_path = tmp_path / "rec.json", tmp_path / "attr.json"
    _write(rec_path, "rec", REC_VALUES)
    _write(attr_path, "attr", ATTR_VALUES)
    sealed_rec = json.loads(rec_path.read_text(encoding="utf-8"))["strategies"]
    sealed_attr = json.loads(attr_path.read_text(encoding="utf-8"))["strategies"]

    rec_rows, rec_ref = _read_rec(rec_path)
    attr_rows, attr_ref = _read_attr(attr_path)

    assert rec_ref["stale_strategy_ids"] == [GRIZZLY, TT]
    assert attr_ref["stale_strategy_ids"] == [GRIZZLY]
    for rows, sealed in ((rec_rows, sealed_rec), (attr_rows, sealed_attr)):
        for row in sealed:
            if row["strategy_id"] == AI or (
                rows is attr_rows and row["strategy_id"] == TT
            ):
                assert rows[row["strategy_id"]] == row  # published unchanged
    stale = rec_rows[GRIZZLY]
    assert set(stale) == STALE_KEYS
    assert stale["status"] == "NOT_READY_STALE_CONTRACT_LEDGER_VINTAGE"
    assert stale["contract_kind"] == "RECONCILIATION"
    assert stale["contract_id"] == "a" * 64
    assert stale["contract_sha256"] == rec_ref["sha256"]
    assert stale["contract_as_of_date"] == AS_OF
    assert (stale["window_start"], stale["window_end"]) == (DATES[0], DATES[-1])
    assert stale["governed_account_return"] == pytest.approx(
        _compound(GOVERNED), abs=1e-12
    )
    assert stale["governed_account_basis"] == "BASELINE_ACCOUNT_LIVE"
    assert stale["governed_observations"] == 4
    assert stale["contract_provider_ledger_sha256"] == LEDGER_CONTRACT
    assert stale["governed_provider_ledger_sha256"] == LEDGER_GOVERNED
    assert stale["superseded_contract_account_return"] == pytest.approx(
        _compound(OLD), abs=1e-12
    )
    assert stale["superseded_contract_account_basis"] == (
        "SUPERSEDED_LEDGER_VINTAGE_NOT_AN_ACCOUNT_RETURN"
    )
    assert stale["repair_ref"] == "N66-R"
    difference = (_compound(OLD) - _compound(GOVERNED)) * 100.0
    assert stale["reason"] == (
        f"Implementation reconciliation contract {'a' * 12} (as of {AS_OF}) was "
        f"built from provider ledger {LEDGER_CONTRACT[:12]}; the governed ledger is "
        f"{LEDGER_GOVERNED[:12]}. Its account return over {DATES[0]}..{DATES[-1]} "
        f"differs from the governed account-live series by {difference:+.4f} pp. "
        "No model return, gap, risk statistic or causal waterfall is published "
        "from it."
    )
    assert attr_rows[GRIZZLY]["contract_kind"] == "RETURN_ATTRIBUTION"
    assert attr_rows[GRIZZLY]["reason"].startswith(
        f"Implementation return attribution contract {'b' * 12}"
    )
    assert "model_return" not in json.dumps(stale)
    for forbidden in ("bridge", "return_reconciliation", "account_leg", "categories"):
        assert forbidden not in stale


def test_f1_legacy_v1_contracts_classify_the_same_way(tmp_path: Path) -> None:
    rec_path = tmp_path / "rec.json"
    _write(rec_path, "rec", REC_VALUES, schema_version=1)

    rows, ref = _read_rec(rec_path)

    assert ref["stale_strategy_ids"] == [GRIZZLY, TT]
    assert rows[GRIZZLY]["status"] == "NOT_READY_STALE_CONTRACT_LEDGER_VINTAGE"
    assert rows[AI]["strategy_id"] == AI and "status" not in rows[AI]


def test_f2_same_ledger_with_a_different_figure_is_still_fatal(tmp_path: Path) -> None:
    path = tmp_path / "rec.json"
    _write(path, "rec", REC_VALUES, ledger=LEDGER_GOVERNED)

    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation account leg differs from the governed account-live "
        "series: grizzlybears-vtma",
    ):
        _read_rec(path)


@pytest.mark.parametrize("kind", ["missing", "duplicated", "not_hex"])
def test_f3_a_missing_or_ambiguous_ledger_pin_is_fatal(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "rec.json"

    def mutate(payload: dict[str, Any]) -> None:
        pins = payload["source_artifacts"]
        if kind == "missing":
            payload["source_artifacts"] = []
        elif kind == "duplicated":
            pins.append({**pins[0], "sha256": "3" * 64})
        else:
            pins[0]["sha256"] = "not-a-digest"

    _write(path, "rec", REC_VALUES, mutate=mutate)

    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation account leg differs from the governed account-live "
        "series",
    ):
        _read_rec(path)


def test_f3_attribution_ledger_pin_is_read_by_its_source_id(tmp_path: Path) -> None:
    path = tmp_path / "attr.json"
    _write(path, "attr", ATTR_VALUES, ledger=None)

    with pytest.raises(
        reporter.ReportValidationError, match="differs from the governed"
    ):
        _read_attr(path)


def test_f4_a_leg_that_is_not_its_published_figure_is_fatal_even_when_stale(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rec.json"

    def mutate(payload: dict[str, Any]) -> None:
        payload["strategies"][0]["return_reconciliation"][
            "account_cumulative_return"
        ] += 0.0023

    _write(path, "rec", REC_VALUES, mutate=mutate)
    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation published account return differs from its leg",
    ):
        _read_rec(path)

    def extra_key(payload: dict[str, Any]) -> None:
        payload["strategies"][0]["account_leg"]["note"] = "x"

    _write(path, "rec", REC_VALUES, mutate=extra_key)
    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation account leg differs from its contract",
    ):
        _read_rec(path)

    _write(
        path,
        "rec",
        REC_VALUES,
        schema_version=2,
        mutate=lambda p: p["strategies"][0].pop("account_leg"),
    )
    with pytest.raises(
        reporter.ReportValidationError, match="implementation account leg is absent"
    ):
        _read_rec(path)


def test_f5_a_window_with_no_governed_date_is_fatal(tmp_path: Path) -> None:
    path = tmp_path / "rec.json"
    _write(path, "rec", REC_VALUES)
    governed = _governed()
    governed[GRIZZLY] = {}

    with pytest.raises(
        reporter.ReportValidationError,
        match="implementation account leg differs from the governed account-live "
        "series: grizzlybears-vtma",
    ):
        _read_rec(path, governed_account_returns=governed)


def test_without_the_governed_ledger_identity_a_difference_stays_fatal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rec.json"
    _write(path, "rec", REC_VALUES)

    with pytest.raises(
        reporter.ReportValidationError, match="differs from the governed"
    ):
        _read_rec(path, governed_provider_ledger_sha256=None)


def test_accepted_contract_reports_no_stale_rows_and_the_pre_policy_label(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rec.json"
    _write(path, "rec", {GRIZZLY: GOVERNED, AI: GOVERNED, TT: GOVERNED})

    rows, ref = _read_rec(path)

    assert ref["stale_strategy_ids"] == []
    assert set(rows) == set(STRATEGIES)
    assert ref["provider_return_policy"] == {
        "effective_start": "2026-08-14",
        "policy_id": "PROVIDER_ADMIT_OR_DECLARE_V1",
    }
    assert ref["pre_policy_strategy_ids"] == [AI]


def test_pre_policy_label_needs_the_contract_to_predate_the_policy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rec.json"
    _write(path, "rec", {GRIZZLY: GOVERNED, AI: GOVERNED, TT: GOVERNED})

    _, after = _read_rec(
        path,
        provider_return_policy={
            "effective_start": "2026-07-01",
            "policy_id": "PROVIDER_ADMIT_OR_DECLARE_V1",
        },
    )
    _, absent = _read_rec(path, provider_return_policy=None)

    assert after["pre_policy_strategy_ids"] == []
    assert (
        absent["pre_policy_strategy_ids"] == []
        and absent["provider_return_policy"] is None
    )


def test_only_p123_ai_factor_is_ever_labelled_pre_policy(tmp_path: Path) -> None:
    """Pins `_implementation_reference_extras`'s hardcoded ``"p123-ai-factor"`` literal.

    `tools/portfolio_report.py` (``_implementation_reference_extras``) names
    ``p123-ai-factor`` by string literal rather than deriving "pre-policy" from
    the contract's own data. Every other predicate the function checks (not
    stale, the contract predates the policy's effective_start, the model
    comparison is available) is satisfied by GRIZZLY and TT here exactly as it
    is by AI -- only the literal excludes them. If a second strategy is ever
    exempted from the provider-return policy in production, this hardcoded
    check will silently leave it unlabelled; this test must fail and be
    revisited (do not widen the literal to a set without a ruling) the day
    that happens.
    """
    path = tmp_path / "rec.json"
    _write(path, "rec", {GRIZZLY: GOVERNED, AI: GOVERNED, TT: GOVERNED})

    _, ref = _read_rec(path)

    assert ref["stale_strategy_ids"] == []  # none of the three is stale here
    assert ref["pre_policy_strategy_ids"] == [AI]
    assert GRIZZLY not in ref["pre_policy_strategy_ids"]
    assert TT not in ref["pre_policy_strategy_ids"]


@pytest.mark.parametrize(
    ("reconciliation_stale", "attribution_stale", "expected"),
    [
        (True, True, "NOT_READY_STALE_CONTRACT_LEDGER_VINTAGE"),
        (True, False, "PARTIAL_CAUSAL_ATTRIBUTION_RECONCILIATION_NOT_READY"),
        (False, True, "PARTIAL_RECONCILIATION_ATTRIBUTION_NOT_READY"),
        (False, False, "VERIFIED_CAUSAL_RETURN_ATTRIBUTION"),
    ],
)
def test_strategy_status_follows_which_contract_is_stale(
    reconciliation_stale: bool, attribution_stale: bool, expected: str
) -> None:
    stale = {"status": "NOT_READY_STALE_CONTRACT_LEDGER_VINTAGE"}
    ready_reconciliation = {
        "return_reconciliation": {"status": "TESTED_EXACT_COMMON_DATES"}
    }
    ready_attribution = {
        "bridge": {"status": "EXACT_OPERATIONAL_BRIDGE_NO_UNNAMED_RESIDUAL"}
    }

    status = reporter._implementation_status(
        "ACCEPTED_ACCOUNT_LIVE",
        stale if reconciliation_stale else ready_reconciliation,
        stale if attribution_stale else ready_attribution,
    )

    assert status == expected
    assert not status.startswith(("VERIFIED", "ACCEPTED", "AVAILABLE")) or (
        not reconciliation_stale and not attribution_stale
    )


def test_status_without_any_contract_keeps_the_history_status() -> None:
    assert reporter._implementation_status("ACCEPTED_ACCOUNT_LIVE", {}, {}) == (
        "ACCEPTED_ACCOUNT_LIVE"
    )
    assert (
        reporter._implementation_status(
            "ACCEPTED_ACCOUNT_LIVE",
            {"return_reconciliation": {"status": "TESTED_EXACT_COMMON_DATES"}},
            {},
        )
        == "VERIFIED_TRADE_AND_RETURN_DECONSTRUCTION"
    )
    assert (
        reporter._implementation_status(
            "ACCEPTED_ACCOUNT_LIVE",
            {"return_reconciliation": {"status": "UNAVAILABLE_POLICY_NOT_ADMITTED"}},
            {"bridge": {"status": "UNAVAILABLE_POLICY_NOT_ADMITTED"}},
        )
        == "UNAVAILABLE_POLICY_NOT_ADMITTED"
    )


def test_a_stale_object_is_not_read_as_a_comparison_by_the_attribution_merge(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rec.json"
    _write(path, "rec", REC_VALUES)
    rows, _ = _read_rec(path)

    result = reporter._merge_attribution(
        [{"strategy_id": GRIZZLY, "implementation_reconciliation": rows[GRIZZLY]}],
        {
            "benchmark_assignments": [{"strategy_id": GRIZZLY}],
            "comparisons": [
                {
                    "strategy_id": GRIZZLY,
                    "status": "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED",
                    "metrics": {"model_return": 0.1, "observations": 200},
                }
            ],
            "costs": [{"strategy_id": GRIZZLY}],
        },
    )[0]

    assert result["implementation_reconciliation"] == rows[GRIZZLY]
    assert result["provider_comparison_status"] == (
        "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED"
    )


def _catalog() -> dict[str, Any]:
    return {
        "as_of_date": "2026-09-17",
        "source_artifacts": [
            {
                "schema_name": "generation-manifest",
                "sha256": "4" * 64,
                "source_key": "provider-ledger-generation",
            },
            {
                "schema_name": "provider-ledger-catalog",
                "sha256": LEDGER_GOVERNED,
                "source_key": "provider-ledger-generation",
            },
        ],
        "strategy_returns": [
            row for strategy_id in STRATEGIES for row in _rows(strategy_id, GOVERNED)
        ],
    }


def test_report_wiring_classifies_against_the_catalog_ledger_identity(
    tmp_path: Path,
) -> None:
    rec_path, attr_path = tmp_path / "rec.json", tmp_path / "attr.json"
    _write(rec_path, "rec", REC_VALUES)
    _write(attr_path, "attr", ATTR_VALUES)

    rec_rows, rec_ref, attr_rows, attr_ref = reporter._implementation_contracts(
        _catalog(), tmp_path, None, rec_path, attr_path
    )

    assert rec_ref["stale_strategy_ids"] == [GRIZZLY, TT]
    assert attr_ref["stale_strategy_ids"] == [GRIZZLY]
    assert rec_rows[AI]["strategy_id"] == AI and attr_rows[TT]["strategy_id"] == TT

    pinned = tmp_path / "pinned.json"
    _write(pinned, "rec", REC_VALUES, ledger=LEDGER_GOVERNED)
    with pytest.raises(
        reporter.ReportValidationError, match="differs from the governed"
    ):
        reporter._implementation_contracts(_catalog(), tmp_path, None, pinned, None)
