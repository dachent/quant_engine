"""Disclosure that goes with a restated strategy row (issue #624, option B).

When a strategy's row is restated onto its sleeve (``strategy_basis``), the figures the
row used to carry for the whole parent account must not disappear, and a reader must see
how the two bases and the audit's convention relate.  Everything here is a pure function
of three inputs, none of it typed:

* the parent-account figures of the strategy row, computed by the report's own engine
  from the UNRESTATED parent series (``account_basis``);
* the sleeve facts (``sleeve-account-facts`` artifact): the sleeves' daily NAVs, flows and
  allocated costs and the perimeter pseudo-account that holds the account's unallocated
  cash and margin;
* the restated sleeve series (the verified observation trace) and its closing value.

Three derivations come out of them:

* the margin liability line: gross sleeve NAV, the unallocated bucket, the net equivalent
  and the identity "sleeve NAVs + bucket = the ledger's account NAV" (fails closed);
* the bridge from the account basis to the sleeve basis (value and return, with both
  windows and what the account perimeter holds besides the sleeve);
* the bridge from the audit's convention to the panel's, in three steps computed from the
  facts and closed against the trace (fails closed if the residual is not zero).

No strategy id, date or figure is written in this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
import hashlib
import json
from typing import Any, Final

from quant_engine.portfolio_ledger import decimal_text

import strategy_basis

DISCLOSURE_SCHEMA: Final = "strategy-basis-disclosure/1"
FACTS_ARTIFACT_ID: Final = "sleeve-account-facts"
FACTS_SCHEMA_VERSION: Final = 1
# half a cent: the facts carry the ledger's NAV exactly, the tolerance only absorbs the
# rounding of a ledger figure published to the cent
IDENTITY_TOLERANCE_USD: Final = Decimal("0.005")
# a return residual of one part in a billion is zero at every published precision
RESIDUAL_TOLERANCE: Final = Decimal("1e-9")
_PRECISION: Final = 60
_DISPLAY_PRECISION: Final = 34

# the strategy-row fields that make up the parent-account basis of the row; the block
# carries them under the row's own names so each stays findable by path
ACCOUNT_BASIS_FIELDS: Final = (
    "account_live_cumulative_return",
    "account_live_cumulative_return_label",
    "account_live_cagr",
    "account_live_cagr_annualization",
    "account_live_cagr_status",
    "account_live_max_drawdown",
    "hwm_max_drawdown",
    "hwm_current_drawdown",
    "ytd_twr",
    "calendar_year_twr",
    "current_value_usd",
    "cost_rate_denominator_usd",
    "systematic_weight",
    "account_live_return_series_schema",
    "account_live_return_series_sha256",
    "account_live_return_observations",
    "account_live_observed_start",
    "account_live_observed_end",
    "account_live_status",
    "account_live_path_metrics_status",
    "hwm_path_metrics_status",
)
ACCOUNT_BASIS_FALLBACK: Final = "PARENT_ACCOUNT_LIVE_SERIES"


class DisclosureError(ValueError):
    """The facts, the trace and the ledger do not close; nothing is disclosed."""


@dataclass(frozen=True)
class SleeveFacts:
    """A parsed ``sleeve-account-facts`` artifact and the digest of its bytes."""

    document: Mapping[str, Any]
    sha256: str

    @property
    def parent_account_id(self) -> str:
        return str(self.document["parent_account_id"])

    @property
    def provider_id(self) -> str:
        return str(self.document["provider_id"])

    @property
    def bindings(self) -> Mapping[str, Any]:
        value = self.document["bindings"]
        assert isinstance(value, Mapping)
        return value


def parse_facts(raw: bytes) -> SleeveFacts:
    """The facts artifact from its bytes (the schema is checked by the caller)."""

    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DisclosureError("sleeve facts are not JSON") from exc
    if (
        not isinstance(document, dict)
        or document.get("artifact_id") != FACTS_ARTIFACT_ID
        or document.get("schema_version") != FACTS_SCHEMA_VERSION
    ):
        raise DisclosureError("sleeve facts artifact identity differs")
    for key in ("parent_account_id", "provider_id", "bindings", "sleeves"):
        if key not in document:
            raise DisclosureError(f"sleeve facts lack {key}")
    if not isinstance(document["bindings"], dict) or not isinstance(
        document["sleeves"], list
    ):
        raise DisclosureError("sleeve facts shape differs")
    return SleeveFacts(document=document, sha256=hashlib.sha256(raw).hexdigest())


def verify_bindings(
    facts: SleeveFacts,
    *,
    parent_account_id: str,
    tag_map_sha256: str | None,
    ledger_generation_id: str | None,
) -> list[str]:
    """Check every binding of the facts the report holds the other side of.

    The names of the bindings that were checked are returned, so the disclosure can say
    exactly which ones it proved.  A binding whose other side the report does not hold
    (``account_rows_sha256``, ``declared_inputs_sha256``, ``builder_code``) is proved by the
    epoch manifest stage that derived the facts, not here.
    """

    checked: list[str] = []
    if facts.parent_account_id != parent_account_id:
        raise DisclosureError(
            f"sleeve facts are of parent {facts.parent_account_id}, "
            f"the restated sleeve's parent is {parent_account_id}"
        )
    checked.append("parent_account_id")
    if tag_map_sha256 is not None:
        if facts.bindings.get("tag_map_sha256") != tag_map_sha256:
            raise DisclosureError(
                "sleeve facts bind a tag map other than the sleeve epoch's"
            )
        checked.append("tag_map_sha256")
    if ledger_generation_id is not None:
        if facts.bindings.get("parent_ledger_generation_id") != ledger_generation_id:
            raise DisclosureError(
                "sleeve facts bind a provider ledger generation other than the "
                "selected ledger's"
            )
        checked.append("parent_ledger_generation_id")
    return checked


def _dec(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - InvalidOperation and TypeError alike
        raise DisclosureError(f"{name} is not a decimal") from exc
    if not result.is_finite():
        raise DisclosureError(f"{name} is not finite")
    return result


def _text(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = _DISPLAY_PRECISION
        return decimal_text(+value)


def _usd(value: Decimal) -> str:
    """Whole cents for prose; the structured fields keep the exact figure."""

    return f"{value.quantize(Decimal('0.01')):,}"


def _sleeve_by_account(facts: SleeveFacts, account_id: str) -> Mapping[str, Any]:
    found = [
        item
        for item in facts.document["sleeves"]
        if isinstance(item, Mapping) and item.get("account_id") == account_id
    ]
    if len(found) != 1:
        raise DisclosureError(f"sleeve facts hold no single sleeve {account_id}")
    return found[0]


def _days_through(sleeve: Mapping[str, Any], cutoff: str) -> list[Mapping[str, Any]]:
    days = [item for item in sleeve["days"] if str(item["day"]) <= cutoff]
    ordered = [str(item["day"]) for item in days]
    if ordered != sorted(set(ordered)):
        raise DisclosureError("sleeve facts days are not strictly ordered")
    return days


def _sleeve_value_at(sleeve: Mapping[str, Any], cutoff: str) -> Decimal:
    """The sleeve's closing NAV after allocated costs on the cutoff day.

    A sleeve with no row on the cutoff day is worth zero only if it was closed (its last
    row is zero) or not yet open; anything else is a gap in the facts.
    """

    days = _days_through(sleeve, cutoff)
    if not days:
        return Decimal(0)
    last = days[-1]
    value = _dec(last["nav_end_after_alloc"], "sleeve NAV")
    if str(last["day"]) != cutoff and value != 0:
        raise DisclosureError(
            f"sleeve {sleeve['sleeve_id']} has a NAV but no row on the cutoff day"
        )
    return value


def sleeve_id(facts: SleeveFacts, sleeve_account_id: str) -> str:
    """The facts' own id of the sleeve account."""

    return str(_sleeve_by_account(facts, sleeve_account_id)["sleeve_id"])


