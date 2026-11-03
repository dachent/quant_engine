"""Sign-convention check and crosswalk authoring for the Alpaca ingest.

``check_signs`` runs after a real capture is ingested and BEFORE any total from
the package is used.  The sign convention of reported ``net_amount`` for
journals, fees, transfers, dividends, and interest is UNVERIFIED against a live
response, and a flipped type would silently corrupt the cash closure.  The check
compares the activities against an independent balance movement:

    delta = closing cash - opening cash          (supplied by the caller)
    S     = sum of every cash amount in the package, signs as reported
    R0    = delta - S                            (0 when the reported signs hold)

For each activity type T with total ``s_T`` it also computes the residual if
that one type's sign were the opposite: ``R1 = delta - (S - 2 * s_T)``.  A type
FAILS when flipping it fits the balance movement strictly better than the
reported sign (``|R1| < |R0|``); it PASSES when the reported sign fits at least
as well; it is INCONCLUSIVE when its total is within tolerance of zero.  Fills
carry a derived cash leg (quantity times price, with the side as the sign), so
they are reported as DERIVED and are never flipped.  A global inversion of every
non-fill type is tested as well.

The totals are blessed only if no type fails, no global inversion fits better,
and the reported totals reproduce the balance movement within the tolerance.
Output is counts and sums only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any

from quant_engine.alpaca.capture.errors import CaptureError, CaptureRefused
from quant_engine.alpaca.native_activities import (
    NativeActivitiesError,
    NativePackage,
    canonical_bytes,
    load_native_package,
    sha256_hex,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_ID = re.compile(r"^account-[0-9a-f]{16}$")
_DECIMAL = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
DERIVED_TYPES = frozenset({"FILL"})


class SignCheckRefused(CaptureRefused):
    """The sign check could not run (bad input or an unreadable package)."""


def _refuse(reason: str) -> SignCheckRefused:
    return SignCheckRefused(f"sign check refused: {reason}")


def parse_amount(value: str, field: str) -> Decimal:
    if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
        raise _refuse(f"{field} is not a plain decimal")
    return Decimal(value)


@dataclass(frozen=True)
class TypeVerdict:
    activity_type: str
    verdict: str
    count: int
    positive: int
    negative: int
    total: Decimal
    residual_if_flipped: Decimal | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity_type": self.activity_type,
            "count": self.count,
            "negative": self.negative,
            "positive": self.positive,
            "residual_if_flipped": (
                None
                if self.residual_if_flipped is None
                else format(self.residual_if_flipped, "f")
            ),
            "total": format(self.total, "f"),
            "verdict": self.verdict,
        }


@dataclass(frozen=True)
class SignReport:
    binding_sha256: str
    window: dict[str, str]
    opening_cash: Decimal
    closing_cash: Decimal
    closing_basis: str
    tolerance: Decimal
    activity_total: Decimal
    balance_movement: Decimal
    residual_as_reported: Decimal
    residual_all_flipped: Decimal
    types: tuple[TypeVerdict, ...]
    totals_reconcile: bool
    global_inversion_fits_better: bool
    blessed: bool

    def to_dict(self) -> dict[str, Any]:
        def text(value: Decimal) -> str:
            return format(value, "f")

        return {
            "balance_movement": text(self.balance_movement),
            "binding_sha256": self.binding_sha256,
            "blessed": self.blessed,
            "closing_basis": self.closing_basis,
            "closing_cash": text(self.closing_cash),
            "global_inversion_fits_better": self.global_inversion_fits_better,
            "opening_cash": text(self.opening_cash),
            "overall": "PASS" if self.blessed else "FAIL",
            "residual_all_flipped": text(self.residual_all_flipped),
            "residual_as_reported": text(self.residual_as_reported),
            "tolerance": text(self.tolerance),
            "total_of_activities": text(self.activity_total),
            "totals_reconcile": self.totals_reconcile,
            "types": [item.to_dict() for item in self.types],
            "window": self.window,
        }


def check_signs(
    package: NativePackage,
    *,
    opening_cash: Decimal,
    closing_cash: Decimal,
    closing_basis: str,
    tolerance: Decimal = Decimal(0),
) -> SignReport:
    if tolerance < 0:
        raise _refuse("the tolerance is negative")
    by_type: Mapping[str, Mapping[str, Any]] = package.summary["by_type"]
    total = Decimal(str(package.summary["net_cash_amount"]))
    delta = closing_cash - opening_cash
    residual = delta - total
    derived_total = sum(
        (
            Decimal(str(by_type[name]["cash_amount"]))
            for name in DERIVED_TYPES
            if name in by_type
        ),
        Decimal(0),
    )
    # Every non-fill type inverted: S' = F - (S - F).
    all_flipped = delta - (derived_total - (total - derived_total))
    verdicts: list[TypeVerdict] = []
    for name, data in sorted(by_type.items()):
        amount = Decimal(str(data["cash_amount"]))
        counts = (int(data["count"]), int(data["positive"]), int(data["negative"]))
        if name in DERIVED_TYPES:
            verdicts.append(TypeVerdict(name, "DERIVED", *counts, amount, None))
            continue
        flipped = delta - (total - 2 * amount)
        if abs(amount) <= tolerance:
            verdict = "INCONCLUSIVE"
        elif abs(flipped) < abs(residual):
            verdict = "FAIL"
        else:
            verdict = "PASS"
        verdicts.append(TypeVerdict(name, verdict, *counts, amount, flipped))
    reconciles = abs(residual) <= tolerance
    inversion = abs(all_flipped) < abs(residual)
    blessed = (
        reconciles
        and not inversion
        and not any(item.verdict == "FAIL" for item in verdicts)
    )
    return SignReport(
        binding_sha256=package.binding_sha256,
        window={"after": package.after, "until": package.until},
        opening_cash=opening_cash,
        closing_cash=closing_cash,
        closing_basis=closing_basis,
        tolerance=tolerance,
        activity_total=total,
        balance_movement=delta,
        residual_as_reported=residual,
        residual_all_flipped=all_flipped,
        types=tuple(verdicts),
        totals_reconcile=reconciles,
        global_inversion_fits_better=inversion,
        blessed=blessed,
    )


def run_sign_check(
    *,
    package_directory: str | Path,
    opening_cash: str,
    closing_cash: str | None,
    closing_from_snapshot: bool,
    tolerance: str = "0",
    out: str | Path | None = None,
) -> SignReport:
    """Load the package, run the check, and optionally write the report create-only."""

    if (closing_cash is None) == (not closing_from_snapshot):
        raise _refuse("give exactly one of --closing-cash or --closing-from-snapshot")
    try:
        package = load_native_package(package_directory)
    except NativeActivitiesError as error:
        raise _refuse(str(error)) from None
    if closing_cash is not None:
        closing = parse_amount(closing_cash, "closing cash")
        basis = "supplied"
    else:
        closing = parse_amount(str(package.account["cash"]), "snapshot cash")
        basis = "account_snapshot_cash"
    report = check_signs(
        package,
        opening_cash=parse_amount(opening_cash, "opening cash"),
        closing_cash=closing,
        closing_basis=basis,
        tolerance=parse_amount(tolerance, "tolerance"),
    )
    if out is not None:
        _write_new(Path(out), canonical_bytes(report.to_dict()))
    return report


def _write_new(path: Path, data: bytes) -> None:
    if not path.is_absolute() or not path.parent.is_dir():
        raise _refuse("the output path must be absolute in an existing directory")
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        raise _refuse("output is create-only: the file exists") from None
    except OSError:
        raise CaptureError("an output file could not be written") from None


def write_crosswalk(
    *, plan_digest: str, internal_account_id: str, out: str | Path
) -> str:
    """Write the one-row crosswalk the ingest consumes; return its SHA-256.

    ``plan_digest`` is the ``account_binding_sha256`` the driver's ``plan``
    prints.  The file is generated, never hand-written, and create-only.
    """

    if not isinstance(plan_digest, str) or _SHA256.fullmatch(plan_digest) is None:
        raise _refuse("the plan digest is not a lowercase SHA-256")
    if _ACCOUNT_ID.fullmatch(internal_account_id) is None:
        raise _refuse("the internal account id is invalid")
    document = {
        "provider": "ALPACA",
        "rows": [
            {
                "account_binding_sha256": plan_digest,
                "internal_account_id": internal_account_id,
            }
        ],
        "schema_version": 1,
    }
    data = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _write_new(Path(out), data)
    return sha256_hex(data)


__all__ = [
    "SignCheckRefused",
    "SignReport",
    "TypeVerdict",
    "check_signs",
    "parse_amount",
    "run_sign_check",
    "write_crosswalk",
]
