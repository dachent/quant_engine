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
