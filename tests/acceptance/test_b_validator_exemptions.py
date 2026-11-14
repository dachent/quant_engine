"""Release B validator exemptions (#895), each with a kill test per loosened condition.

1. C5: a finite-boundary provider absent from the crosswalk is not a source-clock row only
   when the provider ledger's receipt dispositions for it are all
   EXCLUDE / NO_CURRENT_PRODUCT_CONSUMER / NO_CURRENT_PRODUCT_EFFECT.
2. A retired history-only account's capital and last-observation rows are exempt only after
   the oracle and tracker values agree; a mismatch or a non-retired account still refuses.
"""

from __future__ import annotations

import copy
import json

import pytest
from pathlib import Path
from typing import Any

from test_validate_generation import (
    CUTOFF,
    REPORT_GEN,
    Spec,
    _append,
    _crosswalk,
    _policy_fixture,
    build,
)

from acceptance.validate import checks
from acceptance.validate.generation import load_generation

# ------------------------------------------------------------------ 1. receipt exemption

FIDELITY_ROW = {
    "source_id": "fidelity",
    "provider_data_date": "2024-10-03",
    "applies_to_latest_common_eod": False,
    "finality": {"basis": "FINITE_SELECTED_MANIFEST_BOUNDARY"},
}


def _disposition(provider: str = "fidelity", **over: Any) -> dict[str, Any]:
    return {
        "provider_id": provider,
        "action": "EXCLUDE",
        "reason": "NO_CURRENT_PRODUCT_CONSUMER",
        "readiness_impact": "NO_CURRENT_PRODUCT_EFFECT",
        **over,
    }


def _receipt(*dispositions: dict[str, Any], gate: str = "PASS") -> dict[str, Any]:
    return {"admission_gate_status": gate, "dispositions": list(dispositions)}


def _ibkr_only_crosswalk(gen_dir: Path) -> None:
    path = (
        gen_dir.parents[2]
        / "reporting"
        / "generations"
        / REPORT_GEN
        / "input"
        / "account-strategy-current-epoch-crosswalk.json"
    )
    document = _crosswalk()
    for item in document["rows"]:
        item["provider"] = "IBKR"
    path.write_bytes(json.dumps(document, sort_keys=True).encode())


def _source_clock(
    tmp_path: Path,
    receipt: dict[str, Any] | None,
    row: dict[str, Any] = FIDELITY_ROW,
) -> list[checks.Finding]:
    spec = Spec(
        report=[_append(("source_clock", "source_data_as_of"), row)],
        post_write=[_ibkr_only_crosswalk],
    )
    gen_dir = build(tmp_path, spec)
    context = checks.Context(
        gen=load_generation(gen_dir),
        cutoff=checks.to_date(CUTOFF) or checks.date.today(),
        cutoff_source="test",
        repo_root=_policy_fixture(gen_dir.parent / "test-authority"),
        provider_receipt=receipt,
    )
    return [
        f
        for f in checks.run_checks(context)
        if f.check_id == "dates.source_clock" and f.entity == row["source_id"]
    ]


def test_receipt_that_excludes_every_item_exempts_an_absent_finite_provider(tmp_path):
    receipt = _receipt(_disposition(), _disposition())
    assert _source_clock(tmp_path, receipt) == []


def test_no_receipt_leaves_the_absent_provider_not_ready(tmp_path):
    assert [f.klass for f in _source_clock(tmp_path, None)] == ["NOT_READY"]


def test_one_admitted_item_leaves_the_provider_not_ready(tmp_path):
    receipt = _receipt(_disposition(), _disposition(action="ADMIT"))
    assert [f.klass for f in _source_clock(tmp_path, receipt)] == ["NOT_READY"]


def test_a_different_reason_leaves_the_provider_not_ready(tmp_path):
    receipt = _receipt(_disposition(), _disposition(reason="POLICY_DECLARED"))
    assert [f.klass for f in _source_clock(tmp_path, receipt)] == ["NOT_READY"]


def test_a_different_readiness_impact_leaves_the_provider_not_ready(tmp_path):
    receipt = _receipt(_disposition(readiness_impact="BLOCKS_READINESS"))
    assert [f.klass for f in _source_clock(tmp_path, receipt)] == ["NOT_READY"]


def test_a_missing_field_leaves_the_provider_not_ready(tmp_path):
    bare = _disposition()
    del bare["readiness_impact"]
    assert [f.klass for f in _source_clock(tmp_path, _receipt(bare))] == ["NOT_READY"]


def test_a_failed_gate_leaves_the_provider_not_ready(tmp_path):
    receipt = _receipt(_disposition(), gate="FAIL")
    assert [f.klass for f in _source_clock(tmp_path, receipt)] == ["NOT_READY"]


def test_a_receipt_for_another_provider_exempts_nothing(tmp_path):
    receipt = _receipt(_disposition("fidelty"), _disposition("alpaca"))
    assert [f.klass for f in _source_clock(tmp_path, receipt)] == ["NOT_READY"]


def test_an_empty_dispositions_list_exempts_nothing(tmp_path):
    assert [f.klass for f in _source_clock(tmp_path, _receipt())] == ["NOT_READY"]


