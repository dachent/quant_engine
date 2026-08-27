"""Contract tests for the synthetic account-assignment perimeter producer."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest
from quant_engine.core import InputSnapshot
from quant_engine.core.contracts import canonical_json_bytes


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "build_account_manifest_generation.py"
SCHEMA_PATH = (
    REPOSITORY_ROOT
    / "schemas"
    / "portfolio"
    / "account-assignment-perimeter.schema.json"
)
BASELINE_GENERATION_ID = "b" * 64


def _semantic_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@pytest.fixture(scope="module")
def perimeter_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "build_account_manifest_generation", MODULE_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assignment(
    account_id: str,
    disposition: str,
    start: str,
    end: str | None,
    strategy_id: str | None,
) -> dict[str, object]:
    return {
        "account_id": account_id,
        "strategy_id": strategy_id,
        "strategy_mapping_disposition": disposition,
        "effective_start": start,
        "effective_end": end,
    }


def _fixture_inputs() -> dict[str, object]:
    """An 18-account / 26-row synthetic partition retained as acceptance evidence."""
    assignments: list[dict[str, object]] = []
    for number in range(1, 12):
        assignments.append(
            _assignment(
                f"synthetic-account-{number:02d}",
                "STRATEGY_MAPPED",
                "2025-01-01",
                None,
                f"synthetic-strategy-{number:02d}",
            )
        )
    for number in range(12, 16):
        account_id = f"synthetic-account-{number:02d}"
        assignments.extend(
            [
                _assignment(
                    account_id,
                    "STRATEGY_MAPPED",
                    "2025-01-01",
                    "2025-12-31",
                    f"synthetic-strategy-{number:02d}",
                ),
                _assignment(
                    account_id,
                    "HISTORICAL_POOL_ONLY",
                    "2026-01-01",
                    None,
                    None,
                ),
            ]
        )
    for number in range(16, 18):
        assignments.append(
            _assignment(
                f"synthetic-account-{number:02d}",
                "HISTORICAL_POOL_ONLY",
                "2025-01-01",
                None,
                None,
            )
        )
    assignments.extend(
        [
            _assignment(
                "synthetic-account-18",
                "HISTORICAL_POOL_ONLY",
                "2025-01-01",
                "2025-12-31",
                None,
            ),
            _assignment(
                "synthetic-account-18",
                "RESERVE_NOT_APPLICABLE",
                "2026-01-01",
                "2026-01-10",
                None,
            ),
            _assignment(
                "synthetic-account-18",
                "RESERVE_NOT_APPLICABLE",
                "2026-01-11",
                "2026-01-20",
                None,
            ),
            _assignment(
                "synthetic-account-18",
                "RESERVE_NOT_APPLICABLE",
                "2026-01-21",
                "2026-01-30",
                None,
            ),
            _assignment(
                "synthetic-account-18",
                "RESERVE_NOT_APPLICABLE",
                "2026-01-31",
                None,
                None,
            ),
        ]
    )
    accounts = [
        {"account_id": f"synthetic-account-{number:02d}", "provider": "SYNTHETIC"}
        for number in range(1, 19)
    ]
    baseline_payload = {
        "as_of_date": "2026-02-01",
        "baseline_schema_version": 1,
        "accounts": accounts,
        "assignments": assignments,
    }
    return {
        "baseline": InputSnapshot.from_bytes(
            "synthetic portfolio baseline", canonical_json_bytes(baseline_payload)
        ),
        "assignments": assignments,
        "nav_facts": [
            {
                "record_id": "nav-mapped",
                "account_id": "synthetic-account-01",
                "as_of_date": "2025-06-01",
                "nav": {"status": "AVAILABLE", "value": "100"},
            },
            {
                "record_id": "nav-historical",
                "account_id": "synthetic-account-12",
                "as_of_date": "2026-01-15",
                "nav": {"status": "UNAVAILABLE", "reason": "synthetic"},
            },
            {
                "record_id": "nav-reserve",
                "account_id": "synthetic-account-18",
                "as_of_date": "2026-01-15",
                "nav": {"status": "AVAILABLE", "value": "1"},
            },
        ],
        "raw_flow_facts": [
            {
                "record_id": "raw-mapped",
                "account_id": "synthetic-account-01",
                "event_date": "2025-06-01",
            },
            {
                "record_id": "raw-historical",
                "account_id": "synthetic-account-12",
                "event_date": "2026-01-15",
            },
            {
                "record_id": "raw-reserve",
                "account_id": "synthetic-account-18",
                "event_date": "2026-01-15",
            },
        ],
        "classified_flow_facts": [
            {
                "event_id": "classified-mapped",
                "source_record_id": "intentionally-unmatched-5c3-deferred",
                "account_id": "synthetic-account-01",
                "event_date": "2025-06-01",
            },
            {
                "event_id": "classified-historical",
                "source_record_id": "raw-historical",
                "account_id": "synthetic-account-12",
                "event_date": "2026-01-15",
            },
            {
                "event_id": "classified-reserve",
                "source_record_id": "raw-reserve",
                "account_id": "synthetic-account-18",
                "event_date": "2026-01-15",
            },
        ],
        "as_of_date": "2026-02-01",
    }


def _build(module: Any, inputs: dict[str, object]) -> bytes:
    return module.build_account_assignment_perimeter(
        baseline_generation_id=BASELINE_GENERATION_ID,
        baseline=inputs["baseline"],
        nav_facts=inputs["nav_facts"],
        raw_flow_facts=inputs["raw_flow_facts"],
        classified_flow_facts=inputs["classified_flow_facts"],
        as_of_date=inputs["as_of_date"],
    )


def _validate(module: Any, payload: dict[str, object], inputs: dict[str, object]) -> None:
    module.validate_account_assignment_perimeter(
        payload,
        baseline_generation_id=BASELINE_GENERATION_ID,
        baseline=inputs["baseline"],
        nav_facts=inputs["nav_facts"],
        raw_flow_facts=inputs["raw_flow_facts"],
        classified_flow_facts=inputs["classified_flow_facts"],
        as_of_date=inputs["as_of_date"],
    )


def _schema_validator() -> Any:
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _schema_validator_without_format_checker() -> Any:
    from jsonschema import Draft202012Validator

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_red_contract_requires_new_producer_and_schema() -> None:
    """This deliberately fails before 5C.1 creates its two production artifacts."""
    assert MODULE_PATH.is_file(), "5C.1 producer module has not been created"
    assert SCHEMA_PATH.is_file(), "5C.1 perimeter schema has not been created"


def test_builds_schema_valid_complete_partition_and_counts_non_strategy_facts(
    perimeter_module: Any,
) -> None:
    inputs = _fixture_inputs()
    payload_bytes = _build(perimeter_module, inputs)
    payload = json.loads(payload_bytes)

    assert payload_bytes == canonical_json_bytes(payload)
    assert payload["account_count"] == 18
    assert payload["assignment_count"] == 26
    assert payload["partition_counts"] == {
        "STRATEGY_MAPPED": 15,
        "HISTORICAL_POOL_ONLY": 7,
        "RESERVE_NOT_APPLICABLE": 4,
    }
    assert payload["fact_bijection_status"] == "ONE_TO_ONE_VERIFIED"
    assert payload["unassigned_fact_count"] == 0
    assert payload["unassigned_facts"] == []
    assert not list(_schema_validator().iter_errors(payload))

    historical = next(
        row
        for row in payload["rows"]
        if row["account_id"] == "synthetic-account-12"
        and row["effective_start"] == "2026-01-01"
    )
    reserve = next(
        row
        for row in payload["rows"]
        if row["account_id"] == "synthetic-account-18"
        and row["effective_start"] == "2026-01-11"
    )
    assert historical["calculation_eligible"] is False
    assert historical["unavailable_nav"] == historical["raw_flows"] == 1
    assert historical["classified_flows"] == 1
    assert reserve["calculation_eligible"] is False
    assert reserve["available_nav"] == reserve["raw_flows"] == 1
    assert reserve["classified_flows"] == 1


def test_is_byte_deterministic_for_shuffled_fact_inputs(perimeter_module: Any) -> None:
    inputs = _fixture_inputs()
    expected = _build(perimeter_module, inputs)
    for field in ("nav_facts", "raw_flow_facts", "classified_flow_facts"):
        inputs[field].reverse()
    assert _build(perimeter_module, inputs) == expected


def test_reordered_baseline_preserves_perimeter_but_changes_snapshot_identity(
    perimeter_module: Any,
) -> None:
    inputs = _fixture_inputs()
    original = json.loads(_build(perimeter_module, inputs))
    baseline_payload = inputs["baseline"].json_object()
    baseline_payload["accounts"].reverse()
    baseline_payload["assignments"].reverse()
    inputs["baseline"] = InputSnapshot.from_bytes(
        "reordered synthetic baseline", canonical_json_bytes(baseline_payload)
    )
    reordered = json.loads(_build(perimeter_module, inputs))

    assert reordered["selected_baseline_identity"] != original[
        "selected_baseline_identity"
    ]
    for field in (
        "full_perimeter_identity",
        "account_count",
        "assignment_count",
        "partition_counts",
        "rows",
    ):
        assert reordered[field] == original[field]


def test_pins_assignment_perimeter_and_selected_baseline_digest_vectors(
    perimeter_module: Any,
) -> None:
    inputs = _fixture_inputs()
    payload = json.loads(_build(perimeter_module, inputs))
    first_row = payload["rows"][0]
    assignment_preimage = {
        key: first_row[key]
        for key in (
            "account_id",
            "strategy_id",
            "strategy_mapping_disposition",
            "effective_start",
            "effective_end",
        )
    }
    perimeter_preimage = [
        {
            key: row[key]
            for key in (
                "account_id",
                "strategy_id",
                "strategy_mapping_disposition",
                "effective_start",
                "effective_end",
            )
        }
        for row in payload["rows"]
    ]
    assert _semantic_digest(assignment_preimage) == (
        "5a7c37d12124866f09841ce952e642d49f28f6e1d709bbc8b1a4b93c5d949360"
    )
    assert first_row["assignment_identity"]["sha256"] == (
        "5a7c37d12124866f09841ce952e642d49f28f6e1d709bbc8b1a4b93c5d949360"
    )
    assert _semantic_digest(perimeter_preimage) == (
        "4421ad809a972746e7044b5dac4e7d2e0916018bbdea7cc321ea747459468d54"
    )
    assert payload["full_perimeter_identity"]["sha256"] == (
        "4421ad809a972746e7044b5dac4e7d2e0916018bbdea7cc321ea747459468d54"
    )
    assert payload["selected_baseline_identity"] == {
        "generation_id": BASELINE_GENERATION_ID,
        "artifact": {
            "artifact_id": "normalized/portfolio-baseline.json",
            "as_of_date": "2026-02-01",
            "schema": {"name": "portfolio-baseline", "version": "1"},
            "sha256": "35d757b23dfd079c4dab3f3ffd201cad0a8e9f494dd8619b9ccfbba4f4cd7049",
            "size_bytes": 5703,
        },
    }


def _mutate_first_row_to_non_strategy(payload: dict[str, object]) -> None:
    payload["rows"][0].update(
        {
            "strategy_mapping_disposition": "HISTORICAL_POOL_ONLY",
            "strategy_id": None,
            "calculation_eligible": False,
        }
    )


def _mutate_non_strategy_row_to_mapped(payload: dict[str, object]) -> None:
    next(
        row
        for row in payload["rows"]
        if row["strategy_mapping_disposition"] == "HISTORICAL_POOL_ONLY"
    ).update(
        {
            "strategy_mapping_disposition": "STRATEGY_MAPPED",
            "strategy_id": "synthetic-mutated-strategy",
            "calculation_eligible": True,
        }
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["rows"].pop(), "payload differs from expected"),
        (_mutate_first_row_to_non_strategy, "payload differs from expected"),
        (
            lambda value: value["rows"][0].update({"strategy_id": "other"}),
            "payload differs from expected",
        ),
        (_mutate_non_strategy_row_to_mapped, "payload differs from expected"),
        (
            lambda value: value["rows"][0].update({"provider": "OTHER"}),
            "payload differs from expected",
        ),
        (
            lambda value: value["rows"][0].update({"account_id": "other-account"}),
            "payload differs from expected",
        ),
        (
            lambda value: value["rows"][0].update({"effective_start": "2025-01-02"}),
            "payload differs from expected",
        ),
        (
            lambda value: value["rows"][0]["assignment_identity"].update({"sha256": "0" * 64}),
            "payload differs from expected",
        ),
        (
            lambda value: value["full_perimeter_identity"].update({"sha256": "0" * 64}),
            "payload differs from expected",
        ),
        (
            lambda value: value["full_perimeter_identity"].update({"count": 25}),
            "payload differs from expected",
        ),
        (
            lambda value: value["selected_baseline_identity"].update({"generation_id": "0" * 64}),
            "payload differs from expected",
        ),
        (
            lambda value: value["selected_baseline_identity"]["artifact"].update({"sha256": "0" * 64}),
            "payload differs from expected",
        ),
        (
            lambda value: value["selected_baseline_identity"]["artifact"].update({"artifact_id": "other.json"}),
            "payload differs from expected",
        ),
        (
            lambda value: value["selected_baseline_identity"]["artifact"].update({"as_of_date": "2026-02-02"}),
            "payload differs from expected",
        ),
        (
            lambda value: value["selected_baseline_identity"]["artifact"]["schema"].update({"name": "other-schema"}),
            "payload differs from expected",
        ),
        (
            lambda value: value["selected_baseline_identity"]["artifact"]["schema"].update({"version": "2"}),
            "payload differs from expected",
        ),
        (
            lambda value: value["selected_baseline_identity"]["artifact"].update({"size_bytes": 1}),
            "payload differs from expected",
        ),
        (lambda value: value.update({"account_count": 17}), "payload differs from expected"),
        (lambda value: value.update({"assignment_count": 25}), "payload differs from expected"),
        (
            lambda value: value["partition_counts"].update({"STRATEGY_MAPPED": 14}),
            "payload differs from expected",
        ),
        (
            lambda value: value["partition_counts"].update({"HISTORICAL_POOL_ONLY": 6}),
            "payload differs from expected",
        ),
        (
            lambda value: value["partition_counts"].update({"RESERVE_NOT_APPLICABLE": 3}),
            "payload differs from expected",
        ),
        (
            lambda value: value["rows"][0].update({"available_nav": 9}),
            "payload differs from expected",
        ),
        (
            lambda value: value["rows"][0].update({"unavailable_nav": 9}),
            "payload differs from expected",
        ),
        (
            lambda value: value["rows"][0].update({"raw_flows": 9}),
            "payload differs from expected",
        ),
        (
            lambda value: value["rows"][0].update({"classified_flows": 9}),
            "payload differs from expected",
        ),
    ],
)
def test_semantic_validator_rejects_schema_valid_post_build_mutations(
    perimeter_module: Any, mutate: Any, message: str
) -> None:
    inputs = _fixture_inputs()
    payload = json.loads(_build(perimeter_module, inputs))
    mutate(payload)
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError, match=message):
        _validate(perimeter_module, payload, inputs)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["rows"][0].update({"provider": "OTHER"}),
        lambda value: value["rows"][0].update({"strategy_id": "other"}),
        lambda value: value["rows"][0].update({"available_nav": 9}),
        lambda value: value["full_perimeter_identity"].update({"count": 25}),
        lambda value: value.update({"account_count": 17}),
    ],
)
def test_semantic_validator_rejects_schema_valid_mutations(
    perimeter_module: Any, mutate: Any
) -> None:
    inputs = _fixture_inputs()
    payload = json.loads(_build(perimeter_module, inputs))
    mutate(payload)
    assert not list(_schema_validator().iter_errors(payload))
    with pytest.raises(
        perimeter_module.AccountAssignmentPerimeterError,
        match="payload differs from expected",
    ):
        _validate(perimeter_module, payload, inputs)


@pytest.mark.parametrize(
    ("section", "fact_mutation", "message"),
    [
        ("nav_facts", lambda facts: facts.append(deepcopy(facts[0])), "duplicate NAV record_id"),
        (
            "nav_facts",
            lambda facts: facts.append(
                {**deepcopy(facts[0]), "record_id": "other-nav-id"}
            ),
            "duplicate NAV account_id/as_of_date",
        ),
        ("raw_flow_facts", lambda facts: facts.append(deepcopy(facts[0])), "duplicate raw flow record_id"),
        (
            "classified_flow_facts",
            lambda facts: facts.append(deepcopy(facts[0])),
            "duplicate classified flow event_id",
        ),
        (
            "classified_flow_facts",
            lambda facts: facts.append(
                {**deepcopy(facts[0]), "event_id": "other-event-id"}
            ),
            "duplicate classified flow source_record_id",
        ),
    ],
)
def test_rejects_each_required_fact_identity_collision(
    perimeter_module: Any, section: str, fact_mutation: Any, message: str
) -> None:
    inputs = _fixture_inputs()
    fact_mutation(inputs[section])
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError, match=message):
        _build(perimeter_module, inputs)


@pytest.mark.parametrize("field", ["nav_facts", "raw_flow_facts", "classified_flow_facts"])
@pytest.mark.parametrize("date", ["2024-12-31", "2027-01-01"])
def test_rejects_unknown_accounts_and_dates_outside_the_full_perimeter(
    perimeter_module: Any, field: str, date: str
) -> None:
    inputs = _fixture_inputs()
    fact = deepcopy(inputs[field][0])
    fact["account_id"] = "unknown-synthetic-account"
    if field == "classified_flow_facts":
        fact["event_id"] = "unknown-account-event"
        fact["source_record_id"] = "unknown-account-source"
    else:
        fact["record_id"] = "unknown-account-record"
    inputs[field].append(fact)
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError, match="unknown fact account_id"):
        _build(perimeter_module, inputs)

    inputs = _fixture_inputs()
    if date == "2027-01-01":
        baseline_payload = inputs["baseline"].json_object()
        baseline_payload["assignments"][0]["effective_end"] = "2025-12-31"
        inputs["baseline"] = InputSnapshot.from_bytes(
            "closed synthetic account baseline", canonical_json_bytes(baseline_payload)
        )
    fact = deepcopy(inputs[field][0])
    fact["record_id" if field != "classified_flow_facts" else "event_id"] = "date-test"
    fact["as_of_date" if field == "nav_facts" else "event_date"] = date
    if field == "classified_flow_facts":
        fact["source_record_id"] = "date-test-source"
    inputs[field].append(fact)
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError, match="outside every assignment interval"):
        _build(perimeter_module, inputs)


def _make_overlap(assignments: list[dict[str, object]]) -> None:
    assignments[0]["effective_end"] = "2025-12-31"
    assignments.append(
        _assignment(
            "synthetic-account-01",
            "STRATEGY_MAPPED",
            "2025-06-01",
            None,
            "synthetic-strategy-overlap",
        )
    )


def _make_gap(assignments: list[dict[str, object]]) -> None:
    next(
        assignment
        for assignment in assignments
        if assignment["account_id"] == "synthetic-account-12"
        and assignment["effective_start"] == "2026-01-01"
    )["effective_start"] = "2026-01-02"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            _make_overlap,
            "assignments overlap",
        ),
        (
            _make_gap,
            "assignments have a gap",
        ),
    ],
)
def test_rejects_baseline_assignment_overlap_and_gap(
    perimeter_module: Any, mutation: Any, message: str
) -> None:
    inputs = _fixture_inputs()
    baseline_payload = inputs["baseline"].json_object()
    mutation(baseline_payload["assignments"])
    inputs["baseline"] = InputSnapshot.from_bytes(
        "invalid synthetic baseline", canonical_json_bytes(baseline_payload)
    )
    with pytest.raises(perimeter_module.AccountAssignmentPerimeterError, match=message):
        _build(perimeter_module, inputs)


def test_schema_is_general_and_rejects_private_or_invalid_shape(perimeter_module: Any) -> None:
    inputs = _fixture_inputs()
    payload = json.loads(_build(perimeter_module, inputs))
    validator = _schema_validator()
    smaller = deepcopy(payload)
    smaller["rows"] = [deepcopy(payload["rows"][0])]
    smaller["account_count"] = 1
    smaller["assignment_count"] = 1
    smaller["partition_counts"] = {
        "STRATEGY_MAPPED": 1,
        "HISTORICAL_POOL_ONLY": 0,
        "RESERVE_NOT_APPLICABLE": 0,
    }
    smaller["full_perimeter_identity"] = {
        "sha256": smaller["rows"][0]["assignment_identity"]["sha256"],
        "count": 1,
    }
    assert not list(validator.iter_errors(smaller))

    private_shape = deepcopy(payload)
    private_shape["rows"][0]["account_number_last_four"] = "1234"
    assert list(validator.iter_errors(private_shape))

    noncanonical_version = deepcopy(payload)
    noncanonical_version["selected_baseline_identity"]["artifact"]["schema"][
        "version"
    ] = "01"
    assert list(validator.iter_errors(noncanonical_version))


@pytest.mark.parametrize("invalid_date", ["2026-02-30", "2026-13-99", "2025-02-29", "0000-01-01"])
def test_schema_intrinsically_rejects_invalid_calendar_dates_without_format_checker(
    perimeter_module: Any, invalid_date: str
) -> None:
    payload = json.loads(_build(perimeter_module, _fixture_inputs()))
    payload["as_of_date"] = invalid_date
    assert list(_schema_validator_without_format_checker().iter_errors(payload))


@pytest.mark.parametrize("valid_date", ["2024-02-29", "2026-02-28"])
def test_schema_intrinsically_accepts_canonical_calendar_dates_without_format_checker(
    perimeter_module: Any, valid_date: str
) -> None:
    payload = json.loads(_build(perimeter_module, _fixture_inputs()))
    payload["as_of_date"] = valid_date
    assert not list(_schema_validator_without_format_checker().iter_errors(payload))


def test_rejects_negative_baseline_schema_version_before_builder_or_validator_emission(
    perimeter_module: Any,
) -> None:
    inputs = _fixture_inputs()
    valid_payload = json.loads(_build(perimeter_module, inputs))
    baseline_payload = inputs["baseline"].json_object()
    baseline_payload["baseline_schema_version"] = -1
    inputs["baseline"] = InputSnapshot.from_bytes(
        "negative-version synthetic baseline", canonical_json_bytes(baseline_payload)
    )

    with pytest.raises(
        perimeter_module.AccountAssignmentPerimeterError,
        match="baseline_schema_version must be a non-negative integer",
    ):
        _build(perimeter_module, inputs)
    with pytest.raises(
        perimeter_module.AccountAssignmentPerimeterError,
        match="baseline_schema_version must be a non-negative integer",
    ):
        _validate(perimeter_module, valid_payload, inputs)
