"""Factor lens Stage 1: panel staleness states, labels and run-failing assertions.

All fixtures are synthetic. Each test states the precondition it fails without.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import factor_lens_status as fls  # noqa: E402

EVAL = date(2026, 11, 19)


def _french(last: str, region: str = "US", evaluation: date = EVAL) -> dict:
    return fls.french_panel(
        panel_id=f"french-{region.lower()}",
        region=region,
        last_observation=last,
        evaluation_date=evaluation,
        factors=list(fls.FRENCH_FACTORS[region]),
    )


# --- French staleness: required-month rule -------------------------------------------


def test_required_month_is_latest_month_end_plus_45_on_or_before_evaluation() -> None:
    assert fls.required_french_month(date(2026, 11, 19)) == date(2026, 9, 30)
    assert fls.required_french_month(date(2026, 12, 14)) == date(2026, 9, 30)
    assert fls.required_french_month(date(2026, 12, 15)) == date(2026, 10, 31)
    assert fls.required_french_month(date(2027, 1, 13)) == date(2026, 10, 31)
    assert fls.required_french_month(date(2027, 1, 14)) == date(2026, 11, 30)


def test_french_compliant_when_last_observation_reaches_required_month() -> None:
    panel = _french("2026-09-30")
    assert panel["state"] == "COMPLIANT"
    assert panel["required_month_end"] == "2026-09-30"
    assert panel["last_observation"] == "2026-09-30"
    assert panel["age_days"] == 50
    # Oct 31 + 45 days is Dec 15: October data are not yet due on Dec 14.
    assert _french("2026-09-30", evaluation=date(2026, 12, 14))["state"] == "COMPLIANT"


def test_french_month_end_on_weekend_still_counts_for_that_month() -> None:
    # 2026-10-31 is a Saturday; the last daily row is Friday the 30th.
    panel = _french("2026-10-30", evaluation=date(2026, 12, 15))
    assert panel["state"] == "COMPLIANT"


def test_french_hard_stale_with_exit_when_required_month_missing() -> None:
    panel = _french("2026-09-30", evaluation=date(2026, 12, 16))
    assert panel["state"] == "HARD_STALE"
    assert panel["required_month_end"] == "2026-10-31"
    assert "2026-10" in panel["exit"]
    assert panel["contributes_to_headline"] is False
    # Exit: the required month arrives.
    assert _french("2026-10-30", evaluation=date(2026, 12, 16))["state"] == "COMPLIANT"


def test_french_panel_reports_next_due_date() -> None:
    panel = _french("2026-09-30")
    assert panel["next_required_month_end"] == "2026-10-31"
    assert panel["next_due_date"] == "2026-12-15"


# --- ETF bridge: 7 / 14 days ----------------------------------------------------------


@pytest.mark.parametrize(
    ("last", "state"),
    [
        ("2026-11-19", "FRESH"),
        ("2026-11-12", "FRESH"),  # 7 days
        ("2026-11-11", "STALE"),  # 8 days
        ("2026-11-05", "STALE"),  # 14 days
        ("2026-11-04", "HARD_STALE"),  # 15 days
    ],
)
def test_etf_bridge_states(last: str, state: str) -> None:
    panel = fls.etf_bridge_panel(
        panel_id="etf-us",
        region="US",
        last_observation=last,
        evaluation_date=EVAL,
        bridge_ids=["SPY-RF"],
        configured_ids=["SPY-RF"],
    )
    assert panel["state"] == state
    assert panel["exit"]
    assert (panel["contributes_to_headline"] is False) == (state == "HARD_STALE")


# --- P123 panel: 45 / 90 days, UNHASHED ----------------------------------------------


@pytest.mark.parametrize(
    ("capture", "state"),
    [("2026-10-05", "FRESH"), ("2026-10-04", "STALE"), ("2026-08-21", "STALE"), ("2026-08-20", "HARD_STALE")],
)
def test_p123_states_and_unhashed_label(capture: str, state: str) -> None:
    panel = fls.p123_panel(
        panel_id="p123-us", last_observation=capture, evaluation_date=EVAL, definition_hashes_present=False
    )
    assert panel["state"] == state
    assert panel["labels"] == ["UNHASHED"]
    hashed = fls.p123_panel(
        panel_id="p123-us", last_observation=capture, evaluation_date=EVAL, definition_hashes_present=True
    )
    assert "UNHASHED" not in hashed["labels"]


# --- last observation date and age are required --------------------------------------


@pytest.mark.parametrize("bad", [None, "", "not-a-date", "2026-11-20", "2026-11-19T00:00:00"])
def test_panel_without_valid_last_observation_fails(bad: object) -> None:
    with pytest.raises(fls.StatusError):
        fls.french_panel(
            panel_id="french-us",
            region="US",
            last_observation=bad,  # type: ignore[arg-type]
            evaluation_date=EVAL,
            factors=list(fls.FRENCH_FACTORS["US"]),
        )


def test_validate_rejects_panel_record_missing_age_or_date() -> None:
    good = _french("2026-09-30")
    for key in ("last_observation", "age_days"):
        broken = {k: v for k, v in good.items() if k != key}
        failures = fls.validate_panels([broken])
        assert any(key in f for f in failures), failures
    assert fls.validate_panels([good]) == []


# --- lens identity -------------------------------------------------------------------


def test_french_factor_sets_are_exactly_six_per_region() -> None:
    assert fls.FRENCH_FACTORS["US"] == ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom")
    assert fls.FRENCH_FACTORS["Europe"] == ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "WML")
    assert _french("2026-09-30", region="Europe")["factors"] == list(fls.FRENCH_FACTORS["Europe"])


def test_identity_fails_on_swapped_extra_or_missing_factor() -> None:
    with pytest.raises(fls.StatusError, match="lens identity"):
        fls.french_panel(
            panel_id="french-us", region="US", last_observation="2026-09-30", evaluation_date=EVAL,
            factors=["Mkt-RF", "SMB", "HML", "RMW", "CMA", "WML"],  # Europe's momentum name in US
        )
    with pytest.raises(fls.StatusError, match="lens identity"):
        fls.french_panel(
            panel_id="french-us", region="US", last_observation="2026-09-30", evaluation_date=EVAL,
            factors=[*fls.FRENCH_FACTORS["US"], "LowVol"],
        )
    with pytest.raises(fls.StatusError, match="lens identity"):
        fls.french_panel(
            panel_id="french-us", region="US", last_observation="2026-09-30", evaluation_date=EVAL,
            factors=list(fls.FRENCH_FACTORS["US"][:-1]),
        )


def test_etf_bridge_ids_must_equal_configured_list() -> None:
    with pytest.raises(fls.StatusError, match="lens identity"):
        fls.etf_bridge_panel(
            panel_id="etf-us", region="US", last_observation="2026-11-19", evaluation_date=EVAL,
            bridge_ids=["SPY-RF", "IWM-IWB", "XYZ"], configured_ids=["SPY-RF", "IWM-IWB"],
        )
    with pytest.raises(fls.StatusError, match="lens identity"):
        fls.etf_bridge_panel(
            panel_id="etf-us", region="US", last_observation="2026-11-19", evaluation_date=EVAL,
            bridge_ids=["IWM-IWB", "SPY-RF"], configured_ids=["SPY-RF", "IWM-IWB"],  # order is identity too
        )


# --- weekly windows ------------------------------------------------------------------

# Synthetic calendar: Monday 2026-01-05 is a market holiday (fixture), so that week starts Tuesday.
_TRADING = [date(2026, 1, d) for d in (6, 7, 8, 9, 12, 13, 14, 15, 16)]


def test_windows_equal_trading_days_of_the_iso_week_including_holiday_monday() -> None:
    windows = [
        {"week_start": "2026-01-05", "days": ["2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]},
        {"week_start": "2026-01-12", "days": ["2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15", "2026-01-16"]},
    ]
    assert fls.check_weekly_windows(windows, _TRADING) == []


def test_shifted_window_fails() -> None:
    shifted = [
        {"week_start": "2026-01-05", "days": ["2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12"]},
    ]
    failures = fls.check_weekly_windows(shifted, _TRADING)
    assert failures and "2026-01-05" in failures[0]
    # A window that pretends the holiday Monday traded also fails.
    assert fls.check_weekly_windows(
        [{"week_start": "2026-01-05", "days": ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]}],
        _TRADING,
    )


def test_window_week_start_must_be_a_monday() -> None:
    failures = fls.check_weekly_windows([{"week_start": "2026-01-06", "days": ["2026-01-06"]}], _TRADING)
    assert failures


# --- crypto NOT_APPLICABLE -----------------------------------------------------------


def test_strategy_with_no_explanatory_power_under_every_lens_is_not_applicable() -> None:
    crypto = fls.attribution_status({"french": -0.01, "etf": 0.00, "p123": 0.02})
    assert crypto["state"] == "NOT_APPLICABLE"
    assert crypto["reason"]
    live = fls.attribution_status({"french": -0.11, "etf": -0.03})
    assert live["state"] == "NOT_APPLICABLE"


def test_one_lens_above_threshold_keeps_attribution_applicable() -> None:
    assert fls.attribution_status({"french": 0.03, "etf": -0.03})["state"] == "APPLICABLE"
    assert fls.attribution_status({"french": 0.62})["state"] == "APPLICABLE"


def test_attribution_status_refuses_empty_input() -> None:
    with pytest.raises(fls.StatusError):
        fls.attribution_status({})


# --- noise rule and significance marks -----------------------------------------------


def test_status_block_carries_noise_rule() -> None:
    doc = fls.build_status(evaluation_date=EVAL, panels=[_french("2026-09-30")])
    rule = doc["noise_rule"]
    assert rule["adj_r2_threshold"] == 0.2
    assert "0.2" in rule["text"] and "noise" in rule["text"]


def test_live_comparison_needs_threshold_or_interval() -> None:
    bare = {"strategy": "S", "window": "live", "gap": 0.08}
    assert fls.validate_comparisons([bare])
    assert fls.validate_comparisons([{**bare, "interval": [-0.15, 0.17]}]) == []
    assert fls.validate_comparisons([{**bare, "noise_threshold": 0.2}]) == []
    # An old, too-small threshold does not satisfy the rule.
    assert fls.validate_comparisons([{**bare, "noise_threshold": 0.1}])
    # Long windows do not need it.
    assert fls.validate_comparisons([{"strategy": "S", "window": "long", "gap": 0.2}]) == []


def test_interval_must_bracket_two_ordered_numbers() -> None:
    bare = {"strategy": "S", "window": "live", "gap": 0.08}
    assert fls.validate_comparisons([{**bare, "interval": [0.17, -0.15]}])
    assert fls.validate_comparisons([{**bare, "interval": [0.1]}])


def test_no_significance_marks_on_live_increments() -> None:
    ok = {"strategy": "S", "window": "live", "gap": 0.08, "noise_threshold": 0.2}
    assert fls.validate_comparisons([ok]) == []
    for extra in ({"p_value": 0.01}, {"stars": "**"}, {"significant": True}, {"mark": "*"}, {"p_nw": 0.04}):
        failures = fls.validate_comparisons([{**ok, **extra}])
        assert any("significance" in f for f in failures), (extra, failures)
    # Long-window tests may carry p-values.
    assert fls.validate_comparisons([{"strategy": "S", "window": "long", "gap": 0.2, "p_value": 0.01}]) == []


# --- disagreement rule ---------------------------------------------------------------


def test_disagreement_not_testable_in_live_windows_never_plain_no() -> None:
    out = fls.disagreement_state(window="live", french_loading=0.4, french_t=3.0, p123_loading=-0.3, p123_t=-2.5)
    assert out["state"] == "NOT_TESTABLE"
    assert out["state"] != "NO"
    assert out["label"] == "informational, not validated"


def test_disagreement_missing_t_is_not_testable() -> None:
    out = fls.disagreement_state(window="long", french_loading=0.4, french_t=None, p123_loading=-0.3, p123_t=-2.5)
    assert out["state"] == "NOT_TESTABLE"


def test_disagreement_fires_only_on_opposite_signs_and_both_abs_t_at_least_2() -> None:
    fire = fls.disagreement_state(window="long", french_loading=0.4, french_t=2.0, p123_loading=-0.3, p123_t=-2.5)
    assert fire["state"] == "FIRES"
    low_t = fls.disagreement_state(window="long", french_loading=0.4, french_t=1.9, p123_loading=-0.3, p123_t=-2.5)
    assert low_t["state"] == "NO_DISAGREEMENT"
    same = fls.disagreement_state(window="long", french_loading=0.4, french_t=3, p123_loading=0.3, p123_t=2.5)
    assert same["state"] == "NO_DISAGREEMENT"


# --- bridge labels -------------------------------------------------------------------


def test_europe_basic_bridge_is_market_only_with_required_labels() -> None:
    rec = fls.bridge_record(region="Europe", enlarged_configured=False)
    assert rec["coverage"] == "market_only"
    assert rec["never_spliced"] is True
    assert "market only" in rec["label"]
    assert "materially weaker than French" in rec["label"]
    assert "never spliced" in rec["label"]


def test_europe_enlarged_bridge_keeps_weaker_and_never_spliced_labels() -> None:
    rec = fls.bridge_record(region="Europe", enlarged_configured=True)
    assert rec["coverage"] == "enlarged"
    assert "materially weaker than French" in rec["label"]
    assert "never spliced" in rec["label"]


def test_us_bridge_is_never_spliced() -> None:
    rec = fls.bridge_record(region="US", enlarged_configured=False)
    assert rec["never_spliced"] is True
    assert "never spliced" in rec["label"]


def test_splice_flag_fails() -> None:
    with pytest.raises(fls.StatusError, match="splice"):
        fls.bridge_record(region="Europe", enlarged_configured=False, spliced=True)


# --- document build, validation and output -------------------------------------------


def test_build_status_aggregates_and_is_deterministic() -> None:
    kwargs = dict(
        evaluation_date=EVAL,
        panels=[_french("2026-09-30"), _french("2026-09-30", region="Europe")],
        strategies=[{"strategy": "crypto", "adj_r2": {"french": -0.01, "etf": 0.0}}],
        comparisons=[{"strategy": "S", "window": "live", "gap": 0.1, "interval": [-0.2, 0.3]}],
        bridges=[fls.bridge_record(region="Europe", enlarged_configured=False)],
    )
    doc = fls.build_status(**kwargs)
    assert doc["schema"] == fls.SCHEMA
    assert doc["evaluation_date"] == "2026-11-19"
    assert doc["strategies"][0]["attribution"]["state"] == "NOT_APPLICABLE"
    assert fls.render_status(doc) == fls.render_status(fls.build_status(**kwargs))
    assert fls.render_status(doc).endswith(b"\n") and b"\r" not in fls.render_status(doc)


def test_build_status_collects_every_failure() -> None:
    bad_cmp = {"strategy": "S", "window": "live", "gap": 0.1, "stars": "*"}
    with pytest.raises(fls.StatusError) as err:
        fls.build_status(
            evaluation_date=EVAL,
            panels=[_french("2026-09-30"), {"panel_id": "x", "kind": "french"}],
            comparisons=[bad_cmp],
        )
    assert len(err.value.failures) >= 3


def test_build_status_requires_at_least_one_panel() -> None:
    with pytest.raises(fls.StatusError):
        fls.build_status(evaluation_date=EVAL, panels=[])


def test_write_status_is_create_only(tmp_path: Path) -> None:
    doc = fls.build_status(evaluation_date=EVAL, panels=[_french("2026-09-30")])
    out = tmp_path / "factor-lens-status.json"
    fls.write_status(doc, out)
    assert json.loads(out.read_bytes())["schema"] == fls.SCHEMA
    with pytest.raises(FileExistsError):
        fls.write_status(doc, out)


def test_cli_fails_the_run_on_a_failing_assertion(tmp_path: Path) -> None:
    spec = {
        "evaluation_date": "2026-11-19",
        "panels": [
            {
                "kind": "french", "panel_id": "french-us", "region": "US",
                "last_observation": None, "factors": list(fls.FRENCH_FACTORS["US"]),
            }
        ],
    }
    src = tmp_path / "in.json"
    src.write_text(json.dumps(spec), encoding="utf-8")
    out = tmp_path / "out.json"
    assert fls.main(["--input", str(src), "--output", str(out)]) == 1
    assert not out.exists()


def test_cli_writes_status_on_success(tmp_path: Path) -> None:
    spec = {
        "evaluation_date": "2026-11-19",
        "panels": [
            {
                "kind": "french", "panel_id": "french-us", "region": "US",
                "last_observation": "2026-09-30", "factors": list(fls.FRENCH_FACTORS["US"]),
            },
            {
                "kind": "etf_bridge", "panel_id": "etf-eu", "region": "Europe",
                "last_observation": "2026-11-18", "bridge_ids": ["VGK-RF"], "configured_ids": ["VGK-RF"],
            },
        ],
    }
    src = tmp_path / "in.json"
    src.write_text(json.dumps(spec), encoding="utf-8")
    out = tmp_path / "out.json"
    assert fls.main(["--input", str(src), "--output", str(out)]) == 0
    doc = json.loads(out.read_bytes())
    assert [p["state"] for p in doc["panels"]] == ["COMPLIANT", "FRESH"]
