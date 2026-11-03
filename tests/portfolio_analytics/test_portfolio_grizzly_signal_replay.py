from __future__ import annotations

from datetime import date
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_grizzly_signal_replay.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_grizzly_signal_replay", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("unable to load the signal replay tool")
replay_tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replay_tool
SPEC.loader.exec_module(replay_tool)


def _policy_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "policy_id": "grizzly-signal-replay-v1",
        "series_id": "Fixture Strategy",
        "evidence_layer": "DERIVED",
        "return_basis": "VENDOR_SIGNAL_REPLAY_ON_DAILY_ADJUSTED_PRICES",
        "time_zone": "America/New_York",
        "session_open": "09:30",
        "session_close": "16:00",
        "action_states": {"Buy": "LONG", "Rebuy": "LONG", "Sell": "FLAT"},
        "execution": {
            "at_or_before_open": "SAME_DAY_OPEN",
            "during_session": "SAME_DAY_CLOSE",
            "after_close": "NEXT_TRADING_DAY_OPEN",
            "same_phase_conflict": "LAST_SIGNAL_WINS",
        },
        "price_basis": "ADJUSTED_CLOSE_FACTOR_APPLIED_TO_OPEN",
        "overnight_leg": "CREDITED_ONLY_IF_STATE_AFTER_OPEN_LONG",
        "initial_state": "FLAT",
        "validation_gates": {
            "min_overlap_observations": 3,
            "min_correlation": "0.5",
            "max_mean_abs_daily_error": "0.01",
            "max_abs_cumulative_error": "0.05",
        },
    }
    document.update(overrides)
    return document


def _write_policy(path: Path, **overrides: object) -> Path:
    path.write_text(json.dumps(_policy_document(**overrides)), encoding="utf-8")
    return path


# Mon 2026-01-05 .. Fri 2026-01-09, then Mon 2026-01-12.  open/close per day.
_BARS = [
    ("2026-01-05", "100", "102"),
    ("2026-01-06", "104", "103"),
    ("2026-01-07", "105", "110"),
    ("2026-01-08", "108", "109"),
    ("2026-01-09", "111", "99"),
    ("2026-01-12", "100", "101"),
]


def _prices(path: Path) -> Path:
    lines = ["date,open,high,low,close,adjclose,volume,dividend"]
    for day, opened, close in _BARS:
        lines.append(f"{day},{opened},{opened},{opened},{close},{close},1,")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _log(path: Path, events: list[tuple[str, str]]) -> Path:
    lines = ["Date (ET),Action,SPX,Prev Price,Profit / Loss,PnL %,Model,Buy & Hold"]
    for stamp, action in events:
        lines.append(f"{stamp} ET,{action},1,1,,,1,1")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _by_day(rows: list[tuple[date, Decimal, str, str]]) -> dict[str, Decimal]:
    return {day.isoformat(): value for day, value, _, _ in rows}


def test_signal_phases_follow_the_policy_session_rule(tmp_path: Path) -> None:
    policy, _ = replay_tool.load_policy(_write_policy(tmp_path / "p.json"))
    bars = replay_tool.load_bars(_prices(tmp_path / "prices.csv").read_bytes(), policy)
    log = _log(
        tmp_path / "log.csv",
        [
            ("2026-01-04 20:00", "Buy"),  # Sunday evening: next trading day's open
            ("2026-01-06 13:00", "Sell"),  # during the session: that day's close
            ("2026-01-07 18:00", "Rebuy"),  # after the close: next trading day's open
            ("2026-01-09 09:30", "Sell"),  # at the open: that day's open
        ],
    )
    signals = replay_tool.load_signals(log.read_bytes(), policy)
    result = _by_day(replay_tool.replay(signals, bars, policy))

    # 01-05 is the first bar: no predecessor, so it carries no return row.
    assert "2026-01-05" not in result
    # long from the 01-05 open (Sunday signal) is not credited for 01-05; 01-06
    # holds from the prior close through the intraday sell at the close.
    assert result["2026-01-06"] == Decimal("103") / Decimal("102") - 1
    # flat after the close of 01-06 until the 01-07 after-close rebuy
    assert result["2026-01-07"] == 0
    # rebuy executes at the 01-08 open: open-to-close only (no overnight credit)
    assert result["2026-01-08"] == Decimal("109") / Decimal("108") - 1
    # the 01-09 open sell books the whole day as flat
    assert result["2026-01-09"] == 0
    assert result["2026-01-12"] == 0


