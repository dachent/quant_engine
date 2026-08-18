from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "portfolio_financial_mvp_contract.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_financial_mvp_contract", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
contract_tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = contract_tool
SPEC.loader.exec_module(contract_tool)

CONTRACT_PATH = ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
SCHEMA_PATH = (
    ROOT / "schemas" / "portfolio" / "financial-mvp-source-contract.schema.json"
)
REGISTRY_PATH = ROOT / "docs" / "analytics" / "current-strategy-decomposition-plan.json"
GENERATED_ROOT = ROOT / "generated" / "financial-mvp"


def _contract() -> dict[str, object]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _registry() -> dict[str, object]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _validate(contract: dict[str, object]) -> dict[str, object]:
    return contract_tool.validate_contract(contract, registry=_registry())


def _source(contract: dict[str, object], source_id: str) -> dict[str, object]:
    rows = contract["source_segments"]
    assert isinstance(rows, list)
    return next(row for row in rows if row["source_id"] == source_id)


def _outcome(contract: dict[str, object], outcome_id: str) -> dict[str, object]:
    rows = contract["financial_outcomes"]
    assert isinstance(rows, list)
    return next(row for row in rows if row["outcome_id"] == outcome_id)


def _github_comment(comment_id: int, body: str = "") -> dict[str, object]:
    return {
        "id": comment_id,
        "html_url": (
            "https://github.com/dachent/quant_engine/issues/330#issuecomment-"
            f"{comment_id}"
        ),
        "body": body,
        "user": {"login": "dachent"},
        "author_association": "OWNER",
        "created_at": "2026-08-17T12:00:00Z",
        "updated_at": "2026-08-17T12:00:00Z",
    }


def _owner_evidence_comments(
    contract: dict[str, object],
) -> dict[int, dict[str, object]]:
    invested = _outcome(contract, "INVESTED_CAPITAL")["calculation_contract"]
    receipts = invested["owner_decision_receipts"]
    comments: dict[int, dict[str, object]] = {}
    for receipt in receipts.values():
        comments[receipt["comment_id"]] = _github_comment(receipt["comment_id"])
    digest_ids = {receipt["digest_comment_id"] for receipt in receipts.values()}
    for digest_id in digest_ids:
        decision_ids = [
            decision_id
            for decision_id, receipt in receipts.items()
            if receipt["digest_comment_id"] == digest_id
        ]
        record = contract_tool.owner_receipt_evidence_record(contract, decision_ids)
        body = (
            f"{contract_tool._OWNER_RECEIPT_MARKER}\n\n```json\n"
            f"{json.dumps(record, indent=2, sort_keys=True)}\n```\n"
        )
        comments[digest_id] = _github_comment(digest_id, body)
    return comments


def _schema_errors(
    node: dict[str, object],
    value: object,
    root: dict[str, object],
    path: str = "$",
) -> list[str]:
    return contract_tool.schema_errors(node, value, root, path)


def test_authoritative_contract_schema_and_views_exist() -> None:
    assert CONTRACT_PATH.is_file()
    assert SCHEMA_PATH.is_file()
    assert (GENERATED_ROOT / "current-product.md").is_file()
    assert (GENERATED_ROOT / "source-matrix.json").is_file()


def test_contract_is_valid_and_has_stable_identity() -> None:
    contract = contract_tool.load_contract(CONTRACT_PATH, REGISTRY_PATH)
    assert contract_tool.contract_digest(contract) == (
        "223566d79a949c1729d5b6e110152e6db42716ba186b5d4bb14e601b06056192"
    )


def test_contract_validates_against_the_checked_in_schema() -> None:
    contract = _contract()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert _schema_errors(schema, contract, schema) == []

    extra = copy.deepcopy(contract)
    extra["shadow_manifest"] = {}
    assert any(
        "unexpected shadow_manifest" in error
        for error in _schema_errors(schema, extra, schema)
    )

    missing = copy.deepcopy(contract)
    del missing["source_segments"][0]["capture_status"]
    assert any(
        "missing capture_status" in error
        for error in _schema_errors(schema, missing, schema)
    )

    shadow_calculation = copy.deepcopy(contract)
    shadow_calculation["financial_outcomes"][0]["calculation_contract"][
        "shadow_acceptance"
    ] = "PASS"
    assert any(
        "unexpected shadow_acceptance" in error
        for error in _schema_errors(schema, shadow_calculation, schema)
    )