def unallocated_bucket(facts: SleeveFacts, cutoff: str) -> Decimal:
    """The perimeter pseudo-account's NAV on the cutoff day: unallocated cash and margin."""

    return _bucket_at(facts, cutoff)


def _bucket_at(facts: SleeveFacts, cutoff: str) -> Decimal:
    perimeter = facts.document.get("perimeter_account")
    if not isinstance(perimeter, Mapping):
        raise DisclosureError("sleeve facts hold no perimeter account")
    rows = [item for item in perimeter["days"] if str(item["day"]) == cutoff]
    if len(rows) != 1:
        raise DisclosureError("the unallocated bucket has no row on the cutoff day")
    return _dec(rows[0]["nav_end"], "unallocated bucket NAV")


def margin_liability(
    facts: SleeveFacts,
    *,
    sleeve_account_id: str,
    cutoff: str,
    sleeve_value: Decimal,
    ledger_account_value: Decimal,
    leverage_pointer: Mapping[str, Any],
    other_strategy_rows: int,
    strategy_name: str,
    account_label: str,
) -> dict[str, Any]:
    """The value lines of the restated sleeve and the identity that makes them net."""

    sleeve = _sleeve_by_account(facts, sleeve_account_id)
    bucket = _bucket_at(facts, cutoff)
    gross = _sleeve_value_at(sleeve, cutoff)
    if abs(gross - sleeve_value) > IDENTITY_TOLERANCE_USD:
        raise DisclosureError(
            "the sleeve NAV of the facts differs from the restated sleeve value: "
            f"{_text(gross)} against {_text(sleeve_value)}"
        )
    navs = {
        str(item["account_id"]): _sleeve_value_at(item, cutoff)
        for item in facts.document["sleeves"]
    }
    sleeves_total = sum(navs.values(), Decimal(0))
    difference = sleeves_total + bucket - ledger_account_value
    identity = {
        "sleeve_navs_usd": _text(sleeves_total),
        "unallocated_bucket_usd": _text(bucket),
        "account_nav_usd": _text(ledger_account_value),
        "difference_usd": _text(difference),
        "tolerance_usd": _text(IDENTITY_TOLERANCE_USD),
        "status": "HOLDS",
    }
    if abs(difference) > IDENTITY_TOLERANCE_USD:
        identity["status"] = "BROKEN"
        raise DisclosureError(
            "sleeve NAVs plus the unallocated bucket do not equal the ledger account "
            f"NAV at {cutoff}: {_text(sleeves_total)} + {_text(bucket)} against "
            f"{_text(ledger_account_value)}"
        )
    net = gross + bucket
    kind = "MARGIN_DEBIT" if bucket < 0 else "UNALLOCATED_CASH_CREDIT"
    other = sleeves_total - gross
    equal_clause = (
        f"equals the equity of {account_label} ({_usd(ledger_account_value)}) at "
        f"{cutoff}"
        if other == 0
        else (
            f"is the sleeve's share of the equity of {account_label} "
            f"({_usd(ledger_account_value)}), whose other live sleeves hold "
            f"{_usd(other)}"
        )
    )
    statement = (
        f"The other {other_strategy_rows} strategy rows are valued at broker account "
        f"NAVs, net of any debit balance inside the account. The {strategy_name} row "
        f"is gross by definition: its value is the sleeve NAV before the account's "
        f"unallocated cash and margin bucket. The liability line makes the net "
        f"equivalent visible: gross {_usd(gross)} plus the bucket {_usd(bucket)} is "
        f"{_usd(net)}, which {equal_clause}."
    )
    return {
        "cutoff_date": cutoff,
        "gross_sleeve_nav_usd": _text(gross),
        "liability_line": {
            "label": "Unallocated cash and margin bucket of the parent account",
            "kind": kind,
            "account_id": str(facts.document["perimeter_account"]["account_id"]),
            "value_usd": _text(bucket),
        },
        "net_equivalent_usd": _text(net),
        "identity": identity,
        "leverage_disclosure": dict(leverage_pointer),
        "other_strategy_rows": other_strategy_rows,
        "gross_by_definition": True,
        "statement": statement,
    }


