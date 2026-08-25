"""Behavioral contract for the account-strategy epoch producer.

All fixtures are synthetic.  The tests exercise the producer boundary rather
than relying on a panel mock, so a producer regression cannot be hidden by a
consumer fixture.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "account_strategy_manifest.py"
LEDGER_GENERATION_ID = "1" * 64
BASELINE_GENERATION_ID = "2" * 64


@pytest.fixture(scope="module")
def manifest_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "account_strategy_manifest", MODULE_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bytes(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(payload: object) -> str:
    return hashlib.sha256(_bytes(payload)).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.write_bytes(_bytes(payload))


def _flow_classification_policy(as_of_date: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_id": "policy/synthetic-flow-classification.json",
        "as_of_date": as_of_date,
        "policy_id": "SYNTHETIC-FLOW-CLASSIFICATION",
        "policy_version": "1",
        "event_dispositions": {
            "exact": [
                {
                    "flow_type": value,
                    "disposition": "EXTERNAL",
                    "amount_sign": amount_sign,
                }
                for value, amount_sign in (
                    ("EXTERNAL_CONTRIBUTION", "POSITIVE"),
                    ("EXTERNAL_WITHDRAWAL", "NEGATIVE"),
                    ("EXTERNAL_PROVIDER_TRANSFER", "NONZERO"),
                    ("DEPOSIT_OR_WITHDRAWAL", "NONZERO"),
                )
            ]
            + [
                {
                    "flow_type": "ACCOUNT_TRANSFER",
                    "disposition": "INTERNAL",
                    "amount_sign": "NONZERO",
                    "counterparty_rule": (
                        "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT"
                    ),
                }
            ],
            "prefix": [
                {
                    "flow_type_prefix": "TRANSFER:INTERNAL:",
                    "disposition": "INTERNAL",
                    "amount_sign": "NONZERO",
                    "counterparty_rule": (
                        "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT"
                    ),
                }
            ],
        },
    }


def _flow_classification_rules() -> dict[str, Any]:
    policy = _flow_classification_policy("2026-01-06")["event_dispositions"]
    return {
        "exact": {
            rule["flow_type"]: {
                key: value
                for key, value in rule.items()
                if key in {"disposition", "amount_sign", "counterparty_rule"}
            }
            for rule in policy["exact"]
        },
        "prefix": policy["prefix"],
    }


def _baseline_assignments(*, gap: bool = False) -> list[dict[str, Any]]:
    second_start = "2026-01-05" if gap else "2026-01-04"
    return [
        {
            "account_id": "synthetic-account",
            "strategy_id": "synthetic-strategy-a",
            "effective_start": "2026-01-01",
            "effective_end": "2026-01-03",
        },
        {
            "account_id": "synthetic-account",
            "strategy_id": "synthetic-strategy-b",
            "effective_start": second_start,
            "effective_end": None,
        },
    ]


def _admission_policy(
    assignments: list[dict[str, Any]], *, continuity: bool
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "policy_id": "SYNTHETIC-ACCOUNT-STRATEGY-ADMISSION",
        "policy_version": "1",
        "artifact_id": "policy/synthetic-account-strategy-admission.json",
        "as_of_date": "2026-01-06",
        "assignment_sha256": _sha256(assignments),
        "admissions": [
            {
                "account_id": assignment["account_id"],
                "strategy_id": assignment["strategy_id"],
                "effective_start": assignment["effective_start"],
                "effective_end": assignment["effective_end"],
                "status": "FUNDED",
                "authority": "SYNTHETIC_TEST_AUTHORITY",
                "continuity_to_successor": continuity and index == 0,
            }
            for index, assignment in enumerate(assignments)
        ],
    }


def _write_valid_inputs(tmp_path: Path, *, gap: bool = False) -> dict[str, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    ledger_path = tmp_path / "provider-ledger.json"
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [
            {
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "50"},
            },
            {
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-02",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "60"},
            },
            {
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-03",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "70"},
            },
            {
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-04",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "70"},
            },
            {
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-05",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "75"},
            },
            {
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-06",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "80"},
            },
        ],
        "flows": [
            {
                "record_id": "raw-flow-1",
                "account_id": "synthetic-account",
                "event_date": "2026-01-02",
                "flow_type": "EXTERNAL_CONTRIBUTION",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "10"},
            }
        ],
    }
    _write_json(ledger_path, ledger)
    ledger_bytes = ledger_path.read_bytes()
    ledger_identity = {
        "schema_version": 1,
        "generation_id": LEDGER_GENERATION_ID,
        "artifact": {
            "artifact_id": "normalized/provider-ledger.json",
            "as_of_date": "2026-01-06",
            "schema": {"name": "provider-ledger", "version": "1"},
            "sha256": hashlib.sha256(ledger_bytes).hexdigest(),
            "size_bytes": len(ledger_bytes),
        },
    }
    ledger_identity_path = tmp_path / "ledger-identity.json"
    _write_json(ledger_identity_path, ledger_identity)
    ledger_source_identity = {
        "generation_id": ledger_identity["generation_id"],
        "artifact": ledger_identity["artifact"],
    }

    baseline_root = tmp_path / "baseline"
    generation_id = BASELINE_GENERATION_ID
    generation_dir = baseline_root / "generations" / generation_id
    generation_dir.mkdir(parents=True)
    _write_json(baseline_root / "CURRENT.json", {"generation_id": generation_id})
    assignments = _baseline_assignments(gap=gap)
    _write_json(
        generation_dir / "portfolio-baseline.json",
        {
            "as_of_date": "2026-01-06",
            "baseline_schema_version": 1,
            "accounts": [{"account_id": "synthetic-account", "provider": "SYNTHETIC"}],
            "assignments": assignments,
        },
    )

    policy_path = tmp_path / "admission-policy.json"
    _write_json(policy_path, _admission_policy(assignments, continuity=True))

    classification_policy_path = tmp_path / "flow-classification-policy.json"
    _write_json(classification_policy_path, _flow_classification_policy("2026-01-06"))
    classification_policy_sha256 = hashlib.sha256(
        classification_policy_path.read_bytes()
    ).hexdigest()
    flows_path = tmp_path / "classified-flows.json"
    _write_json(
        flows_path,
        {
            "schema_version": 3,
            "artifact_id": "classified/synthetic-flows.json",
            "as_of_date": "2026-01-06",
            "ledger_identity": ledger_source_identity,
            "classification_policy": {
                "policy_id": "SYNTHETIC-FLOW-CLASSIFICATION",
                "policy_version": "1",
                "policy_as_of_date": "2026-01-06",
                "policy_sha256": classification_policy_sha256,
            },
            "events": [
                {
                    "event_id": "classified-flow-1",
                    "source_record_id": "raw-flow-1",
                    "account_id": "synthetic-account",
                    "event_date": "2026-01-02",
                    "amount": "10",
                    "disposition": "EXTERNAL",
                }
            ],
        },
    )
    return {
        "ledger": ledger_path,
        "ledger_identity": ledger_identity_path,
        "baseline_root": baseline_root,
        "policy": policy_path,
        "classification_policy": classification_policy_path,
        "flows": flows_path,
    }


def _producer_argv(inputs: dict[str, Path]) -> list[str]:
    return [
        "--ledger-json",
        str(inputs["ledger"]),
        "--ledger-identity-json",
        str(inputs["ledger_identity"]),
        "--baseline-root",
        str(inputs["baseline_root"]),
        "--baseline-generation-id",
        BASELINE_GENERATION_ID,
        "--admission-policy-json",
        str(inputs["policy"]),
        "--flow-classes-json",
        str(inputs["flows"]),
        "--classification-policy-json",
        str(inputs["classification_policy"]),
        "--format",
        "json",
    ]


def _refresh_ledger_identity(inputs: dict[str, Path]) -> None:
    """Keep synthetic cross-artifact source identities internally exact."""
    ledger_bytes = inputs["ledger"].read_bytes()
    identity = json.loads(inputs["ledger_identity"].read_text(encoding="utf-8"))
    identity["artifact"]["sha256"] = hashlib.sha256(ledger_bytes).hexdigest()
    identity["artifact"]["size_bytes"] = len(ledger_bytes)
    _write_json(inputs["ledger_identity"], identity)
    classified = json.loads(inputs["flows"].read_text(encoding="utf-8"))
    classified["ledger_identity"] = {
        "generation_id": identity["generation_id"],
        "artifact": identity["artifact"],
    }
    _write_json(inputs["flows"], classified)


def _manifest_from_cli(
    manifest_module: Any,
    inputs: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> dict[str, Any]:
    assert manifest_module.main(_producer_argv(inputs)) == 0
    return json.loads(capsys.readouterr().out)


def test_duplicate_account_date_nav_is_rejected_even_when_values_match(
    manifest_module: Any,
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [
            {
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "50"},
            },
            {
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "50"},
            },
        ],
        "flows": [],
    }

    with pytest.raises(manifest_module.ManifestError, match="duplicate NAV"):
        manifest_module.nav_series(ledger)


def test_cli_requires_exact_classified_flow_coverage(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    _write_json(
        inputs["flows"],
        {
            "schema_version": 3,
            "artifact_id": "classified/synthetic-flows.json",
            "as_of_date": "2026-01-06",
            "ledger_identity": {
                key: value
                for key, value in json.loads(
                    inputs["ledger_identity"].read_text()
                ).items()
                if key != "schema_version"
            },
            "classification_policy": {
                "policy_id": "SYNTHETIC-FLOW-CLASSIFICATION",
                "policy_version": "1",
                "policy_as_of_date": "2026-01-06",
                "policy_sha256": hashlib.sha256(
                    inputs["classification_policy"].read_bytes()
                ).hexdigest(),
            },
            "events": [],
        },
    )

    with pytest.raises(manifest_module.ManifestError, match="missing classified"):
        manifest_module.main(_producer_argv(inputs))


def test_cli_rejects_gap_when_policy_claims_epoch_continuity(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path, gap=True)

    with pytest.raises(manifest_module.ManifestError, match="gap"):
        manifest_module.main(_producer_argv(inputs))


def test_cli_emits_bound_source_identities_and_accepts_low_funded_nav(
    manifest_module: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inputs = _write_valid_inputs(tmp_path)

    assert manifest_module.main(_producer_argv(inputs)) == 0

    manifest = json.loads(capsys.readouterr().out)
    assert manifest["schema_version"] == 3
    assert manifest["source_identities"]["ledger"]["generation_id"] == (
        LEDGER_GENERATION_ID
    )
    assert manifest["source_identities"]["classified_flows"]["schema_version"] == 3
    assert (
        manifest["source_identities"]["flow_classification_policy"]["artifact"][
            "sha256"
        ]
        == hashlib.sha256(inputs["classification_policy"].read_bytes()).hexdigest()
    )
    assert manifest["source_identities"]["admission_policy"]["policy_id"] == (
        "SYNTHETIC-ACCOUNT-STRATEGY-ADMISSION"
    )
    first = manifest["rows"][0]
    assert first["status"] == "OK"
    assert first["opening_capital"] == "50"
    assert first["invested"] == "60"
    assert first["current_value"] == "70"
    assert first["gain"] == "10"
    assert (
        first["return_on_net_capital"] == "0.1666666666666666666666666666666666666667"
    )
    assert "mwr" not in first


def test_reusable_build_candidate_matches_cli_output(
    manifest_module: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The panel can independently rebuild exactly the manifest the CLI emits."""
    inputs = _write_valid_inputs(tmp_path)
    expected = _manifest_from_cli(manifest_module, inputs, capsys)

    actual = manifest_module.build_manifest_candidate(
        ledger_path=inputs["ledger"],
        ledger_identity_path=inputs["ledger_identity"],
        baseline_root=inputs["baseline_root"],
        baseline_generation_id=BASELINE_GENERATION_ID,
        classified_flows_path=inputs["flows"],
        classification_policy_path=inputs["classification_policy"],
        admission_policy_path=inputs["policy"],
    )
    assert actual == expected