def test_production_loader_enforces_the_checked_in_schema(tmp_path: Path) -> None:
    broken = _contract()
    del broken["source_segments"][0]["strategy_version"]
    contract_path = tmp_path / "broken-contract.json"
    contract_path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="schema validation failed"
    ):
        contract_tool.load_contract(contract_path, REGISTRY_PATH, SCHEMA_PATH)


def test_schema_is_strict_without_frozen_strategy_or_provider_counts() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["source_segment"]["additionalProperties"] is False
    assert schema["$defs"]["strategy"]["additionalProperties"] is False
    strategies = schema["properties"]["strategies"]
    assert strategies["minItems"] == 1
    assert "maxItems" not in strategies


def test_strategy_perimeter_matches_discoverable_registry_without_disappearing_rows() -> (
    None
):
    contract = _contract()
    registry = _registry()
    expected = [row["strategy_id"] for row in registry["strategies"]]
    actual = [row["strategy_id"] for row in contract["strategies"]]
    assert actual == expected
    assert all(
        row["product_requirement"] == "REQUIRED" for row in contract["strategies"]
    )
    assert all(
        row["readiness"] == "REQUIRED_NOT_READY" for row in contract["strategies"]
    )
    hwm_exclusions = {
        row["strategy_id"]
        for row in contract["strategies"]
        if row["outcome_dispositions"]["AS_HELD_COMPOSITE_HWM"]
        == "EXCLUDED_BY_ACCEPTED_POLICY"
    }
    assert hwm_exclusions == {
        "composer-holy-grail",
        "echotrade-growth-pivots",
    }
    assert all(
        row["outcome_dispositions"]["INVESTED_CAPITAL"] == "REQUIRED"
        and row["outcome_dispositions"]["DECOMPOSITION"] == "REQUIRED"
        and row["outcome_dispositions"]["DEPENDENCE"] == "REQUIRED"
        for row in contract["strategies"]
    )


def test_all_four_outcomes_are_required_and_generic_unavailable_is_not_accepted() -> (
    None
):
    contract = _contract()
    outcomes = contract["financial_outcomes"]
    assert [row["outcome_id"] for row in outcomes] == list(contract_tool.OUTCOME_IDS)
    assert all(row["product_requirement"] == "REQUIRED" for row in outcomes)
    assert all(row["blockers"] for row in outcomes)
    assert all("UNAVAILABLE" not in row["accepted_statuses"] for row in outcomes)


def test_invested_capital_is_not_nav_hwm_or_gains() -> None:
    invested = _outcome(_contract(), "INVESTED_CAPITAL")["calculation_contract"]
    assert invested["definition"] == (
        "CUMULATIVE_EXTERNAL_CONTRIBUTIONS_MINUS_WITHDRAWALS"
    )
    assert invested["gains_treatment"] == "EXCLUDED"
    assert invested["internal_transfer_aggregate_effect"] == "ZERO"
    assert set(invested["flow_classifications"]) == {
        "OPENING_CONTRIBUTED_BASIS",
        "IN_KIND_FUNDING",
        "EXTERNAL_CONTRIBUTION",
        "EXTERNAL_WITHDRAWAL",
        "INTERNAL_TRANSFER",
        "DIVIDEND_INTEREST_REINVESTMENT",
        "FEE_TAX_REBATE_EXPENSE",
        "MARGIN_FINANCING",
        "PREDECESSOR_TRANSITION",
        "CORRECTION_REVERSAL",
    }
    treatments = invested["classification_treatments"]
    assert treatments["EXTERNAL_CONTRIBUTION"]["selected_treatment"] == "INCREASE"
    assert treatments["EXTERNAL_WITHDRAWAL"]["selected_treatment"] == "DECREASE"
    assert treatments["INTERNAL_TRANSFER"]["selected_treatment"] == (
        "BY_STRATEGY_OFFSET_AND_AGGREGATE_ZERO"
    )
    assert treatments["DIVIDEND_INTEREST_REINVESTMENT"]["selected_treatment"] == (
        "NO_INVESTED_CAPITAL_EFFECT"
    )
    assert treatments["IN_KIND_FUNDING"]["decision_status"] == (
        "SETTLED_BY_EXISTING_POLICY"
    )
    assert invested["flow_timing"]["selected_treatment"] == (
        "EXACT_TIMESTAMP_BOUNDARY_WHEN_AVAILABLE; "
        "DATE_ONLY_END_OF_DAY_WITH_INFERRED_TIMING"
    )
    assert invested["fx_base_currency"]["decision_status"] == "APPROVED"
    assert invested["negative_net_contributions"]["selected_treatment"] == (
        "ALLOW_SIGNED_RESULT_WITH_NO_ZERO_FLOOR"
    )
    assert {
        treatments[flow_class]["decision_status"]
        for flow_class in (
            "MARGIN_FINANCING",
            "PREDECESSOR_TRANSITION",
            "CORRECTION_REVERSAL",
        )
    } == {"APPROVED"}
    assert invested["shared_flow_contract"]["current_policy_id"] == "PORT-PERF-1"
    assert invested["change_control"]["downstream_issues"] == [318, 324, 331, 332]