def account_basis_label(
    *, account_name: str, retired_sleeves: int, bucket: Decimal
) -> str:
    """The basis label of the parent-account figures, from what the account holds."""

    parts = [f"whole account {account_name}"]
    holds: list[str] = []
    if retired_sleeves:
        holds.append("retired sleeves")
    holds.append(
        "the unallocated margin bucket" if bucket < 0 else "the unallocated cash bucket"
    )
    parts.append("incl. " + " and ".join(holds))
    return " ".join(parts)


def retired_sleeves(
    facts: SleeveFacts, *, sleeve_account_id: str, cutoff: str
) -> list[dict[str, Any]]:
    """The other sleeves of the parent account: what its whole-account series also holds."""

    result: list[dict[str, Any]] = []
    for item in facts.document["sleeves"]:
        if item["account_id"] == sleeve_account_id:
            continue
        result.append(
            {
                "sleeve_id": str(item["sleeve_id"]),
                "strategy_id": str(item["strategy_id"]),
                "account_id": str(item["account_id"]),
                "first_day": str(item["first_day"]),
                "last_day": str(item["last_day"]),
                "live_at_cutoff": _sleeve_value_at(item, cutoff) != 0,
            }
        )
    return sorted(result, key=lambda entry: (entry["first_day"], entry["sleeve_id"]))


