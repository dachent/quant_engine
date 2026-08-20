from __future__ import annotations

import copy
from datetime import date
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


def _history_correction(
    contract: dict[str, object], correction_id: str
) -> dict[str, object]:
    history = contract["history_corrections"]
    assert isinstance(history, dict)
    rows = history["corrections"]
    assert isinstance(rows, list)
    return next(row for row in rows if row["correction_id"] == correction_id)


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


def _census_owner_comment() -> dict[str, object]:
    record = {
        "decision_id": ("DATA-CENSUS-001-OWNER-PERIMETER-AND-MEMBERSHIP-2026-08-19"),
        "authority_issue": 302,
        "approved_on": "2026-08-19",
        "perimeter": [
            {
                "strategy_id": "composer-holy-grail",
                "disposition": "IMMATERIAL_EXCLUDED_FROM_CURRENT_FINANCIAL_MVP",
            },
            {
                "strategy_id": "echotrade-growth-pivots",
                "disposition": "DEPRECATED_EXCLUDED_FROM_CURRENT_FINANCIAL_MVP",
            },
        ],
        "memberships": [
            {
                "strategy_id": "p123-ai-factor",
                "effective_start": "2026-01-22",
                "effective_end": None,
            },
            {
                "strategy_id": "grizzlybears-vtma",
                "effective_start": "2026-01-02",
                "effective_end": None,
            },
            {
                "strategy_id": "qsi",
                "effective_start": "2026-01-05",
                "effective_end": None,
            },
            {
                "strategy_id": "tt-portfolio",
                "effective_start": "2025-11-25",
                "effective_end": None,
            },
        ],
        "transitions": [
            {
                "from_strategy_id": "tt-portfolio",
                "from_effective_end": "2026-01-04",
                "to_strategy_id": "qsi",
                "to_effective_start": "2026-01-05",
                "scope": "TWO_REDACTED_SLEEVES",
            }
        ],
        "continuity": [
            {
                "strategy_id": "tt-portfolio",
                "rule": "CONTINUOUS_MEMBERSHIP",
                "disconfirming_interval": "2026-07-14/2026-07-30",
                "disposition": (
                    "ZERO_OR_MISSING_COMPONENT_VALUE_DOES_NOT_CREATE_EXIT_OR_REENTRY"
                ),
            }
        ],
        "explicit_non_decisions": [
            "TRANSACTION_OR_FLOW_CLASSIFICATION",
            "UNSUPPORTED_STRATEGY_DESIGN_VERSION",
            "PRIVATE_ACCOUNT_IDENTITY",
            "POINTER_OR_PUBLICATION",
        ],
    }
    return {
        "id": contract_tool._CENSUS_OWNER_COMMENT_ID,
        "html_url": (
            "https://github.com/dachent/quant_engine/issues/302#issuecomment-"
            f"{contract_tool._CENSUS_OWNER_COMMENT_ID}"
        ),
        "body": f"```json\n{json.dumps(record, sort_keys=True)}\n```",
        "user": {"login": "dachent"},
        "author_association": "OWNER",
        "created_at": "2026-08-20T01:08:42Z",
        "updated_at": "2026-08-20T01:08:42Z",
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
        "01d7bd98a166736c10248dabffd36ae315e9c30baf59ae3d23024adc5e02cb39"
    )


def test_current_strategy_registry_binds_exact_applicability_plan_bytes() -> None:
    source = _source(_contract(), "current-strategy-registry")
    digest = hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest()
    assert source["source_version"] == "decomposition-plan-v3"
    assert source["schema"] == {
        "name": "current-strategy-decomposition-plan",
        "version": "3",
    }
    assert source["data_classes"] == [
        "STRATEGY_VERSION",
        "DECOMPOSITION_APPLICABILITY",
        "DECOMPOSITION_SURFACE_POLICY",
    ]
    assert source["current_generation"] == {
        "generation_id": digest,
        "sha256": digest,
        "as_of": "2026-08-18",
    }