def test_leverage_policy_preserves_returns_without_inflating_invested_capital() -> None:
    invested = _outcome(_contract(), "INVESTED_CAPITAL")["calculation_contract"]
    leverage = invested["leverage_accounting"]
    vector = leverage["synthetic_vector"]
    assert leverage["status"] == "APPROVED"
    assert set(leverage["required_facts"]) == {
        "GROSS_EXPOSURE",
        "BORROWED_PRINCIPAL",
        "FINANCING_COST",
        "NAV",
        "OWNER_EXTERNAL_FLOWS",
    }
    assert set(leverage["affected_metrics"]) == {
        "TOTAL_RETURN",
        "TWR",
        "MWR",
        "HWM_DRAWDOWN",
        "DECOMPOSITION_DEPENDENCE",
    }
    assert vector["owner_contributed_capital"] == "100"
    assert vector["borrowed_principal"] == "50"
    assert vector["gross_assets_end"] == "164"
    assert vector["ending_nav"] == "114"
    assert vector["invested_capital"] == "100"
    assert vector["total_return"] == vector["twr"] == vector["mwr_one_year"] == "0.14"
    contract = _contract()
    leverage_classes = {
        "FINANCING_LIABILITY",
        "LEVERAGE_EXPOSURE",
        "FINANCING_COST",
    }
    for source_id in contract_tool.REQUIRED_ACCOUNT_PROVIDER_SOURCE_IDS:
        assert leverage_classes <= set(_source(contract, source_id)["data_classes"])

    changed = _contract()
    changed_vector = _outcome(changed, "INVESTED_CAPITAL")["calculation_contract"][
        "leverage_accounting"
    ]["synthetic_vector"]
    changed_vector["total_return"] = "0.15"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="leverage synthetic value differs: total_return",
    ):
        _validate(changed)

    missing_source_fact = _contract()
    ibkr = _source(missing_source_fact, "ibkr-account-generation")
    ibkr["data_classes"].remove("FINANCING_LIABILITY")
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="account source lacks leverage facts: ibkr-account-generation",
    ):
        _validate(missing_source_fact)


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        (
            "MARGIN_PRINCIPAL",
            "MARGIN_PRINCIPAL_COUNTS_AS_OWNER_INVESTED_CAPITAL_AND_EXTERNAL_FLOW",
        ),
        (
            "TOTAL_RETURN",
            "EXCLUDE_FINANCING_COST_AND_USE_UNLEVERED_ASSETS",
        ),
        (
            "DECOMPOSITION_DEPENDENCE",
            "IGNORE_LEVERAGED_RETURNS_AND_EXPOSURE",
        ),
    ],
)
def test_unapproved_leverage_semantic_reversal_fails(mutation: str, value: str) -> None:
    baseline = _contract()
    unchanged_owner_evidence = _owner_evidence_comments(baseline)
    changed = copy.deepcopy(baseline)
    invested = _outcome(changed, "INVESTED_CAPITAL")["calculation_contract"]
    leverage = invested["leverage_accounting"]
    if mutation == "MARGIN_PRINCIPAL":
        invested["classification_treatments"]["MARGIN_FINANCING"][
            "selected_treatment"
        ] = value
        leverage["invested_capital_treatment"] = value
    else:
        leverage["affected_metrics"][mutation] = value
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="owner decision receipt differs: MARGIN_AND_LEVERAGE",
    ):
        _validate(changed)
    receipts = invested["owner_decision_receipts"]
    receipts["MARGIN_AND_LEVERAGE"]["payload_sha256"] = (
        contract_tool.owner_decision_receipt_digests(changed)["MARGIN_AND_LEVERAGE"]
    )
    _validate(changed)
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="owner digest evidence does not approve current decision payloads",
    ):
        contract_tool.reconcile_owner_receipt_evidence(
            changed, unchanged_owner_evidence
        )