def account_basis_block(
    row: Mapping[str, Any],
    *,
    strategy_id: str,
    account_id: str,
    label: str,
) -> dict[str, Any]:
    """The parent-account figures of a strategy row, as the row's engine computed them.

    ``row`` is the strategy row built from the UNRESTATED parent series; the figures are
    copied under their own names, so that each of them is exactly what the row published
    before the restatement.
    """

    figures = {name: row[name] for name in ACCOUNT_BASIS_FIELDS if name in row}
    annualization = row.get("account_live_cagr_annualization")
    window_start = row.get("account_live_observed_start")
    window_end = row.get("account_live_observed_end")
    window_days = None
    if isinstance(annualization, Mapping):
        window_start = annualization.get("window_start", window_start)
        window_end = annualization.get("window_end", window_end)
        window_days = annualization.get("window_days")
    if window_start is None or window_end is None:
        raise DisclosureError(
            f"the account-basis series of {strategy_id} has no observed window"
        )
    if window_days is None:
        window_days = _days_between(str(window_start), str(window_end))
    return {
        "basis": ACCOUNT_BASIS_FALLBACK,
        "label": label,
        "strategy_id": strategy_id,
        "account_id": account_id,
        "window": {
            "start": str(window_start),
            "end": str(window_end),
            "days": window_days,
        },
        "observations": row.get("account_live_return_observations"),
        "series": {
            "schema": row.get("account_live_return_series_schema"),
            "sha256": row.get("account_live_return_series_sha256"),
        },
        "figures": figures,
    }


def account_basis_ref(*, account_id: str, series_sha256: object, label: str) -> dict:
    """The pointer a restated row's ``return_scope`` carries to its account-basis block."""

    return {
        "block": "account_summary.account_basis",
        "account_id": account_id,
        "series_sha256": series_sha256,
        "label": label,
        "disclosure_block": "strategy_basis_disclosure",
    }


def leverage_pointer(
    rows: Sequence[Mapping[str, Any]], *, account_id: str, strategy_id: str
) -> dict[str, Any]:
    """Where the account's leverage observations are, counted by data class."""

    counts: dict[str, int] = {}
    for item in rows:
        if item.get("account_id") != account_id:
            continue
        if item.get("strategy_id") not in {strategy_id, None}:
            continue
        key = str(item.get("data_class"))
        counts[key] = counts.get(key, 0) + 1
    return {
        "table": "leverage_disclosures",
        "account_id": account_id,
        "strategy_id": strategy_id,
        "row_counts_by_data_class": dict(sorted(counts.items())),
    }


def _chain(returns: Sequence[Decimal]) -> Decimal:
    product = Decimal(1)
    for item in returns:
        product *= 1 + item
    return product - 1


def _flow_legs_agree(
    facts: SleeveFacts, sleeve: Mapping[str, Any], days: Sequence[Mapping[str, Any]]
) -> None:
    """The daily flow columns are the paired legs of the facts, date for date."""

    account_id = str(sleeve["account_id"])
    cutoff = str(days[-1]["day"])
    legs: dict[str, list[Decimal]] = {}
    for leg in facts.document.get("flows", []):
        if leg["account_id"] != account_id or str(leg["nav_recognition_date"]) > cutoff:
            continue
        slot = legs.setdefault(
            str(leg["nav_recognition_date"]), [Decimal(0), Decimal(0)]
        )
        slot[0 if leg["class"] == "INTERNAL_IN" else 1] += _dec(leg["amount"], "leg")
    seen: set[str] = set()
    for item in days:
        day = str(item["day"])
        inflow, outflow = legs.get(day, [Decimal(0), Decimal(0)])
        if inflow != _dec(item["flow_in"], "flow_in") or -outflow != _dec(
            item["flow_out"], "flow_out"
        ):
            raise DisclosureError(
                f"the flow legs of the facts differ from the daily flows on {day}"
            )
        seen.add(day)
    if set(legs) - seen:
        raise DisclosureError("the facts hold a flow leg on a day without a NAV row")