def test_calculation_boundary_policy_separates_completeness_from_age() -> None:
    contract = _contract()
    policy = contract["product"]["as_of_policy"]
    boundary = policy["calculation_boundary"]
    assert boundary == {
        "scope": "REQUIRED_INPUTS_OF_EACH_CALCULATION",
        "rule": (
            "LATEST_COMMON_COMPLETE_ACCEPTED_DATE_ACROSS_REQUIRED_CALCULATION_INPUTS"
        ),
        "calendar_rule": (
            "Intersect accepted complete observation dates under each required "
            "input's declared calendar and gap policy; never fill, zero, relabel, "
            "or use a future observation."
        ),
        "post_boundary_rows": ("RETAIN_IN_SOURCE_EVIDENCE_EXCLUDE_FROM_CALCULATION"),
        "age_disposition": (
            "WARN_WITH_COMPLETE_THROUGH_DATE_AGE_DAYS_THRESHOLD_AND_LIMITING_INPUTS"
        ),
        "completeness_failure_disposition": "FAIL_CLOSED",
        "required_evidence": [
            "EVALUATED_ON_DATE",
            "COMPLETE_THROUGH_DATE",
            "AGE_DAYS",
            "WARNING_AFTER_DAYS",
            "LIMITING_INPUTS",
        ],
    }
    assert policy["execution_profiles"] == {
        "DEVELOPMENT": (
            "VERIFIED_IMMUTABLE_INPUT_REUSE_ALLOWED_WITH_STRUCTURED_AGE_WARNING"
        ),
        "WORKSTATION_QC": (
            "VERIFIED_IMMUTABLE_INPUT_REUSE_ALLOWED_WITH_STRUCTURED_AGE_WARNING"
        ),
        "RELEASE_QC": (
            "CURRENT_RELEASE_RECEIPTS_REQUIRED_WITH_CALCULATION_AGE_DISCLOSED"
        ),
    }

    invalid = copy.deepcopy(contract)
    invalid["product"]["as_of_policy"]["calculation_boundary"][
        "completeness_failure_disposition"
    ] = "WARN"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="calculation completeness must remain fail-closed",
    ):
        _validate(invalid)

    rendered = contract_tool.render_views(contract)
    assert b"LATEST_COMMON_COMPLETE_ACCEPTED_DATE" in rendered.summary_markdown
    assert b"WARN_WITH_COMPLETE_THROUGH_DATE" in rendered.summary_markdown
    matrix = json.loads(rendered.source_matrix_json)
    assert matrix["as_of_policy"] == policy
    assert all(
        row["age_disposition"] == boundary["age_disposition"] for row in matrix["rows"]
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
    requirements = {
        row["strategy_id"]: row["product_requirement"] for row in contract["strategies"]
    }
    assert {key for key, value in requirements.items() if value == "EXCLUDED"} == {
        "composer-holy-grail",
        "echotrade-growth-pivots",
    }
    assert {key for key, value in requirements.items() if value == "REQUIRED"} == {
        "grizzlybears-vtma",
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "robuxio-crypto",
        "tt-portfolio",
        "volatis-strategy",
    }
    assert {
        row["strategy_id"]: row["history_series_id"] for row in contract["strategies"]
    } == {
        "composer-holy-grail": None,
        "echotrade-growth-pivots": "Growth at Market Pivot Points [EchoTrade]",
        "grizzlybears-vtma": "GrizzlyBears VTMA",
        "p123-ai-factor": "AI Factor [P123]",
        "p123-eu": "EU [P123]",
        "qsi": "QSI",
        "robuxio-crypto": "Robuxio Crypto (High Sharpe High Vol)",
        "tt-portfolio": "TT Portfolio",
        "volatis-strategy": "Volatis Strategy",
    }
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
    excluded = {
        "composer-holy-grail",
        "echotrade-growth-pivots",
    }
    for row in contract["strategies"]:
        expected = (
            "EXCLUDED_BY_ACCEPTED_POLICY"
            if row["strategy_id"] in excluded
            else "REQUIRED"
        )
        assert set(row["outcome_dispositions"].values()) == {expected}


def test_current_census_binds_redacted_sleeves_memberships_and_transition() -> None:
    contract = _contract()
    census = contract["current_census"]
    assert census["authority_issue"] == 302
    assert len(census["account_sleeves"]) == 19
    assert len(census["provider_lanes"]) == 10
    assert len(census["evidence_receipts"]) == 6
    current_counts: dict[str, int] = {}
    transition_rows: list[dict[str, object]] = []
    for sleeve in census["account_sleeves"]:
        current = sleeve["current_strategy_id"]
        if current is not None:
            current_counts[current] = current_counts.get(current, 0) + 1
        for assignment in sleeve["assignments"]:
            if assignment["transition_group_id"] is not None:
                transition_rows.append(assignment)
    assert current_counts == {
        "grizzlybears-vtma": 1,
        "p123-ai-factor": 1,
        "p123-eu": 1,
        "qsi": 2,
        "robuxio-crypto": 1,
        "tt-portfolio": 4,
        "volatis-strategy": 1,
    }
    assert len(transition_rows) == 4
    assert {
        (row["strategy_id"], row["effective_start"], row["effective_end"])
        for row in transition_rows
    } == {
        ("tt-portfolio", "2025-11-25", "2026-01-04"),
        ("qsi", "2026-01-05", None),
    }
    tt_funded = [
        assignment
        for sleeve in census["account_sleeves"]
        for assignment in sleeve["assignments"]
        if assignment["strategy_id"] == "tt-portfolio"
        and assignment["status"] == "FUNDED_MEMBERSHIP"
    ]
    assert tt_funded
    assert all(row["effective_end"] is None for row in tt_funded)


def test_census_owner_digest_and_generated_views_are_one_authority() -> None:
    contract = _contract()
    census = contract["current_census"]
    contract_tool.reconcile_census_owner_evidence(contract, _census_owner_comment())
    rendered = contract_tool.render_views(contract)
    matrix = json.loads(rendered.source_matrix_json)
    assert matrix["current_census"] == census
    assert matrix["census_sha256"] == contract_tool.census_digest(census)
    assert matrix["census_rows"]
    assert {row["sleeve_alias"] for row in matrix["census_rows"]} == {
        row["sleeve_alias"] for row in census["account_sleeves"]
    }
    assert b"Current provider/account census" in rendered.summary_markdown
    assert census["decision_payload_sha256"].encode() in rendered.summary_markdown

    tampered = _census_owner_comment()
    tampered["updated_at"] = "2026-08-20T01:08:43Z"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="census owner evidence authority differs",
    ):
        contract_tool.reconcile_census_owner_evidence(contract, tampered)