def test_cli_rejects_classified_policy_hash_not_matching_selected_policy(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    classified = json.loads(inputs["flows"].read_text(encoding="utf-8"))
    classified["classification_policy"]["policy_sha256"] = "f" * 64
    _write_json(inputs["flows"], classified)

    with pytest.raises(manifest_module.ManifestError, match="classification policy"):
        manifest_module.main(_producer_argv(inputs))


def test_cli_rejects_classified_disposition_forgery_against_selected_policy(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    classified = json.loads(inputs["flows"].read_text(encoding="utf-8"))
    classified["events"][0]["disposition"] = "INTERNAL"
    _write_json(inputs["flows"], classified)

    with pytest.raises(manifest_module.ManifestError, match="differs from raw record"):
        manifest_module.main(_producer_argv(inputs))


def test_cli_rejects_flow_currency_that_differs_from_accounting_currency(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    ledger = json.loads(inputs["ledger"].read_text(encoding="utf-8"))
    ledger["flows"][0]["amount"]["currency"] = "EUR"
    _write_json(inputs["ledger"], ledger)
    _refresh_ledger_identity(inputs)

    with pytest.raises(manifest_module.ManifestError, match="currency"):
        manifest_module.main(_producer_argv(inputs))


def test_cli_binds_identity_artifact_and_ignores_mutable_current_pointer(
    manifest_module: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inputs = _write_valid_inputs(tmp_path)

    manifest = _manifest_from_cli(manifest_module, inputs, capsys)

    _write_json(inputs["baseline_root"] / "CURRENT.json", {"generation_id": "f" * 64})
    rebuilt = _manifest_from_cli(manifest_module, inputs, capsys)
    assert rebuilt == manifest
    assert (
        manifest["source_identities"]["ledger"]["identity_artifact"]["sha256"]
        == hashlib.sha256(inputs["ledger_identity"].read_bytes()).hexdigest()
    )


def test_cli_rejects_non_hash_generation_ids_before_path_selection(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    argv = _producer_argv(inputs)
    argv[argv.index("--baseline-generation-id") + 1] = "../../outside"

    with pytest.raises(manifest_module.ManifestError, match="SHA-256 generation id"):
        manifest_module.main(argv)


def test_cli_rejects_assignment_gap_without_a_continuity_claim(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path, gap=True)
    policy = json.loads(inputs["policy"].read_text(encoding="utf-8"))
    for admission in policy["admissions"]:
        admission["continuity_to_successor"] = False
    _write_json(inputs["policy"], policy)

    with pytest.raises(manifest_module.ManifestError, match="gap"):
        manifest_module.main(_producer_argv(inputs))


def test_nav_series_rejects_available_unavailable_duplicate_account_date(
    manifest_module: Any,
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [
            {
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "50"},
            },
            {
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "nav": {"currency": "USD", "status": "UNAVAILABLE"},
            },
        ],
        "flows": [],
    }

    with pytest.raises(manifest_module.ManifestError, match="duplicate NAV"):
        manifest_module.nav_series(ledger)


def test_cli_rejects_assignment_for_unknown_baseline_account(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    baseline_path = (
        inputs["baseline_root"]
        / "generations"
        / BASELINE_GENERATION_ID
        / "portfolio-baseline.json"
    )
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["assignments"][0]["account_id"] = "unknown-account"
    _write_json(baseline_path, baseline)
    policy = json.loads(inputs["policy"].read_text(encoding="utf-8"))
    policy["admissions"][0]["account_id"] = "unknown-account"
    policy["assignment_sha256"] = manifest_module._assignment_identity(
        baseline["assignments"]
    )[0]
    _write_json(inputs["policy"], policy)

    with pytest.raises(
        manifest_module.ManifestError, match="assignment account is unknown"
    ):
        manifest_module.main(_producer_argv(inputs))


@pytest.mark.parametrize(
    ("label", "observation"),
    [
        (
            "before every epoch",
            {
                "account_id": "synthetic-account",
                "as_of_date": "2025-12-31",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "49"},
            },
        ),
        (
            "unknown account",
            {
                "account_id": "unknown-account",
                "as_of_date": "2026-01-02",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "50"},
            },
        ),
    ],
)
def test_cli_rejects_nav_evidence_outside_known_baseline_epoch(
    manifest_module: Any,
    tmp_path: Path,
    label: str,
    observation: dict[str, Any],
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    ledger = json.loads(inputs["ledger"].read_text(encoding="utf-8"))
    ledger["nav_observations"].append(observation)
    _write_json(inputs["ledger"], ledger)
    _refresh_ledger_identity(inputs)

    with pytest.raises(
        manifest_module.ManifestError, match="baseline assignment|account is unknown"
    ):
        manifest_module.main(_producer_argv(inputs))


def test_cli_rejects_reconciled_flow_outside_known_epoch(
    manifest_module: Any, tmp_path: Path
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    ledger = json.loads(inputs["ledger"].read_text(encoding="utf-8"))
    ledger["flows"][0]["event_date"] = "2025-12-31"
    _write_json(inputs["ledger"], ledger)
    classified = json.loads(inputs["flows"].read_text(encoding="utf-8"))
    classified["events"][0]["event_date"] = "2025-12-31"
    _write_json(inputs["flows"], classified)
    _refresh_ledger_identity(inputs)

    with pytest.raises(manifest_module.ManifestError, match="baseline assignment"):
        manifest_module.main(_producer_argv(inputs))


def test_cli_uses_one_ledger_snapshot_for_hash_and_calculation(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    captured = inputs["ledger"].read_bytes()
    original_read_bytes = Path.read_bytes
    ledger_read_count = 0

    def read_bytes_once(path: Path) -> bytes:
        nonlocal ledger_read_count
        if path == inputs["ledger"]:
            ledger_read_count += 1
            if ledger_read_count == 1:
                ledger = json.loads(captured.decode("utf-8"))
                ledger["nav_observations"][0]["nav"]["value"] = "90"
                _write_json(path, ledger)
                return captured
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes_once)
    manifest = _manifest_from_cli(manifest_module, inputs, capsys)

    assert ledger_read_count == 1
    assert manifest["rows"][0]["opening_capital"] == "50"


def test_validator_rejects_blocked_row_with_financial_metrics(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    blocked = manifest["rows"][0]
    blocked["status"] = "BLOCKED_ADMISSION"
    blocked["admission_status"] = "FUNDED"
    blocked["blocking_reason"] = "SYNTHETIC_BLOCK"

    with pytest.raises(manifest_module.ManifestError, match="formal schema"):
        manifest_module.validate_manifest_schema(manifest)


def test_validator_requires_nonempty_late_flows_and_canonical_dates(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    row = manifest["rows"][0]
    row["status"] = "FLOWS_AFTER_CLOSING_NAV"
    row["late_flows"] = []
    for field in (
        "start",
        "end",
        "opening_capital",
        "deposits",
        "internal_in",
        "invested",
        "external_withdrawals",
        "internal_out",
        "withdrawn",
        "net_capital",
        "current_value",
        "gain",
        "return_on_net_capital",
        "annualized_return_on_net_capital",
        "xirr_diagnostic",
        "observation_linked_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "opening_basis",
        "observation_trace",
        "capital_flow_trace",
        "nav_observations",
    ):
        row.pop(field)

    with pytest.raises(manifest_module.ManifestError, match="formal schema"):
        manifest_module.validate_manifest_schema(manifest)

    row["late_flows"] = [{"class": "external", "date": "2026-1-7", "value": "10"}]
    with pytest.raises(manifest_module.ManifestError, match="formal schema"):
        manifest_module.validate_manifest_schema(manifest)


def test_successor_opened_from_predecessor_counts_first_day_flow(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-04": manifest_module.Decimal("80"),
            "2026-01-05": manifest_module.Decimal("80"),
            "2026-01-06": manifest_module.Decimal("80"),
        },
        {"2026-01-04": manifest_module.Decimal("10")},
        {},
        {},
        {},
        manifest_module.Decimal("70"),
        "2026-01-04",
        None,
        "2026-01-03",
    )

    assert stats is not None
    assert stats["invested"] == manifest_module.Decimal("80")
    assert stats["current_value"] == manifest_module.Decimal("80")
    assert stats["gain"] == manifest_module.Decimal("0")
    assert stats["return_on_net_capital"] == manifest_module.Decimal("0")


@pytest.mark.parametrize(
    ("first_nav", "first_day_flow", "expected_return"),
    [
        ("150", None, "0.5"),
        ("150", "50", "0"),
    ],
)
def test_successor_observation_link_includes_predecessor_closing_segment(
    manifest_module: Any,
    first_nav: str,
    first_day_flow: str | None,
    expected_return: str,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-04": manifest_module.Decimal(first_nav),
            "2026-01-05": manifest_module.Decimal(first_nav),
            "2026-01-06": manifest_module.Decimal(first_nav),
        },
        (
            {"2026-01-04": manifest_module.Decimal(first_day_flow)}
            if first_day_flow is not None
            else {}
        ),
        {},
        {},
        {},
        manifest_module.Decimal("100"),
        "2026-01-04",
        None,
        "2026-01-03",
    )

    assert stats is not None
    assert stats["observation_linked_return"] == manifest_module.Decimal(
        expected_return
    )


@pytest.mark.parametrize(
    "navs",
    [
        ["100", "0", "100"],
        ["-1", "100", "100"],
    ],
)
def test_nonpositive_nav_return_base_is_blocked_without_metrics(
    manifest_module: Any, navs: list[str]
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            f"2026-01-0{index + 1}": manifest_module.Decimal(value)
            for index, value in enumerate(navs)
        },
        {},
        {},
        {},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats == {
        "unavailable_status": "BLOCKED_UNDEFINED_RETURN",
        "blocking_reason": "NONPOSITIVE_NAV_RETURN_BASE",
    }


def test_flow_before_first_available_nav_without_predecessor_is_blocked(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-05": manifest_module.Decimal("150"),
            "2026-01-06": manifest_module.Decimal("150"),
            "2026-01-07": manifest_module.Decimal("150"),
        },
        {"2026-01-02": manifest_module.Decimal("50")},
        {},
        {},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats == {
        "unavailable_status": "BLOCKED_UNPROVABLE_OPENING_FLOWS",
        "blocking_reason": (
            "FLOW_ON_OR_BEFORE_FIRST_AVAILABLE_NAV_WITHOUT_PREDECESSOR_CLOSING"
        ),
    }


def test_same_day_opposite_external_flows_preserve_gross_legs(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-01": manifest_module.Decimal("100"),
            "2026-01-02": manifest_module.Decimal("100"),
            "2026-01-03": manifest_module.Decimal("100"),
        },
        {"2026-01-02": manifest_module.Decimal("100")},
        {"2026-01-02": manifest_module.Decimal("100")},
        {},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats is not None
    assert stats["invested"] == manifest_module.Decimal("200")
    assert stats["withdrawn"] == manifest_module.Decimal("100")
    assert stats["net_capital"] == manifest_module.Decimal("100")
    assert stats["gain"] == manifest_module.Decimal("0")


def test_nonpositive_net_capital_makes_ronc_diagnostics_unavailable(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-01": manifest_module.Decimal("100"),
            "2026-01-02": manifest_module.Decimal("100"),
            "2026-01-03": manifest_module.Decimal("100"),
        },
        {},
        {"2026-01-02": manifest_module.Decimal("100")},
        {},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats is not None
    assert stats["net_capital"] == manifest_module.Decimal("0")
    assert stats["return_on_net_capital"] is None
    assert stats["annualized_return_on_net_capital"] is None


def test_xirr_diagnostic_includes_internal_capital_legs(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-01": manifest_module.Decimal("100"),
            "2026-07-02": manifest_module.Decimal("200"),
            "2027-01-01": manifest_module.Decimal("200"),
        },
        {},
        {},
        {"2026-07-02": manifest_module.Decimal("100")},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats is not None
    assert stats["xirr_diagnostic"] == manifest_module.Decimal("0")


@pytest.mark.parametrize(
    ("end", "current"),
    [
        ("2026-12-31", "110"),
        ("2027-01-01", "110"),
        ("2027-01-02", "110"),
        ("2027-01-01", "90"),
    ],
)
def test_annualized_return_on_net_capital_is_consistently_linear(
    manifest_module: Any,
    end: str,
    current: str,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-01": manifest_module.Decimal("100"),
            "2026-06-01": manifest_module.Decimal("100"),
            end: manifest_module.Decimal(current),
        },
        {},
        {},
        {},
        {},
        None,
        "2026-01-01",
        None,
    )

    assert stats is not None
    span_days = (
        manifest_module.date.fromisoformat(end)
        - manifest_module.date.fromisoformat("2026-01-01")
    ).days
    expected = (
        (manifest_module.Decimal(current) - manifest_module.Decimal("100"))
        / manifest_module.Decimal("100")
        * manifest_module.Decimal("365")
        / manifest_module.Decimal(span_days)
    )
    assert stats["annualized_return_on_net_capital"] == expected


@pytest.mark.parametrize(
    ("ledger", "events"),
    [
        (
            {
                "accounting_currency": "USD",
                "nav_observations": [
                    {
                        "account_id": "synthetic-account",
                        "as_of_date": "2026-01-01",
                        "nav": {
                            "currency": "USD",
                            "status": "AVAILABLE",
                            "value": "1.001",
                        },
                    }
                ],
                "flows": [],
            },
            None,
        ),
        (
            {
                "accounting_currency": "USD",
                "nav_observations": [],
                "flows": [
                    {
                        "record_id": "raw-flow-1",
                        "account_id": "synthetic-account",
                        "event_date": "2026-01-01",
                        "flow_type": "EXTERNAL_CONTRIBUTION",
                        "amount": {
                            "currency": "USD",
                            "status": "AVAILABLE",
                            "value": "1.00",
                        },
                    }
                ],
            },
            [
                {
                    "event_id": "classified-flow-1",
                    "source_record_id": "raw-flow-1",
                    "account_id": "synthetic-account",
                    "event_date": "2026-01-01",
                    "amount": "1.001",
                    "disposition": "EXTERNAL",
                }
            ],
        ),
    ],
)
def test_source_money_values_must_be_exactly_cent_representable(
    manifest_module: Any, ledger: dict[str, Any], events: list[dict[str, Any]] | None
) -> None:
    with pytest.raises(manifest_module.ManifestError, match="cent"):
        if events is None:
            manifest_module.nav_series(ledger)
        else:
            manifest_module.validate_classified_flow_coverage(
                ledger, events, _flow_classification_rules()
            )


@pytest.mark.parametrize(
    "nav",
    [
        {"status": "AVAILABLE"},
        {"status": "UNAVAILABLE", "value": "100"},
        {"status": "UNAVAILABLE"},
        {"status": "BROKEN", "value": "100"},
    ],
)
def test_nav_status_contract_is_explicit_and_fail_closed(
    manifest_module: Any, nav: dict[str, str]
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [
            {
                "account_id": "synthetic-account",
                "as_of_date": "2026-01-01",
                "nav": nav,
            }
        ],
        "flows": [],
    }

    with pytest.raises(manifest_module.ManifestError, match="nav"):
        manifest_module.nav_series(ledger)


def test_unavailable_nav_inside_epoch_blocks_the_row(
    manifest_module: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inputs = _write_valid_inputs(tmp_path)
    ledger = json.loads(inputs["ledger"].read_text(encoding="utf-8"))
    observation = next(
        item
        for item in ledger["nav_observations"]
        if item["account_id"] == "synthetic-account"
        and item["as_of_date"] == "2026-01-05"
    )
    observation["nav"] = {
        "currency": "USD",
        "status": "UNAVAILABLE",
        "reason": "SOURCE_NOT_READY",
    }
    _write_json(inputs["ledger"], ledger)
    _refresh_ledger_identity(inputs)

    manifest = _manifest_from_cli(manifest_module, inputs, capsys)

    assert manifest["rows"][1]["status"] == "BLOCKED_UNAVAILABLE_NAV_OBSERVATION"
    assert (
        manifest["rows"][1]["blocking_reason"] == "UNAVAILABLE_NAV_OBSERVATION_IN_EPOCH"
    )


def test_semantic_validation_rejects_money_and_observed_index_mutations(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    valid_manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    manifest = json.loads(json.dumps(valid_manifest))
    manifest["rows"][0]["gain"] = "999"
    with pytest.raises(manifest_module.ManifestError, match="money identity"):
        manifest_module.validate_manifest_schema(manifest)

    manifest = json.loads(json.dumps(valid_manifest))
    manifest["rows"][0]["observed_max_drawdown"] = "0.01"
    with pytest.raises(
        manifest_module.ManifestError,
        match="observation trace summary|observed max drawdown",
    ):
        manifest_module.validate_manifest_schema(manifest)


@pytest.mark.parametrize("replacement", [999, "999.00"])
def test_semantic_validation_rejects_unreproducible_observed_hwm(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    replacement: object,
) -> None:
    valid_manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    manifest = json.loads(json.dumps(valid_manifest))
    manifest["rows"][0]["observed_hwm_index"] = replacement

    with pytest.raises(
        manifest_module.ManifestError,
        match="formal schema|observation trace|canonically spelled",
    ):
        manifest_module.validate_manifest_schema(manifest)


def test_semantic_validation_cross_binds_duplicate_identities(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    valid_manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    manifest = json.loads(json.dumps(valid_manifest))
    manifest["source_identities"]["classified_flows"]["ledger_identity"][
        "generation_id"
    ] = "b" * 64

    with pytest.raises(
        manifest_module.ManifestError, match="classified ledger identity"
    ):
        manifest_module.validate_manifest_schema(manifest)

    manifest = json.loads(json.dumps(valid_manifest))
    manifest["source_identities"]["assignment"]["count"] = 999
    with pytest.raises(manifest_module.ManifestError, match="assignment identity"):
        manifest_module.validate_manifest_schema(manifest)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("factor", "1.1"),
        ("cumulative_index", "101"),
        ("net_flow", "1"),
    ],
)
def test_semantic_validation_rederives_observation_trace(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
    replacement: str,
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    manifest["rows"][0]["observation_trace"]["links"][0][field] = replacement

    with pytest.raises(manifest_module.ManifestError, match="observation trace"):
        manifest_module.validate_manifest_schema(manifest)


def test_semantic_validation_rejects_trace_current_value_mismatch(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    manifest["rows"][0]["observation_trace"]["links"][-1]["ending_nav"] = "999"

    with pytest.raises(manifest_module.ManifestError, match="observation trace"):
        manifest_module.validate_manifest_schema(manifest)


def test_capital_flow_trace_rejects_coordinated_gross_aggregate_mutation(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path / "initial"), capsys
    )
    row = manifest["rows"][0]
    row.update(
        {
            "deposits": "110",
            "external_withdrawals": "100",
            "invested": "160",
            "withdrawn": "100",
            "net_capital": "60",
            "gain": "10",
            "return_on_net_capital": str(Decimal(1) / Decimal(6)),
            "annualized_return_on_net_capital": str(
                Decimal(1) / Decimal(6) * Decimal(365) / Decimal(2)
            ),
        }
    )

    with pytest.raises(
        manifest_module.ManifestError, match="capital flow trace totals"
    ):
        manifest_module.validate_manifest_schema(manifest)


def test_semantic_validation_recomputes_xirr_from_capital_trace(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path / "successor"), capsys
    )
    manifest["rows"][0]["xirr_diagnostic"] = "999"

    with pytest.raises(manifest_module.ManifestError, match="XIRR diagnostic"):
        manifest_module.validate_manifest_schema(manifest)


def test_predecessor_closing_date_is_the_xirr_opening_date(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-12-30": Decimal("100"),
            "2026-12-31": Decimal("150"),
            "2027-01-01": Decimal("200"),
        },
        {},
        {},
        {},
        {},
        Decimal("100"),
        "2026-12-30",
        None,
        "2026-01-01",
    )

    assert stats is not None
    assert stats["observation_trace"]["links"][0]["start_date"] == "2026-01-01"
    assert stats["xirr_diagnostic"] == manifest_module.xirr(
        [
            (manifest_module.date(2026, 1, 1), Decimal("-100")),
            (manifest_module.date(2027, 1, 1), Decimal("200")),
        ]
    )


def test_xirr_diagnostic_is_unavailable_when_cashflows_have_multiple_roots(
    manifest_module: Any,
) -> None:
    """A precise XIRR number is misleading when the cashflow stream is non-unique.

    For annual dates these amounts give the NPV polynomial
    ``-100 + 350x - 350x^2 + 100x^3`` where ``x = 1 / (1 + rate)``.
    It has three valid roots: -50%, 0%, and +100%.
    """
    cashflows = [
        (manifest_module.date(2025, 1, 1), Decimal("-100")),
        (manifest_module.date(2026, 1, 1), Decimal("350")),
        (manifest_module.date(2027, 1, 1), Decimal("-350")),
        (manifest_module.date(2028, 1, 1), Decimal("100")),
    ]

    assert manifest_module.xirr(cashflows) is None


@pytest.mark.parametrize(
    ("terminal_value", "expected"),
    [("0.02", "-0.98"), ("100", "99")],
)
def test_xirr_does_not_drop_unique_roots_outside_implicit_solver_bounds(
    manifest_module: Any, terminal_value: str, expected: str
) -> None:
    """A unique economically admissible root must not be lost to hidden bounds.

    These annual cashflows each have exactly one sign change, hence one root
    on r > -1 under the XIRR equation.  The current implementation silently
    returns None for the -98% and +9,900% roots because it searches only
    [-95%, +5,000%], despite no such admissibility bounds being in the
    manifest contract.
    """
    cashflows = [
        (manifest_module.date(2025, 1, 1), manifest_module.Decimal("-1")),
        (manifest_module.date(2026, 1, 1), manifest_module.Decimal(terminal_value)),
    ]

    actual = manifest_module.xirr(cashflows)

    assert actual is not None
    assert abs(actual - manifest_module.Decimal(expected)) < manifest_module.Decimal(
        "1e-30"
    )


def test_predecessor_continuity_annualized_ronc_counts_known_opening_exposure(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The annualization interval starts at a trusted predecessor close.

    This is deliberately end-to-end: the delayed first successor NAV is legal
    because the predecessor's closing NAV supplies the opening capital and the
    first observation link covers the intervening exposure.
    """
    inputs = _write_valid_inputs(tmp_path)
    ledger = json.loads(inputs["ledger"].read_text(encoding="utf-8"))
    ledger["nav_observations"] = [
        *ledger["nav_observations"][:3],
        {
            "account_id": "synthetic-account",
            "as_of_date": "2026-01-13",
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": "70"},
        },
        {
            "account_id": "synthetic-account",
            "as_of_date": "2026-06-01",
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": "70"},
        },
        {
            "account_id": "synthetic-account",
            "as_of_date": "2027-01-03",
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": "77"},
        },
    ]
    _write_json(inputs["ledger"], ledger)
    _refresh_ledger_identity(inputs)

    manifest = _manifest_from_cli(manifest_module, inputs, capsys)
    successor = manifest["rows"][1]

    assert successor["opening_basis"] == "PREDECESSOR_CLOSING_NAV"
    assert successor["observation_trace"]["links"][0]["start_date"] == "2026-01-03"
    assert Decimal(successor["return_on_net_capital"]) == Decimal("0.1")
    assert Decimal(successor["annualized_return_on_net_capital"]) == Decimal("0.1")


def test_semantic_validation_requires_predecessor_link_to_end_at_first_observed_nav(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A predecessor bridge must terminate at the row's first NAV date.

    The trace validator already requires the predecessor link to start before
    the row start, but without this end-date binding a forged trace can move
    that link past the first observed NAV while retaining every displayed
    financial number and all link arithmetic.
    """
    inputs = _write_valid_inputs(tmp_path)
    ledger = json.loads(inputs["ledger"].read_text(encoding="utf-8"))
    successor_observations = [
        {
            "account_id": "synthetic-account",
            "as_of_date": "2026-01-13",
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": "70"},
        },
        {
            "account_id": "synthetic-account",
            "as_of_date": "2026-06-01",
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": "70"},
        },
        {
            "account_id": "synthetic-account",
            "as_of_date": "2027-01-03",
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": "77"},
        },
    ]
    ledger["nav_observations"] = [
        *ledger["nav_observations"][:3],
        *successor_observations,
    ]
    _write_json(inputs["ledger"], ledger)
    _refresh_ledger_identity(inputs)

    manifest = _manifest_from_cli(manifest_module, inputs, capsys)
    successor = manifest["rows"][1]
    links = successor["observation_trace"]["links"]
    assert successor["start"] == "2026-01-13"
    assert links[0]["end_date"] == successor["start"]

    links[0]["end_date"] = "2026-01-14"
    links[1]["start_date"] = "2026-01-14"

    with pytest.raises(manifest_module.ManifestError, match="observation trace"):
        manifest_module.validate_manifest_schema(manifest)


def test_assignment_projection_and_continuity_handoff_are_semantic_constraints(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path / "initial"), capsys
    )
    manifest["rows"].pop()
    with pytest.raises(manifest_module.ManifestError, match="assignment projection"):
        manifest_module.validate_manifest_schema(manifest)

    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path / "successor"), capsys
    )
    successor = manifest["rows"][1]
    successor["opening_capital"] = "71"
    successor["invested"] = "71"
    successor["net_capital"] = "71"
    successor["gain"] = "9"
    successor["return_on_net_capital"] = str(Decimal(9) / Decimal(71))
    span_days = (
        manifest_module.date.fromisoformat(successor["end"])
        - manifest_module.date.fromisoformat(
            successor["observation_trace"]["links"][0]["start_date"]
        )
    ).days
    successor["annualized_return_on_net_capital"] = str(
        Decimal(9) / Decimal(71) * Decimal(365) / Decimal(span_days)
    )
    successor["observation_trace"]["links"][0]["base_nav"] = "71"
    with pytest.raises(
        manifest_module.ManifestError, match="continuity|observation trace"
    ):
        manifest_module.validate_manifest_schema(manifest)


def test_observation_trace_rejects_coordinated_nav_adjacency_mutation(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    links = manifest["rows"][0]["observation_trace"]["links"]
    links[0]["ending_nav"] = "61"
    links[1]["base_nav"] = "61"

    with pytest.raises(manifest_module.ManifestError, match="observation trace"):
        manifest_module.validate_manifest_schema(manifest)


def test_predecessor_link_includes_every_flow_before_first_successor_nav(
    manifest_module: Any,
) -> None:
    stats = manifest_module.analyze_epoch(
        {
            "2026-01-06": manifest_module.Decimal("120"),
            "2026-01-07": manifest_module.Decimal("120"),
            "2026-01-08": manifest_module.Decimal("120"),
        },
        {"2026-01-05": manifest_module.Decimal("20")},
        {},
        {},
        {},
        manifest_module.Decimal("100"),
        "2026-01-04",
        None,
        "2026-01-03",
    )

    assert stats is not None
    assert stats["observation_trace"]["links"][0][
        "net_flow"
    ] == manifest_module.Decimal("20")
    assert stats["observation_linked_return"] == manifest_module.Decimal("0")


def test_missing_predecessor_block_is_impossible_after_ok_predecessor(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    successor = manifest["rows"][1]
    successor["status"] = "BLOCKED_MISSING_PREDECESSOR_CONTINUITY"
    successor["blocking_reason"] = "PREDECESSOR_CONTINUITY_CLOSING_NAV_UNAVAILABLE"
    for field in (
        "start",
        "end",
        "opening_capital",
        "deposits",
        "internal_in",
        "invested",
        "external_withdrawals",
        "internal_out",
        "withdrawn",
        "net_capital",
        "current_value",
        "gain",
        "return_on_net_capital",
        "annualized_return_on_net_capital",
        "xirr_diagnostic",
        "observation_linked_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "opening_basis",
        "observation_trace",
        "capital_flow_trace",
        "nav_observations",
    ):
        successor.pop(field)

    with pytest.raises(
        manifest_module.ManifestError, match="continuity block is impossible"
    ):
        manifest_module.validate_manifest_schema(manifest)


def test_decimal_json_serialization_is_fixed_point_and_schema_round_trips(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert manifest_module.json_ready({"zero_xirr": Decimal("0E-20")}) == {
        "zero_xirr": "0"
    }
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )

    serialized = json.dumps(manifest_module.json_ready(manifest))
    reparsed = json.loads(serialized)
    manifest_module.validate_manifest_schema(reparsed)


def test_formatted_money_identity_remains_exact_to_two_decimals(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    displayed = manifest_module.format_table(manifest).splitlines()[-1].split("|")

    assert manifest_module.Decimal(displayed[8].strip()) - manifest_module.Decimal(
        displayed[7].strip()
    ) == manifest_module.Decimal(displayed[9].strip())


def _continuity_case(manifest_module: Any, case: str) -> list[dict[str, Any]]:
    first_key = ("synthetic-account", "strategy-a", "2026-01-01", "2026-01-04")
    second_key = ("synthetic-account", "strategy-b", "2026-01-05", None)
    chains = {
        "synthetic-account": [
            {"_key": first_key},
            {"_key": second_key},
        ]
    }
    admissions = {
        first_key: {
            "status": "BLOCKED" if case == "BLOCKED_ADMISSION" else "FUNDED",
            "authority": "SYNTHETIC_TEST_AUTHORITY",
            "continuity_to_successor": True,
        },
        second_key: {
            "status": "FUNDED",
            "authority": "SYNTHETIC_TEST_AUTHORITY",
            "continuity_to_successor": False,
        },
    }
    series: dict[str, dict[str, Any]] = {
        "synthetic-account": {
            "2026-01-01": manifest_module.Decimal("100"),
            "2026-01-02": manifest_module.Decimal("110"),
            "2026-01-03": manifest_module.Decimal("120"),
            "2026-01-05": manifest_module.Decimal("120"),
            "2026-01-06": manifest_module.Decimal("120"),
            "2026-01-07": manifest_module.Decimal("120"),
        }
    }
    buckets: dict[str, dict[str, dict[str, Any]]] = {
        "external_in": {"synthetic-account": {}},
        "external_out": {"synthetic-account": {}},
        "internal_in": {"synthetic-account": {}},
        "internal_out": {"synthetic-account": {}},
    }
    if case == "NO_ADMITTED_NAV":
        for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
            series["synthetic-account"].pop(day)
    elif case == "FLOWS_AFTER_CLOSING_NAV":
        buckets["external_in"]["synthetic-account"]["2026-01-04"] = (
            manifest_module.Decimal("10")
        )
    elif case == "BLOCKED_CONTINUITY_AFTER_WITHDRAWAL":
        buckets["external_out"]["synthetic-account"]["2026-01-02"] = (
            manifest_module.Decimal("10")
        )
    elif case == "BLOCKED_UNDEFINED_RETURN":
        series["synthetic-account"]["2026-01-02"] = manifest_module.Decimal("0")
        series["synthetic-account"]["2026-01-03"] = manifest_module.Decimal("100")

    manifest = manifest_module.assemble_manifest(
        series,
        {"synthetic-account": {"provider": "SYNTHETIC"}},
        buckets,
        chains,
        admissions,
        {},
    )
    return manifest["rows"]


@pytest.mark.parametrize(
    "predecessor_status",
    [
        "BLOCKED_ADMISSION",
        "NO_ADMITTED_NAV",
        "FLOWS_AFTER_CLOSING_NAV",
        "BLOCKED_CONTINUITY_AFTER_WITHDRAWAL",
        "BLOCKED_UNDEFINED_RETURN",
    ],
)
def test_failed_continuity_predecessor_blocks_successor(
    manifest_module: Any, predecessor_status: str
) -> None:
    rows = _continuity_case(manifest_module, predecessor_status)

    assert rows[0]["status"] == predecessor_status
    assert rows[1] == {
        "account_id": "synthetic-account",
        "provider": "SYNTHETIC",
        "strategy_id": "strategy-b",
        "effective_start": "2026-01-05",
        "effective_end": None,
        "admission_status": "FUNDED",
        "admission_policy_reference": "SYNTHETIC_TEST_AUTHORITY",
        "continuity_to_successor": False,
        "status": "BLOCKED_MISSING_PREDECESSOR_CONTINUITY",
        "blocking_reason": "PREDECESSOR_CONTINUITY_CLOSING_NAV_UNAVAILABLE",
    }


def test_schema_forbids_metrics_on_undefined_return_block(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )
    row = manifest["rows"][0]
    row["status"] = "BLOCKED_UNDEFINED_RETURN"
    row["blocking_reason"] = "NONPOSITIVE_NAV_RETURN_BASE"
    for field in (
        "start",
        "end",
        "opening_capital",
        "deposits",
        "internal_in",
        "invested",
        "external_withdrawals",
        "internal_out",
        "withdrawn",
        "net_capital",
        "current_value",
        "gain",
        "return_on_net_capital",
        "annualized_return_on_net_capital",
        "xirr_diagnostic",
        "observation_linked_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "opening_basis",
        "observation_trace",
        "capital_flow_trace",
        "nav_observations",
    ):
        row.pop(field)

    successor = manifest["rows"][1]
    successor["status"] = "BLOCKED_MISSING_PREDECESSOR_CONTINUITY"
    successor["blocking_reason"] = "PREDECESSOR_CONTINUITY_CLOSING_NAV_UNAVAILABLE"
    for field in (
        "start",
        "end",
        "opening_capital",
        "deposits",
        "internal_in",
        "invested",
        "external_withdrawals",
        "internal_out",
        "withdrawn",
        "net_capital",
        "current_value",
        "gain",
        "return_on_net_capital",
        "annualized_return_on_net_capital",
        "xirr_diagnostic",
        "observation_linked_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "opening_basis",
        "observation_trace",
        "capital_flow_trace",
        "nav_observations",
    ):
        successor.pop(field)

    manifest_module.validate_manifest_schema(manifest)
    row["gain"] = "0"
    with pytest.raises(manifest_module.ManifestError, match="formal schema"):
        manifest_module.validate_manifest_schema(manifest)


def test_cli_does_not_emit_private_account_number(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )

    assert "account_number" not in manifest["rows"][0]


def test_format_table_preserves_blocked_status_reason_and_column_count(
    manifest_module: Any,
) -> None:
    table = manifest_module.format_table(
        {
            "rows": [
                {
                    "account_id": "synthetic-account",
                    "strategy_id": "synthetic-strategy",
                    "effective_start": "2026-01-01",
                    "effective_end": None,
                    "status": "BLOCKED_ADMISSION",
                    "blocking_reason": "EPOCH_NOT_EXPLICITLY_FUNDED",
                }
            ]
        }
    )
    row = table.splitlines()[-1]

    assert table.splitlines()[1].count("|") == 15
    assert row.count("|") == 15
    assert "BLOCKED_ADMISSION: EPOCH_NOT_EXPLICITLY_FUNDED" in row


def test_output_declares_source_bound_not_independently_authority_verified(
    manifest_module: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest_from_cli(
        manifest_module, _write_valid_inputs(tmp_path), capsys
    )

    assert manifest["authority_readiness"] == {
        "authority_status": "SOURCE_BOUND_CANDIDATE",
        "independent_authority_verification": "NOT_PERFORMED",
        "publication_readiness": "NOT_READY",
    }


@pytest.mark.parametrize(
    ("events", "message"),
    [
        (
            [
                {
                    "event_id": "event-1",
                    "source_record_id": "raw-1",
                    "account_id": "account-1",
                    "event_date": "2026-01-01",
                    "amount": "11",
                    "disposition": "EXTERNAL",
                }
            ],
            "differs from raw",
        ),
        (
            [
                {
                    "event_id": "event-1",
                    "source_record_id": "raw-missing",
                    "account_id": "account-1",
                    "event_date": "2026-01-01",
                    "amount": "10",
                    "disposition": "EXTERNAL",
                }
            ],
            "extra source_record_id",
        ),
        (
            [
                {
                    "event_id": "event-1",
                    "source_record_id": "raw-1",
                    "account_id": "account-1",
                    "event_date": "2026-01-01",
                    "amount": "10",
                    "disposition": "EXTERNAL",
                },
                {
                    "event_id": "event-1",
                    "source_record_id": "raw-2",
                    "account_id": "account-1",
                    "event_date": "2026-01-02",
                    "amount": "20",
                    "disposition": "EXTERNAL",
                },
            ],
            "duplicate classified event_id",
        ),
    ],
)
def test_classified_event_reconciliation_rejects_non_bijective_records(
    manifest_module: Any, events: list[dict[str, str]], message: str
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [],
        "flows": [
            {
                "record_id": "raw-1",
                "account_id": "account-1",
                "event_date": "2026-01-01",
                "flow_type": "EXTERNAL_CONTRIBUTION",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "10"},
            },
            {
                "record_id": "raw-2",
                "account_id": "account-1",
                "event_date": "2026-01-02",
                "flow_type": "EXTERNAL_CONTRIBUTION",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "20"},
            },
        ],
    }

    with pytest.raises(manifest_module.ManifestError, match=message):
        manifest_module.validate_classified_flow_coverage(
            ledger, events, _flow_classification_rules()
        )


def test_classified_event_reconciliation_rejects_ambiguous_transfer(
    manifest_module: Any,
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [],
        "flows": [
            {
                "record_id": "raw-a",
                "account_id": "account-a",
                "event_date": "2026-01-01",
                "flow_type": "ACCOUNT_TRANSFER",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "10"},
            },
            {
                "record_id": "raw-b",
                "account_id": "account-b",
                "event_date": "2026-01-01",
                "flow_type": "ACCOUNT_TRANSFER",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-10"},
            },
            {
                "record_id": "raw-c",
                "account_id": "account-c",
                "event_date": "2026-01-01",
                "flow_type": "ACCOUNT_TRANSFER",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "-10"},
            },
        ],
    }

    with pytest.raises(manifest_module.ManifestError, match="unpaired or ambiguous"):
        manifest_module.validate_classified_flow_coverage(
            ledger, [], _flow_classification_rules()
        )


def test_internal_prefix_transfer_requires_exact_opposite_counterparty(
    manifest_module: Any,
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [],
        "flows": [
            {
                "record_id": "orphan-internal",
                "account_id": "account-a",
                "event_date": "2026-01-01",
                "flow_type": "TRANSFER:INTERNAL:ORPHAN",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "100"},
            }
        ],
    }
    events = [
        {
            "event_id": "classified-orphan",
            "source_record_id": "orphan-internal",
            "account_id": "account-a",
            "event_date": "2026-01-01",
            "amount": "100",
            "disposition": "INTERNAL",
        }
    ]

    with pytest.raises(manifest_module.ManifestError, match="unpaired or ambiguous"):
        manifest_module.validate_classified_flow_coverage(
            ledger, events, _flow_classification_rules()
        )


def test_external_withdrawal_rejects_positive_amount_direction(
    manifest_module: Any,
) -> None:
    ledger = {
        "accounting_currency": "USD",
        "nav_observations": [],
        "flows": [
            {
                "record_id": "wrong-sign-withdrawal",
                "account_id": "account-a",
                "event_date": "2026-01-01",
                "flow_type": "EXTERNAL_WITHDRAWAL",
                "amount": {"currency": "USD", "status": "AVAILABLE", "value": "100"},
            }
        ],
    }
    events = [
        {
            "event_id": "classified-wrong-sign",
            "source_record_id": "wrong-sign-withdrawal",
            "account_id": "account-a",
            "event_date": "2026-01-01",
            "amount": "100",
            "disposition": "EXTERNAL",
        }
    ]

    with pytest.raises(manifest_module.ManifestError, match="amount sign"):
        manifest_module.validate_classified_flow_coverage(
            ledger, events, _flow_classification_rules()
        )


def _coverage_ledger(flows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"accounting_currency": "USD", "nav_observations": [], "flows": flows}


def _raw_flow(
    record_id: str,
    *,
    account_id: str = "account-a",
    event_date: str = "2026-01-01",
    flow_type: str,
    amount: str,
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "account_id": account_id,
        "event_date": event_date,
        "flow_type": flow_type,
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": amount},
    }


def _classified_flow(
    event_id: str,
    source_record_id: str,
    *,
    account_id: str,
    event_date: str,
    amount: str,
    disposition: str,
    counterparty_source_record_id: str | None = None,
) -> dict[str, Any]:
    event = {
        "event_id": event_id,
        "source_record_id": source_record_id,
        "account_id": account_id,
        "event_date": event_date,
        "amount": amount,
        "disposition": disposition,
    }
    if counterparty_source_record_id is not None:
        event["counterparty_source_record_id"] = counterparty_source_record_id
    return event


def test_external_contribution_rejects_negative_amount_direction(
    manifest_module: Any,
) -> None:
    ledger = _coverage_ledger(
        [
            _raw_flow(
                "negative-contribution", flow_type="EXTERNAL_CONTRIBUTION", amount="-10"
            )
        ]
    )
    events = [
        _classified_flow(
            "classified-negative-contribution",
            "negative-contribution",
            account_id="account-a",
            event_date="2026-01-01",
            amount="-10",
            disposition="EXTERNAL",
        )
    ]

    with pytest.raises(manifest_module.ManifestError, match="amount sign"):
        manifest_module.validate_classified_flow_coverage(
            ledger, events, _flow_classification_rules()
        )


@pytest.mark.parametrize(
    "flow_type", ["EXTERNAL_PROVIDER_TRANSFER", "DEPOSIT_OR_WITHDRAWAL"]
)
def test_zero_nonzero_flow_rejects_zero_amount(
    manifest_module: Any, flow_type: str
) -> None:
    ledger = _coverage_ledger([_raw_flow("zero-flow", flow_type=flow_type, amount="0")])
    events = [
        _classified_flow(
            "classified-zero-flow",
            "zero-flow",
            account_id="account-a",
            event_date="2026-01-01",
            amount="0",
            disposition="EXTERNAL",
        )
    ]

    with pytest.raises(manifest_module.ManifestError, match="amount sign"):
        manifest_module.validate_classified_flow_coverage(
            ledger, events, _flow_classification_rules()
        )


@pytest.mark.parametrize("amount", ["10", "-10"])
def test_deposit_or_withdrawal_accepts_both_amount_directions(
    manifest_module: Any, amount: str
) -> None:
    ledger = _coverage_ledger(
        [
            _raw_flow(
                "bidirectional-flow", flow_type="DEPOSIT_OR_WITHDRAWAL", amount=amount
            )
        ]
    )
    events = [
        _classified_flow(
            "classified-bidirectional-flow",
            "bidirectional-flow",
            account_id="account-a",
            event_date="2026-01-01",
            amount=amount,
            disposition="EXTERNAL",
        )
    ]

    reconciled = manifest_module.validate_classified_flow_coverage(
        ledger, events, _flow_classification_rules()
    )
    assert reconciled[0]["amount"] == manifest_module.Decimal(amount)


def test_internal_prefix_transfer_accepts_valid_paired_counterparties(
    manifest_module: Any,
) -> None:
    ledger = _coverage_ledger(
        [
            _raw_flow(
                "prefix-in",
                account_id="account-a",
                flow_type="TRANSFER:INTERNAL:ACME",
                amount="100",
            ),
            _raw_flow(
                "prefix-out",
                account_id="account-b",
                flow_type="TRANSFER:INTERNAL:ACME",
                amount="-100",
            ),
        ]
    )
    events = [
        _classified_flow(
            "classified-prefix-in",
            "prefix-in",
            account_id="account-a",
            event_date="2026-01-01",
            amount="100",
            disposition="INTERNAL",
            counterparty_source_record_id="prefix-out",
        ),
        _classified_flow(
            "classified-prefix-out",
            "prefix-out",
            account_id="account-b",
            event_date="2026-01-01",
            amount="-100",
            disposition="INTERNAL",
            counterparty_source_record_id="prefix-in",
        ),
    ]

    reconciled = manifest_module.validate_classified_flow_coverage(
        ledger, events, _flow_classification_rules()
    )
    assert [event["counterparty_source_record_id"] for event in reconciled] == [
        "prefix-out",
        "prefix-in",
    ]


@pytest.mark.parametrize(
    "mutated_pair",
    [
        ("2026-01-02", "account-b", "-100"),  # wrong date
        ("2026-01-01", "account-a", "-100"),  # same account
        ("2026-01-01", "account-b", "-99"),  # wrong amount
    ],
)
def test_internal_prefix_transfer_rejects_invalid_counterparty_pair(
    manifest_module: Any, mutated_pair: tuple[str, str, str]
) -> None:
    event_date, account_id, amount = mutated_pair
    ledger = _coverage_ledger(
        [
            _raw_flow(
                "prefix-in",
                account_id="account-a",
                flow_type="TRANSFER:INTERNAL:ACME",
                amount="100",
            ),
            _raw_flow(
                "prefix-out",
                account_id=account_id,
                event_date=event_date,
                flow_type="TRANSFER:INTERNAL:ACME",
                amount=amount,
            ),
        ]
    )

    with pytest.raises(manifest_module.ManifestError, match="unpaired or ambiguous"):
        manifest_module.validate_classified_flow_coverage(
            ledger, [], _flow_classification_rules()
        )


def test_internal_prefix_transfer_rejects_multiple_counterparty_candidates(
    manifest_module: Any,
) -> None:
    ledger = _coverage_ledger(
        [
            _raw_flow(
                "prefix-in",
                account_id="account-a",
                flow_type="TRANSFER:INTERNAL:ACME",
                amount="100",
            ),
            _raw_flow(
                "prefix-out-1",
                account_id="account-b",
                flow_type="TRANSFER:INTERNAL:ACME",
                amount="-100",
            ),
            _raw_flow(
                "prefix-out-2",
                account_id="account-c",
                flow_type="TRANSFER:INTERNAL:ACME",
                amount="-100",
            ),
        ]
    )

    with pytest.raises(manifest_module.ManifestError, match="unpaired or ambiguous"):
        manifest_module.validate_classified_flow_coverage(
            ledger, [], _flow_classification_rules()
        )
