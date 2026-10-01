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
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
import re
from typing import Any

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


def twr_rows(ctx: Context) -> list[tuple[int, dict[str, Any]]]:
    return list(enumerate(rows(ctx.report, "twr_mwr")))


def manifest_rows(
    ctx: Context,
) -> dict[tuple[str, str, str], tuple[int, dict[str, Any]]]:
    cached = ctx._cache.get("manifest_rows")
    if isinstance(cached, dict):
        return cached
    index: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}
    for position, row in enumerate(rows(node(ctx.panel, "account_manifest"), "rows")):
        key = (
            str(row.get("account_id")),
            str(row.get("strategy_id")),
            str(row.get("effective_start")),
        )
        index[key] = (position, row)
    ctx._cache["manifest_rows"] = index
    return index


def epoch_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
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
# row. The coarser ``epoch_key`` (no ``effective_end``) is only how views are matched:
# a funded epoch re-cut from the same start shares it, and keying the skip by it hid
# that funded row's contradiction with the oracle (lane epsilon fix round 1, a).

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
            f"the expected baseline carries no authority pointer for {CENSUS_CONTRACT}",
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


def reserve_accounts(ctx: Context) -> set[str]:
    """Reserve/mandate buckets, recognised from the data rather than by id."""

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


def _is_published(ctx: Context) -> tuple[bool, str | None]:
    pointer = ctx.gen.external.current_pointer
    if (
        isinstance(pointer, dict)
        and pointer.get("generation_id") == ctx.gen.generation_id
    ):
        state = pointer.get("state")
        if state == "PUBLISHED":
            return True, f"{ctx.gen.external.current_pointer_path}#$.state"
    return False, None


def check_publication_state(ctx: Context) -> Iterator[Finding]:
    """D8: a published generation cannot declare itself not ready / unverified."""

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
    """A sealed manifest may not claim no known gaps while its payload fails gates."""

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
    declared.update(
        {
            k: v
            for k, v in _readiness(ctx).items()
            if v in ("NOT_READY", "NOT_PERFORMED")
        }
    )
    if declared:
        yield _finding(
            "identity.manifest_coverage_claim",
            WARNING,
            "generation.coverage",
            {
                "coverage_status": status,
                "known_gaps": gaps,
                "payload_declares": declared,
            },
            "known_gaps names every gap the payload declares",
            GENERATION_MANIFEST,
            "$.coverage.known_gaps",
            (),
            "sealed manifest claims complete coverage with no known gaps while the "
            "payload declares failed/partial facts or not-ready readiness",
        )


def check_release_pinned(ctx: Context) -> Iterator[Finding]:
    """F8: the release that publishes this generation must be named in its bytes."""

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
    release_id = pointer["release_id"]
    manifest = external.release_manifest
    targets: list[str] = []
    if isinstance(manifest, dict):
        _collect_strings(manifest, targets)
    if ctx.gen.generation_id not in targets:
        return
    hits = {
        rel: count for rel, count in ctx.gen.scan.release_id_counts.items() if count
    }
    if not hits:
        yield _finding(
            "identity.release_pinned",
            BLOCKER_AUTHORITY,
            release_id,
            {"files_naming_release_id": 0},
            {"release_id": release_id},
            GENERATION_MANIFEST,
            "$",
            ("F8",),
            "the committed release that publishes this generation is not named "
            "anywhere in the panel bytes",
        )


