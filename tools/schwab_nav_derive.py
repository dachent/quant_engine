"""Derive the Schwab cutoff-day NAV from the verified holdings and the official closes.

The native ``balances.historical`` response can carry a row for the cutoff day D that is a stale carry
of the day before (R2.1: every non-flat account, captured after the New York close). The value this
tool derives is the one the provider ledger replays with no tolerance
(``provider_ledger._replay_schwab_patched_navs``): the sum of quantity x official close, rounded to
cents (half even), plus the cash rows. It reads a verified driver run directory and writes the
derivation record (the ``official-nav-derivation.json`` shape: per account ``official_<MMDD>``,
``official_<prior MMDD>``, ``nav_file_<prior MMDD>`` and ``cash``) create-only, and refuses on any
closure failure:

* every account of the crosswalk is in the holdings capture, and every held symbol has an official
  close for D and for the day before (a missing close is a refusal, never a skipped symbol);
* the official value of the day before equals the captured NAV of that day within ``--tolerance``;
* a flat (cash-only) account carries no patch: its native D and prior rows must both equal its cash;
* a non-flat account whose native D equals its native prior row is a stale carry and is PATCHED; one
  whose native D differs must agree with the derived value within the tolerance, else it refuses.

python tools/schwab_nav_derive.py derive --run-dir R --crosswalk C --closes-dir D --day 2026-11-06 \\
    --prior-day 2026-11-05 --out derivation.json [--report report.json] [--tolerance 0.05]

The ingest then writes the derived cutoff row into the NAV CSVs (``--derivation-record``), and ``declare``
writes the source-branch finality evidence (the composed spec's roots and NAV sources plus the pinned
``derived_patch`` that ``provider_ledger`` replays with no tolerance), create-only; it re-derives the
record from the run and refuses when the file on disk differs:

python tools/schwab_nav_derive.py declare --run-dir R --crosswalk C --closes-dir D --day 2026-11-06 \\
    --prior-day 2026-11-05 --derivation derivation.json --spec composed-spec.json --out finality.json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
import io
import json
from pathlib import Path
import sys
from typing import Any

from quant_engine.core import check_entry_interpreter, strict_json_loads

import schwab_capture_driver as driver

CASH_SECURITY_TYPE = 9
DEFAULT_TOLERANCE = Decimal("0.05")
STATUS_FLAT = "FLAT_NO_PATCH"
STATUS_PATCH = "STALE_CARRY_PATCHED"
STATUS_AGREES = "NATIVE_AGREES_WITH_OFFICIAL"
# the ledger replay's rounding label (provider_ledger._SCHWAB_REPLAY_ROUNDING); a test pins the two together
REPLAY_ROUNDING = "SUM_QUANTITY_X_CLOSE_QUANTIZE_0.01_HALF_EVEN_PLUS_CASH"


class DeriveError(ValueError):
    """The derivation cannot be closed against the captured evidence."""


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise DeriveError(f"{name} is not a number")
    try:
        return Decimal(str(value))
    except ArithmeticError as exc:
        raise DeriveError(f"{name} is not a number") from exc


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise DeriveError(f"{name} is not an object")
    return value


def _list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise DeriveError(f"{name} is not a list")
    return value


def read_holdings(
    raw: object, crosswalk: Mapping[str, str]
) -> dict[str, tuple[dict[str, Decimal], Decimal]]:
    """Internal account id to (quantity by symbol, cash), exactly as the ledger replay reads it."""

    held: dict[str, tuple[dict[str, Decimal], Decimal]] = {}
    for account in _list(
        _mapping(raw, "holdings").get("accounts"), "holdings accounts"
    ):
        external = _mapping(account, "holdings account").get("accountId")
        internal = crosswalk.get(str(external))
        if internal is None or internal in held:
            raise DeriveError("holdings account is outside the crosswalk or repeated")
        quantities: dict[str, Decimal] = {}
        cash = Decimal(0)
        for group in _list(account.get("groupedPositions"), "holdings groups"):
            for row in _list(
                _mapping(group, "holdings group").get("holdingsRows"), "holdings rows"
            ):
                if group.get("securityType") == CASH_SECURITY_TYPE:
                    cash += _decimal(
                        _mapping(row.get("marketValue"), "market value").get("val"),
                        "cash value",
                    )
                else:
                    # the frozen holdings reader prefers dataSymbol over the nested symbol
                    symbol = row.get("dataSymbol")
                    if not isinstance(symbol, str) or not symbol.strip():
                        symbol = _mapping(row.get("symbol"), "symbol").get("symbol")
                    if not isinstance(symbol, str) or not symbol:
                        raise DeriveError("holdings row has no symbol")
                    quantities[symbol] = quantities.get(symbol, Decimal(0)) + _decimal(
                        _mapping(row.get("qty"), "quantity").get("qty"), "quantity"
                    )
        held[internal] = (quantities, cash)
    missing = sorted(set(crosswalk.values()) - set(held))
    if missing:
        raise DeriveError(f"holdings capture lacks accounts: {','.join(missing)}")
    return held


def close_file(closes_dir: Path, symbol: str) -> Path:
    """The Yahoo close file of a held symbol: the Schwab spelling with '/' written as '.'
    (BRK/B -> BRK.B_yahoo_daily.csv), as the Yahoo driver names it."""

    return closes_dir / f"{symbol.replace('/', '.')}_yahoo_daily.csv"


def read_closes(closes_dir: Path, symbol: str) -> dict[date, Decimal]:
    path = close_file(closes_dir, symbol)
    if not path.is_file():
        raise DeriveError(f"no official close file for {symbol}")
    rows = csv.DictReader(io.StringIO(path.read_bytes().decode("utf-8-sig")))
    return {
        date.fromisoformat(row["date"]): _decimal(row["close"], f"{symbol} close")
        for row in rows
    }


def official_value(
    quantities: Mapping[str, Decimal],
    cash: Decimal,
    closes: Mapping[str, Mapping[date, Decimal]],
    day: date,
    account: str,
) -> Decimal:
    total = Decimal(0)
    for symbol, quantity in sorted(quantities.items()):
        close = closes[symbol].get(day)
        if close is None:
            raise DeriveError(f"{account}: {symbol} has no official close for {day}")
        total += quantity * close
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN) + cash


def derive(
    *,
    run_dir: Path,
    crosswalk: Mapping[str, str],
    closes_dir: Path,
    day: date,
    prior_day: date,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> tuple[dict[str, dict[str, str]], list[dict[str, str]]]:
    """The derivation record and the per-account closure report."""

    if prior_day >= day:
        raise DeriveError("the prior day must precede the day")
    held = read_holdings(
        strict_json_loads(
            (run_dir / "raw/holdings.v2.json").read_bytes(), label="raw holdings"
        ),
        crosswalk,
    )
    symbols = sorted({s for quantities, _ in held.values() for s in quantities})
    closes = {symbol: read_closes(closes_dir, symbol) for symbol in symbols}
    day_tag, prior_tag = f"{day:%m%d}", f"{prior_day:%m%d}"
    record: dict[str, dict[str, str]] = {}
    report: list[dict[str, str]] = []
    for account, (quantities, cash) in held.items():
        history = driver.parse_balance_history(
            strict_json_loads(
                (run_dir / f"raw/balances.historical/{account}.json").read_bytes(),
                label="raw balances",
            )
        )
        if day not in history or prior_day not in history:
            raise DeriveError(
                f"{account}: the native history lacks {prior_day} or {day}"
            )
        native_day, native_prior = history[day], history[prior_day]
        official_day = official_value(quantities, cash, closes, day, account)
        official_prior = official_value(quantities, cash, closes, prior_day, account)
        if abs(official_prior - native_prior) > tolerance:
            raise DeriveError(
                f"{account}: the official value {official_prior} of {prior_day} differs from the "
                f"captured NAV {native_prior} by more than {tolerance}"
            )
        if not quantities:
            if not (native_day == native_prior == cash):
                raise DeriveError(
                    f"{account}: a flat account's native NAV of {prior_day} and {day} "
                    f"({native_prior}, {native_day}) must both equal its cash {cash}"
                )
            status = STATUS_FLAT
        elif native_day == native_prior and official_day != native_day:
            status = STATUS_PATCH
        elif abs(official_day - native_day) <= tolerance:
            status = STATUS_AGREES
        else:
            raise DeriveError(
                f"{account}: the native NAV {native_day} of {day} differs from the official value "
                f"{official_day} by more than {tolerance} and is not a carry of {prior_day}"
            )
        record[account] = {
            f"official_{day_tag}": str(official_day),
            f"official_{prior_tag}": str(official_prior),
            f"nav_file_{prior_tag}": str(native_prior),
            "cash": str(cash),
        }
        report.append(
            {
                "account_id": account,
                "native_day": str(native_day),
                "official_day": str(official_day),
                "status": status,
            }
        )
    return record, report


def _create_only(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)


def _pin(path: Path) -> dict[str, Any]:
    path = path.resolve()
    data = path.read_bytes()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def declare(
    *,
    run_dir: Path,
    crosswalk: Mapping[str, str],
    closes_dir: Path,
    day: date,
    prior_day: date,
    tolerance: Decimal,
    derivation: Path,
    spec: Path,
) -> dict[str, Any]:
    """The source-branch finality evidence: the spec's roots and NAV sources plus the derived patch.

    The record on disk must equal the one re-derived from the run, so the pinned file is never
    trusted over the captured evidence it came from.
    """

    record, report = derive(
        run_dir=run_dir,
        crosswalk=crosswalk,
        closes_dir=closes_dir,
        day=day,
        prior_day=prior_day,
        tolerance=tolerance,
    )
    if strict_json_loads(derivation.read_bytes(), label="derivation record") != record:
        raise DeriveError(
            "the derivation record differs from the one derived from the run"
        )
    spec_doc = _mapping(
        strict_json_loads(spec.read_bytes(), label="generation spec"), "generation spec"
    )
    patched = [row for row in report if row["status"] == STATUS_PATCH]
    held = read_holdings(
        strict_json_loads(
            (run_dir / "raw/holdings.v2.json").read_bytes(), label="raw holdings"
        ),
        crosswalk,
    )
    symbols = sorted({s for row in patched for s in held[row["account_id"]][0]})
    return {
        "roots": spec_doc["roots"],
        "sources": [
            item
            for item in _list(spec_doc["sources"], "spec sources")
            if _mapping(item, "spec source").get("role") == "WCH_NAV_CSV"
        ],
        "derived_patch": {
            "schema": "schwab-nav-source-derived-patch/1",
            "derivation_basis": "DERIVED_FROM_OFFICIAL_CLOSES",
            "patched_dates": [day.isoformat()],
            "derivation_record": _pin(derivation),
            "replay": {
                "positions_capture": _pin(run_dir / "raw/holdings.v2.json"),
                "closes": [
                    {"symbol": symbol, **_pin(close_file(closes_dir, symbol))}
                    for symbol in symbols
                ],
                "cash_security_type": CASH_SECURITY_TYPE,
                "rounding": REPLAY_ROUNDING,
            },
            "native_balances": [
                {
                    "account_id": row["account_id"],
                    **_pin(
                        run_dir / f"raw/balances.historical/{row['account_id']}.json"
                    ),
                }
                for row in patched
            ],
            "patches": [
                {
                    "account_id": row["account_id"],
                    "original_values": {day.isoformat(): row["native_day"]},
                    "patched_values": {day.isoformat(): row["official_day"]},
                }
                for row in patched
            ],
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("derive", "declare"):
        run = commands.add_parser(name)
        run.add_argument("--run-dir", type=Path, required=True)
        run.add_argument("--crosswalk", type=Path, required=True)
        run.add_argument("--closes-dir", type=Path, required=True)
        run.add_argument("--day", required=True)
        run.add_argument("--prior-day", required=True)
        run.add_argument("--tolerance", default=str(DEFAULT_TOLERANCE))
        run.add_argument("--out", type=Path, required=True)
        if name == "derive":
            run.add_argument("--report", type=Path)
        else:
            run.add_argument("--derivation", type=Path, required=True)
            run.add_argument("--spec", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    refused = check_entry_interpreter("the Schwab NAV derivation", "schwab-nav-derive")
    if refused is not None:
        return refused
    report: list[dict[str, str]] = []
    try:
        common: dict[str, Any] = {
            "run_dir": args.run_dir,
            "crosswalk": driver.parse_crosswalk(
                strict_json_loads(args.crosswalk.read_bytes(), label="crosswalk")
            ),
            "closes_dir": args.closes_dir,
            "day": date.fromisoformat(args.day),
            "prior_day": date.fromisoformat(args.prior_day),
            "tolerance": _decimal(args.tolerance, "tolerance"),
        }
        if args.command == "declare":
            evidence = declare(**common, derivation=args.derivation, spec=args.spec)
            _create_only(
                args.out,
                (json.dumps(evidence, indent=1, sort_keys=True) + "\n").encode("utf-8"),
            )
            return 0
        record, report = derive(**common)
        _create_only(args.out, (json.dumps(record, indent=1) + "\n").encode("utf-8"))
        if args.report is not None:
            _create_only(
                args.report, (json.dumps(report, indent=1) + "\n").encode("utf-8")
            )
    except (DeriveError, driver.DriverError, OSError, ValueError) as exc:
        print(f"schwab-nav-derive: FAIL: {exc}", file=sys.stderr)
        return 1
    for row in report:
        print(
            f"{row['account_id']}: {row['status']} native {row['native_day']} official {row['official_day']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
