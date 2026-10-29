"""Fill declared vendor NAV gap days from governed alternates, labelled as such.

A vendor NAV endpoint can omit single days.  The governed admission keeps those
days as two-day holding-period spans (it never invents a daily point).  This
tool produces a separate, labelled fill record for each declared day so a
consumer can show a daily point without pretending the vendor published it:

* ``ALTERNATE_SOURCE``: the value is read from a second dated NAV file whose
  agreement with the vendor series on the overlap is measured and reported.
* ``PROVIDER_RETURN_BRIDGE``: the value is bridged from the neighbouring
  vendor NAVs with the provider-published daily return series (forward from
  the previous day and backward from the next day, averaged); the method is
  first scored on every day where both vendor NAVs exist.
* ``LINEAR_INTERPOLATION``: straight line between the neighbouring NAVs.

Every rule, label, column name, series id and tolerance is policy data
(``policy.json``); nothing about a provider or product lives in this module.
The fill never changes the two-day span return: the product of the fill's two
legs equals the span return exactly (closure check).
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Final, Mapping, Sequence

SCHEMA_VERSION: Final = 1
POLICY_SCHEMA: Final = "vendor-nav-gap-fill-policy/1"
RULES: Final = (
    "ALTERNATE_SOURCE",
    "PROVIDER_RETURN_BRIDGE",
    "LINEAR_INTERPOLATION",
)
_PRECISION: Final = 40
#: Offsets (days) searched when proving the provider return alignment.
LAG_SEARCH: Final = 2
_ALT_TIME = re.compile(r"^\s*(\d{4})\.(\d{1,2})\.(\d{1,2})\b")


class VendorGapFillError(ValueError):
    """The fill policy or its inputs cannot support a labelled fill."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


def read_policy(path: Path) -> dict[str, Any]:
    policy: dict[str, Any] = json.loads(path.read_bytes().decode("utf-8"))
    if policy.get("schema") != POLICY_SCHEMA:
        raise VendorGapFillError("fill policy schema differs")
    labels = policy.get("origin_labels")
    if not isinstance(labels, dict) or set(labels) != set(RULES):
        raise VendorGapFillError("fill policy must label every rule")
    days = policy.get("days")
    if not isinstance(days, list) or not days:
        raise VendorGapFillError("fill policy names no days")
    for item in days:
        if item.get("rule") not in RULES:
            raise VendorGapFillError(f"unknown fill rule {item.get('rule')!r}")
    return policy


def read_vendor_nav(
    path: Path, date_column: str, value_column: str
) -> dict[date, Decimal]:
    """Read ``date_column`` (ISO date or timestamp) and ``value_column`` of a vendor NAV csv."""

    result: dict[date, Decimal] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            day = datetime.fromisoformat(row[date_column]).date()
            if day in result:
                raise VendorGapFillError(f"vendor NAV repeats {day}")
            result[day] = Decimal(row[value_column])
    return result


def read_alternate_nav(
    path: Path, time_column: str, value_column: str
) -> dict[date, Decimal]:
    """Read a dated snapshot file whose time column starts ``YYYY.M.D``."""

    result: dict[date, Decimal] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            match = _ALT_TIME.match(row[time_column])
            if match is None:
                raise VendorGapFillError(f"alternate NAV time {row[time_column]!r}")
            day = date(int(match[1]), int(match[2]), int(match[3]))
            if day in result:
                raise VendorGapFillError(f"alternate NAV repeats {day}")
            result[day] = Decimal(row[value_column])
    return result


def read_provider_returns(path: Path, series_id: str) -> dict[date, Decimal]:
    result: dict[date, Decimal] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["series_id"] == series_id:
                result[date.fromisoformat(row["date"])] = Decimal(row["return"])
    if not result:
        raise VendorGapFillError(f"provider series {series_id!r} has no rows")
    return result