def test_census_owner_projection_rejects_coherent_semantic_rewrites() -> None:
    baseline = _contract()
    approved_digest = baseline["current_census"]["decision_payload_sha256"]
    projection = contract_tool.census_owner_decision_projection(
        baseline["current_census"], baseline["strategies"]
    )
    assert hashlib.sha256(
        contract_tool.canonical_json_bytes(projection)
    ).hexdigest() == (approved_digest)

    promoted = copy.deepcopy(baseline)
    composer = next(
        row
        for row in promoted["strategies"]
        if row["strategy_id"] == "composer-holy-grail"
    )
    composer["product_requirement"] = "REQUIRED"
    promoted_projection = contract_tool.census_owner_decision_projection(
        promoted["current_census"], promoted["strategies"]
    )
    assert (
        hashlib.sha256(
            contract_tool.canonical_json_bytes(promoted_projection)
        ).hexdigest()
        != approved_digest
    )

    dropped = copy.deepcopy(baseline)
    p123_ai = next(
        row for row in dropped["strategies"] if row["strategy_id"] == "p123-ai-factor"
    )
    p123_ai["product_requirement"] = "OPTIONAL"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="owner membership is not propagated",
    ):
        contract_tool.census_owner_decision_projection(
            dropped["current_census"], dropped["strategies"]
        )

    false_design = copy.deepcopy(baseline)
    p123_ai = next(
        row
        for row in false_design["strategies"]
        if row["strategy_id"] == "p123-ai-factor"
    )
    p123_ai["strategy_version"] = {
        "status": "EVIDENCED",
        "version_ref": "unapproved-design-v999",
    }
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="design/version evidence is not independently bound",
    ):
        _validate(false_design)

    transition_removed = copy.deepcopy(baseline)
    for sleeve in transition_removed["current_census"]["account_sleeves"]:
        for assignment in sleeve["assignments"]:
            assignment["transition_group_id"] = None
    transition_projection = contract_tool.census_owner_decision_projection(
        transition_removed["current_census"], transition_removed["strategies"]
    )
    assert (
        hashlib.sha256(
            contract_tool.canonical_json_bytes(transition_projection)
        ).hexdigest()
        != approved_digest
    )

    july_split = copy.deepcopy(baseline)
    tt_sleeve = next(
        row
        for row in july_split["current_census"]["account_sleeves"]
        if row["sleeve_alias"] == "schwab-tt-01"
    )
    tt_sleeve["assignments"][0]["effective_end"] = "2026-07-14"
    tt_sleeve["assignments"].append(
        {
            "strategy_id": "tt-portfolio",
            "status": "FUNDED_MEMBERSHIP",
            "effective_start": "2026-07-31",
            "effective_end": None,
            "authority": "UNAPPROVED_REENTRY",
            "transition_group_id": None,
        }
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="invents a July exit or re-entry",
    ):
        contract_tool.census_owner_decision_projection(
            july_split["current_census"], july_split["strategies"]
        )

    reintroduced = copy.deepcopy(baseline)
    registry_source = _source(reintroduced, "current-strategy-registry")
    registry_source["subjects"]["strategy_ids"].append("composer-holy-grail")
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="includes excluded strategies",
    ):
        _validate(reintroduced)


def test_census_fails_on_missing_duplicate_overlap_and_stale_provider_binding() -> None:
    contract = _contract()

    missing = copy.deepcopy(contract)
    missing["current_census"]["account_sleeves"].pop()
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="19 redacted sleeves",
    ):
        _validate(missing)

    duplicate = copy.deepcopy(contract)
    duplicate["current_census"]["account_sleeves"][1]["sleeve_alias"] = duplicate[
        "current_census"
    ]["account_sleeves"][0]["sleeve_alias"]
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="duplicate census sleeve",
    ):
        _validate(duplicate)

    overlap = copy.deepcopy(contract)
    qsi = next(
        row
        for row in overlap["current_census"]["account_sleeves"]
        if row["sleeve_alias"] == "schwab-qsi-01"
    )
    qsi["assignments"][1]["effective_start"] = "2026-01-04"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="assignments overlap",
    ):
        _validate(overlap)

    stale = copy.deepcopy(contract)
    ibkr = next(
        row
        for row in stale["current_census"]["provider_lanes"]
        if row["provider_id"] == "IBKR"
    )
    ibkr["generation"]["generation_id"] = "0" * 64
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="provider/source generation differs",
    ):
        _validate(stale)


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
    assert all(required_fields <= set(source) for source in sources)
    assert all(
        set(source) - required_fields <= {"declared_gap_policy"} for source in sources
    )
    assert {
        source["source_id"] for source in sources if "declared_gap_policy" in source
    } == {"matrixport-account-generation"}


