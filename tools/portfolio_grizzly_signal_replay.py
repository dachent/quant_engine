"""Replay a vendor trade log on daily prices and stage the derived daily returns.

The vendor's published daily simulation ends at its last backtest date, while the
trade log keeps recording signal events.  Between the two the daily path is not
published, so it is derived here: every logged signal moves a long/flat state, the
state is executed on the declared session rule, and each trading day compounds the
open-to-close and close-to-open legs the state covers.

Every rule (time zone, session times, action-to-state map, execution phase, the
price basis, and the gates the validation must pass) is read from a policy file
and recorded in the staged generation; none is a code literal.  The replay is
validated on the dates where the vendor's own simulation exists, and the measured
error is part of the staged evidence.  The tool performs no network operation and
never creates or advances CURRENT.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Final, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from portfolio_strategy_history import excel_cell_date, xlsx_sheet_rows
from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    rename_directory_with_retry,
)


ADAPTER_VERSION: Final = "1"
CATALOG_SCHEMA_VERSION: Final = 1
RETURN_FIELDS: Final = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "return_basis",
)
POLICY_KEYS: Final = {
    "schema_version",
    "policy_id",
    "series_id",
    "evidence_layer",
    "return_basis",
    "time_zone",
    "session_open",
    "session_close",
    "action_states",
    "execution",
    "price_basis",
    "overnight_leg",
    "initial_state",
    "validation_gates",
}
EXECUTION_RULES: Final = {
    "at_or_before_open": "SAME_DAY_OPEN",
    "during_session": "SAME_DAY_CLOSE",
    "after_close": "NEXT_TRADING_DAY_OPEN",
    "same_phase_conflict": "LAST_SIGNAL_WINS",
}
GATE_KEYS: Final = {
    "min_overlap_observations",
    "min_correlation",
    "max_mean_abs_daily_error",
    "max_abs_cumulative_error",
}
OVERNIGHT_RULES: Final = frozenset(
    {"CREDITED_ONLY_IF_STATE_AFTER_OPEN_LONG", "CREDITED_IF_CARRIED_LONG"}
)
STATES: Final = frozenset({"LONG", "FLAT"})


class SignalReplayError(ValueError):
    """The replay inputs, policy or validation violate the declared contract."""


@dataclass(frozen=True)
class Policy:
    policy_id: str
    series_id: str
    evidence_layer: str
    return_basis: str
    zone: ZoneInfo
    session_open: time
    session_close: time
    action_states: Mapping[str, str]
    price_basis: str
    overnight_leg: str
    initial_state: str
    gates: Mapping[str, Decimal]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class Bar:
    open: Decimal
    close: Decimal


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SignalReplayError(f"{label} must be non-empty text")
    return value.strip()


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise SignalReplayError(f"{label} must be numeric")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise SignalReplayError(f"{label} must be numeric") from exc
    if not number.is_finite():
        raise SignalReplayError(f"{label} must be finite")
    return number


def _clock(value: object, label: str) -> time:
    try:
        return time.fromisoformat(_text(value, label))
    except ValueError as exc:
        raise SignalReplayError(f"{label} must be HH:MM") from exc


def load_policy(path: Path) -> tuple[Policy, bytes]:
    if path.is_symlink() or not path.is_file():
        raise SignalReplayError("replay policy must be a regular file")
    content = path.read_bytes()
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SignalReplayError("replay policy is not readable JSON") from exc
    if not isinstance(document, dict) or set(document) != POLICY_KEYS:
        raise SignalReplayError("replay policy keys differ")
    if document["schema_version"] != 1:
        raise SignalReplayError("replay policy schema version is unsupported")
    if document["execution"] != EXECUTION_RULES:
        raise SignalReplayError("replay execution rule is unsupported")
    if document["price_basis"] != "ADJUSTED_CLOSE_FACTOR_APPLIED_TO_OPEN":
        raise SignalReplayError("replay price basis is unsupported")
    if document["overnight_leg"] not in OVERNIGHT_RULES:
        raise SignalReplayError("replay overnight rule is unsupported")
    if document["initial_state"] != "FLAT":
        raise SignalReplayError("replay initial state is unsupported")
    states = document["action_states"]
    if (
        not isinstance(states, dict)
        or not states
        or any(
            not isinstance(key, str) or value not in STATES
            for key, value in states.items()
        )
    ):
        raise SignalReplayError("replay action_states are invalid")
    gates = document["validation_gates"]
    if not isinstance(gates, dict) or set(gates) != GATE_KEYS:
        raise SignalReplayError("replay validation_gates differ")
    try:
        zone = ZoneInfo(_text(document["time_zone"], "time_zone"))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SignalReplayError("replay time zone is unknown") from exc
    session_open = _clock(document["session_open"], "session_open")
    session_close = _clock(document["session_close"], "session_close")
    if not session_open < session_close:
        raise SignalReplayError("replay session is reversed")
    return (
        Policy(
            policy_id=_text(document["policy_id"], "policy_id"),
            series_id=_text(document["series_id"], "series_id"),
            evidence_layer=_text(document["evidence_layer"], "evidence_layer"),
            return_basis=_text(document["return_basis"], "return_basis"),
            zone=zone,
            session_open=session_open,
            session_close=session_close,
            action_states={k.upper(): v for k, v in states.items()},
            price_basis=document["price_basis"],
            overnight_leg=document["overnight_leg"],
            initial_state=document["initial_state"],
            gates={key: _decimal(gates[key], f"gate {key}") for key in GATE_KEYS},
            raw=document,
        ),
        content,
    )


def load_signals(content: bytes, policy: Policy) -> list[tuple[datetime, str]]:
    """Return (local timestamp, target state) in time order."""

    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    if not {"Date (ET)", "Action"} <= set(reader.fieldnames or ()):
        raise SignalReplayError("trade log headers differ")
    signals: list[tuple[datetime, str]] = []
    for row in reader:
        stamp = _text(row.get("Date (ET)"), "trade log timestamp")
        stamp = stamp.rsplit(" ", 1)[0] if stamp.endswith(" ET") else stamp
        try:
            observed = datetime.strptime(stamp, "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise SignalReplayError("trade log timestamp is invalid") from exc
        action = _text(row.get("Action"), "trade log action").upper()
        if action not in policy.action_states:
            raise SignalReplayError(f"trade log action is not in the policy: {action}")
        signals.append((observed, policy.action_states[action]))
    if signals != sorted(signals, key=lambda item: item[0]):
        raise SignalReplayError("trade log is not in time order")
    if len({item[0] for item in signals}) != len(signals):
        raise SignalReplayError("trade log timestamps overlap")
    if not signals:
        raise SignalReplayError("trade log is empty")
    return signals


def load_bars(content: bytes, policy: Policy) -> dict[date, Bar]:
    """Daily bars with the open scaled by the day's adjusted-close factor."""

    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    if not {"date", "open", "close", "adjclose"} <= set(reader.fieldnames or ()):
        raise SignalReplayError("price file headers differ")
    bars: dict[date, Bar] = {}
    for row in reader:
        try:
            day = date.fromisoformat(_text(row.get("date"), "price date"))
        except ValueError as exc:
            raise SignalReplayError("price date is invalid") from exc
        close = _decimal(row.get("close"), "price close")
        adjusted = _decimal(row.get("adjclose"), "price adjclose")
        opened = _decimal(row.get("open"), "price open")
        if close <= 0 or adjusted <= 0 or opened <= 0:
            raise SignalReplayError("price must be positive")
        factor = adjusted / close
        if day in bars:
            raise SignalReplayError("price file has a duplicate date")
        bars[day] = Bar(open=opened * factor, close=adjusted)
    if len(bars) < 2:
        raise SignalReplayError("price file is too short")
    return bars