def test_the_receipt_does_not_replace_the_finite_label(tmp_path):
    row = {**FIDELITY_ROW, "finality": {"basis": "PROVIDER_FINAL"}}
    found = _source_clock(tmp_path, _receipt(_disposition()), row)
    assert [f.klass for f in found] == ["NOT_READY"]


def test_the_receipt_does_not_override_an_active_crosswalk_account(tmp_path):
    # The provider IS named by the crosswalk with an active account: the receipt is not
    # consulted, the crosswalk decides.
    row = {**FIDELITY_ROW, "source_id": "ibkr"}
    found = _source_clock(tmp_path, _receipt(_disposition("ibkr")), row)
    assert [f.klass for f in found] == ["NOT_READY"]


# ------------------------------------------------------------------ 2. retired accounts

RETIRED = "X1"
TRACKER_VALUE = "0.19"
ORACLE_AS_OF = "2026-07-17"
TRACKER_AS_OF = "2025-11-24"


def _tracker_row(
    account: str = RETIRED, value: str = TRACKER_VALUE, as_of: str = TRACKER_AS_OF
) -> dict[str, Any]:
    return {
        "account_id": account,
        "as_of_date": as_of,
        "latest_value_usd": value,
        "status": "AVAILABLE_LATEST_OBSERVATION",
    }


def _crosswalk_edit(**changes: Any):
    def hook(gen_dir: Path) -> None:
        path = (
            gen_dir.parents[2]
            / "reporting"
            / "generations"
            / REPORT_GEN
            / "input"
            / "account-strategy-current-epoch-crosswalk.json"
        )
        document = json.loads(path.read_bytes())
        for item in document["rows"]:
            if item["account_id"] == RETIRED:
                item.update(changes)
        path.write_bytes(json.dumps(document, sort_keys=True).encode())

    return hook


def _expected_capital(account: str, value: str = TRACKER_VALUE) -> dict[str, Any]:
    return {
        "metric_id": "capital.account.cutoff",
        "entity": account,
        "expected": value,
        "as_of": ORACLE_AS_OF,
    }


def _last_observation(account: str = RETIRED) -> dict[str, Any]:
    return {
        "metric_id": "date.source_last_observation",
        "entity": account,
        "reason": "no ledger NAV carries a MAPPED mapping",
    }


def _findings(
    tmp_path: Path,
    expected: list[dict[str, Any]],
    not_derivable: list[dict[str, Any]],
    *,
    hooks: tuple[Any, ...] = (),
    tracker: dict[str, Any] | None = None,
) -> list[checks.Finding]:
    tracker = tracker if tracker is not None else _tracker_row()
    spec = Spec(
        report=[_append(("account_summary",), tracker)],
        post_write=list(hooks),
    )
    gen_dir = build(tmp_path, spec)
    context = checks.Context(
        gen=load_generation(gen_dir),
        cutoff=checks.to_date(CUTOFF) or checks.date.today(),
        cutoff_source="test",
        expected=copy.deepcopy(expected),
        expected_not_derivable=copy.deepcopy(not_derivable),
        repo_root=_policy_fixture(gen_dir.parent / "test-authority"),
    )
    return [f for f in checks.run_checks(context) if f.check_id.startswith("expected.")]


def _classes(found: list[checks.Finding], check_id: str) -> list[str]:
    return sorted(f.klass for f in found if f.check_id == check_id)


CAPITAL = "expected.capital.account.cutoff"
LAST_OBS = "expected.not_derivable"


def test_retired_account_with_equal_value_has_no_capital_date_finding(tmp_path):
    found = _findings(tmp_path, [_expected_capital(RETIRED)], [])
    assert _classes(found, CAPITAL) == []


def test_retired_account_with_a_different_value_still_refuses(tmp_path):
    found = _findings(tmp_path, [_expected_capital(RETIRED, "0.25")], [])
    assert set(_classes(found, CAPITAL)) == {checks.BLOCKER_REGRESSION}


def test_active_account_with_equal_value_and_a_different_date_still_refuses(tmp_path):
    # A1 is active: 1100 on the cutoff against an oracle dated earlier.
    row = {**_expected_capital("A1", "1100"), "as_of": "2026-07-17"}
    found = _findings(tmp_path, [row], [])
    assert set(_classes(found, CAPITAL)) == {checks.BLOCKER_REGRESSION}


def test_a_retired_account_whose_epoch_has_not_ended_still_refuses(tmp_path):
    found = _findings(
        tmp_path,
        [_expected_capital(RETIRED)],
        [],
        hooks=(_crosswalk_edit(effective_end=None),),
    )
    assert set(_classes(found, CAPITAL)) == {checks.BLOCKER_REGRESSION}


def test_an_immaterial_exclusion_is_not_a_retired_history_only_account(tmp_path):
    found = _findings(
        tmp_path,
        [_expected_capital(RETIRED)],
        [],
        hooks=(_crosswalk_edit(product_epoch_disposition="EXCLUDED_IMMATERIAL"),),
    )
    assert set(_classes(found, CAPITAL)) == {checks.BLOCKER_REGRESSION}


