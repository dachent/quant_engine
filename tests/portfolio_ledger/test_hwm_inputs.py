"""Consumption-boundary tests for the shared hwm_inputs validator."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from quant_engine.portfolio_ledger import (
    HwmInputsContractError,
    hwm_policy_exclusions,
    validate_hwm_inputs_envelope,
)
from quant_engine.portfolio_ledger.hwm_inputs import _SHA256


def _valid_envelope() -> dict[str, Any]:
    return {
        "contract_version": 1,
        "anchor": {
            "anchor_date": "2026-01-03",
            "index_base_value": "1.0",
            "hwm_policy_id": "PORT-HWM-3",
            "acceptance_provenance": {
                "accepted_at": "2026-01-03T00:00:00Z",
                "accepted_by": "synthetic",
                "source_artifact_id": "anchor-1",
                "source_artifact_sha256": "a" * 64,
            },
        },
        "calendar_authorities": [
            {
                "lane": "ACCOUNT",
                "authority_id": "synthetic",
                "authority_version": "1",
                "calendar_kind": "EXCHANGE",
                "admitted_source_artifact_id": "calendar-1",
            }
        ],
        "periods": [
            {
                "strategy_id": "strategy-a",
                "date": "2026-01-03",
                "lane": "ACCOUNT",
                "disposition": "UNAVAILABLE",
                "beginning_capital_usd": None,
                "return_source_record_id": None,
                "flow_status": "MISSING",
                "fx_status": "MISSING",
            }
        ],
        "reconciliation": {
            "period_capital_method": "NAV_ROLLFORWARD",
            "allocation_endpoint_check": {"matched": True, "delta_usd": "0"},
        },
    }


_ARTIFACT_ROLES: dict[str, tuple[str, str]] = {
    "anchor-1": ("hwm-anchor-acceptance", "a" * 64),
    "calendar-1": ("hwm-calendar-authority-account", "b" * 64),
}


def _validate(
    envelope: dict[str, Any], exclusions: dict[str, date] | None = None
) -> None:
    validate_hwm_inputs_envelope(
        envelope,
        as_of_date=date(2026, 1, 3),
        perimeter=["strategy-a"],
        crosswalk=[],
        artifact_roles=_ARTIFACT_ROLES,
        policy_exclusions=exclusions or {},
    )


def test_valid_envelope_passes() -> None:
    _validate(_valid_envelope())


def test_policy_exclusion_fraud_rejected() -> None:
    envelope = _valid_envelope()
    envelope["periods"][0]["disposition"] = "EXCLUDED_BY_POLICY"
    with pytest.raises(HwmInputsContractError) as excinfo:
        _validate(envelope)
    assert excinfo.value.code == "POLICY_EXCLUSION_INVALID"


def test_policy_exclusion_before_effective_date_rejected() -> None:
    envelope = _valid_envelope()
    envelope["periods"][0]["disposition"] = "EXCLUDED_BY_POLICY"
    with pytest.raises(HwmInputsContractError) as excinfo:
        _validate(envelope, {"strategy-a": date(2026, 2, 1)})
    assert excinfo.value.code == "POLICY_EXCLUSION_INVALID"


def test_policy_exclusion_named_and_effective_passes() -> None:
    envelope = _valid_envelope()
    envelope["periods"][0]["disposition"] = "EXCLUDED_BY_POLICY"
    _validate(envelope, {"strategy-a": date(2026, 1, 1)})


def test_failed_endpoint_check_rejected() -> None:
    for endpoint in (
        {"matched": False, "delta_usd": "999"},
        {"matched": True, "delta_usd": "999"},
        {"matched": False, "delta_usd": "0"},
    ):
        envelope = _valid_envelope()
        envelope["reconciliation"]["allocation_endpoint_check"] = endpoint
        with pytest.raises(HwmInputsContractError) as excinfo:
            _validate(envelope)
        assert excinfo.value.code == "ENDPOINT_NOT_RECONCILED"


def test_incomplete_envelope_shape_rejected() -> None:
    with pytest.raises(HwmInputsContractError) as excinfo:
        validate_hwm_inputs_envelope(
            {"periods": []},
            as_of_date=date(2026, 1, 3),
            perimeter=["strategy-a"],
            crosswalk=[],
            artifact_roles=_ARTIFACT_ROLES,
            policy_exclusions={},
        )
    assert excinfo.value.code == "HWM_INPUTS_INVALID"


def test_policy_extractor_requires_port_hwm_3() -> None:
    with pytest.raises(HwmInputsContractError, match="must be PORT-HWM-3"):
        hwm_policy_exclusions({"policy_id": "PORT-HWM-2", "material_perimeter": {}})


def test_policy_extractor_reads_the_admitted_contract() -> None:
    import json
    from pathlib import Path

    contract = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "portfolio-hwm-policy-v3.json"
    )
    exclusions = hwm_policy_exclusions(json.loads(contract.read_text(encoding="utf-8")))
    assert exclusions == {
        "composer-holy-grail": date(2026, 8, 1),
        "echotrade-growth-pivots": date(2026, 8, 6),
    }
    assert _SHA256.fullmatch("a" * 64) is not None


# ---------------------------------------------------------------------------
# QC7 regression probes: every probe must be rejected by the shared validator
# (and therefore by the report consumer, panel consumer, and builder admission
# which all call this same validator).
# ---------------------------------------------------------------------------


def _available_period_fixture() -> dict[str, Any]:
    """A valid envelope with a single AVAILABLE ACCOUNT row on 2026-01-03."""
    return {
        "contract_version": 1,
        "anchor": {
            "anchor_date": "2026-01-03",
            "index_base_value": "1.0",
            "hwm_policy_id": "PORT-HWM-3",
            "acceptance_provenance": {
                "accepted_at": "2026-01-03T00:00:00Z",
                "accepted_by": "synthetic",
                "source_artifact_id": "anchor-1",
                "source_artifact_sha256": "a" * 64,
            },
        },
        "calendar_authorities": [
            {
                "lane": "ACCOUNT",
                "authority_id": "synthetic",
                "authority_version": "1",
                "calendar_kind": "EXCHANGE",
                "admitted_source_artifact_id": "calendar-1",
            },
            {
                "lane": "PROVIDER",
                "authority_id": "synthetic",
                "authority_version": "1",
                "calendar_kind": "PROVIDER_PUBLICATION",
                "admitted_source_artifact_id": "calendar-provider-1",
            },
            {
                "lane": "LEGACY",
                "authority_id": "synthetic",
                "authority_version": "1",
                "calendar_kind": "ALWAYS_ON",
                "admitted_source_artifact_id": "calendar-legacy-1",
            },
        ],
        "periods": [
            {
                "strategy_id": "strategy-a",
                "date": "2026-01-03",
                "lane": "ACCOUNT",
                "disposition": "AVAILABLE",
                "beginning_capital_usd": "100.00",
                "return_source_record_id": "rec-1",
                "component_account_ids": ["acct-1"],
                "flow_status": "OBSERVED",
                "fx_status": "NOT_REQUIRED",
            }
        ],
        "reconciliation": {
            "period_capital_method": "NAV_ROLLFORWARD",
            "allocation_endpoint_check": {"matched": True, "delta_usd": "0"},
        },
    }


# Extend artifact roles for AVAILABLE-period fixtures (need the return ref
# to bind to an ACCOUNT_LIVE rollforward row).
_AVAILABLE_ARTIFACT_ROLES: dict[str, tuple[str, str]] = {
    **_ARTIFACT_ROLES,
    "calendar-provider-1": ("hwm-calendar-authority-provider", "p" * 64),
    "calendar-legacy-1": ("hwm-calendar-authority-legacy", "l" * 64),
}
_AVAILABLE_CROSSWALK: list[dict[str, Any]] = [
    {
        "strategy_id": "strategy-a",
        "date": "2026-01-03",
        "evidence_layer": "ACCOUNT_LIVE",
        "account_reconciliation_sha256": "rec-1",
        "beginning_capital": "100.00",
        "return": "0.02",
        "component_account_ids": ["acct-1"],
    }
]


def _validate_available(envelope: dict[str, Any]) -> None:
    validate_hwm_inputs_envelope(
        envelope,
        as_of_date=date(2026, 1, 3),
        perimeter=["strategy-a"],
        crosswalk=_AVAILABLE_CROSSWALK,
        artifact_roles=_AVAILABLE_ARTIFACT_ROLES,
        policy_exclusions={},
    )


# --- QC7 probe: AVAILABLE row relabeled to PROVIDER lane ---


def test_available_row_provider_lane_rejected() -> None:
    """QC7: an AVAILABLE row relabeled to PROVIDER must be rejected."""
    envelope = _available_period_fixture()
    envelope["periods"][0]["lane"] = "PROVIDER"
    with pytest.raises(HwmInputsContractError) as excinfo:
        _validate_available(envelope)
    assert excinfo.value.code == "LANE_CAPITAL_UNAVAILABLE"


# --- QC7 probe: AVAILABLE row relabeled to LEGACY lane ---


def test_available_row_legacy_lane_rejected() -> None:
    """QC7: an AVAILABLE row relabeled to LEGACY must be rejected."""
    envelope = _available_period_fixture()
    envelope["periods"][0]["lane"] = "LEGACY"
    with pytest.raises(HwmInputsContractError) as excinfo:
        _validate_available(envelope)
    assert excinfo.value.code == "LANE_CAPITAL_UNAVAILABLE"


# --- QC7 probe: anchor moved later + earlier periods deleted ---


def test_anchor_moved_later_keyspace_mismatch_rejected() -> None:
    """QC7: moving the anchor forward while retaining old periods causes a
    keyspace mismatch (the expected keyspace is shorter than the actual)."""
    envelope = _valid_envelope()
    # Move anchor to 2026-01-04 and as_of_date to 2026-01-04 so the
    # anchor_date check passes.  The 2026-01-03 period is now outside the
    # expected keyspace.  Also update accepted_at so it's ≥ new anchor.
    envelope["anchor"]["anchor_date"] = "2026-01-04"
    envelope["anchor"]["acceptance_provenance"]["accepted_at"] = "2026-01-04T00:00:00Z"
    with pytest.raises(HwmInputsContractError) as excinfo:
        validate_hwm_inputs_envelope(
            envelope,
            as_of_date=date(2026, 1, 4),
            perimeter=["strategy-a"],
            crosswalk=[],
            artifact_roles=_ARTIFACT_ROLES,
            policy_exclusions={},
        )
    assert excinfo.value.code == "KEYSPACE_DIFFERS"


# --- QC7 probe: allocation inflated with declaration kept at zero ---


def test_allocation_inflated_endpoint_delta_mismatch_rejected() -> None:
    """QC7: an inflated allocation with the endpoint declared as
    {matched: true, delta_usd: "0"} must be rejected when allocations are
    supplied for independent recomputation."""
    envelope = _available_period_fixture()
    # The envelope has one AVAILABLE ACCOUNT period at 2026-01-03 with
    # capital=100.00 and return=0.02 → endpoint = 102.00.
    # An allocation claiming value=999 at that date must mismatch.
    allocations: list[dict[str, Any]] = [
        {
            "scope": "SYSTEMATIC",
            "strategy_id": "strategy-a",
            "as_of_date": "2026-01-03",
            "current_value": {"value": "999"},
        }
    ]
    with pytest.raises(HwmInputsContractError) as excinfo:
        _validate_available_with_alloc(envelope, allocations)
    assert excinfo.value.code == "ENDPOINT_NOT_RECONCILED"


def _validate_available_with_alloc(
    envelope: dict[str, Any], allocations: list[dict[str, Any]]
) -> None:
    validate_hwm_inputs_envelope(
        envelope,
        as_of_date=date(2026, 1, 3),
        perimeter=["strategy-a"],
        crosswalk=_AVAILABLE_CROSSWALK,
        artifact_roles=_AVAILABLE_ARTIFACT_ROLES,
        policy_exclusions={},
        allocations=allocations,
    )


# --- QC7 probe: anchor-content mismatch (fabricated anchor date) ---


def test_anchor_content_date_mismatch_rejected() -> None:
    """QC7: the envelope anchor must match the acceptance record content
    when the record is supplied."""
    envelope = _valid_envelope()
    acceptance_record = {
        "anchor_date": "2025-12-31",  # different from envelope's 2026-01-03
        "index_base_value": "1.0",
        "hwm_policy_id": "PORT-HWM-3",
    }
    with pytest.raises(HwmInputsContractError) as excinfo:
        validate_hwm_inputs_envelope(
            envelope,
            as_of_date=date(2026, 1, 3),
            perimeter=["strategy-a"],
            crosswalk=[],
            artifact_roles=_ARTIFACT_ROLES,
            policy_exclusions={},
            acceptance_record=acceptance_record,
        )
    assert excinfo.value.code == "ANCHOR_CONTENT_MISMATCH"


# --- QC7 probe: anchor-content mismatch (fabricated policy id) ---


def test_anchor_content_policy_id_mismatch_rejected() -> None:
    """QC7: the envelope anchor hwm_policy_id must match the acceptance
    record content."""
    envelope = _valid_envelope()
    acceptance_record = {
        "anchor_date": "2026-01-03",
        "index_base_value": "1.0",
        "hwm_policy_id": "PORT-HWM-2",  # wrong policy
    }
    with pytest.raises(HwmInputsContractError) as excinfo:
        validate_hwm_inputs_envelope(
            envelope,
            as_of_date=date(2026, 1, 3),
            perimeter=["strategy-a"],
            crosswalk=[],
            artifact_roles=_ARTIFACT_ROLES,
            policy_exclusions={},
            acceptance_record=acceptance_record,
        )
    assert excinfo.value.code == "ANCHOR_CONTENT_MISMATCH"


# --- Parity: valid envelope passes with all optional admission params ---


def test_valid_envelope_passes_with_all_admission_params() -> None:
    """A valid envelope passes when both allocations and acceptance_record
    are supplied (the full admission-builder context)."""
    envelope = _valid_envelope()
    acceptance_record = {
        "anchor_date": "2026-01-03",
        "index_base_value": "1.0",
        "hwm_policy_id": "PORT-HWM-3",
    }
    allocations: list[dict[str, Any]] = [
        {
            "scope": "SYSTEMATIC",
            "strategy_id": "strategy-a",
            "as_of_date": "2026-01-03",
            "current_value": {"value": "0"},
        }
    ]
    # Should not raise.
    validate_hwm_inputs_envelope(
        envelope,
        as_of_date=date(2026, 1, 3),
        perimeter=["strategy-a"],
        crosswalk=[],
        artifact_roles=_ARTIFACT_ROLES,
        policy_exclusions={},
        allocations=allocations,
        acceptance_record=acceptance_record,
    )
