"""One-command governed Yahoo re-capture of the base proxies and the style tickers.

Wraps ``yahoo_fetch_driver`` (config-check, make-evidence, fetch, verify) so a refresh
or re-capture needs no hand-built config, evidence or acceptance script:

  prepare  --cutoff D --label R4 --out DIR
      writes DIR/<kind>-config.json for each kind (base, style, sector, held, qsi, qb) and DIR/comment.txt, a
      value-free governance comment naming the run ids and bound config shas. Post that
      comment on the governance issue (e.g. ``ghx -Method POST
      repos/<owner>/<repo>/issues/283/comments -Body '{"body": ...}'``); the posting and
      any owner approval record stay human or control actions, never done here.
  run      --cutoff D --label R4 --review COMMENT_URL --out-root DIR [--rerun-index 2]
      for each kind: make-evidence, governed fetch, driver verify, then acceptance
      (last date, rows, key parity raw/adjusted, month-aware distribution record for the
      monthly payers). Writes ACCEPTANCE.json and SHA256SUMS.txt in the run folder and
      exits non-zero when any run fails acceptance.

Run id convention: ``<LABEL>-BASE`` / ``<LABEL>-STYLE``; a re-capture of the same cutoff
appends ``-<n>`` (R3-BASE-2, R3-STYLE-2). Public Yahoo, no login, no account write.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import yahoo_fetch_driver as driver

SCHEMA_VERSION = 1
MAX_REQUESTS = 300
EVIDENCE_HOURS = 20
# (group_id, first bar, symbols, calendar) per kind. The first bar is each group's
# inception, so a group never asks for dates before its first listing.
BASE_GROUPS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("base-proxies", "2007-05-30", ("SPY", "IWM", "HYG", "BIL"), "weekday"),
)
STYLE_GROUPS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    (
        "style-etfs-2007",
        "2007-01-01",
        (
            "IWS",
            "IWP",
            "IWN",
            "IWO",
            "ITOT",
            "IEF",
            "TLT",
            "LQD",
            "VNQ",
            "GLD",
            "DBC",
            "SPY",
        ),
        "weekday",
    ),
    ("style-bil", "2007-05-30", ("BIL",), "weekday"),
    ("style-hyg", "2007-04-11", ("HYG",), "weekday"),
    ("style-iefa-iemg", "2012-10-24", ("IEFA", "IEMG"), "weekday"),
    ("style-iwx-iwy", "2009-09-28", ("IWX", "IWY"), "weekday"),
    ("style-spxl", "2008-11-05", ("SPXL",), "weekday"),
    ("style-uup", "2007-03-01", ("UUP",), "weekday"),
    ("style-crypto-btc", "2014-09-17", ("BTC-USD",), "every_day"),
    ("style-crypto-eth", "2017-11-09", ("ETH-USD",), "every_day"),
)
SECTOR_GROUPS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    (
        "sector-spdrs-2000",
        "2000-01-01",
        ("XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLK", "XLU"),
        "weekday",
    ),
    ("sector-xlre", "2015-10-08", ("XLRE",), "weekday"),
    ("sector-xlc", "2018-06-19", ("XLC",), "weekday"),
    ("sector-bil", "2007-05-30", ("BIL",), "weekday"),
)
# Held Schwab symbols not covered by base/style/sector. IBIT, the youngest, first traded 2024-01-11;
# one common start keeps the row floor honest for every symbol.
HELD_GROUPS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    (
        "held-closes-2024",
        "2024-01-11",
        (
            "BNY",
            "BRK-B",
            "CURE",
            "DUK",
            "IBIT",
            "PEP",
            "PG",
            "SHY",
            "SQQQ",
            "TMF",
            "TYD",
            "UPRO",
        ),
        "weekday",
    ),
)
# QSI refresh: the 15 QSI tickers, one group each, first bar = first row of the F1 price files
# (qsi/inputs/prices); a common start would fail the row floor of the later listings.
QSI_GROUPS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("qsi-bil", "2007-05-30", ("BIL",), "weekday"),
    ("qsi-cure", "2011-06-15", ("CURE",), "weekday"),
    ("qsi-dgp", "2008-02-28", ("DGP",), "weekday"),
    ("qsi-efo", "2009-06-04", ("EFO",), "weekday"),
    ("qsi-erx", "2008-11-19", ("ERX",), "weekday"),
    ("qsi-fas", "2008-11-19", ("FAS",), "weekday"),
    ("qsi-soxl", "2010-03-11", ("SOXL",), "weekday"),
    ("qsi-spxl", "2008-11-05", ("SPXL",), "weekday"),
    ("qsi-sqqq", "2010-02-11", ("SQQQ",), "weekday"),
    ("qsi-tecl", "2008-12-30", ("TECL",), "weekday"),
    ("qsi-tmf", "2009-04-16", ("TMF",), "weekday"),
    ("qsi-tqqq", "2010-02-11", ("TQQQ",), "weekday"),
    ("qsi-tyd", "2009-04-16", ("TYD",), "weekday"),
    ("qsi-uge", "2007-02-01", ("UGE",), "weekday"),
    ("qsi-upw", "2007-02-01", ("UPW",), "weekday"),
)
# Quantbase position-closes closure check: SH, GLD and TQQQ, one group each from first bar.
QB_GROUPS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("qb-sh", "2006-06-21", ("SH",), "weekday"),
    ("qb-gld", "2004-11-18", ("GLD",), "weekday"),
    ("qb-tqqq", "2010-02-11", ("TQQQ",), "weekday"),
)
KINDS: Mapping[str, tuple[tuple[str, str, tuple[str, ...], str], ...]] = {
    "base": BASE_GROUPS,
    "style": STYLE_GROUPS,
    "sector": SECTOR_GROUPS,
    "held": HELD_GROUPS,
    "qsi": QSI_GROUPS,
    "qb": QB_GROUPS,
}
MONTHLY_PAYERS = ("BIL", "HYG", "IEF", "LQD", "TLT")
CRYPTO = ("BTC-USD", "ETH-USD")
MONTH_WINDOW_DAYS = 1095
MAX_DISTRIBUTION_GAP_DAYS = 52
LATEST_DISTRIBUTION_MAX_AGE_DAYS = 35
ROW_FLOOR = 0.95  # fraction of the calendar's expected bars a group must return
LAST_BAR_LAG_DAYS = 4  # weekend + holiday allowance for exchange-calendar tickers


class RefreshCaptureError(RuntimeError):
    """A refresh capture step failed or an acceptance rule was not met."""


def run_id(label: str, kind: str, rerun_index: int | None) -> str:
    suffix = f"-{rerun_index}" if rerun_index and rerun_index > 1 else ""
    return f"{label}-{kind.upper()}{suffix}"


def _expected_bars(start: date, cutoff: date, calendar: str) -> int:
    days = (cutoff - start).days + 1
    if calendar == "every_day":
        return days
    return sum(
        1 for offset in range(days) if (start + timedelta(days=offset)).weekday() < 5
    )


def build_config(kind: str, cutoff: date, label: str) -> dict[str, Any]:
    """The driver config document for one kind, deterministic in (kind, cutoff, label)."""

    groups: list[dict[str, Any]] = []
    for group_id, first_bar, symbols, calendar in KINDS[kind]:
        start = date.fromisoformat(first_bar)
        group: dict[str, Any] = {
            "group_id": group_id,
            "interval": "1d",
            "kind": "yfinance",
            "max_missing_weekdays": 280,
            "min_rows": int(_expected_bars(start, cutoff, calendar) * ROW_FLOOR),
            "start": first_bar,
            "symbols": list(symbols),
            "through": "cutoff",
        }
        if calendar != "weekday":
            group["calendar"] = calendar
        groups.append(group)
    return {
        "cutoff": cutoff.isoformat(),
        "groups": groups,
        "limits": {
            "batch_size": 20,
            "retry_attempts": 1,
            "retry_backoff_seconds": 2.0,
            "threads": 1,
            "timeout_seconds": 30,
        },
        "note": f"{label} {kind} Yahoo capture (raw history and history_adjusted); "
        f"cutoff {cutoff.isoformat()} is data.",
        "run_label": f"{label.lower()}-{kind}",
        "schema_version": SCHEMA_VERSION,
        "symbol_resolution": [],
    }


def config_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def config_sha256(document: Mapping[str, Any]) -> str:
    return driver.parse_config(
        config_bytes(document), cutoff_override=document["cutoff"]
    ).sha256


def comment_body(
    cutoff: date, label: str, kinds: Sequence[str], rerun_index: int | None
) -> str:
    runs = "; ".join(
        f"{run_id(label, k, rerun_index)} config_sha256 "
        f"{config_sha256(build_config(k, cutoff, label))}"
        for k in kinds
    )
    return (
        f"Record (control-directed; not an owner approval): Yahoo public price fetch under "
        f"#283, cutoff {cutoff.isoformat()}. Scope: read-only public Yahoo Finance requests "
        f"through the committed yahoo_fetch_driver fetch (file sha256 "
        f"{_driver_sha256()}), daily bars, history and history_adjusted, through the "
        f"cutoff; no login, no credentials, no account write, no refresh of any retained "
        f"dataset. Runs: {runs}; max_requests {MAX_REQUESTS}, single use each. Outputs stay "
        f"under the evidence area; nothing is published as CURRENT by this step."
    )


def _driver_sha256() -> str:
    return hashlib.sha256(Path(driver.__file__).read_bytes()).hexdigest()


# ---------------------------------------------------------------- acceptance


def _rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["rows"]
    return rows


def distribution_findings(
    dividend_dates: Sequence[str], cutoff: date, first_bar: date | None = None
) -> list[str]:
    """Month-aware distribution record of one monthly payer (empty list = sound).

    The window is the last 1095 days, or from the ticker's first bar when it listed later.
    """

    found: list[str] = []
    days = sorted(date.fromisoformat(d) for d in dividend_dates)
    if not days:
        return ["no distribution recorded"]
    if (cutoff - days[-1]).days > LATEST_DISTRIBUTION_MAX_AGE_DAYS:
        found.append(
            f"latest distribution {days[-1]} is older than {LATEST_DISTRIBUTION_MAX_AGE_DAYS} days"
        )
    window_start = cutoff - timedelta(days=MONTH_WINDOW_DAYS)
    if first_bar is not None:
        window_start = max(window_start, first_bar)
    in_window = [d for d in days if d >= window_start]
    present = {(d.year, d.month) for d in in_window}
    month = date(window_start.year, window_start.month, 1)
    # The cutoff's own month is demanded only from the 10th (ex-dates fall in the first days).
    last_month = (
        cutoff.replace(day=1)
        if cutoff.day >= 10
        else (cutoff.replace(day=1) - timedelta(days=1)).replace(day=1)
    )
    while month <= last_month:
        # January is excused (a year-end distribution can land on either side of it);
        # the first partial month of the window is not demanded either.
        if (
            month.month != 1
            and month > window_start.replace(day=1)
            and (month.year, month.month) not in present
        ):
            found.append(f"no distribution in {month.year}-{month.month:02d}")
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    for earlier, later in zip(in_window, in_window[1:]):
        if (later - earlier).days > MAX_DISTRIBUTION_GAP_DAYS:
            found.append(
                f"gap {earlier} -> {later} exceeds {MAX_DISTRIBUTION_GAP_DAYS} days"
            )
    return found


def acceptance(artifacts: Path, kind: str, cutoff: date) -> dict[str, Any]:
    """Acceptance of one staged run; ``passed`` is False with findings when not sound."""

    findings: list[str] = []
    tickers: dict[str, Any] = {}
    for group_id, first_bar, symbols, calendar in KINDS[kind]:
        raw = _rows(artifacts / f"yahoo-{group_id}-history.json")
        adjusted = _rows(artifacts / f"yahoo-{group_id}-history_adjusted.json")
        raw_keys = {(r["Ticker"], r["Date"][:10]) for r in raw}
        adjusted_keys = {(r["Ticker"], r["Date"][:10]) for r in adjusted}
        if raw_keys != adjusted_keys:
            findings.append(f"{group_id}: raw and adjusted rows cover different dates")
        floor = int(
            _expected_bars(date.fromisoformat(first_bar), cutoff, calendar) * ROW_FLOOR
        )
        for symbol in symbols:
            mine = sorted(
                (r for r in raw if r["Ticker"] == symbol), key=lambda r: r["Date"]
            )
            dates = [r["Date"][:10] for r in mine]
            if not mine:
                findings.append(f"{symbol}: no rows")
                continue
            last = date.fromisoformat(dates[-1])
            lag = (cutoff - last).days
            if (
                last > cutoff
                or (symbol in CRYPTO and lag != 0)
                or lag > LAST_BAR_LAG_DAYS
            ):
                findings.append(f"{symbol}: last bar {last} vs cutoff {cutoff}")
            if len(mine) < floor:
                findings.append(f"{symbol}: {len(mine)} rows below the floor {floor}")
            if len(set(dates)) != len(dates):
                findings.append(f"{symbol}: duplicate dates")
            if any(not r["Close"] or r["Close"] <= 0 for r in mine):
                findings.append(f"{symbol}: non-positive close")
            paid = [r["Date"][:10] for r in mine if (r.get("Dividends") or 0) > 0]
            entry: dict[str, Any] = {
                "first": dates[0],
                "last": dates[-1],
                "rows": len(mine),
                "last_distribution": paid[-1] if paid else None,
            }
            if symbol in MONTHLY_PAYERS:
                problems = distribution_findings(
                    paid, cutoff, date.fromisoformat(dates[0])
                )
                entry["distribution_findings"] = problems
                findings.extend(f"{symbol}: {p}" for p in problems)
            tickers[symbol] = entry
    return {
        "kind": kind,
        "cutoff": cutoff.isoformat(),
        "passed": not findings,
        "findings": findings,
        "tickers": tickers,
    }


# ------------------------------------------------------------------- driver


def _driver(argv: Sequence[str]) -> None:
    code = driver.main(list(argv))
    if code != 0:
        raise RefreshCaptureError(f"yahoo_fetch_driver {argv[0]} exited {code}")


def _sums(folder: Path) -> str:
    lines = [
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(folder).as_posix()}"
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.name not in {"SHA256SUMS.txt"}
    ]
    return "\n".join(lines) + "\n"


def run_kind(
    kind: str,
    cutoff: date,
    label: str,
    rerun_index: int | None,
    review: str,
    out_root: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    rid = run_id(label, kind, rerun_index)
    work = out_root / rid
    work.mkdir(parents=True, exist_ok=False)
    config_path = work / "config.json"
    config_path.write_bytes(config_bytes(build_config(kind, cutoff, label)))
    started = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    evidence = work / "governance-evidence.json"
    _driver(
        [
            "make-evidence",
            "--config",
            str(config_path),
            "--cutoff",
            cutoff.isoformat(),
            "--run-id",
            rid,
            "--review",
            review,
            "--valid-from-utc",
            (started - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "--valid-until-utc",
            (started + timedelta(hours=EVIDENCE_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "--max-requests",
            str(MAX_REQUESTS),
            "--out",
            str(evidence),
        ]
    )
    stage = work / "stage"
    stage.mkdir()
    _driver(
        [
            "fetch",
            "--config",
            str(config_path),
            "--stage-root",
            str(stage),
            "--run-id",
            rid,
            "--governance",
            str(evidence),
            "--cutoff",
            cutoff.isoformat(),
        ]
    )
    _driver(["verify", str(stage / rid)])
    result = acceptance(stage / rid / "artifacts", kind, cutoff)
    result["run_id"] = rid
    result["config_sha256"] = config_sha256(build_config(kind, cutoff, label))
    result["review"] = review
    (stage / rid / "ACCEPTANCE.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (stage / rid / "SHA256SUMS.txt").write_text(_sums(stage / rid), encoding="utf-8")
    return result


def _kinds(value: str) -> list[str]:
    kinds = [k for k in value.split(",") if k]
    if not kinds or any(k not in KINDS for k in kinds):
        raise argparse.ArgumentTypeError(
            "kinds must be a comma list of base, style, sector, held, qsi, qb"
        )
    return kinds


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run"):
        p = sub.add_parser(name)
        p.add_argument("--cutoff", required=True, type=date.fromisoformat)
        p.add_argument("--label", required=True, help="release label, e.g. R4")
        p.add_argument("--kinds", type=_kinds, default=["base", "style"])
        p.add_argument(
            "--rerun-index",
            type=int,
            default=None,
            help="2 for a re-capture of the same cutoff (R3-BASE-2)",
        )
        if name == "prepare":
            p.add_argument("--out", required=True, type=Path)
        else:
            p.add_argument("--review", required=True, help="the governance comment URL")
            p.add_argument("--out-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            args.out.mkdir(parents=True, exist_ok=True)
            for kind in args.kinds:
                (args.out / f"{kind}-config.json").write_bytes(
                    config_bytes(build_config(kind, args.cutoff, args.label))
                )
            body = comment_body(args.cutoff, args.label, args.kinds, args.rerun_index)
            (args.out / "comment.txt").write_text(body + "\n", encoding="utf-8")
            print(body)
            return 0
        results = [
            run_kind(
                k, args.cutoff, args.label, args.rerun_index, args.review, args.out_root
            )
            for k in args.kinds
        ]
        for r in results:
            print(
                f"{r['run_id']}: {'PASS' if r['passed'] else 'FAIL'}",
                *r["findings"],
                sep="\n  ",
            )
        return 0 if all(r["passed"] for r in results) else 1
    except (RefreshCaptureError, driver.DriverError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