def test_matrixport_declared_gap_policy_is_authoritative_and_propagated() -> None:
    contract = _contract()
    source = _source(contract, "matrixport-account-generation")
    policy = source["declared_gap_policy"]
    assert policy["authority_issue"] > 0
    assert policy["policy_id"]
    assert policy["provenance"]
    assert policy["reason"]
    assert policy["return_rule"]
    missing = {
        date.fromisoformat(value) for value in policy["declared_missing_source_dates"]
    }
    omitted = {date.fromisoformat(value) for value in policy["omitted_return_dates"]}
    unavailable = {
        date.fromisoformat(value) for value in policy["unavailable_return_dates"]
    }
    assert omitted == {
        value + date.resolution
        for value in missing
        if value + date.resolution not in missing
    }
    assert unavailable == missing | omitted
    assert source["readiness"] == "REQUIRED_NOT_READY"
    assert source["blockers"] == [324, 318]
    assert source["admission"] == {
        "facts": [
            "NAV_OBSERVATION",
            "CAPITAL_ANCHOR",
            "FINANCING_LIABILITY",
            "LEVERAGE_EXPOSURE",
            "FINANCING_COST",
        ],
        "transform": "provider-ledger",
        "version": "1",
    }
    assert policy["return_transform"] == {
        "facts": [
            "ACCOUNT_LIVE_DAILY_RETURN",
            "PROVIDER_LIVE_DAILY_RETURN",
            "DECLARED_GAP_COVERAGE",
        ],
        "transform": "matrixport-declared-gap-one-day-returns",
        "version": "1",
    }
    robuxio = next(
        row for row in contract["strategies"] if row["strategy_id"] == "robuxio-crypto"
    )
    assert "unexplained" not in robuxio["readiness_reason"].lower()

    views = contract_tool.render_views(contract)
    matrix = json.loads(views.source_matrix_json)
    rows = [
        row
        for row in matrix["rows"]
        if row["source_id"] == "matrixport-account-generation"
    ]
    assert len(rows) == 30
    assert all(row["declared_gap_policy"] == policy for row in rows)
    assert all(
        row["declared_gap_policy"] is None
        for row in matrix["rows"]
        if row["source_id"] != "matrixport-account-generation"
    )
    markdown = views.summary_markdown.decode("utf-8")
    assert "## Declared source-gap policies" in markdown
    assert ", ".join(policy["declared_missing_source_dates"]) in markdown
    assert ", ".join(policy["omitted_return_dates"]) in markdown
    assert "matrixport-declared-gap-one-day-returns@1" in markdown
    assert ", ".join(policy["return_transform"]["facts"]) in markdown

    changed_date = copy.deepcopy(contract)
    changed_policy = _source(changed_date, "matrixport-account-generation")[
        "declared_gap_policy"
    ]
    changed_policy["omitted_return_dates"] = ["2026-01-29", "2026-07-04"]
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="omitted return dates differ",
    ):
        _validate(changed_date)

    missing_policy = copy.deepcopy(contract)
    del _source(missing_policy, "matrixport-account-generation")["declared_gap_policy"]
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="lacks the owner-declared gap policy",
    ):
        _validate(missing_policy)


def test_history_existence_capture_and_evidence_layers_are_separate() -> None:
    contract = _contract()
    p123 = _source(contract, "portfolio123-model-generation")
    assert p123["existence_status"] == "EXISTS"
    assert p123["capture_status"] == "CAPTURED_GOVERNED"
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


