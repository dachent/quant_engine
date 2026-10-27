"""Compare the two independent sleeve derivations, and either with the manifest.

``compare_derivations`` reads two derivation documents (``acceptance/derive_a`` and
``acceptance/derive_b`` written with ``--sleeve-inputs``) and lists every difference:
the sleeve metrics, and the epochs' own series (NAV by day, flow legs) from the
``sleeve_derivation`` block.  ``compare_with_manifest`` compares one derivation with the
account manifest's epoch rows.  Neither function reads a production output other than
the manifest rows it is given, and neither tolerates a missing side: an epoch that only
one side has is a difference.

Run as a script, ``python -m acceptance.validate.sleeve_compare A.json B.json
[--manifest PANEL.json]`` exits 0 only when there is no difference.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .checks import (
    DERIVED_EPOCH_FIELDS,
    DERIVED_EPOCH_METRICS,
    DERIVED_UNIVERSE_METRICS,
    OPEN_EPOCH,
    _equal,
    metric_unit,
    to_decimal,
)

MONEY_TOLERANCE = Decimal("0.01")
RATIO_TOLERANCE = Decimal("0.000001")
# Metric ids a derived account carries besides the epoch and universe rows.
_RETURN_METRICS = frozenset(
    {"twr.account", "mwr.account", "flows.account.net", "flows.account.count"}
)


@dataclass(frozen=True)
class Difference:
    """One disagreement: where it is, what each side says."""

    where: str
    left: Any
    right: Any
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "where": self.where,
            "left": self.left,
            "right": self.right,
            "note": self.note,
        }


def _block(document: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    block = document.get("sleeve_derivation")
    if not isinstance(block, Mapping):
        raise ValueError(f"{label}: no sleeve_derivation block")
    return block


def derived_accounts(document: Mapping[str, Any]) -> frozenset[str]:
    """The derived epoch accounts a derivation declares."""

    return frozenset(
        str(row["account_id"]) for row in _block(document, "document")["epochs"]
    )


def _sleeve_metrics(
    document: Mapping[str, Any], accounts: frozenset[str]
) -> dict[tuple[str, str], Mapping[str, Any]]:
    out: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in document.get("metrics", []):
        metric = str(row.get("metric_id"))
        entity = str(row.get("entity"))
        if (
            metric in DERIVED_EPOCH_METRICS
            or metric in DERIVED_UNIVERSE_METRICS
            or (metric in _RETURN_METRICS and entity in accounts)
        ):
            key = (metric, entity)
            if key in out:
                raise ValueError(f"duplicate metric row {metric} {entity}")
            out[key] = row
    return out


def _unit_tolerance(row: Mapping[str, Any]) -> tuple[str, Decimal | None]:
    metric = str(row.get("metric_id"))
    unit = metric_unit(metric, row.get("unit"))
    declared = to_decimal(row.get("tolerance"))
    if declared is not None:
        return unit, declared
    if unit == "USD":
        return unit, MONEY_TOLERANCE
    if unit == "ratio":
        return unit, RATIO_TOLERANCE
    return unit, None


def _amount(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not a decimal amount: {value!r}") from exc


def _leg_key(leg: Mapping[str, Any]) -> tuple[str, str, str, Decimal]:
    return (
        str(leg["recognition_day"]),
        str(leg["event_day"]),
        str(leg["class"]),
        _amount(leg["amount"]),
    )


def _legs_agree(
    left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]
) -> bool:
    """The same legs, each amount within the money tolerance (order ignored)."""

    if len(left) != len(right):
        return False
    for a, b in zip(
        sorted(map(_leg_key, left)), sorted(map(_leg_key, right)), strict=True
    ):
        if a[:3] != b[:3] or abs(a[3] - b[3]) > MONEY_TOLERANCE:
            return False
    return True


def compare_derivations(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> list[Difference]:
    """Every difference between two derivations of the sleeve epochs."""

    differences: list[Difference] = []
    left_block, right_block = _block(left, "A"), _block(right, "B")
    left_epochs = {str(row["account_id"]): row for row in left_block["epochs"]}
    right_epochs = {str(row["account_id"]): row for row in right_block["epochs"]}
    for account in sorted(set(left_epochs) | set(right_epochs)):
        a, b = left_epochs.get(account), right_epochs.get(account)
        if a is None or b is None:
            differences.append(
                Difference(
                    f"epoch {account}",
                    "present" if a else "absent",
                    "present" if b else "absent",
                    "only one derivation declares this epoch",
                )
            )
            continue
        for key in ("strategy_id", "effective_start", "effective_end"):
            if a.get(key) != b.get(key):
                differences.append(
                    Difference(f"epoch {account}.{key}", a.get(key), b.get(key))
                )
        for key in ("opening", "net_capital", "closing"):
            if abs(_amount(a[key]) - _amount(b[key])) > MONEY_TOLERANCE:
                differences.append(Difference(f"epoch {account}.{key}", a[key], b[key]))
        nav_a, nav_b = a["nav"], b["nav"]
        for day in sorted(set(nav_a) | set(nav_b)):
            if day not in nav_a or day not in nav_b:
                differences.append(
                    Difference(
                        f"epoch {account}.nav[{day}]",
                        nav_a.get(day),
                        nav_b.get(day),
                        "one derivation has no NAV on this day",
                    )
                )
            elif abs(_amount(nav_a[day]) - _amount(nav_b[day])) > MONEY_TOLERANCE:
                differences.append(
                    Difference(f"epoch {account}.nav[{day}]", nav_a[day], nav_b[day])
                )
        if not _legs_agree(a["legs"], b["legs"]):
            differences.append(
                Difference(
                    f"epoch {account}.legs",
                    [[*map(str, _leg_key(leg))] for leg in a["legs"]],
                    [[*map(str, _leg_key(leg))] for leg in b["legs"]],
                    "the flow legs differ",
                )
            )
    accounts = frozenset(left_epochs) | frozenset(right_epochs)
    metrics_a = _sleeve_metrics(left, accounts)
    metrics_b = _sleeve_metrics(right, accounts)
    for metric, entity in sorted(set(metrics_a) | set(metrics_b)):
        row_a, row_b = metrics_a.get((metric, entity)), metrics_b.get((metric, entity))
        where = f"metric {metric} {entity}"
        if row_a is None or row_b is None:
            differences.append(
                Difference(
                    where,
                    None if row_a is None else row_a.get("value"),
                    None if row_b is None else row_b.get("value"),
                    "only one derivation emits this metric",
                )
            )
            continue
        unit, tolerance = _unit_tolerance(row_a)
        if not _equal(unit, row_b.get("value"), row_a.get("value"), tolerance):
            differences.append(
                Difference(where, row_a.get("value"), row_b.get("value"))
            )
    return differences


def compare_with_manifest(
    document: Mapping[str, Any], manifest_rows: Sequence[Mapping[str, Any]]
) -> list[Difference]:
    """Every difference between one derivation and the manifest's epoch rows.

    ``manifest_rows`` are rows of the account manifest (the panel's
    ``account_manifest.rows``, or the manifest generation's rows): ``account_id``,
    ``strategy_id``, ``effective_start``, ``effective_end``, ``opening_capital``,
    ``net_capital``, ``current_value``.  A derived account with no row, a row of a
    derived account the derivation does not declare, and a residual parent that still
    carries a row are all differences.
    """

    differences: list[Difference] = []
    block = _block(document, "derivation")
    epochs = {str(row["account_id"]): row for row in block["epochs"]}
    parents = {account.rsplit("-", 1)[0] for account in epochs}
    by_account: dict[str, list[Mapping[str, Any]]] = {}
    for row in manifest_rows:
        by_account.setdefault(str(row.get("account_id")), []).append(row)
    for account, epoch in sorted(epochs.items()):
        found = by_account.get(account, [])
        if len(found) != 1:
            differences.append(
                Difference(
                    f"manifest {account}",
                    "one epoch row",
                    f"{len(found)} rows",
                    "a derived epoch must have exactly one manifest row",
                )
            )
            continue
        row = found[0]
        expected_end = epoch.get("effective_end")
        observed_end = row.get("effective_end")
        pairs: list[tuple[str, Any, Any]] = [
            ("strategy_id", epoch.get("strategy_id"), row.get("strategy_id")),
            (
                "effective_start",
                epoch.get("effective_start"),
                row.get("effective_start"),
            ),
            (
                "effective_end",
                OPEN_EPOCH if expected_end in (None, OPEN_EPOCH) else expected_end,
                OPEN_EPOCH if observed_end is None else observed_end,
            ),
        ]
        for field, expected_value, observed_value in pairs:
            if expected_value != observed_value:
                differences.append(
                    Difference(
                        f"manifest {account}.{field}", expected_value, observed_value
                    )
                )
        for key, field in (
            ("opening", DERIVED_EPOCH_FIELDS["capital.epoch.opening"]),
            ("net_capital", DERIVED_EPOCH_FIELDS["capital.epoch.net_capital"]),
            ("closing", DERIVED_EPOCH_FIELDS["capital.epoch.closing"]),
        ):
            observed = to_decimal(row.get(field))
            if (
                observed is None
                or abs(_amount(epoch[key]) - observed) > MONEY_TOLERANCE
            ):
                differences.append(
                    Difference(
                        f"manifest {account}.{field}", epoch[key], row.get(field)
                    )
                )
    for account, found in sorted(by_account.items()):
        if account in epochs:
            continue
        if account in parents:
            differences.append(
                Difference(
                    f"manifest {account}",
                    "no epoch (residual parent)",
                    f"{len(found)} rows",
                    "the parent of derived epochs still carries an epoch",
                )
            )
        elif any(account.startswith(f"{parent}-") for parent in parents):
            differences.append(
                Difference(
                    f"manifest {account}",
                    "not derived",
                    f"{len(found)} rows",
                    "the manifest has a derived-looking account nobody derived",
                )
            )
    return differences


def _load(path: Path) -> Mapping[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError(f"{path}: not a JSON object")
    return data


def _manifest_rows(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The rows of a panel (``account_manifest.rows``) or a bare manifest (``rows``)."""

    section = document.get("account_manifest")
    candidate = section.get("rows") if isinstance(section, Mapping) else None
    candidate = candidate if candidate is not None else document.get("rows")
    if not isinstance(candidate, list):
        raise ValueError("manifest document has no rows")
    return [row for row in candidate if isinstance(row, Mapping)]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("derivation_a", type=Path)
    parser.add_argument("derivation_b", type=Path)
    parser.add_argument("--manifest", type=Path, help="panel or manifest JSON")
    args = parser.parse_args(argv)
    left, right = _load(args.derivation_a), _load(args.derivation_b)
    result = {"a_vs_b": [d.as_dict() for d in compare_derivations(left, right)]}
    if args.manifest is not None:
        rows = _manifest_rows(_load(args.manifest))
        result["a_vs_manifest"] = [
            d.as_dict() for d in compare_with_manifest(left, rows)
        ]
        result["b_vs_manifest"] = [
            d.as_dict() for d in compare_with_manifest(right, rows)
        ]
    sys.stdout.write(json.dumps(result, indent=1, sort_keys=True) + "\n")
    return 0 if not any(result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