def test_current_owner_receipt_evidence_reconciles() -> None:
    contract = _contract()
    comments = _owner_evidence_comments(contract)
    contract_tool.reconcile_owner_receipt_evidence(contract, comments)

    not_owner = copy.deepcopy(comments)
    digest_comment_id = next(
        receipt["digest_comment_id"]
        for receipt in _outcome(contract, "INVESTED_CAPITAL")["calculation_contract"][
            "owner_decision_receipts"
        ].values()
    )
    not_owner[digest_comment_id]["author_association"] = "CONTRIBUTOR"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="owner evidence author differs",
    ):
        contract_tool.reconcile_owner_receipt_evidence(contract, not_owner)

    edited = copy.deepcopy(comments)
    edited[digest_comment_id]["updated_at"] = "2026-08-17T12:01:00Z"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="owner evidence comment was edited",
    ):
        contract_tool.reconcile_owner_receipt_evidence(contract, edited)


def test_decomposition_and_dependence_surfaces_are_complete() -> None:
    contract = _contract()
    decomposition = _outcome(contract, "DECOMPOSITION")["calculation_contract"]
    assert decomposition["lenses"] == list(contract_tool.DECOMPOSITION_LENSES)
    assert decomposition["evidence_modes"] == list(contract_tool.EVIDENCE_MODES)
    assert set(decomposition["surfaces"]) == {"STATIC", "DYNAMIC_FLS"}
    dependence = _outcome(contract, "DEPENDENCE")["calculation_contract"]
    assert dependence["evidence_modes"] == list(contract_tool.EVIDENCE_MODES)
    assert dependence["horizons"] == list(contract_tool.DEPENDENCE_HORIZONS)
    assert {
        "PEARSON",
        "SPEARMAN",
        "ROLLING",
        "DOWNSIDE",
        "DRAWDOWN_REGIMES",
        "JOINT_TAIL",
        "CONCEPTUAL_PRIOR",
        "POSTERIOR_STRESS_PSD",
        "NO_DIVERSIFICATION_CREDIT",
        "CENTRAL_CASE_LOSS",
        "CORRELATION_ONE_BOUND",
        "PERFORMANCE",
        "ECONOMIC_DRIVER_CONCENTRATION",
        "IMPLEMENTATION_CONCENTRATION",
    } == set(dependence["analyses"])
    assert dependence["splice"] == (
        "VERSION_COMPATIBLE_PREHISTORY_BEFORE_LIVE_INCEPTION_ONLY"
    )


def test_source_matrix_covers_every_mandated_class_and_provider_family() -> None:
    contract = _contract()
    sources = contract["source_segments"]
    classes = {
        data_class for source in sources for data_class in source["data_classes"]
    }
    assert contract_tool.REQUIRED_SOURCE_CLASSES <= classes
    source_ids = {source["source_id"] for source in sources}
    assert contract_tool.REQUIRED_PROVIDER_SOURCE_IDS <= source_ids
    required_fields = {
        "absence_behavior",
        "acquisition",
        "admission",
        "authority",
        "blockers",
        "capture_status",
        "consumers",
        "current_generation",
        "data_classes",
        "endpoint_or_artifact_selector",
        "evidence_layer",
        "existence_status",
        "freshness",
        "interval",
        "invalidated_outputs",
        "owner_policy_decision",
        "product_requirement",
        "readiness",
        "schema",
        "source_id",
        "source_version",
        "strategy_version",
        "subjects",
        "temporal",
        "units",
    }
    assert all(set(source) == required_fields for source in sources)


def test_history_existence_capture_and_evidence_layers_are_separate() -> None:
    contract = _contract()
    p123 = _source(contract, "portfolio123-model-generation")
    assert p123["existence_status"] == "EXISTS"
    assert p123["capture_status"] == "NOT_CAPTURED"
    assert p123["evidence_layer"] == "PROVIDER_LIVE"
    owner_live = _source(contract, "owner-account-live-history")
    assert owner_live["evidence_layer"] == "ACCOUNT_LIVE"
    assert (
        "NEVER_RELABEL_AS_ACCOUNT_LIVE"
        in _source(contract, "provider-track-record")["interval"]["splice_policy"]
    )
    assert {"PROVIDER_TRACK_RECORD", "PROVIDER_LIVE", "ACCOUNT_LIVE"} <= {
        row["evidence_layer"] for row in contract["source_segments"]
    }