def test_truthful_history_corrections_bind_layers_funding_and_versions() -> None:
    contract = _contract()
    history = contract["history_corrections"]
    assert history["policy_id"] == "PORT-HISTORY-TRUTH-1"
    assert history["owner_issue"] == 319
    rows = history["corrections"]
    assert len(rows) == 11
    segment_identities = history["materialized_segment_identities"]
    assert [row["segment_id"] for row in segment_identities] == sorted(
        row["segment_id"] for row in segment_identities
    )
    assert {row["segment_id"] for row in segment_identities} == {
        "grizzlybears-vtma:account-live",
        "p123-ai-factor:account-live",
        "p123-eu:account-live",
        "qsi:account-live",
        "robuxio-crypto:account-live",
        "robuxio-crypto:provider-live-before-funding",
        "tt-portfolio:account-live",
        "volatis-strategy:account-live",
    }
    sources = {row["source_id"]: row for row in contract["source_segments"]}
    for identity in segment_identities:
        generation = sources[identity["source_id"]]["current_generation"]
        assert (
            identity["source_generation_id"],
            identity["source_generation_sha256"],
        ) == (generation["generation_id"], generation["sha256"])
        assert identity["canonicalization"] == (
            "CORRELATION_FIELDS_V2_CANONICAL_CSV_V1"
        )
    rendered = contract_tool.render_views(contract)
    assert b"### Materialized segment value authority" in rendered.summary_markdown
    assert segment_identities[0]["sha256"].encode("utf-8") in rendered.summary_markdown
    assert {(row["strategy_id"], row["evidence_layer"]) for row in rows} == {
        ("robuxio-crypto", "PROVIDER_TRACK_RECORD"),
        ("robuxio-crypto", "PROVIDER_LIVE"),
        ("robuxio-crypto", "ACCOUNT_LIVE"),
        ("p123-ai-factor", "BACKTEST"),
        ("p123-eu", "BACKTEST"),
        ("p123-eu", "PROVIDER_LIVE"),
        ("p123-eu", "ACCOUNT_LIVE"),
        ("volatis-strategy", "BACKTEST"),
        ("volatis-strategy", "PROVIDER_LIVE"),
        ("volatis-strategy", "ACCOUNT_LIVE"),
        ("echotrade-growth-pivots", "ACCOUNT_LIVE"),
    }

    robux_live = _history_correction(contract, "robuxio-account-live-after-funding")
    assert robux_live["funding_event"]["event_date"] == "2025-12-06"
    assert robux_live["interval"]["observed_start"] == "2025-12-07"
    assert robux_live["interval"]["assignment_date"] == "2025-08-26"
    assert robux_live["mode_admission"] == {
        "LIVE_ONLY": "INCLUDE_SELECTED_LIVE",
        "LIVE_PLUS_BACKTEST": "INCLUDE_SELECTED_LIVE",
    }
    robux_provider = _history_correction(
        contract, "robuxio-provider-live-before-funding"
    )
    assert robux_provider["interval"]["observed_end"] == "2025-12-05"
    assert set(robux_provider["mode_admission"].values()) == {"EXCLUDE_COMPARISON_ONLY"}

    p123_provider = _history_correction(contract, "p123-eu-provider-live")
    assert p123_provider["capture"]["status"] == "CAPTURED_GOVERNED"
    assert p123_provider["strategy_version"]["compatibility"] == "EXACT_MATCH"
    assert p123_provider["interval"]["observed_start"] == "2025-11-13"
    assert p123_provider["derived_evidence_identity"] == {
        "calendar": "WEEKDAY_SOURCE_CALENDAR",
        "cash_added_treatment": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
        "equity_overlap_observations": 157,
        "observed_end": "2026-08-13",
        "observed_start": "2025-11-13",
        "raw_level_observations": 197,
        "return_observations": 196,
        "transform_id": "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1",
    }
    rendered_history = contract_tool.render_views(contract).summary_markdown
    assert (
        b"197 levels / 196 returns / 157 equity overlaps / "
        b"WEEKDAY_SOURCE_CALENDAR / Cash Added: "
        b"UNRESOLVED_NOT_USED_IN_RETURN_FORMULA / "
        b"P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1" in rendered_history
    )
    assert (
        _source(contract, "portfolio123-model-generation")["strategy_version"][
            "version_refs"
        ]["p123-eu"]
        == p123_provider["strategy_version"]["version_ref"]
    )
    p123_ai_backtest = _history_correction(contract, "p123-ai-backtest-candidate")
    assert p123_ai_backtest["derived_backtest_identity"] == {
        "index_observations": 1599,
        "observed_end": "2026-06-26",
        "observed_start": "2020-05-12",
        "relationship_type": "live_sim_twin",
        "return_observations": 1598,
        "source_labels": ["api"],
    }
    assert p123_ai_backtest["mode_admission"] == {
        "LIVE_ONLY": "NOT_APPLICABLE",
        "LIVE_PLUS_BACKTEST": "REQUIRED_NOT_READY",
    }
    assert (
        b"1599 index levels / 1598 returns / 2020-05-12..2026-06-26 / "
        b"live_sim_twin / sources: api" in rendered_history
    )
    p123_backtest = _history_correction(contract, "p123-eu-backtest-not-captured")
    assert p123_backtest["history_existence"]["status"] == "EXISTS"
    assert p123_backtest["capture"]["status"] == "NOT_CAPTURED"
    assert p123_backtest["strategy_version"]["compatibility"] == "UNRESOLVED"

    volatis_live = _history_correction(contract, "volatis-account-live")
    assert volatis_live["funding_event"]["event_date"] == "2024-10-15"
    assert volatis_live["interval"]["observed_start"] == "2024-10-16"
    echotrade = _history_correction(
        contract, "echotrade-event-only-account-live-rejected"
    )
    assert echotrade["readiness"] == "REQUIRED_NOT_READY"
    assert set(echotrade["mode_admission"].values()) == {
        "EXCLUDE_EVENT_ONLY_RECONSTRUCTION"
    }
    assert echotrade["funding_event"]["status"] == "UNRESOLVED"
    rejected_identity = echotrade["rejected_evidence_identity"]
    provider_ledger_catalog_receipt = next(
        receipt
        for receipt in echotrade["source_receipts"]
        if receipt["artifact_role"] == "provider-ledger-catalog"
    )
    assert rejected_identity == {
        "canonicalization": "TARGET_FIELDS_CANONICAL_JSON_V1",
        "return_observations": 13,
        "sha256": "55d339e5299607daf72b44be72a03cc13998d34e81fc42d8e0a756152347ef4f",
        "source_receipt_sha256": provider_ledger_catalog_receipt["sha256"],
    }
    assert (
        "EARLIER_PURCHASE_LACKED_ACCEPTED_EVENT_TIME_VALUE"
        in (volatis_live["funding_event"]["authority"])
    )
    volatis_backtest = _history_correction(contract, "volatis-backtest-not-proved")
    assert volatis_backtest["history_existence"]["status"] == "UNRESOLVED"
    assert volatis_backtest["capture"]["status"] == "NOT_CAPTURED"
    owner_versions = _source(contract, "owner-account-live-history")[
        "strategy_version"
    ]["version_refs"]
    assert owner_versions == {
        "p123-eu": p123_provider["strategy_version"]["version_ref"],
        "robuxio-crypto": robux_live["strategy_version"]["version_ref"],
        "volatis-strategy": volatis_live["strategy_version"]["version_ref"],
    }
    p123_live = _history_correction(contract, "p123-eu-account-live")
    assert "INTERNAL_TRANSFER_IN" in p123_live["funding_event"]["authority"]
    assert (
        "ZERO_AGGREGATE_INVESTED_CAPITAL_EFFECT"
        in (p123_live["funding_event"]["authority"])
    )
    assert "EXTERNAL_FLOW" not in p123_live["funding_event"]["authority"]
    assert p123_provider["readiness"] == "REQUIRED_NOT_READY"
    assert p123_provider["blockers"] == [303]
    assert any(
        receipt["manifest_artifact_id"] == "tables/har_managed_performance.parquet"
        for receipt in p123_provider["source_receipts"]
    )
    assert (
        "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1"
        in p123_provider["disposition_reason"]
    )
    assert "No provider-live return is admitted" in p123_provider["disposition_reason"]

    reordered = copy.deepcopy(contract)
    reordered["history_corrections"]["materialized_segment_identities"][0:2] = reversed(
        reordered["history_corrections"]["materialized_segment_identities"][0:2]
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="segment identities are not canonical",
    ):
        _validate(reordered)

    wrong_receipt = copy.deepcopy(contract)
    wrong_receipt["history_corrections"]["materialized_segment_identities"][0][
        "source_generation_sha256"
    ] = "0" * 64
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="source receipt differs",
    ):
        _validate(wrong_receipt)


