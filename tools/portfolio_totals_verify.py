"""Independent re-derivation of the portfolio-level report totals (issue 593).

The producer (``portfolio_totals``) builds ``portfolio_twr_cumulative``,
``portfolio_mwr_annualised`` and ``capital_reconciliation`` from the in-memory
verified manifest.  This module reads only the bytes a candidate carries (the
published report JSON, the bound epoch manifest and the current-epoch
crosswalk) and re-derives every figure by a different route:

* capital: a per-account chain view built from the governed events, which must
  agree with the producer's per-epoch lines and with the named components;
* MWR: its own bisection on the net present value, not the ledger's XIRR;
* TWR: the index path's first and last point, and an own boundary-aligned
  summed-value composite of the epoch traces.

It imports nothing from the producer, so a defect shared by construction cannot
agree with itself.  Any disagreement raises ``PortfolioTotalsVerificationError``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, localcontext
from typing import Any

PRECISION = 100
RATE_TOLERANCE = Decimal("1e-15")
TOTAL_KEYS = (
    "portfolio_twr_cumulative",
    "portfolio_mwr_annualised",
    "capital_reconciliation",
)
OPENING_AND_FLOW_CLASSES = (
    "OPENING_CONTRIBUTED_BASIS",
    "EXTERNAL_CONTRIBUTION",
    "EXTERNAL_WITHDRAWAL",
    "INTERNAL_TRANSFER",
)
FLOW_CLASSES = (
    "OPENING_CONTRIBUTED_BASIS",
    "EXTERNAL_CONTRIBUTION",
    "EXTERNAL_WITHDRAWAL",
)


class PortfolioTotalsVerificationError(ValueError):
    """The published totals differ from the independent re-derivation."""


def _fail(message: str) -> PortfolioTotalsVerificationError:
    return PortfolioTotalsVerificationError(f"portfolio totals: {message}")


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def _text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _event_class(event: Mapping[str, Any]) -> str:
    return str(event.get("effective_classification") or event["classification"])


def included_rows(
    manifest: Mapping[str, Any], crosswalk: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    """The manifest rows of the current product epochs the crosswalk includes.

    A derived sleeve epoch (#567) has no crosswalk row: its account is not one
    the crosswalk names, and its strategy is one the crosswalk includes (the
    sleeve stands for its residual parent's epoch).  A derived epoch of any
    other strategy is retained history and no part of the product totals.
    """

    wanted = {
        (r["account_id"], r["strategy_id"], r["effective_start"], r["effective_end"])
        for r in crosswalk["rows"]
        if r["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH"
    }
    product_strategies = {key[1] for key in wanted}
    crosswalk_accounts = {r["account_id"] for r in crosswalk["rows"]}
    return [
        row
        for row in manifest["rows"]
        if (
            row.get("account_id"),
            row.get("strategy_id"),
            row.get("effective_start"),
            row.get("effective_end"),
        )
        in wanted
        or (
            row.get("account_id") not in crosswalk_accounts
            and row.get("strategy_id") in product_strategies
        )
    ]


# --------------------------------------------------------------- capital


def _check_capital(
    block: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    capital: Mapping[str, Any],
    cutoff: str,
) -> None:
    if block.get("status") != "RECONCILED":
        # Only the producer's named unavailability reasons may stand in for a
        # reconciliation, and only when the closure inputs are really absent
        # or inconsistent; a reconcilable perimeter must be published.
        epochs = capital.get("required_epochs") or []
        keys = {
            (e.get("account_id"), e["strategy_id"], e["effective_start"])
            for e in epochs
        }
        row_keys = {
            (r["account_id"], r["strategy_id"], r["effective_start"]) for r in rows
        }
        reconcilable = (
            bool(capital.get("events"))
            and all(r.get("status") == "OK" for r in rows)
            and keys == row_keys
            and len(row_keys) == len(rows)
        )
        if reconcilable:
            raise _fail(
                "capital_reconciliation is unavailable for a reconcilable perimeter"
            )
        return
    with localcontext() as context:
        context.prec = PRECISION
        gains = sum((_d(r["gain"]) for r in rows), Decimal(0))
        open_current = sum(
            (_d(r["current_value"]) for r in rows if r["end"] == cutoff), Decimal(0)
        )
        derived = open_current - gains
        governed = _d(capital["current_aggregate_usd"])
        for field, expected in (
            ("current_of_open_epochs_usd", open_current),
            ("gain_of_all_epochs_usd", gains),
            ("derived_total_net_capital_usd", derived),
            ("governed_invested_capital_usd", governed),
            ("difference_usd", derived - governed),
        ):
            if block.get(field) != _text(expected):
                raise _fail(
                    f"capital_reconciliation.{field} differs from the re-derivation"
                )
        if block.get("residual_usd") != "0":
            raise _fail("capital_reconciliation residual is not zero")
        if block.get("open_epoch_count") != sum(1 for r in rows if r["end"] == cutoff):
            raise _fail("capital_reconciliation open epoch count differs")
        if block.get("epoch_count") != len(rows):
            raise _fail("capital_reconciliation epoch count differs")
        components = block.get("components")
        if not isinstance(components, list):
            raise _fail("capital_reconciliation components are absent")
        total = sum((_d(c["amount_usd"]) for c in components), Decimal(0))
        if total != derived - governed:
            raise _fail(
                "capital_reconciliation components do not sum to the difference: "
                f"{_text(total)} against {_text(derived - governed)}"
            )
        # Independent chain view: per account, derived chain value against the
        # governed chain balance, taken from the events themselves.
        account_of_epoch = {
            e["epoch_id"]: e.get("account_id") for e in capital["required_epochs"]
        }
        governed_by_account: dict[Any, Decimal] = defaultdict(Decimal)
        transition_by_account: dict[Any, Decimal] = defaultdict(Decimal)
        for event in capital["events"]:
            account = account_of_epoch.get(event["epoch_id"])
            amount = _d(event["invested_capital_effect_usd"])
            governed_by_account[account] += amount
            if _event_class(event) not in OPENING_AND_FLOW_CLASSES:
                transition_by_account[account] += amount
        derived_by_account: dict[Any, Decimal] = defaultdict(Decimal)
        for r in rows:
            derived_by_account[r["account_id"]] -= _d(r["gain"])
            if r["end"] == cutoff:
                derived_by_account[r["account_id"]] += _d(r["current_value"])
        if sum(governed_by_account.values(), Decimal(0)) != governed:
            raise _fail("governed events do not sum to the governed invested capital")
        lines_by_account: dict[Any, Decimal] = defaultdict(Decimal)
        for component in components:
            for line in component["lines"]:
                lines_by_account[line["account_id"]] += _d(line["amount_usd"])
        for account in set(derived_by_account) | set(governed_by_account):
            chain = derived_by_account[account] - governed_by_account[account]
            published = lines_by_account[account] - transition_by_account[account]
            if chain != published:
                raise _fail(
                    f"capital_reconciliation lines for {account} give {_text(published)} "
                    f"but the chain difference is {_text(chain)}"
                )
        other = sum(
            (
                _d(e["invested_capital_effect_usd"])
                for e in capital["events"]
                if _event_class(e)
                not in {
                    "OPENING_CONTRIBUTED_BASIS",
                    "EXTERNAL_CONTRIBUTION",
                    "EXTERNAL_WITHDRAWAL",
                    "INTERNAL_TRANSFER",
                    "PREDECESSOR_TRANSITION",
                }
            ),
            Decimal(0),
        )
        transitions = sum(transition_by_account.values(), Decimal(0)) - other
        by_id = {c["component_id"]: c for c in components}
        for component_id, adjustment in (
            ("EPOCH_HANDOFF_BOUNDARY_NAV_GAP", -transitions),
            ("OTHER_GOVERNED_EVENT_CLASSES", -other),
        ):
            component = by_id.get(component_id)
            if component is None:
                raise _fail(f"capital_reconciliation lacks {component_id}")
            lines = sum((_d(x["amount_usd"]) for x in component["lines"]), Decimal(0))
            if _d(component["amount_usd"]) != lines + adjustment:
                raise _fail(
                    f"{component_id} differs from its lines and governed events"
                )
        for component in components:
            if component["component_id"] in {
                "EPOCH_HANDOFF_BOUNDARY_NAV_GAP",
                "OTHER_GOVERNED_EVENT_CLASSES",
            }:
                continue
            lines = sum((_d(x["amount_usd"]) for x in component["lines"]), Decimal(0))
            if _d(component["amount_usd"]) != lines:
                raise _fail(f"{component['component_id']} differs from its lines")


# ------------------------------------------------------------------- MWR


def _npv(flows: Sequence[tuple[date, Decimal]], rate: Decimal) -> Decimal:
    origin = flows[0][0]
    return sum(
        (
            amount
            / (Decimal(1) + rate) ** (Decimal((day - origin).days) / Decimal(365))
            for day, amount in flows
        ),
        Decimal(0),
    )


def _bracket(
    flows: Sequence[tuple[date, Decimal]],
) -> tuple[Decimal, Decimal, Decimal] | None:
    """A sign-changing rate interval, widened by powers of ten on both sides."""

    candidates_low = [
        Decimal(-1) + Decimal(10) ** -exponent for exponent in (9, 20, 40)
    ]
    candidates_high = [Decimal(10) ** exponent for exponent in (3, 6, 12, 24, 40)]
    for low in candidates_low:
        f_low = _npv(flows, low)
        for high in candidates_high:
            f_high = _npv(flows, high)
            if f_low == 0:
                return low, low, f_low
            if f_high == 0:
                return high, high, f_high
            if (f_low < 0) != (f_high < 0):
                return low, high, f_low
    return None


def _solve_rate(flows: Sequence[tuple[date, Decimal]]) -> Decimal | None:
    found = _bracket(flows)
    if found is None:
        return None
    low, high, f_low = found
    if low == high:
        return low
    for _ in range(400):
        middle = (low + high) / 2
        f_middle = _npv(flows, middle)
        if (f_middle < 0) == (f_low < 0):
            low, f_low = middle, f_middle
        else:
            high = middle
    return (low + high) / 2


def _check_mwr(
    block: Mapping[str, Any],
    capital: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    hwm_summary: Mapping[str, Any],
    cutoff: str,
) -> None:
    with localcontext() as context:
        context.prec = 50
        net: dict[date, Decimal] = defaultdict(Decimal)
        for event in capital.get("events") or []:
            if _event_class(event) in FLOW_CLASSES:
                net[date.fromisoformat(event["date"])] -= _d(
                    event["invested_capital_effect_usd"]
                )
        flows = sorted((day, amount) for day, amount in net.items() if amount != 0)
        end = date.fromisoformat(cutoff)
        perimeter = hwm_summary.get("governed_perimeter_value_usd")
        open_current = sum(
            (_d(r["current_value"]) for r in rows if r["end"] == cutoff), Decimal(0)
        )
        terminal = _d(perimeter) if perimeter is not None else None
        justified = (
            not flows
            or terminal is None
            or abs(terminal - open_current) > Decimal("1e-18")
            or end <= flows[0][0]
        )
        if not justified:
            assert terminal is not None
            amounts = [amount for _day, amount in [*flows, (end, terminal)]]
            changes = sum(
                1
                for a, b in zip(amounts, amounts[1:], strict=False)
                if (a < 0) != (b < 0)
            )
            # One chronological sign change proves a unique root: an
            # unavailable block is then only justified if no root exists.
            if changes == 1 and _solve_rate([*flows, (end, terminal)]) is not None:
                if block.get("status") != "AVAILABLE":
                    raise _fail(
                        "portfolio_mwr_annualised is unavailable although the "
                        "governed flows have a unique root"
                    )
        if block.get("status") != "AVAILABLE":
            return
        if terminal is None or abs(terminal - open_current) > Decimal("1e-18"):
            raise _fail(
                "portfolio_mwr_annualised is available but the perimeter value differs"
            )
        flows.append((end, terminal))
        days = (end - flows[0][0]).days
        if (
            block.get("window_start") != flows[0][0].isoformat()
            or block.get("window_end") != cutoff
            or block.get("window_days") != days
            or block.get("flow_count") != len(flows) - 1
            or block.get("terminal_value_usd") != _text(terminal)
        ):
            raise _fail("portfolio_mwr_annualised window or flows differ")
        rate = _solve_rate(flows)
        if rate is None:
            raise _fail("portfolio_mwr_annualised has no independent root")
        equivalent = (Decimal(1) + rate) ** (Decimal(days) / Decimal(365)) - 1
        tolerance = RATE_TOLERANCE * max(Decimal(1), abs(equivalent))
        if abs(_d(block["window_equivalent_cumulative"]) - equivalent) > tolerance:
            raise _fail("portfolio_mwr_annualised window equivalent differs")
        if days >= 365:
            if block.get("value") is None or abs(
                _d(block["value"]) - rate
            ) > RATE_TOLERANCE * max(Decimal(1), abs(rate)):
                raise _fail(
                    "portfolio_mwr_annualised value differs from the re-derivation"
                )
        elif block.get("value") is not None:
            raise _fail("portfolio_mwr_annualised is annualised under 365 days")


# ------------------------------------------------------------------- TWR


def _summed_epoch(rows: Sequence[Mapping[str, Any]]) -> tuple[str, Decimal | None]:
    by_end: dict[date, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    spans: list[tuple[str, date, date]] = []
    for r in rows:
        trace = r.get("observation_trace")
        if r.get("status") != "OK" or not trace or not trace.get("links"):
            continue
        member = f"{r['account_id']}|{r['strategy_id']}|{r['effective_start']}"
        for link in trace["links"]:
            capital = _d(link["base_nav"]) + _d(link["beginning_flow"])
            start, end = (
                date.fromisoformat(link["start_date"]),
                date.fromisoformat(link["end_date"]),
            )
            by_end[end].append((capital, capital * _d(link["factor"])))
            spans.append((member, start, end))
    if not spans:
        return "UNAVAILABLE", None
    if len({member for member, _s, _e in spans}) > 1:
        bounds = sorted({s for _m, s, _e in spans} | {e for _m, _s, e in spans})
        previous = dict(zip(bounds[1:], bounds, strict=False))
        if any(previous[e] != s for _m, s, e in spans):
            return "UNAVAILABLE", None
    wealth = Decimal(1)
    for day in sorted(by_end):
        capital = sum((c for c, _v in by_end[day]), Decimal(0))
        value = sum((v for _c, v in by_end[day]), Decimal(0))
        if capital == 0:
            continue
        wealth *= value / capital
    return "AVAILABLE", wealth - 1


def _check_twr(
    block: Mapping[str, Any],
    proof: Mapping[str, Any] | None,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    if proof is None or proof.get("status") != "AVAILABLE":
        if block.get("status") != "UNAVAILABLE":
            raise _fail(
                "portfolio_twr_cumulative is published without an available proof"
            )
        return
    path = proof["index_path"]
    with localcontext() as context:
        context.prec = PRECISION
        first, last = path[0], path[-1]
        headline = _d(last["index"]) / _d(first["index"]) - 1
        start, end = (
            date.fromisoformat(first["as_of"]),
            date.fromisoformat(last["as_of"]),
        )
        if (
            block.get("status") != "AVAILABLE"
            or block.get("basis") != "GOVERNED_HWM_INDEX"
            or block.get("value") != _text(headline)
            or block.get("window_start") != start.isoformat()
            or block.get("window_end") != end.isoformat()
            or block.get("window_days") != (end - start).days
            or block.get("current_index") != _text(_d(last["index"]))
        ):
            raise _fail("portfolio_twr_cumulative differs from the governed HWM index")
        summed = block["summed_epoch_twr"]
        status, value = _summed_epoch(rows)
        if summed["status"] != status:
            raise _fail("summed-epoch TWR availability differs from the re-derivation")
        if value is not None:
            if abs(_d(summed["value"]) - value) > Decimal("1e-30"):
                raise _fail("summed-epoch TWR differs from the re-derivation")
            if block["headline_minus_summed_epoch_twr"] != _text(
                _d(block["value"]) - _d(summed["value"])
            ):
                raise _fail("headline minus summed-epoch TWR differs")
        elif block["headline_minus_summed_epoch_twr"] is not None:
            raise _fail("a difference is published without a summed-epoch TWR")


def verify_portfolio_totals(
    report: Mapping[str, Any],
    manifest: Mapping[str, Any],
    crosswalk: Mapping[str, Any],
) -> None:
    """Re-derive the three totals from candidate bytes; raise on any difference."""

    capital = report.get("invested_capital")
    present = [key for key in TOTAL_KEYS if key in report]
    if capital is None:
        if present:
            raise _fail("totals are published without governed invested capital")
        return
    rows = included_rows(manifest, crosswalk)
    cutoff = crosswalk["calculation_cutoff_date"]
    if not rows:
        if present:
            raise _fail("totals are published without epoch rows")
        return
    if len(present) != len(TOTAL_KEYS):
        raise _fail("totals are absent although governed capital and epoch rows exist")
    _check_capital(report["capital_reconciliation"], rows, capital, cutoff)
    _check_mwr(
        report["portfolio_mwr_annualised"],
        capital,
        rows,
        report.get("hwm_summary") or {},
        cutoff,
    )
    _check_twr(
        report["portfolio_twr_cumulative"], report.get("hwm_governed_proof"), rows
    )
