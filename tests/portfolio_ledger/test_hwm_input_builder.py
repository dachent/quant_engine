"""Governed HWM input-generation builder tests.

The builder derives kernel-consumable component periods from observed
evidence only: owner-validated funded-membership dates, capital observations
between independently observed brackets, and account-live returns. It fails
closed on terminal-only backcast, pre-evidence membership, unreconciled
brackets, and unknown strategies.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from quant_engine.portfolio_ledger.hwm import AS_HELD_HWM_RETURN_BASIS
from quant_engine.portfolio_ledger.hwm_input_builder import (
    HwmInputBuilderError,
    build_governed_hwm_inputs,
)


def _canon(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


POLICY_SHA = "a" * 64
CALENDAR_SHA = "b" * 64
GENERATION_SHA = "c" * 64


def _member(
    strategy_id: str,
    effective_start: str,
    *,
    evidence_seed: str = "packet",
) -> dict[str, object]:
    return {
        "strategy_id": strategy_id,
        "effective_start": effective_start,
        "evidence_kind": "OWNER_VALIDATED_PACKET",
        "evidence_ref": evidence_seed,
    }


def _capital_obs(strategy_id: str, date: str, capital_usd: str) -> dict[str, object]:
    return {
        "strategy_id": strategy_id,
        "date": date,
        "closing_capital_usd": capital_usd,
        "observation_kind": "ACCOUNT_NAV",
        "source_artifact_sha256": CALENDAR_SHA,
    }


def _return_obs(
    strategy_id: str,
    interval_start: str,
    interval_end: str,
    net_return: str,
) -> dict[str, object]:
    return {
        "strategy_id": strategy_id,
        "interval_start": interval_start,
        "interval_end": interval_end,
        "net_return": net_return,
        "return_basis": AS_HELD_HWM_RETURN_BASIS,
        "source_artifact_sha256": GENERATION_SHA,
    }


def test_membership_and_bracket_capital_derive_component_periods() -> None:
    result = build_governed_hwm_inputs(
        members=[
            _member("grizzlybears-vtma", "2026-01-02"),
            _member("robuxio-crypto", "2026-01-02"),
        ],
        capital_observations=[
            _capital_obs("grizzlybears-vtma", "2026-01-02", "1000"),
            _capital_obs("grizzlybears-vtma", "2026-01-05", "1100"),
            _capital_obs("grizzlybears-vtma", "2026-01-06", "990"),
            _capital_obs("robuxio-crypto", "2026-01-02", "500"),
            _capital_obs("robuxio-crypto", "2026-01-05", "450"),
            _capital_obs("robuxio-crypto", "2026-01-06", "455"),
        ],
        return_observations=[
            _return_obs("grizzlybears-vtma", "2026-01-03", "2026-01-04", "0.04"),
            _return_obs("grizzlybears-vtma", "2026-01-05", "2026-01-06", "-0.10"),
            _return_obs("robuxio-crypto", "2026-01-03", "2026-01-04", "0.02"),
            _return_obs("robuxio-crypto", "2026-01-05", "2026-01-06", "0.00"),
        ],
        calendar_authority_sha256=CALENDAR_SHA,
        hwm_policy_sha256=POLICY_SHA,
    )

    assert result["builder"] == "portfolio-hwm-inputs-governed"
    assert result["authority_profile"] == "ACCEPTED_GOVERNED"
    assert len(result["memberships"]) == 2
    grizzly_periods = [
        row
        for row in result["component_periods"]
        if row["strategy_id"] == "grizzlybears-vtma"
    ]
    # weights come from bracket beginning capital
    assert [row["beginning_capital_usd"] for row in grizzly_periods] == [
        "1000",
        "1100",
    ]
    # every period carries the governing identity bindings
    for row in result["component_periods"]:
        assert row["calendar_sha256"] == CALENDAR_SHA
        assert row["policy_binding_sha256"] == POLICY_SHA


def test_terminal_only_backcast_rejected() -> None:
    with pytest.raises(HwmInputBuilderError, match="bracket"):
        build_governed_hwm_inputs(
            members=[_member("grizzlybears-vtma", "2026-01-02")],
            capital_observations=[
                _capital_obs("grizzlybears-vtma", "2026-08-13", "2000"),
            ],
            return_observations=[
                _return_obs("grizzlybears-vtma", "2026-01-03", "2026-01-04", "0.01")
            ],
            calendar_authority_sha256=CALENDAR_SHA,
            hwm_policy_sha256=POLICY_SHA,
        )


def test_return_before_first_capital_anchor_rejected() -> None:
    with pytest.raises(HwmInputBuilderError, match="anchor"):
        build_governed_hwm_inputs(
            members=[_member("grizzlybears-vtma", "2026-01-02")],
            capital_observations=[
                _capital_obs("grizzlybears-vtma", "2026-01-05", "1000"),
            ],
            return_observations=[
                _return_obs("grizzlybears-vtma", "2026-01-03", "2026-01-04", "0.01")
            ],
            calendar_authority_sha256=CALENDAR_SHA,
            hwm_policy_sha256=POLICY_SHA,
        )


def test_return_for_non_member_rejected() -> None:
    with pytest.raises(HwmInputBuilderError, match="perimeter"):
        build_governed_hwm_inputs(
            members=[_member("grizzlybears-vtma", "2026-01-02")],
            capital_observations=[
                _capital_obs("composer-holy-grail", "2026-01-02", "700")
            ],
            return_observations=[
                _return_obs("composer-holy-grail", "2026-01-03", "2026-01-04", "0.01")
            ],
            calendar_authority_sha256=CALENDAR_SHA,
            hwm_policy_sha256=POLICY_SHA,
        )


def test_duplicate_strategy_member_rejected() -> None:
    with pytest.raises(HwmInputBuilderError, match="duplicated"):
        build_governed_hwm_inputs(
            members=[
                _member("grizzlybears-vtma", "2026-01-02"),
                _member("grizzlybears-vtma", "2026-01-03"),
            ],
            capital_observations=[],
            return_observations=[],
            calendar_authority_sha256=CALENDAR_SHA,
            hwm_policy_sha256=POLICY_SHA,
        )


def test_generation_identity_is_canonical_self_hash() -> None:
    result = build_governed_hwm_inputs(
        members=[_member("robuxio-crypto", "2026-01-02")],
        capital_observations=[
            _capital_obs("robuxio-crypto", "2026-01-02", "500"),
        ],
        return_observations=[],
        calendar_authority_sha256=CALENDAR_SHA,
        hwm_policy_sha256=POLICY_SHA,
    )
    declared = result["generation_evidence_sha256"]
    unhashed = dict(result)
    unhashed.pop("generation_evidence_sha256")
    assert _sha(unhashed) == declared


def test_trailing_capital_anchor_required_for_every_period() -> None:
    with pytest.raises(HwmInputBuilderError, match="closing"):
        build_governed_hwm_inputs(
            members=[_member("grizzlybears-vtma", "2026-01-02")],
            capital_observations=[
                _capital_obs("grizzlybears-vtma", "2026-01-02", "1000"),
            ],
            return_observations=[
                _return_obs("grizzlybears-vtma", "2026-01-03", "2026-01-04", "0.01")
            ],
            calendar_authority_sha256=CALENDAR_SHA,
            hwm_policy_sha256=POLICY_SHA,
        )


def test_account_live_net_basis_rejected() -> None:
    members_rows = [_member("grizzlybears-vtma", "2026-01-02")]
    caps = [
        _capital_obs("grizzlybears-vtma", "2026-01-02", "1000"),
        _capital_obs("grizzlybears-vtma", "2026-01-07", "1100"),
    ]
    rets = [_return_obs("grizzlybears-vtma", "2026-01-03", "2026-01-04", "0.01")]
    rets[0]["return_basis"] = "ACCOUNT_LIVE_NET"
    with pytest.raises(
        HwmInputBuilderError, match="LEVERED_NET_NAV_AFTER_FINANCING_COST_FLOW_NEUTRAL"
    ):
        build_governed_hwm_inputs(
            members=members_rows,
            capital_observations=caps,
            return_observations=rets,
            calendar_authority_sha256=CALENDAR_SHA,
            hwm_policy_sha256=POLICY_SHA,
        )


def test_multi_day_interval_rejected() -> None:
    with pytest.raises(HwmInputBuilderError, match="one-day"):
        build_governed_hwm_inputs(
            members=[_member("grizzlybears-vtma", "2026-01-02")],
            capital_observations=[
                _capital_obs("grizzlybears-vtma", "2026-01-02", "1000"),
                _capital_obs("grizzlybears-vtma", "2026-01-08", "1100"),
            ],
            return_observations=[
                _return_obs("grizzlybears-vtma", "2026-01-03", "2026-01-06", "0.01")
            ],
            calendar_authority_sha256=CALENDAR_SHA,
            hwm_policy_sha256=POLICY_SHA,
        )


def test_return_row_outside_perimeter_rejected() -> None:
    with pytest.raises(HwmInputBuilderError, match="perimeter"):
        build_governed_hwm_inputs(
            members=[_member("grizzlybears-vtma", "2026-01-02")],
            capital_observations=[
                _capital_obs("grizzlybears-vtma", "2026-01-02", "1000"),
                _capital_obs("grizzlybears-vtma", "2026-01-07", "1100"),
            ],
            return_observations=[
                _return_obs("grizzlybear-vtma-typo", "2026-01-03", "2026-01-04", "0.01")
            ],
            calendar_authority_sha256=CALENDAR_SHA,
            hwm_policy_sha256=POLICY_SHA,
        )