def test_history_correction_omission_and_capture_conflation_fail() -> None:
    missing = copy.deepcopy(_contract())
    missing["history_corrections"]["corrections"].pop()
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="history correction layers differ",
    ):
        _validate(missing)

    uncaptured_with_generation = copy.deepcopy(_contract())
    row = _history_correction(
        uncaptured_with_generation, "robuxio-provider-live-before-funding"
    )
    row["capture"]["status"] = "NOT_CAPTURED"
    row["capture"]["capture_defect"] = "SYNTHETIC_MISSING_CAPTURE"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="uncaptured history asserts a generation",
    ):
        _validate(uncaptured_with_generation)


def test_provider_history_cannot_be_relabelled_or_assignment_funded() -> None:
    relabelled = copy.deepcopy(_contract())
    provider = _history_correction(relabelled, "p123-eu-provider-live")
    provider["mode_admission"]["LIVE_ONLY"] = "INCLUDE_SELECTED_LIVE"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="provider history was relabeled",
    ):
        _validate(relabelled)

    assignment_funded = copy.deepcopy(_contract())
    account = _history_correction(
        assignment_funded, "robuxio-account-live-after-funding"
    )
    account["funding_event"]["authority"] = "ASSIGNMENT_DATE"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="assignment date cannot be funding authority",
    ):
        _validate(assignment_funded)


