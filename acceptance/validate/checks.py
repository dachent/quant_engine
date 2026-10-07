"""Deterministic acceptance checks over one control-panel generation (plan v5 4.5B/C/E).

Each check reads the generation's *outputs* (the published report and panel payloads,
the shell, the candidate record) and emits findings. None of it calls or imports the
production report or panel builders. Expected values, when supplied, come from the
standalone derivations (``--expected``), never from these outputs.

Classes (plan 4.5B):

``BLOCKER_INVARIANT``   mathematical or logical contradiction
``BLOCKER_AUTHORITY``   wrong, unadmitted or stale identity, status or source
``BLOCKER_REGRESSION``  a fixed-baseline expected value regressed
``NOT_READY``           required data unavailable and honestly labelled
``WARNING``             plausibility lint; never blocks a run (plan 4.5E)

A check that cannot evaluate raises; the runner turns that into a blocking
``validator.check_error`` finding so a schema drift fails closed instead of passing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, cast

from . import shell
from .generation import (
    APP,
    PAYLOAD_FILES,
    CANDIDATE,
    CENSUS_CONTRACT,
    FRESHNESS,
    GENERATION_MANIFEST,
    INDEX,
    PANEL,
    PANEL_SCRIPT,
    REPORT,
    ContractCensus,
    Generation,
    GenerationError,
    load_contract_census,
)

BLOCKER_INVARIANT = "BLOCKER_INVARIANT"
BLOCKER_AUTHORITY = "BLOCKER_AUTHORITY"
BLOCKER_REGRESSION = "BLOCKER_REGRESSION"
NOT_READY = "NOT_READY"
WARNING = "WARNING"
CLASSES = (BLOCKER_INVARIANT, BLOCKER_AUTHORITY, BLOCKER_REGRESSION, NOT_READY, WARNING)
BLOCKERS = frozenset({BLOCKER_INVARIANT, BLOCKER_AUTHORITY, BLOCKER_REGRESSION})

USD_TOLERANCE = Decimal("0.01")
RETURN_TOLERANCE = Decimal("1e-8")
# The custody producer adds money events exactly, at 100 significant digits
# (``tools/portfolio_baseline._EXACT_PRECISION``); a check that compares a sum with a
# producer value by equality must add at the same precision, not at the ambient 28. The
# acceptance layer does not import product code, so the literal is pinned to the
# producer's by ``tests/acceptance/test_n56_capital_labels.py``.
EXACT_PRECISION = 100
# C1 section 5-5: the published label of an epoch TWR that splices hash-bound HPR
# gap spans, and the only span length the epoch HPR receipt admits.
EPOCH_RETURN_LABEL = "EPOCH_CUMULATIVE_TWR_WITH_HASH_BOUND_HPR_GAPS"
EPOCH_SPAN_DAYS = 2
WINDOW_START_SLACK_DAYS = 5
ONE_YEAR_DAYS = 365

FINDINGS_SCHEMA = "qe-validate-generation/1"

_AVAILABLE_PREFIXES = (
    "AVAILABLE",
    "EVIDENCED",
    "DESCRIPTIVE",
    "PASS",
    "OK",
    "ACCEPTED",
    "COMPLETE",
    "VERIFIED",
    "GOVERNED",
)
_NOT_READY_VALUES = frozenset({"REQUIRED_NOT_READY", "NOT_READY"})
_UNADMITTED_FLOW_PATHS = frozenset(
    {"NO_ACCEPTED_FACT_PATH", "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF"}
)
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_NUMBER = r"(\d+|" + "|".join(_NUMBER_WORDS) + r")"
_UNIVERSE_QUALIFIERS = (
    r"current|active|live|funded|included|systematic|tracked|product|portfolio|"
    r"governed|mapped|running"
)
# "Nine current strategies", "9 active live strategies" (anywhere, payload prose too)
_QUALIFIED_COUNT = re.compile(
    rf"(?i)\b{_NUMBER}\s+(?:(?:{_UNIVERSE_QUALIFIERS})\s+){{1,2}}strateg(?:y|ies)\b"
)
# "7 strategies", "Strategies: 9", "strategies (9)", "strategy count = 9" (shell only)
_BARE_COUNT = re.compile(rf"(?i)\b{_NUMBER}\s+strategies\b")
_LABEL_COUNT = re.compile(
    rf"(?i)\bstrateg(?:y|ies)(?:\s+count)?\s*(?:[:=(]|\bof\b)\s*{_NUMBER}\b"
)
_MACHINE_WORD = re.compile(
    r"\b[a-z]+(?:_[a-z]+)+:[A-Za-z0-9_:.-]+|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"
)


# --------------------------------------------------------------------------- model


@dataclass(frozen=True)
class Finding:
    check_id: str
    klass: str
    entity: str
    observed: Any
    expected: Any
    file: str
    json_path: str
    defect_ids: tuple[str, ...] = ()
    message: str = ""

    @property
    def pointer(self) -> str:
        return f"{self.file}#{self.json_path}"

    def as_json(self) -> dict[str, Any]:
        """The contract shape (state/briefs/findings-contract.md): exactly 7 keys."""

        return {
            "check_id": self.check_id,
            "class": self.klass,
            "entity": self.entity,
            "observed": jsonable(self.observed),
            "expected": jsonable(self.expected),
            "pointer": self.pointer,
            "defect_ids": sorted(set(self.defect_ids)),
        }

    def sort_key(self) -> tuple[int, str, str, str, str]:
        return (
            CLASSES.index(self.klass),
            self.check_id,
            self.entity,
            self.file,
            self.json_path,
        )


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format_decimal(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(jsonable(item) for item in value)
    return value


def format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


@dataclass
class Context:
    gen: Generation
    cutoff: date
    cutoff_source: str
    expected: list[dict[str, Any]] | None = None
    expected_not_derivable: list[dict[str, Any]] = field(default_factory=list)
    corrections: list[dict[str, Any]] | None = None
    _cache: dict[str, Any] = field(default_factory=dict)
    # The repository whose ``contracts/`` the census check reads (upstream authority,
    # never a production output). ``None`` is the checkout that holds this module,
    # which is where ``tools/validate_generation.py`` runs from.
    repo_root: Path | None = None

    @property
    def report(self) -> dict[str, Any]:
        return self.gen.report

    @property
    def panel(self) -> dict[str, Any]:
        return self.gen.panel


# ------------------------------------------------------------------------ helpers


def exact_sum(values: Iterable[Decimal]) -> Decimal:
    """Add money at the producer's exact precision, whatever the ambient context is."""

    with localcontext() as context:
        context.prec = EXACT_PRECISION
        return sum(values, Decimal(0))


def to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = Decimal(value.strip())
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() else None
    return None


def to_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def rows(document: Any, key: str) -> list[dict[str, Any]]:
    value = document.get(key) if isinstance(document, Mapping) else None
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def node(document: Any, *keys: str | int) -> Any:
    current = document
    for key in keys:
        if isinstance(key, int):
            if not isinstance(current, list) or key >= len(current):
                return None
            current = current[key]
        else:
            if not isinstance(current, Mapping):
                return None
            current = current.get(key)
    return current


def is_available(status: Any) -> bool:
    return isinstance(status, str) and status.upper().startswith(_AVAILABLE_PREFIXES)


def is_unavailable(status: Any) -> bool:
    if not isinstance(status, str):
        return True
    text = status.upper()
    return text.startswith(("UNAVAILABLE", "FAILED", "BLOCKED", "NOT_", "NONE"))


def compound(returns: Iterable[Decimal]) -> Decimal:
    total = Decimal(1)
    for value in returns:
        total *= Decimal(1) + value
    return total - Decimal(1)


def windows_equivalent(
    first: tuple[date | None, date | None], second: tuple[date | None, date | None]
) -> bool:
    start_a, end_a = first
    start_b, end_b = second
    if end_a is None or end_b is None or end_a != end_b:
        return False
    if start_a is None or start_b is None:
        return True
    return abs((start_a - start_b).days) <= WINDOW_START_SLACK_DAYS


def iso_week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def weekdays_between(start: date, end: date) -> int:
    """Weekdays in the half-open interval ``(start, end]``."""

    if end <= start:
        return 0
    days = 0
    current = start + timedelta(days=1)
    while current <= end:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)
    return days


def spread(values: Sequence[Decimal]) -> Decimal:
    return max(values) - min(values) if values else Decimal(0)


def _finding(
    check_id: str,
    klass: str,
    entity: str,
    observed: Any,
    expected: Any,
    file: str,
    json_path: str,
    defect_ids: Sequence[str] = (),
    message: str = "",
) -> Finding:
    return Finding(
        check_id=check_id,
        klass=klass,
        entity=entity,
        observed=observed,
        expected=expected,
        file=file,
        json_path=json_path,
        defect_ids=tuple(defect_ids),
        message=message,
    )


# ----------------------------------------------------------------- derived indices


TableRow = tuple[int, dict[str, Any]]


def twr_rows(ctx: Context) -> list[TableRow]:
    return list(enumerate(rows(ctx.report, "twr_mwr")))


def manifest_rows(ctx: Context) -> list[TableRow]:
    """Every row of the panel's account manifest and its position, in panel order.

    Not an index: two epochs of one account and strategy that start together (a funded
    epoch re-cut from a blocked epoch's start) are two rows, and neither replaces the
    other. Whatever needs the row of ONE epoch asks ``manifest_match``.
    """

    cached = ctx._cache.get("manifest_rows")
    if isinstance(cached, list):
        return cached
    entries = list(enumerate(rows(node(ctx.panel, "account_manifest"), "rows")))
    ctx._cache["manifest_rows"] = entries
    return entries


def epoch_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    """The label of an epoch's row in a finding: ``account|strategy|effective_start``.

    A label only. Epochs that start together share it, so no row is ever matched or
    combined by it: that is ``epoch_identity`` (``manifest_match``, ``report_match``).
    """

    return (
        str(row.get("account_id")),
        str(row.get("strategy_id")),
        str(row.get("effective_start")),
    )


def current_accounts_by_strategy(ctx: Context) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for _, row in twr_rows(ctx):
        if row.get("effective_end") is None:
            mapping.setdefault(str(row.get("strategy_id")), []).append(
                str(row.get("account_id"))
            )
    return mapping


def strategy_names(ctx: Context) -> dict[str, str]:
    names: dict[str, str] = {}
    for row in rows(ctx.report, "strategy_summary"):
        if isinstance(row.get("strategy_name"), str):
            names[str(row.get("strategy_id"))] = row["strategy_name"]
    return names


def returns_by_strategy(ctx: Context) -> dict[str, list[tuple[date, Decimal, str]]]:
    cached = ctx._cache.get("returns_by_strategy")
    if isinstance(cached, dict):
        return cached
    series: dict[str, list[tuple[date, Decimal, str]]] = {}
    for row in rows(ctx.report, "strategy_returns"):
        day = to_date(row.get("date"))
        value = to_decimal(row.get("return"))
        if day is None or value is None:
            continue
        series.setdefault(str(row.get("strategy_id")), []).append(
            (day, value, str(row.get("evidence_layer")))
        )
    for values in series.values():
        values.sort(key=lambda item: item[0])
    ctx._cache["returns_by_strategy"] = series
    return series


def compound_window(
    ctx: Context,
    strategy_id: str,
    start: date | None,
    end: date | None,
    layer: str = "ACCOUNT_LIVE",
) -> tuple[Decimal | None, int]:
    """The plain geometric compound of the daily rows dated in ``[start, end]``.

    Daily-only by design: it never splices an epoch HPR gap span. The benchmark
    account return and the dependence performance are daily-only views, so the
    epoch-basis comparisons use ``splice_window`` instead (C1 section 5-5).
    """

    selected = [
        value
        for day, value, evidence in returns_by_strategy(ctx).get(strategy_id, [])
        if evidence == layer
        and (start is None or day >= start)
        and (end is None or day <= end)
    ]
    if not selected:
        return None, 0
    return compound(selected), len(selected)


# ------------------------- blocked epoch rows and the contract census (L6a, N26)
#
# A ``BLOCKED_ADMISSION`` epoch is a governed outcome, not a numeric view: its epoch
# is an ``ASSIGNMENT_ONLY`` interval of the contract census, and spec R2 keeps those
# out of account TWR / MWR / flows (the oracle's account values are the funded
# epoch's values). The report publishes such a row with null windows and values, so
# comparing it with an account-level expected value can only give a false
# regression. The validator therefore skips a blocked row, and its matched panel
# manifest row, for the account-level expected metrics, but only when the census it
# reads from upstream authority confirms the row: the census must be the very bytes
# the expected baseline pins, hold an ASSIGNMENT_ONLY assignment with the row's
# (strategy_id, effective_start, effective_end), hold no FUNDED_MEMBERSHIP
# assignment with that tuple, and hold at least as many ASSIGNMENT_ONLY assignments
# as blocked rows share the tuple. Anything else fails closed: the row is compared
# exactly as before and ``identity.blocked_admission_census`` says why.
#
# What is skipped is exactly what was confirmed: a report row that is itself a blocked
# admission, and a panel manifest row that is itself blocked, that has the whole
# identity (account, strategy, effective_start, effective_end) of a confirmed blocked
# row. The coarser ``epoch_key`` (no ``effective_end``) is only a label: a funded epoch
# re-cut from the same start shares it, and keying the skip by it hid that funded row's
# contradiction with the oracle (lane epsilon fix round 1, a). The account-level window
# recompute (``recompute_metric``) leaves a confirmed blocked epoch out the same way;
# the strategy-level recompute is unchanged.
#
# The panel manifest rows keep their whole identity everywhere else too (fix round 2):
# ``manifest_rows`` is every row, none replaced by a row that shares its
# (account, strategy, effective_start), and a report row is matched with the manifest row
# of its own epoch (``manifest_match``, ``report_match``). The one-row-per-key index let
# a blocked manifest row listed after a funded one stand in for it: the funded row's
# trace, net capital and panel views left every aggregation and every match. A row whose
# whole identity the other table lacks is still compared with the row of its kind that
# shares its start, never with a row of the other kind.
#
# Fix round 3 (Codex lane-eps-r2) closes the two ways that left a disagreement unsaid.
# The stand-in was compared on its numbers alone, so two tables that differed only in an
# epoch's end produced no finding at all: every use of a stand-in is now a finding of its
# own (``epoch_disagreements``, said by ``check_epoch_identity``). And a row whose whole
# identity the other table holds only as a row of the OTHER kind used to be matched with
# that row all the same, a funded row read against a blocked one: it has no row now (no
# sibling stands in either, its epoch is there as something else) and the admission
# disagreement is the finding. Declining the match without a finding would have left the
# end-only case silent: no existing finding compares the report's ``twr_mwr`` rows with
# the panel's ``account_manifest`` rows (``preservation.report_to_panel`` maps
# ``twr_mwr`` to the panel's ``performance`` table).

CENSUS_CHECK = "identity.blocked_admission_census"
_ASSIGNMENT_ONLY = "ASSIGNMENT_ONLY"
_FUNDED_MEMBERSHIP = "FUNDED_MEMBERSHIP"
_CENSUS_DEFECTS = ("N26",)

EpochTuple = tuple[str, str | None, str | None]
EpochIdentity = tuple[str, str, str, str]


def is_blocked_admission(row: Mapping[str, Any]) -> bool:
    """A ``twr_mwr`` row whose two statuses both say the epoch was blocked."""

    return (
        row.get("twr_status") == "BLOCKED_ADMISSION"
        and row.get("epoch_admission_status") == "BLOCKED"
    )


def is_blocked_manifest_row(row: Mapping[str, Any]) -> bool:
    """A panel manifest row whose two statuses both say the epoch was blocked."""

    return (
        row.get("status") == "BLOCKED_ADMISSION"
        and row.get("admission_status") == "BLOCKED"
    )


def epoch_identity(row: Mapping[str, Any]) -> EpochIdentity:
    """An epoch's whole identity, on a report row and on a panel manifest row alike.

    ``epoch_key`` leaves out ``effective_end``; two epochs of one account and strategy
    that share a start (a re-cut) share it, so whatever must act on one epoch only
    uses this.
    """

    return (
        str(row.get("account_id")),
        str(row.get("strategy_id")),
        str(row.get("effective_start")),
        str(row.get("effective_end")),
    )


def _identity_index(
    entries: Iterable[TableRow],
) -> dict[EpochIdentity, list[TableRow]]:
    index: dict[EpochIdentity, list[TableRow]] = {}
    for entry in entries:
        index.setdefault(epoch_identity(entry[1]), []).append(entry)
    return index


# What a pair of rows that carry one epoch apart disagree about; also the field of the
# report row the finding points at.
END_DISAGREEMENT = "effective_end"
ADMISSION_DISAGREEMENT = "epoch_admission_status"


@dataclass(frozen=True)
class EpochMatch:
    """What the other table holds for one row's epoch.

    ``row`` is the row the comparisons read, ``None`` when there is none. ``disagreement``
    is ``None`` for an exact match (the row of the whole identity and of the row's
    kind), ``END_DISAGREEMENT`` when ``row`` is a stand-in that shares only the start,
    ``ADMISSION_DISAGREEMENT`` when the whole identity is held only by rows of the other
    kind (``row`` is ``None``: nothing is read across kinds). ``partners`` are the rows
    a disagreement is with.
    """

    row: TableRow | None
    disagreement: str | None = None
    partners: tuple[TableRow, ...] = ()


def _same_epoch(
    index: Mapping[EpochIdentity, Sequence[TableRow]],
    row: Mapping[str, Any],
    blocked: bool,
    is_blocked: Callable[[Mapping[str, Any]], bool],
) -> EpochMatch:
    """What ``index`` holds for ``row``'s own epoch.

    The entries with ``row``'s whole identity; among those the last one that is blocked
    exactly when ``row`` is (a funded and a blocked row can share even the whole
    identity): an exact match. Entries with the whole identity but none of ``row``'s
    kind: no row at all, and an ``ADMISSION_DISAGREEMENT`` with every one of them. The
    epoch of the other kind is never ``row``'s row, and no sibling stands in for it
    either: its own epoch is there, as something else (fix round 3: this used to fall
    back to the entries of the other kind, so a funded row was read against a blocked
    one). When no entry has the whole identity, the last entry that is blocked exactly
    when ``row`` is and shares only its (account, strategy, effective_start) stands in,
    with an ``END_DISAGREEMENT``: a table that carries the epoch with another end
    contradicts the other table, which a comparison of numbers alone would never say
    (fix round 3). Nothing qualifies: no row, no disagreement.
    """

    own = index.get(epoch_identity(row))
    if own:
        alike = [entry for entry in own if is_blocked(entry[1]) == blocked]
        if alike:
            return EpochMatch(alike[-1])
        return EpochMatch(None, ADMISSION_DISAGREEMENT, tuple(own))
    key = epoch_key(row)
    sharing = [
        entry
        for identity, group in index.items()
        if identity[:3] == key
        for entry in group
        if is_blocked(entry[1]) == blocked
    ]
    if not sharing:
        return EpochMatch(None)
    stand_in = max(sharing, key=lambda entry: entry[0])
    return EpochMatch(stand_in, END_DISAGREEMENT, (stand_in,))


def _manifest_epoch(ctx: Context, row: Mapping[str, Any]) -> EpochMatch:
    index = ctx._cache.get("manifest_by_identity")
    if not isinstance(index, dict):
        index = _identity_index(manifest_rows(ctx))
        ctx._cache["manifest_by_identity"] = index
    return _same_epoch(index, row, is_blocked_admission(row), is_blocked_manifest_row)


def _report_epoch(ctx: Context, row: Mapping[str, Any]) -> EpochMatch:
    index = ctx._cache.get("twr_by_identity")
    if not isinstance(index, dict):
        index = _identity_index(twr_rows(ctx))
        ctx._cache["twr_by_identity"] = index
    return _same_epoch(index, row, is_blocked_manifest_row(row), is_blocked_admission)


def manifest_match(ctx: Context, row: Mapping[str, Any]) -> TableRow | None:
    """The panel manifest row of one ``twr_mwr`` row: the row of its own epoch."""

    return _manifest_epoch(ctx, row).row


def report_match(ctx: Context, row: Mapping[str, Any]) -> TableRow | None:
    """The ``twr_mwr`` row of one panel manifest row: the same match, the other way."""

    return _report_epoch(ctx, row).row


@dataclass(frozen=True)
class EpochDisagreement:
    """One ``twr_mwr`` row and one panel manifest row that carry one epoch apart.

    ``kind`` is ``END_DISAGREEMENT`` (they share account, strategy and start, and no
    row of either table has the other's whole identity: one stands in for the other)
    or ``ADMISSION_DISAGREEMENT`` (they share the whole identity and only one of them is
    a blocked admission).
    """

    kind: str
    report: TableRow
    panel: TableRow


def epoch_disagreements(ctx: Context) -> list[EpochDisagreement]:
    """Every pair of rows, one per table, that carry one epoch apart.

    Each report row asks the panel and each panel row asks the report, so a row that only
    one table carries (a second end for the epoch) is found from the table that has it;
    a pair that both sides find is one pair. Ordered by report row, then panel row.
    """

    cached = ctx._cache.get("epoch_disagreements")
    if isinstance(cached, list):
        return cached
    pairs: dict[tuple[int, int], EpochDisagreement] = {}
    for entry in twr_rows(ctx):
        match = _manifest_epoch(ctx, entry[1])
        if match.disagreement is not None:
            for partner in match.partners:
                pairs[(entry[0], partner[0])] = EpochDisagreement(
                    match.disagreement, entry, partner
                )
    for entry in manifest_rows(ctx):
        match = _report_epoch(ctx, entry[1])
        if match.disagreement is not None:
            for partner in match.partners:
                pairs[(partner[0], entry[0])] = EpochDisagreement(
                    match.disagreement, partner, entry
                )
    found = [pairs[key] for key in sorted(pairs)]
    ctx._cache["epoch_disagreements"] = found
    return found


def _day_text(value: Any) -> str | None:
    """A date as comparable text: ``None`` stays ``None``; junk never matches a date."""

    if value is None:
        return None
    day = to_date(value)
    return day.isoformat() if day is not None else f"<unreadable:{value!r}>"


def _epoch_tuple(row: Mapping[str, Any]) -> EpochTuple:
    return (
        str(row.get("strategy_id")),
        _day_text(row.get("effective_start")),
        _day_text(row.get("effective_end")),
    )


@dataclass(frozen=True)
class CensusState:
    """The census when it can be trusted, else why it cannot."""

    census: ContractCensus | None
    problem: str | None


def _census_authority_digests(ctx: Context) -> tuple[set[str], bool]:
    """The census digests the expected baseline pins, and whether a pointer is unusable."""

    digests: set[str] = set()
    unusable = False
    for row in ctx.expected or []:
        pointers = row.get("authority_pointer")
        if not isinstance(pointers, list):
            continue
        for pointer in pointers:
            if (
                not isinstance(pointer, Mapping)
                or pointer.get("path") != CENSUS_CONTRACT
            ):
                continue
            digest = pointer.get("sha256")
            if isinstance(digest, str) and digest:
                digests.add(digest.lower())
            else:
                unusable = True
    return digests, unusable


def _read_census(ctx: Context) -> CensusState:
    if ctx.expected is None:
        return CensusState(
            None,
            "no expected baseline was supplied, so the census has no authority "
            "pointer to be bound to",
        )
    digests, unusable = _census_authority_digests(ctx)
    if unusable:
        return CensusState(
            None, f"an authority pointer for {CENSUS_CONTRACT} carries no sha256"
        )
    if not digests:
        return CensusState(
            None,
            f"the expected baseline carries no authority pointer for {CENSUS_CONTRACT} "
            f"(the census authority pin) on any of its {len(ctx.expected)} row(s)",
        )
    if len(digests) > 1:
        return CensusState(
            None,
            f"the expected baseline pins several digests for {CENSUS_CONTRACT}: "
            f"{sorted(digests)}",
        )
    root = (
        ctx.repo_root
        if ctx.repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    try:
        census = load_contract_census(root)
    except GenerationError as exc:
        return CensusState(None, str(exc))
    (pinned,) = digests
    if census.sha256 != pinned:
        return CensusState(
            None,
            f"the census contract sha256 {census.sha256} differs from the expected "
            f"baseline's authority pointer {pinned}",
        )
    return CensusState(census, None)


def census_state(ctx: Context) -> CensusState:
    cached = ctx._cache.get("census_state")
    if isinstance(cached, CensusState):
        return cached
    state = _read_census(ctx)
    ctx._cache["census_state"] = state
    return state


@dataclass(frozen=True)
class BlockedEpochs:
    """The blocked rows the census confirmed (skipped) and the findings about the rest."""

    skipped: frozenset[EpochIdentity]
    findings: tuple[Finding, ...]

    def skips_report_row(self, row: Mapping[str, Any]) -> bool:
        """Whether this ``twr_mwr`` row is a blocked admission the census confirmed."""

        return is_blocked_admission(row) and epoch_identity(row) in self.skipped

    def skips_manifest_row(self, row: Mapping[str, Any]) -> bool:
        """Whether this manifest row is the blocked row of a confirmed blocked epoch."""

        return is_blocked_manifest_row(row) and epoch_identity(row) in self.skipped


def _classify_blocked_epochs(ctx: Context) -> BlockedEpochs:
    blocked = [
        (position, row) for position, row in twr_rows(ctx) if is_blocked_admission(row)
    ]
    if not blocked:
        return BlockedEpochs(frozenset(), ())
    state = census_state(ctx)
    census = state.census
    if census is None:
        return BlockedEpochs(
            frozenset(),
            (
                _finding(
                    CENSUS_CHECK,
                    NOT_READY,
                    "contract-census",
                    state.problem,
                    "a readable contract census whose sha256 equals the expected "
                    "baseline's authority pointer",
                    CENSUS_CONTRACT,
                    "$.current_census.account_sleeves",
                    _CENSUS_DEFECTS,
                    f"the contract census cannot confirm the {len(blocked)} blocked "
                    f"epoch row(s) ({state.problem}); no blocked row is skipped",
                ),
            ),
        )
    assignment_only: Counter[EpochTuple] = Counter()
    funded: Counter[EpochTuple] = Counter()
    for assignment in census.assignments:
        key = (
            assignment.strategy_id,
            _day_text(assignment.effective_start),
            _day_text(assignment.effective_end),
        )
        if assignment.status == _ASSIGNMENT_ONLY:
            assignment_only[key] += 1
        elif assignment.status == _FUNDED_MEMBERSHIP:
            funded[key] += 1
    groups: dict[EpochTuple, list[tuple[int, dict[str, Any]]]] = {}
    for position, row in blocked:
        groups.setdefault(_epoch_tuple(row), []).append((position, row))
    skipped: set[EpochIdentity] = set()
    findings: list[Finding] = []
    for key, group in groups.items():
        reasons: list[str] = []
        if assignment_only[key] == 0:
            reasons.append(
                "the census holds no ASSIGNMENT_ONLY assignment with this "
                "(strategy_id, effective_start, effective_end)"
            )
        if funded[key] > 0:
            reasons.append(
                f"the census holds {funded[key]} FUNDED_MEMBERSHIP assignment(s) "
                "with this tuple"
            )
        if reasons:
            for position, row in group:
                findings.append(
                    _finding(
                        CENSUS_CHECK,
                        BLOCKER_AUTHORITY,
                        "|".join(epoch_key(row)),
                        {
                            "strategy_id": key[0],
                            "effective_start": row.get("effective_start"),
                            "effective_end": row.get("effective_end"),
                            "twr_status": row.get("twr_status"),
                            "epoch_admission_status": row.get("epoch_admission_status"),
                            "epoch_blocking_reason": row.get("epoch_blocking_reason"),
                        },
                        {
                            "census": CENSUS_CONTRACT,
                            "requires": "an ASSIGNMENT_ONLY assignment with this "
                            "(strategy_id, effective_start, effective_end) and no "
                            "FUNDED_MEMBERSHIP assignment with it",
                        },
                        REPORT,
                        f"$.twr_mwr[{position}]",
                        _CENSUS_DEFECTS,
                        "the blocked epoch row is not an ASSIGNMENT_ONLY census "
                        "interval: " + "; ".join(reasons),
                    )
                )
        elif len(group) > assignment_only[key]:
            findings.append(
                _finding(
                    CENSUS_CHECK,
                    BLOCKER_AUTHORITY,
                    "|".join(str(part) for part in key),
                    {
                        "blocked_rows": len(group),
                        "accounts": sorted(
                            str(row.get("account_id")) for _, row in group
                        ),
                    },
                    {"assignment_only_assignments_at_least": len(group)},
                    REPORT,
                    f"$.twr_mwr[{group[0][0]}]",
                    _CENSUS_DEFECTS,
                    f"{len(group)} blocked rows share this (strategy_id, "
                    f"effective_start, effective_end) but the census holds only "
                    f"{assignment_only[key]} ASSIGNMENT_ONLY assignment(s); none of "
                    "them is skipped",
                )
            )
        else:
            skipped.update(epoch_identity(row) for _, row in group)
    return BlockedEpochs(frozenset(skipped), tuple(findings))


def blocked_epochs(ctx: Context) -> BlockedEpochs:
    cached = ctx._cache.get("blocked_epochs")
    if isinstance(cached, BlockedEpochs):
        return cached
    result = _classify_blocked_epochs(ctx)
    ctx._cache["blocked_epochs"] = result
    return result


# ------------------------------------------ epoch HPR span receipts (C1 section 5-5)
#
# The contract forbids spans in the daily ``strategy_returns`` series and requires them
# in the epoch TWR: a declared missing source date is bridged by a two-day, no-flow
# holding-period span published in ``strategy_history_contract.epoch_hpr_receipts``.
# The validator re-implements the splice (it may not import the production return
# engine) and never trusts a receipt: each span is re-verified against the report's
# own daily rows and capital chain, and a span that fails is never spliced.


@dataclass(frozen=True)
class LiveRow:
    day: date
    value: Decimal
    capital: Decimal | None


@dataclass(frozen=True)
class ReceiptSpan:
    """A declared gap span ``(start, end]``; only a verified span is ever spliced.

    ``blocked`` marks an unverified span whose failure is already a BLOCKER-class
    ``returns.epoch_hpr_receipts_verified`` finding: a contradicted or unverifiable
    span, or one of a strategy that publishes its receipt twice. A span left
    unverified only because its receipt runs past the published rows is not blocked;
    that receipt is reported NOT_READY instead.
    """

    start: date
    end: date
    value: Decimal | None
    verified: bool
    blocked: bool = False


@dataclass(frozen=True)
class EpochReceipt:
    strategy_id: str
    position: int
    declares_spans: bool
    spans: tuple[ReceiptSpan, ...]


@dataclass(frozen=True)
class Splice:
    """Daily rows in a window times the verified spans ``(S, E]`` inside it.

    ``value`` is ``None`` when the window has no daily row or when the comparator
    declines (``declined`` says why): an edge strictly inside a span, or an
    unverified span inside the window, has no exact value. ``edge`` names the window
    edge (``"start"`` or ``"end"``) that lies strictly inside a span. ``reported`` is
    true when the only reason for the decline is an unverified span that already has
    a BLOCKER-class receipt finding, so ``check_strategy_return_views`` does not
    report that span twice.
    """

    value: Decimal | None
    rows: int
    spans: int
    declined: str | None = None
    edge: str | None = None
    reported: bool = False


_RECEIPTS_PATH = "$.strategy_history_contract.epoch_hpr_receipts"
_SPAN_EXPECTATION = (
    "a two-day, no-flow span (S, E] that starts on an observed daily row, replaces "
    "exactly the missing rows, and whose return equals the report's capital chain "
    "bc(first row after E) / (bc(S) x (1 + r_S)) - 1 (tolerance 1e-8)"
)


def live_rows(ctx: Context) -> dict[str, list[LiveRow]]:
    """Each strategy's ACCOUNT_LIVE daily rows in date order, with their capital."""

    cached = ctx._cache.get("live_rows")
    if isinstance(cached, dict):
        return cached
    series: dict[str, list[LiveRow]] = {}
    for row in rows(ctx.report, "strategy_returns"):
        day = to_date(row.get("date"))
        value = to_decimal(row.get("return"))
        if row.get("evidence_layer") != "ACCOUNT_LIVE" or day is None or value is None:
            continue
        series.setdefault(str(row.get("strategy_id")), []).append(
            LiveRow(day, value, to_decimal(row.get("beginning_capital")))
        )
    for values in series.values():
        values.sort(key=lambda item: item.day)
    ctx._cache["live_rows"] = series
    return series


def _receipt_finding(
    klass: str,
    entity: str,
    observed: Any,
    expected: Any,
    json_path: str,
    message: str,
) -> Finding:
    return _finding(
        "returns.epoch_hpr_receipts_verified",
        klass,
        entity,
        observed,
        expected,
        REPORT,
        json_path,
        ("B2",),
        message,
    )


def _span_problems(
    raw: Mapping[str, Any],
    start: date,
    end: date,
    value: Decimal | None,
    series: Sequence[LiveRow],
    previous_end: date | None,
) -> tuple[list[str], list[str], Decimal | None]:
    """Contradictions and missing evidence for one span, and its capital-chain return.

    The admission shape is the production one (two days, the missing source date
    inside, no subscription event, return above -1, S observed, no row in (S, E]),
    re-checked here, plus the capital chain: with no flow in the span the capital
    at the close of E (the beginning capital of the row dated E + 1) over the
    capital at the close of S (``bc(S) x (1 + r_S)``) is the span return.
    """

    contradictions: list[str] = []
    missing: list[str] = []
    span_days = raw.get("span_days")
    if span_days != EPOCH_SPAN_DAYS or (end - start).days != span_days:
        contradictions.append(
            f"span_days {span_days!r} is not {EPOCH_SPAN_DAYS} = E - S "
            f"({(end - start).days})"
        )
    missing_day = to_date(raw.get("missing_source_date"))
    if missing_day is None or not start < missing_day < end:
        contradictions.append(
            "missing_source_date does not fall strictly inside (S, E)"
        )
    events = raw.get("subscription_events_in_span")
    if isinstance(events, bool) or events != 0:
        contradictions.append(f"subscription_events_in_span is {events!r}, not 0")
    if value is None or value <= -1:
        contradictions.append("the span return is not a decimal above -1")
    if previous_end is not None and start <= previous_end:
        contradictions.append(f"the span overlaps a span ending {previous_end}")
    at_start = [row for row in series if row.day == start]
    if len(at_start) != 1:
        contradictions.append(f"S {start} is not exactly one observed daily row")
    inside = [row.day.isoformat() for row in series if start < row.day <= end]
    if inside:
        contradictions.append(f"daily rows lie inside the span: {', '.join(inside)}")
    after = next((row for row in series if row.day > end), None)
    chained: Decimal | None = None
    if after is None or after.day != end + timedelta(days=1):
        contradictions.append(
            "the first daily row after E is "
            + ("absent" if after is None else f"dated {after.day}")
            + f", not {end + timedelta(days=1)}: the span does not close the gap"
        )
    elif len(at_start) == 1 and sum(row.day == after.day for row in series) == 1:
        begin = at_start[0]
        if (
            begin.capital is None
            or after.capital is None
            or begin.capital <= 0
            or after.capital <= 0
            or begin.value <= -1
        ):
            missing.append(
                "the capital chain (beginning_capital of the rows dated S and E + 1) "
                "is unavailable, so the span return cannot be verified"
            )
        else:
            chained = after.capital / (begin.capital * (Decimal(1) + begin.value))
            chained -= Decimal(1)
            if value is not None and abs(value - chained) > RETURN_TOLERANCE:
                contradictions.append(
                    f"the span return differs from the capital chain {chained} by "
                    f"{value - chained}"
                )
    else:
        contradictions.append("duplicate daily rows make the capital chain ambiguous")
    return contradictions, missing, chained


def _verify_receipt(
    ctx: Context, strategy: str, position: int, outer: Mapping[str, Any]
) -> tuple[EpochReceipt, list[Finding]]:
    """Re-derive one strategy's epoch HPR receipt from the report's own rows."""

    pointer = f"{_RECEIPTS_PATH}[{position}].receipt"
    findings: list[Finding] = []
    receipt = outer.get("receipt")
    raw_spans = (
        receipt.get("span_return_receipts") if isinstance(receipt, Mapping) else None
    )
    if not isinstance(receipt, Mapping) or not isinstance(raw_spans, list):
        findings.append(
            _receipt_finding(
                BLOCKER_AUTHORITY,
                strategy,
                sorted(receipt) if isinstance(receipt, Mapping) else receipt,
                "a receipt object with a span_return_receipts list",
                pointer,
                "the epoch HPR receipt is malformed; none of its spans is spliced",
            )
        )
        return EpochReceipt(strategy, position, False, ()), findings
    series = live_rows(ctx).get(strategy, [])
    rows_end = series[-1].day if series else None
    receipt_start = to_date(receipt.get("observed_start"))
    receipt_end = to_date(receipt.get("observed_end"))
    # A receipt may describe evidence past the published daily rows (a ragged
    # perimeter past the calculation cutoff); spans starting at or after the last
    # published row are then never spliced, and its totals are not recomputable.
    # Nothing in the report can vouch for either, so the receipt is reported
    # NOT_READY (visible, never blocking) with what was left unverified.
    ragged = receipt_end is not None and rows_end is not None and receipt_end > rows_end
    if not series:
        findings.append(
            _receipt_finding(
                BLOCKER_AUTHORITY,
                strategy,
                {"strategy_returns_account_live_rows": 0},
                "the receipt's daily ACCOUNT_LIVE rows in strategy_returns",
                pointer,
                "an epoch HPR receipt names a strategy without daily rows",
            )
        )
    spans: list[ReceiptSpan] = []
    skipped: list[str] = []
    previous_end: date | None = None
    ordered: list[tuple[date, date, int, Mapping[str, Any]]] = []
    for index, raw in enumerate(raw_spans):
        span_path = f"{pointer}.span_return_receipts[{index}]"
        start = (
            to_date(raw.get("span_start_date")) if isinstance(raw, Mapping) else None
        )
        end = to_date(raw.get("span_end_date")) if isinstance(raw, Mapping) else None
        if not isinstance(raw, Mapping) or start is None or end is None or end <= start:
            findings.append(
                _receipt_finding(
                    BLOCKER_INVARIANT,
                    strategy,
                    raw,
                    _SPAN_EXPECTATION,
                    span_path,
                    "the span is not a dated interval (S, E] with S before E",
                )
            )
            continue
        ordered.append((start, end, index, raw))
    for start, end, index, raw in sorted(ordered, key=lambda item: item[:3]):
        value = to_decimal(raw.get("return"))
        if ragged and rows_end is not None and start >= rows_end:
            spans.append(ReceiptSpan(start, end, value, False))
            skipped.append(f"{start.isoformat()}..{end.isoformat()}")
            continue
        contradictions, missing, chained = _span_problems(
            raw, start, end, value, series, previous_end
        )
        previous_end = end if previous_end is None else max(previous_end, end)
        failed = bool(contradictions or missing)
        spans.append(ReceiptSpan(start, end, value, not failed, failed))
        if failed:
            findings.append(
                _receipt_finding(
                    BLOCKER_INVARIANT if contradictions else BLOCKER_AUTHORITY,
                    f"{strategy}|{start.isoformat()}..{end.isoformat()}",
                    {
                        "span_start_date": raw.get("span_start_date"),
                        "span_end_date": raw.get("span_end_date"),
                        "missing_source_date": raw.get("missing_source_date"),
                        "span_days": raw.get("span_days"),
                        "subscription_events_in_span": raw.get(
                            "subscription_events_in_span"
                        ),
                        "return": raw.get("return"),
                        "capital_chain_return": chained,
                    },
                    _SPAN_EXPECTATION,
                    f"{pointer}.span_return_receipts[{index}]",
                    "; ".join(contradictions + missing),
                )
            )
    if ragged and rows_end is not None:
        findings.append(
            _receipt_finding(
                NOT_READY,
                strategy,
                {
                    "observed_end": receipt.get("observed_end"),
                    "last_published_row": rows_end.isoformat(),
                    "unverified_spans": skipped,
                    "totals_recomputed": False,
                },
                "a receipt that ends on or before the report's last published daily "
                "row, so its spans and totals can be re-derived from the report's rows",
                f"{pointer}.observed_end",
                "the receipt runs past the report's last published daily row: its "
                "totals are not re-derived and its spans starting at or after that "
                "row are not verified",
            )
        )
    findings.extend(
        _receipt_totals(
            strategy, pointer, receipt, raw_spans, spans, series, ragged, receipt_start
        )
    )
    return EpochReceipt(strategy, position, bool(raw_spans), tuple(spans)), findings


def _receipt_totals(
    strategy: str,
    pointer: str,
    receipt: Mapping[str, Any],
    raw_spans: Sequence[Any],
    spans: Sequence[ReceiptSpan],
    series: Sequence[LiveRow],
    ragged: bool,
    receipt_start: date | None,
) -> list[Finding]:
    """The receipt's totals re-derive from the report's rows and the receipt's spans.

    The receipt identity ``(1 + daily) x prod(1 + span) = 1 + epoch`` is the only
    tie between the daily-basis and epoch-basis views; ``daily_cumulative_return``
    and ``epoch_cumulative_return`` are recomputed from the published daily rows of
    the receipt window and its spans, unless the receipt runs past those rows.
    """

    daily_claim = to_decimal(receipt.get("daily_cumulative_return"))
    epoch_claim = to_decimal(receipt.get("epoch_cumulative_return"))
    count_claim = receipt.get("daily_return_observations")
    links_claim = receipt.get("epoch_holding_period_link_observations")
    receipt_end = to_date(receipt.get("observed_end"))
    contradictions: list[str] = []
    missing: list[str] = []
    expected: dict[str, Any] = {}
    if daily_claim is None or epoch_claim is None:
        missing.append("the receipt lacks a decimal daily or epoch cumulative return")
    values = [span.value for span in spans]
    if (
        daily_claim is not None
        and epoch_claim is not None
        and len(values) == len(raw_spans)
        and all(value is not None for value in values)
    ):
        linked = Decimal(1) + daily_claim
        for value in values:
            assert value is not None
            linked *= Decimal(1) + value
        expected["receipt_identity"] = "(1 + daily) x prod(1 + span) = 1 + epoch"
        if abs(linked - (Decimal(1) + epoch_claim)) > RETURN_TOLERANCE:
            contradictions.append(
                "(1 + daily_cumulative_return) x prod(1 + span return) differs from "
                f"1 + epoch_cumulative_return by {linked - Decimal(1) - epoch_claim}"
            )
    if (
        isinstance(count_claim, int)
        and not isinstance(count_claim, bool)
        and links_claim != count_claim + len(raw_spans)
    ):
        contradictions.append(
            f"epoch_holding_period_link_observations {links_claim!r} is not "
            f"daily_return_observations + spans ({count_claim + len(raw_spans)})"
        )
    if series and receipt_start is not None and receipt_end is not None and not ragged:
        window = [row for row in series if receipt_start <= row.day <= receipt_end]
        daily = compound(row.value for row in window)
        epoch = Decimal(1) + daily
        boundary = receipt_start - timedelta(days=1)
        for span in spans:
            if (
                span.value is not None
                and span.start >= boundary
                and span.end <= receipt_end
            ):
                epoch *= Decimal(1) + span.value
        epoch -= Decimal(1)
        expected.update(
            {
                "daily_cumulative_return": daily,
                "epoch_cumulative_return": epoch,
                "daily_return_observations": len(window),
            }
        )
        if daily_claim is not None and abs(daily_claim - daily) > RETURN_TOLERANCE:
            contradictions.append(
                f"daily_cumulative_return differs from the compound of the report's "
                f"{len(window)} daily rows {daily} by {daily_claim - daily}"
            )
        if epoch_claim is not None and abs(epoch_claim - epoch) > RETURN_TOLERANCE:
            contradictions.append(
                f"epoch_cumulative_return differs from the report's daily rows spliced "
                f"with the receipt spans {epoch} by {epoch_claim - epoch}"
            )
        if count_claim != len(window):
            contradictions.append(
                f"daily_return_observations {count_claim!r} is not the report's "
                f"{len(window)} daily rows in the receipt window"
            )
    elif receipt_start is None or receipt_end is None:
        missing.append("the receipt lacks an observed_start/observed_end window")
    if not contradictions and not missing:
        return []
    return [
        _receipt_finding(
            BLOCKER_INVARIANT if contradictions else BLOCKER_AUTHORITY,
            strategy,
            {
                "daily_cumulative_return": receipt.get("daily_cumulative_return"),
                "epoch_cumulative_return": receipt.get("epoch_cumulative_return"),
                "daily_return_observations": count_claim,
                "epoch_holding_period_link_observations": links_claim,
                "observed_start": receipt.get("observed_start"),
                "observed_end": receipt.get("observed_end"),
                "spans": len(raw_spans),
            },
            expected
            or "receipt totals re-derived from the report's own rows and spans",
            pointer,
            "; ".join(contradictions + missing),
        )
    ]


def _epoch_receipt_index(ctx: Context) -> tuple[dict[str, EpochReceipt], list[Finding]]:
    cached = ctx._cache.get("epoch_receipts")
    if isinstance(cached, tuple):
        return cached
    receipts: dict[str, EpochReceipt] = {}
    findings: list[Finding] = []
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    contract = node(ctx.report, "strategy_history_contract")
    for position, outer in enumerate(rows(contract, "epoch_hpr_receipts")):
        if outer.get("evidence_layer") != "ACCOUNT_LIVE":
            continue
        strategy = outer.get("strategy_id")
        if not isinstance(strategy, str) or not strategy:
            findings.append(
                _receipt_finding(
                    BLOCKER_AUTHORITY,
                    "strategy_history_contract.epoch_hpr_receipts",
                    strategy,
                    "a strategy_id",
                    f"{_RECEIPTS_PATH}[{position}].strategy_id",
                    "an epoch HPR receipt names no strategy",
                )
            )
            continue
        grouped.setdefault(strategy, []).append((position, outer))
    for strategy, entries in grouped.items():
        verified = []
        for position, outer in entries:
            receipt, problems = _verify_receipt(ctx, strategy, position, outer)
            verified.append(receipt)
            findings.extend(problems)
        if len(entries) == 1:
            receipts[strategy] = verified[0]
            continue
        findings.append(
            _receipt_finding(
                BLOCKER_AUTHORITY,
                strategy,
                [f"{_RECEIPTS_PATH}[{position}]" for position, _ in entries],
                "one ACCOUNT_LIVE epoch HPR receipt per strategy",
                f"{_RECEIPTS_PATH}[{entries[1][0]}]",
                "the strategy has more than one epoch HPR receipt; none of their "
                "spans is spliced",
            )
        )
        receipts[strategy] = EpochReceipt(
            strategy,
            entries[0][0],
            any(receipt.declares_spans for receipt in verified),
            tuple(
                replace(span, verified=False, blocked=True)
                for receipt in verified
                for span in receipt.spans
            ),
        )
    ctx._cache["epoch_receipts"] = (receipts, findings)
    return receipts, findings


def epoch_receipts(ctx: Context) -> dict[str, EpochReceipt]:
    return _epoch_receipt_index(ctx)[0]


def splice_window(
    ctx: Context,
    strategy_id: str,
    start: date | None,
    end: date | None,
    layer: str = "ACCOUNT_LIVE",
) -> Splice:
    """The epoch-basis compound over ``(B, end]``, ``B`` the day before ``start``.

    Wealth is the product of ``1 + r`` over the daily rows dated in ``[start,
    end]`` times ``1 + span`` over each verified receipt span ``(S, E]`` inside
    ``(B, end]`` (``S >= B`` and ``E <= end``; spec R1). ``B`` defaults to the day
    before the first daily row in the window and an open ``end`` to its last daily
    row. A window edge strictly inside a span (``S < B < E`` or ``S < end < E``) or
    an unverified span inside the window declines (value ``None``): there is no
    observed NAV to cut at, and a receipt the report cannot verify is never spliced.
    Receipts are ACCOUNT_LIVE only.
    """

    selected = [
        (day, value)
        for day, value, evidence in returns_by_strategy(ctx).get(strategy_id, [])
        if evidence == layer
        and (start is None or day >= start)
        and (end is None or day <= end)
    ]
    if not selected:
        return Splice(None, 0, 0)
    first = start if start is not None else selected[0][0]
    boundary = first - timedelta(days=1) if first > date.min else first
    last = end if end is not None else selected[-1][0]
    receipt = epoch_receipts(ctx).get(strategy_id) if layer == "ACCOUNT_LIVE" else None
    total = Decimal(1)
    for _, value in selected:
        total *= Decimal(1) + value
    spliced = 0
    for span in receipt.spans if receipt is not None else ():
        if span.start < boundary < span.end:
            return Splice(
                None,
                len(selected),
                0,
                f"window start {boundary} lies inside span ({span.start}, {span.end}]",
                edge="start",
            )
        if span.start < last < span.end:
            return Splice(
                None,
                len(selected),
                0,
                f"window end {last} lies inside span ({span.start}, {span.end}]",
                edge="end",
            )
        if span.start >= boundary and span.end <= last:
            if not span.verified or span.value is None:
                return Splice(
                    None,
                    len(selected),
                    0,
                    f"span ({span.start}, {span.end}] inside the window is unverified",
                    reported=span.blocked,
                )
            total *= Decimal(1) + span.value
            spliced += 1
    return Splice(total - Decimal(1), len(selected), spliced)


@dataclass(frozen=True)
class Universe:
    source: str
    active_strategies: frozenset[str]
    active_accounts: frozenset[str]
    excluded_accounts: dict[str, str]
    excluded_strategies: dict[str, str]
    known_accounts: frozenset[str]


def _epoch_covers_cutoff(row: Mapping[str, Any], cutoff: date) -> bool:
    """R7-consistent activity test (T2.8, PR #487 bot finding 4113723393).

    An ``INCLUDED_PRODUCT_EPOCH`` row is active exactly when its interval
    covers the fixed historical cutoff: ``effective_start <= cutoff`` and no
    ``effective_end`` before it. A merely open-ended (``effective_end is
    None``) row is not enough -- a future-dated ``effective_start`` has not
    started yet as of the cutoff, and a non-null ``effective_end`` that falls
    on or *after* the cutoff has not ended yet either. An unparseable or
    missing ``effective_start`` fails closed (not active).

    Both endpoints are inclusive of their own day (cross-family review,
    T2.8 follow-up): the crosswalk/MVP-source transition convention pairs a
    predecessor's ``effective_end`` with the successor's ``effective_start``
    exactly one day later (e.g. ``contracts/portfolio-financial-mvp-source-
    v1.json``'s ``schwab-qsi-01`` sleeve -- ``tt-portfolio`` ends
    2026-01-04, ``qsi`` starts 2026-01-05 -- with no gap and no overlap), and
    this file's own ``weekdays_between`` already documents an end-inclusive
    half-open ``(start, end]`` day-counting convention. So ``effective_end
    == cutoff`` is still the epoch's last active day, not yet ended.
    """

    if row.get("product_epoch_disposition") != "INCLUDED_PRODUCT_EPOCH":
        return False
    start = to_date(row.get("effective_start"))
    if start is None or start > cutoff:
        return False
    end = to_date(row.get("effective_end"))
    return end is None or end >= cutoff


def universe(ctx: Context) -> Universe | None:
    """The active universe from the authoritative epoch crosswalk, or ``None``.

    The generation's own ``strategy_summary``/``twr_mwr`` is never used as its own
    universe authority: without a readable crosswalk the universe-derived checks do
    not run and ``universe.authority_missing`` fails closed instead.
    """

    if "universe" in ctx._cache:
        cached = ctx._cache["universe"]
        return cached if isinstance(cached, Universe) else None
    crosswalk = ctx.gen.external.crosswalk
    result: Universe | None = None
    crosswalk_rows = rows(crosswalk, "rows") if isinstance(crosswalk, dict) else []
    if crosswalk_rows:
        active_strategies: set[str] = set()
        active_accounts: set[str] = set()
        excluded_accounts: dict[str, str] = {}
        excluded_strategies: dict[str, str] = {}
        known: set[str] = set()
        for row in crosswalk_rows:
            account = str(row.get("account_id"))
            known.add(account)
            if _epoch_covers_cutoff(row, ctx.cutoff):
                active_strategies.add(str(row.get("strategy_id")))
                active_accounts.add(account)
        for row in crosswalk_rows:
            account = str(row.get("account_id"))
            strategy = str(row.get("strategy_id"))
            disposition = str(row.get("product_epoch_disposition"))
            if account not in active_accounts:
                if disposition != "INCLUDED_PRODUCT_EPOCH":
                    excluded_accounts[account] = disposition
                else:
                    start = to_date(row.get("effective_start"))
                    if start is not None and start > ctx.cutoff:
                        excluded_accounts[account] = (
                            f"EPOCH_NOT_YET_STARTED_{row.get('effective_start')}"
                        )
                    else:
                        excluded_accounts[account] = (
                            f"EPOCH_ENDED_{row.get('effective_end')}"
                        )
            if strategy not in active_strategies:
                excluded_strategies[strategy] = disposition
        if active_strategies:
            result = Universe(
                source=f"crosswalk:{ctx.gen.external.crosswalk_path}",
                active_strategies=frozenset(active_strategies),
                active_accounts=frozenset(active_accounts),
                excluded_accounts=excluded_accounts,
                excluded_strategies=excluded_strategies,
                known_accounts=frozenset(known),
            )
    ctx._cache["universe"] = result
    return result


# The views a custody reserve component list can excuse an account in: the coverage and
# leverage disclosures that describe the custody perimeter. Performance and account
# tables are not custody views, so a retired account there stays reported even when the
# report's own reserve list names it.
_RESERVE_LABEL_VIEWS = frozenset(
    {"report.fact_coverage_matrix", "report.leverage_disclosures"}
)


def reserve_accounts(ctx: Context, view: str | None = None) -> set[str]:
    """Reserve/mandate buckets, recognised from the data rather than by id.

    The role and allocation-scope labels apply to every view; the custody reserve
    component list only to the custody views (``_RESERVE_LABEL_VIEWS``).
    """

    accounts = {
        str(row.get("account_id"))
        for row in rows(ctx.report, "account_summary")
        if row.get("role") == "RESERVE"
    }
    accounts |= {
        str(row.get("account_id"))
        for row in rows(ctx.report, "allocation_history")
        if row.get("scope") == "RESERVE" and row.get("account_id") is not None
    }
    # The reserve component list is the label: a reserve account is expected in the
    # current coverage and leverage views once it is named there.
    components = node(ctx.report, "custody_current_capital", "reserve", "components")
    if isinstance(components, list) and view in _RESERVE_LABEL_VIEWS:
        accounts |= {str(account) for account in components if account is not None}
    return accounts


_UNIVERSE_CHECKS = (
    "universe.active_set_consistent",
    "universe.hardcoded_count_text",
    "universe.retired_strategies_in_current",
    "universe.inactive_accounts_in_current",
    "dates.freshness_vs_cutoff (retired-subject rule)",
)


def check_universe_authority(ctx: Context) -> Iterator[Finding]:
    """D5/D6/D9 need the authoritative crosswalk; without it, fail closed."""

    if universe(ctx) is not None:
        return
    external = ctx.gen.external
    yield _finding(
        "universe.authority_missing",
        BLOCKER_AUTHORITY,
        "account-strategy-current-epoch-crosswalk",
        {
            "crosswalk_path": external.crosswalk_path,
            "readable_rows_with_an_active_epoch": 0,
            "checks_not_evaluated": list(_UNIVERSE_CHECKS),
        },
        "a readable crosswalk with at least one current INCLUDED_PRODUCT_EPOCH row",
        "(crosswalk)",
        "$.rows",
        ("D5", "D6", "D9"),
        "the active universe authority is missing; the generation is not used as "
        "its own universe authority, so retired/inactive filtering is unverified",
    )


# ------------------------------------------------------------------ identity checks

_REQUIRED_SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "report",
        REPORT,
        (
            "account_summary",
            "allocation_history",
            "calculation_cutoff_date",
            "fact_coverage_matrix",
            "portfolio_summary",
            "reconciliation_checks",
            "source_clock",
            "strategy_attribution",
            "strategy_returns",
            "strategy_summary",
            "transactions_and_flows",
            "twr_mwr",
        ),
    ),
    (
        "panel",
        PANEL,
        (
            "account_manifest",
            "account_summary",
            "advisory",
            "allocations",
            "capital",
            "counts",
            "fact_status",
            "gates",
            "meta",
            "performance",
            "strategies",
            "weekly_returns",
        ),
    ),
    ("candidate", CANDIDATE, ("artifact_sha256", "portfolio_as_of", "state")),
    ("manifest", GENERATION_MANIFEST, ("artifacts", "generation_id")),
)


def check_required_sections(ctx: Context) -> Iterator[Finding]:
    documents = {
        "report": ctx.report,
        "panel": ctx.panel,
        "candidate": ctx.gen.candidate,
        "manifest": ctx.gen.manifest,
    }
    for label, file, keys in _REQUIRED_SECTIONS:
        for key in keys:
            if key not in documents[label]:
                yield _finding(
                    "identity.required_sections",
                    BLOCKER_AUTHORITY,
                    f"{label}.{key}",
                    None,
                    "present",
                    file,
                    f"$.{key}",
                    message="required output section is absent; checks that need it "
                    "cannot pass",
                )


def check_generation_id(ctx: Context) -> Iterator[Finding]:
    manifest_id = ctx.gen.manifest.get("generation_id")
    if manifest_id != ctx.gen.generation_id:
        yield _finding(
            "identity.generation_id",
            BLOCKER_AUTHORITY,
            "generation",
            manifest_id,
            ctx.gen.generation_id,
            GENERATION_MANIFEST,
            "$.generation_id",
            ("P1",),
            "outer generation directory id differs from the embedded manifest id",
        )
    pointer = ctx.gen.external.current_pointer
    if (
        isinstance(pointer, dict)
        and pointer.get("generation_id") == ctx.gen.generation_id
    ):
        manifest_sha = ctx.gen.scan.files.get(GENERATION_MANIFEST, (0, ""))[1]
        if pointer.get("manifest_sha256") not in (None, manifest_sha):
            yield _finding(
                "identity.generation_id",
                BLOCKER_AUTHORITY,
                "generation",
                pointer.get("manifest_sha256"),
                manifest_sha,
                str(ctx.gen.external.current_pointer_path),
                "$.manifest_sha256",
                ("P1",),
                "publication pointer names this generation with a different manifest",
            )


_EMBEDDED_ID_KEYS = (
    "report_generation_id",
    "correlation_generation_id",
    "decomposition_generation_id",
    "portfolio_as_of",
    "account_manifest_verification",
    "account_manifest_authority_profile",
)


def check_embedded_ids(ctx: Context) -> Iterator[Finding]:
    meta = node(ctx.panel, "meta")
    for key in _EMBEDDED_ID_KEYS:
        if key not in ctx.gen.candidate:
            continue
        outer = ctx.gen.candidate.get(key)
        inner = node(meta, key)
        if outer != inner:
            yield _finding(
                "identity.embedded_ids",
                BLOCKER_AUTHORITY,
                key,
                inner,
                outer,
                PANEL,
                f"$.meta.{key}",
                ("P1",),
                "candidate record and embedded panel metadata name different sources",
            )
    report_sha = ctx.gen.scan.files.get(REPORT, (0, None))[1]
    declared = node(meta, "report_sha256")
    if declared is not None and declared != report_sha:
        yield _finding(
            "identity.embedded_ids",
            BLOCKER_AUTHORITY,
            "report_sha256",
            report_sha,
            declared,
            PANEL,
            "$.meta.report_sha256",
            ("P1",),
            "embedded report digest differs from the published report bytes",
        )


def _nested_identity(
    value: Any, path: str, found: list[tuple[str, Any, Any]], depth: int = 0
) -> None:
    if depth > 64:
        return
    if isinstance(value, dict):
        outer = value.get("generation_id")
        source = value.get("source")
        if isinstance(outer, str) and isinstance(source, dict):
            inner = source.get("generation_id")
            if isinstance(inner, str) and inner != outer:
                found.append((path, outer, inner))
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                _nested_identity(item, f"{path}.{key}", found, depth + 1)
    elif isinstance(value, list):
        for position, item in enumerate(value):
            if isinstance(item, dict):
                _nested_identity(item, f"{path}[{position}]", found, depth + 1)


def check_nested_generation_ids(ctx: Context) -> Iterator[Finding]:
    """P1/M4: an outer generation id must equal the id embedded in its own source."""

    for file, document in (
        (CANDIDATE, ctx.gen.candidate),
        (
            GENERATION_MANIFEST,
            {k: v for k, v in ctx.gen.manifest.items() if k != "artifacts"},
        ),
        (PANEL, node(ctx.panel, "meta")),
        (PANEL, node(ctx.panel, "account_manifest")),
        (REPORT, node(ctx.report, "provenance")),
    ):
        found: list[tuple[str, Any, Any]] = []
        _nested_identity(document, "$", found)
        for path, outer, inner in found:
            yield _finding(
                "identity.nested_generation_id",
                BLOCKER_AUTHORITY,
                path,
                outer,
                inner,
                file,
                path,
                ("P1",),
                "outer generation_id differs from the embedded source generation_id",
            )


def check_artifact_hashes(ctx: Context) -> Iterator[Finding]:
    files = ctx.gen.scan.files
    declared = ctx.gen.candidate.get("artifact_sha256")
    if isinstance(declared, dict):
        for rel, digest in sorted(declared.items()):
            actual = files.get(rel)
            if actual is None or actual[1] != digest:
                yield _finding(
                    "identity.artifact_hashes",
                    BLOCKER_AUTHORITY,
                    rel,
                    actual[1] if actual else None,
                    digest,
                    CANDIDATE,
                    f"$.artifact_sha256['{rel}']",
                    ("P1",),
                    "published bytes do not match the candidate record",
                )
    for position, artifact in enumerate(rows(ctx.gen.manifest, "artifacts")):
        artifact_id = str(artifact.get("artifact_id"))
        if artifact_id.startswith("panel/"):
            rel = artifact_id[len("panel/") :]
        elif artifact_id.startswith("evidence/"):
            rel = "qa/" + artifact_id[len("evidence/") :]
        else:
            continue
        actual = files.get(rel)
        expected = (artifact.get("size_bytes"), artifact.get("sha256"))
        if actual is None or actual != expected:
            yield _finding(
                "identity.artifact_hashes",
                BLOCKER_AUTHORITY,
                rel,
                list(actual) if actual else None,
                list(expected),
                GENERATION_MANIFEST,
                f"$.artifacts[{position}]",
                ("P1",),
                "generation manifest artifact does not match the published bytes",
            )


def check_embedded_payload_copies(ctx: Context) -> Iterator[Finding]:
    scan = ctx.gen.scan
    panel_sha = scan.stripped_sha256.get(PANEL)
    if PANEL_SCRIPT in scan.files and scan.panel_script_payload_sha256 != panel_sha:
        yield _finding(
            "identity.embedded_payload_copies",
            BLOCKER_AUTHORITY,
            PANEL_SCRIPT,
            scan.panel_script_payload_sha256,
            panel_sha,
            PANEL_SCRIPT,
            "window.PORTFOLIO_DATA",
            (),
            "the rendered data script does not carry the published panel payload",
        )
    report_sha = scan.stripped_sha256.get(REPORT)
    embedded = scan.embedded_payloads.get("portfolio-report-data.json")
    if INDEX in scan.files and embedded is not None:
        contiguous = embedded.chunk_indexes == tuple(range(len(embedded.chunk_indexes)))
        full = scan.files.get(REPORT, (0, ""))[1]
        if not contiguous or embedded.sha256 not in (report_sha, full):
            yield _finding(
                "identity.embedded_payload_copies",
                BLOCKER_AUTHORITY,
                "index.html:portfolio-report-data.json",
                {"sha256": embedded.sha256, "chunks_contiguous": contiguous},
                {"sha256": full},
                INDEX,
                "script[data-download-filename='portfolio-report-data.json']",
                (),
                "the embedded report download differs from the published report",
            )


def _readiness(ctx: Context) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for label, value in (
        ("candidate.state", ctx.gen.candidate.get("state")),
        (
            "candidate.account_manifest_verification.publication_readiness",
            node(
                ctx.gen.candidate,
                "account_manifest_verification",
                "publication_readiness",
            ),
        ),
        (
            "panel.account_manifest.authority_readiness.publication_readiness",
            node(
                ctx.panel,
                "account_manifest",
                "authority_readiness",
                "publication_readiness",
            ),
        ),
        (
            "panel.account_manifest.authority_readiness.independent_authority_verification",
            node(
                ctx.panel,
                "account_manifest",
                "authority_readiness",
                "independent_authority_verification",
            ),
        ),
    ):
        if value is not None:
            values[label] = value
    return values


# The panel's root in a coordinated release (packages/core pipeline: ``portfolio-panel``).
PANEL_ROOT_ID = "portfolio-panel"
_RELEASE_ROOT_ID = re.compile(r"[a-z][a-z0-9-]*")
_RELEASE_DIGEST = re.compile(r"[0-9a-f]{64}")


def _release_evidence_problem(ctx: Context) -> tuple[str, str] | None:
    """Why a committed release pointer's evidence cannot be used, if it cannot.

    ``None`` when there is no release pointer (nothing is committed, nothing to read) or
    its release manifest is readable and well formed. ``("unreadable", why)`` when the
    pointer names a release whose ``release.json`` is missing, a placeholder or not a
    JSON object; ``("malformed", why)`` when its ``targets`` are not a list of objects
    that each name a ``proposed_generation_id``. Either is broken publication
    authority, never an inapplicable check.
    """

    external = ctx.gen.external
    pointer = external.release_pointer
    if not isinstance(pointer, dict) or not isinstance(pointer.get("release_id"), str):
        return None
    manifest = external.release_manifest
    if not isinstance(manifest, dict):
        return (
            "unreadable",
            f"release {pointer['release_id']} names a release.json that is missing, "
            "not hydrated or not a JSON object",
        )
    targets = manifest.get("targets")
    if not isinstance(targets, list) or not targets:
        return "malformed", "the release manifest has no non-empty list of targets"
    for position, target in enumerate(targets):
        if not isinstance(target, dict):
            return "malformed", f"release target {position} is not an object"
        root_id = target.get("root_id")
        generation_id = target.get("proposed_generation_id")
        if not isinstance(root_id, str) or not _RELEASE_ROOT_ID.fullmatch(root_id):
            return (
                "malformed",
                f"release target {position} has no valid root_id "
                "(schemas/core/release-manifest.schema.json)",
            )
        # A target that names this very generation is bound to its id by the
        # generation-id check; every other target must carry a real digest.
        if not isinstance(generation_id, str) or not (
            _RELEASE_DIGEST.fullmatch(generation_id)
            or generation_id == ctx.gen.generation_id
        ):
            return (
                "malformed",
                f"release target {position} has no valid proposed_generation_id "
                "(64 lowercase hex characters)",
            )
    return None


def _release_evidence_finding(
    ctx: Context, check_id: str, defect: str
) -> Finding | None:
    problem = _release_evidence_problem(ctx)
    if problem is None:
        return None
    kind, why = problem
    return _finding(
        check_id,
        NOT_READY if kind == "unreadable" else BLOCKER_AUTHORITY,
        "release",
        kind,
        "a readable release manifest whose targets each name a generation",
        "(release manifest)",
        "$.targets",
        (defect,),
        f"committed release evidence cannot be used: {why}",
    )


def _release_targets(ctx: Context) -> list[tuple[dict[str, Any], str]]:
    """Every committed release target that pins this generation under the panel root.

    Publication is the committed release (``CURRENT_RELEASE`` and its ``release.json``
    targets), not a stage ``CURRENT.json``: a stage pointer can only ever say PUBLISHED.
    A target counts only when it names the panel root (``portfolio-panel``) and this
    generation id; one that names the generation under another root does not.
    """

    external = ctx.gen.external
    manifest = external.release_manifest
    targets = manifest.get("targets") if isinstance(manifest, dict) else None
    if not isinstance(targets, list):
        return []
    return [
        (target, f"{external.release_manifest_path}#$.targets[{position}]")
        for position, target in enumerate(targets)
        if isinstance(target, dict)
        and target.get("root_id") == PANEL_ROOT_ID
        and target.get("proposed_generation_id") == ctx.gen.generation_id
    ]


def _release_misrooted(ctx: Context) -> list[tuple[str, str]]:
    """Targets that name this generation under a root other than the panel's."""

    external = ctx.gen.external
    manifest = external.release_manifest
    targets = manifest.get("targets") if isinstance(manifest, dict) else None
    if not isinstance(targets, list):
        return []
    return [
        (
            str(target.get("root_id")),
            f"{external.release_manifest_path}#$.targets[{position}]",
        )
        for position, target in enumerate(targets)
        if isinstance(target, dict)
        and target.get("root_id") != PANEL_ROOT_ID
        and target.get("proposed_generation_id") == ctx.gen.generation_id
    ]


def _is_published(ctx: Context) -> tuple[bool, str | None]:
    found = _release_targets(ctx)
    if not found:
        return False, None
    return True, found[0][1]


def check_publication_state(ctx: Context) -> Iterator[Finding]:
    """D8: a published generation cannot declare itself not ready / unverified."""

    evidence = _release_evidence_finding(ctx, "identity.publication_state", "D8")
    if evidence is not None:
        yield evidence
        return
    published, where = _is_published(ctx)
    if not published:
        return
    readiness = _readiness(ctx)
    contradictions = {
        key: value
        for key, value in readiness.items()
        if value in ("STAGED", "NOT_READY", "NOT_PERFORMED", "REQUIRED_NOT_READY")
    }
    if contradictions:
        yield _finding(
            "identity.publication_state",
            BLOCKER_AUTHORITY,
            "generation",
            contradictions,
            {"state": "PUBLISHED", "readiness": "READY/VERIFIED"},
            CANDIDATE,
            "$.state",
            ("D8",),
            f"generation is PUBLISHED ({where}) while its own records say not "
            "ready or not independently verified",
        )


def check_manifest_coverage_claim(ctx: Context) -> Iterator[Finding]:
    """A COMPLETE manifest's boundary must name every non-available fact it renders.

    Ruling N6: a manifest cannot carry ``known_gaps`` and stay publishable, so the gaps are
    disclosed in the payload ``fact_status`` and the manifest ``coverage.boundary`` text
    ends ``; declared non-available facts: key=value, ...`` (or ``none``). The declared
    entries come from ``fact_status`` only (FAILED, PARTIAL or UNAVAILABLE); readiness
    NOT_READY / NOT_PERFORMED is ``identity.publication_state``'s. A WARNING, because the
    gaps are rendered in the payload: this is a missing disclosure in the sealed identity,
    not a false claim of availability.
    """

    coverage = node(ctx.gen.manifest, "coverage")
    if not isinstance(coverage, Mapping):
        return
    status = coverage.get("status")
    gaps = coverage.get("known_gaps")
    if not (
        isinstance(status, str)
        and status.startswith("COMPLETE")
        and isinstance(gaps, list)
        and not gaps
    ):
        return
    fact_status = node(ctx.panel, "fact_status")
    declared = {
        f"fact_status.{key}": value
        for key, value in (
            fact_status.items() if isinstance(fact_status, Mapping) else []
        )
        if value in ("FAILED", "PARTIAL", "UNAVAILABLE")
    }
    boundary = str(coverage.get("boundary", ""))
    marker = "declared non-available facts:"
    _, found, tail = boundary.rpartition(marker)
    named = {entry.strip() for entry in tail.split(",")} if found else set()
    missing = sorted(
        key
        for key, value in declared.items()
        if f"{key.split('.', 1)[1]}={value}" not in named
    )
    if missing:
        yield _finding(
            "identity.manifest_coverage_claim",
            WARNING,
            "generation.coverage",
            {
                "coverage_status": status,
                "known_gaps": gaps,
                "missing": missing,
                "payload_declares": declared,
            },
            "coverage.boundary names every non-available fact_status entry",
            GENERATION_MANIFEST,
            "$.coverage.boundary",
            (),
            "sealed manifest claims complete coverage but its boundary does not name "
            f"the non-available facts the payload declares: {', '.join(missing)}",
        )


def check_release_pinned(ctx: Context) -> Iterator[Finding]:
    """F8: the committed release target for the panel must name this generation.

    A reverse binding: the release id is a hash over the proposed generation id, and the
    generation id hashes the sealed panel bytes, so those bytes cannot contain the release
    id. The release that publishes the generation is therefore checked from its side: its
    target must carry this generation id and the sha256 of this ``generation.json``.
    """

    external = ctx.gen.external
    pointer = external.release_pointer
    if not isinstance(pointer, dict) or not isinstance(pointer.get("release_id"), str):
        yield _finding(
            "identity.release_pinned",
            NOT_READY,
            "release",
            None,
            "a release pointer that can be read",
            "(release pointer)",
            "$.release_id",
            ("F8",),
            "no release pointer found; release pinning cannot be evaluated",
        )
        return
    evidence = _release_evidence_finding(ctx, "identity.release_pinned", "F8")
    if evidence is not None:
        yield evidence
        return
    found = _release_targets(ctx)
    if not found:
        for root_id, where in _release_misrooted(ctx):
            yield _finding(
                "identity.release_pinned",
                BLOCKER_AUTHORITY,
                pointer["release_id"],
                {"root_id": root_id},
                {"root_id": PANEL_ROOT_ID},
                "(release manifest)",
                where,
                ("F8",),
                "the committed release names this generation under a root other than "
                "the panel's, so it does not pin the panel",
            )
        return
    manifest_path = ctx.gen.gen_dir / GENERATION_MANIFEST
    actual = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    for target, where in found:
        bound = target.get("proposed_manifest_sha256")
        if bound != actual:
            yield _finding(
                "identity.release_pinned",
                BLOCKER_AUTHORITY,
                pointer["release_id"],
                {"proposed_manifest_sha256": bound},
                {
                    "proposed_generation_id": ctx.gen.generation_id,
                    "proposed_manifest_sha256": actual,
                },
                "(release manifest)",
                where,
                ("F8",),
                "a committed release target that names this generation does not carry "
                "the sha256 of its generation.json",
            )


def check_absolute_paths(ctx: Context) -> Iterator[Finding]:
    """F6: published artifacts must not embed machine-local absolute locators."""

    counts = ctx.gen.scan.absolute_path_counts
    if not counts:
        return
    samples = [
        {"file": hit.file, "offset": hit.offset, "text": hit.snippet}
        for hit in ctx.gen.scan.absolute_path_hits[:8]
    ]
    first = sorted(counts)[0]
    yield _finding(
        "identity.absolute_local_paths",
        BLOCKER_AUTHORITY,
        "generation",
        {
            "files": len(counts),
            "occurrences": sum(counts.values()),
            "per_file": dict(sorted(counts.items())),
            "samples": samples,
        },
        {"occurrences": 0},
        first,
        "(byte scan)",
        ("F6",),
        "published generation embeds absolute local paths",
    )


def check_blocked_admission_census(ctx: Context) -> Iterator[Finding]:
    """N26/R2: every BLOCKED_ADMISSION epoch row is an ASSIGNMENT_ONLY census interval.

    A blocked row is skipped by the account-level expected metrics only when this
    check has nothing to say about it (see ``blocked_epochs``). A row whose tuple the
    census does not hold, a tuple the census funds, or more blocked rows than the
    census counts is a ``BLOCKER_AUTHORITY``; a census that cannot be read, or whose
    bytes the expected baseline does not pin, is one ``NOT_READY`` and nothing is
    skipped. Silent when the report has no blocked row.
    """

    yield from blocked_epochs(ctx).findings


def check_cutoff_consistency(ctx: Context) -> Iterator[Finding]:
    cutoff = ctx.cutoff.isoformat()
    for file, path, value in (
        (
            REPORT,
            "$.calculation_cutoff_date",
            ctx.report.get("calculation_cutoff_date"),
        ),
        (REPORT, "$.as_of_date", ctx.report.get("as_of_date")),
        (
            REPORT,
            "$.source_clock.selected_financial_cutoff",
            node(ctx.report, "source_clock", "selected_financial_cutoff"),
        ),
        (PANEL, "$.meta.portfolio_as_of", node(ctx.panel, "meta", "portfolio_as_of")),
        (
            PANEL,
            "$.account_manifest.calculation_cutoff_date",
            node(ctx.panel, "account_manifest", "calculation_cutoff_date"),
        ),
        (CANDIDATE, "$.portfolio_as_of", ctx.gen.candidate.get("portfolio_as_of")),
    ):
        if value is not None and value != cutoff:
            yield _finding(
                "identity.cutoff_consistency",
                BLOCKER_AUTHORITY,
                path,
                value,
                cutoff,
                file,
                path,
                (),
                f"declared cutoff ({ctx.cutoff_source}) differs from this cutoff field",
            )


# -------------------------------------------------------------- preservation checks

# Declared report -> panel mapping (truth-lineage section 1-6): each report table is
# rendered from one panel table, rows are matched by the key fields, and EVERY field of
# the report row is required in the matching panel row with an identical value, except
# the declared exemptions (none today). The panel may add fields; it may not drop,
# duplicate or change one.
_PRESERVED_TABLES: tuple[tuple[str, str, tuple[str, ...], frozenset[str]], ...] = (
    ("account_summary", "account_summary", ("account_id",), frozenset()),
    ("allocation_history", "allocations", ("allocation_id",), frozenset()),
    ("strategy_summary", "strategies", ("strategy_id",), frozenset()),
    (
        "twr_mwr",
        "performance",
        ("account_id", "strategy_id", "effective_start"),
        frozenset(),
    ),
    ("reconciliation_checks", "reconciliation_checks", ("check_id",), frozenset()),
    (
        "fact_coverage_matrix",
        "fact_coverage_matrix",
        ("account_id", "fact_type"),
        frozenset(),
    ),
    (
        "attribution_dispositions",
        "attribution_dispositions",
        ("account_id", "effective_start"),
        frozenset(),
    ),
    (
        "observation_windows",
        "observation_windows",
        ("window_type", "fact_type", "provider_id"),
        frozenset(),
    ),
    (
        "positions",
        "positions",
        ("account_id", "security_id", "as_of_date"),
        frozenset(),
    ),
    ("provenance", "provenance", ("artifact", "sha256"), frozenset()),
    ("coverage_data_quality", "coverage", ("category", "item"), frozenset()),
)
_PRESERVED_OBJECTS = (
    "hwm_summary",
    "hwm_governed_proof",
    "invested_capital",
)
_ABSENT = "<absent in panel>"


def _row_key(row: Mapping[str, Any], fields: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(row.get(name)) for name in fields)


def _grouped(
    table: list[dict[str, Any]], key_fields: Sequence[str]
) -> dict[tuple[str, ...], list[tuple[int, dict[str, Any]]]]:
    groups: dict[tuple[str, ...], list[tuple[int, dict[str, Any]]]] = {}
    for position, row in enumerate(table):
        groups.setdefault(_row_key(row, key_fields), []).append((position, row))
    return groups


def check_report_to_panel(ctx: Context) -> Iterator[Finding]:
    for report_key, panel_key, key_fields, exempt in _PRESERVED_TABLES:
        if report_key not in ctx.report:
            continue
        report_rows = rows(ctx.report, report_key)
        if report_rows and panel_key not in ctx.panel:
            yield _finding(
                "preservation.report_to_panel",
                BLOCKER_INVARIANT,
                f"{report_key}->{panel_key}",
                {"panel_table": None, "report_rows": len(report_rows)},
                "the mapped panel table is present",
                PANEL,
                f"$.{panel_key}",
                (),
                "a mapped report table is missing from the panel",
            )
            continue
        panel_groups = _grouped(rows(ctx.panel, panel_key), key_fields)
        mismatches: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        for key, report_group in _grouped(report_rows, key_fields).items():
            panel_group = panel_groups.get(key, [])
            if len(panel_group) < len(report_group):
                mismatches.append(
                    {
                        "key": list(key),
                        "report_rows": [position for position, _ in report_group],
                        "panel_rows": [position for position, _ in panel_group],
                    }
                )
                continue
            if len(panel_group) > len(report_group):
                duplicates.append(
                    {
                        "key": list(key),
                        "report_rows": [position for position, _ in report_group],
                        "panel_rows": [position for position, _ in panel_group],
                    }
                )
            for (position, row), (panel_position, panel_row) in zip(
                report_group, panel_group
            ):
                for name, value in row.items():
                    if name in exempt:
                        continue
                    shown = panel_row[name] if name in panel_row else _ABSENT
                    if shown != value:
                        mismatches.append(
                            {
                                "report_row": position,
                                "panel_row": panel_position,
                                "field": name,
                                "report": value,
                                "panel": shown,
                            }
                        )
        if mismatches:
            yield _finding(
                "preservation.report_to_panel",
                BLOCKER_INVARIANT,
                f"{report_key}->{panel_key}",
                {"mismatch_count": len(mismatches), "first": mismatches[:5]},
                "every report row and field preserved in the mapped panel row",
                PANEL,
                f"$.{panel_key}",
                (),
                "a report value was dropped or changed in the panel",
            )
        if duplicates:
            yield _finding(
                "preservation.report_to_panel",
                WARNING,
                f"{report_key}->{panel_key}",
                {"duplicated_keys": len(duplicates), "first": duplicates[:5]},
                "each report row rendered once",
                PANEL,
                f"$.{panel_key}",
                (),
                "the panel repeats a report row (display duplication; disposition needed)",
            )
    for section in _PRESERVED_OBJECTS:
        if section in ctx.report and ctx.report.get(section) != ctx.panel.get(section):
            yield _finding(
                "preservation.report_to_panel",
                BLOCKER_INVARIANT,
                section,
                "panel value differs",
                "identical to report",
                PANEL,
                f"$.{section}",
                (),
                "a report section was not preserved in the panel",
            )
    report_clock = ctx.report.get("source_clock")
    if report_clock is not None and report_clock != node(
        ctx.panel, "meta", "source_clock"
    ):
        yield _finding(
            "preservation.report_to_panel",
            BLOCKER_INVARIANT,
            "source_clock",
            "panel meta.source_clock differs",
            "identical to report",
            PANEL,
            "$.meta.source_clock",
            (),
            "the source clock was not preserved in the panel",
        )


_SUMMARY_CAPITAL = (
    ("Total liquid", "total_liquid"),
    ("Systematic capital", "systematic"),
    ("Reserve", "reserve"),
    ("Retained excluded systematic balance", "retained_excluded"),
)


def summary_value(ctx: Context, metric: str) -> tuple[int, Decimal | None] | None:
    for position, row in enumerate(rows(ctx.report, "portfolio_summary")):
        if row.get("metric") == metric:
            return position, to_decimal(row.get("value"))
    return None


def check_capital_summary_preserved(ctx: Context) -> Iterator[Finding]:
    capital = node(ctx.panel, "capital")
    for metric, panel_field in _SUMMARY_CAPITAL:
        located = summary_value(ctx, metric)
        if located is None:
            continue
        position, report_value = located
        panel_value = to_decimal(node(capital, panel_field))
        if report_value is None:
            continue
        if panel_value is None and report_value == 0:
            continue
        if panel_value is None or abs(panel_value - report_value) > USD_TOLERANCE:
            yield _finding(
                "preservation.capital_summary",
                BLOCKER_INVARIANT,
                panel_field,
                panel_value,
                report_value,
                PANEL,
                f"$.capital.{panel_field}",
                ("B6",),
                f"report portfolio_summary[{position}] '{metric}' is not preserved in "
                "the panel capital block",
            )


def check_counts(ctx: Context) -> Iterator[Finding]:
    counts = node(ctx.panel, "counts")
    if not isinstance(counts, Mapping):
        return
    checks = rows(ctx.panel, "reconciliation_checks")
    performance = rows(ctx.panel, "performance")
    derived: dict[str, tuple[int, tuple[str, ...]]] = {
        "reconciliation_checks": (len(checks), ()),
        "reconciliation_passed": (
            sum(1 for row in checks if str(row.get("status", "")).startswith("PASS")),
            ("B11",),
        ),
        "twr_rows": (len(performance), ()),
        "twr_available": (
            sum(1 for row in performance if row.get("twr") is not None),
            (),
        ),
        "mwr_available": (
            sum(1 for row in performance if row.get("mwr_status") == "AVAILABLE"),
            (),
        ),
        "accounts": (len(rows(ctx.panel, "account_summary")), ()),
        "strategies": (len(rows(ctx.panel, "strategies")), ("D9",)),
        "positions": (len(rows(ctx.panel, "positions")), ()),
    }
    for key, (value, defects) in derived.items():
        if key in counts and counts[key] != value:
            yield _finding(
                "preservation.counts",
                BLOCKER_INVARIANT,
                key,
                counts[key],
                value,
                PANEL,
                f"$.counts.{key}",
                defects,
                "displayed count differs from the rows it counts",
            )


# ------------------------------------------------------------------ capital checks


def check_liquid_identity(ctx: Context) -> Iterator[Finding]:
    capital = node(ctx.panel, "capital")
    total = to_decimal(node(capital, "total_liquid"))
    parts = [
        to_decimal(node(capital, key))
        for key in ("systematic", "reserve", "retained_excluded")
        if node(capital, key) is not None
    ]
    if total is not None and parts and all(part is not None for part in parts):
        components = sum(part for part in parts if part is not None)
        if abs(total - components) > USD_TOLERANCE:
            yield _finding(
                "capital.liquid_identity",
                BLOCKER_INVARIANT,
                "panel.capital",
                {"total_liquid": total, "sum_of_displayed_components": components},
                {"difference": Decimal(0)},
                PANEL,
                "$.capital.total_liquid",
                ("B6",),
                f"total liquid exceeds its displayed components by {total - components}",
            )
    report_total = summary_value(ctx, "Total liquid")
    report_parts = [
        summary_value(ctx, metric)
        for metric in (
            "Systematic capital",
            "Reserve",
            "Retained excluded systematic balance",
        )
    ]
    if report_total and report_total[1] is not None and all(report_parts):
        report_components = sum(
            part[1] for part in report_parts if part is not None and part[1] is not None
        )
        if abs(report_total[1] - report_components) > USD_TOLERANCE:
            yield _finding(
                "capital.liquid_identity",
                BLOCKER_INVARIANT,
                "report.portfolio_summary",
                {
                    "total_liquid": report_total[1],
                    "sum_of_components": report_components,
                },
                {"difference": Decimal(0)},
                REPORT,
                f"$.portfolio_summary[{report_total[0]}]",
                ("B6",),
                "report total liquid differs from systematic + retained + reserve",
            )


def _historical_snapshot(row: Mapping[str, Any]) -> bool:
    raw_day = row.get("as_of_date")
    day = to_date(raw_day)
    return (
        isinstance(raw_day, str)
        and len(raw_day) == 10
        and day is not None
        and raw_day == day.isoformat()
        and row.get("weight_basis") == f"HISTORICAL_SNAPSHOT_{day:%Y_%m_%d}"
    )


def _allocation_totals(
    ctx: Context, *, current_only: bool = False
) -> tuple[Decimal, Decimal | None, list[str]]:
    systematic = Decimal(0)
    reserve: Decimal | None = None
    dates: list[str] = []
    for row in rows(ctx.report, "allocation_history"):
        if current_only and _historical_snapshot(row):
            continue
        value = to_decimal(row.get("current_value_usd")) or Decimal(0)
        if row.get("scope") == "SYSTEMATIC":
            systematic += value
        elif row.get("scope") == "RESERVE":
            reserve = (reserve or Decimal(0)) + value
        dates.append(str(row.get("as_of_date")))
    return systematic, reserve, sorted(set(dates))


def check_allocation_sum(ctx: Context) -> Iterator[Finding]:
    if not rows(ctx.report, "allocation_history"):
        return
    _allocation_systematic, reserve, _ = _allocation_totals(ctx, current_only=True)
    systematic = sum(_current_capital_values(ctx).values(), Decimal(0))
    for expected in ctx.expected or []:
        if expected.get("metric_id") == "capital.portfolio.systematic.cutoff":
            amount = to_decimal(expected.get("expected"))
            if amount is not None:
                systematic = amount
    for file, path, value in (
        (PANEL, "$.capital.systematic", node(ctx.panel, "capital", "systematic")),
        (
            REPORT,
            "$.hwm_summary.governed_perimeter_value_usd",
            node(ctx.report, "hwm_summary", "governed_perimeter_value_usd"),
        ),
    ):
        amount = to_decimal(value)
        if amount is not None and abs(amount - systematic) > USD_TOLERANCE:
            yield _finding(
                "capital.allocation_sum",
                BLOCKER_INVARIANT,
                path,
                amount,
                systematic,
                file,
                path,
                ("B6",),
                "systematic total differs from current-cutoff capital authority",
            )
    panel_reserve = to_decimal(node(ctx.panel, "capital", "reserve"))
    if reserve is not None and panel_reserve is not None:
        if abs(panel_reserve - reserve) > USD_TOLERANCE:
            yield _finding(
                "capital.allocation_sum",
                BLOCKER_INVARIANT,
                "$.capital.reserve",
                panel_reserve,
                reserve,
                PANEL,
                "$.capital.reserve",
                ("B6",),
                "reserve differs from its allocation row",
            )


def _details(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("details_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        import json

        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# A reconciliation detail declares a capital *total* when its name says so
# (value_usd, total_usd, systematic_total_usd, liquid_value_usd, ...). Differences,
# gaps, tolerances and residuals (difference_usd, delta_usd, tolerance_usd, ...) are
# not totals and are never compared with itemized sums.
_TOTAL_FIELD = re.compile(r"(?:^|_)(?:value|total|sum|balance|amount|capital)(?:_|$)")
_NON_TOTAL_FIELD = re.compile(
    r"(?:^|_)(?:diff|difference|delta|gap|tolerance|residual|error|variance|"
    r"mismatch|change|adjustment|max_abs|absolute_difference)(?:_|$)"
)


def _capital_claims(row: Mapping[str, Any]) -> dict[str, Decimal]:
    """USD totals a reconciliation declares (never its differences or tolerances)."""

    claims = {}
    for key, value in _details(row).items():
        if not key.endswith("_usd") or _NON_TOTAL_FIELD.search(key):
            continue
        if not _TOTAL_FIELD.search(key[: -len("_usd")] + "_"):
            continue
        amount = to_decimal(value)
        if amount is not None:
            claims[key] = amount
    return claims


def _itemized_candidates(
    ctx: Context, row: Mapping[str, Any]
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """Totals a capital reconciliation can legitimately equal: (allocation, cutoff).

    Allocation candidates sum the itemized allocation rows; cutoff candidates sum the
    current accounts' values at the cutoff. A stamped PASS total must equal one.
    """

    systematic, reserve, _ = _allocation_totals(ctx)
    reserve_value = reserve or Decimal(0)
    cutoff = cutoff_capital_by_strategy(ctx)
    cutoff_systematic = sum((amount for amount, _ in cutoff.values()), Decimal(0))
    allocation = {
        "sum(allocation_history SYSTEMATIC)": systematic,
        "sum(allocation_history SYSTEMATIC + RESERVE)": systematic + reserve_value,
    }
    at_cutoff = {
        "sum(current account values at cutoff)": cutoff_systematic,
        "sum(current account values at cutoff) + reserve": cutoff_systematic
        + reserve_value,
    }
    strategy = row.get("strategy_id")
    if isinstance(strategy, str):
        for item in rows(ctx.report, "allocation_history"):
            if item.get("strategy_id") == strategy:
                amount = to_decimal(item.get("current_value_usd"))
                if amount is not None:
                    allocation[f"allocation_history[{strategy}]"] = amount
        if strategy in cutoff:
            at_cutoff[f"current account values at cutoff[{strategy}]"] = cutoff[
                strategy
            ][0]
    return allocation, at_cutoff


def _matches(value: Decimal, candidates: Mapping[str, Decimal]) -> list[str]:
    return [
        name
        for name, amount in candidates.items()
        if abs(value - amount) <= USD_TOLERANCE
    ]


def check_reconciliation_recompute(ctx: Context) -> Iterator[Finding]:
    """B6/B13: every stamped-PASS capital reconciliation must be reproducible.

    Bound to what a reconciliation claims (its ``*_usd`` totals), not to its id: any
    PASS row whose claimed total equals no itemized allocation or cutoff-account total
    is unexplained arithmetic, whatever the check is called.
    """

    retained = summary_value(ctx, "Retained excluded systematic balance")
    retained_value = retained[1] if retained and retained[1] is not None else Decimal(0)
    for position, row in enumerate(rows(ctx.report, "reconciliation_checks")):
        status = str(row.get("status"))
        if not status.startswith("PASS"):
            continue
        allocation, at_cutoff = _itemized_candidates(ctx, row)
        for field_name, value in sorted(_capital_claims(row).items()):
            if _matches(value, allocation) or _matches(value, at_cutoff):
                continue
            nearest = min(
                ({**allocation, **at_cutoff}).items(),
                key=lambda item: abs(value - item[1]),
            )
            yield _finding(
                "capital.reconciliation_recompute",
                BLOCKER_INVARIANT,
                str(row.get("check_id")),
                {"recorded": {field_name: value}, "status": status},
                {"reproducible_from": {**allocation, **at_cutoff}},
                REPORT,
                f"$.reconciliation_checks[{position}].details_json.{field_name}",
                ("B6", "B13"),
                f"reconciliation stamped {status} claims a total no itemized rows "
                f"reproduce (nearest {nearest[0]}, off by {value - nearest[1]}; "
                f"retained excluded balance in the report: "
                f"{format_decimal(retained_value)})",
            )


# -------------------------- value-basis bounds and the oracle (L6c, N28)
#
# A strategy whose admitted FX ends before the cutoff has no USD value past that end
# (FXB-1), so a published date before the cutoff can be correct. The report says so
# in its top-level ``value_basis_bounds`` block. The validator never takes that
# block at its word: a date finding becomes ``NOT_READY`` instead of
# ``BLOCKER_AUTHORITY`` only when an entry of the block has the finding's
# (section, entity, date) and a non-empty ``bounded_by``, and the oracle confirms it
# -- every bounded strategy has an expected ``capital.strategy.cutoff`` row whose
# ``as_of`` is that date -- and, for an account entry, the account is one the block
# declares for a ``bounded_by`` strategy (lens entries name no account and are
# exempt). ``dates.value_basis_bound`` then checks the block itself against the
# oracle and the validator's cutoff. Without an expected baseline nothing is
# reclassified and only the oracle-free clauses of the block check run.

BOUNDS_CHECK = "dates.value_basis_bound"
_BOUNDS_BLOCK = "value_basis_bounds"
_BOUNDS_DEFECTS = ("N28",)


@dataclass(frozen=True)
class BoundStrategy:
    position: int
    strategy_id: str
    effective_end: date
    raw_end: Any
    # The accounts the block declares for the strategy; empty when ``account_ids`` is
    # not a list of strings, so that an unreadable list binds no account.
    account_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BoundedSection:
    position: int
    section: str
    entity: str
    day: date
    bounded_by: tuple[str, ...]


@dataclass(frozen=True)
class ValueBasisDisclosure:
    """The ``value_basis_bounds`` block as read, with every structural problem."""

    cutoff: date | None
    raw_cutoff: Any
    strategies: tuple[BoundStrategy, ...]
    sections: tuple[BoundedSection, ...]
    problems: tuple[str, ...]


def _read_value_basis_disclosure(block: Any) -> ValueBasisDisclosure:
    if not isinstance(block, Mapping):
        return ValueBasisDisclosure(
            None, None, (), (), ("value_basis_bounds is not an object",)
        )
    problems: list[str] = []
    raw_cutoff = block.get("calculation_cutoff_date")
    cutoff = to_date(raw_cutoff)
    if cutoff is None:
        problems.append("calculation_cutoff_date is not a date")
    strategies: list[BoundStrategy] = []
    raw_strategies = block.get("strategies")
    if not isinstance(raw_strategies, list):
        problems.append("strategies is not a list")
    else:
        seen: set[str] = set()
        for position, item in enumerate(raw_strategies):
            strategy_id = item.get("strategy_id") if isinstance(item, Mapping) else None
            if not isinstance(strategy_id, str) or not strategy_id:
                problems.append(f"strategies[{position}] has no strategy_id")
                continue
            if strategy_id in seen:
                problems.append(
                    f"strategies[{position}] repeats strategy {strategy_id}"
                )
                continue
            seen.add(strategy_id)
            raw_end = item.get("effective_end")
            end = to_date(raw_end)
            if end is None:
                problems.append(f"strategies[{position}].effective_end is not a date")
                continue
            raw_accounts = item.get("account_ids")
            account_ids = (
                tuple(raw_accounts)
                if isinstance(raw_accounts, list)
                and all(isinstance(account, str) for account in raw_accounts)
                else ()
            )
            strategies.append(
                BoundStrategy(position, strategy_id, end, raw_end, account_ids)
            )
    sections: list[BoundedSection] = []
    raw_sections = block.get("bounded_sections")
    if not isinstance(raw_sections, list):
        problems.append("bounded_sections is not a list")
    else:
        for position, item in enumerate(raw_sections):
            if not isinstance(item, Mapping):
                problems.append(f"bounded_sections[{position}] is not an object")
                continue
            section, entity = item.get("section"), item.get("entity")
            bounded_by = item.get("bounded_by")
            day = to_date(item.get("date"))
            if not isinstance(section, str) or not isinstance(entity, str):
                problems.append(
                    f"bounded_sections[{position}] has no section or entity"
                )
            elif day is None:
                problems.append(f"bounded_sections[{position}].date is not a date")
            elif not isinstance(bounded_by, list) or not all(
                isinstance(name, str) and name for name in bounded_by
            ):
                problems.append(
                    f"bounded_sections[{position}].bounded_by is not a list of strategy ids"
                )
            else:
                sections.append(
                    BoundedSection(position, section, entity, day, tuple(bounded_by))
                )
    return ValueBasisDisclosure(
        cutoff, raw_cutoff, tuple(strategies), tuple(sections), tuple(problems)
    )


def value_basis_disclosure(ctx: Context) -> ValueBasisDisclosure | None:
    """The report's ``value_basis_bounds`` block, or ``None`` when it has none."""

    if "value_basis_disclosure" in ctx._cache:
        cached = ctx._cache["value_basis_disclosure"]
        return cached if isinstance(cached, ValueBasisDisclosure) else None
    block = ctx.report.get(_BOUNDS_BLOCK)
    result = None if block is None else _read_value_basis_disclosure(block)
    ctx._cache["value_basis_disclosure"] = result
    return result


def _oracle_cutoff_dates(ctx: Context) -> dict[str, frozenset[date]] | None:
    """Per strategy, the ``as_of`` dates of its expected ``capital.strategy.cutoff`` rows.

    ``None`` without an expected baseline; a strategy whose rows carry no ``as_of``
    maps to an empty set (the oracle names no date for it).
    """

    if ctx.expected is None:
        return None
    cached = ctx._cache.get("oracle_cutoff_dates")
    if isinstance(cached, dict):
        return cached
    found: dict[str, set[date]] = {}
    for row in ctx.expected:
        if row.get("metric_id") != "capital.strategy.cutoff":
            continue
        days = found.setdefault(str(row.get("entity")), set())
        day = to_date(row.get("as_of"))
        if day is not None:
            days.add(day)
    result = {strategy: frozenset(days) for strategy, days in found.items()}
    ctx._cache["oracle_cutoff_dates"] = result
    return result


def verified_value_basis_bound(
    ctx: Context, section: str, entity: str, day: date
) -> BoundedSection | None:
    """The block entry that explains this date, when the oracle confirms it.

    The entry has the same (section, entity, date) and a non-empty ``bounded_by``,
    the date precedes the cutoff, the block has no structural problem, and every
    bounded strategy has an expected ``capital.strategy.cutoff`` row whose only
    ``as_of`` is that date. An ``account_summary`` entry also has to name an account
    the block itself declares (``strategies[*].account_ids``) for at least one of its
    ``bounded_by`` strategies: published view against published view, the oracle
    holding no account-to-strategy map. Without that the date could belong to an
    account the bound says nothing about, and a real freshness blocker would be
    relabelled. A ``decomposition_lenses`` entry is exempt: its entity is
    ``{lens_id}:{evidence_mode}``, not an account id, and only the oracle check above
    ties it to its strategies. ``None`` means the finding is unchanged.
    """

    disclosure = value_basis_disclosure(ctx)
    oracle = _oracle_cutoff_dates(ctx)
    if disclosure is None or disclosure.problems or oracle is None or day >= ctx.cutoff:
        return None
    declared = {item.strategy_id: item.account_ids for item in disclosure.strategies}
    for entry in disclosure.sections:
        if (
            (entry.section, entry.entity, entry.day) == (section, entity, day)
            and entry.bounded_by
            and all(oracle.get(name) == frozenset({day}) for name in entry.bounded_by)
            and (
                section != "account_summary"
                or any(entity in declared.get(name, ()) for name in entry.bounded_by)
            )
        ):
            return entry
    return None


def _bound_message(entry: BoundedSection, what: str) -> str:
    return (
        f"{what} ends before the cutoff only because the value basis of "
        f"{', '.join(entry.bounded_by)} ends at that date (value_basis_bounds entry "
        "confirmed by the oracle capital.strategy.cutoff as_of, FXB-1)"
    )


def check_value_basis_bound(ctx: Context) -> Iterator[Finding]:
    """N28/FXB-1: the report's ``value_basis_bounds`` block against the oracle.

    ``BLOCKER_AUTHORITY`` for a bounded strategy whose ``effective_end`` differs from
    the oracle's ``capital.strategy.cutoff`` ``as_of`` (a missing oracle row counts)
    or whose block ``calculation_cutoff_date`` differs from the validator cutoff;
    ``BLOCKER_INVARIANT`` for an entry whose date is not the earliest
    ``effective_end`` over its ``bounded_by`` (empty, or naming a strategy the
    block does not list) and for a block the check cannot read. Without an expected
    baseline only the oracle-free clauses run. Silent when the report has no block.
    """

    disclosure = value_basis_disclosure(ctx)
    if disclosure is None:
        return
    for problem in disclosure.problems:
        yield _finding(
            BOUNDS_CHECK,
            BLOCKER_INVARIANT,
            _BOUNDS_BLOCK,
            problem,
            "a well-formed value_basis_bounds block",
            REPORT,
            f"$.{_BOUNDS_BLOCK}",
            _BOUNDS_DEFECTS,
            f"the value-basis disclosure cannot be read: {problem}",
        )
    oracle = _oracle_cutoff_dates(ctx)
    for strategy in disclosure.strategies:
        reasons: list[str] = []
        expected_end: Any = None
        if oracle is not None:
            days = oracle.get(strategy.strategy_id, frozenset())
            expected_end = (
                next(iter(days)).isoformat()
                if len(days) == 1
                else sorted(day.isoformat() for day in days) or None
            )
            if not days:
                reasons.append(
                    "the oracle has no capital.strategy.cutoff row with an as_of "
                    f"for {strategy.strategy_id}"
                )
            elif days != frozenset({strategy.effective_end}):
                reasons.append(
                    f"effective_end {strategy.effective_end} differs from the oracle "
                    f"capital.strategy.cutoff as_of {expected_end}"
                )
        if disclosure.cutoff is not None and disclosure.cutoff != ctx.cutoff:
            reasons.append(
                f"calculation_cutoff_date {disclosure.cutoff} differs from the "
                f"declared cutoff {ctx.cutoff}"
            )
        if reasons:
            yield _finding(
                BOUNDS_CHECK,
                BLOCKER_AUTHORITY,
                strategy.strategy_id,
                {
                    "effective_end": strategy.raw_end,
                    "calculation_cutoff_date": disclosure.raw_cutoff,
                },
                {
                    "effective_end": expected_end,
                    "calculation_cutoff_date": ctx.cutoff,
                },
                REPORT,
                f"$.{_BOUNDS_BLOCK}.strategies[{strategy.position}]",
                _BOUNDS_DEFECTS,
                "the value-basis bound is not the oracle's: " + "; ".join(reasons),
            )
    ends = {
        strategy.strategy_id: strategy.effective_end
        for strategy in disclosure.strategies
    }
    for entry in disclosure.sections:
        reason = None
        if not entry.bounded_by:
            reason = (
                "bounded_by is empty, so no strategy's value basis ends at the date"
            )
        else:
            unknown = [name for name in entry.bounded_by if name not in ends]
            if unknown:
                reason = (
                    f"bounded_by names strategies the block does not bound: {unknown}"
                )
            else:
                earliest = min(ends[name] for name in entry.bounded_by)
                if entry.day != earliest:
                    reason = (
                        f"the date {entry.day} is not the earliest effective_end "
                        f"({earliest}) over bounded_by"
                    )
        if reason is not None:
            yield _finding(
                BOUNDS_CHECK,
                BLOCKER_INVARIANT,
                f"{entry.section}|{entry.entity}",
                {"date": entry.day, "bounded_by": list(entry.bounded_by)},
                "date = min(effective_end over bounded_by)",
                REPORT,
                f"$.{_BOUNDS_BLOCK}.bounded_sections[{entry.position}]",
                _BOUNDS_DEFECTS,
                f"the value-basis entry is inconsistent: {reason}",
            )


# N56 coverage bound: a custody cash line is dated by the end of the admissions it is
# derived from, which can precede the cutoff without any FX value-basis bound (the
# Matrixport sleeve allocations are admitted through the last subscription).  The report
# states it in ``capital_coverage_bounds``: one ``lines`` entry per such cash line and
# the ``current_capital`` dates each line explains.  The validator downgrades a date to
# NOT_READY only when the block, the report's own cash line and the oracle agree, and
# ``capital.coverage_bound`` blocks a block the report does not bear out.

COVERAGE_BOUNDS_CHECK = "capital.coverage_bound"
_COVERAGE_BLOCK = "capital_coverage_bounds"
_COVERAGE_DEFECTS = ("N56",)


@dataclass(frozen=True)
class CoverageLine:
    position: int
    strategy_id: str
    account_id: str
    as_of: date
    coverage_end: date


@dataclass(frozen=True)
class CoverageDisclosure:
    """The ``capital_coverage_bounds`` block as read, with every structural problem."""

    cutoff: date | None
    raw_cutoff: Any
    lines: tuple[CoverageLine, ...]
    sections: tuple[BoundedSection, ...]
    problems: tuple[str, ...]


def _read_capital_coverage_disclosure(block: Any) -> CoverageDisclosure:
    if not isinstance(block, Mapping):
        return CoverageDisclosure(
            None, None, (), (), (f"{_COVERAGE_BLOCK} is not an object",)
        )
    problems: list[str] = []
    raw_cutoff = block.get("calculation_cutoff_date")
    cutoff = to_date(raw_cutoff)
    if cutoff is None:
        problems.append("calculation_cutoff_date is not a date")
    lines: list[CoverageLine] = []
    raw_lines = block.get("lines")
    if not isinstance(raw_lines, list):
        problems.append("lines is not a list")
    else:
        for position, item in enumerate(raw_lines):
            strategy = item.get("strategy_id") if isinstance(item, Mapping) else None
            account = item.get("account_id") if isinstance(item, Mapping) else None
            as_of = (
                to_date(item.get("as_of_date")) if isinstance(item, Mapping) else None
            )
            end = (
                to_date(item.get("coverage_end")) if isinstance(item, Mapping) else None
            )
            if (
                not isinstance(strategy, str)
                or not strategy
                or not isinstance(account, str)
                or not account
            ):
                problems.append(f"lines[{position}] has no strategy_id or account_id")
            elif as_of is None or end is None:
                problems.append(f"lines[{position}] has no as_of_date or coverage_end")
            else:
                lines.append(CoverageLine(position, strategy, account, as_of, end))
    sections: list[BoundedSection] = []
    raw_sections = block.get("bounded_sections")
    if not isinstance(raw_sections, list):
        problems.append("bounded_sections is not a list")
    else:
        for position, item in enumerate(raw_sections):
            if not isinstance(item, Mapping):
                problems.append(f"bounded_sections[{position}] is not an object")
                continue
            section, entity = item.get("section"), item.get("entity")
            bounded_by = item.get("bounded_by")
            day = to_date(item.get("date"))
            if not isinstance(section, str) or not isinstance(entity, str):
                problems.append(
                    f"bounded_sections[{position}] has no section or entity"
                )
            elif day is None:
                problems.append(f"bounded_sections[{position}].date is not a date")
            elif not isinstance(bounded_by, list) or not all(
                isinstance(name, str) and name for name in bounded_by
            ):
                problems.append(
                    f"bounded_sections[{position}].bounded_by is not a list of "
                    "strategy ids"
                )
            else:
                sections.append(
                    BoundedSection(position, section, entity, day, tuple(bounded_by))
                )
    return CoverageDisclosure(
        cutoff, raw_cutoff, tuple(lines), tuple(sections), tuple(problems)
    )


def capital_coverage_disclosure(ctx: Context) -> CoverageDisclosure | None:
    """The report's ``capital_coverage_bounds`` block, or ``None`` when it has none."""

    if "capital_coverage_disclosure" in ctx._cache:
        cached = ctx._cache["capital_coverage_disclosure"]
        return cached if isinstance(cached, CoverageDisclosure) else None
    block = ctx.report.get(_COVERAGE_BLOCK)
    result = None if block is None else _read_capital_coverage_disclosure(block)
    ctx._cache["capital_coverage_disclosure"] = result
    return result


def _coverage_explainable(
    disclosure: CoverageDisclosure,
) -> dict[tuple[str, date], tuple[str, ...]]:
    """What the lines the block states can explain: the line, its row, the total.

    The row of a strategy is explained at the oldest coverage end of its lines and the
    Systematic total at the oldest of those, naming the strategies that reach it.
    """

    explained: dict[tuple[str, date], tuple[str, ...]] = {}
    oldest: dict[str, date] = {}
    for line in disclosure.lines:
        explained[(f"{line.strategy_id}:{line.account_id}", line.coverage_end)] = (
            line.strategy_id,
        )
        oldest[line.strategy_id] = min(
            oldest.get(line.strategy_id, line.coverage_end), line.coverage_end
        )
    for strategy, day in oldest.items():
        explained[(strategy, day)] = (strategy,)
    if oldest:
        total = min(oldest.values())
        explained[("systematic", total)] = tuple(
            sorted(name for name, day in oldest.items() if day == total)
        )
    return explained


def _coverage_line_mismatches(
    ctx: Context, disclosure: CoverageDisclosure
) -> list[str]:
    """Every way a stated line is not the report's own cash line at that date."""

    problems: list[str] = []
    custody = node(ctx.report, "custody_current_capital")
    carried: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows(custody, "strategies"):
        if row.get("scope", "SYSTEMATIC") != "SYSTEMATIC":
            continue
        for line in rows(row, "derived_cash"):
            carried[(str(row.get("strategy_id")), str(line.get("account_id")))] = line
    for item in disclosure.lines:
        label = f"lines[{item.position}] ({item.strategy_id}:{item.account_id})"
        report_line = carried.get((item.strategy_id, item.account_id))
        if item.as_of != item.coverage_end:
            problems.append(f"{label} is not dated at its own coverage end")
        if disclosure.cutoff is not None and item.coverage_end >= disclosure.cutoff:
            problems.append(f"{label} does not end before the cutoff")
        if report_line is None:
            problems.append(f"{label} is not a cash line of the report")
        elif not (
            _canonical_iso_day(report_line.get("as_of_date"))
            == _canonical_iso_day(report_line.get("coverage_end"))
            == item.coverage_end
        ):
            problems.append(
                f"{label} differs from the report's cash line "
                f"(as_of_date {report_line.get('as_of_date')}, coverage_end "
                f"{report_line.get('coverage_end')})"
            )
    return problems


def verified_capital_coverage_bound(
    ctx: Context, entity: str, day: date
) -> BoundedSection | None:
    """The coverage entry that explains this ``current_capital`` date, when verified.

    The entry has the same ``(entity, date)``, names the strategies the lines the block
    states give for it, the block states the validator's cutoff, every stated line is the
    report's own cash line at its date, and the oracle dates every bounded strategy's
    ``capital.strategy.cutoff`` on or after the label (a label may err old, never new; an
    oracle that is absent confirms nothing).  ``None`` means the finding is unchanged.
    """

    disclosure = capital_coverage_disclosure(ctx)
    oracle = _oracle_cutoff_dates(ctx)
    if (
        disclosure is None
        or disclosure.problems
        or oracle is None
        or day >= ctx.cutoff
        or disclosure.cutoff != ctx.cutoff
        or _coverage_line_mismatches(ctx, disclosure)
    ):
        return None
    explained = _coverage_explainable(disclosure)
    for entry in disclosure.sections:
        if (
            (entry.section, entry.entity, entry.day) == ("current_capital", entity, day)
            and entry.bounded_by
            and explained.get((entry.entity, entry.day))
            == tuple(sorted(entry.bounded_by))
            and all(
                oracle.get(name) and min(oracle[name]) >= day
                for name in entry.bounded_by
            )
        ):
            return entry
    return None


def _coverage_message(entry: BoundedSection) -> str:
    return (
        "current capital ends before the cutoff only because the admitted coverage of "
        f"{', '.join(entry.bounded_by)} ends at that date (capital_coverage_bounds "
        "entry confirmed by the report's cash line and the oracle, N56)"
    )


def check_capital_coverage_bound(ctx: Context) -> Iterator[Finding]:
    """N56: the report's ``capital_coverage_bounds`` block must be what its lines bear out.

    ``BLOCKER_INVARIANT`` for a block the check cannot read, a line that is not the
    report's own cash line at its date or does not end before the cutoff, and an entry
    that the stated lines do not explain; ``BLOCKER_AUTHORITY`` for a block dated at a
    cutoff other than the validator's.  Silent when the report has no block.
    """

    disclosure = capital_coverage_disclosure(ctx)
    if disclosure is None:
        return

    def finding(
        klass: str,
        entity: str,
        observed: Any,
        expected: Any,
        pointer: str,
        message: str,
    ) -> Finding:
        return _finding(
            COVERAGE_BOUNDS_CHECK,
            klass,
            entity,
            observed,
            expected,
            REPORT,
            pointer,
            _COVERAGE_DEFECTS,
            message,
        )

    for problem in disclosure.problems:
        yield finding(
            BLOCKER_INVARIANT,
            _COVERAGE_BLOCK,
            problem,
            "a well-formed capital_coverage_bounds block",
            f"$.{_COVERAGE_BLOCK}",
            f"the coverage disclosure cannot be read: {problem}",
        )
    if disclosure.cutoff is not None and disclosure.cutoff != ctx.cutoff:
        yield finding(
            BLOCKER_AUTHORITY,
            _COVERAGE_BLOCK,
            disclosure.raw_cutoff,
            ctx.cutoff,
            f"$.{_COVERAGE_BLOCK}.calculation_cutoff_date",
            "the coverage disclosure is dated at a cutoff other than the declared one",
        )
    for problem in _coverage_line_mismatches(ctx, disclosure):
        yield finding(
            BLOCKER_INVARIANT,
            _COVERAGE_BLOCK,
            problem,
            "each stated line is the report's own cash line at its coverage end",
            f"$.{_COVERAGE_BLOCK}.lines",
            f"the coverage disclosure is not borne out by the report: {problem}",
        )
    explained = _coverage_explainable(disclosure)
    for entry in disclosure.sections:
        if entry.section != "current_capital" or explained.get(
            (entry.entity, entry.day)
        ) != tuple(sorted(entry.bounded_by)):
            yield finding(
                BLOCKER_INVARIANT,
                f"{entry.section}|{entry.entity}",
                {"date": entry.day, "bounded_by": list(entry.bounded_by)},
                "a current_capital date the stated lines explain, by the strategies "
                "that reach it",
                f"$.{_COVERAGE_BLOCK}.bounded_sections[{entry.position}]",
                "the coverage entry is not explained by the lines the block states",
            )


def check_capital_as_of(ctx: Context) -> Iterator[Finding]:
    """B4/B13: current capital must be dated at the declared cutoff."""

    cutoff = ctx.cutoff
    stale_allocations = [
        (position, row)
        for position, row in enumerate(rows(ctx.report, "allocation_history"))
        if (to_date(row.get("as_of_date")) or cutoff) < cutoff
        and not _historical_snapshot(row)
    ]
    if stale_allocations:
        dates = sorted({str(row.get("as_of_date")) for _, row in stale_allocations})
        yield _finding(
            "capital.as_of_cutoff",
            BLOCKER_AUTHORITY,
            "allocation_history",
            {"rows": len(stale_allocations), "as_of_dates": dates},
            cutoff,
            REPORT,
            "$.allocation_history[*].as_of_date",
            ("B4",),
            "current allocation / capital rows are dated before the declared cutoff",
        )
    for position, row in enumerate(rows(ctx.report, "account_summary")):
        as_of = to_date(row.get("as_of_date"))
        if as_of is not None and as_of < cutoff and is_available(row.get("status")):
            account = str(row.get("account_id"))
            bound = verified_value_basis_bound(ctx, "account_summary", account, as_of)
            yield _finding(
                "capital.as_of_cutoff",
                BLOCKER_AUTHORITY if bound is None else NOT_READY,
                account,
                row.get("as_of_date"),
                cutoff,
                REPORT,
                f"$.account_summary[{position}].as_of_date",
                ("B4",),
                "current account value is presented available but dated before the cutoff"
                if bound is None
                else _bound_message(bound, "current account value"),
            )
    if stale_allocations:
        for position, row in enumerate(rows(ctx.report, "reconciliation_checks")):
            check_id = str(row.get("check_id"))
            claims = _capital_claims(row)
            _, at_cutoff = _itemized_candidates(ctx, row)
            if (
                claims
                and str(row.get("status")).startswith("PASS")
                and not all(_matches(value, at_cutoff) for value in claims.values())
            ):
                yield _finding(
                    "capital.as_of_cutoff",
                    BLOCKER_AUTHORITY,
                    check_id,
                    {
                        "stamped_source_as_of_date": row.get("source_as_of_date"),
                        "allocation_rows_as_of": sorted(
                            {
                                str(item.get("as_of_date"))
                                for _, item in stale_allocations
                            }
                        ),
                    },
                    cutoff,
                    REPORT,
                    f"$.reconciliation_checks[{position}]",
                    ("B13",),
                    "capital reconciliation stamped PASS does not tie to cutoff "
                    "account values while allocation rows predate the cutoff "
                    "(circular: stale rows reconciled against a stale total)",
                )


_CASH_BASIS = (
    "DERIVED_ADMITTED_EXTERNAL_FLOWS_MINUS_SLEEVE_ALLOCATIONS_PLUS_REDEMPTIONS"
)
_CASH_EVENT_TOLERANCE = Decimal("0.000001")
CASH_EVIDENCE_CHECK = "capital.derived_cash_evidence"


def _event_key(event: Mapping[str, Any]) -> str:
    """The identity an event is counted by: its record id, else its whole content."""

    for name in ("record_id", "event_sha256"):
        value = event.get(name)
        if isinstance(value, str) and value:
            return f"{name}:{value}"
    return "content:" + json.dumps(event, sort_keys=True, default=str)


RateTable = Mapping[tuple[str, str], frozenset[str]]


def cash_rate_table(report: Mapping[str, Any]) -> RateTable:
    """Every event-time rate the custody cash events state, by (currency, date).

    The report carries no FX-rate table, only each event's own ``event_time_fx``; this is
    the table those statements make together, so an event can be held to its siblings.
    """

    found: dict[tuple[str, str], set[str]] = {}
    for strategy in rows(node(report, "custody_current_capital"), "strategies"):
        for line in rows(strategy, "derived_cash"):
            for event in rows(line, "events"):
                fx = event.get("event_time_fx")
                rate = to_decimal(fx.get("rate")) if isinstance(fx, Mapping) else None
                if rate is None or not isinstance(fx, Mapping):
                    continue
                currency = str(event.get("native_currency") or fx.get("currency"))
                day = str(fx.get("date") or event.get("event_date"))
                found.setdefault((currency, day), set()).add(str(rate.normalize()))
    return {key: frozenset(rates) for key, rates in found.items()}


def _amount_text(value: Any) -> bool:
    """A non-empty string that parses to a finite number (the schema types amounts so)."""

    return (
        isinstance(value, str) and bool(value.strip()) and to_decimal(value) is not None
    )


def _event_shape_problems(event: Mapping[str, Any]) -> list[str]:
    """Fields every custody cash event must carry (report-data.schema.json).

    ``event_date``, ``record_id``, ``event_sha256``, ``native_currency``, ``native_value``,
    ``event_time_fx`` (an object, or null for native USD) and ``signed_usd`` are all required;
    a missing, null, boolean, numeric-typed, non-numeric or non-finite value is not evidence,
    and an event that fails here is not run through the native-amount check.
    """

    problems: list[str] = []
    if _canonical_iso_day(event.get("event_date")) is None:
        problems.append("event_date is missing or not a canonical ISO date")
    record = event.get("record_id")
    if not isinstance(record, str) or not record:
        problems.append("record_id is missing or not a non-empty string")
    digest = event.get("event_sha256")
    if not isinstance(digest, str) or not _RELEASE_DIGEST.fullmatch(digest):
        problems.append(
            "event_sha256 is missing or not a 64 character lowercase digest"
        )
    currency = event.get("native_currency")
    if not isinstance(currency, str) or not currency:
        problems.append("native_currency is missing or not a non-empty string")
    if not _amount_text(event.get("native_value")):
        problems.append("native_value is missing or not a finite numeric string")
    if not _amount_text(event.get("signed_usd")):
        problems.append("signed_usd is missing or not a finite numeric string")
    if "event_time_fx" not in event or not (
        event["event_time_fx"] is None or isinstance(event["event_time_fx"], Mapping)
    ):
        problems.append("event_time_fx is missing or neither an object nor null")
    return problems


def _native_event_problem(
    event: Mapping[str, Any], rates: RateTable | None = None
) -> str | None:
    """An event's ``signed_usd`` must follow from its native amount and event-time rate.

    Only an event that states a native amount is checked: ``|signed_usd|`` equals the
    native amount times the event-time rate. A native USD amount carries rate exactly 1; a
    foreign native amount needs a positive rate, the same one every event of that currency
    and date states (``rates``). ACCEPTED RESIDUAL: the report carries no FX-rate table, so
    the absolute rate is not independently recomputed here; the oracle's
    ``capital.strategy_cash.cutoff`` row is the independent check of the amount.
    """

    native = to_decimal(event.get("native_value"))
    signed = to_decimal(event.get("signed_usd"))
    if native is None or signed is None:
        return None
    currency = event.get("native_currency")
    fx = event.get("event_time_fx")
    rate = to_decimal(fx.get("rate")) if isinstance(fx, Mapping) else None
    if currency in (None, "USD"):
        if rate is not None and rate != 1:
            return f"a native USD amount cannot carry the rate {rate}"
        rate = Decimal(1)
    elif rate is None:
        return "a foreign native amount carries no event-time rate"
    elif rate <= 0:
        return f"the event-time rate {rate} is not positive"
    elif rates is not None and isinstance(fx, Mapping):
        day = str(fx.get("date") or event.get("event_date"))
        stated = rates.get((str(currency), day), frozenset())
        if len(stated) > 1:
            return (
                f"{currency} carries {len(stated)} different event-time rates on {day} "
                "across the custody cash events"
            )
    if abs(abs(signed) - abs(native) * rate) > _CASH_EVENT_TOLERANCE:
        return (
            f"signed_usd {signed} is not the native amount {native} at the event-time "
            f"rate {rate}"
        )
    return None


def _balance_and_identity_problems(events: Sequence[Mapping[str, Any]]) -> list[str]:
    """Dated running balance and event identity of one line's flow events.

    The custody contract forbids negative cash: the balance at the end of every event date
    (events of one day are netted, so a same-day withdrawal listed before its deposit is
    fine) must not be negative. An event with no canonical date cannot be ordered, and one
    record counted twice is summed twice.
    """

    problems: list[str] = []
    seen: set[str] = set()
    by_day: dict[date, Decimal] = {}
    undated = 0
    for event in events:
        key = _event_key(event)
        if key in seen:
            problems.append(f"the event {key} is counted twice in the line")
        seen.add(key)
        signed = to_decimal(event.get("signed_usd"))
        day = _canonical_iso_day(event.get("event_date"))
        if day is None:
            undated += 1
        elif signed is not None:
            by_day[day] = by_day.get(day, Decimal(0)) + signed
    if undated:
        problems.append(
            f"{undated} event(s) have no canonical event_date to order them by"
        )
    balance = Decimal(0)
    for day in sorted(by_day):
        balance += by_day[day]
        if balance < -_CASH_EVENT_TOLERANCE:
            problems.append(
                f"the running balance is negative ({balance}) at the end of {day}"
            )
            break
    return problems


def derived_cash_issues(
    line: Any, rates: RateTable | None = None
) -> tuple[Decimal | None, list[str]]:
    """The amount of an AVAILABLE derived cash line and why it cannot be trusted.

    OWNER_318 and the custody contract: the line is derived (admitted deposits less
    withdrawals less sleeve allocations plus redemptions) in USD at event-date FX and a
    negative line fails closed. So the line must be USD, not negative at any dated point,
    carry the derived basis, count each event once, and reproduce from its own flow events
    (the sum of their ``signed_usd``, each following from its native amount and rate); a
    producer-supplied total is never evidence of itself. ``(None, [])`` is a line that is
    not AVAILABLE (not evidence, not a finding).
    """

    value = node(line, "current_value")
    if not isinstance(value, Mapping) or not is_available(value.get("status")):
        return None, []
    problems: list[str] = []
    amount = to_decimal(value.get("value"))
    if amount is None:
        return None, ["the line has no numeric amount"]
    if value.get("currency") != "USD":
        problems.append(f"the line is not in USD ({value.get('currency')})")
    if amount < 0:
        problems.append("the line is negative (a negative cash line fails closed)")
    if node(line, "basis") != _CASH_BASIS:
        problems.append("the line does not carry the derived basis")
    events = node(line, "events")
    signed = [to_decimal(item.get("signed_usd")) for item in rows(line, "events")]
    if not isinstance(events, list) or not events or None in signed:
        problems.append("the line has no flow events to reproduce it from")
    else:
        total = sum((item for item in signed if item is not None), Decimal(0))
        if abs(total - amount) > _CASH_EVENT_TOLERANCE:
            problems.append(
                f"the line's flow events sum to {total}, not the published {amount}"
            )
        problems.extend(_balance_and_identity_problems(rows(line, "events")))
    for position, event in enumerate(rows(line, "events")):
        shape = _event_shape_problems(event)
        if shape:
            problems.extend(f"event {position}: {problem}" for problem in shape)
            continue
        problem = _native_event_problem(event, rates)
        if problem is not None:
            problems.append(f"event {position}: {problem}")
    return amount, problems


@dataclass(frozen=True)
class CashLine:
    strategy_id: str
    position: int
    line_position: int
    account_id: Any
    amount: Decimal | None
    problems: tuple[str, ...]
    counted: bool


def derived_cash_lines(ctx: Context) -> list[CashLine]:
    """Every derived cash component with its verdict, repeats marked and not counted.

    A component repeats when its account id was already listed for the strategy or any of
    its events was already counted in an earlier component anywhere in the custody block:
    the repeat is a finding and its amount is not summed again.
    """

    cached = ctx._cache.get("derived_cash_lines")
    if isinstance(cached, list):
        return cast(list[CashLine], cached)
    rates = cash_rate_table(ctx.report)
    seen_events: set[str] = set()
    seen_accounts: set[tuple[str, str]] = set()
    found: list[CashLine] = []
    custody = node(ctx.report, "custody_current_capital")
    for position, strategy in enumerate(rows(custody, "strategies")):
        lines = strategy.get("derived_cash")
        if not isinstance(lines, list):
            continue
        strategy_id = str(strategy.get("strategy_id"))
        for line_position, line in enumerate(lines):
            amount, problems = derived_cash_issues(line, rates)
            if amount is None and not problems:
                continue
            keys = {_event_key(event) for event in rows(line, "events")}
            account = (strategy_id, str(node(line, "account_id")))
            repeated = bool(keys & seen_events) or account in seen_accounts
            if repeated:
                problems.append(
                    "the component repeats an account or events already counted in "
                    "the custody block (it would be summed twice)"
                )
            seen_events |= keys
            seen_accounts.add(account)
            found.append(
                CashLine(
                    strategy_id,
                    position,
                    line_position,
                    node(line, "account_id"),
                    amount,
                    tuple(problems),
                    counted=not problems,
                )
            )
    ctx._cache["derived_cash_lines"] = found
    return found


def derived_cash_by_strategy(ctx: Context) -> dict[str, Decimal]:
    """The unallocated cash each strategy's custody row publishes (OWNER_318).

    A strategy's capital is its sleeve account NAV plus this derived cash; only a component
    that ``derived_cash_lines`` finds sound counts, so an unverified or repeated line
    cannot make a total agree with itself.
    """

    cash: dict[str, Decimal] = {}
    for line in derived_cash_lines(ctx):
        if line.counted and line.amount is not None:
            cash[line.strategy_id] = (
                cash.get(line.strategy_id, Decimal(0)) + line.amount
            )
    return cash


def check_derived_cash_evidence(ctx: Context) -> Iterator[Finding]:
    """B13: every AVAILABLE derived cash line is sound, and counted once."""

    for line in derived_cash_lines(ctx):
        if not line.problems:
            continue
        yield _finding(
            CASH_EVIDENCE_CHECK,
            BLOCKER_INVARIANT,
            f"cash:{line.strategy_id}:{line.account_id}",
            line.amount,
            {
                "currency": "USD",
                "non_negative_dated_balance": True,
                "basis": _CASH_BASIS,
                "events_reproduce_the_amount": True,
                "each_event_counted_once": True,
            },
            REPORT,
            f"$.custody_current_capital.strategies[{line.position}]"
            f".derived_cash[{line.line_position}]",
            ("B13",),
            "derived cash is not sound: " + "; ".join(line.problems),
        )


def cutoff_capital_by_strategy(ctx: Context) -> dict[str, tuple[Decimal, list[str]]]:
    values = {
        str(row.get("account_id")): to_decimal(row.get("latest_value_usd"))
        for row in rows(ctx.report, "account_summary")
    }
    cash = derived_cash_by_strategy(ctx)
    totals: dict[str, tuple[Decimal, list[str]]] = {}
    for strategy, accounts in current_accounts_by_strategy(ctx).items():
        amount = cash.get(strategy, Decimal(0))
        for account in accounts:
            amount += values.get(account) or Decimal(0)
        totals[strategy] = (amount, sorted(accounts))
    return totals


def check_strategy_vs_cutoff_accounts(ctx: Context) -> Iterator[Finding]:
    """B4: strategy capital must equal the sum of its accounts' cutoff values."""

    totals = cutoff_capital_by_strategy(ctx)
    for position, row in enumerate(rows(ctx.report, "strategy_summary")):
        strategy = str(row.get("strategy_id"))
        shown = to_decimal(row.get("current_value_usd"))
        if strategy not in totals or shown is None:
            continue
        expected, accounts = totals[strategy]
        if abs(shown - expected) > USD_TOLERANCE:
            yield _finding(
                "capital.strategy_vs_cutoff_accounts",
                BLOCKER_INVARIANT,
                strategy,
                shown,
                {"sum_of_account_values_at_cutoff": expected, "accounts": accounts},
                REPORT,
                f"$.strategy_summary[{position}].current_value_usd",
                ("B4",),
                f"strategy capital differs from its accounts at the cutoff by "
                f"{shown - expected}",
            )
    systematic = to_decimal(node(ctx.panel, "capital", "systematic"))
    if systematic is not None and totals:
        cutoff_total = sum(amount for amount, _ in totals.values())
        if abs(systematic - cutoff_total) > USD_TOLERANCE:
            yield _finding(
                "capital.strategy_vs_cutoff_accounts",
                BLOCKER_INVARIANT,
                "portfolio.systematic",
                systematic,
                cutoff_total,
                PANEL,
                "$.capital.systematic",
                ("B4",),
                f"displayed systematic capital differs from the cutoff account values "
                f"by {systematic - cutoff_total}",
            )


def check_epoch_identity(ctx: Context) -> Iterator[Finding]:
    """The report and the panel carry each account epoch alike (fix round 3).

    One ``BLOCKER_INVARIANT`` per pair of a ``twr_mwr`` row and a panel manifest row
    that carry one epoch apart (``epoch_disagreements``), pointing at the report row:
    the two ends when one row stands in for the other, the two admission statuses when
    the epoch's whole identity is held only as a row of the other kind. They are
    published as ``capital.account_identity`` because no check id names an epoch
    identity; this is the one whose subject is the account's epoch row across the report
    and the panel, entity ``account|strategy|effective_start`` like its siblings. No
    defect id is attached: none describes it.
    """

    # A missing list is the legacy contract. An explicit list, even empty,
    # declares this cross-table universe and must account for each funded epoch.
    if isinstance(node(ctx.panel, "account_manifest", "rows"), list):
        blocked = blocked_epochs(ctx)
        for entries, match, skip, file, path in (
            (
                twr_rows(ctx),
                _manifest_epoch,
                blocked.skips_report_row,
                REPORT,
                "$.twr_mwr",
            ),
            (
                manifest_rows(ctx),
                _report_epoch,
                blocked.skips_manifest_row,
                PANEL,
                "$.account_manifest.rows",
            ),
        ):
            for position, row in entries:
                partner = match(ctx, row)
                if (
                    skip(row)
                    or partner.row is not None
                    or partner.disagreement is not None
                ):
                    continue
                yield _finding(
                    "capital.account_identity",
                    BLOCKER_AUTHORITY,
                    "|".join(epoch_key(row)),
                    {"epoch": epoch_identity(row)},
                    "same whole epoch identity and admission kind in the other table",
                    file,
                    f"{path}[{position}]",
                    (),
                    "account epoch counterpart is missing from the other declared table",
                )

    for item in epoch_disagreements(ctx):
        position, row = item.report
        panel_position, panel_row = item.panel
        shown = {
            "report": {
                "row": position,
                "effective_end": row.get("effective_end"),
                "twr_status": row.get("twr_status"),
                "epoch_admission_status": row.get("epoch_admission_status"),
            },
            "panel": {
                "row": panel_position,
                "effective_end": panel_row.get("effective_end"),
                "status": panel_row.get("status"),
                "admission_status": panel_row.get("admission_status"),
            },
        }
        key = epoch_key(row)
        if item.kind == END_DISAGREEMENT:
            expected = (
                "the same effective_end in the report's twr_mwr row and the panel's "
                "account_manifest row of one epoch"
            )
            message = (
                f"the report row {position} of {key[0]} / {key[1]} from {key[2]} ends "
                f"{row.get('effective_end')} but the panel manifest row {panel_position} "
                f"of the same start ends {panel_row.get('effective_end')}: the two are "
                "paired by account, strategy and start only, because one of them has "
                "no counterpart with its whole identity in the other table"
            )
        else:
            expected = (
                "one admission kind for an epoch, blocked or not, in the report's "
                "twr_mwr row and the panel's account_manifest row"
            )
            message = (
                f"the report row {position} and the panel manifest row "
                f"{panel_position} of {key[0]} / {key[1]} from {key[2]} (effective_end "
                f"{row.get('effective_end')}) carry the same epoch but only one of "
                "them is a blocked admission"
            )
        yield _finding(
            "capital.account_identity",
            BLOCKER_INVARIANT,
            "|".join(key),
            shown,
            expected,
            REPORT,
            f"$.twr_mwr[{position}].{item.kind}",
            (),
            message,
        )


def check_account_identity(ctx: Context) -> Iterator[Finding]:
    """Account-row capital arithmetic and terminal values agree across views.

    And the two tables carry each account epoch alike (``check_epoch_identity``).
    """

    yield from check_epoch_identity(ctx)
    summary = {
        str(row.get("account_id")): (position, row)
        for position, row in enumerate(rows(ctx.report, "account_summary"))
    }
    for position, row in sorted(
        manifest_rows(ctx), key=lambda entry: epoch_key(entry[1])
    ):
        key = epoch_key(row)
        amounts = {
            name: to_decimal(row.get(name))
            for name in (
                "opening_capital",
                "deposits",
                "internal_in",
                "external_withdrawals",
                "internal_out",
                "withdrawn",
                "invested",
                "net_capital",
                "current_value",
                "gain",
            )
        }
        if any(value is None for value in amounts.values()):
            continue
        a = {name: value for name, value in amounts.items() if value is not None}
        identities = {
            "net_capital": a["opening_capital"]
            + a["deposits"]
            + a["internal_in"]
            - a["external_withdrawals"]
            - a["internal_out"],
            "withdrawn": a["external_withdrawals"] + a["internal_out"],
            "invested": a["opening_capital"] + a["deposits"] + a["internal_in"],
            "gain": a["current_value"] - a["net_capital"],
        }
        for name, recomputed in identities.items():
            if abs(a[name] - recomputed) > USD_TOLERANCE:
                yield _finding(
                    "capital.account_identity",
                    BLOCKER_INVARIANT,
                    "|".join(key),
                    {name: a[name]},
                    {name: recomputed},
                    PANEL,
                    f"$.account_manifest.rows[{position}].{name}",
                    (),
                    "account capital arithmetic does not hold",
                )
        if row.get("effective_end") is not None:
            continue
        views: dict[str, Decimal | None] = {
            "manifest.current_value": a["current_value"]
        }
        matched = report_match(ctx, row)
        if matched is not None:
            views["twr_mwr.mwr_terminal_value"] = to_decimal(
                matched[1].get("mwr_terminal_value")
            )
            flows = matched[1].get("mwr_cashflows")
            if isinstance(flows, list) and flows and isinstance(flows[-1], dict):
                views["twr_mwr.mwr_cashflows[-1]"] = to_decimal(flows[-1].get("amount"))
        account = summary.get(key[0])
        if account is not None:
            views["account_summary.latest_value_usd"] = to_decimal(
                account[1].get("latest_value_usd")
            )
        present = [value for value in views.values() if value is not None]
        if present and spread(present) > USD_TOLERANCE:
            yield _finding(
                "capital.account_identity",
                BLOCKER_INVARIANT,
                "|".join(key),
                views,
                "all terminal account values equal",
                PANEL,
                f"$.account_manifest.rows[{position}].current_value",
                (),
                "the account's terminal value differs between views",
            )


# ----------------------------------------------------------------- universe checks

_STRATEGY_VIEWS: tuple[tuple[str, str, tuple[str | int, ...], str], ...] = (
    ("report.strategy_summary", REPORT, ("strategy_summary",), "strategy_id"),
    ("panel.strategies", PANEL, ("strategies",), "strategy_id"),
    (
        "panel.canonical_strategy_series",
        PANEL,
        ("canonical_strategy_series",),
        "strategy_id",
    ),
    (
        "report.strategy_attribution.comparisons",
        REPORT,
        ("strategy_attribution", "comparisons"),
        "strategy_id",
    ),
    (
        "report.strategy_attribution.costs",
        REPORT,
        ("strategy_attribution", "costs"),
        "strategy_id",
    ),
    (
        "report.strategy_attribution.benchmark_assignments",
        REPORT,
        ("strategy_attribution", "benchmark_assignments"),
        "strategy_id",
    ),
    (
        "report.strategy_provider_evidence",
        REPORT,
        ("strategy_provider_evidence",),
        "strategy_id",
    ),
    (
        "panel.strategy_underwriting.rows",
        PANEL,
        ("strategy_underwriting", "rows"),
        "strategy_id",
    ),
    (
        "report.invested_capital.current_by_strategy",
        REPORT,
        ("invested_capital", "current_by_strategy"),
        "strategy_id",
    ),
)
_RETIRED_VIEWS: tuple[tuple[str, str, tuple[str | int, ...], str], ...] = (
    _STRATEGY_VIEWS
    + (
        (
            "report.strategy_attribution.benchmark_comparisons",
            REPORT,
            ("strategy_attribution", "benchmark_comparisons"),
            "strategy_id",
        ),
        ("report.allocation_history", REPORT, ("allocation_history",), "strategy_id"),
    )
)


def _view_ids(
    ctx: Context, file: str, path: Sequence[str | int], key: str
) -> list[str] | None:
    document = ctx.report if file == REPORT else ctx.panel
    value = node(document, *path)
    if not isinstance(value, list):
        return None
    return [
        str(row.get(key))
        for row in value
        if isinstance(row, dict) and row.get(key) is not None
    ]


def check_active_universe(ctx: Context) -> Iterator[Finding]:
    """D9: every current view carries exactly the active strategy universe."""

    reference = universe(ctx)
    if reference is None:
        return
    active = reference.active_strategies
    for label, file, path, key in _STRATEGY_VIEWS:
        ids = _view_ids(ctx, file, path, key)
        if ids is None:
            continue
        missing = sorted(active - set(ids))
        if missing:
            yield _finding(
                "universe.active_set_consistent",
                BLOCKER_INVARIANT,
                label,
                {"missing_active_strategies": missing},
                sorted(active),
                file,
                "$." + ".".join(str(part) for part in path),
                ("D9",),
                f"current view omits active strategies (reference: {reference.source})",
            )
    for count_file, count_path, value in (
        (PANEL, "$.counts.strategies", node(ctx.panel, "counts", "strategies")),
        (
            PANEL,
            "$.strategy_underwriting.counts.strategies",
            node(ctx.panel, "strategy_underwriting", "counts", "strategies"),
        ),
    ):
        if isinstance(value, int) and value != len(active):
            yield _finding(
                "universe.active_set_consistent",
                BLOCKER_INVARIANT,
                count_path,
                value,
                len(active),
                count_file,
                count_path,
                ("D9",),
                f"displayed strategy count differs from the active universe "
                f"({reference.source})",
            )
    required = node(ctx.report, "invested_capital", "required_strategy_ids")
    if isinstance(required, list) and set(map(str, required)) != set(active):
        yield _finding(
            "universe.active_set_consistent",
            BLOCKER_INVARIANT,
            "invested_capital.required_strategy_ids",
            sorted(map(str, required)),
            sorted(active),
            REPORT,
            "$.invested_capital.required_strategy_ids",
            ("D9",),
            "required strategy set differs from the active universe",
        )


@dataclass(frozen=True)
class RenderedCount:
    file: str
    offset: int
    text: str
    number: int


def rendered_strategy_counts(ctx: Context) -> list[RenderedCount]:
    """Literal strategy totals in rendered text (markup, ARIA, labels, prose).

    Shell files accept qualified ("9 active strategies"), bare ("7 strategies") and
    label ("Strategies: 9") forms; payload prose only the qualified form, because
    payload text also counts subsets ("0 strategies are excluded").
    """

    found: dict[tuple[str, int], RenderedCount] = {}
    for hit in ctx.gen.scan.count_text_hits:
        base = max(0, hit.offset - 64)
        patterns = [_QUALIFIED_COUNT]
        if hit.file not in PAYLOAD_FILES and not hit.file.startswith("qa/"):
            patterns += [_BARE_COUNT, _LABEL_COUNT]
        for pattern in patterns:
            for match in pattern.finditer(hit.snippet):
                word = match.group(1).lower()
                number = _NUMBER_WORDS.get(word)
                if number is None and word.isdigit():
                    number = int(word)
                if number is None:
                    continue
                position = base + match.start()
                found.setdefault(
                    (hit.file, position),
                    RenderedCount(hit.file, position, match.group(0), number),
                )
    return [found[key] for key in sorted(found)]


def check_hardcoded_count_text(ctx: Context) -> Iterator[Finding]:
    """D9: rendered text must not hard-code a strategy count that differs."""

    reference = universe(ctx)
    if reference is None:
        return
    active = len(reference.active_strategies)
    for count in rendered_strategy_counts(ctx):
        if count.number != active:
            yield _finding(
                "universe.hardcoded_count_text",
                BLOCKER_INVARIANT,
                f"{count.file}@{count.offset}",
                count.text,
                f"{active} strategies (active universe, {reference.source})",
                count.file,
                f"(byte offset {count.offset})",
                ("D9",),
                "rendered text hard-codes a strategy count that differs from the "
                "active universe",
            )


def check_retired_strategies(ctx: Context) -> Iterator[Finding]:
    """D5: retired or excluded strategies must not appear in current outputs."""

    reference = universe(ctx)
    if reference is None:
        return
    for label, file, path, key in _RETIRED_VIEWS:
        ids = _view_ids(ctx, file, path, key)
        if ids is None:
            continue
        extra = sorted(set(ids) - reference.active_strategies - {"None"})
        if extra:
            yield _finding(
                "universe.retired_strategies_in_current",
                BLOCKER_AUTHORITY,
                label,
                {
                    strategy: reference.excluded_strategies.get(strategy, "NOT_ACTIVE")
                    for strategy in extra
                },
                sorted(reference.active_strategies),
                file,
                "$." + ".".join(str(part) for part in path),
                ("D5",),
                f"current output carries non-active strategies ({reference.source})",
            )


_ACCOUNT_VIEWS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("report.fact_coverage_matrix", REPORT, ("fact_coverage_matrix",), "account_id"),
    ("report.leverage_disclosures", REPORT, ("leverage_disclosures",), "account_id"),
    ("report.account_summary", REPORT, ("account_summary",), "account_id"),
    ("report.twr_mwr", REPORT, ("twr_mwr",), "account_id"),
)


def check_inactive_accounts(ctx: Context) -> Iterator[Finding]:
    """D6: inactive, retired or excluded accounts in current coverage/leverage."""

    reference = universe(ctx)
    if reference is None:
        return
    for label, file, path, key in _ACCOUNT_VIEWS:
        allowed = set(reference.active_accounts) | reserve_accounts(ctx, label)
        ids = _view_ids(ctx, file, path, key)
        if ids is None:
            continue
        counts: dict[str, int] = {}
        for account in ids:
            if account not in allowed:
                counts[account] = counts.get(account, 0) + 1
        excluded = {
            account: {
                "rows": count,
                "disposition": reference.excluded_accounts[account],
            }
            for account, count in sorted(counts.items())
            if account in reference.excluded_accounts
        }
        unknown = {
            account: count
            for account, count in sorted(counts.items())
            if account not in reference.excluded_accounts
        }
        if excluded:
            yield _finding(
                "universe.inactive_accounts_in_current",
                BLOCKER_AUTHORITY,
                label,
                excluded,
                "only current product-epoch accounts",
                file,
                "$." + ".".join(path),
                ("D6",),
                f"retired/excluded accounts appear in a current output ({reference.source})",
            )
        if unknown:
            yield _finding(
                "universe.inactive_accounts_in_current",
                WARNING,
                label,
                unknown,
                "only current product-epoch accounts",
                file,
                "$." + ".".join(path),
                ("D6",),
                "accounts outside the current product universe appear in a current "
                f"output; disposition needed ({reference.source})",
            )
    for position, row in enumerate(rows(ctx.report, "leverage_disclosures")):
        strategy = row.get("strategy_id")
        if isinstance(strategy, str) and strategy in reference.excluded_strategies:
            yield _finding(
                "universe.inactive_accounts_in_current",
                BLOCKER_AUTHORITY,
                "report.leverage_disclosures.strategy_id",
                strategy,
                sorted(reference.active_strategies),
                REPORT,
                f"$.leverage_disclosures[{position}].strategy_id",
                ("D6",),
                "leverage disclosure keyed to a retired/excluded strategy",
            )
            break


# ------------------------------------------------------------------ return checks


def check_twr_views(ctx: Context) -> Iterator[Finding]:
    """B1/B2/A1: every TWR shown for one account epoch and window must agree."""

    current = current_accounts_by_strategy(ctx)
    for position, row in twr_rows(ctx):
        key = epoch_key(row)
        entity = "|".join(key)
        views: list[tuple[str, Any, tuple[date | None, date | None]]] = [
            (
                f"report.twr_mwr[{position}].twr",
                row.get("twr"),
                (to_date(row.get("observed_start")), to_date(row.get("observed_end"))),
            )
        ]
        matched = manifest_match(ctx, row)
        if matched is not None:
            views.append(
                (
                    f"panel.account_manifest.rows[{matched[0]}].observation_linked_return",
                    matched[1].get("observation_linked_return"),
                    (to_date(matched[1].get("start")), to_date(matched[1].get("end"))),
                )
            )
        single = len(current.get(str(row.get("strategy_id")), [])) == 1
        if row.get("effective_end") is None and single:
            views.append(
                (
                    f"report.twr_mwr[{position}].return_path_cumulative_return",
                    row.get("return_path_cumulative_return"),
                    (
                        to_date(row.get("return_path_observed_start")),
                        to_date(row.get("return_path_observed_end")),
                    ),
                )
            )
        anchor = views[0][2]
        comparable = [view for view in views if windows_equivalent(anchor, view[2])]
        values = {name: to_decimal(value) for name, value, _ in comparable}
        present = [value for value in values.values() if value is not None]
        missing = [name for name, value in values.items() if value is None]
        defects = ["B2"]
        if missing and present:
            defects.append("B1")
        linked = [name for name in values if "observation_linked_return" in name]
        twr = values.get(views[0][0])
        if linked and twr is not None and values[linked[0]] is not None:
            linked_value = values[linked[0]]
            assert linked_value is not None
            if abs(twr - linked_value) > RETURN_TOLERANCE:
                defects.append("B1")
        if not (missing and present) and spread(present) <= RETURN_TOLERANCE:
            continue
        if present and spread(present) > Decimal(1):
            defects.append("A1")
        yield _finding(
            "returns.twr_views_agree",
            BLOCKER_INVARIANT,
            entity,
            {
                name: {"value": values[name], "window": [start, end]}
                for name, _, (start, end) in comparable
            },
            "one TWR per account epoch and window (tolerance 1e-8)",
            REPORT,
            f"$.twr_mwr[{position}].twr",
            defects,
            "TWR is unavailable in one view and available in another"
            if missing
            else f"TWR views for one account epoch disagree by {spread(present)}",
        )


@dataclass(frozen=True)
class _ReturnView:
    name: str
    value: Any
    window: tuple[date | None, date | None]
    json_path: str
    epoch: bool


def _return_group(
    strategy: str,
    window: tuple[date | None, date | None],
    views: Sequence[_ReturnView],
    json_path: str | None,
    expected: str,
    message: str,
) -> Iterator[Finding]:
    comparable = {
        view.name: to_decimal(view.value)
        for view in views
        if windows_equivalent(window, view.window)
    }
    present = [value for value in comparable.values() if value is not None]
    if len(present) < 2 or spread(present) <= RETURN_TOLERANCE:
        return
    if json_path is None:
        # Point at a published view of the group; the computed one only if alone.
        json_path = next(
            view.json_path
            for view in sorted(
                views, key=lambda view: view.name.startswith("computed:")
            )
            if windows_equivalent(window, view.window)
            and to_decimal(view.value) is not None
        )
    yield _finding(
        "returns.strategy_return_views_agree",
        BLOCKER_INVARIANT,
        strategy,
        comparable,
        expected,
        REPORT,
        json_path,
        ("B2",),
        f"{message} {spread(present)}",
    )


def check_strategy_return_views(ctx: Context) -> Iterator[Finding]:
    """B2: strategy cumulative returns shown for one window must agree.

    Two path bases, compared separately (C1 section 5-5). A strategy whose
    ACCOUNT_LIVE epoch declares HPR gap spans in
    ``strategy_history_contract.epoch_hpr_receipts``, or that publishes a value
    labelled ``EPOCH_CUMULATIVE_TWR_WITH_HASH_BOUND_HPR_GAPS``, has an epoch basis
    (epoch-labelled summary headline and provider-evidence account value, and the
    validator's own splice of the daily rows with the verified receipt spans) and a
    daily basis (daily-labelled or unlabelled values, the plain daily compound and
    the daily-only dependence performance). The bases are never compared with each
    other, except over a summary window that holds no daily row (below): they meet
    only through the receipt, whose identity ``(1 + daily) x prod(1 + span) = 1 +
    epoch`` and totals ``check_epoch_hpr_receipts`` re-derives from the report's own
    rows. A strategy with span receipts whose headline is not epoch-labelled is
    itself a finding. Without receipts or an epoch label every view is one daily
    group, exactly as before.

    A decline is never silent. When ``splice_window`` cannot give an epoch-basis
    value for the summary window (an edge strictly inside a span, or an unverified
    span inside the window) the epoch-labelled headline has nothing to be compared
    with; that is one ``BLOCKER_INVARIANT`` per strategy, at the summary field whose
    edge is inside the span (the headline for an unverified span), unless the decline
    is only an unverified span that a BLOCKER-class receipt finding already reports.

    A summary window that holds no daily row of its layer is the exception to the two
    bases. No row anchors either of them (there is no splice, so nothing to decline),
    and the epoch-labelled headline would be compared only with epoch-labelled views:
    a forged headline beside a same-window dependence performance or daily-labelled
    evidence value would pass. Every view of that window is one group, compared as for
    a strategy without an epoch basis; the label rule above still applies.
    """

    names = strategy_names(ctx)
    performance = node(
        ctx.report, "dependence_analysis", "mode_summaries", "LIVE_ONLY", "performance"
    )
    evidence = {
        str(row.get("strategy_id")): (position, row)
        for position, row in enumerate(rows(ctx.report, "strategy_provider_evidence"))
    }
    receipts = epoch_receipts(ctx)
    for position, row in enumerate(rows(ctx.report, "strategy_summary")):
        strategy = str(row.get("strategy_id"))
        window = (
            to_date(row.get("account_live_observed_start")),
            to_date(row.get("account_live_observed_end")),
        )
        headline_path = f"$.strategy_summary[{position}].account_live_cumulative_return"
        views = [
            _ReturnView(
                f"report.strategy_summary[{position}].account_live_cumulative_return",
                row.get("account_live_cumulative_return"),
                window,
                headline_path,
                row.get("account_live_cumulative_return_label") == EPOCH_RETURN_LABEL,
            )
        ]
        layer = str(row.get("nav_evidence_layer") or "ACCOUNT_LIVE")
        computed, count = compound_window(ctx, strategy, window[0], window[1], layer)
        compound_name = f"computed:compound(report.strategy_returns[{strategy}])"
        if count:
            views.append(
                _ReturnView(
                    compound_name, computed, window, "$.strategy_returns", False
                )
            )
        perf = node(performance, names.get(strategy, strategy))
        if isinstance(perf, Mapping):
            perf_path = (
                f"dependence_analysis.mode_summaries.LIVE_ONLY.performance"
                f"['{names.get(strategy, strategy)}'].cumulative_return"
            )
            views.append(
                _ReturnView(
                    f"report.{perf_path}",
                    perf.get("cumulative_return"),
                    (
                        to_date(perf.get("observed_start")),
                        to_date(perf.get("observed_end")),
                    ),
                    f"$.{perf_path}",
                    False,
                )
            )
        matched = evidence.get(strategy)
        if matched is not None and isinstance(matched[1].get("account"), Mapping):
            account = matched[1]["account"]
            views.append(
                _ReturnView(
                    f"report.strategy_provider_evidence[{matched[0]}].account"
                    ".cumulative_return",
                    account.get("cumulative_return"),
                    (
                        to_date(account.get("observed_start")),
                        to_date(account.get("observed_end")),
                    ),
                    f"$.strategy_provider_evidence[{matched[0]}].account"
                    ".cumulative_return",
                    account.get("cumulative_return_label") == EPOCH_RETURN_LABEL,
                )
            )
        receipt = receipts.get(strategy) if layer == "ACCOUNT_LIVE" else None
        declares_spans = receipt is not None and receipt.declares_spans
        if not declares_spans and not any(view.epoch for view in views):
            yield from _return_group(
                strategy,
                window,
                views,
                headline_path,
                "one cumulative return per strategy and window (tolerance 1e-8)",
                "strategy return views for one window disagree by",
            )
            continue
        spliced = splice_window(ctx, strategy, window[0], window[1], layer)
        if spliced.value is not None:
            views.append(
                _ReturnView(
                    compound_name
                    if not spliced.spans
                    else f"computed:splice(report.strategy_returns[{strategy}], "
                    f"{spliced.spans} verified epoch_hpr_receipts span(s))",
                    spliced.value,
                    window,
                    "$.strategy_returns",
                    True,
                )
            )
        elif spliced.rows and not spliced.reported:
            # The splice declined (a window edge strictly inside a span, or an
            # unverified span inside the window) and no receipt blocker says why: the
            # epoch-labelled views are left with no row-anchored value to be compared
            # with, and a forged headline would pass unseen. A window the epoch basis
            # cannot anchor to the rows is a finding, never a silence.
            field = (
                "account_live_cumulative_return"
                if spliced.edge is None
                else f"account_live_observed_{spliced.edge}"
            )
            yield _finding(
                "returns.strategy_return_views_agree",
                BLOCKER_INVARIANT,
                strategy,
                {
                    "account_live_cumulative_return": row.get(
                        "account_live_cumulative_return"
                    ),
                    "account_live_cumulative_return_label": row.get(
                        "account_live_cumulative_return_label"
                    ),
                    "window": list(window),
                    "declined": spliced.declined,
                },
                "an epoch-basis window anchored to the report's daily rows: the "
                "epoch-labelled views equal the daily rows spliced with the verified "
                "epoch HPR receipt spans",
                REPORT,
                f"$.strategy_summary[{position}].{field}",
                ("B2",),
                f"the epoch-basis views cannot be anchored to the rows: {spliced.declined}",
            )
        if not count:
            # No daily row of this layer is dated in the summary window, so neither
            # basis has a row to be anchored to: there is no splice to compare with
            # and no decline to report, and the epoch-labelled views would only be
            # compared with each other. A headline forged beside the same-window
            # dependence performance or a daily-labelled evidence account would pass
            # where a single group (as before the basis split) blocks it. Every view
            # of the window is one group, exactly as for a strategy with no epoch
            # basis; the label check below still applies.
            yield from _return_group(
                strategy,
                window,
                views,
                headline_path,
                "one cumulative return per strategy and window (tolerance 1e-8)",
                "strategy return views for one window disagree by",
            )
        else:
            yield from _return_group(
                strategy,
                window,
                [view for view in views if view.epoch],
                None,
                "one epoch-basis cumulative return per strategy and window: the "
                "epoch-labelled views equal the daily rows spliced with the verified "
                "epoch HPR receipt spans (tolerance 1e-8)",
                "epoch-basis strategy return views for one window disagree"
                + (
                    ""
                    if declares_spans
                    else " (no epoch HPR span receipt is published)"
                )
                + " by",
            )
            yield from _return_group(
                strategy,
                window,
                [view for view in views if not view.epoch],
                None,
                "one daily-basis cumulative return per strategy and window: the "
                "daily-labelled or unlabelled views equal the daily rows compounded "
                "without spans (tolerance 1e-8)",
                "daily-basis strategy return views for one window disagree by",
            )
        label = row.get("account_live_cumulative_return_label")
        headline = row.get("account_live_cumulative_return")
        if declares_spans and headline is not None and label != EPOCH_RETURN_LABEL:
            yield _finding(
                "returns.strategy_return_views_agree",
                BLOCKER_INVARIANT,
                strategy,
                {
                    "account_live_cumulative_return": headline,
                    "account_live_cumulative_return_label": label,
                },
                {"account_live_cumulative_return_label": EPOCH_RETURN_LABEL},
                REPORT,
                f"$.strategy_summary[{position}].account_live_cumulative_return_label",
                ("B2",),
                "the strategy declares epoch HPR gap spans but its headline is not "
                "labelled the epoch TWR",
            )


def check_epoch_hpr_receipts(ctx: Context) -> Iterator[Finding]:
    """B2 / C1 section 5-5: never trust an epoch HPR receipt.

    Each ACCOUNT_LIVE receipt in ``strategy_history_contract.epoch_hpr_receipts``
    is re-derived from the report's own ``strategy_returns``: every span's
    admission shape and its return against the capital chain, and the receipt's
    daily and epoch totals, row count and identity. A contradiction is a
    ``BLOCKER_INVARIANT``; a receipt the report gives no means to verify (malformed,
    duplicated, no daily rows, no capital chain) is a ``BLOCKER_AUTHORITY``. A span
    that fails is never spliced, so a forged receipt cannot launder a headline. A
    receipt that runs past the report's last published daily row cannot be fully
    re-derived: its spans from that row on are unverified and its totals are not
    recomputed, which is one ``NOT_READY`` per receipt (visible, never blocking).
    """

    yield from _epoch_receipt_index(ctx)[1]


RETAINED_ACCOUNT_SOURCE = "RETAINED_REFERENCE_ONLY_ACCOUNT_HISTORY"


def strategy_confirmed_retained(ctx: Context, strategy_id: str) -> bool:
    """Whether the authoritative crosswalk shows this strategy as not active.

    A comparison's own ``account_series_source`` label never establishes that: the
    strategy has to be absent from the active universe and present among the
    crosswalk's excluded strategies (retained or ended product epochs).
    """

    reference = universe(ctx)
    return (
        reference is not None
        and strategy_id in reference.excluded_strategies
        and strategy_id not in reference.active_strategies
    )


def check_benchmark_account_return(ctx: Context) -> Iterator[Finding]:
    """B3: the account return inside a benchmark comparison equals the live series."""

    summary = {
        str(row.get("strategy_id")): (position, row)
        for position, row in enumerate(rows(ctx.report, "strategy_summary"))
    }
    comparisons = node(ctx.report, "strategy_attribution", "benchmark_comparisons")
    if not isinstance(comparisons, list):
        return
    for position, row in enumerate(comparisons):
        if not isinstance(row, dict) or not isinstance(row.get("metrics"), Mapping):
            continue
        metrics = row["metrics"]
        strategy = str(row.get("strategy_id"))
        start, end = (
            to_date(metrics.get("observed_start")),
            to_date(metrics.get("observed_end")),
        )
        shown = to_decimal(metrics.get("account_cumulative_return"))
        computed, count = compound_window(ctx, strategy, start, end)
        pointer = f"$.strategy_attribution.benchmark_comparisons[{position}].metrics"
        if shown is None:
            continue
        if (
            count == 0
            and row.get("account_series_source") == RETAINED_ACCOUNT_SOURCE
            and strategy_confirmed_retained(ctx, strategy)
        ):
            # A retained strategy's comparison discloses a reference-only account
            # series (ruling S1 of N8): it has no live series by design. The label
            # alone grants nothing: the crosswalk has to show the strategy as retained.
            continue
        if count == 0:
            yield _finding(
                "returns.benchmark_account_return",
                NOT_READY,
                strategy,
                {"benchmark_account_cumulative_return": shown, "live_series_rows": 0},
                "a live series for the benchmark window",
                REPORT,
                pointer,
                ("B3",),
                "benchmark account return has no live series in this generation",
            )
            continue
        assert computed is not None
        if abs(shown - computed) > RETURN_TOLERANCE:
            yield _finding(
                "returns.benchmark_account_return",
                BLOCKER_INVARIANT,
                strategy,
                {
                    "benchmark_account_cumulative_return": shown,
                    "benchmark_observations": metrics.get("observations"),
                },
                {
                    "compound_live_series": computed,
                    "live_series_rows_in_window": count,
                    "window": [start, end],
                },
                REPORT,
                f"{pointer}.account_cumulative_return",
                ("B3",),
                f"benchmark account return differs from the live series over the same "
                f"window by {shown - computed}",
            )
        matched = summary.get(strategy)
        if matched is not None:
            displayed = to_decimal(
                matched[1].get("benchmark_account_cumulative_return")
            )
            if displayed is not None and abs(displayed - shown) > RETURN_TOLERANCE:
                yield _finding(
                    "returns.benchmark_account_return",
                    BLOCKER_INVARIANT,
                    strategy,
                    displayed,
                    shown,
                    REPORT,
                    f"$.strategy_summary[{matched[0]}].benchmark_account_cumulative_return",
                    ("B3",),
                    "displayed benchmark account return differs from the comparison",
                )


def _interior_cashflows(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    flows = [item for item in row.get("mwr_cashflows") or [] if isinstance(item, dict)]
    return flows[1:-1] if len(flows) >= 2 else []


def check_flow_lists(ctx: Context) -> Iterator[Finding]:
    """B1: the TWR flow list, the MWR cash flows and the capital trace are one list.

    L6b (M3, N27): the MWR opens at the manifest's ``performance_opening_value`` (the
    NAV at the boundary), not at the contributed ``opening_capital``, so the
    non-terminal MWR cash flows sum to ``net_capital - opening_capital +
    performance_opening_value``. A row without ``performance_opening_value`` (a
    pre-M3 generation) keeps the original comparison with ``net_capital``. A row that
    carries the value but no readable ``opening_capital`` is itself the finding: it
    never falls back to the pre-M3 comparison (fix round 1).
    """

    for position, row in twr_rows(ctx):
        key = epoch_key(row)
        matched = manifest_match(ctx, row)
        if matched is None:
            continue
        trace = [
            item
            for item in matched[1].get("capital_flow_trace") or []
            if isinstance(item, dict)
        ]
        flow_count = row.get("flow_count")
        if isinstance(flow_count, int) and flow_count != len(trace):
            yield _finding(
                "returns.flow_lists_agree",
                BLOCKER_INVARIANT,
                "|".join(key),
                {"twr_flow_count": flow_count},
                {"capital_flow_trace_events": len(trace)},
                REPORT,
                f"$.twr_mwr[{position}].flow_count",
                ("B1",),
                "the TWR path counts a different flow list than the capital trace",
            )
        flows = [
            item for item in row.get("mwr_cashflows") or [] if isinstance(item, dict)
        ]
        net = to_decimal(matched[1].get("net_capital"))
        if len(flows) >= 2 and net is not None:
            invested = -sum(
                (to_decimal(item.get("amount")) or Decimal(0)) for item in flows[:-1]
            )
            contributed = to_decimal(matched[1].get("opening_capital"))
            performance_opening = to_decimal(
                matched[1].get("performance_opening_value")
            )
            if performance_opening is not None:
                if contributed is None:
                    # An M3 row without a readable contributed basis cannot be tied
                    # to its net capital; falling back to the pre-M3 comparison would
                    # pass it, or blame net_capital, instead of naming the field.
                    yield _finding(
                        "returns.flow_lists_agree",
                        BLOCKER_INVARIANT,
                        "|".join(key),
                        {
                            "minus_sum_of_non_terminal_mwr_cashflows": invested,
                            "net_capital": net,
                            "performance_opening_value": performance_opening,
                            "opening_capital": matched[1].get("opening_capital"),
                        },
                        {
                            "opening_capital": "a readable amount beside the "
                            "performance_opening_value"
                        },
                        REPORT,
                        f"$.twr_mwr[{position}].mwr_cashflows",
                        ("B1",),
                        "the manifest row carries a performance opening value but no "
                        "readable opening capital, so the MWR cash flows cannot be "
                        "tied to its net capital",
                    )
                else:
                    expected_invested = net - contributed + performance_opening
                    if abs(invested - expected_invested) > USD_TOLERANCE:
                        yield _finding(
                            "returns.flow_lists_agree",
                            BLOCKER_INVARIANT,
                            "|".join(key),
                            {"minus_sum_of_non_terminal_mwr_cashflows": invested},
                            {
                                "net_capital_less_opening_capital_plus_performance_"
                                "opening_value": expected_invested,
                                "net_capital": net,
                                "opening_capital": contributed,
                                "performance_opening_value": performance_opening,
                            },
                            REPORT,
                            f"$.twr_mwr[{position}].mwr_cashflows",
                            ("B1",),
                            "MWR cash flows do not sum to the account's net capital "
                            "with the performance opening value in place of the "
                            "contributed opening capital",
                        )
            elif abs(invested - net) > USD_TOLERANCE:
                yield _finding(
                    "returns.flow_lists_agree",
                    BLOCKER_INVARIANT,
                    "|".join(key),
                    {"minus_sum_of_non_terminal_mwr_cashflows": invested},
                    {"net_capital": net},
                    REPORT,
                    f"$.twr_mwr[{position}].mwr_cashflows",
                    ("B1",),
                    "MWR cash flows do not sum to the account's net capital",
                )


def check_mwr_opening(ctx: Context) -> Iterator[Finding]:
    """B8: opening value, first cash flow and first-flow timing agree.

    L6b (M3, N27): the report's MWR opening value and date are the manifest's
    ``performance_opening_value`` and ``performance_opening_date`` (the NAV at the
    boundary). A manifest row without ``performance_opening_value`` (a pre-M3
    generation) keeps the original comparison with ``opening_capital``. A row that
    carries the value but no readable ``performance_opening_date`` cannot have its
    opening date compared, and is itself the finding (fix round 1).
    """

    for position, row in twr_rows(ctx):
        flows = [
            item for item in row.get("mwr_cashflows") or [] if isinstance(item, dict)
        ]
        opening = to_decimal(row.get("mwr_opening_value"))
        if not flows or opening is None:
            continue
        first_amount = to_decimal(flows[0].get("amount"))
        observed = {
            "mwr_opening_date": row.get("mwr_opening_date"),
            "mwr_opening_value": opening,
            "first_cashflow_date": flows[0].get("date"),
            "first_cashflow_amount": first_amount,
        }
        matched = manifest_match(ctx, row)
        performance_opening = (
            to_decimal(matched[1].get("performance_opening_value"))
            if matched is not None
            else None
        )
        manifest_date: str | None = None
        date_unreadable = False
        if performance_opening is not None and matched is not None:
            manifest_opening: Decimal | None = performance_opening
            observed["manifest_performance_opening_value"] = performance_opening
            raw_date = matched[1].get("performance_opening_date")
            observed["manifest_performance_opening_date"] = raw_date
            if isinstance(raw_date, str) and to_date(raw_date) is not None:
                manifest_date = raw_date
            else:
                # the value is there, so the date has to be: a row that does not
                # supply one must not pass the date clause by omission
                date_unreadable = True
        else:
            manifest_opening = (
                to_decimal(matched[1].get("opening_capital"))
                if matched is not None
                else None
            )
            if manifest_opening is not None:
                observed["manifest_opening_capital"] = manifest_opening
        disagree = (
            flows[0].get("date") != row.get("mwr_opening_date")
            or first_amount is None
            or abs(-first_amount - opening) > USD_TOLERANCE
            or (
                manifest_opening is not None
                and abs(manifest_opening - opening) > USD_TOLERANCE
            )
            or (
                manifest_date is not None
                and row.get("mwr_opening_date") != manifest_date
            )
        )
        if disagree or date_unreadable:
            reasons: list[str] = []
            if disagree:
                reasons.append(
                    "opening value and first cash flow disagree (flow merged into "
                    "or shifted from the opening)"
                )
            if date_unreadable:
                reasons.append(
                    "the manifest row carries a performance opening value but no "
                    "readable performance opening date, so the MWR opening date "
                    "cannot be compared with it"
                )
            yield _finding(
                "returns.mwr_opening_matches_first_flow",
                BLOCKER_INVARIANT,
                "|".join(epoch_key(row)),
                observed,
                "first cash flow = -opening value on the opening date",
                REPORT,
                f"$.twr_mwr[{position}].mwr_cashflows[0]",
                ("B8",),
                "; ".join(reasons),
            )


def check_implied_flows(ctx: Context) -> Iterator[Finding]:
    """B14: with no recorded flows, NAV endpoints and the return path must agree."""

    current = current_accounts_by_strategy(ctx)
    for position, row in twr_rows(ctx):
        if row.get("effective_end") is not None:
            continue
        if len(current.get(str(row.get("strategy_id")), [])) != 1:
            continue
        matched = manifest_match(ctx, row)
        trace = matched[1].get("capital_flow_trace") if matched else None
        if trace or _interior_cashflows(row):
            continue
        opening = to_decimal(row.get("mwr_opening_value"))
        terminal = to_decimal(row.get("mwr_terminal_value"))
        path = to_decimal(row.get("return_path_cumulative_return"))
        if opening is None or terminal is None or path is None or opening == 0:
            continue
        implied = terminal - opening * (Decimal(1) + path)
        if abs(implied) > USD_TOLERANCE:
            yield _finding(
                "returns.implied_unrecorded_flows",
                BLOCKER_INVARIANT,
                "|".join(epoch_key(row)),
                {
                    "opening_value": opening,
                    "terminal_value": terminal,
                    "return_path_cumulative_return": path,
                    "implied_unrecorded_flow_usd": implied.quantize(Decimal("0.01")),
                },
                {"recorded_flows": 0, "implied_flow_usd": "0"},
                REPORT,
                f"$.twr_mwr[{position}]",
                ("B14",),
                "NAV endpoints imply an external flow that no flow record carries",
            )


def _policy_weights(
    ctx: Context,
) -> tuple[dict[str, tuple[str, Decimal]], dict[str, str]]:
    """Read independent policy authority once; never derive it from displayed weights."""
    if "policy_weights" in ctx._cache:
        return cast(
            tuple[dict[str, tuple[str, Decimal]], dict[str, str]],
            ctx._cache["policy_weights"],
        )
    root = (
        ctx.repo_root
        if ctx.repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    payload = json.loads(
        (root / "contracts" / "portfolio-dependence-policy-v1.json").read_text(
            encoding="utf-8"
        )
    )
    if not isinstance(payload, dict) or payload.get("policy_id") != "PORT-DEPENDENCE-1":
        raise ValueError("dependence policy identity differs")
    day = node(payload, "allocation_authority", "as_of_date")
    parsed_day = to_date(day)
    if (
        not isinstance(day, str)
        or len(day) != 10
        or parsed_day is None
        or day != parsed_day.isoformat()
    ):
        raise ValueError("dependence policy allocation date is invalid")
    source = payload.get("series")
    if not isinstance(source, list) or not source:
        raise ValueError("dependence policy series are absent")
    declared: dict[str, tuple[str, Decimal]] = {}
    strategies: set[str] = set()
    for row in source:
        if not isinstance(row, dict):
            raise ValueError("dependence policy series is invalid")
        series, strategy = row.get("series_id"), row.get("strategy_id")
        weight = to_decimal(row.get("allocation_weight"))
        if (
            not isinstance(series, str)
            or not series.strip()
            or not isinstance(strategy, str)
            or not strategy.strip()
            or series in declared
            or strategy in strategies
            or weight is None
            or weight < 0
        ):
            raise ValueError("dependence policy series identity or weight is invalid")
        declared[series] = (strategy, weight)
        strategies.add(strategy)
    total = sum((weight for _, weight in declared.values()), Decimal(0))
    if total <= 0:
        raise ValueError("dependence policy weight total is not positive")
    result = (
        {
            name: (strategy, weight / total)
            for name, (strategy, weight) in declared.items()
        },
        {
            "policy_id": "PORT-DEPENDENCE-1",
            "as_of_date": str(day),
            "normalization": "NORMALIZE_WITHIN_OWNER_APPROVED_REQUIRED_DEPENDENCE_PERIMETER",
        },
    )
    ctx._cache["policy_weights"] = result
    return result


def _current_capital_values(ctx: Context) -> dict[str, Decimal]:
    values = {
        strategy: amount
        for strategy, (amount, _) in cutoff_capital_by_strategy(ctx).items()
    }
    for row in ctx.expected or []:
        if row.get("metric_id") == "capital.strategy.cutoff":
            amount = to_decimal(row.get("expected"))
            if amount is not None:
                values[str(row.get("entity"))] = amount
    return values


def check_allocation_weights(ctx: Context) -> Iterator[Finding]:
    """B5: current shares and policy weights are distinct, independently checked sets."""
    tolerance = Decimal("5e-8")
    names = strategy_names(ctx)
    by_name = {name: strategy for strategy, name in names.items()}
    sets: dict[str, dict[str, Decimal]] = {}

    def problem(
        entity: str, shown: Any, expected: Any, path: str, message: str
    ) -> Finding:
        return _finding(
            "returns.allocation_weight_sets",
            BLOCKER_AUTHORITY,
            entity,
            shown,
            expected,
            REPORT,
            path,
            ("B5",),
            message,
        )

    for section in ("allocation_history", "strategy_summary"):
        for position, row in enumerate(rows(ctx.report, section)):
            if section == "allocation_history":
                if row.get("scope") != "SYSTEMATIC" or _historical_snapshot(row):
                    continue
            strategy = str(row.get("strategy_id"))
            basis = row.get("weight_basis")
            path = f"$.{section}[{position}]"
            if basis != "CURRENT_CUTOFF":
                yield problem(
                    strategy,
                    basis,
                    "CURRENT_CUTOFF or a correctly dated historical allocation",
                    path + ".weight_basis",
                    "current weight basis is missing or unrecognized",
                )
                continue
            amount = to_decimal(row.get("systematic_weight"))
            if amount is None:
                if row.get("current_value_usd") is not None:
                    yield problem(
                        strategy,
                        row.get("systematic_weight"),
                        "finite current weight",
                        path + ".systematic_weight",
                        "available capital lacks a finite weight",
                    )
            else:
                sets.setdefault(strategy, {})[f"report.{section}.systematic_weight"] = (
                    amount
                )
    totals = _current_capital_values(ctx)
    grand = sum(totals.values(), Decimal(0))
    if grand > 0:
        for strategy, amount in totals.items():
            sets.setdefault(strategy, {})["computed:cutoff_account_value_share"] = (
                amount / grand
            )

    dependence = node(ctx.report, "dependence_analysis")
    modes = node(ctx.report, "dependence_analysis", "mode_summaries")
    if isinstance(modes, dict) and modes:
        basis = dependence.get("weight_basis") if isinstance(dependence, dict) else None
        policy = None
        if basis == "POLICY_PORT_DEPENDENCE_1":
            try:
                policy, authority = _policy_weights(ctx)
            except (OSError, ValueError, TypeError, ArithmeticError) as exc:
                yield problem(
                    "policy",
                    None,
                    "usable independent PORT-DEPENDENCE-1 policy",
                    "$.dependence_analysis.weight_basis",
                    str(exc),
                )
            else:
                shown_authority = dependence.get("weight_authority")
                if "weight_authority" in dependence and shown_authority != authority:
                    yield problem(
                        "policy",
                        shown_authority,
                        authority,
                        "$.dependence_analysis.weight_authority",
                        "policy weight authority differs",
                    )
        elif basis != "CURRENT_CUTOFF":
            yield problem(
                "dependence",
                basis,
                "POLICY_PORT_DEPENDENCE_1 or CURRENT_CUTOFF",
                "$.dependence_analysis.weight_basis",
                "dependence weight basis is missing or unknown",
            )
        for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
            for block in ("correlation_one", "central_case"):
                components = node(modes, mode, block, "components")
                if not isinstance(components, list):
                    continue
                observed_names = []
                for position, item in enumerate(components):
                    if not isinstance(item, dict):
                        yield problem(
                            "dependence",
                            item,
                            "weight component object",
                            "$.dependence_analysis",
                            "weight component is invalid",
                        )
                        continue
                    name = str(item.get("series_id"))
                    observed_names.append(name)
                    amount = to_decimal(item.get("allocation_weight"))
                    path = f"$.dependence_analysis.mode_summaries.{mode}.{block}.components[{position}].allocation_weight"
                    if basis == "POLICY_PORT_DEPENDENCE_1" and policy is not None:
                        declared = policy.get(name)
                        if (
                            declared is None
                            or by_name.get(name) != declared[0]
                            or amount is None
                            or abs(amount - declared[1]) > tolerance
                        ):
                            yield problem(
                                name,
                                item.get("allocation_weight"),
                                declared[1] if declared else "declared policy series",
                                path,
                                "policy component differs from independent normalized policy weight",
                            )
                    elif basis == "CURRENT_CUTOFF":
                        matched_strategy = by_name.get(name)
                        if matched_strategy is None or amount is None:
                            yield problem(
                                name,
                                item,
                                "known current strategy with finite weight",
                                path,
                                "current component identity or weight is invalid",
                            )
                        else:
                            sets.setdefault(matched_strategy, {})[
                                f"report.dependence_analysis.{mode}.{block}.allocation_weight"
                            ] = amount
                if policy is not None and (
                    set(observed_names) != set(policy)
                    or len(observed_names) != len(set(observed_names))
                ):
                    yield problem(
                        mode + ":" + block,
                        observed_names,
                        sorted(policy),
                        f"$.dependence_analysis.mode_summaries.{mode}.{block}.components",
                        "policy component perimeter differs",
                    )
    for strategy in sorted(sets):
        views = sets[strategy]
        if len(views) < 2 or spread(list(views.values())) <= tolerance:
            continue
        yield _finding(
            "returns.allocation_weight_sets",
            BLOCKER_INVARIANT,
            strategy,
            views,
            "one current systematic weight per strategy",
            REPORT,
            "$.strategy_summary[*].systematic_weight",
            ("B5", "B4"),
            f"current weight differs across views by {spread(list(views.values()))}",
        )


# -------------------------------------------------------------- flow / admission


def check_flow_admission(ctx: Context) -> Iterator[Finding]:
    """D2: flows used for capital/MWR must be admitted flow records."""

    admitted: dict[str, int] = {}
    for row in rows(ctx.report, "transactions_and_flows"):
        if row.get("record_type") == "FLOW":
            account = str(row.get("account_id"))
            admitted[account] = admitted.get(account, 0) + 1
    used: dict[str, int] = {}
    for _, row in manifest_rows(ctx):
        account = str(row.get("account_id"))
        trace = row.get("capital_flow_trace")
        used[account] = used.get(account, 0) + (
            len(trace) if isinstance(trace, list) else 0
        )
    coverage = {
        str(row.get("account_id")): (position, row)
        for position, row in enumerate(rows(ctx.report, "fact_coverage_matrix"))
        if row.get("fact_type") == "flows"
    }
    for position, row in twr_rows(ctx):
        if row.get("effective_end") is not None:
            continue
        account = str(row.get("account_id"))
        if used.get(account, 0) > admitted.get(account, 0):
            yield _finding(
                "flows.admission_consistency",
                BLOCKER_AUTHORITY,
                account,
                {"flows_used_by_capital_and_mwr": used.get(account, 0)},
                {"admitted_flow_records": admitted.get(account, 0)},
                REPORT,
                "$.transactions_and_flows[?record_type=='FLOW']",
                ("D2",),
                "capital/MWR use flows that are not admitted flow records",
            )
        located = coverage.get(account)
        if (
            located is not None
            and located[1].get("status") in _UNADMITTED_FLOW_PATHS
            and row.get("mwr_coverage_status") == "COMPLETE"
            # mwr_coverage_status is the manifest's flow-coverage receipt, copied
            # verbatim; the report's flows coverage status travels beside it as
            # mwr_flow_path_status.  A row that discloses the very status the
            # coverage matrix carries makes the receipt truthful, not a claim of
            # an admitted path.
            and row.get("mwr_flow_path_status") != located[1].get("status")
        ):
            yield _finding(
                "flows.admission_consistency",
                BLOCKER_AUTHORITY,
                account,
                {
                    "flow_fact_path": located[1].get("status"),
                    "mwr_coverage_status": "COMPLETE",
                },
                "COMPLETE flow coverage only over an admitted flow path",
                REPORT,
                f"$.twr_mwr[{position}].mwr_coverage_status",
                ("D2",),
                "MWR claims complete flows for an account with no admitted flow path",
            )


# ------------------------------------------------------------------ status checks


def _walk_dicts(
    value: Any, path: str, depth: int = 0
) -> Iterator[tuple[str, dict[str, Any]]]:
    if depth > 64:
        return
    if isinstance(value, dict):
        yield path, value
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                yield from _walk_dicts(item, f"{path}.{key}", depth + 1)
    elif isinstance(value, list):
        for position, item in enumerate(value):
            if isinstance(item, (dict, list)):
                yield from _walk_dicts(item, f"{path}[{position}]", depth + 1)


def check_required_not_ready(ctx: Context) -> Iterator[Finding]:
    """D1: REQUIRED_NOT_READY must never render as available."""

    for file, document in ((REPORT, ctx.report), (PANEL, ctx.panel)):
        for path, item in _walk_dicts(document, "$"):
            readiness = {
                key: value
                for key, value in item.items()
                if "readiness" in key
                and isinstance(value, str)
                and value in _NOT_READY_VALUES
            }
            status = item.get("status")
            if readiness and is_available(status):
                yield _finding(
                    "status.required_not_ready_displayed_available",
                    BLOCKER_AUTHORITY,
                    path,
                    {"status": status, **readiness},
                    "status must not be AVAILABLE while readiness is NOT_READY",
                    file,
                    f"{path}.status",
                    ("D1",),
                    "a required-not-ready output is displayed as available",
                )


def check_backtest_status(ctx: Context) -> Iterator[Finding]:
    """D3: backtest cannot be UNAVAILABLE while live+backtest history is used."""

    eligible = {
        str(item.get("strategy_id"))
        for item in rows(node(ctx.panel, "hybrid_mode_gate"), "eligible_strategies")
    }
    for position, row in enumerate(rows(ctx.report, "strategy_summary")):
        strategy = str(row.get("strategy_id"))
        uses_history = (
            str(row.get("live_plus_backtest_status", "")).startswith("ELIGIBLE")
            or str(row.get("history_compatibility_status", "")).startswith("ACCEPTED")
            or strategy in eligible
        )
        if row.get("backtest_status") == "UNAVAILABLE" and uses_history:
            yield _finding(
                "status.backtest_vs_hybrid",
                BLOCKER_INVARIANT,
                strategy,
                {
                    "backtest_status": row.get("backtest_status"),
                    "live_plus_backtest_status": row.get("live_plus_backtest_status"),
                    "history_compatibility_status": row.get(
                        "history_compatibility_status"
                    ),
                    "hybrid_gate_eligible": strategy in eligible,
                },
                "backtest status available whenever live+backtest history is used",
                REPORT,
                f"$.strategy_summary[{position}].backtest_status",
                ("D3",),
                "backtest shown unavailable while its history feeds LIVE_PLUS_BACKTEST",
            )


def check_inference_version(ctx: Context) -> Iterator[Finding]:
    """D4: economic inference may not be accepted while the version is unresolved."""

    gate = {
        str(item.get("strategy_id")): item.get("compatibility_status")
        for item in rows(node(ctx.panel, "hybrid_mode_gate"), "eligible_strategies")
    }
    for position, row in enumerate(rows(ctx.report, "strategy_summary")):
        if (
            row.get("history_compatibility_status") == "ACCEPTED_FOR_ECONOMIC_INFERENCE"
            and row.get("version_compatibility_status") == "UNRESOLVED"
        ):
            strategy = str(row.get("strategy_id"))
            yield _finding(
                "status.inference_version_unresolved",
                BLOCKER_AUTHORITY,
                strategy,
                {
                    "history_compatibility_status": row.get(
                        "history_compatibility_status"
                    ),
                    "version_compatibility_status": row.get(
                        "version_compatibility_status"
                    ),
                    "hybrid_gate_compatibility_status": gate.get(strategy),
                },
                "inference accepted only with a resolved (compatible) version",
                REPORT,
                f"$.strategy_summary[{position}].history_compatibility_status",
                ("D4",),
                "economic inference accepted although the strategy version is unresolved",
            )


def check_methodology(ctx: Context) -> Iterator[Finding]:
    """B10: methodology text must not contradict the data it describes."""

    available = sum(
        1 for _, row in twr_rows(ctx) if row.get("mwr_status") == "AVAILABLE"
    )
    for position, row in enumerate(rows(ctx.report, "methodology")):
        text = str(row.get("value", ""))
        if (
            row.get("item") == "MWR"
            and text.upper().startswith("UNAVAILABLE")
            and available
        ):
            yield _finding(
                "status.methodology_vs_data",
                BLOCKER_INVARIANT,
                "methodology.MWR",
                text,
                f"consistent with {available} AVAILABLE MWR rows",
                REPORT,
                f"$.methodology[{position}].value",
                ("B10",),
                "methodology says MWR is unavailable while MWR rows are available",
            )


# A clause the report scopes to the provider that declared it and the date of its
# generation ("schwab (as of 2026-09-18): ...", portfolio_report._scoped_known_gap): a
# statement about that provider's snapshot, not about the balances the panel shows.
_PROVIDER_SCOPED_CLAUSE = re.compile(
    r"^[a-z0-9_-]+(?:/[a-z0-9_-]+)* \(as of \d{4}-\d{2}-\d{2}\): "
)


def check_gate_notes(ctx: Context) -> Iterator[Finding]:
    """E9: a gate note cannot claim balances unavailable when they are shown."""

    shown = [
        row
        for row in rows(ctx.report, "account_summary")
        if is_available(row.get("status"))
        and to_date(row.get("as_of_date")) == ctx.cutoff
    ]
    for position, row in enumerate(rows(ctx.panel, "gates")):
        note = str(row.get("note", ""))
        if (
            any(
                re.search(r"(?i)balances?\s+(?:remain|are|is)\s+unavailable", clause)
                and not _PROVIDER_SCOPED_CLAUSE.match(clause)
                for clause in note.split("; ")
            )
            and shown
        ):
            yield _finding(
                "status.gate_note_contradiction",
                BLOCKER_INVARIANT,
                str(row.get("item")),
                "note says account balances remain unavailable",
                f"{len(shown)} account balances available at the cutoff",
                PANEL,
                f"$.gates[{position}].note",
                ("E9",),
                "gate note contradicts the balances the panel displays",
            )


# ------------------------------------------------------------------- date checks


def _policies(ctx: Context) -> dict[str, dict[str, Any]]:
    return {
        str(policy.get("policy_id")): policy
        for policy in rows(ctx.gen.freshness, "policies")
    }


def _age(policy: Mapping[str, Any], last: date, cutoff: date) -> int:
    if policy.get("calendar") == "WEEKDAY_SESSION":
        return weekdays_between(last, cutoff)
    return max(0, (cutoff - last).days)


_LEDGER_IDENTITY_FIELDS = frozenset(
    {
        "as_of_date",
        "catalog_sha256",
        "catalog_size_bytes",
        "generation_id",
        "manifest_sha256",
        "manifest_size_bytes",
    }
)


def _ledger_identity_problem(
    ledger: Any, cutoff: date, executed: date | None
) -> str | None:
    """Name what makes a schema-4 provider-ledger identity unusable, else ``None``.

    Mirrors the pipeline gate: exactly the six identity fields, 64-hex digests,
    positive integer sizes, and a manifest date that follows the cutoff but not the
    execution date.
    """

    if not isinstance(ledger, Mapping) or not ledger:
        return "it is not a non-empty mapping"
    if set(ledger) != _LEDGER_IDENTITY_FIELDS:
        return "its fields differ from the identity fields"
    for name in ("catalog_sha256", "generation_id", "manifest_sha256"):
        value = ledger[name]
        if not isinstance(value, str) or not _RELEASE_DIGEST.fullmatch(value):
            return f"{name} is not a sha-256 digest"
    for name in ("catalog_size_bytes", "manifest_size_bytes"):
        size = ledger[name]
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            return f"{name} is not a positive integer"
    raw = ledger["as_of_date"]
    try:
        manifest_day = date.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        manifest_day = None
    if manifest_day is None or manifest_day.isoformat() != raw:
        return "as_of_date is not a canonical date"
    if manifest_day < cutoff:
        return "its manifest date precedes the data cutoff"
    if executed is not None and manifest_day > executed:
        return "its manifest date follows the execution date"
    return None


def check_freshness(ctx: Context) -> Iterator[Finding]:
    """B12/D7: validate operating freshness separately from financial coverage."""

    cutoff = ctx.cutoff

    def canonical_day(value: Any) -> date | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.isoformat() == value else None

    def authority(
        entity: str,
        observed: Any,
        expected: Any,
        path: str,
        message: str,
    ) -> Finding:
        return _finding(
            "dates.freshness_vs_cutoff",
            BLOCKER_AUTHORITY,
            entity,
            observed,
            expected,
            FRESHNESS,
            path,
            ("B12",),
            message,
        )

    raw_evaluated = node(ctx.gen.freshness, "as_of_date")
    evaluated = canonical_day(raw_evaluated)
    if evaluated is None:
        yield authority(
            "freshness-health",
            raw_evaluated,
            "canonical operational as_of_date",
            "$.as_of_date",
            "freshness operational clock is missing or invalid",
        )
        return

    if node(ctx.gen.freshness, "assessment_schema_version") == 4:
        # Schema 4 is dated at the data cutoff and bound to the ledger it was judged
        # against; the formal schema and the pipeline gate require both.
        fresh_map = cast(Mapping[str, Any], ctx.gen.freshness)
        raw_executed = fresh_map.get("evaluated_on")
        executed = canonical_day(raw_executed)
        if executed is None or executed < evaluated:
            yield authority(
                "freshness-health",
                raw_executed,
                f"canonical execution date not before {evaluated.isoformat()}",
                "$.evaluated_on",
                "a schema-4 freshness bundle carries no valid execution date",
            )
        if "provider_ledger" not in fresh_map:
            yield authority(
                "freshness-health",
                None,
                "the provider ledger identity the bundle was judged against",
                "$.provider_ledger",
                "a schema-4 freshness bundle carries no provider ledger identity",
            )
        else:
            ledger = fresh_map["provider_ledger"]
            problem = _ledger_identity_problem(ledger, evaluated, executed)
            if problem is not None:
                yield authority(
                    "freshness-health",
                    ledger,
                    "the provider ledger identity the bundle was judged against",
                    "$.provider_ledger",
                    f"a schema-4 freshness bundle carries an unusable provider ledger "
                    f"identity: {problem}",
                )
        if evaluated != cutoff:
            yield authority(
                "freshness-health",
                evaluated.isoformat(),
                cutoff.isoformat(),
                "$.as_of_date",
                "a schema-4 freshness bundle is not dated at the data cutoff",
            )

    panel_meta = node(ctx.panel, "meta")
    if isinstance(panel_meta, Mapping) and "freshness_evaluated_on" in panel_meta:
        panel_clock_raw = panel_meta.get("freshness_evaluated_on")
        panel_clock = canonical_day(panel_clock_raw)
        if panel_clock is None or panel_clock != evaluated:
            yield authority(
                "panel-freshness-clock",
                {"freshness_evaluated_on": panel_clock_raw},
                {"freshness_evaluated_on": evaluated.isoformat()},
                "$.meta.freshness_evaluated_on",
                "panel and freshness health operational clocks differ or are invalid",
            )

    raw_policies = node(ctx.gen.freshness, "policies")
    policy_map: dict[str, dict[str, Any]] = {}
    duplicate_policies: set[str] = set()
    policy_rows = raw_policies if isinstance(raw_policies, list) else []
    if not isinstance(raw_policies, list):
        yield authority(
            "freshness-health",
            raw_policies,
            "policy list",
            "$.policies",
            "freshness policy metadata is missing or malformed",
        )
    for position, policy in enumerate(policy_rows):
        path = f"$.policies[{position}]"
        if not isinstance(policy, Mapping):
            yield authority(
                "freshness-health",
                policy,
                "policy object",
                path,
                "freshness policy metadata is malformed",
            )
            continue
        policy_id = policy.get("policy_id")
        if not isinstance(policy_id, str) or not policy_id:
            yield authority(
                "freshness-health",
                {"policy_id": policy_id},
                "non-empty policy_id",
                f"{path}.policy_id",
                "freshness policy identity is missing or invalid",
            )
            continue
        if policy_id in policy_map:
            duplicate_policies.add(policy_id)
            yield authority(
                f"policy:{policy_id}",
                {"policy_id": policy_id},
                "unique policy_id",
                path,
                "freshness policy identity is duplicated",
            )
            continue
        policy_map[policy_id] = dict(policy)

        mode = policy.get("mode")
        calendar = policy.get("calendar")
        grace = policy.get("grace_days")
        interval = policy.get("expected_interval_days")
        stale_after = policy.get("stale_after_days")
        valid = (
            isinstance(mode, str)
            and mode in {"FINITE", "RECURRING"}
            and isinstance(calendar, str)
            and calendar in {"CALENDAR_DAY", "WEEKDAY_SESSION"}
            and type(grace) is int
            and grace >= 0
        )
        if mode == "RECURRING":
            valid = valid and (
                type(interval) is int
                and interval > 0
                and type(stale_after) is int
                and stale_after >= interval + cast(int, grace)
            )
        elif mode == "FINITE":
            valid = valid and interval is None and stale_after is None
        if not valid:
            yield authority(
                f"policy:{policy_id}",
                dict(policy),
                "valid policy mode, calendar, and numeric thresholds",
                path,
                "freshness policy metadata is invalid or contradictory",
            )

    for policy_id in duplicate_policies:
        policy_map.pop(policy_id, None)

    raw_assessments = node(ctx.gen.freshness, "assessments")
    if not isinstance(raw_assessments, list):
        yield authority(
            "freshness-health",
            raw_assessments,
            "assessment list",
            "$.assessments",
            "freshness assessment metadata is missing or malformed",
        )
        return
    health_rows = [row if isinstance(row, Mapping) else None for row in raw_assessments]
    for position, row in enumerate(health_rows):
        if row is None:
            yield authority(
                "freshness-health",
                raw_assessments[position],
                "assessment object",
                f"$.assessments[{position}]",
                "freshness assessment metadata is malformed",
            )

    reference = universe(ctx)
    active_strategies = reference.active_strategies if reference else None
    validated_health: list[tuple[Mapping[str, Any], date | None]] = []
    for position, row in enumerate(health_rows):
        if row is None:
            continue
        path = f"$.assessments[{position}]"
        state = row.get("state")
        policy_id = row.get("policy_id")
        entity = (
            f"{row.get('provider_id')}:{row.get('subject_id')}:"
            f"{row.get('lane')}:{row.get('fact_type')}"
        )
        if not isinstance(state, str) or state not in {
            "FRESH",
            "LATE",
            "STALE",
            "UNAVAILABLE",
            "FINITE_COMPLETE",
        }:
            yield authority(
                entity,
                {"state": state},
                "declared freshness state",
                f"{path}.state",
                "freshness assessment state is missing or invalid",
            )
            continue
        row_evaluated_raw = row.get("evaluated_on")
        row_evaluated = canonical_day(row_evaluated_raw)
        if row_evaluated is None or row_evaluated != evaluated:
            yield authority(
                entity,
                {"evaluated_on": row_evaluated_raw},
                {"evaluated_on": evaluated.isoformat()},
                f"{path}.evaluated_on",
                "freshness row clock differs from its operational root",
            )

        policy = policy_map.get(policy_id) if isinstance(policy_id, str) else None
        if policy is None:
            yield authority(
                entity,
                {"policy_id": policy_id},
                "a unique declared freshness policy",
                f"{path}.policy_id",
                "freshness assessment references an unknown or ambiguous policy",
            )

        required = row.get("required_for_current")
        if type(required) is not bool:
            yield authority(
                entity,
                {"required_for_current": required},
                "boolean required_for_current",
                f"{path}.required_for_current",
                "freshness current-requirement metadata is invalid",
            )

        has_observation = "last_accepted_date" in row
        raw_last = row.get("last_accepted_date")
        last = canonical_day(raw_last)
        observation_valid = False
        if not has_observation:
            yield authority(
                entity,
                {"last_accepted_date": "missing"},
                "canonical date or explicit null with UNAVAILABLE state",
                f"{path}.last_accepted_date",
                "freshness observation date is missing",
            )
        elif raw_last is None:
            observation_valid = state == "UNAVAILABLE"
            if not observation_valid:
                yield authority(
                    entity,
                    {"last_accepted_date": None, "state": state},
                    "explicit null only with UNAVAILABLE state",
                    f"{path}.last_accepted_date",
                    "freshness observation date is null for a non-UNAVAILABLE state",
                )
        elif last is None:
            yield authority(
                entity,
                {"last_accepted_date": raw_last},
                "canonical ISO observation date",
                f"{path}.last_accepted_date",
                "freshness observation date is invalid",
            )
        else:
            observation_valid = True
            if last > evaluated:
                yield authority(
                    entity,
                    {"last_accepted_date": raw_last},
                    {"on_or_before_evaluated_on": evaluated.isoformat()},
                    f"{path}.last_accepted_date",
                    "freshness observation is later than its operational evaluation date",
                )

        if policy is None:
            continue

        subject_is_retired = (
            row.get("subject_type") == "STRATEGY"
            and active_strategies is not None
            and str(row.get("subject_id")) not in active_strategies
        )
        mode = policy.get("mode")
        if required is True and (subject_is_retired or mode == "FINITE"):
            yield _finding(
                "dates.freshness_vs_cutoff",
                BLOCKER_AUTHORITY,
                entity,
                {
                    "required_for_current": True,
                    "state": state,
                    "last_accepted_date": row.get("last_accepted_date"),
                    "subject_active": not subject_is_retired,
                },
                {"required_for_current": False},
                FRESHNESS,
                f"$.assessments[{position}].required_for_current",
                ("D7",),
                "a retired or finite, ended source is counted as required-current",
            )

        if not observation_valid:
            if mode == "FINITE" and state != "UNAVAILABLE":
                yield authority(
                    entity,
                    {"state": state, "last_accepted_date": raw_last},
                    "UNAVAILABLE with explicit null observation for finite policy",
                    path,
                    "finite freshness state contradicts its missing observation",
                )
            continue
        if mode == "FINITE":
            expected_finite_state = (
                "FINITE_COMPLETE" if last is not None else "UNAVAILABLE"
            )
            if state != expected_finite_state:
                yield authority(
                    entity,
                    {"state": state, "last_accepted_date": raw_last},
                    f"{expected_finite_state} with the matching finite observation state",
                    f"{path}.state",
                    "finite freshness state contradicts its observed history",
                )
            validated_health.append((row, last))
            continue

        if mode != "RECURRING":
            # The policy row already emitted an authority finding above.
            continue
        if last is None:
            # An explicit null plus UNAVAILABLE is an honest absence. The owning
            # producer carries its required/blocking semantics separately.
            validated_health.append((row, None))
            continue

        interval = policy["expected_interval_days"]
        grace = policy["grace_days"]
        stale_after = policy["stale_after_days"]
        age = _age(policy, last, evaluated)
        expected_state = (
            "STALE"
            if age > stale_after
            else "LATE"
            if age > interval + grace
            else "FRESH"
        )
        if state != expected_state:
            yield _finding(
                "dates.freshness_vs_cutoff",
                BLOCKER_AUTHORITY
                if state == "FRESH" and expected_state == "STALE"
                else BLOCKER_INVARIANT,
                entity,
                {"state": state, "last_accepted_date": row.get("last_accepted_date")},
                {"state": expected_state, "evaluated_on": evaluated},
                FRESHNESS,
                f"{path}.state",
                ("D7",),
                "freshness state differs from the declared operating policy and clock",
            )

        validated_health.append((row, last))

    # Mirror only the selected evidence identity needed to verify the report's
    # copied HWM freshness. This is independent of the report builder and never
    # substitutes PROVIDER evidence for an ACCOUNT_LIVE NAV path.
    state_rank = {
        "UNAVAILABLE": 5,
        "STALE": 4,
        "LATE": 3,
        "FRESH": 2,
        "FINITE_COMPLETE": 1,
    }
    for position, row in enumerate(rows(ctx.report, "strategy_summary")):
        # Some compact acceptance fixtures retain legacy HWM date labels but do
        # not claim an evaluated HWM result. Require the complete operational
        # status or a NAV endpoint before binding these copied labels.
        if (
            row.get("hwm_freshness_status") is None
            and row.get("nav_observed_end") is None
        ):
            continue
        fields = (
            "hwm_freshness_observed_end",
            "hwm_freshness_state",
            "hwm_freshness_status",
            "nav_evidence_layer",
        )
        if not any(row.get(field) is not None for field in fields):
            continue
        entity = f"strategy:{row.get('strategy_id')}"
        path = f"$.strategy_summary[{position}]"
        layer = row.get("nav_evidence_layer")
        eligible_lanes = (
            {"PROVIDER"}
            if layer == "PROVIDER_LIVE"
            else {"ACCOUNT", "LEGACY"}
            if layer == "ACCOUNT_LIVE"
            else None
        )
        if eligible_lanes is None:
            yield authority(
                entity,
                {"nav_evidence_layer": layer},
                "ACCOUNT_LIVE or PROVIDER_LIVE evidence layer",
                f"{path}.nav_evidence_layer",
                "HWM freshness is not bound to a recognized NAV evidence lane",
            )
            continue
        candidates = [
            (assessment, last)
            for assessment, last in validated_health
            if assessment.get("subject_id") == row.get("strategy_id")
            and assessment.get("subject_type") == "STRATEGY"
            and assessment.get("lane") in eligible_lanes
        ]
        if not candidates:
            if (
                row.get("hwm_freshness_status") == "UNAVAILABLE"
                and row.get("hwm_freshness_state") is None
                and row.get("hwm_freshness_observed_end") is None
                and row.get("nav_observed_end") is None
            ):
                continue
            yield authority(
                entity,
                {
                    "nav_evidence_layer": layer,
                    "hwm_freshness_state": row.get("hwm_freshness_state"),
                    "hwm_freshness_observed_end": row.get("hwm_freshness_observed_end"),
                },
                "one matching health assessment in the selected evidence lane",
                f"{path}.hwm_freshness_state",
                "HWM freshness has no matching health evidence in its selected lane",
            )
            continue
        selected_key = max(
            (
                state_rank.get(str(assessment.get("state")), 6),
                str(assessment.get("lane")),
            )
            for assessment, _last in candidates
        )
        selected = [
            (assessment, last)
            for assessment, last in candidates
            if (
                state_rank.get(str(assessment.get("state")), 6),
                str(assessment.get("lane")),
            )
            == selected_key
        ]
        selected_values = {
            (assessment.get("state"), last) for assessment, last in selected
        }
        if len(selected_values) != 1:
            yield authority(
                entity,
                [
                    {
                        "lane": assessment.get("lane"),
                        "state": assessment.get("state"),
                        "last_accepted_date": last.isoformat() if last else None,
                    }
                    for assessment, last in selected
                ],
                "one unambiguous selected-lane freshness state and date",
                f"{path}.hwm_freshness_state",
                "HWM freshness evidence is ambiguous in the selected lane",
            )
            continue
        selected_state, selected_day = next(iter(selected_values))
        raw_hwm_day = row.get("hwm_freshness_observed_end")
        hwm_day = canonical_day(raw_hwm_day) if raw_hwm_day is not None else None
        raw_nav_day = row.get("nav_observed_end")
        nav_day = canonical_day(raw_nav_day) if raw_nav_day is not None else None
        if (raw_hwm_day is not None and hwm_day is None) or (
            raw_nav_day is not None and nav_day is None
        ):
            yield authority(
                entity,
                {
                    "hwm_freshness_observed_end": raw_hwm_day,
                    "nav_observed_end": raw_nav_day,
                },
                "canonical HWM and NAV observation dates",
                f"{path}.hwm_freshness_observed_end",
                "HWM evidence observation date is invalid",
            )
            continue
        truncated_at_boundary = (
            nav_day is not None
            and selected_day is not None
            and nav_day == cutoff
            and selected_day > nav_day
        )
        boundary_mismatch = (
            nav_day is not None
            and selected_day is not None
            and nav_day != selected_day
            and not truncated_at_boundary
        )
        expected_boundary_match = (
            None if nav_day is None or selected_day is None else not boundary_mismatch
        )
        blockers = any(
            assessment.get("blocks_current_consumers") is True
            or assessment.get("state") in {"STALE", "UNAVAILABLE"}
            for assessment, _last in candidates
        )
        expected_status = (
            "BOUNDARY_MISMATCH"
            if boundary_mismatch
            else "BLOCKED"
            if blockers
            else "ACCEPTED"
        )
        mismatch = (
            row.get("hwm_freshness_state") != selected_state
            or hwm_day != selected_day
            or row.get("hwm_freshness_status") != expected_status
            or row.get("hwm_freshness_boundary_matches_evidence")
            != expected_boundary_match
        )
        if mismatch:
            yield authority(
                entity,
                {
                    "hwm_freshness_state": row.get("hwm_freshness_state"),
                    "hwm_freshness_observed_end": raw_hwm_day,
                    "hwm_freshness_status": row.get("hwm_freshness_status"),
                    "nav_evidence_layer": layer,
                },
                {
                    "hwm_freshness_state": selected_state,
                    "hwm_freshness_observed_end": (
                        selected_day.isoformat() if selected_day else None
                    ),
                    "hwm_freshness_status": expected_status,
                    "hwm_freshness_boundary_matches_evidence": expected_boundary_match,
                },
                f"{path}.hwm_freshness_state",
                "strategy HWM freshness does not match its selected health lane, date, and state",
            )
    for position, row in enumerate(rows(ctx.report, "strategy_summary")):
        end = to_date(row.get("hwm_freshness_observed_end"))
        state = row.get("hwm_freshness_state")
        if end is not None and end >= cutoff and state in ("STALE", "LATE"):
            matching_account = any(
                assessment.get("subject_id") == row.get("strategy_id")
                and assessment.get("lane") == "ACCOUNT"
                and assessment.get("state") == state
                and to_date(assessment.get("last_accepted_date")) == end
                for assessment in rows(ctx.gen.freshness, "assessments")
            )
            if matching_account:
                continue
            yield _finding(
                "dates.freshness_vs_cutoff",
                BLOCKER_INVARIANT,
                f"strategy:{row.get('strategy_id')}",
                {
                    "hwm_freshness_state": state,
                    "hwm_freshness_observed_end": row.get("hwm_freshness_observed_end"),
                },
                {"hwm_freshness_state": state, "lane": "ACCOUNT"},
                REPORT,
                f"$.strategy_summary[{position}].hwm_freshness_state",
                ("B12",),
                "strategy HWM freshness does not match its ACCOUNT health evidence",
            )


def _dated_sections(
    ctx: Context,
) -> Iterator[tuple[str, str, str, Any, Any, tuple[str, ...], tuple[str, Any] | None]]:
    """(entity, file, path, date, status, defects, optional date role)."""

    comparisons = node(ctx.report, "strategy_attribution", "benchmark_comparisons")
    attribution = node(ctx.report, "strategy_attribution")
    if (
        isinstance(attribution, Mapping)
        and "benchmark_comparisons" in attribution
        and not isinstance(comparisons, list)
    ):
        yield (
            "benchmark:comparisons-container",
            REPORT,
            "$.strategy_attribution.benchmark_comparisons",
            None,
            None,
            ("C2",),
            ("c2_malformed", comparisons),
        )
    for position, row in enumerate(
        comparisons if isinstance(comparisons, list) else []
    ):
        if not isinstance(row, dict):
            yield (
                f"benchmark:malformed:{position}",
                REPORT,
                f"$.strategy_attribution.benchmark_comparisons[{position}]",
                None,
                None,
                ("C2",),
                ("c2_malformed", row),
            )
            continue
        yield (
            f"benchmark:{row.get('strategy_id')}",
            REPORT,
            f"$.strategy_attribution.benchmark_comparisons[{position}].observed_end",
            row.get("observed_end"),
            row.get("status"),
            ("C2",),
            ("c2_descriptive", row),
        )
    # The alias is only one published view. Check every mode, retaining a
    # divergent alias but avoiding duplicate findings for the same date claim.
    lens_views: list[tuple[str, str, list[Any]]] = []
    modes = node(ctx.report, "decomposition_lenses", "modes")
    if isinstance(modes, Mapping):
        for mode, mode_rows in modes.items():
            if isinstance(mode_rows, list):
                lens_views.append(
                    (str(mode), f"$.decomposition_lenses.modes.{mode}", mode_rows)
                )
    lenses = node(ctx.report, "decomposition_lenses", "lenses")
    if isinstance(lenses, list):
        lens_views.append(("", "$.decomposition_lenses.lenses", lenses))
    seen_dates: set[tuple[str, str, str]] = set()
    for mode, prefix, view_rows in lens_views:
        for position, row in enumerate(view_rows):
            if not isinstance(row, dict):
                continue
            entity = (
                f"decomposition:{row.get('lens_id')}:{mode or row.get('evidence_mode')}"
            )
            status = (
                "AVAILABLE" if (row.get("available_count") or 0) > 0 else "UNAVAILABLE"
            )
            key = (entity, str(row.get("as_of_date")), status)
            if key in seen_dates:
                continue
            seen_dates.add(key)
            yield (
                entity,
                REPORT,
                f"{prefix}[{position}].as_of_date",
                row.get("as_of_date"),
                status,
                ("C3",),
                None,
            )
    factor = node(ctx.panel, "factor_analysis")
    if isinstance(factor, Mapping) and factor.get("as_of_date") is not None:
        yield (
            "factor_analysis",
            PANEL,
            "$.factor_analysis.as_of_date",
            factor.get("as_of_date"),
            "AVAILABLE" if (factor.get("available_count") or 0) > 0 else "UNAVAILABLE",
            ("C3",),
            None,
        )
    for metadata_key in (
        "implementation_reconciliation_contract",
        "implementation_return_attribution_contract",
    ):
        if metadata_key in ctx.report:
            metadata = ctx.report[metadata_key]
            value = (
                metadata.get("as_of_date") if isinstance(metadata, Mapping) else None
            )
            yield (
                metadata_key,
                REPORT,
                f"$.{metadata_key}.as_of_date",
                value,
                "AVAILABLE",
                ("C4",),
                ("c4_historical", (metadata_key, metadata)),
            )
    # ``strategy_summary.live_boundary_observed_end`` is deliberately not a section
    # as-of: it is a pinned version-identity window (the targets rows hashed into the
    # version reference, tools/portfolio_provenance.py), and extending it to the cutoff
    # would re-pin the identity (D4 ruling 1 of the S2.1 decision seat).


def _canonical_iso_day(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if value == parsed.isoformat() else None


def _date_role_is_valid(role: tuple[str, Any], status: Any, value: Any) -> bool:
    kind, detail = role
    if kind == "c2_malformed":
        return False
    if kind == "c2_descriptive":
        row = detail
        if status == "UNAVAILABLE":
            return (
                isinstance(row.get("strategy_id"), str)
                and bool(row["strategy_id"].strip())
                and all(
                    key in row and row[key] is None
                    for key in (
                        "observed_start",
                        "observed_end",
                        "account_series_id",
                        "reference_series_id",
                    )
                )
            )
        start = _canonical_iso_day(row.get("observed_start"))
        end = _canonical_iso_day(row.get("observed_end"))
        return (
            status == "DESCRIPTIVE_EXACT_DATE_VERSION_COMPATIBLE"
            and row.get("version_compatibility_status") == "PROVEN_COMPATIBLE"
            and all(
                isinstance(row.get(key), str) and bool(row[key].strip())
                for key in ("strategy_id", "account_series_id", "reference_series_id")
            )
            and start is not None
            and end is not None
            and start <= end
            and value == row.get("observed_end")
        )
    if kind == "c4_historical":
        key, metadata = detail
        identity = (
            "reconciliation_id"
            if key == "implementation_reconciliation_contract"
            else "attribution_id"
        )
        return (
            isinstance(metadata, Mapping)
            and _canonical_iso_day(value) is not None
            and value == metadata.get("as_of_date")
            and isinstance(metadata.get("schema_version"), int)
            and not isinstance(metadata.get("schema_version"), bool)
            and metadata.get("schema_version") == 1
            and isinstance(metadata.get(identity), str)
            and bool(metadata[identity].strip())
            and isinstance(metadata.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"]) is not None
            and isinstance(metadata.get("size_bytes"), int)
            and not isinstance(metadata.get("size_bytes"), bool)
            and metadata["size_bytes"] > 0
            and isinstance(metadata.get("strategy_count"), int)
            and not isinstance(metadata.get("strategy_count"), bool)
            and metadata["strategy_count"] == 3
        )
    return False


# Provider publication lag (V10). A public factor provider that publishes monthly with a
# fixed lag has no observation past its last eligible month, so a lens built only on its
# sources ends there at any later cutoff. The lag is the policy's, the sources are the
# governed decomposition plan's: the report's own lens row is never taken at its word.
_LAG_POLICY_ID = re.compile(r"_(\d+)D_V\d+$")


def _plan_sources_by_lens(
    ctx: Context,
) -> dict[str, dict[str, int | None]]:
    """Per lens, the sources the governed plan assigns it and each one's publication lag.

    The lag is read from the plan's freshness policy id (``..._45D_V1`` states 45 days);
    a source with no policy or an id that states no lag has ``None`` (not lagged).
    ACCEPTED RESIDUAL: the lag is the id's suffix, not a policy document, because the
    repository carries the policy only as that identifier.
    """

    plan = decomposition_plan(ctx)
    sources = plan.get("sources") if plan is not None else None
    found: dict[str, dict[str, int | None]] = {}
    for key, config in sources.items() if isinstance(sources, Mapping) else []:
        if not isinstance(config, Mapping) or not isinstance(config.get("lens"), str):
            continue
        policy = config.get("freshness_policy_id")
        matched = _LAG_POLICY_ID.search(policy) if isinstance(policy, str) else None
        found.setdefault(str(config["lens"]), {})[str(key)] = (
            int(matched.group(1)) if matched else None
        )
    return found


def _last_eligible_month_end(cutoff: date, lag_days: int) -> date:
    month_end = cutoff.replace(day=1) - timedelta(days=1)
    while month_end + timedelta(days=lag_days) > cutoff:
        month_end = month_end.replace(day=1) - timedelta(days=1)
    return month_end


def provider_lag_bound(ctx: Context, entity: str, day: date) -> str | None:
    """Why a lens may end before the cutoff because its providers publish late.

    ``entity`` is ``decomposition:{lens_id}:{mode}``. The lens's expected sources are the
    governed plan's assignments for that lens; the row's own ``sources`` must equal them,
    every one of them must carry a publication lag, and the lens must end in the last
    month publishable at the cutoff under the longest lag (its final observation can fall
    up to four days before that month end). ``None`` leaves the finding unchanged.
    """

    lens_id, _, mode = entity.removeprefix("decomposition:").partition(":")
    expected = _plan_sources_by_lens(ctx).get(lens_id)
    if not expected or any(lag is None for lag in expected.values()):
        return None
    modes = node(ctx.report, "decomposition_lenses", "modes")
    candidates = list(rows(node(ctx.report, "decomposition_lenses"), "lenses"))
    if isinstance(modes, Mapping):
        for mode_name, mode_rows in modes.items():
            if mode in ("", str(mode_name)) and isinstance(mode_rows, list):
                candidates += [row for row in mode_rows if isinstance(row, dict)]
    matching = [
        row
        for row in candidates
        if row.get("lens_id") == lens_id and to_date(row.get("as_of_date")) == day
    ]
    if not matching:
        return None
    for row in matching:
        named = {
            str(source.get("source_key"))
            for source in rows(row, "sources")
            if source.get("source_key") is not None
        }
        if named != set(expected):
            return None
    lag = max(lag for lag in expected.values() if lag is not None)
    eligible = _last_eligible_month_end(ctx.cutoff, lag)
    if (day.year, day.month) != (eligible.year, eligible.month) or (
        eligible - day
    ).days > 4:
        return None
    return (
        f"lens ends {day} because its governed sources ({', '.join(sorted(expected))}) "
        f"publish monthly with a {lag} day publication lag: the last eligible month at "
        f"the cutoff is {eligible:%Y-%m} (governed decomposition plan freshness policy)"
    )


def check_section_dates(ctx: Context) -> Iterator[Finding]:
    """C2-C5: a section's as-of must be the cutoff, or be labelled as not current."""

    cutoff = ctx.cutoff
    for entity, file, path, value, status, defects, role in _dated_sections(ctx):
        if role is not None and not _date_role_is_valid(role, status, value):
            yield _finding(
                "dates.section_as_of_vs_cutoff",
                BLOCKER_AUTHORITY,
                entity,
                {"as_of": value, "status": status},
                cutoff,
                file,
                path,
                defects,
                "section has malformed or unrecognized date-role metadata",
            )
            continue
        if (
            role is not None
            and role[0] == "c2_descriptive"
            and status == "UNAVAILABLE"
            and value is None
        ):
            continue
        day = _canonical_iso_day(value) if role is not None else to_date(value)
        if role is not None and day is None:
            yield _finding(
                "dates.section_as_of_vs_cutoff",
                BLOCKER_AUTHORITY,
                entity,
                {"as_of": value, "status": status},
                cutoff,
                file,
                path,
                defects,
                "section does not declare a canonical ISO date",
            )
            continue
        if (
            entity.startswith("decomposition:")
            and is_available(status)
            and (
                not isinstance(value, str)
                or len(value) != 10
                or day is None
                or value != day.isoformat()
            )
        ):
            yield _finding(
                "dates.section_as_of_vs_cutoff",
                BLOCKER_AUTHORITY,
                entity,
                {"as_of": value, "status": status},
                cutoff,
                file,
                path,
                defects,
                "available decomposition section does not declare a canonical ISO date",
            )
            continue
        if day is None or day == cutoff:
            continue
        after = day > cutoff
        if role is not None and not after:
            if (
                role[0] == "c2_descriptive"
                and isinstance(role[1], Mapping)
                and role[1].get("account_series_source") == RETAINED_ACCOUNT_SOURCE
                and strategy_confirmed_retained(ctx, str(role[1].get("strategy_id")))
            ):
                # A retained strategy's reference-only account history ends where the
                # retained history ends, by design (ruling S1 of N8); the crosswalk
                # lifecycle confirms the strategy is retained.
                continue
            # A role that passed the validation above and ends before the cutoff keeps
            # its true end date and is NOT_READY (descriptive benchmark windows, the
            # recipe-pinned historical contracts); an invalid role, a missing label or
            # a date after the cutoff never reaches this branch as NOT_READY.
            yield _finding(
                "dates.section_as_of_vs_cutoff",
                NOT_READY,
                entity,
                {"as_of": value, "status": status},
                cutoff,
                file,
                path,
                defects,
                "labelled section ends before the declared cutoff (its true end date "
                "is published, not presented as current)",
            )
            continue
        bound = (
            verified_value_basis_bound(
                ctx, "decomposition_lenses", entity.removeprefix("decomposition:"), day
            )
            if not after
            and is_available(status)
            and entity.startswith("decomposition:")
            else None
        )
        lag = (
            provider_lag_bound(ctx, entity, day)
            if bound is None
            and not after
            and is_available(status)
            and entity.startswith("decomposition:")
            else None
        )
        if bound is not None:
            klass = NOT_READY
            message = _bound_message(bound, "section")
        elif lag is not None:
            klass = NOT_READY
            message = lag
        elif is_available(status) or after:
            klass = BLOCKER_AUTHORITY
            message = (
                "section is dated after the declared cutoff"
                if after
                else "section is presented as current but ends before the cutoff"
            )
        else:
            klass = NOT_READY
            message = "section ends before the cutoff and is labelled unavailable"
        yield _finding(
            "dates.section_as_of_vs_cutoff",
            klass,
            entity,
            {"as_of": value, "status": status},
            cutoff,
            file,
            path,
            defects,
            message,
        )
    for position, row in enumerate(
        rows(node(ctx.report, "source_clock"), "source_data_as_of")
    ):
        day = to_date(row.get("provider_data_date"))
        latest = to_date(node(ctx.report, "source_clock", "latest_common_eod"))
        if day is None:
            continue
        if row.get("applies_to_latest_common_eod") is True and latest and day < latest:
            yield _finding(
                "dates.source_clock",
                BLOCKER_AUTHORITY,
                str(row.get("source_id")),
                {
                    "provider_data_date": row.get("provider_data_date"),
                    "applies_to_latest_common_eod": True,
                },
                {"provider_data_date_at_least": latest},
                REPORT,
                f"$.source_clock.source_data_as_of[{position}]",
                ("D7",),
                "provider counted as current at the common EOD although its data ends earlier",
            )
        elif day < cutoff:
            yield _finding(
                "dates.source_clock",
                NOT_READY,
                str(row.get("source_id")),
                row.get("provider_data_date"),
                cutoff,
                REPORT,
                f"$.source_clock.source_data_as_of[{position}].provider_data_date",
                ("C5",),
                "provider clock lags the cutoff (labelled not applicable to the common EOD)",
            )
    policies = _policies(ctx)
    for position, row in enumerate(rows(ctx.gen.freshness, "assessments")):
        last = to_date(row.get("last_accepted_date"))
        if row.get("state") == "STALE" and last is not None and last < cutoff:
            policy = policies.get(str(row.get("policy_id")), {})
            if policy.get("mode") == "FINITE":
                continue
            yield _finding(
                "dates.source_clock",
                NOT_READY,
                f"{row.get('provider_id')}:{row.get('subject_id')}:{row.get('lane')}:{row.get('fact_type')}",
                row.get("last_accepted_date"),
                cutoff,
                FRESHNESS,
                f"$.assessments[{position}].last_accepted_date",
                ("C5",),
                "recurring source is stale at the cutoff (labelled STALE)",
            )


def check_weekly_current_week(ctx: Context) -> Iterator[Finding]:
    """C6: the current week is source-bound, partial as applicable, and visible."""

    weekly = node(ctx.panel, "weekly_returns")
    source_artifact_id = "input/portfolio-weekly-returns.json"
    artifacts = [
        artifact
        for artifact in rows(ctx.gen.manifest, "artifacts")
        if artifact.get("artifact_id") == source_artifact_id
    ]
    has_source_identity = any(
        weekly.get(name) is not None for name in ("generation_id", "source_sha256")
    )
    if weekly.get("status") == "UNAVAILABLE_NOT_CONFIGURED":
        current = node(weekly, "current_week")
        has_current_week_data = isinstance(current, Mapping) and any(
            current.get(name) is not None
            for name in ("week_start", "week_end", "data_through_date")
        )
        payload_rows = {
            name: len(weekly[name])
            for name in ("weeks", "series")
            if isinstance(weekly.get(name), list) and weekly[name]
        }
        if artifacts or has_source_identity or has_current_week_data or payload_rows:
            yield _finding(
                "dates.weekly_current_week",
                BLOCKER_AUTHORITY,
                "weekly_returns.status",
                {
                    "source_artifact_count": len(artifacts),
                    "current_week_data_present": has_current_week_data,
                    "source_identity_present": has_source_identity,
                    "payload_rows": payload_rows,
                },
                "UNAVAILABLE only when no source artifact or source identity is bound "
                "and the weekly payload is empty",
                PANEL,
                "$.weekly_returns.status",
                ("C6",),
                "configured weekly-source evidence or an unbound weekly payload "
                "cannot be labelled unavailable",
            )
        return

    if len(artifacts) != 1:
        yield _finding(
            "dates.weekly_current_week",
            BLOCKER_AUTHORITY,
            "weekly_returns.source",
            len(artifacts),
            "exactly one manifest-bound weekly source artifact",
            GENERATION_MANIFEST,
            "$.artifacts",
            ("C6",),
            "available weekly returns require one bound source snapshot",
        )
        return

    artifact = artifacts[0]
    manifest_as_of = to_date(artifact.get("as_of_date"))
    source_hash = artifact.get("sha256")
    # A manifest entry nests its schema identity as ``schema {name, version}``; the
    # version is a string in the sealed manifest (generation.json) and 2 from 8302d4e.
    schema = artifact.get("schema")
    schema_name = schema.get("name") if isinstance(schema, Mapping) else None
    schema_version = schema.get("version") if isinstance(schema, Mapping) else None
    artifact_valid = (
        schema_name == "portfolio-weekly-returns"
        and not isinstance(schema_version, bool)
        and str(schema_version) == "2"
        and isinstance(source_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", source_hash) is not None
        and isinstance(artifact.get("size_bytes"), int)
        and not isinstance(artifact.get("size_bytes"), bool)
        and artifact.get("size_bytes", 0) > 0
    )
    if not artifact_valid or manifest_as_of is None:
        yield _finding(
            "dates.weekly_current_week",
            BLOCKER_AUTHORITY,
            "weekly_returns.source",
            {
                "schema": schema,
                "sha256_valid": isinstance(source_hash, str)
                and re.fullmatch(r"[0-9a-f]{64}", source_hash) is not None,
                "size_bytes": artifact.get("size_bytes"),
            },
            "valid portfolio-weekly-returns/2 manifest identity and source cutoff",
            GENERATION_MANIFEST,
            "$.artifacts",
            ("C6",),
            "weekly source artifact metadata is incomplete or invalid (its nested "
            "schema must be portfolio-weekly-returns version 2)",
        )
        return
    # The source is judged against the DECLARED cutoff, not against its own manifest
    # date: the manifest ``as_of_date`` is an execution date and may lie after the
    # cutoff (N54), and a source that trails the cutoff by more than the last completed
    # week is stale. A week closes on its Friday: for a Friday, Saturday or Sunday
    # cutoff the cutoff's own week is complete and its Friday is the floor, otherwise the
    # last completed week is the previous one (its Friday).
    latest_allowed = ctx.cutoff
    this_friday = iso_week_start(ctx.cutoff) + timedelta(days=4)
    earliest_allowed = (
        this_friday if ctx.cutoff.weekday() >= 4 else this_friday - timedelta(days=7)
    )
    if manifest_as_of > latest_allowed or manifest_as_of < earliest_allowed:
        after = manifest_as_of > latest_allowed
        yield _finding(
            "dates.weekly_current_week",
            BLOCKER_AUTHORITY,
            "weekly_returns.source_cutoff",
            manifest_as_of.isoformat(),
            {"source_as_of_not_after": latest_allowed.isoformat()}
            if after
            else {"source_as_of_at_least": earliest_allowed.isoformat()},
            GENERATION_MANIFEST,
            "$.artifacts[input/portfolio-weekly-returns.json].as_of_date",
            ("C6",),
            "weekly source is dated after the declared cutoff (an execution date, not "
            "the data cutoff)"
            if after
            else "weekly source ends before the last completed week at the declared "
            "cutoff (stale)",
        )
        return
    if weekly.get("source_sha256") != source_hash:
        yield _finding(
            "dates.weekly_current_week",
            BLOCKER_AUTHORITY,
            "weekly_returns.source_sha256",
            weekly.get("source_sha256"),
            source_hash,
            PANEL,
            "$.weekly_returns.source_sha256",
            ("C6",),
            "panel weekly source hash differs from the manifest-bound snapshot",
        )
        return
    source_cutoff = manifest_as_of.isoformat()
    if weekly.get("as_of_date") != source_cutoff:
        yield _finding(
            "dates.weekly_current_week",
            BLOCKER_AUTHORITY,
            "weekly_returns.as_of_date",
            weekly.get("as_of_date"),
            source_cutoff,
            PANEL,
            "$.weekly_returns.as_of_date",
            ("C6",),
            "panel weekly cutoff differs from its manifest-bound source",
        )
        return

    current = node(weekly, "current_week")
    if not isinstance(current, Mapping):
        yield _finding(
            "dates.weekly_current_week",
            BLOCKER_AUTHORITY,
            "weekly_returns.current_week",
            current,
            "source-bound current-week object",
            PANEL,
            "$.weekly_returns.current_week",
            ("C6",),
            "available weekly source requires a current-week record",
        )
        return

    source_week_start = iso_week_start(manifest_as_of)
    week_start = to_date(current.get("week_start"))
    if week_start != source_week_start:
        yield _finding(
            "dates.weekly_current_week",
            BLOCKER_AUTHORITY,
            "weekly_returns.current_week",
            current.get("week_start"),
            source_week_start.isoformat(),
            PANEL,
            "$.weekly_returns.current_week.week_start",
            ("C6",),
            "current week must be anchored to the verified weekly-source cutoff",
        )
    expected_week_end = source_week_start + timedelta(days=6)
    expected_partial = manifest_as_of < expected_week_end
    if (
        current.get("week_end") != expected_week_end.isoformat()
        or current.get("data_through_date") != source_cutoff
        or current.get("is_partial") is not expected_partial
        or current.get("default_visible") is not True
    ):
        yield _finding(
            "dates.weekly_current_week",
            BLOCKER_AUTHORITY,
            "weekly_returns.current_week",
            {
                "is_partial": current.get("is_partial"),
                "default_visible": current.get("default_visible"),
                "week_end": current.get("week_end"),
                "data_through_date": current.get("data_through_date"),
            },
            {
                "is_partial": expected_partial,
                "default_visible": True,
                "week_end": expected_week_end.isoformat(),
                "data_through_date": source_cutoff,
            },
            PANEL,
            "$.weekly_returns.current_week",
            ("C6",),
            "the current week must match the verified source cutoff and visibility (#417)",
        )


# ------------------------------------------------------------- completeness (E*)


_DECOMPOSITION_PLAN = "docs/analytics/current-strategy-decomposition-plan.json"
_NOT_APPLICABLE = "NOT_APPLICABLE_METHOD_SCOPE"
# The two out-of-scope requirement values the governed plan carries (a test pins the plan's
# set to exactly these; a new kind in the plan is not honoured until it is added here).
_OUT_OF_SCOPE = frozenset({_NOT_APPLICABLE, "NOT_APPLICABLE_OWNER_APPROVED"})


def decomposition_plan(ctx: Context) -> Mapping[str, Any] | None:
    """The governed decomposition plan the repository carries, or ``None`` if unreadable.

    It is the owner-accepted authority for which strategy needs which source and which
    lens a source belongs to (ISSUE_328 routing); no report field stands in for it.
    """

    if "decomposition_plan" in ctx._cache:
        cached = ctx._cache["decomposition_plan"]
        return cached if isinstance(cached, Mapping) else None
    root = (
        ctx.repo_root
        if ctx.repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    plan: Any
    try:
        plan = json.loads((root / _DECOMPOSITION_PLAN).read_bytes())
    except (OSError, ValueError):
        plan = None
    ctx._cache["decomposition_plan"] = plan if isinstance(plan, dict) else None
    return plan if isinstance(plan, dict) else None


def _plan_dispositions(ctx: Context) -> dict[tuple[str, str], Mapping[str, Any]]:
    """The plan's per (series, source) disposition: requirement, readiness, reason."""

    plan = decomposition_plan(ctx)
    found: dict[tuple[str, str], Mapping[str, Any]] = {}
    for strategy in rows(plan, "strategies") if plan is not None else []:
        dispositions = strategy.get("source_dispositions")
        if not isinstance(dispositions, Mapping):
            continue
        for source_key, disposition in dispositions.items():
            if isinstance(disposition, Mapping):
                found[(str(strategy.get("series_id")), str(source_key))] = disposition
    return found


def _cell_is_explained(
    cell: Mapping[str, Any],
    dispositions: Mapping[tuple[str, str], Mapping[str, Any]],
) -> bool:
    """Whether the governed plan confirms why a non-available cell is not a gap.

    A cell labelled with an out-of-scope kind (``NOT_APPLICABLE_METHOD_SCOPE`` or
    ``NOT_APPLICABLE_OWNER_APPROVED``) is excused only when the plan carries that
    (series, source) pair with that same requirement; a ``DIAGNOSTIC_ONLY`` cell (the model-quality
    gate) only when the plan requires and readies that pair, so a REQUIRED cell can never
    exempt itself with its own ``selection_status``. ACCEPTED RESIDUAL: the plan confirms
    the pair, not that the producer labelled this cell truthfully, so the cell's own label
    still gates the excuse.
    """

    disposition = dispositions.get(
        (str(cell.get("series_id")), str(cell.get("source_key")))
    )
    if disposition is None:
        return False
    if cell.get("status") == "DIAGNOSTIC_ONLY":
        return (
            disposition.get("requirement") == "REQUIRED"
            and disposition.get("readiness") == "READY_TO_MODEL"
        )
    requirement = disposition.get("requirement")
    return requirement in _OUT_OF_SCOPE and cell.get("selection_status") == requirement


def _incomplete_series_count(
    lens: Mapping[str, Any],
    dispositions: Mapping[tuple[str, str], Mapping[str, Any]],
) -> int:
    """The cells of a lens that are unavailable and not explained by the governed plan.

    Recomputed from the series rows; the producer's ``unavailable_count`` is only
    reconciled (a count larger than the unavailable rows shows is unexplained and counts
    as incomplete). A lens without series rows is measured by its own counter.
    """

    declared = int(lens.get("unavailable_count") or 0)
    series = lens.get("series")
    if not isinstance(series, list):
        return declared
    unavailable = [
        row
        for row in series
        if isinstance(row, Mapping) and not is_available(row.get("status"))
    ]
    gaps = sum(1 for row in unavailable if not _cell_is_explained(row, dispositions))
    return gaps + max(0, declared - len(unavailable))


def check_completeness(ctx: Context) -> Iterator[Finding]:
    """E1-E8: required outputs that are unavailable, as NOT_READY register entries."""

    latest = [
        row
        for row in rows(ctx.report, "positions")
        if str(row.get("snapshot_status", "")).startswith("LATEST")
    ]
    missing_symbol = [row for row in latest if not row.get("symbol")]
    if missing_symbol:
        yield _finding(
            "completeness.required_outputs",
            NOT_READY,
            "positions.symbol",
            {"latest_positions": len(latest), "without_symbol": len(missing_symbol)},
            "every current holding identified by ticker",
            REPORT,
            "$.positions[*].symbol",
            ("E1",),
            "current holdings lack tickers",
        )
    status = node(ctx.report, "hwm_summary", "hwm_numerical_status")
    if status is not None and status != "AVAILABLE":
        yield _finding(
            "completeness.required_outputs",
            NOT_READY,
            "hwm_summary",
            {
                "hwm_numerical_status": status,
                "reason": node(ctx.report, "hwm_summary", "hwm_numerical_reason"),
            },
            "AVAILABLE",
            REPORT,
            "$.hwm_summary.hwm_numerical_status",
            ("E2",),
            "numeric HWM unavailable",
        )
    fx = node(ctx.panel, "fact_status", "fx")
    if fx is not None and not is_available(fx):
        yield _finding(
            "completeness.required_outputs",
            NOT_READY,
            "fact_status.fx",
            fx,
            "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            PANEL,
            "$.fact_status.fx",
            ("E3",),
            "FX evidence gate not passed",
        )
    modes = node(ctx.report, "decomposition_lenses", "modes")
    dispositions = _plan_dispositions(ctx)
    if isinstance(modes, Mapping):
        incomplete = {}
        for mode, lenses in sorted(modes.items()):
            for lens in lenses if isinstance(lenses, list) else []:
                if not isinstance(lens, dict):
                    continue
                count = _incomplete_series_count(lens, dispositions)
                if count > 0:
                    incomplete[f"{mode}:{lens.get('lens_id')}"] = count
        if incomplete:
            yield _finding(
                "completeness.required_outputs",
                NOT_READY,
                "decomposition_lenses",
                {"unavailable_series_by_lens": incomplete},
                "every lens complete",
                REPORT,
                "$.decomposition_lenses.modes",
                ("E4",),
                "decomposition incomplete",
            )
    summary = rows(ctx.report, "strategy_summary")
    for field_name, defect, label in (
        ("benchmark_evidence_status", "E5", "benchmark evidence"),
        ("cost_evidence_status", "E6", "cost evidence"),
        ("cash_drag_status", "E6", "cash drag"),
        ("after_tax_status", "E6", "after-tax"),
    ):
        unavailable = sorted(
            str(row.get("strategy_id"))
            for row in summary
            if field_name in row and not is_available(row.get(field_name))
        )
        if unavailable:
            yield _finding(
                "completeness.required_outputs",
                NOT_READY,
                f"strategy_summary.{field_name}",
                {
                    "unavailable": unavailable,
                    "available": len(summary) - len(unavailable),
                },
                f"{label} available for all {len(summary)} strategies",
                REPORT,
                f"$.strategy_summary[*].{field_name}",
                (defect,),
                f"{label} unavailable",
            )
    if "provider_strategy_returns" in ctx.report and not rows(
        ctx.report, "provider_strategy_returns"
    ):
        yield _finding(
            "completeness.required_outputs",
            NOT_READY,
            "provider_strategy_returns",
            0,
            "provider strategy return rows",
            REPORT,
            "$.provider_strategy_returns",
            ("E7",),
            "provider strategy returns empty",
        )
    # The contract (leverage_accounting, required_facts) requires gross exposure and
    # financing cost to be disclosed for the levered accounts; it names no
    # ``leverage_ratio`` field. Coverage is read per applicable account and measure: the
    # active accounts of the crosswalk (the accounts named in the disclosures when there
    # is no crosswalk), each needing a covered row of each measure. A missing row, an
    # unavailable one and an observed-only one all leave that account uncovered.
    disclosures = rows(ctx.report, "leverage_disclosures")
    if disclosures:
        reference = universe(ctx)
        applicable = sorted(
            reference.active_accounts
            if reference is not None
            else {
                str(row["account_id"])
                for row in disclosures
                if row.get("account_id") is not None
            }
        )
        for measure, entity, basis in (
            (
                "LEVERAGE_EXPOSURE",
                "leverage_disclosures.gross_exposure",
                "a gross-exposure disclosure for each levered account",
            ),
            (
                "FINANCING_COST",
                "leverage_disclosures.financing_cost",
                "a reconciled financing cost basis for each levered account",
            ),
        ):
            measured = [row for row in disclosures if row.get("data_class") == measure]
            state: dict[str, str] = {}
            for account in applicable:
                statuses = [
                    str(row.get("coverage_status"))
                    for row in measured
                    if str(row.get("account_id")) == account
                ]
                if not any(is_available(status) for status in statuses):
                    state[account] = (
                        "NO_ROW" if not statuses else "|".join(sorted(set(statuses)))
                    )
            if applicable:
                gap = bool(state)
            else:
                gap = not any(
                    is_available(row.get("coverage_status")) for row in measured
                )
            if gap:
                yield _finding(
                    "completeness.required_outputs",
                    NOT_READY,
                    entity,
                    {
                        "rows": len(measured),
                        "applicable_accounts": len(applicable),
                        "uncovered_accounts": state,
                    },
                    basis,
                    REPORT,
                    "$.leverage_disclosures",
                    ("E8",),
                    f"{measure.lower().replace('_', ' ')} is not covered for every "
                    "levered account (missing, observed only or unavailable)",
                )


# -------------------------------------------------------------------- code checks


def _payload_globals(ctx: Context) -> set[str]:
    name = ctx.gen.scan.panel_script_global
    if name:
        return {name}
    js = ctx.gen.app_js or ""
    return set(re.findall(r"window\s*\.\s*([A-Z][A-Z0-9_]{2,})\b", js))


def check_app_guard(ctx: Context) -> Iterator[Finding]:
    """F2: a payload binding dereferenced at load time before any null guard.

    The payload global is read from the data script (``window.X = {...}``); every
    binding of it is followed, whatever its name, and any equivalent guard form
    (``!x``, ``x == null``, ``typeof x``, ``x &&``/``??``) counts. Dereferences inside
    deferred function bodies are assumed to run after the guard.
    """

    js = ctx.gen.app_js
    if not js:
        return
    for item in shell.payload_dereferences_before_guard(js, _payload_globals(ctx)):
        yield _finding(
            "code.data_deref_before_guard",
            BLOCKER_INVARIANT,
            f"{APP}:{item.binding}",
            {"binding": item.binding, "first_eager_dereference_line": item.line},
            {"guard_before_dereference": True, "first_guard_line": item.guard_line},
            APP,
            f"line {item.line}",
            ("F2",),
            "payload dereferenced at load time before the missing-payload guard; "
            "the error state is unreachable",
        )


def check_unescaped_dates(ctx: Context) -> Iterator[Finding]:
    """F7: a date formatter whose fallback returns raw input into an HTML sink.

    Date formatters are recognised by what they do (``new Date``/``Date.parse``/
    ``toLocaleDateString``/``DateTimeFormat``), not by name; escape helpers by their
    HTML-entity replacement. A return branch that hands back its argument unescaped,
    used in markup without an escape wrapper, fires.
    """

    js = ctx.gen.app_js
    if not js:
        return
    for item in shell.unescaped_date_formatters(js):
        yield _finding(
            "code.unescaped_date_fallback",
            BLOCKER_INVARIANT,
            f"{APP}:{item.name}",
            {
                "formatter": item.name,
                "raw_fallback_returns": list(item.raw_returns),
                "html_sink_lines": list(item.sink_lines)[:20],
                "html_sinks": len(item.sink_lines),
            },
            "every fallback branch escaped before reaching HTML",
            APP,
            f"line {item.line}",
            ("F7",),
            "unparseable date values are interpolated into HTML unescaped",
        )


def check_stress_callout(ctx: Context) -> Iterator[Finding]:
    """F1: a stress callout must render only when the payload says it applies.

    Callouts are tied to the advisory stress payload by data flow (markup on a line
    that uses ``advisory.*stress*`` or a value derived from it), whatever the wording;
    the render condition and any literal percentage are compared to the payload.
    """

    js = ctx.gen.app_js
    if not js:
        return
    limit = to_decimal(node(ctx.panel, "advisory", "hwm_limit"))
    stress = to_decimal(node(ctx.panel, "advisory", "correlation_one_stress"))
    for callout in shell.stress_callouts(js):
        thresholds = [
            Decimal(value) / Decimal(100) for value in callout.literal_thresholds
        ]
        observed = {
            "unconditional": callout.unconditional,
            "literal_thresholds": thresholds,
            "payload_hwm_limit": limit,
            "payload_stress": stress,
        }
        mismatch = limit is not None and any(value != limit for value in thresholds)
        if (
            callout.unconditional
            and stress is not None
            and limit is not None
            and stress <= limit
        ):
            klass, message = (
                BLOCKER_INVARIANT,
                "callout claims a breach the data does not show",
            )
        elif mismatch:
            klass, message = (
                BLOCKER_INVARIANT,
                "hard-coded callout threshold differs from the payload",
            )
        elif callout.unconditional:
            klass, message = (
                WARNING,
                "callout is unconditional (currently true); render it from the payload",
            )
        else:
            continue
        yield _finding(
            "code.stress_callout",
            klass,
            f"{APP}:line {callout.line}",
            observed,
            "callout rendered only when stress > payload hwm_limit; no literal threshold",
            APP,
            f"line {callout.line}",
            ("F1",),
            message,
        )


def check_hardcoded_status(ctx: Context) -> Iterator[Finding]:
    """D8: the shell may not render a literal positive status against the payload.

    Any positive readiness word (PUBLISHED, VERIFIED, READY, ...) rendered from a
    literal — text node, attribute, DOM text sink or a literal argument inside a
    template interpolation — contradicts a payload that says NOT_READY/NOT_PERFORMED.
    """

    readiness = _readiness(ctx)
    not_ready = {
        k: v
        for k, v in readiness.items()
        if v in ("NOT_READY", "NOT_PERFORMED", "REQUIRED_NOT_READY", "STAGED")
    }
    if not not_ready:
        return
    literals = []
    if ctx.gen.app_js:
        literals += shell.hard_coded_statuses(APP, ctx.gen.app_js)
    if ctx.gen.index_shell:
        literals += shell.hard_coded_statuses(INDEX, ctx.gen.index_shell)
    for item in literals:
        yield _finding(
            "code.hardcoded_status_literal",
            BLOCKER_AUTHORITY,
            f"{item.file}:line {item.line}",
            {"literal": item.literal, "context": item.context, "payload": not_ready},
            "status rendered from the readiness payload",
            item.file,
            f"line {item.line}",
            ("D8",),
            "shell renders a literal positive status while the payload says not ready",
        )


# --------------------------------------------------------------------- lint (4.5E)


def _window_days(start: Any, end: Any) -> int | None:
    first, last = to_date(start), to_date(end)
    if first is None or last is None:
        return None
    return (last - first).days


def _unique_label(prefix: str, row: Mapping[str, Any], taken: Mapping[str, Any]) -> str:
    """``prefix`` and the row's ``account|strategy|start`` label, never one already taken.

    Epochs that start together share that label; the later one is told apart by its end
    (and, were that shared too, a count), so a lint payload keeps an entry for each.
    """

    label = prefix + "|".join(epoch_key(row))
    if label in taken:
        label = f"{label}|{row.get('effective_end')}"
    base, count = label, 1
    while label in taken:
        count += 1
        label = f"{base}#{count}"
    return label


def check_lint_returns(ctx: Context) -> Iterator[Finding]:
    """A2/A4/A5 and extreme live returns: plausibility warnings only."""

    modes = node(ctx.report, "dependence_analysis", "mode_summaries")
    extreme: dict[str, Any] = {}
    short: dict[str, Any] = {}
    for mode, summary in sorted(modes.items()) if isinstance(modes, Mapping) else []:
        performance = node(summary, "performance")
        for series, perf in (
            sorted(performance.items()) if isinstance(performance, Mapping) else []
        ):
            if not isinstance(perf, Mapping):
                continue
            cagr = to_decimal(perf.get("cagr"))
            cumulative = to_decimal(perf.get("cumulative_return"))
            if cagr is not None and (cagr > 1 or cagr < Decimal("-0.5")):
                extreme[f"{mode}:{series}"] = {
                    "cagr": cagr,
                    "cumulative_return": cumulative,
                }
            days = _window_days(perf.get("observed_start"), perf.get("observed_end"))
            if cagr is not None and days is not None and days < ONE_YEAR_DAYS:
                short[f"dependence.{mode}:{series}"] = days
    for row in rows(ctx.report, "strategy_summary"):
        cagr = to_decimal(row.get("account_live_cagr"))
        days = _window_days(
            row.get("account_live_observed_start"), row.get("account_live_observed_end")
        )
        if cagr is not None and (cagr > 1 or cagr < Decimal("-0.5")):
            extreme[f"strategy_summary:{row.get('strategy_id')}"] = {"cagr": cagr}
        if cagr is not None and days is not None and days < ONE_YEAR_DAYS:
            short[f"strategy_summary.account_live_cagr:{row.get('strategy_id')}"] = days
    for _, row in twr_rows(ctx):
        days = _window_days(row.get("mwr_opening_date"), row.get("mwr_terminal_date"))
        if (
            row.get("mwr_unit") == "ANNUALIZED_RETURN"
            and days is not None
            and days < ONE_YEAR_DAYS
            and row.get("mwr_xirr") is not None
        ):
            short[_unique_label("twr_mwr.mwr_xirr:", row, short)] = days
    impossible: dict[str, Any] = {}
    comparisons = node(ctx.report, "strategy_attribution", "benchmark_comparisons")
    for row in comparisons if isinstance(comparisons, list) else []:
        metrics = row.get("metrics") if isinstance(row, dict) else None
        if not isinstance(metrics, Mapping):
            continue
        days = _window_days(metrics.get("observed_start"), metrics.get("observed_end"))
        for key, value in metrics.items():
            amount = to_decimal(value)
            if "annualized" not in key or amount is None:
                continue
            if amount < -1:
                impossible[f"{row.get('strategy_id')}.{key}"] = amount
            if days is not None and days < ONE_YEAR_DAYS:
                short[f"benchmark.{row.get('strategy_id')}.{key}"] = days
    if extreme:
        yield _finding(
            "lint.extreme_cagr",
            WARNING,
            "cagr",
            extreme,
            "|CAGR| plausible (<=100%/yr) or provenanced",
            REPORT,
            "$.dependence_analysis.mode_summaries[*].performance",
            ("A2",),
            "extreme CAGR (spliced backtest?) needs provenance",
        )
    if impossible:
        yield _finding(
            "lint.impossible_annualized",
            WARNING,
            "annualized",
            impossible,
            ">= -100% annualized",
            REPORT,
            "$.strategy_attribution.benchmark_comparisons[*].metrics",
            ("A4",),
            "annualized return below -100%",
        )
    if short:
        yield _finding(
            "lint.short_window_annualization",
            WARNING,
            "annualized",
            short,
            f"annualize only windows >= {ONE_YEAR_DAYS} days",
            REPORT,
            "(multiple)",
            ("A5",),
            "annualized figures on windows shorter than one year",
        )
    live: dict[str, Any] = {}
    for _, row in twr_rows(ctx):
        for key in ("twr", "return_path_cumulative_return"):
            amount = to_decimal(row.get(key))
            if amount is not None and amount > 5:
                live[_unique_label(f"twr_mwr.{key}:", row, live)] = amount
    for _, row in manifest_rows(ctx):
        amount = to_decimal(row.get("observation_linked_return"))
        if amount is not None and amount > 5:
            label = _unique_label(
                "account_manifest.observation_linked_return:", row, live
            )
            live[label] = amount
    if live:
        yield _finding(
            "lint.extreme_live_return",
            WARNING,
            "live_return",
            live,
            "cumulative live return <= 500% or provenanced",
            REPORT,
            "$.twr_mwr",
            ("A1",) if any(".twr:" in key for key in live) else (),
            "implausible cumulative live return",
        )


def check_lint_alpha(ctx: Context) -> Iterator[Finding]:
    """A3: extreme or mode-contradictory alpha."""

    values: dict[tuple[str, str, str], dict[str, Decimal]] = {}
    modes = node(ctx.report, "decomposition_lenses", "modes")
    for mode, lenses in sorted(modes.items()) if isinstance(modes, Mapping) else []:
        for lens in lenses if isinstance(lenses, list) else []:
            for series in rows(lens, "series"):
                alpha = to_decimal(node(series, "alpha", "net_annualized"))
                if alpha is not None:
                    key = (
                        str(lens.get("lens_id")),
                        str(series.get("series_id")),
                        str(series.get("source_key")),
                    )
                    values.setdefault(key, {})[mode] = alpha
    extreme = {
        f"{lens}:{series}:{source}:{mode}": alpha
        for (lens, series, source), by_mode in sorted(values.items())
        for mode, alpha in sorted(by_mode.items())
        if abs(alpha) > Decimal("0.5")
    }
    flips = {
        f"{lens}:{series}:{source}": by_mode
        for (lens, series, source), by_mode in sorted(values.items())
        if len(by_mode) == 2
        and min(abs(v) for v in by_mode.values()) > Decimal("0.1")
        and (by_mode.get("LIVE_ONLY", Decimal(0)) > 0)
        != (by_mode.get("LIVE_PLUS_BACKTEST", Decimal(0)) > 0)
    }
    if extreme or flips:
        yield _finding(
            "lint.extreme_alpha",
            WARNING,
            "decomposition alpha",
            {
                "extreme_abs_over_50pct": extreme,
                "sign_contradiction_between_modes": flips,
            },
            "plausible, mode-consistent alpha or provenance",
            REPORT,
            "$.decomposition_lenses.modes[*][*].series[*].alpha.net_annualized",
            ("A3",),
            "extreme or contradictory alpha",
        )


def check_lint_daily_moves(ctx: Context) -> Iterator[Finding]:
    """A6: a >4 sigma daily move within 3 days of a flow."""

    flow_days: dict[str, set[date]] = {}
    for _, row in manifest_rows(ctx):
        strategy = str(row.get("strategy_id"))
        for item in row.get("capital_flow_trace") or []:
            if isinstance(item, dict):
                for key in ("event_date", "nav_recognition_date"):
                    day = to_date(item.get(key))
                    if day is not None:
                        flow_days.setdefault(strategy, set()).add(day)
    for _, row in twr_rows(ctx):
        for item in _interior_cashflows(row):
            day = to_date(item.get("date"))
            if day is not None:
                flow_days.setdefault(str(row.get("strategy_id")), set()).add(day)
    near: dict[str, Any] = {}
    for strategy, series in sorted(returns_by_strategy(ctx).items()):
        values = [float(value) for _, value, _ in series]
        if len(values) < 20:
            continue
        mean = sum(values) / len(values)
        sigma = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
        if sigma == 0:
            continue
        for day, value, _ in series:
            if abs(float(value) - mean) > 4 * sigma:
                close = sorted(
                    flow_day
                    for flow_day in flow_days.get(strategy, set())
                    if abs((flow_day - day).days) <= 3
                )
                if close:
                    near[f"{strategy}:{day.isoformat()}"] = {
                        "return": value,
                        "sigma_multiple": round(abs(float(value) - mean) / sigma, 2),
                        "flow_dates": [item.isoformat() for item in close],
                    }
    if near:
        yield _finding(
            "lint.daily_move_near_flow",
            WARNING,
            "strategy_returns",
            near,
            "no >4 sigma move adjacent to a flow without provenance",
            REPORT,
            "$.strategy_returns",
            ("A6",),
            "isolated extreme daily return next to an external flow",
        )


def check_lint_invested_capital(ctx: Context) -> Iterator[Finding]:
    """B7: invested capital versus net contributed capital."""

    net: dict[str, Decimal] = {}
    for _, row in manifest_rows(ctx):
        strategy = str(row.get("strategy_id"))
        if row.get("effective_end") is None:
            net[strategy] = net.get(strategy, Decimal(0)) + (
                to_decimal(row.get("net_capital")) or Decimal(0)
            )
    differences: dict[str, Any] = {}
    for row in rows(node(ctx.report, "invested_capital"), "current_by_strategy"):
        strategy = str(row.get("strategy_id"))
        invested = to_decimal(row.get("invested_capital_usd"))
        if (
            invested is not None
            and strategy in net
            and abs(invested - net[strategy]) > USD_TOLERANCE
        ):
            differences[strategy] = {
                "invested_capital_usd": invested,
                "net_contributed_capital": net[strategy],
            }
    if differences:
        yield _finding(
            "lint.invested_capital_vs_contributions",
            WARNING,
            "invested_capital",
            differences,
            "invested capital reconciles to contributions or is provenanced",
            REPORT,
            "$.invested_capital.current_by_strategy",
            ("B7",),
            "invested capital differs from net contributions",
        )


def check_lint_display_tokens(ctx: Context) -> Iterator[Finding]:
    """F3: internal machine tokens rendered in operator-facing notes."""

    tokens: dict[str, list[str]] = {}
    for position, row in enumerate(rows(ctx.panel, "gates")):
        found = sorted(set(_MACHINE_WORD.findall(str(row.get("note", "")))))
        if found:
            tokens[f"gates[{position}]:{row.get('item')}"] = found[:10]
    if tokens:
        yield _finding(
            "lint.internal_tokens_displayed",
            WARNING,
            "gates.note",
            tokens,
            "operator-readable text",
            PANEL,
            "$.gates[*].note",
            ("F3",),
            "internal tokens rendered in gate notes",
        )


def check_lint_payload_size(ctx: Context) -> Iterator[Finding]:
    """F4/F5: payload size and duplicate copies (never a G1 prerequisite)."""

    files = ctx.gen.scan.files
    large = {rel: size for rel, (size, _) in sorted(files.items()) if size > 50_000_000}
    by_sha: dict[str, list[str]] = {}
    for rel, (size, digest) in files.items():
        if size > 10_000_000:
            by_sha.setdefault(digest, []).append(rel)
    duplicates = {
        digest: sorted(paths)
        for digest, paths in sorted(by_sha.items())
        if len(paths) > 1
    }
    weekly = node(ctx.panel, "weekly_returns")
    weeks = node(weekly, "weeks")
    series = node(weekly, "series")
    payload_cells = (
        len(weeks) * len(series)
        if isinstance(weeks, list) and isinstance(series, list)
        else 0
    )
    # The cells painted are the default window's weeks times the series, not the whole
    # grid, when the renderer really slices to that window: ``shell.weekly_window_bound``
    # checks the declaration, the slicing function and its use (a structural heuristic,
    # not an execution of the renderer). Without it the whole payload is measured.
    window = shell.weekly_window_bound(ctx.gen.app_js or "")
    cells = (
        min(window, len(weeks)) * len(series)
        if window is not None and isinstance(weeks, list) and isinstance(series, list)
        else payload_cells
    )
    if large or duplicates:
        yield _finding(
            "lint.payload_size",
            WARNING,
            "generation",
            {"files_over_50MB": large, "duplicate_large_files": duplicates},
            "bounded, de-duplicated payload",
            INDEX,
            "(file sizes)",
            ("F5",),
            "very large or duplicated published payloads",
        )
    if cells > 100_000:
        yield _finding(
            "lint.payload_size",
            WARNING,
            "weekly_returns",
            {
                "weeks": len(weeks) if isinstance(weeks, list) else 0,
                "series": len(series) if isinstance(series, list) else 0,
                "rendered_cells": cells,
                "payload_cells": payload_cells,
            },
            "lazy or bounded weekly render",
            PANEL,
            "$.weekly_returns",
            ("F4",),
            "eager weekly render over a very large grid",
        )


# ----------------------------------------------------------------- expected values

_RETAINED_EXCLUDED_DATE = (
    "a RETAINED_EXCLUDED member is dated by its last admitted NAV; the oracle reads "
    "later reference rows of the deprecated account (N59)"
)

_METRIC_DEFECTS: dict[str, tuple[str, ...]] = {
    "capital.account.cutoff": ("B4",),
    "capital.strategy.cutoff": ("B4",),
    "capital.reserve.cutoff": ("B4", "B6"),
    "capital.retained_excluded.cutoff": ("B6",),
    "capital.portfolio.systematic.cutoff": ("B4", "B13"),
    "capital.portfolio.liquid.cutoff": ("B4", "B6"),
    "universe.active_strategy_ids": ("D5", "D9"),
    "universe.active_strategy_count": ("D9",),
    "flows.account.net": ("D2", "B1", "B14"),
    "flows.account.count": ("D2", "B1"),
    "twr.account": ("A1", "B1", "B2"),
    "twr.strategy": ("B2", "B3"),
    "mwr.account": (),
    "mwr.strategy": (),
    "date.source_last_observation": ("C5",),
}


def metric_unit(metric_id: str, declared: Any) -> str:
    if isinstance(declared, str) and declared:
        return declared
    if metric_id.startswith("capital.") or metric_id == "flows.account.net":
        return "USD"
    if metric_id.startswith(("twr.", "mwr.", "weight.")):
        return "ratio"
    if metric_id.endswith("_ids"):
        return "list"
    if metric_id.endswith(("count", "_count")):
        return "count"
    if metric_id.startswith("date."):
        return "date"
    return "status"


@dataclass(frozen=True)
class Observed:
    view: str
    value: Any
    start: date | None
    end: date | None
    as_of: str | None
    file: str
    json_path: str
    # Why a label older than the oracle's date is still honest (N56, N59); when it is
    # set, a matching value dated earlier is NOT_READY instead of a regression.
    date_disclosure: str | None = None


def _epoch_views(ctx: Context, account: str) -> list[tuple[int, dict[str, Any]]]:
    return [
        (position, row)
        for position, row in twr_rows(ctx)
        if str(row.get("account_id")) == account
    ]


def _flows_net(row: Mapping[str, Any]) -> Decimal | None:
    net = to_decimal(row.get("net_capital"))
    opening = to_decimal(row.get("opening_capital"))
    return net - opening if net is not None and opening is not None else None


def _cash_label(
    ctx: Context, row: Mapping[str, Any], counted: Sequence[CashLine]
) -> tuple[Any, str | None]:
    """The date the counted cash lines of a strategy carry, and why it is honest.

    The oldest ``as_of_date`` of the counted lines; a date before the cutoff is
    disclosed when every such line is explained by a verified coverage bound.  Lines
    without a canonical date fall back to the strategy row's label, undisclosed.
    """

    lines = rows(row, "derived_cash")
    days: list[tuple[date, str]] = []
    for item in counted:
        if item.line_position >= len(lines):
            return row.get("as_of_date"), None
        day = _canonical_iso_day(lines[item.line_position].get("as_of_date"))
        if day is None:
            return row.get("as_of_date"), None
        days.append((day, f"{item.strategy_id}:{item.account_id}"))
    oldest = min(day for day, _ in days)
    early = [(day, entity) for day, entity in days if day < ctx.cutoff]
    if not early:
        return oldest.isoformat(), None
    entries = [
        verified_capital_coverage_bound(ctx, entity, day) for day, entity in early
    ]
    if all(entry is not None for entry in entries):
        names = sorted(
            {
                name
                for entry in entries
                if entry is not None
                for name in entry.bounded_by
            }
        )
        return oldest.isoformat(), (
            "the cash line is dated by the admitted coverage of "
            f"{', '.join(names)} (capital_coverage_bounds, N56)"
        )
    return oldest.isoformat(), None


def resolve_metric(ctx: Context, metric_id: str, entity: str) -> list[Observed]:
    report, panel = ctx.report, ctx.panel
    out: list[Observed] = []
    if metric_id == "capital.account.cutoff":
        for file, document in ((REPORT, report), (PANEL, panel)):
            for position, row in enumerate(rows(document, "account_summary")):
                if row.get("account_id") == entity:
                    out.append(
                        Observed(
                            f"{file}:account_summary.latest_value_usd",
                            row.get("latest_value_usd"),
                            None,
                            None,
                            row.get("as_of_date"),
                            file,
                            f"$.account_summary[{position}].latest_value_usd",
                        )
                    )
        # The custody block carries every account the capital identity names, including
        # a retained or excluded account that has no account_summary row.
        for position, row in enumerate(
            rows(node(report, "custody_current_capital"), "strategies")
        ):
            for member_position, member in enumerate(rows(row, "members")):
                value = member.get("value")
                if (
                    member.get("account_id") == entity
                    and isinstance(value, Mapping)
                    and is_available(value.get("status"))
                ):
                    out.append(
                        Observed(
                            "report:custody_current_capital.strategies.members.value",
                            value.get("value"),
                            None,
                            None,
                            member.get("as_of_date"),
                            REPORT,
                            f"$.custody_current_capital.strategies[{position}]"
                            f".members[{member_position}].value.value",
                            _RETAINED_EXCLUDED_DATE
                            if row.get("scope") == "RETAINED_EXCLUDED"
                            else None,
                        )
                    )
        for position, row in manifest_rows(ctx):
            if (
                str(row.get("account_id")) == entity
                and row.get("effective_end") is None
            ):
                out.append(
                    Observed(
                        "panel:account_manifest.current_value",
                        row.get("current_value"),
                        None,
                        None,
                        row.get("end"),
                        PANEL,
                        f"$.account_manifest.rows[{position}].current_value",
                    )
                )
    elif metric_id == "weight.strategy.cutoff":
        for file, key in ((REPORT, "strategy_summary"), (PANEL, "strategies")):
            for position, row in enumerate(
                rows(report if file == REPORT else panel, key)
            ):
                if row.get("strategy_id") == entity and "systematic_weight" in row:
                    out.append(
                        Observed(
                            f"{file}:{key}.systematic_weight",
                            row.get("systematic_weight"),
                            None,
                            None,
                            None,
                            file,
                            f"$.{key}[{position}].systematic_weight",
                        )
                    )
    elif metric_id == "capital.strategy_cash.cutoff":
        for position, row in enumerate(
            rows(node(report, "custody_current_capital"), "strategies")
        ):
            if row.get("strategy_id") != entity:
                continue
            counted = [
                line
                for line in derived_cash_lines(ctx)
                if line.strategy_id == entity
                and line.position == position
                and line.counted
                and line.amount is not None
            ]
            if counted:
                label, disclosure = _cash_label(ctx, row, counted)
                out.append(
                    Observed(
                        "report:custody_current_capital.strategies.derived_cash",
                        sum(
                            (line.amount or Decimal(0) for line in counted), Decimal(0)
                        ),
                        None,
                        None,
                        label,
                        REPORT,
                        f"$.custody_current_capital.strategies[{position}].derived_cash",
                        disclosure,
                    )
                )
    elif metric_id == "capital.strategy.cutoff":
        for file, key in ((REPORT, "strategy_summary"), (PANEL, "strategies")):
            for position, row in enumerate(
                rows(report if file == REPORT else panel, key)
            ):
                if row.get("strategy_id") == entity:
                    out.append(
                        Observed(
                            f"{file}:{key}.current_value_usd",
                            row.get("current_value_usd"),
                            None,
                            None,
                            None,
                            file,
                            f"$.{key}[{position}].current_value_usd",
                        )
                    )
        for position, row in enumerate(rows(report, "allocation_history")):
            if row.get("strategy_id") == entity and not _historical_snapshot(row):
                out.append(
                    Observed(
                        "report:allocation_history.current_value_usd",
                        row.get("current_value_usd"),
                        None,
                        None,
                        row.get("as_of_date"),
                        REPORT,
                        f"$.allocation_history[{position}].current_value_usd",
                    )
                )
    elif metric_id == "capital.reserve.cutoff":
        out.append(
            Observed(
                "panel:capital.reserve",
                node(panel, "capital", "reserve"),
                None,
                None,
                None,
                PANEL,
                "$.capital.reserve",
            )
        )
        located = summary_value(ctx, "Reserve")
        if located:
            out.append(
                Observed(
                    "report:portfolio_summary.Reserve",
                    located[1],
                    None,
                    None,
                    None,
                    REPORT,
                    f"$.portfolio_summary[{located[0]}].value",
                )
            )
        for position, row in enumerate(rows(report, "allocation_history")):
            if row.get("scope") == "RESERVE" and not _historical_snapshot(row):
                out.append(
                    Observed(
                        "report:allocation_history.RESERVE",
                        row.get("current_value_usd"),
                        None,
                        None,
                        row.get("as_of_date"),
                        REPORT,
                        f"$.allocation_history[{position}].current_value_usd",
                    )
                )
    elif metric_id == "capital.retained_excluded.cutoff":
        out.append(
            Observed(
                "panel:capital.retained_excluded",
                node(panel, "capital", "retained_excluded"),
                None,
                None,
                None,
                PANEL,
                "$.capital.retained_excluded",
            )
        )
        located = summary_value(ctx, "Retained excluded systematic balance")
        if located:
            out.append(
                Observed(
                    "report:portfolio_summary.Retained excluded systematic balance",
                    located[1],
                    None,
                    None,
                    None,
                    REPORT,
                    f"$.portfolio_summary[{located[0]}].value",
                )
            )
    elif metric_id == "capital.portfolio.systematic.cutoff":
        out.append(
            Observed(
                "panel:capital.systematic",
                node(panel, "capital", "systematic"),
                None,
                None,
                None,
                PANEL,
                "$.capital.systematic",
            )
        )
        located = summary_value(ctx, "Systematic capital")
        if located:
            out.append(
                Observed(
                    "report:portfolio_summary.Systematic capital",
                    located[1],
                    None,
                    None,
                    None,
                    REPORT,
                    f"$.portfolio_summary[{located[0]}].value",
                )
            )
        out.append(
            Observed(
                "report:hwm_summary.governed_perimeter_value_usd",
                node(report, "hwm_summary", "governed_perimeter_value_usd"),
                None,
                None,
                None,
                REPORT,
                "$.hwm_summary.governed_perimeter_value_usd",
            )
        )
    elif metric_id == "capital.portfolio.liquid.cutoff":
        out.append(
            Observed(
                "panel:capital.total_liquid",
                node(panel, "capital", "total_liquid"),
                None,
                None,
                None,
                PANEL,
                "$.capital.total_liquid",
            )
        )
        located = summary_value(ctx, "Total liquid")
        if located:
            out.append(
                Observed(
                    "report:portfolio_summary.Total liquid",
                    located[1],
                    None,
                    None,
                    None,
                    REPORT,
                    f"$.portfolio_summary[{located[0]}].value",
                )
            )
    elif metric_id == "universe.active_strategy_ids":
        for label, file, path, key in _RETIRED_VIEWS[:2] + (_RETIRED_VIEWS[-2],):
            ids = _view_ids(ctx, file, path, key)
            if ids is not None:
                out.append(
                    Observed(
                        label,
                        sorted(set(ids)),
                        None,
                        None,
                        None,
                        file,
                        "$." + ".".join(str(p) for p in path),
                    )
                )
    elif metric_id == "universe.active_strategy_count":
        counts = node(panel, "counts", "strategies")
        if counts is not None:
            out.append(
                Observed(
                    "panel:counts.strategies",
                    counts,
                    None,
                    None,
                    None,
                    PANEL,
                    "$.counts.strategies",
                )
            )
        out.append(
            Observed(
                "report:len(strategy_summary)",
                len(rows(report, "strategy_summary")),
                None,
                None,
                None,
                REPORT,
                "$.strategy_summary",
            )
        )
        for count in rendered_strategy_counts(ctx):
            out.append(
                Observed(
                    f"text:{count.file}@{count.offset}",
                    count.number,
                    None,
                    None,
                    None,
                    count.file,
                    f"(byte offset {count.offset})",
                )
            )
    elif metric_id in (
        "twr.account",
        "mwr.account",
        "flows.account.net",
        "flows.account.count",
        "date.source_last_observation",
    ):
        # L6a: a blocked epoch row the census confirms never enters an account view
        # (R2); an unconfirmed one is compared as before, and so is every other row,
        # whatever part of its key it shares with a confirmed blocked row. Each report
        # row reads the manifest row of its own epoch, and the aggregates below visit
        # every manifest row, none standing in for another (fix round 2).
        blocked = blocked_epochs(ctx)
        epochs = [
            (position, row)
            for position, row in _epoch_views(ctx, entity)
            if not blocked.skips_report_row(row)
        ]
        for position, row in epochs:
            start, end = (
                to_date(row.get("observed_start")),
                to_date(row.get("observed_end")),
            )
            matched = manifest_match(ctx, row)
            label = "|".join(epoch_key(row))
            if metric_id == "twr.account":
                out.append(
                    Observed(
                        f"report:twr_mwr.twr[{label}]",
                        row.get("twr"),
                        start,
                        end,
                        None,
                        REPORT,
                        f"$.twr_mwr[{position}].twr",
                    )
                )
                if matched:
                    out.append(
                        Observed(
                            f"panel:account_manifest.observation_linked_return[{label}]",
                            matched[1].get("observation_linked_return"),
                            to_date(matched[1].get("start")),
                            to_date(matched[1].get("end")),
                            None,
                            PANEL,
                            f"$.account_manifest.rows[{matched[0]}].observation_linked_return",
                        )
                    )
            elif metric_id == "mwr.account":
                out.append(
                    Observed(
                        f"report:twr_mwr.mwr_xirr[{label}]",
                        row.get("mwr_xirr"),
                        to_date(row.get("mwr_opening_date")),
                        to_date(row.get("mwr_terminal_date")),
                        None,
                        REPORT,
                        f"$.twr_mwr[{position}].mwr_xirr",
                    )
                )
            elif metric_id == "flows.account.net" and matched:
                out.append(
                    Observed(
                        f"panel:account_manifest.net_capital-opening_capital[{label}]",
                        _flows_net(matched[1]),
                        to_date(matched[1].get("start")),
                        to_date(matched[1].get("end")),
                        None,
                        PANEL,
                        f"$.account_manifest.rows[{matched[0]}]",
                    )
                )
            elif metric_id == "flows.account.count":
                out.append(
                    Observed(
                        f"report:twr_mwr.flow_count[{label}]",
                        row.get("flow_count"),
                        start,
                        end,
                        None,
                        REPORT,
                        f"$.twr_mwr[{position}].flow_count",
                    )
                )
                if matched:
                    trace = matched[1].get("capital_flow_trace")
                    out.append(
                        Observed(
                            f"panel:len(account_manifest.capital_flow_trace)[{label}]",
                            len(trace) if isinstance(trace, list) else None,
                            to_date(matched[1].get("start")),
                            to_date(matched[1].get("end")),
                            None,
                            PANEL,
                            f"$.account_manifest.rows[{matched[0]}].capital_flow_trace",
                        )
                    )
            elif (
                metric_id == "date.source_last_observation"
                and row.get("effective_end") is None
            ):
                raw_coverage = row.get("source_last_observation")
                if raw_coverage is not None:
                    # R6 v2: raw observation coverage is exposed on a separate
                    # field; the return window's observed_end stays the
                    # converted valuation basis.
                    out.append(
                        Observed(
                            f"report:twr_mwr.source_last_observation[{label}]",
                            raw_coverage,
                            None,
                            None,
                            None,
                            REPORT,
                            f"$.twr_mwr[{position}].source_last_observation",
                        )
                    )
                else:
                    out.append(
                        Observed(
                            f"report:twr_mwr.observed_end[{label}]",
                            row.get("observed_end"),
                            None,
                            None,
                            None,
                            REPORT,
                            f"$.twr_mwr[{position}].observed_end",
                        )
                    )
        if metric_id == "date.source_last_observation":
            for position, row in enumerate(rows(report, "account_summary")):
                if row.get("account_id") == entity:
                    raw_coverage = row.get("source_last_observation")
                    if raw_coverage is not None:
                        # R6 v2: raw observation coverage is exposed on a
                        # separate field; capital's valuation date stays on
                        # as_of_date (the last FX-converted/ledger-admitted
                        # basis).
                        out.append(
                            Observed(
                                "report:account_summary.source_last_observation",
                                raw_coverage,
                                None,
                                None,
                                None,
                                REPORT,
                                f"$.account_summary[{position}]"
                                ".source_last_observation",
                            )
                        )
                    else:
                        out.append(
                            Observed(
                                "report:account_summary.as_of_date",
                                row.get("as_of_date"),
                                None,
                                None,
                                None,
                                REPORT,
                                f"$.account_summary[{position}].as_of_date",
                            )
                        )
        if (
            metric_id in ("flows.account.net", "flows.account.count")
            and len(epochs) > 1
        ):
            starts = [to_date(row.get("observed_start")) for _, row in epochs]
            ends = [to_date(row.get("observed_end")) for _, row in epochs]
            first = (
                min(day for day in starts if day is not None) if any(starts) else None
            )
            last = max(day for day in ends if day is not None) if any(ends) else None
            total: Any
            if metric_id == "flows.account.net":
                parts = [
                    _flows_net(row)
                    for _, row in manifest_rows(ctx)
                    if str(row.get("account_id")) == entity
                    and not blocked.skips_manifest_row(row)
                ]
                total = (
                    sum(part for part in parts if part is not None)
                    if parts and all(part is not None for part in parts)
                    else None
                )
            else:
                traces = [
                    row.get("capital_flow_trace")
                    for _, row in manifest_rows(ctx)
                    if str(row.get("account_id")) == entity
                    and not blocked.skips_manifest_row(row)
                ]
                total = sum(len(t) for t in traces if isinstance(t, list))
            out.append(
                Observed(
                    f"computed:sum_over_epochs[{entity}]",
                    total,
                    first,
                    last,
                    None,
                    PANEL,
                    "$.account_manifest.rows",
                )
            )
    elif metric_id == "twr.strategy":
        for position, row in enumerate(rows(report, "strategy_summary")):
            if row.get("strategy_id") == entity:
                out.append(
                    Observed(
                        "report:strategy_summary.account_live_cumulative_return",
                        row.get("account_live_cumulative_return"),
                        to_date(row.get("account_live_observed_start")),
                        to_date(row.get("account_live_observed_end")),
                        None,
                        REPORT,
                        f"$.strategy_summary[{position}].account_live_cumulative_return",
                    )
                )
    return out


def _equal(unit: str, observed: Any, expected: Any, tolerance: Decimal | None) -> bool:
    if unit in ("USD", "ratio"):
        a, b = to_decimal(observed), to_decimal(expected)
        if a is None or b is None:
            return False
        limit = (
            tolerance
            if tolerance is not None
            else (USD_TOLERANCE if unit == "USD" else RETURN_TOLERANCE)
        )
        return abs(a - b) <= limit
    if unit == "count":
        try:
            return observed is not None and int(observed) == int(expected)
        except (TypeError, ValueError):
            return False
    if unit == "date":
        return (
            isinstance(observed, str)
            and isinstance(expected, str)
            and observed[:10] == expected[:10]
        )
    if unit == "list":
        if isinstance(expected, str):
            expected = [item.strip() for item in expected.split(",") if item.strip()]
        return (
            isinstance(observed, list)
            and isinstance(expected, list)
            and sorted(map(str, observed)) == sorted(map(str, expected))
        )
    return bool(observed == expected)


def _row_window(
    row: Mapping[str, Any], cutoff: date
) -> tuple[date | None, date | None]:
    window = row.get("window")
    start = to_date(row.get("window_start"))
    end = to_date(row.get("window_end"))
    if isinstance(window, Mapping):
        start = start or to_date(window.get("start"))
        end = end or to_date(window.get("end"))
    if end is None:
        end = to_date(row.get("as_of")) or cutoff
    return start, end


# -------------------------------------------- recomputation over an expected window
#
# A critical expected value must never be undecidable. When the generation shows the
# metric only over a different window, its own series are recomputed over the expected
# window (daily strategy returns, per-epoch TWRs, capital-flow traces, MWR cash flows)
# and compared; a view that runs past the expected row's authoritative as_of is itself
# a finding (expected.as_of_beyond_authority).


def _xirr(flows: Sequence[tuple[date, Decimal]]) -> Decimal | None:
    """Annual IRR (ACTUAL_365) by bisection; ``None`` when no root is bracketed."""

    if not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        return None
    origin = min(day for day, _ in flows)
    points = [((day - origin).days / 365.0, float(amount)) for day, amount in flows]

    def npv(rate: float) -> float:
        return float(sum(amount / (1.0 + rate) ** years for years, amount in points))

    low, high = -0.9999, 1.0
    f_low, f_high = npv(low), npv(high)
    while f_low * f_high > 0 and high < 1e6:
        high *= 4
        f_high = npv(high)
    if f_low * f_high > 0:
        return None
    for _ in range(400):
        middle = (low + high) / 2
        f_middle = npv(middle)
        if f_low * f_middle <= 0:
            high, f_high = middle, f_middle
        else:
            low, f_low = middle, f_middle
    return Decimal(repr((low + high) / 2)).quantize(Decimal("1e-12"))


def _nav_at(ctx: Context, strategy: str, day: date) -> Decimal | None:
    """Close-of-day capital from the next strategy-return row's beginning capital."""

    later = sorted(
        (
            (to_date(row.get("date")), row)
            for row in rows(ctx.report, "strategy_returns")
            if row.get("strategy_id") == strategy
        ),
        key=lambda item: item[0] or date.min,
    )
    for row_day, row in later:
        if row_day is not None and row_day > day:
            return to_decimal(row.get("beginning_capital"))
    return None


def _cash_flows(row: Mapping[str, Any]) -> list[tuple[date, Decimal]]:
    flows = []
    for item in row.get("mwr_cashflows") or []:
        if isinstance(item, dict):
            day, amount = to_date(item.get("date")), to_decimal(item.get("amount"))
            if day is not None and amount is not None:
                flows.append((day, amount))
    return flows


def _single_account(ctx: Context, row: Mapping[str, Any]) -> bool:
    accounts = current_accounts_by_strategy(ctx).get(str(row.get("strategy_id")), [])
    return row.get("effective_end") is None and len(accounts) == 1


def _terminal(
    ctx: Context, row: Mapping[str, Any], end: date
) -> tuple[date, Decimal] | None:
    """The epoch's value at ``min(end, its terminal date)``."""

    flows = _cash_flows(row)
    terminal_day = to_date(row.get("mwr_terminal_date"))
    if flows and terminal_day is not None and terminal_day <= end:
        return flows[-1]
    if _single_account(ctx, row):
        nav = _nav_at(ctx, str(row.get("strategy_id")), end)
        if nav is not None:
            return end, nav
    return None


def _flow_day(item: Mapping[str, Any]) -> date | None:
    """Spec R4: a flow is dated by the NAV that recognises it, else its event date."""

    return to_date(item.get("nav_recognition_date")) or to_date(item.get("event_date"))


def recompute_metric(
    ctx: Context,
    metric_id: str,
    entity: str,
    window: tuple[date | None, date | None],
    explicit_start: date | None = None,
) -> list[Observed]:
    """The generation's own value for ``metric_id`` strictly over the expected window.

    Spec R1: the window is ``(B, E]`` where ``B`` is the valuation boundary — the
    expected row's explicit start, else the entity's ITD start (its first epoch's
    opening date) — and ``E`` the row's as_of. The opening value is the value at ``B``;
    only returns and flows dated after ``B`` and on or before ``E`` enter. Epochs that
    end on or before ``B`` are excluded; an epoch straddling ``B`` or ``E`` is cut at the
    boundary from the daily series when the account alone carries its strategy, and
    the recompute declines (returns ``[]``) when it cannot be cut exactly. A strategy
    TWR and an epoch cut splice the verified epoch HPR gap spans inside ``(B, E]``
    (``splice_window``) and decline when ``B`` or ``E`` lies strictly inside a span.
    """

    end = window[1]
    if end is None:
        return []
    strategy_level = metric_id in ("twr.strategy", "mwr.strategy")
    # L6a: a blocked epoch the census confirms is outside the account's TWR and MWR
    # (R2) and publishes no dates or cash flows, so it would only make the account
    # recompute decline. The strategy-level recompute is not changed (l0-design 5.1
    # skips account-level views only).
    blocked = blocked_epochs(ctx)
    epochs = sorted(
        (
            (position, row)
            for position, row in twr_rows(ctx)
            if str(row.get("strategy_id" if strategy_level else "account_id")) == entity
            and (strategy_level or not blocked.skips_report_row(row))
        ),
        key=lambda item: str(item[1].get("effective_start")),
    )
    epoch_starts = [
        day
        for _, row in epochs
        if (day := to_date(row.get("observed_start"))) is not None
    ]
    boundary = explicit_start or (min(epoch_starts) if epoch_starts else None)
    if metric_id == "twr.strategy":
        after = boundary if boundary is not None else date.min
        spliced = splice_window(ctx, entity, after + timedelta(days=1), end)
        if spliced.value is None:
            return []
        return [
            Observed(
                (
                    f"recomputed:compound(report.strategy_returns[{entity}])"
                    if not spliced.spans
                    else f"recomputed:splice(report.strategy_returns[{entity}], "
                    f"{spliced.spans} verified epoch_hpr_receipts span(s))"
                )
                + f" over ({after}, {end}]",
                spliced.value,
                boundary,
                end,
                None,
                REPORT,
                "$.strategy_returns",
            )
        ]
    if not epochs or boundary is None:
        return []
    if metric_id == "twr.account":
        chain = Decimal(1)
        parts = []
        for position, row in epochs:
            epoch_start = to_date(row.get("observed_start"))
            epoch_end = to_date(row.get("observed_end"))
            if epoch_start is None or epoch_end is None:
                return []
            if epoch_end <= boundary or epoch_start >= end:
                continue
            if epoch_start >= boundary and epoch_end <= end:
                value = to_decimal(row.get("twr"))
                if value is None:
                    return []
                parts.append(f"twr_mwr[{position}].twr")
            elif _single_account(ctx, row):
                cut_from = max(boundary, epoch_start)
                cut_to = min(end, epoch_end)
                cut = splice_window(
                    ctx,
                    str(row.get("strategy_id")),
                    cut_from + timedelta(days=1),
                    cut_to,
                )
                if cut.value is None:
                    return []
                value = cut.value
                parts.append(
                    (
                        f"compound(strategy_returns[{row.get('strategy_id')}]"
                        if not cut.spans
                        else f"splice(strategy_returns[{row.get('strategy_id')}], "
                        f"{cut.spans} verified epoch_hpr_receipts span(s)"
                    )
                    + f" over ({cut_from}, {cut_to}])"
                )
            else:
                return []
            chain *= Decimal(1) + value
        if not parts:
            return []
        return [
            Observed(
                "recomputed:" + " x ".join(parts),
                chain - Decimal(1),
                boundary,
                end,
                None,
                REPORT,
                "$.twr_mwr",
            )
        ]
    if metric_id in ("flows.account.net", "flows.account.count"):
        events = []
        for _, row in manifest_rows(ctx):
            if str(row.get("account_id")) != entity or blocked.skips_manifest_row(row):
                continue
            for item in row.get("capital_flow_trace") or []:
                day = _flow_day(item) if isinstance(item, dict) else None
                if day is not None and boundary < day <= end:
                    events.append(item)
        if metric_id == "flows.account.count":
            value_out: Any = len(events)
        else:
            total = Decimal(0)
            for item in events:
                amount = to_decimal(item.get("amount")) or Decimal(0)
                kind = str(item.get("class", "")).upper()
                total += -amount if kind.endswith("_OUT") else amount
            value_out = total
        return [
            Observed(
                f"recomputed:account_manifest.capital_flow_trace[{entity}] over "
                f"({boundary}, {end}]",
                value_out,
                boundary,
                end,
                None,
                PANEL,
                "$.account_manifest.rows[*].capital_flow_trace",
            )
        ]
    if metric_id not in ("mwr.account", "mwr.strategy"):
        return []
    flows: list[tuple[date, Decimal]] = []
    opened = False
    for _, row in epochs:
        epoch_flows = _cash_flows(row)
        epoch_start = to_date(row.get("observed_start"))
        terminal_day = to_date(row.get("mwr_terminal_date"))
        if not epoch_flows or epoch_start is None or terminal_day is None:
            return []
        if terminal_day <= boundary or epoch_start >= end:
            continue
        if epoch_start >= boundary:
            if metric_id == "mwr.strategy" or not opened:
                flows.append(epoch_flows[0])
        elif _single_account(ctx, row):
            nav = _nav_at(ctx, str(row.get("strategy_id")), boundary)
            if nav is None:
                return []
            flows.append((boundary, -nav))
        else:
            return []
        opened = True
        flows += [f for f in epoch_flows[1:-1] if boundary < f[0] <= end]
        if terminal_day <= end:
            if metric_id == "mwr.strategy" or row is epochs[-1][1]:
                flows.append(epoch_flows[-1])
        else:
            terminal = _terminal(ctx, row, end)
            if terminal is None:
                return []
            flows.append(terminal)
    rate = _xirr(flows) if flows else None
    if rate is None:
        return []
    return [
        Observed(
            f"recomputed:xirr(twr_mwr.mwr_cashflows[{entity}]) over ({boundary}, {end}]",
            rate,
            boundary,
            end,
            None,
            REPORT,
            "$.twr_mwr[*].mwr_cashflows",
        )
    ]


# Per-account performance is not a required output for an owner-excluded account (its
# capital is: the retained-excluded identity), so only these metrics are expected
# exclusions when the oracle states them not derivable.
_PERFORMANCE_METRICS = frozenset(
    {"twr.account", "mwr.account", "flows.account.net", "flows.account.count"}
)


# The only EXCLUDED_ dispositions the crosswalk contract defines. An out-of-vocabulary
# value (a corrupted or new label) is not an owner exclusion and excludes nothing.
OWNER_EXCLUSION_DISPOSITIONS = frozenset(
    {"EXCLUDED_DEPRECATED_HISTORY_ONLY", "EXCLUDED_IMMATERIAL"}
)


def _owner_excluded_accounts(ctx: Context) -> dict[str, str]:
    """Accounts the crosswalk excludes by an owner disposition, by account."""

    reference = universe(ctx)
    if reference is None:
        return {}
    return {
        account: disposition
        for account, disposition in reference.excluded_accounts.items()
        if disposition in OWNER_EXCLUSION_DISPOSITIONS
    }


def _is_owner_exclusion(
    row: Mapping[str, Any], owner_excluded: Mapping[str, str]
) -> bool:
    return (
        row.get("metric_id") in _PERFORMANCE_METRICS
        and str(row.get("entity")) in owner_excluded
    )


def expected_exclusions(ctx: Context) -> list[dict[str, Any]]:
    """Oracle not-derivable performance rows of owner-excluded accounts.

    They are not findings (the findings contract has five classes and the oracle states
    them not derivable by design): the validator writes them to its details file.
    """

    owner_excluded = _owner_excluded_accounts(ctx)
    return [
        {
            "metric_id": row.get("metric_id"),
            "entity": row.get("entity"),
            "disposition": owner_excluded[str(row.get("entity"))],
            "reason": row.get("reason"),
        }
        for row in ctx.expected_not_derivable
        if _is_owner_exclusion(row, owner_excluded)
    ]


def check_expected(ctx: Context) -> Iterator[Finding]:
    if ctx.expected is None:
        yield _finding(
            "expected.not_supplied",
            NOT_READY,
            "expected-baseline",
            None,
            "an --expected baseline",
            "(none)",
            "$",
            (),
            "no fixed-baseline expectations supplied; regression checks skipped",
        )
        return
    owner_excluded = _owner_excluded_accounts(ctx)
    for row in ctx.expected_not_derivable:
        if _is_owner_exclusion(row, owner_excluded):
            continue
        yield _finding(
            "expected.not_derivable",
            NOT_READY,
            f"{row.get('metric_id')}:{row.get('entity')}",
            None,
            row.get("reason"),
            "(expected baseline)",
            "$.not_derivable",
            _METRIC_DEFECTS.get(str(row.get("metric_id")), ()),
            "metric not derivable from upstream authority",
        )
    for position, row in enumerate(ctx.expected):
        metric_id = str(row.get("metric_id"))
        entity = str(row.get("entity"))
        expected = row.get("expected", row.get("value"))
        unit = metric_unit(metric_id, row.get("unit"))
        tolerance = to_decimal(row.get("tolerance"))
        defects = _METRIC_DEFECTS.get(metric_id, ())
        label = f"{entity}|{row.get('window') or row.get('as_of') or ''}"
        observed = resolve_metric(ctx, metric_id, entity)
        wanted = _row_window(row, ctx.cutoff)
        if not observed:
            recomputed = recompute_metric(
                ctx, metric_id, entity, wanted, _row_window(row, ctx.cutoff)[0]
            )
            if not recomputed:
                yield _finding(
                    f"expected.{metric_id}",
                    NOT_READY,
                    label,
                    None,
                    expected,
                    "(expected baseline)",
                    f"$[{position}]",
                    defects,
                    "metric has no production view and no series to recompute it from",
                )
            yield from _compare_views(
                metric_id, label, row, recomputed, expected, unit, tolerance, defects
            )
            continue
        has_window = any(view.end is not None for view in observed)
        if has_window:
            if wanted[0] is None:
                starts = [view.start for view in observed if view.start is not None]
                wanted = (min(starts) if starts else None, wanted[1])
            comparable = [
                view
                for view in observed
                if view.end is None
                or windows_equivalent(wanted, (view.start, view.end))
            ]
        else:
            comparable = observed
        if not comparable:
            beyond = [
                view
                for view in observed
                if view.end is not None
                and wanted[1] is not None
                and view.end > wanted[1]
                and (
                    view.start is None
                    or wanted[0] is None
                    or abs((view.start - wanted[0]).days) <= WINDOW_START_SLACK_DAYS
                )
            ]
            if beyond:
                yield _finding(
                    "expected.as_of_beyond_authority",
                    BLOCKER_AUTHORITY,
                    f"{metric_id}:{label}",
                    [
                        {"view": view.view, "window": [view.start, view.end]}
                        for view in beyond
                    ],
                    {"authoritative_as_of": wanted[1], "window": list(wanted)},
                    beyond[0].file,
                    beyond[0].json_path,
                    tuple(defects) + ("C5",),
                    "the output claims currency past its last authoritative observation",
                )
            recomputed = recompute_metric(
                ctx, metric_id, entity, wanted, _row_window(row, ctx.cutoff)[0]
            )
            if not beyond and not recomputed:
                yield _finding(
                    f"expected.{metric_id}",
                    NOT_READY,
                    label,
                    [
                        {"view": view.view, "window": [view.start, view.end]}
                        for view in observed
                    ],
                    {"expected": expected, "window": list(wanted)},
                    "(expected baseline)",
                    f"$[{position}]",
                    defects,
                    "no production view covers the expected window and its own "
                    "series cannot be recomputed over it",
                )
            yield from _compare_views(
                metric_id, label, row, recomputed, expected, unit, tolerance, defects
            )
            continue
        yield from _compare_views(
            metric_id, label, row, comparable, expected, unit, tolerance, defects
        )


def _compare_views(
    metric_id: str,
    label: str,
    row: Mapping[str, Any],
    views: Sequence[Observed],
    expected: Any,
    unit: str,
    tolerance: Decimal | None,
    defects: Sequence[str],
) -> Iterator[Finding]:
    for view in views:
        if not _equal(unit, view.value, expected, tolerance):
            yield _finding(
                f"expected.{metric_id}",
                BLOCKER_REGRESSION,
                label,
                {"view": view.view, "value": view.value, "as_of": view.as_of},
                {
                    "value": expected,
                    "authority_pointer": row.get("authority_pointer"),
                },
                view.file,
                view.json_path,
                defects,
                "fixed-baseline expected value differs",
            )
        elif (
            view.as_of
            and row.get("as_of")
            and str(view.as_of)[:10] != str(row.get("as_of"))[:10]
            and metric_id.startswith("capital.")
        ):
            # A label that errs old and says why is the disclosed N56/N59 condition;
            # the amount agrees with the oracle, so it is NOT_READY.  A newer label, or
            # an older one nothing explains, is still the regression.
            disclosed = (
                view.date_disclosure is not None
                and str(view.as_of)[:10] < str(row.get("as_of"))[:10]
            )
            yield _finding(
                f"expected.{metric_id}",
                NOT_READY if disclosed else BLOCKER_REGRESSION,
                label,
                {"view": view.view, "as_of": view.as_of},
                {"as_of": row.get("as_of")},
                view.file,
                view.json_path,
                defects,
                "value matches; its date is earlier than the oracle's and "
                f"{view.date_disclosure}"
                if disclosed
                else "value matches but is dated differently",
            )


# ------------------------------------------------------- corrections (#326 mapping)

_RESOLVED_CORRECTION_STATES = frozenset(
    {"RESOLVED", "CLOSED", "REDERIVED", "APPLIED", "DISPROVED", "SUPERSEDED"}
)
_NON_BLOCKING_APPLICABILITY = frozenset({"NOT_APPLICABLE", "NON_BLOCKING", "ADVISORY"})


def _correction_applies(ctx: Context, record: Mapping[str, Any]) -> bool:
    applicability = record.get("release_applicability")
    if (
        isinstance(applicability, str)
        and applicability.upper() in _NON_BLOCKING_APPLICABILITY
    ):
        return False
    if record.get("applicable") is False:
        return False
    generations = record.get("applies_to_generation_ids")
    if isinstance(generations, list):
        own = {ctx.gen.generation_id, str(ctx.gen.manifest.get("generation_id"))}
        embedded = {
            str(ctx.gen.candidate.get(key))
            for key in (
                "report_generation_id",
                "correlation_generation_id",
                "decomposition_generation_id",
            )
        }
        return bool(set(map(str, generations)) & (own | embedded))
    return True


def check_corrections(ctx: Context) -> Iterator[Finding]:
    """#326: an applicable, unresolved correction for an output blocks release."""

    sources: list[tuple[str, str, list[dict[str, Any]]]] = [
        (REPORT, "$.corrections", rows(ctx.report, "corrections"))
    ]
    if ctx.corrections is not None:
        sources.append(("(corrections file)", "$", ctx.corrections))
    for file, base, records in sources:
        for position, record in enumerate(records):
            status = record.get("status")
            if status is None and file == REPORT:
                continue  # generation-embedded rows without a status are applied edits
            if (
                isinstance(status, str)
                and status.upper() in _RESOLVED_CORRECTION_STATES
            ):
                continue
            if not _correction_applies(ctx, record):
                continue
            yield _finding(
                "correction.unresolved",
                BLOCKER_AUTHORITY,
                str(record.get("correction_id") or f"{base}[{position}]"),
                {
                    "status": status,
                    "subject": record.get("semantic_subject") or record.get("field"),
                },
                "resolved (re-derived) before release",
                file,
                f"{base}[{position}]",
                (),
                "an applicable correction for an output in this generation is unresolved",
            )


# -------------------------------------------------------------------------- runner

Check = Callable[[Context], Iterable[Finding]]


# The producer's lifecycle values that make no live-window claim (tools/portfolio_control_
# panel.py writes ACTIVE, DEPRECATED_HISTORY_ONLY and IMMATERIAL_EXCLUDED); any other value
# is treated as an active strategy.
_INACTIVE_LIFECYCLES = frozenset({"DEPRECATED_HISTORY_ONLY", "IMMATERIAL_EXCLUDED"})


def check_account_live_window(ctx: Context) -> Iterator[Finding]:
    """C5: an active strategy's account live window reaches the cutoff.

    ``live_boundary_observed_end`` is a pinned identity window and is not compared (V2);
    this is the window the account live return is computed over. A strategy that is not
    active (the producer's DEPRECATED_HISTORY_ONLY or IMMATERIAL_EXCLUDED) or whose live
    status is not available makes no claim.
    An active, available strategy must publish a canonical end: a missing, null or
    malformed one is a blocker. An end before the cutoff is a blocker unless the report's
    ``value_basis_bounds`` explains it (FXB-1) and the oracle confirms that bound
    (``verified_value_basis_bound``), when it is NOT_READY with the bound message.
    """

    for position, row in enumerate(rows(ctx.report, "strategy_summary")):
        if row.get("lifecycle_status") in _INACTIVE_LIFECYCLES:
            continue
        status = row.get("account_live_status")
        if status is not None and not is_available(status):
            continue
        strategy = str(row.get("strategy_id"))
        raw = row.get("account_live_observed_end")
        entity = f"account_live:{strategy}"
        pointer = f"$.strategy_summary[{position}].account_live_observed_end"
        end = _canonical_iso_day(raw)
        if end is None:
            yield _finding(
                "dates.section_as_of_vs_cutoff",
                BLOCKER_AUTHORITY,
                entity,
                {"as_of": raw, "status": status},
                ctx.cutoff,
                REPORT,
                pointer,
                ("C5",),
                "an active strategy with an available live status does not publish a "
                "canonical account live window end",
            )
            continue
        if end == ctx.cutoff:
            continue
        bound = (
            verified_value_basis_bound(ctx, "strategy_summary", strategy, end)
            if end < ctx.cutoff
            else None
        )
        yield _finding(
            "dates.section_as_of_vs_cutoff",
            NOT_READY if bound is not None else BLOCKER_AUTHORITY,
            entity,
            {"as_of": raw, "status": status},
            ctx.cutoff,
            REPORT,
            pointer,
            ("C5",),
            _bound_message(bound, "account live window")
            if bound is not None
            else "section is dated after the declared cutoff"
            if end > ctx.cutoff
            else "account live window is presented as current but ends before the cutoff",
        )


CAPITAL_SCOPES = frozenset({"SYSTEMATIC", "RETAINED_EXCLUDED"})


def _capital_available(entry: Mapping[str, Any]) -> bool:
    """Whether a custody capital object is an AVAILABLE value (not explicitly unavailable)."""

    status = entry.get("status") if "status" in entry else None
    if status is not None and not is_available(status):
        return False
    value = node(entry, "current_value")
    return not (isinstance(value, Mapping) and not is_available(value.get("status")))


def check_current_capital_as_of(ctx: Context) -> Iterator[Finding]:
    """N56: the custody capital labels are recomputed, not trusted.

    ``as_of_date`` on a custody capital object is the last date through which its value is
    proven true: a NAV member's observation date; a derived cash line's
    ``min(cutoff, coverage_end)`` (its ``last_event_date`` is the latest event, not the
    proof); a strategy row's the oldest of its members and cash lines; the Systematic
    total's the oldest of its rows. Rules, in order:

    1. every AVAILABLE member, cash line, row and total needs a canonical ``as_of_date``
       (missing: BLOCKER_AUTHORITY);
    2. a cash line that states ``last_event_date`` / ``coverage_end`` (schema versions
       baseline 2, report 3, panel 23) must have ``last_event_date`` equal to its latest
       event date and ``as_of_date`` equal to ``min(cutoff, coverage_end)``
       (BLOCKER_INVARIANT); lines of older versions are judged by rule 4 only;
    3. a row's date equals the oldest of its AVAILABLE members and cash lines, the
       Systematic date the oldest of its rows (BLOCKER_INVARIANT: a stale member hidden
       behind a cutoff label);
    4. anything dated before the cutoff is NOT_READY only when its labels are honest
       (rules 2 and 3 hold for it and everything under it) and
       ``verified_value_basis_bound`` discloses it (section ``current_capital``);
       otherwise BLOCKER_AUTHORITY, so it never passes;
    5. a date after the cutoff is BLOCKER_AUTHORITY.

    A custody scope outside the producer's vocabulary is a finding, and
    ``RETAINED_EXCLUDED`` rows are not systematic (echotrade is N59).
    """

    custody = node(ctx.report, "custody_current_capital")
    base = "$.custody_current_capital"
    findings: list[Finding] = []
    declared_version = ctx.report.get("report_schema_version")
    if isinstance(declared_version, bool):
        declared_version = None

    def emit(
        klass: str,
        entity: str,
        observed: Any,
        expected: Any,
        pointer: str,
        message: str,
    ) -> None:
        findings.append(
            _finding(
                "capital.as_of_cutoff",
                klass,
                entity,
                observed,
                expected,
                REPORT,
                pointer,
                ("N56",),
                message,
            )
        )

    def judge_date(
        entity: str,
        pointer: str,
        entry: Mapping[str, Any],
        honest: bool,
        section_entity: str,
    ) -> date | None:
        """Rules 1, 4 and 5 for one object; returns its canonical date (or None)."""

        day = _canonical_iso_day(entry.get("as_of_date"))
        if day is None:
            emit(
                BLOCKER_AUTHORITY,
                entity,
                entry.get("as_of_date"),
                "a canonical ISO as_of_date",
                f"{pointer}.as_of_date",
                "an available current-capital value carries no canonical as_of_date",
            )
            return None
        if day > ctx.cutoff:
            emit(
                BLOCKER_AUTHORITY,
                entity,
                entry.get("as_of_date"),
                ctx.cutoff,
                f"{pointer}.as_of_date",
                "current capital is valued at the cutoff but labelled with an as_of "
                "after it",
            )
        elif day < ctx.cutoff:
            bound = (
                verified_value_basis_bound(ctx, "current_capital", section_entity, day)
                if honest
                else None
            )
            coverage = (
                verified_capital_coverage_bound(ctx, section_entity, day)
                if honest and bound is None
                else None
            )
            emit(
                NOT_READY
                if bound is not None or coverage is not None
                else BLOCKER_AUTHORITY,
                entity,
                entry.get("as_of_date"),
                ctx.cutoff,
                f"{pointer}.as_of_date",
                _bound_message(bound, "current capital")
                if bound is not None
                else _coverage_message(coverage)
                if coverage is not None
                else "current capital is valued at the cutoff but labelled with an "
                "as_of before it"
                + ("" if honest else " (its labels do not recompute)"),
            )
        return day

    rows_honest: list[tuple[date, bool]] = []
    for position, row in enumerate(rows(custody, "strategies")):
        pointer = f"{base}.strategies[{position}]"
        strategy = str(row.get("strategy_id"))
        if "scope" in row and row.get("scope") not in CAPITAL_SCOPES:
            emit(
                BLOCKER_AUTHORITY,
                f"current_capital:{strategy}:scope",
                row.get("scope"),
                sorted(CAPITAL_SCOPES),
                f"{pointer}.scope",
                "the custody scope is outside the producer's vocabulary, so the "
                "capital date check cannot classify the row",
            )
            continue
        if row.get("scope", "SYSTEMATIC") != "SYSTEMATIC" or not _capital_available(
            row
        ):
            continue
        component_days: list[date] = []
        components_honest = True
        components_complete = True
        for member_position, member in enumerate(rows(row, "members")):
            if not _capital_available(member):
                continue
            day = judge_date(
                f"current_capital:{strategy}:member:{member.get('account_id')}",
                f"{pointer}.members[{member_position}]",
                member,
                True,
                f"{strategy}:{member.get('account_id')}",
            )
            if day is None:
                components_complete = False
            else:
                component_days.append(day)
        for line_position, line in enumerate(rows(row, "derived_cash")):
            if not _capital_available(line):
                continue
            line_pointer = f"{pointer}.derived_cash[{line_position}]"
            entity = f"current_capital:{strategy}:cash:{line.get('account_id')}"
            honest = True
            # The exemption is a property of the declared report schema, not of
            # field absence: a version-3 line must carry both fields.
            stated = (
                declared_version == 3
                or "last_event_date" in line
                or "coverage_end" in line
            )
            if stated:
                last = _canonical_iso_day(line.get("last_event_date"))
                coverage_end = _canonical_iso_day(line.get("coverage_end"))
                events = [
                    _canonical_iso_day(event.get("event_date"))
                    for event in rows(line, "events")
                ]
                latest = max((day for day in events if day is not None), default=None)
                if last is None or coverage_end is None:
                    honest = False
                    emit(
                        BLOCKER_INVARIANT,
                        entity,
                        {
                            "last_event_date": line.get("last_event_date"),
                            "coverage_end": line.get("coverage_end"),
                        },
                        "canonical last_event_date and coverage_end",
                        line_pointer,
                        "the cash line states last_event_date or coverage_end but not "
                        "both as canonical dates",
                    )
                else:
                    if last != latest:
                        honest = False
                        emit(
                            BLOCKER_INVARIANT,
                            entity,
                            line.get("last_event_date"),
                            latest.isoformat() if latest else None,
                            f"{line_pointer}.last_event_date",
                            "last_event_date is not the latest event date of the line",
                        )
                    label = _canonical_iso_day(line.get("as_of_date"))
                    if label is not None and latest is not None and latest > label:
                        honest = False
                        emit(
                            BLOCKER_INVARIANT,
                            entity,
                            latest.isoformat(),
                            f"an event on or before {label.isoformat()}",
                            f"{line_pointer}.events",
                            "the line counts an event dated after its as_of_date, so it "
                            "states a balance it did not have on that date",
                        )
                    amount = to_decimal(node(line, "current_value", "value"))
                    signed = [
                        to_decimal(event.get("signed_usd"))
                        for event in rows(line, "events")
                    ]
                    if (
                        amount is not None
                        and signed
                        and None not in signed
                        and exact_sum(s for s in signed if s is not None) != amount
                    ):
                        honest = False
                        emit(
                            BLOCKER_INVARIANT,
                            entity,
                            line.get("current_value"),
                            "the sum of its events' signed_usd",
                            f"{line_pointer}.current_value",
                            "the line's value is not the sum of its events' signed "
                            "amounts",
                        )
                    proven = min(ctx.cutoff, coverage_end)
                    if _canonical_iso_day(line.get("as_of_date")) != proven:
                        honest = False
                        emit(
                            BLOCKER_INVARIANT,
                            entity,
                            line.get("as_of_date"),
                            proven.isoformat(),
                            f"{line_pointer}.as_of_date",
                            "the cash line's as_of_date is not min(cutoff, coverage_end)",
                        )
            day = judge_date(
                entity,
                line_pointer,
                line,
                honest,
                f"{strategy}:{line.get('account_id')}",
            )
            components_honest = components_honest and honest
            if day is None:
                components_complete = False
            else:
                component_days.append(day)
        row_day_raw = row.get("as_of_date")
        row_honest = components_honest
        if components_complete and component_days:
            oldest = min(component_days)
            if _canonical_iso_day(row_day_raw) not in (None, oldest):
                row_honest = False
                emit(
                    BLOCKER_INVARIANT,
                    f"current_capital:{strategy}",
                    row_day_raw,
                    oldest.isoformat(),
                    f"{pointer}.as_of_date",
                    "the strategy's as_of_date is not the oldest of its members and "
                    "cash lines",
                )
        row_day = judge_date(
            f"current_capital:{strategy}", pointer, row, row_honest, strategy
        )
        if row_day is not None:
            rows_honest.append((row_day, row_honest))
    systematic = node(custody, "systematic")
    if isinstance(systematic, Mapping) and _capital_available(systematic):
        honest = all(flag for _, flag in rows_honest)
        raw = systematic.get("as_of_date")
        if rows_honest and _canonical_iso_day(raw) not in (
            None,
            min(day for day, _ in rows_honest),
        ):
            honest = False
            emit(
                BLOCKER_INVARIANT,
                "current_capital:systematic",
                raw,
                min(day for day, _ in rows_honest).isoformat(),
                f"{base}.systematic.as_of_date",
                "the Systematic as_of_date is not the oldest of its rows",
            )
        judge_date(
            "current_capital:systematic",
            f"{base}.systematic",
            systematic,
            honest,
            "systematic",
        )
    yield from findings


def check_evaluation_clock(ctx: Context) -> Iterator[Finding]:
    """N54: the evaluation clock must not be published as the data date.

    The sealed manifest has one date field and downstream readers take it as the data
    date, so it must be the requested cutoff: the panel producer stamps the later of the
    data date and the freshness (execution) clock on it, which is a real defect (the
    manifest is not exempt, nor is the freshness clock that drives it). The panel's own
    data date (``meta.portfolio_as_of``) is checked beside it. The history contract keeps
    two named clocks: ``as_of_date`` is the data date and must be the cutoff, and
    ``evaluated_on`` is the execution clock, which may be later than the cutoff but never
    earlier than the data date. No label a record declares about itself exempts a clock.
    """

    manifest_day = to_date(ctx.gen.manifest.get("as_of_date"))
    if manifest_day is not None and manifest_day != ctx.cutoff:
        yield _finding(
            "dates.evaluation_clock",
            BLOCKER_AUTHORITY,
            "generation.json.as_of_date",
            ctx.gen.manifest.get("as_of_date"),
            ctx.cutoff,
            GENERATION_MANIFEST,
            "$.as_of_date",
            ("N54",),
            "the generation manifest is dated at the evaluation clock, not the "
            "requested data cutoff",
        )
    portfolio_as_of = node(ctx.panel, "meta", "portfolio_as_of")
    if portfolio_as_of is not None and to_date(portfolio_as_of) != ctx.cutoff:
        yield _finding(
            "dates.evaluation_clock",
            BLOCKER_AUTHORITY,
            "meta.portfolio_as_of",
            portfolio_as_of,
            ctx.cutoff,
            PANEL,
            "$.meta.portfolio_as_of",
            ("N54",),
            "the panel data date is not the requested data cutoff",
        )
    history = node(ctx.report, "strategy_history_contract")
    if not isinstance(history, Mapping):
        return
    data_day = to_date(history.get("as_of_date"))
    if data_day != ctx.cutoff:
        yield _finding(
            "dates.evaluation_clock",
            BLOCKER_AUTHORITY,
            "strategy_history_contract.as_of_date",
            history.get("as_of_date"),
            ctx.cutoff,
            REPORT,
            "$.strategy_history_contract.as_of_date",
            ("N54",),
            "the strategy history data date is not the requested data cutoff",
        )
    evaluated = to_date(history.get("evaluated_on"))
    if evaluated is not None and data_day is not None and evaluated < data_day:
        yield _finding(
            "dates.evaluation_clock",
            BLOCKER_INVARIANT,
            "strategy_history_contract.evaluated_on",
            history.get("evaluated_on"),
            {"not_before": data_day.isoformat()},
            REPORT,
            "$.strategy_history_contract.evaluated_on",
            ("N54",),
            "the history was evaluated before its own data date",
        )


CHECKS: tuple[tuple[str, Check], ...] = (
    ("identity.required_sections", check_required_sections),
    ("identity.generation_id", check_generation_id),
    ("identity.embedded_ids", check_embedded_ids),
    ("identity.nested_generation_id", check_nested_generation_ids),
    ("identity.artifact_hashes", check_artifact_hashes),
    ("identity.embedded_payload_copies", check_embedded_payload_copies),
    ("identity.publication_state", check_publication_state),
    ("identity.manifest_coverage_claim", check_manifest_coverage_claim),
    ("identity.release_pinned", check_release_pinned),
    ("identity.absolute_local_paths", check_absolute_paths),
    ("identity.cutoff_consistency", check_cutoff_consistency),
    (CENSUS_CHECK, check_blocked_admission_census),
    ("preservation.report_to_panel", check_report_to_panel),
    ("preservation.capital_summary", check_capital_summary_preserved),
    ("preservation.counts", check_counts),
    ("capital.liquid_identity", check_liquid_identity),
    ("capital.allocation_sum", check_allocation_sum),
    ("capital.reconciliation_recompute", check_reconciliation_recompute),
    ("capital.as_of_cutoff", check_capital_as_of),
    ("capital.strategy_vs_cutoff_accounts", check_strategy_vs_cutoff_accounts),
    ("capital.account_identity", check_account_identity),
    ("universe.authority_missing", check_universe_authority),
    ("universe.active_set_consistent", check_active_universe),
    ("universe.hardcoded_count_text", check_hardcoded_count_text),
    ("universe.retired_strategies_in_current", check_retired_strategies),
    ("universe.inactive_accounts_in_current", check_inactive_accounts),
    ("returns.twr_views_agree", check_twr_views),
    ("returns.strategy_return_views_agree", check_strategy_return_views),
    ("returns.epoch_hpr_receipts_verified", check_epoch_hpr_receipts),
    ("returns.benchmark_account_return", check_benchmark_account_return),
    ("returns.flow_lists_agree", check_flow_lists),
    ("returns.mwr_opening_matches_first_flow", check_mwr_opening),
    ("returns.implied_unrecorded_flows", check_implied_flows),
    ("returns.allocation_weight_sets", check_allocation_weights),
    ("flows.admission_consistency", check_flow_admission),
    ("status.required_not_ready_displayed_available", check_required_not_ready),
    ("status.backtest_vs_hybrid", check_backtest_status),
    ("status.inference_version_unresolved", check_inference_version),
    ("status.methodology_vs_data", check_methodology),
    ("status.gate_note_contradiction", check_gate_notes),
    ("dates.freshness_vs_cutoff", check_freshness),
    ("dates.section_as_of_vs_cutoff", check_section_dates),
    ("dates.section_as_of_vs_cutoff", check_account_live_window),
    ("capital.as_of_cutoff", check_current_capital_as_of),
    (CASH_EVIDENCE_CHECK, check_derived_cash_evidence),
    ("dates.evaluation_clock", check_evaluation_clock),
    (BOUNDS_CHECK, check_value_basis_bound),
    (COVERAGE_BOUNDS_CHECK, check_capital_coverage_bound),
    ("dates.weekly_current_week", check_weekly_current_week),
    ("completeness.required_outputs", check_completeness),
    ("code.data_deref_before_guard", check_app_guard),
    ("code.unescaped_date_fallback", check_unescaped_dates),
    ("code.stress_callout", check_stress_callout),
    ("code.hardcoded_status_literal", check_hardcoded_status),
    ("lint.returns", check_lint_returns),
    ("lint.extreme_alpha", check_lint_alpha),
    ("lint.daily_move_near_flow", check_lint_daily_moves),
    ("lint.invested_capital_vs_contributions", check_lint_invested_capital),
    ("lint.internal_tokens_displayed", check_lint_display_tokens),
    ("lint.payload_size", check_lint_payload_size),
    ("correction.unresolved", check_corrections),
    ("expected", check_expected),
)


def run_checks(
    ctx: Context, checks: Sequence[tuple[str, Check]] = CHECKS
) -> list[Finding]:
    findings: list[Finding] = []
    for name, check in checks:
        try:
            findings.extend(check(ctx))
        except Exception as exc:  # noqa: BLE001 - fail closed on any check defect
            findings.append(
                _finding(
                    "validator.check_error",
                    BLOCKER_AUTHORITY,
                    name,
                    f"{type(exc).__name__}: {exc}",
                    "check evaluates the generation",
                    "(validator)",
                    name,
                    (),
                    "a check could not evaluate this generation (schema drift or "
                    "validator defect); failing closed",
                )
            )
    return sorted(findings, key=Finding.sort_key)


def summarize(findings: Sequence[Finding]) -> dict[str, int]:
    """Per-class counts: the contract's ``summary`` object (exactly five keys)."""

    by_class = {klass: 0 for klass in CLASSES}
    for finding in findings:
        by_class[finding.klass] += 1
    return by_class


def rollup(findings: Sequence[Finding]) -> dict[str, dict[str, int]]:
    """Per-check and per-defect counts, for the human summary only."""

    checks: dict[str, int] = {}
    defects: dict[str, int] = {}
    for finding in findings:
        checks[finding.check_id] = checks.get(finding.check_id, 0) + 1
        for defect in finding.defect_ids:
            defects[defect] = defects.get(defect, 0) + 1
    return {
        "by_check": dict(sorted(checks.items())),
        "by_defect": dict(sorted(defects.items())),
    }


def findings_document(
    *,
    generation_id: str,
    generation_dir: str,
    validator_commit: str,
    expected_baseline_sha256: str | None,
    findings: Sequence[Finding],
) -> dict[str, Any]:
    """The findings document of ``state/briefs/findings-contract.md``."""

    return {
        "schema": FINDINGS_SCHEMA,
        "generation_id": generation_id,
        "generation_dir": generation_dir,
        "validator_commit": validator_commit,
        "expected_baseline_sha256": expected_baseline_sha256,
        "summary": summarize(findings),
        "findings": [finding.as_json() for finding in findings],
    }