def bridge_score(
    nav: Mapping[date, Decimal], provider: Mapping[date, Decimal], lag_days: int
) -> dict[str, Any]:
    """Score ``NAV(d+1)/NAV(d) - 1 == provider(d - lag)`` on every adjacent vendor pair."""

    errors: list[Decimal] = []
    for day, value in nav.items():
        nxt = day + timedelta(days=1)
        ret = provider.get(day - timedelta(days=lag_days))
        if nxt in nav and ret is not None:
            errors.append(abs(nav[nxt] / value - Decimal(1) - ret))
    if not errors:
        raise VendorGapFillError("bridge has no scoring pairs")
    errors.sort()
    return {
        "pairs": len(errors),
        "mean_abs_daily_error": _fmt(sum(errors, Decimal(0)) / len(errors)),
        "median_abs_daily_error": _fmt(errors[len(errors) // 2]),
        "max_abs_daily_error": _fmt(errors[-1]),
    }


def _bridge_value(
    day: date,
    nav: Mapping[date, Decimal],
    provider: Mapping[date, Decimal],
    lag_days: int,
) -> tuple[Decimal, Decimal, Decimal]:
    prev, nxt = day - timedelta(days=1), day + timedelta(days=1)
    r_prev = provider.get(prev - timedelta(days=lag_days))
    r_day = provider.get(day - timedelta(days=lag_days))
    if prev not in nav or nxt not in nav or r_prev is None or r_day is None:
        raise VendorGapFillError(f"bridge lacks neighbours for {day}")
    forward = nav[prev] * (Decimal(1) + r_prev)
    backward = nav[nxt] / (Decimal(1) + r_day)
    return (forward + backward) / 2, forward, backward


def _require_reconciled(policy: Mapping[str, Any], overlap: Mapping[str, Any]) -> None:
    """An alternate source fills a day only if it reproduces the vendor series."""

    tolerance = policy.get("alternate_tolerance")
    minimum = policy.get("alternate_min_overlap_days")
    if tolerance is None or minimum is None:
        raise VendorGapFillError(
            "policy must declare the alternate tolerance and overlap"
        )
    if overlap["days"] < int(minimum):
        raise VendorGapFillError("alternate source overlap is too short to reconcile")
    if Decimal(overlap["max_abs_nav_difference"]) > Decimal(str(tolerance)):
        raise VendorGapFillError("alternate source disagrees with the vendor series")


def build_fills(
    policy: Mapping[str, Any],
    nav: Mapping[date, Decimal],
    alternate: Mapping[date, Decimal] | None,
    provider: Mapping[date, Decimal] | None,
) -> dict[str, Any]:
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        lag = int(policy.get("provider_return_lag_days", 0))
        report: dict[str, Any] = {}
        if alternate is not None:
            both = sorted(set(alternate) & set(nav))
            diffs = [abs(alternate[d] - nav[d]) for d in both]
            report["alternate_overlap"] = {
                "days": len(both),
                "first": both[0].isoformat() if both else None,
                "last": both[-1].isoformat() if both else None,
                "max_abs_nav_difference": _fmt(max(diffs)) if diffs else None,
            }
            report["alternate_overlap"]["tolerance"] = policy.get("alternate_tolerance")
            report["alternate_overlap"]["minimum_days"] = policy.get(
                "alternate_min_overlap_days"
            )
        if provider is not None:
            report["provider_bridge_score"] = bridge_score(nav, provider, lag)
            candidates = {}
            for offset in range(-LAG_SEARCH, LAG_SEARCH + 1):
                try:
                    candidates[offset] = bridge_score(nav, provider, offset)
                except VendorGapFillError:
                    continue
            medians = {
                offset: Decimal(score["median_abs_daily_error"])
                for offset, score in candidates.items()
            }
            report["provider_lag_median_errors"] = {
                str(offset): _fmt(value) for offset, value in sorted(medians.items())
            }
            if min(medians, key=lambda offset: medians[offset]) != lag:
                raise VendorGapFillError(
                    "declared provider return lag is not the best-scoring alignment"
                )
        fills = []
        for item in policy["days"]:
            day = date.fromisoformat(item["date"])
            if day in nav:
                raise VendorGapFillError(
                    f"{day} is published by the vendor; nothing to fill"
                )
            prev, nxt = day - timedelta(days=1), day + timedelta(days=1)
            if prev not in nav or nxt not in nav:
                raise VendorGapFillError(f"{day} lacks adjacent vendor NAVs")
            linear = (nav[prev] + nav[nxt]) / 2
            detail: dict[str, Any] = {"linear_interpolation_nav": _fmt(linear)}
            rule = item["rule"]
            if rule == "ALTERNATE_SOURCE":
                if alternate is None or day not in alternate:
                    raise VendorGapFillError(f"alternate source lacks {day}")
                _require_reconciled(policy, report["alternate_overlap"])
                value = alternate[day]
            elif rule == "PROVIDER_RETURN_BRIDGE":
                if provider is None:
                    raise VendorGapFillError("bridge rule needs the provider returns")
                value, forward, backward = _bridge_value(day, nav, provider, lag)
                detail["forward_bridge_nav"] = _fmt(forward)
                detail["backward_bridge_nav"] = _fmt(backward)
            else:
                value = linear
            leg_in = value / nav[prev] - Decimal(1)
            leg_out = nav[nxt] / value - Decimal(1)
            span = nav[nxt] / nav[prev] - Decimal(1)
            closure = (Decimal(1) + leg_in) * (Decimal(1) + leg_out) - Decimal(1) - span
            fills.append(
                {
                    "date": day.isoformat(),
                    "nav": _fmt(value),
                    "rule": rule,
                    "origin_label": policy["origin_labels"][rule],
                    "previous_nav": {"date": prev.isoformat(), "nav": _fmt(nav[prev])},
                    "next_nav": {"date": nxt.isoformat(), "nav": _fmt(nav[nxt])},
                    "leg_in_return": _fmt(leg_in),
                    "leg_out_return": _fmt(leg_out),
                    "span_return": _fmt(span),
                    "span_closure_residual": _fmt(closure),
                    "linear_interpolation_return_in": _fmt(
                        linear / nav[prev] - Decimal(1)
                    ),
                    **detail,
                }
            )
    return {"schema_version": SCHEMA_VERSION, "fills": fills, **report}


def build_record(
    policy_path: Path,
    nav_path: Path,
    alternate_path: Path | None,
    provider_path: Path | None,
) -> dict[str, Any]:
    policy = read_policy(policy_path)
    cols = policy["vendor_nav_columns"]
    nav = read_vendor_nav(nav_path, cols["date"], cols["value"])
    alternate = None
    if alternate_path is not None:
        acols = policy["alternate_columns"]
        alternate = read_alternate_nav(alternate_path, acols["time"], acols["value"])
    provider = None
    if provider_path is not None:
        provider = read_provider_returns(provider_path, policy["provider_series_id"])
    record = build_fills(policy, nav, alternate, provider)
    record["inputs"] = {
        "policy_sha256": _sha256(policy_path),
        "vendor_nav_sha256": _sha256(nav_path),
        "alternate_nav_sha256": _sha256(alternate_path) if alternate_path else None,
        "provider_returns_sha256": _sha256(provider_path) if provider_path else None,
    }
    record["subject"] = policy.get("subject")
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--vendor-nav", type=Path, required=True)
    parser.add_argument("--alternate-nav", type=Path)
    parser.add_argument("--provider-returns", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    record = build_record(
        args.policy, args.vendor_nav, args.alternate_nav, args.provider_returns
    )
    args.out.write_bytes(
        (json.dumps(record, sort_keys=True, indent=2) + "\n").encode("utf-8")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