def test_history_truth_recurrence_mutations_fail_closed() -> None:
    unbound_echo = copy.deepcopy(_contract())
    echo = _history_correction(
        unbound_echo, "echotrade-event-only-account-live-rejected"
    )
    echo["rejected_evidence_identity"]["source_receipt_sha256"] = "0" * 64
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="rejected evidence is not bound",
    ):
        _validate(unbound_echo)

    p123_wrong_owner = copy.deepcopy(_contract())
    provider = _history_correction(p123_wrong_owner, "p123-eu-provider-live")
    provider["blockers"] = [320]
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="unadmitted transform disposition",
    ):
        _validate(p123_wrong_owner)

    p123_unbound_transform = copy.deepcopy(_contract())
    provider = _history_correction(p123_unbound_transform, "p123-eu-provider-live")
    provider["source_receipts"] = [
        receipt
        for receipt in provider["source_receipts"]
        if receipt["manifest_artifact_id"] != "tables/har_managed_performance.parquet"
    ]
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="unadmitted transform disposition",
    ):
        _validate(p123_unbound_transform)

    p123_rewritten_identity = copy.deepcopy(_contract())
    provider = _history_correction(p123_rewritten_identity, "p123-eu-provider-live")
    provider["derived_evidence_identity"]["return_observations"] = 155
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="derived evidence identity differs",
    ):
        _validate(p123_rewritten_identity)

    p123_impossible_overlap = copy.deepcopy(_contract())
    provider = _history_correction(p123_impossible_overlap, "p123-eu-provider-live")
    provider["derived_evidence_identity"]["equity_overlap_observations"] = 198
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="derived evidence identity differs",
    ):
        _validate(p123_impossible_overlap)

    p123_ai_rewritten_identity = copy.deepcopy(_contract())
    ai_backtest = _history_correction(
        p123_ai_rewritten_identity, "p123-ai-backtest-candidate"
    )
    ai_backtest["derived_backtest_identity"]["observed_start"] = "2020-05-13"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="derived backtest identity differs",
    ):
        _validate(p123_ai_rewritten_identity)

    p123_ai_unbound = copy.deepcopy(_contract())
    ai_backtest = _history_correction(p123_ai_unbound, "p123-ai-backtest-candidate")
    ai_backtest["source_receipts"] = [
        receipt
        for receipt in ai_backtest["source_receipts"]
        if receipt["manifest_artifact_id"] != "tables/strategy_relationships.parquet"
    ]
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="unresolved version disposition",
    ):
        _validate(p123_ai_unbound)

    p123_external = copy.deepcopy(_contract())
    account = _history_correction(p123_external, "p123-eu-account-live")
    account["funding_event"]["authority"] = "FIRST_HASH_BOUND_IBKR_EXTERNAL_FLOW"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="misclassified as external capital",
    ):
        _validate(p123_external)

    early_volatis = copy.deepcopy(_contract())
    account = _history_correction(early_volatis, "volatis-account-live")
    account["funding_event"]["event_date"] = "2024-10-13"
    account["funding_event"]["account_live_start"] = "2024-10-13"
    account["interval"]["effective_start"] = "2024-10-13"
    account["interval"]["observed_start"] = "2024-10-14"
    strategy = next(
        row
        for row in early_volatis["strategies"]
        if row["strategy_id"] == "volatis-strategy"
    )
    strategy["funded_membership"]["effective_start"] = "2024-10-13"
    volatis_sleeve = next(
        row
        for row in early_volatis["current_census"]["account_sleeves"]
        if row["sleeve_alias"] == "quantbase-volatis-01"
    )
    volatis_sleeve["assignments"][0]["effective_start"] = "2024-10-13"
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="valued governed evidence",
    ):
        _validate(early_volatis)


def test_incompatible_backtest_splice_and_unbound_generation_fail() -> None:
    spliced = copy.deepcopy(_contract())
    backtest = _history_correction(spliced, "p123-eu-backtest-not-captured")
    backtest["mode_admission"]["LIVE_PLUS_BACKTEST"] = (
        "INCLUDE_EXACT_VERSION_BACKTEST_PREHISTORY"
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="incompatible backtest entered hybrid mode",
    ):
        _validate(spliced)

    unbound = copy.deepcopy(_contract())
    provider = _history_correction(unbound, "p123-eu-provider-live")
    provider["source_receipts"][0]["sha256"] = "0" * 64
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="history generation is not receipt-bound",
    ):
        _validate(unbound)