def test_overnight_rule_is_policy_not_code(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    strict, _ = replay_tool.load_policy(_write_policy(base / "p.json"))
    credited, _ = replay_tool.load_policy(
        _write_policy(base / "q.json", overnight_leg="CREDITED_IF_CARRIED_LONG")
    )
    bars = replay_tool.load_bars(_prices(base / "prices.csv").read_bytes(), strict)
    log = _log(
        base / "log.csv",
        [("2026-01-05 07:00", "Buy"), ("2026-01-08 08:00", "Sell")],
    )
    signals = replay_tool.load_signals(log.read_bytes(), strict)
    strict_rows = _by_day(replay_tool.replay(signals, bars, strict))
    credited_rows = _by_day(replay_tool.replay(signals, bars, credited))
    assert strict_rows["2026-01-08"] == 0
    assert credited_rows["2026-01-08"] == Decimal("108") / Decimal("110") - 1
    assert strict_rows["2026-01-07"] == credited_rows["2026-01-07"]


def test_last_signal_in_a_phase_wins_and_prehistory_sets_the_carried_state(
    tmp_path: Path,
) -> None:
    policy, _ = replay_tool.load_policy(_write_policy(tmp_path / "p.json"))
    bars = replay_tool.load_bars(_prices(tmp_path / "prices.csv").read_bytes(), policy)
    log = _log(
        tmp_path / "log.csv",
        [
            ("2025-12-01 10:00", "Buy"),  # before the first bar: long when it opens
            ("2026-01-06 01:00", "Sell"),
            ("2026-01-06 07:00", "Rebuy"),  # same phase as the sell: the rebuy wins
        ],
    )
    result = _by_day(
        replay_tool.replay(
            replay_tool.load_signals(log.read_bytes(), policy), bars, policy
        )
    )
    assert result["2026-01-06"] == Decimal("103") / Decimal("102") - 1


def test_validation_reports_the_error_and_refuses_a_replay_that_misses_the_gates(
    tmp_path: Path,
) -> None:
    policy, _ = replay_tool.load_policy(_write_policy(tmp_path / "p.json"))
    rows = [
        (date(2026, 1, 5 + offset), Decimal(value), "LONG", "LONG")
        for offset, value in enumerate(["0.01", "-0.02", "0.03", "0.00"])
    ]
    reference = {day: value for day, value, _, _ in rows}
    report = replay_tool.validate(rows, reference, policy)
    assert report["status"] == "VALIDATED"
    assert report["overlap_observations"] == 4
    assert Decimal(report["mean_abs_daily_error"]) == 0

    off = {day: value + Decimal("0.2") for day, value in reference.items()}
    with pytest.raises(replay_tool.SignalReplayError, match="failed validation"):
        replay_tool.validate(rows, off, policy)
    with pytest.raises(replay_tool.SignalReplayError, match="too short"):
        replay_tool.validate(rows[:2], reference, policy)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"execution": {"at_or_before_open": "NEXT_DAY_CLOSE"}}, "execution rule"),
        ({"overnight_leg": "ALWAYS"}, "overnight rule"),
        ({"time_zone": "Mars/Base"}, "time zone"),
        ({"session_open": "17:00"}, "reversed"),
        ({"action_states": {"Buy": "SHORT"}}, "action_states"),
        ({"price_basis": "RAW_CLOSE"}, "price basis"),
        ({"validation_gates": {"min_correlation": "0.5"}}, "validation_gates"),
    ],
)
def test_policy_rejects_unsupported_rules(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(replay_tool.SignalReplayError, match=message):
        replay_tool.load_policy(_write_policy(tmp_path / "p.json", **overrides))


def test_trade_log_with_unknown_action_or_disorder_is_refused(tmp_path: Path) -> None:
    policy, _ = replay_tool.load_policy(_write_policy(tmp_path / "p.json"))
    unknown = _log(tmp_path / "a.csv", [("2026-01-05 10:00", "Hold")])
    with pytest.raises(replay_tool.SignalReplayError, match="not in the policy"):
        replay_tool.load_signals(unknown.read_bytes(), policy)
    unordered = _log(
        tmp_path / "b.csv", [("2026-01-06 10:00", "Buy"), ("2026-01-05 10:00", "Sell")]
    )
    with pytest.raises(replay_tool.SignalReplayError, match="time order"):
        replay_tool.load_signals(unordered.read_bytes(), policy)


def test_stage_is_create_only_validated_and_never_publishes(tmp_path: Path) -> None:
    policy_path = _write_policy(tmp_path / "p.json")
    prices = _prices(tmp_path / "prices.csv")
    log = _log(
        tmp_path / "log.csv",
        [("2026-01-05 07:00", "Buy"), ("2026-01-09 13:00", "Sell")],
    )
    policy, _ = replay_tool.load_policy(policy_path)
    signals = replay_tool.load_signals(log.read_bytes(), policy)
    bars = replay_tool.load_bars(prices.read_bytes(), policy)
    reference = {
        day: value for day, value, _, _ in replay_tool.replay(signals, bars, policy)
    }
    arguments = {
        "trade_log": log,
        "prices": prices,
        "policy_path": policy_path,
        "reference": reference,
        "reference_identity": {"name": "fixture", "sha256": "0" * 64},
        "window_start": date(2026, 1, 6),
        "window_end": date(2026, 1, 9),
        "as_of": date(2026, 1, 12),
    }
    files, manifest, catalog = replay_tool.build(**arguments)
    assert catalog["validation"]["status"] == "VALIDATED"
    rows = files[Path("returns.csv")].decode("utf-8").splitlines()
    assert rows[0] == "series_id,date,return,evidence_layer,return_basis"
    assert [line.split(",")[1] for line in rows[1:]] == [
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
    ]
    assert all(",DERIVED," in line for line in rows[1:])
    # the replay window may not run past the last logged signal
    with pytest.raises(replay_tool.SignalReplayError, match="ends before"):
        replay_tool.build(**{**arguments, "window_end": date(2026, 1, 12)})
    again = replay_tool.build(**arguments)
    assert again[1].generation_id == manifest.generation_id


def _stage_arguments(tmp_path: Path) -> dict[str, object]:
    policy_path = _write_policy(tmp_path / "p.json")
    prices = _prices(tmp_path / "prices.csv")
    log = _log(
        tmp_path / "log.csv",
        [("2026-01-05 07:00", "Buy"), ("2026-01-09 13:00", "Sell")],
    )
    policy, _ = replay_tool.load_policy(policy_path)
    signals = replay_tool.load_signals(log.read_bytes(), policy)
    bars = replay_tool.load_bars(prices.read_bytes(), policy)
    reference = {
        day: value for day, value, _, _ in replay_tool.replay(signals, bars, policy)
    }
    return {
        "trade_log": log,
        "prices": prices,
        "policy_path": policy_path,
        "reference": reference,
        "reference_identity": {"name": "fixture", "sha256": "0" * 64},
        "window_start": date(2026, 1, 6),
        "window_end": date(2026, 1, 9),
        "as_of": date(2026, 1, 12),
    }


def test_window_may_not_pass_the_declared_as_of_date(tmp_path: Path) -> None:
    arguments = _stage_arguments(tmp_path)
    with pytest.raises(replay_tool.SignalReplayError, match="as-of"):
        replay_tool.build(**{**arguments, "as_of": date(2026, 1, 8)})


def test_price_file_must_cover_the_window(tmp_path: Path) -> None:
    arguments = _stage_arguments(tmp_path)
    prices = arguments["prices"]
    assert isinstance(prices, Path)
    kept = [
        line
        for line in prices.read_text().splitlines()
        if "2026-01-08" not in line and "2026-01-07" not in line
    ]
    prices.write_text("\n".join(kept) + "\n", encoding="utf-8")
    with pytest.raises(replay_tool.SignalReplayError, match="consecutive weekdays"):
        replay_tool.build(**arguments)


def test_signal_times_are_converted_into_the_policy_zone(tmp_path: Path) -> None:
    log = _log(tmp_path / "log.csv", [("2026-01-05 08:00", "Buy")])
    new_york, _ = replay_tool.load_policy(_write_policy(tmp_path / "ny.json"))
    utc, _ = replay_tool.load_policy(
        _write_policy(
            tmp_path / "utc.json",
            time_zone="UTC",
            session_open="14:30",
            session_close="21:00",
        )
    )
    local = replay_tool.load_signals(log.read_bytes(), new_york)[0][0]
    converted = replay_tool.load_signals(log.read_bytes(), utc)[0][0]
    assert local.hour == 8
    assert converted.hour == 13