def sleeve_to_audit_bridge(
    facts: SleeveFacts,
    *,
    sleeve_account_id: str,
    opening_date: str,
    cutoff: str,
    sleeve_return: Decimal,
) -> dict[str, Any]:
    """The audit's convention to the panel's, in three steps closed against the trace.

    The audit measures the sleeve's own P&L (before allocated costs) from the day it is
    funded, with every inflow at the start of its day and every outflow at its end.  The
    panel's sleeve series (ADR 0002) is the NAV after allocated costs, opens at the first
    closing NAV (the entry day is outside the window), and places date-only flows at the
    end of their day.  Each step changes one thing; the chain ends on a return recomputed
    from the facts under the panel's convention, and the trace must agree with it.
    """

    sleeve = _sleeve_by_account(facts, sleeve_account_id)
    days = _days_through(sleeve, cutoff)
    if len(days) < 2:
        raise DisclosureError("the sleeve has no day after its entry day")
    if str(days[0]["day"]) != opening_date:
        raise DisclosureError(
            "the sleeve's first facts day differs from the restated opening date: "
            f"{days[0]['day']} against {opening_date}"
        )
    if str(days[-1]["day"]) != cutoff:
        raise DisclosureError("the sleeve facts do not reach the cutoff day")
    if _dec(days[0]["nav_prev"], "entry nav_prev") != 0:
        raise DisclosureError("the sleeve's first facts day is not an entry day")
    _flow_legs_agree(facts, sleeve, days)
    with localcontext() as context:
        context.prec = _PRECISION
        audit: list[Decimal] = []
        after: list[Decimal] = []
        end_of_day: list[Decimal] = []
        previous = Decimal(0)
        allocated = Decimal(0)
        flow_days: list[dict[str, str]] = []
        for index, item in enumerate(days):
            flow_in = _dec(item["flow_in"], "flow_in")
            net_flow = flow_in + _dec(item["flow_out"], "flow_out")
            start = _dec(item["nav_prev"], "nav_prev")
            own_base = start + flow_in
            if own_base <= 0:
                raise DisclosureError(
                    f"the audit's return base is not positive on {item['day']}"
                )
            audit.append(
                (_dec(item["nav_end"], "nav_end") - start - net_flow) / own_base
            )
            closing = _dec(item["nav_end_after_alloc"], "nav_end_after_alloc")
            if previous + flow_in <= 0:
                raise DisclosureError(
                    f"the after-allocation return base is not positive on {item['day']}"
                )
            after.append((closing - previous - net_flow) / (previous + flow_in))
            allocated += _dec(item["alloc_total"], "alloc_total")
            if index > 0:
                if previous <= 0:
                    raise DisclosureError(
                        "the end-of-day convention has no opening base on "
                        f"{item['day']}"
                    )
                end_of_day.append((closing - net_flow) / previous - 1)
                if net_flow != 0:
                    flow_days.append(
                        {"date": str(item["day"]), "net_flow_usd": _text(net_flow)}
                    )
            previous = closing
        folded = _dec(days[-1]["nav_end_after_alloc"], "closing NAV") - _dec(
            days[-1]["nav_end"], "closing NAV"
        )
        if abs(folded - allocated) > IDENTITY_TOLERANCE_USD:
            raise DisclosureError(
                "the allocated costs do not account for the gap between the facts' NAV "
                "series"
            )
        audit_return = _chain(audit)
        after_return = _chain(after)
        excluded_return = _chain(after[1:])
        panel_return = _chain(end_of_day)
        residual = sleeve_return - panel_return
        steps = [
            (
                "ALLOCATED_COSTS",
                "Allocated interest, fees and dividend timing folded into the sleeve NAV",
                after_return - audit_return,
            ),
            (
                "ENTRY_DAY_EXCLUSION",
                "The epoch opens at the first closing NAV; the entry day is outside the window",
                excluded_return - after_return,
            ),
            (
                "END_OF_DAY_FLOW_CONVENTION",
                "ADR 0002: date-only flows are placed at the end of their day",
                panel_return - excluded_return,
            ),
        ]
        if abs(residual) > RESIDUAL_TOLERANCE:
            raise DisclosureError(
                "the three-convention bridge does not close against the sleeve "
                f"series: residual {_text(residual)}"
            )
        entry_return = after[0]
        step_rows = [
            {"step": name, "description": text, "effect": _text(effect)}
            for name, text, effect in steps
        ]
        step_rows[0]["allocated_cost_usd"] = _text(allocated)
        step_rows[1]["entry_day"] = str(days[0]["day"])
        step_rows[1]["entry_day_return"] = _text(entry_return)
        step_rows[2]["flow_days"] = flow_days  # type: ignore[assignment]
        return {
            "direction": "AUDIT_TO_PANEL",
            "window": {"start": opening_date, "end": cutoff},
            "audit_convention_return": _text(audit_return),
            "steps": step_rows,
            "panel_return": _text(sleeve_return),
            "recomputed_panel_return": _text(panel_return),
            "residual": _text(residual),
            "residual_tolerance": _text(RESIDUAL_TOLERANCE),
            "closure_status": "CLOSED",
        }


