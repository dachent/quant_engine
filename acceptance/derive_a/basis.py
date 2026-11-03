"""Declaration-driven sleeve basis for the strategy rows (#624, option B), derivation A.

A ``strategy-basis-declaration/1`` names the strategies stated on their derived sleeve
instead of the parent account.  Without one nothing here runs and no row changes.  With
one, for each named strategy and for that stated reason only, four expected-baseline row
families move:

* ``twr.strategy`` is the strategy's open sleeve epoch's own ITD return (this
  derivation's ``twr.account`` of the epoch, its engine unchanged);
* ``capital.strategy.cutoff`` is the epoch's closing sleeve NAV;
* ``capital.portfolio.systematic.cutoff`` follows (the old strategy value replaced by the
  sleeve NAV, at full precision when the strategy rows recorded it);
* ``weight.strategy.cutoff`` of every strategy is its capital over the new total.

``mwr.strategy`` already stands on the sleeve (combined run) and every other row is left
exactly as derived.  The derivation shares no code with ``derive_b`` or ``tools/``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal as D
from typing import TYPE_CHECKING, Any

from .sources import InputError

if TYPE_CHECKING:
    from .derive import Output

SCHEMA = "strategy-basis-declaration/1"
BASIS = "SLEEVE_EPOCH"
TWR = "twr.strategy"
CAPITAL = "capital.strategy.cutoff"
SYSTEMATIC = "capital.portfolio.systematic.cutoff"
WEIGHT = "weight.strategy.cutoff"
EXACT_SYSTEMATIC = "systematic"


def exact_capital_key(strategy_id: str) -> str:
    return f"capital:{strategy_id}"


@dataclass(frozen=True)
class Declaration:
    strategies: tuple[str, ...]
    sha256: str
    label: str


def load_declaration(data: bytes, label: str) -> Declaration:
    """The strategies a ``strategy-basis-declaration/1`` states on their sleeve."""
    try:
        document = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise InputError(f"strategy basis declaration {label} is not JSON") from exc
    if not isinstance(document, dict) or set(document) != {"schema", "strategies"}:
        raise InputError(f"strategy basis declaration {label}: keys differ")
    if document["schema"] != SCHEMA:
        raise InputError(f"strategy basis declaration {label}: schema differs")
    entries = document["strategies"]
    if not isinstance(entries, list) or not entries:
        raise InputError(f"strategy basis declaration {label} names no strategy")
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"strategy_id", "basis"}:
            raise InputError(f"strategy basis declaration {label}: entry keys differ")
        strategy, basis = entry["strategy_id"], entry["basis"]
        if not isinstance(strategy, str) or not strategy:
            raise InputError(f"strategy basis declaration {label}: empty strategy id")
        if basis != BASIS:
            raise InputError(
                f"strategy basis declaration {label}: {basis!r} is not restatable"
            )
        if strategy in names:
            raise InputError(
                f"strategy basis declaration {label}: {strategy} declared twice"
            )
        names.append(strategy)
    return Declaration(tuple(sorted(names)), hashlib.sha256(data).hexdigest(), label)


def _asof(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def _take(
    rows: list[dict[str, Any]], metric: str, entity: str, window: str | None
) -> dict[str, Any] | None:
    """Remove and return the row of a key (a window of ``""`` matches ``None``)."""
    for index, row in enumerate(rows):
        if (
            row["metric_id"] == metric
            and row["entity"] == entity
            and (row.get("window") or None) == (window or None)
        ):
            return rows.pop(index)
    return None


def _peek(
    rows: list[dict[str, Any]], metric: str, entity: str, window: str | None
) -> dict[str, Any] | None:
    for row in rows:
        if (
            row["metric_id"] == metric
            and row["entity"] == entity
            and (row.get("window") or None) == (window or None)
        ):
            return row
    return None


def restate(
    output: Output, declaration: Declaration, sleeve_document: dict[str, Any]
) -> list[str]:
    """Move the four row families of every declared strategy; return the moved keys."""
    from .derive import WINDOW, _money, _ratio

    derivation = sleeve_document["sleeve_derivation"]
    open_epochs: dict[str, list[dict[str, Any]]] = {}
    for epoch in derivation["epochs"]:
        if epoch["effective_end"] is None:
            open_epochs.setdefault(epoch["strategy_id"], []).append(epoch)
    sleeve_inputs = sorted(record["path"] for record in derivation["inputs"].values())
    moved: list[str] = []
    capital_after: dict[str, D] = {}
    capital_before: dict[str, D] = {}
    for strategy in declaration.strategies:
        found = open_epochs.get(strategy, [])
        if len(found) != 1:
            raise InputError(
                f"declared strategy {strategy}: {len(found)} derived sleeve epochs "
                "are open at the cutoff, exactly one is required"
            )
        epoch = found[0]
        account = epoch["account_id"]
        closing = D(epoch["closing"])
        as_of = dt.date.fromisoformat(max(epoch["nav"]))
        inputs = sorted({*sleeve_inputs, declaration.label})
        stated = (
            f"strategy-basis-declaration/1 {declaration.sha256[:16]} states "
            f"{strategy} on its sleeve epoch {account} (opening NAV "
            f"{epoch['effective_start']})"
        )
        twr_row = _peek(sleeve_document["metrics"], "twr.account", account, WINDOW)
        before = _take(output.metrics, TWR, strategy, WINDOW)
        _take(output.not_derivable, TWR, strategy, WINDOW)
        if twr_row is None:
            reason = next(
                (
                    row["reason"]
                    for row in sleeve_document["not_derivable"]
                    if row["metric_id"] == "twr.account" and row["entity"] == account
                ),
                f"{account} has no derived return",
            )
            output.missing(TWR, strategy, WINDOW, f"{stated}: {reason}")
        else:
            output.metric(
                TWR,
                strategy,
                WINDOW,
                _asof(twr_row["as_of"]),
                twr_row["value"],
                "ratio",
                sorted({*inputs, *twr_row["inputs_used"]}),
                [
                    stated + "; the return is that epoch's ITD return, the sleeve NAV "
                    "with the sleeve's transfers external, date-only at end of day"
                    + (
                        f"; the parent-account basis row was {before['value']}"
                        if before is not None
                        else ""
                    )
                ],
            )
        moved.append(f"{TWR}|{strategy}")
        old = _take(output.metrics, CAPITAL, strategy, None)
        _take(output.not_derivable, CAPITAL, strategy, None)
        capital_after[strategy] = closing
        if old is not None:
            capital_before[strategy] = output.exact.get(
                exact_capital_key(strategy), D(old["value"])
            )
        output.metric(
            CAPITAL,
            strategy,
            None,
            as_of,
            _money(closing),
            "USD",
            inputs,
            [
                stated
                + "; capital is the sleeve's closing NAV after allocated costs"
                + (
                    f"; the parent-account basis row was {old['value']}"
                    if old is not None
                    else ""
                )
            ],
        )
        moved.append(f"{CAPITAL}|{strategy}")
    # the systematic total and every weight follow from the moved capital rows
    total_row = _peek(output.metrics, SYSTEMATIC, "portfolio", None)
    if total_row is None:
        return moved
    old_total = output.exact.get(EXACT_SYSTEMATIC, D(total_row["value"]))
    total = old_total
    for strategy in declaration.strategies:
        if strategy not in capital_before:
            raise InputError(
                f"declared strategy {strategy} has no capital row to restate in the "
                "systematic total"
            )
        total += capital_after[strategy] - capital_before[strategy]
    _take(output.metrics, SYSTEMATIC, "portfolio", None)
    output.metric(
        SYSTEMATIC,
        "portfolio",
        None,
        _asof(total_row["as_of"]),
        _money(total),
        "USD",
        sorted({*total_row["inputs_used"], *sleeve_inputs, declaration.label}),
        [
            total_row["notes"],
            f"strategy-basis-declaration/1 {declaration.sha256[:16]}: "
            f"{', '.join(declaration.strategies)} valued as its sleeve; the parent-"
            f"account total was {total_row['value']}",
        ],
    )
    moved.append(f"{SYSTEMATIC}|portfolio")
    capitals: dict[str, D] = {}
    for row in output.metrics:
        if row["metric_id"] == CAPITAL:
            capitals[row["entity"]] = output.exact.get(
                exact_capital_key(row["entity"]), D(row["value"])
            )
    capitals.update(capital_after)
    for row in [r for r in output.metrics if r["metric_id"] == WEIGHT]:
        strategy = row["entity"]
        if strategy not in capitals:
            continue
        _take(output.metrics, WEIGHT, strategy, None)
        output.metric(
            WEIGHT,
            strategy,
            None,
            _asof(row["as_of"]),
            _ratio(capitals[strategy] / total),
            "ratio",
            sorted({*row["inputs_used"], *sleeve_inputs, declaration.label}),
            [
                f"capital.strategy.cutoff {_money(capitals[strategy])} / "
                f"capital.portfolio.systematic.cutoff {_money(total)}"
                + (
                    "; the strategy is stated on its sleeve"
                    if strategy in capital_after
                    else "; the perimeter moved because a declared strategy is "
                    "stated on its sleeve"
                )
            ],
        )
        moved.append(f"{WEIGHT}|{strategy}")
    return moved