def test_outcome_semantics_and_provider_layers_are_runtime_gates() -> None:
    contract = copy.deepcopy(_contract())
    _outcome(contract, "DEPENDENCE")["calculation_contract"]["splice"] = (
        "ALLOW_POST_LIVE_BACKTEST_FILL"
    )
    with pytest.raises(contract_tool.FinancialMvpContractError, match="splice rule"):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    _outcome(contract, "INVESTED_CAPITAL")["calculation_contract"]["frequency"] = (
        "CURRENT_ONLY"
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="daily and current"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    _source(contract, "portfolio123-model-generation")["evidence_layer"] = (
        "PROVIDER_TRACK_RECORD"
    )
    with pytest.raises(contract_tool.FinancialMvpContractError, match="PROVIDER_LIVE"):
        _validate(contract)


def test_every_required_outcome_has_a_source_route() -> None:
    contract = copy.deepcopy(_contract())
    for row in contract["source_segments"]:
        row["consumers"] = [
            consumer for consumer in row["consumers"] if consumer != "INVESTED_CAPITAL"
        ]
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="INVESTED_CAPITAL"
    ):
        _validate(contract)


def test_per_strategy_history_and_decomposition_routes_fail_on_omission() -> None:
    contract = copy.deepcopy(_contract())
    provider_live = _source(contract, "provider-live-history")
    provider_live["subjects"]["strategy_ids"].remove("qsi")
    with pytest.raises(contract_tool.FinancialMvpContractError, match="qsi"):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    factors = _source(contract, "public-economic-and-industry-factors")
    factors["subjects"]["strategy_ids"].remove("composer-holy-grail")
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="composer-holy-grail"
    ):
        _validate(contract)


def test_source_strategy_version_and_cost_are_explicit() -> None:
    contract = _contract()
    assert all("strategy_version" in row for row in contract["source_segments"])
    assert any(
        "STRATEGY_COST" in row["data_classes"] for row in contract["source_segments"]
    )

    mutated = copy.deepcopy(contract)
    _source(mutated, "strategy-backtests")["strategy_version"]["version_refs"] = {
        "qsi": "invented-v1"
    }
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="asserts unresolved"
    ):
        _validate(mutated)


def test_proposed_market_candidates_are_not_promoted_to_authority() -> None:
    contract = _contract()
    for source_id in (
        "public-economic-and-industry-factors",
        "public-etf-style-sector-and-market-prices",
    ):
        row = _source(contract, source_id)
        assert row["authority"].endswith("PENDING_328")
        assert row["acquisition"]["mode"] == (
            "NO_ACQUISITION_BEFORE_OWNER_SOURCE_APPROVAL"
        )


def test_public_factor_run_and_publication_timing_propagate_to_every_view() -> None:
    contract = _contract()
    source = _source(contract, "public-economic-and-industry-factors")
    expected_cadence = (
        "DAILY_TO_WEEKLY_FULL_PIPELINE; CAPTURE_EVERY_SCHEDULED_RUN"
    )
    expected_rule = (
        "CURRENT_RUN_CAPTURE_REQUIRED; MONTHLY_PROVIDER_PUBLICATION; "
        "REQUIRE_DAILY_OBSERVATIONS_THROUGH_LATEST_MONTH_END_WHOSE_45_"
        "CALENDAR_DAY_ALLOWANCE_HAS_ELAPSED"
    )
    assert source["temporal"] == {
        "timezone": "PROVIDER_DECLARED",
        "calendar": "WEEKDAY_SOURCE_CALENDAR",
        "cadence": expected_cadence,
    }
    assert source["freshness"] == {
        "max_age_days": 0,
        "publication_lag_days": 45,
        "rule": expected_rule,
    }

    views = contract_tool.render_views(contract)
    matrix = json.loads(views.source_matrix_json)
    rows = [
        row
        for row in matrix["rows"]
        if row["source_id"] == "public-economic-and-industry-factors"
    ]
    assert len(rows) == 135
    assert all(
        row["cadence"] == expected_cadence
        and row["max_age_days"] == 0
        and row["publication_lag_days"] == 45
        and row["freshness_rule"] == expected_rule
        and row["owner_policy_decision"] == source["owner_policy_decision"]
        for row in rows
    )
    assert b"## Source timing" in views.summary_markdown
    assert expected_cadence.encode() in views.summary_markdown
    assert expected_rule.encode() in views.summary_markdown
    source_bytes = json.dumps(source, sort_keys=True).encode()
    assert b"DAILY_WITH_PROVIDER_LAG" not in source_bytes
    assert b"SOURCE_SPECIFIC_MAX_AGE_AND_PROVIDER_LAG" not in source_bytes