def test_last_observation_row_clears_after_the_value_check(tmp_path):
    found = _findings(tmp_path, [_expected_capital(RETIRED)], [_last_observation()])
    assert _classes(found, LAST_OBS) == []


def test_last_observation_row_stays_when_the_value_differs(tmp_path):
    found = _findings(
        tmp_path, [_expected_capital(RETIRED, "0.25")], [_last_observation()]
    )
    assert set(_classes(found, LAST_OBS)) == {checks.NOT_READY}


def test_last_observation_row_stays_without_an_expected_capital_row(tmp_path):
    found = _findings(tmp_path, [], [_last_observation()])
    assert set(_classes(found, LAST_OBS)) == {checks.NOT_READY}


def test_last_observation_row_stays_without_a_tracker_view(tmp_path):
    # The report carries no capital line for the retired account: nothing to compare.
    found = _findings(
        tmp_path,
        [_expected_capital(RETIRED)],
        [_last_observation()],
        tracker=_tracker_row("OTHER"),
    )
    assert set(_classes(found, LAST_OBS)) == {checks.NOT_READY}


def test_last_observation_row_of_an_active_account_stays(tmp_path):
    found = _findings(
        tmp_path, [_expected_capital("A1", "1100")], [_last_observation("A1")]
    )
    assert set(_classes(found, LAST_OBS)) == {checks.NOT_READY}


def test_last_observation_row_stays_when_the_epoch_has_not_ended(tmp_path):
    found = _findings(
        tmp_path,
        [_expected_capital(RETIRED)],
        [_last_observation()],
        hooks=(_crosswalk_edit(effective_end=None),),
    )
    assert set(_classes(found, LAST_OBS)) == {checks.NOT_READY}


def test_the_exemption_is_limited_to_the_last_observation_metric(tmp_path):
    other = {**_last_observation(), "metric_id": "date.source_first_observation"}
    found = _findings(tmp_path, [_expected_capital(RETIRED)], [other])
    assert set(_classes(found, LAST_OBS)) == {checks.NOT_READY}


def test_a_tracker_label_newer_than_the_oracles_still_refuses(tmp_path):
    found = _findings(
        tmp_path,
        [_expected_capital(RETIRED)],
        [],
        tracker=_tracker_row(as_of="2026-08-01"),
    )
    assert set(_classes(found, CAPITAL)) == {checks.BLOCKER_REGRESSION}


def test_a_tracker_label_after_the_epoch_end_still_refuses(tmp_path):
    # older than the oracle's 2026-07-17, but past the account's epoch end 2025-11-24
    found = _findings(
        tmp_path,
        [_expected_capital(RETIRED)],
        [],
        tracker=_tracker_row(as_of="2026-01-05"),
    )
    assert set(_classes(found, CAPITAL)) == {checks.BLOCKER_REGRESSION}


def test_a_tracker_label_on_the_epoch_end_is_exempt(tmp_path):
    found = _findings(
        tmp_path,
        [_expected_capital(RETIRED)],
        [],
        tracker=_tracker_row(as_of="2025-11-24"),
    )
    assert _classes(found, CAPITAL) == []


def test_a_tracker_label_newer_than_an_oracle_inside_the_epoch_still_refuses(tmp_path):
    # label 2025-11-24 is on the epoch end but newer than the oracle's 2025-10-01
    row = {**_expected_capital(RETIRED), "as_of": "2025-10-01"}
    found = _findings(tmp_path, [row], [], tracker=_tracker_row(as_of="2025-11-24"))
    assert set(_classes(found, CAPITAL)) == {checks.BLOCKER_REGRESSION}


def _malformed_receipt_rows() -> list[Any]:
    return [
        "fidelity ADMIT",
        {"action": "ADMIT", "provider_id": ["fidelity"]},
        {"action": "ADMIT"},
        {**_disposition(), "provider_id": "  "},
    ]


@pytest.mark.parametrize("bad", _malformed_receipt_rows())
def test_a_row_without_a_provider_voids_the_whole_receipt(tmp_path, bad):
    receipt = _receipt(_disposition(), bad)
    assert [f.klass for f in _source_clock(tmp_path, receipt)] == ["NOT_READY"]


def test_a_composite_entity_is_not_the_retired_account(tmp_path):
    other = {**_last_observation(), "entity": f"{RETIRED}|A1"}
    found = _findings(tmp_path, [_expected_capital(RETIRED)], [other])
    assert _classes(found, LAST_OBS) == [checks.NOT_READY]


def test_the_value_check_ignores_the_rows_own_tolerance(tmp_path):
    # 0.25 expected against the tracker's 0.19: a loose row tolerance passes the capital
    # comparison, but the exemption needs agreement to the cent.
    row = {**_expected_capital(RETIRED, "0.25"), "tolerance": "1000"}
    found = _findings(tmp_path, [row], [_last_observation()])
    assert _classes(found, LAST_OBS) == [checks.NOT_READY]


def test_agreement_within_a_cent_verifies(tmp_path):
    found = _findings(
        tmp_path, [_expected_capital(RETIRED, "0.195")], [_last_observation()]
    )
    assert _classes(found, LAST_OBS) == []