def account_to_sleeve_bridge(
    facts: SleeveFacts,
    *,
    account_basis: Mapping[str, Any],
    sleeve_account_id: str,
    sleeve_value: Decimal,
    sleeve_return: Decimal,
    sleeve_opening_date: str,
    cutoff: str,
    ledger_account_value: Decimal,
) -> dict[str, Any]:
    """The account basis to the sleeve basis: value, return and the two windows."""

    figures = account_basis["figures"]
    account_value = _dec(figures["current_value_usd"], "account-basis value")
    if abs(account_value - ledger_account_value) > IDENTITY_TOLERANCE_USD:
        raise DisclosureError(
            "the account-basis value is not the parent account's NAV: "
            f"{_text(account_value)} against {_text(ledger_account_value)}"
        )
    account_return = _dec(
        figures["account_live_cumulative_return"], "account-basis return"
    )
    bucket = _bucket_at(facts, cutoff)
    sleeve = _sleeve_by_account(facts, sleeve_account_id)
    others = sum(
        (
            _sleeve_value_at(item, cutoff)
            for item in facts.document["sleeves"]
            if item is not sleeve
        ),
        Decimal(0),
    )
    rebuilt = account_value - bucket - others
    if abs(rebuilt - sleeve_value) > IDENTITY_TOLERANCE_USD:
        raise DisclosureError(
            "the account value less the bucket and the other sleeves is not the "
            f"sleeve value: {_text(rebuilt)} against {_text(sleeve_value)}"
        )
    window = account_basis["window"]
    start = str(window["start"])
    with localcontext() as context:
        context.prec = _PRECISION
        gap_days = (
            _days_between(start, sleeve_opening_date)
            if start <= sleeve_opening_date
            else -_days_between(sleeve_opening_date, start)
        )
        difference = sleeve_return - account_return
    return {
        "value": {
            "account_value_usd": _text(account_value),
            "unallocated_bucket_usd": _text(bucket),
            "other_live_sleeves_usd": _text(others),
            "sleeve_value_usd": _text(sleeve_value),
            "relation": (
                "account value minus the unallocated bucket minus the other live "
                "sleeves equals the sleeve value"
            ),
        },
        "return": {
            "account_cumulative_return": _text(account_return),
            "sleeve_cumulative_return": _text(sleeve_return),
            "difference": _text(difference),
            "account_window": {"start": start, "end": str(window["end"])},
            "sleeve_window": {"start": sleeve_opening_date, "end": cutoff},
            "window_start_difference_days": gap_days,
        },
        "account_series_also_holds": retired_sleeves(
            facts, sleeve_account_id=sleeve_account_id, cutoff=cutoff
        ),
    }


def _days_between(first: str, second: str) -> int:
    return (date.fromisoformat(second) - date.fromisoformat(first)).days


def sleeve_cumulative_return(restatement: strategy_basis.SleeveRestatement) -> Decimal:
    """The restated series' compounded return: the same rows the report's row stands on."""

    with localcontext() as context:
        context.prec = _PRECISION
        return _chain(
            [_dec(row["return"], "restated return") for row in restatement.rows]
        )
