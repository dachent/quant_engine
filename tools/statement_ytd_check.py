"""Cross-check John Hancock statement contributions against the plan site's YTD line.

The quarterly statements give contributions as period aggregates; the plan site's "My
Contributions" page prints a year-to-date total.  For the year the captured page covers,
the CONTRIBUTION flows of the generation must equal that total (or, when the page was taken
after the generation's last statement end, may not exceed it); any other year is DISCLOSED as
not covered, never silently passed.  A pure function of its inputs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
import re
from typing import Any


class YtdContributionError(ValueError):
    """The statements and the plan page disagree about the year's contributions."""


_AS_OF = re.compile(r"as of (\d\d/\d\d/\d{4})")
_ROTH = re.compile(r"Roth\s+\$([\d,]+\.\d\d)")


def ytd_contribution_check(
    page_text: str, flows: Sequence[Mapping[str, Any]], coverage_end: date
) -> dict[str, Any]:
    as_of = _AS_OF.search(page_text)
    roth = _ROTH.search(page_text)
    if as_of is None or roth is None:
        raise YtdContributionError("contributions page layout not recognised")
    page_day = datetime.strptime(as_of.group(1), "%m/%d/%Y").date()
    page_total = Decimal(roth.group(1).replace(",", ""))
    by_year: dict[int, Decimal] = {}
    for flow in flows:
        if flow.get("flow_type") != "CONTRIBUTION":
            continue
        day = date.fromisoformat(str(flow["event_date"]))
        by_year[day.year] = by_year.get(day.year, Decimal(0)) + Decimal(
            str(flow["amount"]["value"])
        )
    year = page_day.year
    flows_total = by_year.get(year, Decimal(0))
    if page_day <= coverage_end:
        if flows_total != page_total:
            raise YtdContributionError(
                f"{year} contribution flows differ from the plan page year-to-date total"
            )
        status = "COVERED_EQUAL"
    else:
        # contributions after the last statement end are outside the generation
        if flows_total > page_total:
            raise YtdContributionError(
                f"{year} contribution flows exceed the plan page year-to-date total"
            )
        status = (
            "COVERED_EQUAL"
            if flows_total == page_total
            else "COVERED_PAGE_AFTER_COVERAGE_HIGHER"
        )
    return {
        "flows_total": str(flows_total),
        "not_covered_years": sorted(y for y in by_year if y != year),
        "page_as_of": page_day.isoformat(),
        "page_total": str(page_total),
        "status": status,
        "year": year,
    }