def _execution_slot(
    stamp: datetime, calendar: Sequence[date], policy: Policy
) -> tuple[date, str] | None:
    """Return (trading day, OPEN|CLOSE) the signal executes on, or None past the end."""

    day = stamp.date()
    clock = stamp.time()
    trading = set(calendar)
    if day in trading and clock <= policy.session_open:
        return day, "OPEN"
    if day in trading and policy.session_open < clock <= policy.session_close:
        return day, "CLOSE"
    following = [candidate for candidate in calendar if candidate > day]
    if day in trading and clock > policy.session_close or day not in trading:
        return (following[0], "OPEN") if following else None
    return None


def replay(
    signals: Sequence[tuple[datetime, str]],
    bars: Mapping[date, Bar],
    policy: Policy,
) -> list[tuple[date, Decimal, str, str]]:
    """Daily (date, return, state_at_open_leg, state_at_close).

    A trading day compounds the open-to-close leg while the state after any open
    execution is long.  The close-to-open leg follows the policy's overnight rule:
    credited only when the state after the open execution is long (the vendor's own
    simulation books a sell at the open as flat for the whole day), or credited
    whenever the carried state is long.  A state change at the close takes effect
    from the next day's open.
    """

    calendar = sorted(bars)
    slots: dict[tuple[date, str], str] = {}
    pre_history_state = policy.initial_state
    for stamp, state in signals:
        slot = _execution_slot(stamp, calendar, policy)
        if slot is None:
            continue
        if slot[0] < calendar[0]:
            pre_history_state = state
            continue
        # the log is time ordered, so an assignment keeps the last signal of a slot
        slots[slot] = state
    # State at the first bar's close: signals executed on the first bar itself count.
    carried = slots.get(
        (calendar[0], "CLOSE"), slots.get((calendar[0], "OPEN"), pre_history_state)
    )
    result: list[tuple[date, Decimal, str, str]] = []
    for previous, day in zip(calendar, calendar[1:], strict=False):
        factor = Decimal(1)
        after_open = slots.get((day, "OPEN"), carried)
        overnight_long = (
            carried == "LONG" and after_open == "LONG"
            if policy.overnight_leg == "CREDITED_ONLY_IF_STATE_AFTER_OPEN_LONG"
            else carried == "LONG"
        )
        if overnight_long:
            factor *= bars[day].open / bars[previous].close
        if after_open == "LONG":
            factor *= bars[day].close / bars[day].open
        result.append((day, factor - Decimal(1), carried, after_open))
        carried = slots.get((day, "CLOSE"), after_open)
    return result