def test_delete_or_rename_any_required_outcome_fails() -> None:
    missing = copy.deepcopy(_contract())
    missing["financial_outcomes"].pop()
    with pytest.raises(contract_tool.FinancialMvpContractError, match="four financial"):
        _validate(missing)

    renamed = copy.deepcopy(_contract())
    renamed["financial_outcomes"][0]["outcome_id"] = "ARTIFACT_GENERATED"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="four financial"):
        _validate(renamed)


def test_generic_unavailable_cannot_accept_an_outcome_or_source() -> None:
    contract = copy.deepcopy(_contract())
    contract["financial_outcomes"][0]["accepted_statuses"] = ["UNAVAILABLE"]
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="generic UNAVAILABLE"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    contract["source_segments"][0]["absence_behavior"] = "DISCLOSE_AND_CONTINUE"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="fail closed"):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    contract["source_segments"][0]["absence_behavior"] = "UNAVAILABLE"
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="generic UNAVAILABLE"
    ):
        _validate(contract)


def test_missing_required_source_class_or_provider_family_fails() -> None:
    contract = copy.deepcopy(_contract())
    contract["source_segments"] = [
        row
        for row in contract["source_segments"]
        if row["source_id"] != "portfolio-security-master"
    ]
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="SECURITY_MASTER"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    contract["source_segments"] = [
        row
        for row in contract["source_segments"]
        if row["source_id"] != "alpaca-account-generation"
    ]
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="provider families"
    ):
        _validate(contract)


def test_duplicate_source_and_unknown_consumer_fail() -> None:
    contract = copy.deepcopy(_contract())
    contract["source_segments"].append(copy.deepcopy(contract["source_segments"][0]))
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="duplicate source"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    contract["source_segments"][0]["consumers"].append("TOPOLOGY_ONLY_PASS")
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="unknown consumers"
    ):
        _validate(contract)


def test_required_not_ready_needs_blocker_and_ready_needs_generation() -> None:
    contract = copy.deepcopy(_contract())
    contract["source_segments"][0]["blockers"] = []
    with pytest.raises(contract_tool.FinancialMvpContractError, match="lacks blocker"):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    strategy = contract["strategies"][0]
    strategy["readiness"] = "READY"
    strategy["blockers"] = []
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="version/membership"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    source = _source(contract, "portfolio123-model-generation")
    source["readiness"] = "READY"
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="lacks a current generation"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    source = _source(contract, "current-strategy-registry")
    source["readiness"] = "READY"
    source["capture_status"] = "UNRESOLVED"
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="uncaptured source"
    ):
        _validate(contract)


def test_capture_generation_invariants_fail_closed() -> None:
    contract = copy.deepcopy(_contract())
    source = _source(contract, "portfolio123-model-generation")
    source["capture_status"] = "CAPTURED_GOVERNED"
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="immutable generation"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    source = _source(contract, "portfolio123-model-generation")
    source["current_generation"] = {
        "generation_id": "a" * 64,
        "sha256": "b" * 64,
        "as_of": "2026-08-17",
    }
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="uncaptured source"
    ):
        _validate(contract)


def test_private_absolute_path_and_account_identifier_are_rejected() -> None:
    contract = copy.deepcopy(_contract())
    contract["source_segments"][0]["endpoint_or_artifact_selector"] = (
        "C:\\private\\source.json"
    )
    with pytest.raises(contract_tool.FinancialMvpContractError, match="absolute"):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    contract["source_segments"][0]["endpoint_or_artifact_selector"] = (
        "restricted/account-secret"
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="account identifier"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    contract["source_segments"][0]["authority"] = "C:\\Users\\private\\account-123"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="absolute"):
        _validate(contract)


def test_golden_case_has_exact_financial_values_and_all_cells() -> None:
    contract = _contract()
    expected = contract["golden_case"]["expected"]
    assert expected["hwm"]["current_index"] == "93.6"
    assert expected["hwm"]["current_hwm"] == "104"
    assert expected["hwm"]["current_drawdown"] == "-0.1"
    assert expected["invested_capital"]["aggregate_current"] == "900"
    assert len(expected["decomposition"]) == 8
    assert set(expected["dependence"]) == set(contract_tool.EVIDENCE_MODES)
    assert len(expected["source_bindings"]) == 7
    assert set(expected["stage_identities"]) == set(contract_tool.OUTCOME_IDS)
    assert expected["report_panel_parity"] == {
        "hwm_equal": True,
        "invested_capital_equal": True,
        "decomposition_equal": True,
        "dependence_equal": True,
    }

    mutated = copy.deepcopy(contract)
    mutated["golden_case"]["expected"]["hwm"]["current_index"] = "104"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="golden HWM"):
        _validate(mutated)


