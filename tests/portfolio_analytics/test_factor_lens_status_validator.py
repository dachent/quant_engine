"""Factor lens status: the validator wiring (build_status and main), review round on PR 1058.

Every rule here is exercised through ``build_status`` or ``main`` so a mutant that skips the
check in the aggregate fails a test. Synthetic fixtures only.
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
DAYS = [date(2026, 11, d) for d in (9, 10, 11, 12, 13)]
WINDOWS = [{"week_start": "2026-11-09", "days": [d.isoformat() for d in DAYS]}]
FACTORS = list(fls.FRENCH_FACTORS["US"])


def french(last: str, evaluation: date = EVAL) -> dict:
    return fls.french_panel(
        panel_id="french-us",
        region="US",
        last_observation=last,
        evaluation_date=evaluation,
        factors=FACTORS,
    )


def status(**kw: object) -> dict:
    base: dict = {"evaluation_date": EVAL, "windows": WINDOWS, "trading_days": DAYS}
    return fls.build_status(**{**base, **kw})  # type: ignore[arg-type]


def spec(**over: object) -> dict:
    base: dict = {
        "evaluation_date": "2026-11-19",
        "windows": WINDOWS,
        "trading_days": [d.isoformat() for d in DAYS],
        "panels": [
            {
                "kind": "french",
                "panel_id": "french-us",
                "region": "US",
                "last_observation": "2026-09-30",
                "factors": FACTORS,
            }
        ],
    }
    return {**base, **over}


def run_main(tmp_path: Path, doc: dict) -> int:
    src = tmp_path / "in.json"
    src.write_text(json.dumps(doc), encoding="utf-8")
    return fls.main(["--input", str(src), "--output", str(tmp_path / "out.json")])


# --- age from the month end of the last observation's month (P2-7) ----------------------


@pytest.mark.parametrize(
    ("last", "evaluation", "age", "state"),
    [
        # 2026-02-28 is a Saturday; the last daily row is Friday the 27th.
        ("2026-02-27", date(2026, 5, 14), 75, "COMPLIANT"),
        ("2026-02-27", date(2026, 5, 15), 76, "HARD_STALE"),  # March now required
        (
            "2026-05-29",
            date(2026, 8, 13),
            74,
            "COMPLIANT",
        ),  # May ends on Sunday the 31st
        ("2026-05-29", date(2026, 8, 14), 75, "HARD_STALE"),  # June required on Aug 14
    ],
)
def test_age_counts_from_month_end_of_last_observation(
    last: str, evaluation: date, age: int, state: str
) -> None:
    panel = french(last, evaluation)
    assert panel["age_days"] == age
    assert panel["state"] == state
    assert panel["last_observation"] == last


def test_french_exit_text_uses_data_already_present() -> None:
    panel = french("2026-10-30")  # October data in hand at 2026-11-19
    assert panel["state"] == "COMPLIANT"
    assert panel["next_required_month_end"] == "2026-11-30"
    assert panel["next_due_date"] == "2027-01-14"
    assert "2026-11" in panel["exit"]
    assert "stale from 2027-01-14" in panel["exit"]
    assert "holds until" not in panel["exit"]


# --- panel records are re-derived, never trusted (P2-2) ----------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"last_observation": "2020-01-01", "age_days": 0, "state": "COMPLIANT"},
        {"state": "WHATEVER"},
        {"state": "HARD_STALE"},  # contributes_to_headline stays True
        {"contributes_to_headline": False},
        {"age_days": 1},
    ],
)
def test_build_status_rejects_forged_panels(changes: dict) -> None:
    with pytest.raises(fls.StatusError):
        status(panels=[{**french("2026-09-30"), **changes}])


def test_build_status_accepts_honest_panel_and_rejects_one_built_for_another_date() -> (
    None
):
    assert status(panels=[french("2026-09-30")])["panels"][0]["state"] == "COMPLIANT"
    with pytest.raises(fls.StatusError, match="declared state"):
        status(panels=[french("2026-09-30", date(2026, 12, 16))])


def test_unknown_panel_kind_fails() -> None:
    with pytest.raises(fls.StatusError, match="unknown panel kind"):
        status(panels=[{**french("2026-09-30"), "kind": "mystery"}])


# --- windows are wired (P2-3) -----------------------------------------------------------


def test_build_status_requires_windows_and_trading_days() -> None:
    panels = [french("2026-09-30")]
    for kw in (
        {},
        {"windows": [], "trading_days": DAYS},
        {"windows": WINDOWS},
        {"windows": WINDOWS, "trading_days": []},
    ):
        with pytest.raises(fls.StatusError, match="windows"):
            fls.build_status(evaluation_date=EVAL, panels=panels, **kw)  # type: ignore[arg-type]


def test_build_status_fails_on_shifted_window() -> None:
    shifted = [
        {
            "week_start": "2026-11-09",
            "days": [
                "2026-11-10",
                "2026-11-11",
                "2026-11-12",
                "2026-11-13",
                "2026-11-16",
            ],
        }
    ]
    with pytest.raises(fls.StatusError, match="2026-11-09"):
        status(panels=[french("2026-09-30")], windows=shifted)


def test_main_fails_without_windows(tmp_path: Path) -> None:
    doc = spec()
    del doc["windows"]
    assert run_main(tmp_path, doc) == 1
    assert not (tmp_path / "out.json").exists()


# --- bridges go through validation (P2-3) -----------------------------------------------


def bridge(**changes: object) -> dict:
    return {**fls.bridge_record(region="Europe", enlarged_configured=False), **changes}


def test_build_status_accepts_canonical_bridge() -> None:
    assert status(panels=[french("2026-09-30")], bridges=[bridge()])["bridges"]


@pytest.mark.parametrize(
    "bad",
    [
        {"never_spliced": False},
        {"never_spliced": "true"},
        {"never_spliced": 1},
        {"label": "market only, spliced to French levels"},
        {"label": "as good as French"},
        {"label": "market only, never spliced to French levels"},
        {"coverage": "enlarged"},
        {"region": "Mars"},
    ],
)
def test_build_status_rejects_bad_bridge(bad: dict) -> None:
    with pytest.raises(fls.StatusError):
        status(panels=[french("2026-09-30")], bridges=[bridge(**bad)])


def test_build_status_rejects_bridge_missing_never_spliced() -> None:
    rec = bridge()
    del rec["never_spliced"]
    with pytest.raises(fls.StatusError, match="never_spliced"):
        status(panels=[french("2026-09-30")], bridges=[rec])


# --- Stage 2 cannot slip through (P2-4) -------------------------------------------------


def labelled() -> dict:
    return fls.p123_panel(
        panel_id="lp",
        last_observation="2026-11-01",
        evaluation_date=EVAL,
        definition_hashes_present=True,
    )


def test_labelled_panel_cannot_pass_unchecked() -> None:
    with pytest.raises(NotImplementedError, match="Stage 2"):
        status(panels=[labelled()])
    with pytest.raises(NotImplementedError, match="Stage 2"):
        fls.check_labelled_panel_rows(labelled())


def test_non_labelled_panels_never_reach_the_placeholder() -> None:
    assert status(panels=[french("2026-09-30")])


def test_main_fails_on_a_labelled_panel(tmp_path: Path) -> None:
    panels = [
        {"kind": "labelled_panel", "panel_id": "lp", "last_observation": "2026-11-01"}
    ]
    assert run_main(tmp_path, spec(panels=panels)) == 1


# --- live-window gate and significance marks (P2-5) -------------------------------------


@pytest.mark.parametrize(
    "window", ["live", "LIVE", " Live ", None, "", "long", "weird", 3]
)
def test_any_window_not_full_or_backtest_is_live(window: object) -> None:
    cmp = {"strategy": "S", "gap": 0.1, "window": window}
    with pytest.raises(fls.StatusError, match="noise threshold"):
        status(panels=[french("2026-09-30")], comparisons=[cmp])


def test_missing_window_key_is_live() -> None:
    with pytest.raises(fls.StatusError, match="noise threshold"):
        status(
            panels=[french("2026-09-30")], comparisons=[{"strategy": "S", "gap": 0.1}]
        )


@pytest.mark.parametrize("window", ["full", "FULL", " Backtest "])
def test_full_and_backtest_windows_may_carry_p_values(window: str) -> None:
    cmp = {"strategy": "S", "gap": 0.1, "window": window, "p_value": 0.01}
    assert status(panels=[french("2026-09-30")], comparisons=[cmp])["comparisons"]


@pytest.mark.parametrize(
    "extra",
    [
        {"stars": "***"},
        {"note": "0.01***"},
        {"detail": {"p_value": 0.01}},
        {"detail": [{"label": "*"}]},
        {"t_stat": 2.4},
        {"t": 2.4},
        {"p": 0.03},
        {"wild_bootstrap_p": 0.04},
        {"bootstrap": 0.04},
        {"pvalue": 0.04},
        {"Significant": True},
        {"detail": {"inner": {"stars": 3}}},
        {"p_nw": 0.04},
        {"mark": "*"},
    ],
)
def test_significance_marks_fail_through_build_status(extra: dict) -> None:
    ok = {"strategy": "S", "window": "live", "gap": 0.08, "noise_threshold": 0.2}
    assert status(panels=[french("2026-09-30")], comparisons=[ok])
    with pytest.raises(fls.StatusError, match="significance"):
        status(panels=[french("2026-09-30")], comparisons=[{**ok, **extra}])


@pytest.mark.parametrize(
    "interval",
    [
        [0.1, 0.1],
        [0.1, float("nan")],
        [float("-inf"), 0.2],
        ["a", "b"],
        [0.2, 0.1],
        [0.1],
    ],
)
def test_bad_interval_fails_through_build_status(interval: list) -> None:
    cmp = {"strategy": "S", "window": "live", "gap": 0.1, "interval": interval}
    with pytest.raises(fls.StatusError, match="interval"):
        status(panels=[french("2026-09-30")], comparisons=[cmp])


def test_main_fails_on_significance_mark(tmp_path: Path) -> None:
    cmp = {
        "strategy": "S",
        "window": "live",
        "gap": 0.1,
        "noise_threshold": 0.2,
        "stars": "**",
    }
    assert run_main(tmp_path, spec(comparisons=[cmp])) == 1


# --- disagreement gate (P2-1) -----------------------------------------------------------


@pytest.mark.parametrize("window", ["live", "LIVE", " live", None, "weird"])
def test_live_disagreement_is_always_not_testable(window: object) -> None:
    for french_loading, p123_loading in ((0.4, -0.3), (0.4, 0.3)):
        out = fls.disagreement_state(
            window=window,  # type: ignore[arg-type]
            french_loading=french_loading,
            french_t=5.0,
            p123_loading=p123_loading,
            p123_t=5.0,
        )
        assert out["state"] == "NOT_TESTABLE"


@pytest.mark.parametrize(
    ("french_t", "p123_t"),
    [
        (1.9, 3.0),
        (3.0, 1.9),
        (float("nan"), 3.0),
        (3.0, float("inf")),
        (None, 3.0),
        ("3", 3.0),
    ],
)
def test_disagreement_needs_both_abs_t_at_least_2_and_finite(
    french_t: object, p123_t: object
) -> None:
    out = fls.disagreement_state(
        window="full",
        french_loading=0.4,
        french_t=french_t,  # type: ignore[arg-type]
        p123_loading=-0.3,
        p123_t=p123_t,  # type: ignore[arg-type]
    )
    assert out["state"] == "NOT_TESTABLE"


def test_disagreement_non_finite_loading_is_not_testable() -> None:
    out = fls.disagreement_state(
        window="full",
        french_loading=float("nan"),
        french_t=3.0,
        p123_loading=-0.3,
        p123_t=3.0,
    )
    assert out["state"] == "NOT_TESTABLE"


def test_disagreement_testable_states() -> None:
    fires = fls.disagreement_state(
        window="Backtest",
        french_loading=0.4,
        french_t=-2.0,
        p123_loading=-0.3,
        p123_t=2.0,
    )
    same = fls.disagreement_state(
        window="full", french_loading=0.4, french_t=2.0, p123_loading=0.3, p123_t=-2.5
    )
    assert fires["state"] == "FIRES"
    assert same["state"] == "NO_DISAGREEMENT"


# --- P123 hash label (P2-6) -------------------------------------------------------------


@pytest.mark.parametrize(
    "flag", ["false", "no", "0", "UNHASHED", "true", 1, 0, None, [], {}]
)
def test_only_real_true_clears_unhashed(flag: object) -> None:
    panel = fls.p123_panel(
        panel_id="lp",
        last_observation="2026-11-01",
        evaluation_date=EVAL,
        definition_hashes_present=flag,
    )
    assert panel["labels"] == ["UNHASHED"]


def test_real_true_clears_unhashed() -> None:
    assert labelled()["labels"] == []


# --- attribution input and output (P3) --------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "0.3", None, True])
def test_attribution_rejects_non_finite_or_non_numeric(bad: object) -> None:
    with pytest.raises(fls.StatusError):
        fls.attribution_status({"french": bad, "etf": 0.1})  # type: ignore[dict-item]


def test_bad_attribution_input_fails_cleanly_through_build_status_and_main(
    tmp_path: Path,
) -> None:
    with pytest.raises(fls.StatusError):
        status(
            panels=[french("2026-09-30")],
            strategies=[{"strategy": "S", "adj_r2": {"french": float("nan")}}],
        )
    bad = spec(strategies=[{"strategy": "S", "adj_r2": {"french": "x"}}])
    assert run_main(tmp_path, bad) == 1  # a clean exit code, no traceback


def test_not_applicable_rows_strip_loadings() -> None:
    doc = status(
        panels=[french("2026-09-30")],
        strategies=[
            {
                "strategy": "crypto",
                "adj_r2": {"french": -0.01},
                "loadings": {"Mkt-RF": 0.4},
                "betas": [0.1],
                "factor_coef": 1,
            },
            {
                "strategy": "real",
                "adj_r2": {"french": 0.6},
                "loadings": {"Mkt-RF": 0.9},
            },
        ],
    )
    crypto, real = doc["strategies"]
    assert crypto["attribution"]["state"] == "NOT_APPLICABLE"
    assert not {"loadings", "betas", "factor_coef"} & set(crypto)
    assert real["loadings"] == {"Mkt-RF": 0.9}