def validate(
    replayed: Sequence[tuple[date, Decimal, str, str]],
    reference: Mapping[date, Decimal],
    policy: Policy,
) -> dict[str, Any]:
    """Compare against the vendor's own daily simulation on shared dates."""

    mine = {day: value for day, value, _, _ in replayed}
    shared = sorted(set(mine) & set(reference))
    gates = policy.gates
    if len(shared) < int(gates["min_overlap_observations"]):
        raise SignalReplayError("replay overlap with the reference is too short")
    errors = [mine[day] - reference[day] for day in shared]
    mean_abs = sum((abs(item) for item in errors), Decimal(0)) / len(errors)
    cumulative_mine = Decimal(1)
    cumulative_ref = Decimal(1)
    for day in shared:
        cumulative_mine *= Decimal(1) + mine[day]
        cumulative_ref *= Decimal(1) + reference[day]
    cumulative_error = cumulative_mine / cumulative_ref - Decimal(1)
    xs = [float(mine[day]) for day in shared]
    ys = [float(reference[day]) for day in shared]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    spread = math.sqrt(sum((x - mean_x) ** 2 for x in xs)) * math.sqrt(
        sum((y - mean_y) ** 2 for y in ys)
    )
    correlation = covariance / spread if spread else 0.0
    exact = sum(1 for item in errors if abs(item) <= Decimal("1E-9"))
    worst = sorted(zip(shared, errors, strict=True), key=lambda p: -abs(p[1]))[:10]
    mismatch_by_year: dict[str, int] = {}
    for day, error in zip(shared, errors, strict=True):
        if abs(error) > Decimal("1E-6"):
            mismatch_by_year[str(day.year)] = mismatch_by_year.get(str(day.year), 0) + 1
    report: dict[str, Any] = {
        "overlap_observations": len(shared),
        "overlap_start": shared[0].isoformat(),
        "overlap_end": shared[-1].isoformat(),
        "days_equal_within_1e-9": exact,
        "days_differing_over_1e-6_by_year": mismatch_by_year,
        "mean_abs_daily_error": format(mean_abs, "f"),
        "max_abs_daily_error": format(max(abs(item) for item in errors), "f"),
        "correlation": correlation,
        "replay_cumulative_return": format(cumulative_mine - 1, "f"),
        "reference_cumulative_return": format(cumulative_ref - 1, "f"),
        "cumulative_error_ratio_minus_one": format(cumulative_error, "f"),
        "worst_days": [
            {"date": day.isoformat(), "error": format(error, "f")}
            for day, error in worst
        ],
    }
    failures = []
    if mean_abs > gates["max_mean_abs_daily_error"]:
        failures.append("mean absolute daily error")
    if abs(cumulative_error) > gates["max_abs_cumulative_error"]:
        failures.append("cumulative error")
    if Decimal(str(correlation)) < gates["min_correlation"]:
        failures.append("correlation")
    report["gates"] = {key: format(value, "f") for key, value in gates.items()}
    report["status"] = "VALIDATED" if not failures else "FAILED_" + "_".join(failures)
    if failures:
        raise SignalReplayError(
            "replay failed validation: " + ", ".join(failures) + f" {report}"
        )
    return report


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_json_bytes(value: Mapping[str, Any] | Sequence[Any]) -> bytes:
    """Encode a catalog exactly as the core manifest encoding does (sorted, compact, LF)."""

    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _returns_bytes(rows: Sequence[Mapping[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=RETURN_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _reference_returns(path: Path) -> dict[date, Decimal]:
    """Read the vendor simulation sheet through the history builder's own reader."""

    rows = xlsx_sheet_rows(path, "Simulation")
    headers = {str(value).strip(): index for index, value in enumerate(rows[0])}
    if not {"date", "day_ret"} <= set(headers):
        raise SignalReplayError("vendor simulation headers differ")
    result: dict[date, Decimal] = {}
    for row in rows[1:]:
        raw_day = row[headers["date"]]
        raw_ret = row[headers["day_ret"]]
        if raw_day is None or raw_ret is None:
            continue
        day = excel_cell_date(raw_day, "vendor simulation date")  # noqa: SLF001
        result[day] = _decimal(raw_ret, "vendor simulation day_ret")
    return result


def build(
    *,
    trade_log: Path,
    prices: Path,
    policy_path: Path,
    reference: Mapping[date, Decimal],
    reference_identity: Mapping[str, str],
    window_start: date,
    window_end: date,
    as_of: date,
) -> tuple[dict[Path, bytes], GenerationManifest, dict[str, Any]]:
    policy, policy_bytes = load_policy(policy_path)
    for path in (trade_log, prices):
        if path.is_symlink() or not path.is_file():
            raise SignalReplayError(f"input is not a regular file: {path.name}")
    log_bytes = trade_log.read_bytes()
    price_bytes = prices.read_bytes()
    signals = load_signals(log_bytes, policy)
    bars = load_bars(price_bytes, policy)
    replayed = replay(signals, bars, policy)
    # The log only constrains the state from its first signal, so the comparison
    # starts at the first trading day after it.
    covered = [row for row in replayed if row[0] > signals[0][0].date()]
    validation = validate(covered, reference, policy)
    selected = [
        (day, value, carried, opened)
        for day, value, carried, opened in replayed
        if window_start <= day <= window_end
    ]
    if not selected:
        raise SignalReplayError("replay window holds no trading day")
    if signals[-1][0].date() < window_end:
        raise SignalReplayError(
            "trade log ends before the replay window: signals after "
            f"{signals[-1][0].date().isoformat()} are unconfirmed"
        )
    rows = [
        {
            "series_id": policy.series_id,
            "date": day.isoformat(),
            "return": format(value, "f"),
            "evidence_layer": policy.evidence_layer,
            "return_basis": policy.return_basis,
        }
        for day, value, _, _ in selected
    ]
    returns_bytes = _returns_bytes(rows)
    catalog: dict[str, object] = {
        "as_of_date": as_of.isoformat(),
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "coverage": {
            "observed_end": rows[-1]["date"],
            "observed_start": rows[0]["date"],
            "return_observations": len(rows),
            "status": "COMPLETE_WITHIN_DECLARED_WINDOW",
        },
        "evidence_layer": policy.evidence_layer,
        "generation_status": "CANDIDATE_PENDING_OWNER_SELECTION",
        "inputs": {
            "policy": {"sha256": _sha(policy_bytes), "policy_id": policy.policy_id},
            "prices": {"name": prices.name, "sha256": _sha(price_bytes)},
            "reference_workbook": dict(reference_identity),
            "trade_log": {
                "last_signal": signals[-1][0].isoformat(),
                "name": trade_log.name,
                "sha256": _sha(log_bytes),
                "signal_count": len(signals),
            },
        },
        "returns_artifact_id": "returns.csv",
        "selector_authority": "OWNER_SELECTION_REQUIRED",
        "series_id": policy.series_id,
        "splice_policy": (
            "DERIVED_FROM_VENDOR_SIGNALS_NOT_OWNER_ACCOUNT_NOT_A_VENDOR_PUBLISHED_SERIES; "
            "FILLS_ONLY_WEEKS_BETWEEN_THE_VENDOR_SIMULATION_END_AND_ACCOUNT_LIVE_START"
        ),
        "validation": validation,
        "window": {"end": window_end.isoformat(), "start": window_start.isoformat()},
    }
    catalog_bytes = canonical_json_bytes(catalog)
    files: dict[Path, bytes] = {
        Path("catalog.json"): catalog_bytes,
        Path("returns.csv"): returns_bytes,
        Path("policy.json"): policy_bytes,
        Path("raw/trade-log.csv"): log_bytes,
        Path("raw/prices.csv"): price_bytes,
    }
    schemas = {
        "catalog.json": "signal-replay-catalog",
        "returns.csv": "signal-replay-returns",
        "policy.json": "signal-replay-policy",
        "raw/trade-log.csv": "vendor-trade-log",
        "raw/prices.csv": "daily-price-bars",
    }
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("portfolio-grizzly-signal-replay", ADAPTER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                f"{len(rows)} replayed daily returns from {rows[0]['date']} through "
                f"{rows[-1]['date']}; replay validated on {validation['overlap_observations']} "
                "dates of the vendor simulation"
            ),
            fact_types=("DERIVED_SIGNAL_REPLAY_RETURNS",),
            observed_start=date.fromisoformat(rows[0]["date"]),
            observed_end=date.fromisoformat(rows[-1]["date"]),
            known_gaps=(),
        ),
        artifacts=tuple(
            SourceArtifact(
                artifact_id=relative.as_posix(),
                sha256=_sha(content),
                size_bytes=len(content),
                as_of_date=as_of,
                schema_name=schemas[relative.as_posix()],
                schema_version="1",
            )
            for relative, content in sorted(files.items())
        ),
        verification=VerificationStatus.VERIFIED,
    )
    return files, manifest, catalog


def stage_candidate(
    output_root: Path,
    *,
    trade_log: Path,
    prices: Path,
    policy_path: Path,
    reference_workbook: Path,
    window_start: str,
    window_end: str,
    as_of: str,
) -> dict[str, object]:
    try:
        start = date.fromisoformat(window_start)
        end = date.fromisoformat(window_end)
        as_of_day = date.fromisoformat(as_of)
    except ValueError as exc:
        raise SignalReplayError("window and as-of dates must be ISO dates") from exc
    if reference_workbook.is_symlink() or not reference_workbook.is_file():
        raise SignalReplayError("reference workbook is not a regular file")
    files, manifest, catalog = build(
        trade_log=trade_log,
        prices=prices,
        policy_path=policy_path,
        reference=_reference_returns(reference_workbook),
        reference_identity={
            "name": reference_workbook.name,
            "sha256": _sha(reference_workbook.read_bytes()),
        },
        window_start=start,
        window_end=end,
        as_of=as_of_day,
    )
    expected = {Path("generation.json"): manifest.to_json_bytes(), **files}
    generations = output_root.resolve() / "generations"
    final = generations / manifest.generation_id
    if final.exists():
        if final.is_symlink() or not final.is_dir():
            raise SignalReplayError("candidate generation path conflicts")
        for relative, content in expected.items():
            path = final / relative
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise SignalReplayError("candidate generation differs")
    else:
        generations.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".portfolio-signal-replay-", dir=generations)
        )
        try:
            for relative, content in expected.items():
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            rename_directory_with_retry(temporary, final)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return {
        "generation_id": manifest.generation_id,
        "manifest_sha256": _sha(manifest.to_json_bytes()),
        "published": False,
        "returns_sha256": _sha(files[Path("returns.csv")]),
        "state": "STAGED_CANDIDATE_PENDING_OWNER_SELECTION",
        "validation": catalog["validation"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-log", type=Path, required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--reference-workbook", type=Path, required=True)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--as-of-date", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = stage_candidate(
            args.output_root,
            trade_log=args.trade_log,
            prices=args.prices,
            policy_path=args.policy,
            reference_workbook=args.reference_workbook,
            window_start=args.window_start,
            window_end=args.window_end,
            as_of=args.as_of_date,
        )
    except SignalReplayError as exc:
        print(f"portfolio-grizzly-signal-replay: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