def test_every_golden_input_output_and_identity_is_reconciled() -> None:
    mutated = copy.deepcopy(_contract())
    mutated["golden_case"]["inputs"]["component_returns"][2]["return"] = "0.99"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="golden HWM"):
        _validate(mutated)

    mutated = copy.deepcopy(_contract())
    mutated["golden_case"]["expected"]["hwm"]["index_by_date"]["2026-01-05"] = "999"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="index path"):
        _validate(mutated)

    mutated = copy.deepcopy(_contract())
    mutated["golden_case"]["expected"]["invested_capital"]["daily_aggregate"][
        "2026-01-06"
    ] = "999"
    with pytest.raises(contract_tool.FinancialMvpContractError, match="daily invested"):
        _validate(mutated)

    mutated = copy.deepcopy(_contract())
    mutated["golden_case"]["expected"]["decomposition"][0]["factor_contribution"] = (
        "999"
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="decomposition value"
    ):
        _validate(mutated)

    mutated = copy.deepcopy(_contract())
    del mutated["golden_case"]["expected"]["dependence"]["LIVE_ONLY"]["horizons"]["5"]
    with pytest.raises(contract_tool.FinancialMvpContractError, match="horizons"):
        _validate(mutated)

    mutated = copy.deepcopy(_contract())
    mutated["golden_case"]["expected"]["source_bindings"][0]["generation_sha256"] = (
        "f" * 64
    )
    with pytest.raises(contract_tool.FinancialMvpContractError, match="source binding"):
        _validate(mutated)

    mutated = copy.deepcopy(_contract())
    mutated["golden_case"]["expected"]["stage_identities"]["DEPENDENCE"][
        "input_sha256"
    ] = "f" * 64
    with pytest.raises(contract_tool.FinancialMvpContractError, match="stage identity"):
        _validate(mutated)


def test_generated_views_are_deterministic_and_match_authoritative_bytes(
    tmp_path: Path,
) -> None:
    contract = contract_tool.load_contract(CONTRACT_PATH, REGISTRY_PATH)
    first = contract_tool.render_views(contract)
    second = contract_tool.render_views(copy.deepcopy(contract))
    assert first == second

    identities = contract_tool.generate_views(contract, tmp_path)
    assert identities == {
        "current-product.md": hashlib.sha256(first.summary_markdown).hexdigest(),
        "source-matrix.json": hashlib.sha256(first.source_matrix_json).hexdigest(),
    }
    assert (tmp_path / "current-product.md").read_bytes() == first.summary_markdown
    assert (tmp_path / "source-matrix.json").read_bytes() == first.source_matrix_json
    contract_tool.check_views(contract, GENERATED_ROOT)

    matrix = json.loads(first.source_matrix_json)
    rows = matrix["rows"]
    receipts = _outcome(contract, "INVESTED_CAPITAL")["calculation_contract"][
        "owner_decision_receipts"
    ]
    assert matrix["owner_decision_receipts"] == {
        decision_id: receipts[decision_id]["payload_sha256"]
        for decision_id in contract_tool.OWNER_DECISION_IDS
    }
    assert rows
    assert {row["data_class"] for row in rows} >= contract_tool.REQUIRED_SOURCE_CLASSES
    assert all(
        row["contract_sha256"] == contract_tool.contract_digest(contract)
        for row in rows
    )
    assert all(
        not row["endpoint_or_artifact_selector"].startswith("C:\\") for row in rows
    )
    unresolved = {
        row["strategy_id"]
        for row in rows
        if row["source_id"] == "alpaca-account-generation"
    }
    assert unresolved == {"UNRESOLVED_SUBJECT_BINDING"}
    assert all(row["strategy_id"] != "ALL_PORTFOLIO" for row in rows)


def test_authoritative_byte_change_reidentifies_every_view() -> None:
    contract = contract_tool.load_contract(CONTRACT_PATH, REGISTRY_PATH)
    baseline_digest = contract_tool.contract_digest(contract)
    baseline_views = contract_tool.render_views(contract)
    changed = copy.deepcopy(contract)
    changed["product"]["operator_outcome"] += " Candidate changed."
    changed_digest = contract_tool.contract_digest(changed)
    changed_views = contract_tool.render_views(changed)
    assert changed_digest != baseline_digest
    assert changed_views.summary_markdown != baseline_views.summary_markdown
    assert changed_views.source_matrix_json != baseline_views.source_matrix_json