def test_history_receipts_resolve_to_governed_bytes_and_reject_coherent_rewrite(
    tmp_path: Path,
) -> None:
    authority = tmp_path / "matrixport-authority"
    generation_id = "1" * 64
    generation_root = authority / "generations" / generation_id
    artifact_path = generation_root / "csv" / "subscription_details.csv"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"synthetic governed receipt bytes\n")
    artifact_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    manifest_path = generation_root / "generation.json"
    manifest_path.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "artifact_id": "csv/subscription_details.csv",
                        "sha256": artifact_sha,
                        "size_bytes": artifact_path.stat().st_size,
                    }
                ],
                "generation_id": generation_id,
                "verification": "VERIFIED",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    pointer_path = authority / "CURRENT.json"
    pointer_path.write_text(
        json.dumps(
            {
                "generation_id": generation_id,
                "manifest_sha256": manifest_sha,
                "state": "PUBLISHED",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    pointer_sha = hashlib.sha256(pointer_path.read_bytes()).hexdigest()
    receipts = [
        {
            "artifact_role": "synthetic-current-pointer",
            "artifact_selector": "CURRENT.json",
            "authority_root": "MATRIXPORT",
            "generation_id": generation_id,
            "locator_kind": "CURRENT_POINTER",
            "manifest_artifact_id": None,
            "sha256": pointer_sha,
        },
        {
            "artifact_role": "synthetic-generation-manifest",
            "artifact_selector": f"generations/{generation_id}/generation.json",
            "authority_root": "MATRIXPORT",
            "generation_id": generation_id,
            "locator_kind": "GENERATION_MANIFEST",
            "manifest_artifact_id": None,
            "sha256": manifest_sha,
        },
        {
            "artifact_role": "synthetic-secondary-artifact",
            "artifact_selector": (
                f"generations/{generation_id}/csv/subscription_details.csv"
            ),
            "authority_root": "MATRIXPORT",
            "generation_id": generation_id,
            "locator_kind": "GENERATION_ARTIFACT",
            "manifest_artifact_id": "csv/subscription_details.csv",
            "sha256": artifact_sha,
        },
    ]
    minimal = {"history_corrections": {"corrections": [{"source_receipts": receipts}]}}
    assert (
        contract_tool.verify_history_evidence(minimal, {"MATRIXPORT": authority})[
            "verified_receipts"
        ]
        == 3
    )

    invented_artifact = copy.deepcopy(minimal)
    invented_artifact["history_corrections"]["corrections"][0]["source_receipts"][2][
        "sha256"
    ] = "a" * 64
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="differs from governed evidence bytes",
    ):
        contract_tool.verify_history_evidence(
            invented_artifact, {"MATRIXPORT": authority}
        )

    coherent_rewrite = copy.deepcopy(minimal)
    for receipt in coherent_rewrite["history_corrections"]["corrections"][0][
        "source_receipts"
    ]:
        receipt["generation_id"] = "b" * 64
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="not the published current authority",
    ):
        contract_tool.verify_history_evidence(
            coherent_rewrite, {"MATRIXPORT": authority}
        )


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
    factors["subjects"]["strategy_ids"].remove("qsi")
    with pytest.raises(contract_tool.FinancialMvpContractError, match="qsi"):
        _validate(contract)


def test_source_strategy_version_and_cost_are_explicit() -> None:
    contract = _contract()
    assert all("strategy_version" in row for row in contract["source_segments"])
    assert any(
        "STRATEGY_COST" in row["data_classes"] for row in contract["source_segments"]
    )
    p123 = _source(contract, "portfolio123-model-generation")
    assert p123["strategy_version"]["status"] == "MIXED"

    matrix = json.loads(contract_tool.render_views(contract).source_matrix_json)["rows"]
    p123_rows = [
        row for row in matrix if row["source_id"] == "portfolio123-model-generation"
    ]
    assert {
        strategy_id: {
            row["strategy_version_status"]
            for row in p123_rows
            if row["strategy_id"] == strategy_id
        }
        for strategy_id in ("p123-ai-factor", "p123-eu")
    } == {
        "p123-ai-factor": {"UNRESOLVED"},
        "p123-eu": {"EVIDENCED"},
    }
    assert all(
        row["strategy_version_refs"] == {}
        for row in p123_rows
        if row["strategy_id"] == "p123-ai-factor"
    )
    assert all(
        row["strategy_version_refs"] == p123["strategy_version"]["version_refs"]
        for row in p123_rows
        if row["strategy_id"] == "p123-eu"
    )

    mutated = copy.deepcopy(contract)
    _source(mutated, "portfolio123-model-generation")["strategy_version"]["status"] = (
        "EVIDENCED"
    )
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="lacks evidenced version refs for subjects.*p123-ai-factor",
    ):
        _validate(mutated)

    mutated = copy.deepcopy(contract)
    _source(mutated, "strategy-backtests")["strategy_version"]["version_refs"] = {
        "qsi": "invented-v1"
    }
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="asserts unresolved"
    ):
        _validate(mutated)

    mutated = copy.deepcopy(contract)
    p123 = _source(mutated, "portfolio123-model-generation")
    p123["subjects"]["strategy_ids"].remove("p123-eu")
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="version refs outside its subjects",
    ):
        _validate(mutated)

    mutated = copy.deepcopy(contract)
    _source(mutated, "portfolio123-model-generation")["strategy_version"][
        "version_refs"
    ] = {}
    with pytest.raises(
        contract_tool.FinancialMvpContractError,
        match="claims evidenced versions without identities",
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
    expected_cadence = "DAILY_TO_WEEKLY_FULL_PIPELINE; CAPTURE_EVERY_SCHEDULED_RUN"
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
    assert len(rows) == 105
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
    strategy = next(
        row for row in contract["strategies"] if row["strategy_id"] == "p123-ai-factor"
    )
    strategy["readiness"] = "READY"
    strategy["blockers"] = []
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="version/membership"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    source = _source(contract, "provider-live-history")
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
    source = _source(contract, "provider-live-history")
    source["capture_status"] = "CAPTURED_GOVERNED"
    source["existence_status"] = "EXISTS"
    with pytest.raises(
        contract_tool.FinancialMvpContractError, match="immutable generation"
    ):
        _validate(contract)

    contract = copy.deepcopy(_contract())
    source = _source(contract, "provider-live-history")
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
    assert matrix["history_corrections"] == contract["history_corrections"]
    assert b"## Truthful strategy-history corrections \xe2\x80\x94 #319" in (
        first.summary_markdown
    )
    assert b"robuxio-account-live-after-funding" in first.summary_markdown
    assert b"MATRIXPORT:output/20260816T141010.494Z/csv/subscription_details.csv" in (
        first.summary_markdown
    )
    assert b"p123-eu-provider-live" in first.summary_markdown
    assert b"volatis-backtest-not-proved" in first.summary_markdown
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
    assert unresolved == {"echotrade-growth-pivots"}
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