def _collect_strings(value: Any, into: list[str]) -> None:
    if isinstance(value, str):
        into.append(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            _collect_strings(item, into)
    elif isinstance(value, list):
        for item in value:
            _collect_strings(item, into)


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


def _allocation_totals(ctx: Context) -> tuple[Decimal, Decimal | None, list[str]]:
    systematic = Decimal(0)
    reserve: Decimal | None = None
    dates: list[str] = []
    for row in rows(ctx.report, "allocation_history"):
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
    systematic, reserve, _ = _allocation_totals(ctx)
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
                "systematic total differs from the sum of its allocation rows",
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


def check_capital_as_of(ctx: Context) -> Iterator[Finding]:
    """B4/B13: current capital must be dated at the declared cutoff."""

    cutoff = ctx.cutoff
    stale_allocations = [
        (position, row)
        for position, row in enumerate(rows(ctx.report, "allocation_history"))
        if (to_date(row.get("as_of_date")) or cutoff) < cutoff
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


def cutoff_capital_by_strategy(ctx: Context) -> dict[str, tuple[Decimal, list[str]]]:
    values = {
        str(row.get("account_id")): to_decimal(row.get("latest_value_usd"))
        for row in rows(ctx.report, "account_summary")
    }
    totals: dict[str, tuple[Decimal, list[str]]] = {}
    for strategy, accounts in current_accounts_by_strategy(ctx).items():
        amount = Decimal(0)
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


def check_account_identity(ctx: Context) -> Iterator[Finding]:
    """Account-row capital arithmetic and terminal values agree across views."""

    summary = {
        str(row.get("account_id")): (position, row)
        for position, row in enumerate(rows(ctx.report, "account_summary"))
    }
    twr_by_key = {epoch_key(row): (position, row) for position, row in twr_rows(ctx)}
    for key, (position, row) in sorted(manifest_rows(ctx).items()):
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
        matched = twr_by_key.get(key)
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
    allowed = set(reference.active_accounts) | reserve_accounts(ctx)
    for label, file, path, key in _ACCOUNT_VIEWS:
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

    manifest = manifest_rows(ctx)
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
        matched = manifest.get(key)
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
    pre-M3 generation) keeps the original comparison with ``net_capital``.
    """

    manifest = manifest_rows(ctx)
    for position, row in twr_rows(ctx):
        key = epoch_key(row)
        matched = manifest.get(key)
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
            if contributed is not None and performance_opening is not None:
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
                        "MWR cash flows do not sum to the account's net capital with "
                        "the performance opening value in place of the contributed "
                        "opening capital",
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

    manifest = manifest_rows(ctx)
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
        matched = manifest.get(epoch_key(row))
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

    manifest = manifest_rows(ctx)
    current = current_accounts_by_strategy(ctx)
    for position, row in twr_rows(ctx):
        if row.get("effective_end") is not None:
            continue
        if len(current.get(str(row.get("strategy_id")), [])) != 1:
            continue
        matched = manifest.get(epoch_key(row))
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


def check_allocation_weights(ctx: Context) -> Iterator[Finding]:
    """B5: one current systematic weight per strategy across every view."""

    names = strategy_names(ctx)
    by_name = {name: strategy for strategy, name in names.items()}
    sets: dict[str, dict[str, Decimal]] = {}

    def put(view: str, strategy: str, value: Any) -> None:
        amount = to_decimal(value)
        if amount is not None:
            sets.setdefault(strategy, {})[view] = amount

    for row in rows(ctx.report, "allocation_history"):
        if row.get("scope") == "SYSTEMATIC":
            put(
                "report.allocation_history.systematic_weight",
                str(row.get("strategy_id")),
                row.get("systematic_weight"),
            )
    for row in rows(ctx.report, "strategy_summary"):
        put(
            "report.strategy_summary.systematic_weight",
            str(row.get("strategy_id")),
            row.get("systematic_weight"),
        )
    for mode in ("LIVE_ONLY", "LIVE_PLUS_BACKTEST"):
        for block in ("correlation_one", "central_case"):
            components = node(
                ctx.report,
                "dependence_analysis",
                "mode_summaries",
                mode,
                block,
                "components",
            )
            for item in components if isinstance(components, list) else []:
                if isinstance(item, dict):
                    strategy = by_name.get(str(item.get("series_id")))
                    if strategy is not None:
                        put(
                            f"report.dependence_analysis.{mode}.{block}.allocation_weight",
                            strategy,
                            item.get("allocation_weight"),
                        )
    totals = cutoff_capital_by_strategy(ctx)
    grand = sum(amount for amount, _ in totals.values())
    if grand:
        for strategy, (amount, _) in totals.items():
            sets.setdefault(strategy, {})["computed:cutoff_account_value_share"] = (
                amount / grand
            )
    for strategy in sorted(sets):
        views = sets[strategy]
        if len(views) < 2 or spread(list(views.values())) <= Decimal("1e-8"):
            continue
        defects = ["B5"]
        if "computed:cutoff_account_value_share" in views:
            defects.append("B4")
        yield _finding(
            "returns.allocation_weight_sets",
            BLOCKER_INVARIANT,
            strategy,
            views,
            "one current systematic weight per strategy",
            REPORT,
            "$.strategy_summary[*].systematic_weight",
            defects,
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
    for (account, _, _), (_, row) in manifest_rows(ctx).items():
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
            re.search(r"(?i)balances?\s+(?:remain|are|is)\s+unavailable", note)
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


def check_freshness(ctx: Context) -> Iterator[Finding]:
    """B12/D7: freshness states must be judged against the declared cutoff."""

    cutoff = ctx.cutoff
    policies = _policies(ctx)
    reference = universe(ctx)
    active_strategies = reference.active_strategies if reference else None
    for position, row in enumerate(rows(ctx.gen.freshness, "assessments")):
        last = to_date(row.get("last_accepted_date"))
        state = row.get("state")
        policy = policies.get(str(row.get("policy_id")), {})
        if last is None:
            continue
        entity = (
            f"{row.get('provider_id')}:{row.get('subject_id')}:"
            f"{row.get('lane')}:{row.get('fact_type')}"
        )
        subject_is_retired = (
            row.get("subject_type") == "STRATEGY"
            and active_strategies is not None
            and str(row.get("subject_id")) not in active_strategies
        )
        if row.get("required_for_current") is True and (
            subject_is_retired or (policy.get("mode") == "FINITE" and last < cutoff)
        ):
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
        if policy.get("mode") == "FINITE":
            continue
        if last >= cutoff and state in ("STALE", "LATE"):
            yield _finding(
                "dates.freshness_vs_cutoff",
                BLOCKER_INVARIANT,
                entity,
                {"state": state, "last_accepted_date": row.get("last_accepted_date")},
                {"state": "FRESH", "declared_cutoff": cutoff},
                FRESHNESS,
                f"$.assessments[{position}].state",
                ("B12",),
                f"{state} although data is accepted through the declared cutoff (state "
                "is computed against the evaluation date, not the cutoff)",
            )
        stale_after = policy.get("stale_after_days")
        if (
            isinstance(stale_after, int)
            and state == "FRESH"
            and _age(policy, last, cutoff) > stale_after
        ):
            yield _finding(
                "dates.freshness_vs_cutoff",
                BLOCKER_AUTHORITY,
                entity,
                {"state": state, "last_accepted_date": row.get("last_accepted_date")},
                {"state": "STALE", "stale_after_days": stale_after},
                FRESHNESS,
                f"$.assessments[{position}].state",
                ("D7",),
                "stale source counted as current",
            )
    for position, row in enumerate(
        rows(node(ctx.report, "hwm_summary"), "uncovered_details")
    ):
        last = to_date(row.get("last_accepted_date"))
        if (
            last is not None
            and last >= cutoff
            and row.get("freshness_state") in ("STALE", "LATE")
        ):
            yield _finding(
                "dates.freshness_vs_cutoff",
                BLOCKER_INVARIANT,
                f"hwm:{row.get('strategy_id')}",
                {
                    "freshness_state": row.get("freshness_state"),
                    "last_accepted_date": row.get("last_accepted_date"),
                },
                {"freshness_state": "FRESH", "declared_cutoff": cutoff},
                REPORT,
                f"$.hwm_summary.uncovered_details[{position}].freshness_state",
                ("B12",),
                "HWM sleeve excluded as stale although accepted through the cutoff",
            )
    for position, row in enumerate(rows(ctx.report, "strategy_summary")):
        end = to_date(row.get("hwm_freshness_observed_end"))
        state = row.get("hwm_freshness_state")
        if end is not None and end >= cutoff and state in ("STALE", "LATE"):
            yield _finding(
                "dates.freshness_vs_cutoff",
                BLOCKER_INVARIANT,
                f"strategy:{row.get('strategy_id')}",
                {
                    "hwm_freshness_state": state,
                    "hwm_freshness_observed_end": row.get("hwm_freshness_observed_end"),
                },
                {"hwm_freshness_state": "FRESH", "declared_cutoff": cutoff},
                REPORT,
                f"$.strategy_summary[{position}].hwm_freshness_state",
                ("B12",),
                f"strategy freshness shown {state} although observed through the cutoff",
            )


def _dated_sections(
    ctx: Context,
) -> Iterator[tuple[str, str, str, Any, Any, tuple[str, ...]]]:
    """(entity, file, path, date, status, defects) for current-claim section dates."""

    comparisons = node(ctx.report, "strategy_attribution", "benchmark_comparisons")
    for position, row in enumerate(
        comparisons if isinstance(comparisons, list) else []
    ):
        if isinstance(row, dict) and row.get("observed_end") is not None:
            yield (
                f"benchmark:{row.get('strategy_id')}",
                REPORT,
                f"$.strategy_attribution.benchmark_comparisons[{position}].observed_end",
                row.get("observed_end"),
                row.get("status"),
                ("C2",),
            )
    lenses = node(ctx.report, "decomposition_lenses", "lenses")
    for position, row in enumerate(lenses if isinstance(lenses, list) else []):
        if isinstance(row, dict):
            yield (
                f"decomposition:{row.get('lens_id')}:{row.get('evidence_mode')}",
                REPORT,
                f"$.decomposition_lenses.lenses[{position}].as_of_date",
                row.get("as_of_date"),
                "AVAILABLE" if (row.get("available_count") or 0) > 0 else "UNAVAILABLE",
                ("C3",),
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
        )
    for key in (
        "implementation_reconciliation_contract",
        "implementation_return_attribution_contract",
    ):
        value = node(ctx.report, key, "as_of_date")
        if value is not None:
            yield (key, REPORT, f"$.{key}.as_of_date", value, "AVAILABLE", ("C4",))
    for position, row in enumerate(rows(ctx.report, "strategy_summary")):
        if row.get("live_boundary_observed_end") is not None:
            yield (
                f"live_boundary:{row.get('strategy_id')}",
                REPORT,
                f"$.strategy_summary[{position}].live_boundary_observed_end",
                row.get("live_boundary_observed_end"),
                row.get("live_boundary_status"),
                ("C5",),
            )


def check_section_dates(ctx: Context) -> Iterator[Finding]:
    """C2-C5: a section's as-of must be the cutoff, or be labelled as not current."""

    cutoff = ctx.cutoff
    for entity, file, path, value, status, defects in _dated_sections(ctx):
        day = to_date(value)
        if day is None or day == cutoff:
            continue
        after = day > cutoff
        bound = (
            verified_value_basis_bound(
                ctx, "decomposition_lenses", entity.removeprefix("decomposition:"), day
            )
            if not after
            and is_available(status)
            and entity.startswith("decomposition:")
            else None
        )
        if bound is not None:
            klass = NOT_READY
            message = _bound_message(bound, "section")
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
    """C6: the current week is the cutoff's week, partial, visible and labelled."""

    weekly = node(ctx.panel, "weekly_returns")
    current = node(weekly, "current_week")
    if not isinstance(current, Mapping):
        return
    cutoff_week = iso_week_start(ctx.cutoff)
    week_start = to_date(current.get("week_start"))
    if week_start != cutoff_week:
        yield _finding(
            "dates.weekly_current_week",
            BLOCKER_AUTHORITY,
            "weekly_returns.current_week",
            current.get("week_start"),
            cutoff_week,
            PANEL,
            "$.weekly_returns.current_week.week_start",
            ("C6",),
            "current week is anchored to the evaluation date, not the declared cutoff",
        )
    cutoff_week_partial = ctx.cutoff.weekday() < 6
    if cutoff_week_partial and (
        current.get("is_partial") is not True
        or current.get("default_visible") is not True
    ):
        yield _finding(
            "dates.weekly_current_week",
            BLOCKER_AUTHORITY,
            "weekly_returns.current_week",
            {
                "is_partial": current.get("is_partial"),
                "default_visible": current.get("default_visible"),
            },
            {"is_partial": True, "default_visible": True},
            PANEL,
            "$.weekly_returns.current_week",
            ("C6",),
            "the partial cutoff week must be shown by default and marked partial (#417)",
        )


# ------------------------------------------------------------- completeness (E*)


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
    if isinstance(modes, Mapping):
        incomplete = {}
        for mode, lenses in sorted(modes.items()):
            for lens in lenses if isinstance(lenses, list) else []:
                if isinstance(lens, dict) and (lens.get("unavailable_count") or 0) > 0:
                    incomplete[f"{mode}:{lens.get('lens_id')}"] = lens.get(
                        "unavailable_count"
                    )
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
    disclosures = rows(ctx.report, "leverage_disclosures")
    has_ratio = any("leverage_ratio" in row for row in disclosures)
    unavailable_financing = sum(
        1
        for row in disclosures
        if row.get("data_class") == "FINANCING_COST"
        and row.get("coverage_status") == "UNAVAILABLE"
    )
    if disclosures and not has_ratio:
        yield _finding(
            "completeness.required_outputs",
            NOT_READY,
            "leverage_disclosures",
            {
                "rows": len(disclosures),
                "leverage_ratio_fields": 0,
                "unavailable_financing_cost_rows": unavailable_financing,
            },
            "a leverage ratio per levered account",
            REPORT,
            "$.leverage_disclosures",
            ("E8",),
            "leverage ratio absent; financing cost unmapped",
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
            short[f"twr_mwr.mwr_xirr:{'|'.join(epoch_key(row))}"] = days
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
                live[f"twr_mwr.{key}:{'|'.join(epoch_key(row))}"] = amount
    for key, (_, row) in manifest_rows(ctx).items():
        amount = to_decimal(row.get("observation_linked_return"))
        if amount is not None and amount > 5:
            live[f"account_manifest.observation_linked_return:{'|'.join(key)}"] = amount
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
    for (_, strategy, _), (_, row) in manifest_rows(ctx).items():
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
    for (_, strategy, _), (_, row) in manifest_rows(ctx).items():
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
    cells = (
        len(weeks) * len(series)
        if isinstance(weeks, list) and isinstance(series, list)
        else 0
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
            },
            "lazy or bounded weekly render",
            PANEL,
            "$.weekly_returns",
            ("F4",),
            "eager weekly render over a very large grid",
        )


# ----------------------------------------------------------------- expected values

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
    if metric_id.startswith(("twr.", "mwr.")):
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
        for (account, _, _), (position, row) in manifest_rows(ctx).items():
            if account == entity and row.get("effective_end") is None:
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
            if row.get("strategy_id") == entity:
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
            if row.get("scope") == "RESERVE":
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
        # whatever part of its key it shares with a confirmed blocked row.
        blocked = blocked_epochs(ctx)
        epochs = [
            (position, row)
            for position, row in _epoch_views(ctx, entity)
            if not blocked.skips_report_row(row)
        ]
        manifest = manifest_rows(ctx)
        for position, row in epochs:
            start, end = (
                to_date(row.get("observed_start")),
                to_date(row.get("observed_end")),
            )
            matched = manifest.get(epoch_key(row))
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
                    for key, (_, row) in manifest.items()
                    if key[0] == entity and not blocked.skips_manifest_row(row)
                ]
                total = (
                    sum(part for part in parts if part is not None)
                    if parts and all(part is not None for part in parts)
                    else None
                )
            else:
                traces = [
                    row.get("capital_flow_trace")
                    for key, (_, row) in manifest.items()
                    if key[0] == entity and not blocked.skips_manifest_row(row)
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
    epochs = sorted(
        (
            (position, row)
            for position, row in twr_rows(ctx)
            if str(row.get("strategy_id" if strategy_level else "account_id")) == entity
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
        for key, (_, row) in manifest_rows(ctx).items():
            if key[0] != entity:
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
    for row in ctx.expected_not_derivable:
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
            yield _finding(
                f"expected.{metric_id}",
                BLOCKER_REGRESSION,
                label,
                {"view": view.view, "as_of": view.as_of},
                {"as_of": row.get("as_of")},
                view.file,
                view.json_path,
                defects,
                "value matches but is dated differently",
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
    (BOUNDS_CHECK, check_value_basis_bound),
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