def test_invested_capital_policy_changes_in_one_contract_block_and_propagates(
    tmp_path: Path,
) -> None:
    baseline = contract_tool.load_contract(CONTRACT_PATH, REGISTRY_PATH)
    baseline_contract_digest = contract_tool.contract_digest(baseline)
    baseline_policy_digest = contract_tool.invested_capital_policy_digest(baseline)
    changed = copy.deepcopy(baseline)
    invested = _outcome(changed, "INVESTED_CAPITAL")["calculation_contract"]
    negative_net = invested["negative_net_contributions"]
    approved_treatment = "SYNTHETIC_TEST_APPROVED_TREATMENT"
    negative_net["decision_status"] = "APPROVED"
    negative_net["selected_treatment"] = approved_treatment
    negative_net["recommended_treatment"] = None
    new_comment_id = 6000000000
    new_evidence = (
        "https://github.com/dachent/quant_engine/issues/330#issuecomment-"
        f"{new_comment_id}"
    )
    negative_net["approval_evidence"] = new_evidence
    receipt = invested["owner_decision_receipts"]["NEGATIVE_NET_CONTRIBUTIONS"]
    receipt["comment_id"] = new_comment_id
    receipt["evidence_url"] = new_evidence
    new_digest_comment_id = 6000000001
    receipt["digest_comment_id"] = new_digest_comment_id
    receipt["digest_evidence_url"] = (
        "https://github.com/dachent/quant_engine/issues/330#issuecomment-"
        f"{new_digest_comment_id}"
    )
    receipt["payload_sha256"] = contract_tool.owner_decision_receipt_digests(changed)[
        "NEGATIVE_NET_CONTRIBUTIONS"
    ]

    _validate(changed)
    comments = _owner_evidence_comments(baseline)
    comments[new_comment_id] = _github_comment(new_comment_id)
    new_record = contract_tool.owner_receipt_evidence_record(
        changed, ["NEGATIVE_NET_CONTRIBUTIONS"]
    )
    new_body = (
        f"{contract_tool._OWNER_RECEIPT_MARKER}\n\n```json\n"
        f"{json.dumps(new_record, indent=2, sort_keys=True)}\n```\n"
    )
    comments[new_digest_comment_id] = _github_comment(new_digest_comment_id, new_body)
    contract_tool.reconcile_owner_receipt_evidence(changed, comments)
    changed_contract_digest = contract_tool.contract_digest(changed)
    changed_policy_digest = contract_tool.invested_capital_policy_digest(changed)
    rendered = contract_tool.render_views(changed)
    matrix = json.loads(rendered.source_matrix_json)

    assert changed_contract_digest != baseline_contract_digest
    assert changed_policy_digest != baseline_policy_digest
    assert matrix["invested_capital_policy_sha256"] == changed_policy_digest
    assert (
        matrix["owner_decision_receipts"]["NEGATIVE_NET_CONTRIBUTIONS"]
        == (receipt["payload_sha256"])
    )
    assert b"negative_net_contributions" in rendered.summary_markdown
    assert approved_treatment.encode("utf-8") in rendered.summary_markdown
    assert receipt["payload_sha256"].encode("utf-8") in rendered.summary_markdown
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="generated view drift"
    ):
        contract_tool.check_views(changed, GENERATED_ROOT)

    contract_tool.generate_views(changed, tmp_path)
    contract_tool.check_views(changed, tmp_path)


def test_policy_decision_cannot_claim_approval_without_evidence() -> None:
    changed = _contract()
    invested = _outcome(changed, "INVESTED_CAPITAL")["calculation_contract"]
    negative_net = invested["negative_net_contributions"]
    negative_net["approval_evidence"] = None
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="approval record is incomplete"
    ):
        _validate(changed)


def test_hand_edit_of_generated_view_is_detected(tmp_path: Path) -> None:
    contract = contract_tool.load_contract(CONTRACT_PATH, REGISTRY_PATH)
    contract_tool.generate_views(contract, tmp_path)
    summary = tmp_path / "current-product.md"
    summary.write_text("hand edited\n", encoding="utf-8")
    with pytest.raises(contract_tool.FinancialMvpContractError, match="view drift"):
        contract_tool.check_views(contract, tmp_path)
