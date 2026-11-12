"""#893: the relative payload-budget rule.

The committed ``payload_budget.json`` gave every block a fixed limit of measured + 1 %.
A block that grows with the number of days (every time-indexed block) outgrew that
headroom within a week or two, and each refusal cost a code edit, a new frozen commit and
a full rerun.  The rule here computes each block's limit from the *previous committed
release's* measured sizes, so nothing is edited weekly:

    relative(block) = ceil(previous(block) x (1 + GROWTH_PER_DAY x added_days + MARGIN) / 1000) x 1000
    limit(block)    = min(ceiling(block), max(file_limit(block), relative(block)))
    ceiling(block)  = ceil(CEILING_FACTOR x committed_bytes(block) / 1000) x 1000

``file_limit`` is the explicit, reviewed figure in ``payload_budget.json`` (raised only in
the change that deliberately grows a block).  ``ceiling`` is the absolute bound that stays
in code: a block that grows past it needs a code change on purpose, however slowly it got
there.  Without a previous release the file limits apply unchanged; when the previous
release is no longer discoverable (the candidate already is the current release) only the
ceiling applies.

Pure functions; nothing here reads a file.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
import math
from typing import Any

# Calibrated on the committed panels, not on the window arithmetic: a block that holds one
# row per observation grows 1/N per added day on an N-day window (about 0.14 %), but R2.2 ->
# R3 (50 added days) grew factor_analysis 16.6 %, leverage_disclosures 21.5 % and
# account_manifest 11.7 % because the new data also changed the live/stale lens statuses
# (0.43 %/day at worst, tools/panel_payload_size.py over the retained generations).  0.5 %
# per added calendar day covers that; the code ceiling bounds the accumulation.
GROWTH_PER_DAY = 0.005
MARGIN = 0.01
CEILING_FACTOR = 1.5
ROUND_TO = 1000

# payload_budget.json ``physical`` key -> ``committed_bytes`` key
PHYSICAL_COMMITTED = {
    "total_bytes": "total_physical",
    "index_html_bytes": "index_html",
    "dashboard_data_json_bytes": "dashboard_data_json",
    "dashboard_data_js_bytes": "dashboard_data_js",
}


@dataclass(frozen=True)
class ContentAllowance:
    """A declared content change: ``block`` may grow by ``extra_fraction`` beyond the
    day-based allowance in the release whose panel schema version becomes
    ``schema_version`` (and only there: the previous release had another version).

    A ticket that adds payload through code (a new column, a new table) adds its entry
    here in the same change, with ``reason`` naming the ticket, so the weekly rule does
    not refuse the first release that carries the code (the F4 freeze of R3).  The code
    ceiling still binds.
    """

    block: str
    schema_version: int
    extra_fraction: float
    reason: str


CONTENT_ALLOWANCES: tuple[ContentAllowance, ...] = ()


def active_allowances(
    previous_schema: Any,
    schema: Any,
    table: tuple[ContentAllowance, ...] | None = None,
) -> dict[str, float]:
    """Extra fraction per block for a release that changes the panel schema version."""

    if previous_schema == schema or not isinstance(schema, int):
        return {}
    out: dict[str, float] = {}
    for entry in CONTENT_ALLOWANCES if table is None else table:
        if entry.schema_version == schema:
            out[entry.block] = out.get(entry.block, 0.0) + entry.extra_fraction
    return out


def _round_up(value: float) -> int:
    return math.ceil(value / ROUND_TO) * ROUND_TO


def added_days(previous_as_of: Any, as_of: Any) -> int | None:
    """Calendar days from the previous release's as-of date to this one.

    ``None`` when either date is unusable or the previous release is *later* than this
    one: that is not a predecessor, so it cannot set a baseline.
    """

    try:
        previous = date.fromisoformat(str(previous_as_of)[:10])
        current = date.fromisoformat(str(as_of)[:10])
    except ValueError:
        return None
    days = (current - previous).days
    return days if days >= 0 else None


def relative_limit(previous: int, days: int, extra: float = 0.0) -> int:
    return _round_up(previous * (1 + GROWTH_PER_DAY * days + MARGIN + extra))


def ceiling(committed: int) -> int:
    return _round_up(committed * CEILING_FACTOR)


def derive_budget(
    budget: Mapping[str, Any],
    previous_blocks: Mapping[str, int] | None,
    previous_physical: Mapping[str, int] | None,
    days: int | None,
    *,
    previous_unknown: bool = False,
    allowances: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """The effective budget: same shape as ``payload_budget.json``, limits recomputed.

    ``previous_blocks`` is None and ``previous_unknown`` False: no previous release, the
    file limits stand.  ``previous_unknown`` True: the previous release cannot be found,
    only the ceiling applies.
    """

    committed = budget.get("committed_bytes", {})
    committed_blocks = committed.get("blocks", {})
    derived: dict[str, Any] = {
        "blocks": dict(budget["blocks"]),
        "physical": dict(budget["physical"]),
        "unlisted_block_max_bytes": budget["unlisted_block_max_bytes"],
        "other_blocks_max_bytes": budget["other_blocks_max_bytes"],
    }
    if previous_blocks is None and not previous_unknown:
        return derived

    extra_by_block = allowances or {}

    def pick(
        file_limit: int,
        committed_size: int | None,
        prev: int | None,
        extra: float = 0.0,
    ) -> int:
        top = ceiling(committed_size if committed_size else file_limit)
        if previous_unknown or prev is None or days is None:
            return top if previous_unknown else file_limit
        return min(top, max(file_limit, relative_limit(prev, days, extra)))

    for name, file_limit in budget["blocks"].items():
        derived["blocks"][name] = pick(
            file_limit,
            committed_blocks.get(name),
            (previous_blocks or {}).get(name),
            extra_by_block.get(name, 0.0),
        )
    for key, file_limit in budget["physical"].items():
        derived["physical"][key] = pick(
            file_limit,
            committed.get(PHYSICAL_COMMITTED.get(key, "")),
            (previous_physical or {}).get(key),
            max(extra_by_block.values(), default=0.0),
        )
    return derived
