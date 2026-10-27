"""Independent, stdlib-only acceptance metric derivation (derivation b)."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import types
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, DecimalException, ROUND_HALF_UP, getcontext
from pathlib import Path, PurePath
from typing import Callable, Iterable, cast


getcontext().prec = 50

# A by-path load (the tests' harness) does not register this module in
# sys.modules before executing it, and the dataclasses in the R2 section below
# resolve their string annotations through sys.modules[cls.__module__]; a
# stand-in keeps that resolution working (no annotation is a ClassVar/InitVar).
sys.modules.setdefault(__name__, types.ModuleType(__name__))

CUTOFF = date(2026, 9, 17)
LEDGER_PATH = Path(
    "dataPortfolio/provider-ledger/generations/"
    "3b1445eee410890d1e0d9259ad7543011959208f6091e059eb4daa7c1c8c5a1f/"
    "provider-ledger.json"
)
CROSSWALK_PATH = Path(
    "dataPortfolio/reporting/generations/"
    "d14355537c5e6eeaeb8716c4fe58f4bea2c2b11e3b19a14824359389727509ae/"
    "input/account-strategy-current-epoch-crosswalk.json"
)
QUANTBASE_PRICE_HISTORY_PATH = Path(
    "dataQuantbase/source-runs/refresh-20260902/csv_full/"
    "dashboard_Volatis_Strategy_pricehistory.csv"
)
QUANTBASE_ACTIVITY_PATH = Path(
    "dataQuantbase/source-runs/refresh-20260902/csv_full/account_activity.csv"
)
MATRIXPORT_NAV_PATH = Path(
    "dataMatrixport/output/20260903T051029.331Z/csv/nav_daily.csv"
)
MATRIXPORT_DEPOSITS_PATH = Path(
    "dataMatrixport/output/20260903T051029.331Z/csv/wallet_deposits.csv"
)
MATRIXPORT_BILLS_PATH = Path(
    "dataMatrixport/output/20260903T051029.331Z/csv/wallet_bills.csv"
)
FX_PATH = Path(
    "dataPortfolio/source-runs/portfolio-fx-yahoo-candidate-20260906T234500Z/"
    "raw/yfinance-history.json"
)
FX_LAST_ADMITTED_DATE = date(2026, 9, 3)
MATRIXPORT_FX_PROVENANCE = (
    "fx_provenance: USDT->USD daily Close from the resident raw input of "
    "contract-bound FX generation 9812f253 through 2026-09-03."
)
EXTERNAL_FLOW_TYPES = {
    "EXTERNAL_CONTRIBUTION",
    "EXTERNAL_WITHDRAWAL",
    "EXTERNAL_PROVIDER_TRANSFER",
    "DEPOSIT_OR_WITHDRAWAL",
}
INPUT_MANIFEST_SCHEMA = "qe-cp-acceptance-inputs/2"
INPUT_MANIFEST_SCHEMA_V3 = "qe-cp-acceptance-inputs/3"
INPUT_ROLE_BASENAMES = {
    "ledger": "provider-ledger.json",
    "crosswalk": "account-strategy-current-epoch-crosswalk.json",
    "quantbase_price_history": "dashboard_Volatis_Strategy_pricehistory.csv",
    "quantbase_activity": "account_activity.csv",
    "matrixport_nav": "nav_daily.csv",
    "matrixport_deposits": "wallet_deposits.csv",
    "matrixport_bills": "wallet_bills.csv",
    "fx": "yfinance-history.json",
}
# A role whose file may be renamed on export is also resolved by its manifest
# family when the basename is absent; both present and disagreeing is ambiguous.
INPUT_ROLE_FAMILY_FALLBACK = {
    "crosswalk": "account_strategy_map_and_active_universe",
}
# The declared account-strategy manifest (pipeline-authored by design) carries
# the declared cutoff and, for closed funded epochs, the funded_end evidence
# (performance_closed_on / post_liquidation_income).  It is optional so older
# manifests stay readable, but when present it is bound to the crosswalk rows.
ACCOUNT_MANIFEST_FAMILY = "declared_cutoff_date"
V3_AUTHORITY_FAMILIES = frozenset(
    {
        "financial_source_contract",
        "fx_generation_manifest",
        "fx_usdc_usd_daily",
        "fx_usdt_usd_daily",
        "fx_daily_rates",
        "provider_capital_policy",
        "provider_capital_owner_receipt_a",
        "provider_capital_owner_receipt_b",
        "provider_capital_owner_receipt_b_response",
    }
)
# The approved PROVIDER_CUSTODY_PERIMETER decision shape, mirroring the contract
# validator: the eight base keys, plus the four fund-scoped amendment keys as an
# all-or-none set (the 2026-10-16/17 owner amendments for #536/#568).
POLICY_DECISION_KEYS = frozenset(
    {
        "approval_evidence",
        "calculation_effect",
        "decision_authority",
        "decision_status",
        "examples",
        "plain_language",
        "recommended_treatment",
        "selected_treatment",
    }
)
FUND_SCOPED_DECISION_KEYS = frozenset(
    {
        "amends_approval_evidence",
        "fund_scoped_effect",
        "fund_scoped_providers",
        "fund_scoped_treatment",
    }
)
# Phrases the owner approval comment must carry to bind the amended fund-scoped
# custody policy body (the #568 Matrixport amendment of the #536 decision).
RECEIPT_A_REQUIRED_PHRASES = (
    "PROVIDER_CUSTODY_PERIMETER",
    "product subscription",
    "not the strategy's capital",
    "no derived cash line",
)
NATIVE_MATRIXPORT_FAMILIES = frozenset(
    {
        "matrixport_generation_manifest",
        "matrixport_subscription_details",
        "matrixport_generation_catalog",
        "matrixport_owner_flow_coverage",
        "matrixport_owner_flow_request_manifest",
    }
)
# An optional, separately sha-pinned second (base) capture set for matrixport.
# These two families are admitted together (both or neither), each exactly one
# entry; when present their trade_record request windows are chained with the
# selected generation's under custody_coverage_end.  They are NOT part of the
# required NATIVE_MATRIXPORT_FAMILIES set and are never bound to the selected
# generation's artifact pins: each base file is verified fail-closed by its own
# declared sha256/bytes pin in the inputs manifest.
NATIVE_MATRIXPORT_BASE_FAMILIES = frozenset(
    {
        "matrixport_owner_flow_base_coverage",
        "matrixport_owner_flow_base_request_manifest",
    }
)


class ManifestError(ValueError):
    """An inputs manifest or a declared input fails closed verification."""


def _sleeve_engine() -> Engine:
    """Bind the sleeve derivation to this module's existing return engine."""

    def not_derivable_row(
        metric_id: str, entity: str, reason: str, window: str | None = None
    ) -> dict[str, object]:
        row: dict[str, object] = {
            "metric_id": metric_id,
            "entity": entity,
            "reason": reason,
        }
        if window is not None:
            row["window"] = window
        return row

    return Engine(
        time_weighted_return=time_weighted_return,
        xirr=xirr,
        money=money,
        ratio=ratio,
        metric=metric,
        not_derivable_row=not_derivable_row,
    )


def _derive_sleeve_block(qe_root: Path, sleeve_inputs: Path) -> dict[str, object]:
    """Run the SPEC-HARNESS sleeve derivation (the R2 section below)."""
    return derive_sleeves(
        sleeve_inputs_path=sleeve_inputs,
        qe_root=qe_root,
        repo_root=Path(__file__).resolve().parents[2],
        cutoff=CUTOFF,
        engine=_sleeve_engine(),
    )


def default_input_paths() -> dict[str, Path]:
    """Return the v1 input selection used when no inputs manifest is given."""
    return {
        "ledger": LEDGER_PATH,
        "crosswalk": CROSSWALK_PATH,
        "quantbase_price_history": QUANTBASE_PRICE_HISTORY_PATH,
        "quantbase_activity": QUANTBASE_ACTIVITY_PATH,
        "matrixport_nav": MATRIXPORT_NAV_PATH,
        "matrixport_deposits": MATRIXPORT_DEPOSITS_PATH,
        "matrixport_bills": MATRIXPORT_BILLS_PATH,
        "fx": FX_PATH,
    }


def load_inputs_manifest(path: Path) -> dict[str, object]:
    """Read an acceptance-inputs manifest, failing closed on any contract break."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"inputs manifest {path} is unreadable: {error}") from error
    if not isinstance(manifest, dict):
        raise ManifestError(f"inputs manifest {path} is not a JSON object")
    schema = manifest.get("schema")
    if schema not in {INPUT_MANIFEST_SCHEMA, INPUT_MANIFEST_SCHEMA_V3}:
        raise ManifestError(
            f"inputs manifest {path} has an unsupported schema: {schema!r}; "
            f"expected {INPUT_MANIFEST_SCHEMA!r} or {INPUT_MANIFEST_SCHEMA_V3!r}"
        )
    cutoff = manifest.get("cutoff")
    if cutoff != CUTOFF.isoformat():
        raise ManifestError(
            f"inputs manifest cutoff {cutoff!r} differs from the fixed "
            f"{CUTOFF.isoformat()} cutoff"
        )
    entries = manifest.get("inputs")
    if not isinstance(entries, list) or not entries:
        raise ManifestError(f"inputs manifest {path} declares no inputs")
    if schema == INPUT_MANIFEST_SCHEMA_V3:
        family_entries: dict[str, list[dict[str, object]]] = defaultdict(list)
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("family"), str):
                family_entries[entry["family"]].append(entry)
        missing = sorted(V3_AUTHORITY_FAMILIES - family_entries.keys())
        repeated = sorted(
            family
            for family in V3_AUTHORITY_FAMILIES
            if len(family_entries.get(family, [])) != 1
        )
        if missing or repeated:
            raise ManifestError(
                "schema-3 inputs manifest lacks exactly one of the required "
                f"v3 custody authority families (missing={missing}, repeated={repeated})"
            )
        for family in sorted(V3_AUTHORITY_FAMILIES):
            entry = family_entries[family][0]
            sha256 = entry.get("sha256")
            bytes_count = entry.get("bytes")
            if (
                not isinstance(sha256, str)
                or len(sha256) != 64
                or any(character not in "0123456789abcdef" for character in sha256)
                or not isinstance(bytes_count, int)
                or isinstance(bytes_count, bool)
                or bytes_count < 0
            ):
                raise ManifestError(
                    f"schema-3 custody authority entry {family!r} lacks valid sha256/bytes pins"
                )
    return manifest


def resolve_input_path(qe_root: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    return candidate if candidate.is_absolute() else qe_root / candidate


def output_input_path(qe_root: Path, path: Path) -> str:
    """Emit a machine-independent provenance path string.

    A relative path is emitted as declared.  A manifest-declared absolute
    path is relativized against qe_root so the emitted derivation stays
    byte-reproducible across machines.
    """
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(qe_root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def manifest_input_paths(manifest: dict[str, object], qe_root: Path) -> dict[str, Path]:
    """Select declared inputs by basename and verify every pinned hash fail-closed.

    Every manifest entry that declares sha256 or bytes is verified even when this
    derivation does not consume the file. Relative paths resolve under qe_root.
    """
    entries = manifest["inputs"]
    if not isinstance(entries, list):
        raise ManifestError("inputs manifest inputs must be a list")
    paths_by_basename: dict[str, set[str]] = defaultdict(set)
    paths_by_family: dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        if not isinstance(entry, dict):
            raise ManifestError("inputs manifest entry is not a JSON object")
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ManifestError("inputs manifest entry lacks a path")
        paths_by_basename[Path(raw_path).name].add(raw_path)
        family = entry.get("family")
        if isinstance(family, str):
            paths_by_family[family].add(raw_path)
    resolved: dict[str, Path] = {}
    for role, basename in INPUT_ROLE_BASENAMES.items():
        candidates = paths_by_basename.get(basename, set())
        family = INPUT_ROLE_FAMILY_FALLBACK.get(role)
        family_candidates = paths_by_family.get(family, set()) if family else set()
        if not candidates and family_candidates:
            candidates = family_candidates
        elif candidates and family_candidates and candidates != family_candidates:
            raise ManifestError(
                f"inputs manifest declares conflicting paths for role {role}: "
                f"basename {sorted(candidates)} vs family {sorted(family_candidates)}"
            )
        if len(candidates) != 1:
            raise ManifestError(
                f"inputs manifest must declare exactly one distinct {basename} "
                f"path for role {role}; found {len(candidates)}"
            )
        resolved[role] = Path(next(iter(candidates)))
    account_manifest_candidates = paths_by_family.get(ACCOUNT_MANIFEST_FAMILY, set())
    if len(account_manifest_candidates) > 1:
        raise ManifestError(
            f"inputs manifest must declare at most one {ACCOUNT_MANIFEST_FAMILY} "
            f"entry; found {len(account_manifest_candidates)}"
        )
    if account_manifest_candidates:
        resolved["account_manifest"] = Path(next(iter(account_manifest_candidates)))
    for entry in entries:
        raw_path = str(entry["path"])
        expected_sha256 = entry.get("sha256")
        expected_bytes = entry.get("bytes")
        if expected_sha256 is None and expected_bytes is None:
            continue
        absolute = resolve_input_path(qe_root, raw_path)
        if not absolute.is_file():
            raise ManifestError(f"declared input is missing: {raw_path}")
        if expected_bytes is not None and absolute.stat().st_size != expected_bytes:
            raise ManifestError(
                f"declared input byte size mismatch: {raw_path} "
                f"(expected {expected_bytes}, found {absolute.stat().st_size})"
            )
        if expected_sha256 is not None:
            actual_sha256 = file_sha256(absolute)
            if actual_sha256 != expected_sha256:
                raise ManifestError(
                    f"declared input sha256 mismatch: {raw_path} "
                    f"(expected {expected_sha256}, found {actual_sha256})"
                )
    return resolved


def native_matrixport_input_paths(
    manifest: dict[str, object], qe_root: Path
) -> dict[str, Path]:
    """Resolve the five native Matrixport interface families as one set."""
    entries = manifest.get("inputs")
    if not isinstance(entries, list):
        raise ManifestError("inputs manifest inputs must be a list")
    family_entries: dict[str, list[dict[str, object]]] = defaultdict(list)
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("family"), str):
            family_entries[entry["family"]].append(entry)
    present = NATIVE_MATRIXPORT_FAMILIES.intersection(family_entries)
    if present and present != NATIVE_MATRIXPORT_FAMILIES:
        raise ManifestError(
            "native Matrixport interface families must be admitted together"
        )
    if not present:
        return {}
    result: dict[str, Path] = {}
    for family in sorted(NATIVE_MATRIXPORT_FAMILIES):
        selected = family_entries[family]
        if len(selected) != 1:
            raise ManifestError(
                f"native Matrixport family {family!r} must have exactly one entry"
            )
        raw_path = selected[0].get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ManifestError(f"native Matrixport family {family!r} lacks a path")
        result[family] = resolve_input_path(qe_root, raw_path)
    # The optional base-run capture set is admitted together: both families or
    # neither, each exactly one entry, each verified by its own declared pin.
    base_present = NATIVE_MATRIXPORT_BASE_FAMILIES.intersection(family_entries)
    if base_present and base_present != NATIVE_MATRIXPORT_BASE_FAMILIES:
        raise ManifestError(
            "native Matrixport base-run capture families must be admitted together"
        )
    if base_present:
        for family in sorted(NATIVE_MATRIXPORT_BASE_FAMILIES):
            selected = family_entries[family]
            if len(selected) != 1:
                raise ManifestError(
                    f"native Matrixport base family {family!r} must have exactly one entry"
                )
            raw_path = selected[0].get("path")
            if not isinstance(raw_path, str) or not raw_path:
                raise ManifestError(
                    f"native Matrixport base family {family!r} lacks a path"
                )
            result[family] = resolve_input_path(qe_root, raw_path)
    return result


class FlowData:
    """Cash-flow dates plus their distinct TWR boundary treatment."""

    def __init__(
        self,
        cash_flows: dict[date, Decimal],
        twr_end_flows: dict[date, Decimal],
        twr_beginning_flows: dict[date, Decimal],
        count: int,
    ) -> None:
        self.cash_flows = cash_flows
        self.twr_end_flows = twr_end_flows
        self.twr_beginning_flows = twr_beginning_flows
        self.count = count


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def ratio(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_UP))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def code_commit() -> str:
    """Read this checkout's commit without invoking repository tooling.

    A distribution copy of the sources may carry no .git metadata at all; the
    derivation must still run, so the provenance field then carries an explicit
    sentinel instead of raising.
    """
    try:
        return _read_code_commit()
    except (OSError, ValueError):
        return "unavailable:no-git-metadata"


def _read_code_commit() -> str:
    dot_git = Path(__file__).resolve().parents[2] / ".git"
    git_dir = dot_git
    if dot_git.is_file():
        pointer = dot_git.read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir: "):
            raise ValueError("unreadable .git directory pointer")
        git_dir = Path(pointer.removeprefix("gitdir: "))
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if head.startswith("ref: "):
        ref = head.removeprefix("ref: ")
        ref_path = git_dir / ref
        if not ref_path.is_file():
            common_dir = (
                git_dir / (git_dir / "commondir").read_text(encoding="utf-8").strip()
            ).resolve()
            ref_path = common_dir / ref
        head = ref_path.read_text(encoding="utf-8").strip()
    if len(head) != 40 or any(
        character not in "0123456789abcdef" for character in head
    ):
        raise ValueError("checkout HEAD is not a full lowercase SHA-1")
    return head


def has_reserve_or_bucket_identifier(value: object) -> bool:
    """Inspect selected ledger values for a reserve entity marker."""
    if isinstance(value, dict):
        return any(
            "reserve" in str(key).lower()
            or "bucket" in str(key).lower()
            or has_reserve_or_bucket_identifier(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(has_reserve_or_bucket_identifier(item) for item in value)
    return isinstance(value, str) and (
        "reserve" in value.lower() or "bucket" in value.lower()
    )


def time_weighted_return(
    observations: Iterable[tuple[date, Decimal]],
    external_flows: dict[date, Decimal],
    beginning_external_flows: dict[date, Decimal] | None = None,
) -> Decimal:
    """Calculate non-annualized TWR using the date-only end-of-day convention."""
    points = sorted(observations)
    if len(points) < 2:
        raise ValueError("at least two observations are required for TWR")
    beginning_external_flows = beginning_external_flows or {}
    observation_dates = {observed_on for observed_on, _ in points}
    missing_dates = set(external_flows).difference(observation_dates)
    missing_beginning_dates = set(beginning_external_flows).difference(
        observation_dates
    )
    if missing_dates or missing_beginning_dates:
        raise ValueError("an external flow has no same-date valuation")
    result = Decimal("1")
    prior = points[0][1]
    for index, (observed_on, ending_value) in enumerate(points[1:], start=1):
        beginning_flow = beginning_external_flows.get(
            points[index - 1][0], Decimal("0")
        )
        ending_flow = external_flows.get(observed_on, Decimal("0"))
        beginning_value = prior + beginning_flow
        ending_value_excluding_eod_flow = ending_value - ending_flow
        if beginning_value == 0:
            # A flat zero interval contains no capital or return.  A later
            # non-zero valuation is valid only when its same-day flow funded
            # the opening value; its return contribution is therefore neutral.
            if ending_value_excluding_eod_flow == 0:
                prior = ending_value
                continue
            raise ValueError("zero beginning valuation without same-day funding")
        result *= ending_value_excluding_eod_flow / beginning_value
        prior = ending_value
    return result - Decimal("1")


def xirr(cash_flows: Iterable[tuple[date, Decimal]]) -> Decimal | None:
    """Return a unique ACTUAL/365 annualized XIRR, or None when uniqueness is unproved."""
    by_date: dict[date, Decimal] = defaultdict(Decimal)
    for occurred_on, amount in cash_flows:
        by_date[occurred_on] += amount
    series = [
        (occurred_on, amount)
        for occurred_on, amount in sorted(by_date.items())
        if amount
    ]
    if len(series) < 2:
        return None

    def sign_changes(values: Iterable[Decimal]) -> int:
        signs = [value > 0 for value in values if value]
        return sum(left != right for left, right in zip(signs, signs[1:]))

    partial_sums: list[Decimal] = []
    cumulative = Decimal("0")
    for _, amount in series:
        cumulative += amount
        partial_sums.append(cumulative)
    reverse_partial_sums: list[Decimal] = []
    cumulative = Decimal("0")
    for _, amount in reversed(series):
        cumulative += amount
        reverse_partial_sums.append(cumulative)
    # Laguerre/Norstrom: a single sign change in the applicable partial sums
    # proves at most one root on that side of zero.  We still require a bracket
    # below, which proves existence.
    positive_root_proved = sign_changes(partial_sums) <= 1
    negative_root_proved = sign_changes(reverse_partial_sums) <= 1
    if not positive_root_proved and not negative_root_proved:
        return None
    origin = series[0][0]

    def npv(rate: Decimal) -> Decimal:
        base = Decimal("1") + rate
        return sum(
            amount / (base ** (Decimal((occurred_on - origin).days) / Decimal("365")))
            for occurred_on, amount in series
        )

    low = Decimal("-0.9999999999")
    high = Decimal("1")
    try:
        low_value = npv(low)
        high_value = npv(high)
        while low_value * high_value > 0 and high < Decimal("1000000"):
            high *= 2
            high_value = npv(high)
    except DecimalException:
        return None
    if low_value == 0:
        return low if negative_root_proved else None
    if high_value == 0:
        return high if positive_root_proved else None
    if low_value * high_value > 0:
        return None
    for _ in range(250):
        midpoint = (low + high) / 2
        midpoint_value = npv(midpoint)
        if midpoint_value == 0:
            if midpoint >= 0 and positive_root_proved:
                return midpoint
            if midpoint < 0 and negative_root_proved:
                return midpoint
            return None
        if low_value * midpoint_value < 0:
            high = midpoint
        else:
            low = midpoint
            low_value = midpoint_value
    result = (low + high) / 2
    if result >= 0 and positive_root_proved:
        return result
    if result < 0 and negative_root_proved:
        return result
    return None


def aggregate_capital(
    account_values: dict[str, Decimal],
    account_strategies: dict[str, str],
    excluded_accounts: set[str],
    reserve_value: Decimal,
) -> dict[str, object]:
    strategies: dict[str, Decimal] = defaultdict(Decimal)
    retained_excluded = Decimal("0")
    for account_id, value in account_values.items():
        strategy_id = account_strategies.get(account_id)
        if strategy_id is not None:
            strategies[strategy_id] += value
        elif account_id in excluded_accounts:
            retained_excluded += value
    systematic = sum(strategies.values(), Decimal("0"))
    return {
        "strategies": dict(strategies),
        "retained_excluded": retained_excluded,
        "systematic": systematic,
        "liquid": systematic + reserve_value,
    }


def current_crosswalk(
    rows: list[dict[str, object]],
) -> tuple[dict[str, str], set[str], dict[str, str]]:
    """Return current mappings and explicit failures for unusable crosswalk rows."""
    account_strategies: dict[str, str] = {}
    excluded: set[str] = set()
    problems: dict[str, str] = {}
    in_effect_rows: dict[str, list[dict[str, object]]] = defaultdict(list)
    out_of_effect_included: set[str] = set()
    for row in rows:
        account_id = str(row["account_id"])
        effective_start = parse_date(str(row["effective_start"]))
        effective_end = row.get("effective_end")
        in_effect = effective_start <= CUTOFF and (
            effective_end is None or parse_date(str(effective_end)) >= CUTOFF
        )
        if in_effect:
            in_effect_rows[account_id].append(row)
        elif row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH":
            out_of_effect_included.add(account_id)
    for account_id, account_rows in in_effect_rows.items():
        if len(account_rows) != 1:
            problems[account_id] = "multiple crosswalk rows are in effect at cutoff"
            continue
        row = account_rows[0]
        if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH":
            account_strategies[account_id] = str(row["strategy_id"])
        else:
            excluded.add(account_id)
    for account_id in out_of_effect_included:
        if account_id not in account_strategies:
            problems.setdefault(
                account_id, "included crosswalk row is out of effect at cutoff"
            )
    for account_id in problems:
        account_strategies.pop(account_id, None)
        excluded.discard(account_id)
    return account_strategies, excluded, problems


def active_included_strategy_ids(rows: list[dict[str, object]]) -> list[str]:
    """Return every distinct included strategy that is in effect at the cutoff."""
    strategy_ids: set[str] = set()
    for row in rows:
        if row["product_epoch_disposition"] != "INCLUDED_PRODUCT_EPOCH":
            continue
        effective_start = parse_date(str(row["effective_start"]))
        effective_end = row.get("effective_end")
        if effective_start <= CUTOFF and (
            effective_end is None or parse_date(str(effective_end)) >= CUTOFF
        ):
            strategy_ids.add(str(row["strategy_id"]))
    return sorted(strategy_ids)


def excluded_account_ids(rows: list[dict[str, object]]) -> set[str]:
    """Return every explicitly retained/excluded crosswalk account, even if ended."""
    return {
        str(row["account_id"])
        for row in rows
        if row["product_epoch_disposition"] != "INCLUDED_PRODUCT_EPOCH"
    }


def current_membership_starts(rows: list[dict[str, object]]) -> dict[str, date]:
    """Map each uniquely current included account to its funded interval start."""
    starts: dict[str, date] = {}
    for row in rows:
        if row["product_epoch_disposition"] != "INCLUDED_PRODUCT_EPOCH":
            continue
        start = parse_date(str(row["effective_start"]))
        end = row.get("effective_end")
        if start <= CUTOFF and (end is None or parse_date(str(end)) >= CUTOFF):
            account_id = str(row["account_id"])
            if account_id not in starts:
                starts[account_id] = start
    return starts


def included_epochs_by_account(
    rows: list[dict[str, object]],
) -> dict[str, list[tuple[date, date | None, str]]]:
    """Map each account to every included funded-membership epoch, oldest first.

    The crosswalk carries one row per funded product epoch; accounts reassigned
    between strategies carry their predecessor epochs as further included rows
    (the OWNER_539 TT predecessor ruling), and every included epoch is a funded
    member epoch of its strategy.
    """
    epochs: dict[str, list[tuple[date, date | None, str]]] = defaultdict(list)
    for row in rows:
        if row["product_epoch_disposition"] != "INCLUDED_PRODUCT_EPOCH":
            continue
        start = parse_date(str(row["effective_start"]))
        raw_end = row.get("effective_end")
        epochs[str(row["account_id"])].append(
            (
                start,
                None if raw_end is None else parse_date(str(raw_end)),
                str(row["strategy_id"]),
            )
        )
    return {
        account_id: sorted(
            account_epochs,
            key=lambda epoch: (epoch[0], epoch[1] is None, epoch[1] or date.max),
        )
        for account_id, account_epochs in epochs.items()
    }


def epoch_window_problem(epochs: list[tuple[date, date | None, str]]) -> str | None:
    """Fail closed unless the account's funded epochs tile one continuous interval."""
    for previous, following in zip(epochs, epochs[1:]):
        if previous[1] is None:
            return "an open-ended included epoch is followed by another included epoch"
        if following[0] <= previous[1]:
            return "included epochs of the account overlap"
        if following[0] > previous[1] + timedelta(days=1):
            return "included epochs of the account leave an unfunded gap"
    return None


def strategy_member_epochs(
    rows: list[dict[str, object]],
) -> dict[str, list[tuple[str, date, date | None]]]:
    """Map each strategy to every funded member epoch (account, start, end)."""
    members: dict[str, list[tuple[str, date, date | None]]] = defaultdict(list)
    for row in rows:
        if row["product_epoch_disposition"] != "INCLUDED_PRODUCT_EPOCH":
            continue
        start = parse_date(str(row["effective_start"]))
        if start > CUTOFF:
            continue
        raw_end = row.get("effective_end")
        members[str(row["strategy_id"])].append(
            (
                str(row["account_id"]),
                start,
                None if raw_end is None else parse_date(str(raw_end)),
            )
        )
    return {
        strategy_id: sorted(
            epochs, key=lambda epoch: (epoch[0], epoch[1], epoch[2] or date.max)
        )
        for strategy_id, epochs in members.items()
    }


def _epoch_identity_key(
    account_id: object, strategy_id: object, start: object, end: object
) -> tuple[str, str, str, str | None]:
    return (
        str(account_id),
        str(strategy_id),
        str(start),
        None if end is None else str(end),
    )


def _format_epoch_key(key: tuple[str, str, str, str | None]) -> str:
    return "{}|{}|{}|{}".format(
        key[0], key[1], key[2], key[3] if key[3] is not None else "null"
    )


def _manifest_row_closure(
    row: dict[str, object],
    closures: dict[tuple[str, date], dict[str, object]],
) -> None:
    """Parse one declared manifest row's optional epoch closure evidence."""
    raw_closed = row.get("performance_closed_on")
    if raw_closed is None:
        return
    try:
        closed_on = parse_date(str(raw_closed))
        epoch_start = parse_date(str(row.get("effective_start")))
    except ValueError as error:
        raise ManifestError(
            "declared account-strategy manifest carries an invalid "
            f"performance_closed_on: {error}"
        ) from error
    raw_income = row.get("post_liquidation_income")
    try:
        income = None if raw_income is None else Decimal(str(raw_income))
    except DecimalException as error:
        raise ManifestError(
            "declared account-strategy manifest carries an invalid "
            f"post_liquidation_income: {error}"
        ) from error
    raw_epoch_end = row.get("effective_end")
    epoch_end = None if raw_epoch_end is None else parse_date(str(raw_epoch_end))
    if epoch_end is not None and closed_on > epoch_end:
        raise ManifestError(
            "declared performance_closed_on is after the epoch's effective_end: "
            f"{raw_closed!r}"
        )
    if not epoch_start <= closed_on <= CUTOFF:
        raise ManifestError(
            "declared performance_closed_on is outside the epoch or after "
            f"the cutoff: {raw_closed!r}"
        )
    closure_key = (str(row.get("account_id")), epoch_start)
    if closure_key in closures:
        raise ManifestError(
            "declared account-strategy manifest repeats an epoch closure"
        )
    closures[closure_key] = {
        "funded_end": closed_on,
        "post_liquidation_income": income,
    }


def declared_epoch_closures(
    account_manifest: dict[str, object], crosswalk_rows: list[dict[str, object]]
) -> dict[tuple[str, date], dict[str, object]]:
    """Bind the declared account-strategy manifest to the crosswalk epoch rows.

    The declared manifest is pipeline-authored by design.  It must carry exactly
    the crosswalk's included epoch rows; a row carrying ``performance_closed_on``
    declares that epoch's funded_end (the last funded day), and its declared
    ``post_liquidation_income`` is reconciled against the raw bytes by the
    caller.  Returns ``(account_id, effective_start)`` -> closure evidence.
    """
    rows = account_manifest.get("rows")
    if not isinstance(rows, list):
        raise ManifestError("declared account-strategy manifest lacks rows")

    crosswalk_keys = {
        _epoch_identity_key(
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            row.get("effective_end"),
        )
        for row in crosswalk_rows
        if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH"
    }
    declared_keys: set[tuple[str, str, str, str | None]] = set()
    closures: dict[tuple[str, date], dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ManifestError(
                "declared account-strategy manifest row is not an object"
            )
        declared_keys.add(
            _epoch_identity_key(
                row.get("account_id"),
                row.get("strategy_id"),
                row.get("effective_start"),
                row.get("effective_end"),
            )
        )
        _manifest_row_closure(row, closures)
    if declared_keys != crosswalk_keys:
        raise ManifestError(
            "declared account-strategy manifest rows do not match the crosswalk "
            "included epochs"
        )
    return closures


def effective_perimeter_closures(
    account_manifest: dict[str, object],
    crosswalk_rows: list[dict[str, object]],
    derived_epochs: list[Epoch],
    epoch_parents: dict[str, str],
) -> tuple[
    dict[tuple[str, date], dict[str, object]],
    set[tuple[str, str, str, str | None]],
]:
    """SPEC-COMBINED 1: bind the declared manifest to the effective perimeter.

    The effective perimeter is (crosswalk INCLUDED epochs - epochs the
    manifest dropped) + the derived-sleeve epochs of this derivation (the
    derivation, not the manifest's coverage rows, is the authority for WHICH
    epochs are derived).  The declared manifest must equal it exactly as sets
    of the four-part identity (open end = null); any difference fails closed
    (SleeveInputError, exit status 2) naming the first difference, and neither
    side may be reconciled by tuning.  Returns the rows' closure evidence and
    the dropped baseline epoch keys.
    """
    rows = account_manifest.get("rows")
    if not isinstance(rows, list):
        raise ManifestError("declared account-strategy manifest lacks rows")
    declared_keys: set[tuple[str, str, str, str | None]] = set()
    closures: dict[tuple[str, date], dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ManifestError(
                "declared account-strategy manifest row is not an object"
            )
        declared_keys.add(
            _epoch_identity_key(
                row.get("account_id"),
                row.get("strategy_id"),
                row.get("effective_start"),
                row.get("effective_end"),
            )
        )
        _manifest_row_closure(row, closures)
    baseline_keys = {
        _epoch_identity_key(
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            row.get("effective_end"),
        )
        for row in crosswalk_rows
        if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH"
    }
    # R3: no two derived epochs may claim one account/strategy/start.
    seen_derived: set[tuple[str, str, str]] = set()
    for epoch in derived_epochs:
        claim = (
            epoch.account_id,
            epoch.strategy_id,
            epoch.effective_start.isoformat(),
        )
        if claim in seen_derived:
            raise SleeveInputError(
                "two derived epochs claim one account/strategy/start: "
                f"{claim[0]}|{claim[1]}|{claim[2]}"
            )
        seen_derived.add(claim)
    derived_keys = {
        _epoch_identity_key(
            epoch.account_id,
            epoch.strategy_id,
            epoch.effective_start.isoformat(),
            epoch.effective_end.isoformat()
            if epoch.effective_end is not None
            else None,
        )
        for epoch in derived_epochs
    }
    dropped = baseline_keys - declared_keys
    baseline_strategies = {key[1] for key in baseline_keys}
    # R3: a derived epoch of a strategy the crosswalk includes replaces its
    # parent's epoch for that strategy; the parent's epoch must be dropped.
    for epoch in derived_epochs:
        if epoch.strategy_id not in baseline_strategies:
            continue
        parent = epoch_parents[epoch.account_id]
        kept = sorted(
            key
            for key in baseline_keys
            if key[0] == parent and key[1] == epoch.strategy_id and key not in dropped
        )
        if kept:
            derived_key = _epoch_identity_key(
                epoch.account_id,
                epoch.strategy_id,
                epoch.effective_start.isoformat(),
                epoch.effective_end.isoformat()
                if epoch.effective_end is not None
                else None,
            )
            raise SleeveInputError(
                f"derived epoch {_format_epoch_key(derived_key)} claims strategy "
                f"{epoch.strategy_id}, which the crosswalk includes, while its "
                f"parent {parent} keeps its baseline epoch "
                f"{_format_epoch_key(kept[0])} for that strategy"
            )
    # R2: an epoch may be dropped only under a parent of derived epochs, and
    # a parent that drops one baseline epoch drops all of its baseline epochs.
    parent_accounts = set(epoch_parents.values())
    for key in sorted(dropped):
        if key[0] not in parent_accounts:
            raise SleeveInputError(
                "declared account-strategy manifest drops baseline epoch "
                f"{_format_epoch_key(key)}, but {key[0]} is not a parent of "
                "derived epochs"
            )
        if any(other[0] == key[0] and other not in dropped for other in baseline_keys):
            raise SleeveInputError(
                "declared account-strategy manifest drops some but not all "
                f"baseline epochs of {key[0]}: a residual parent never stays "
                "partly a strategy"
            )
    # R1: the manifest must equal the effective perimeter exactly.
    effective = (baseline_keys - dropped) | derived_keys
    if declared_keys != effective:
        extra = sorted(declared_keys - effective)
        missing = sorted(effective - declared_keys)
        if extra and (not missing or extra[0] <= missing[0]):
            difference = f"declared-only epoch {_format_epoch_key(extra[0])}"
        else:
            difference = f"missing declared epoch {_format_epoch_key(missing[0])}"
        raise SleeveInputError(
            "effective epoch perimeter differs from the declared "
            f"account-strategy manifest: first difference: {difference}"
        )
    return closures, dropped


def post_liquidation_income_from_raw(
    observations: list[tuple[date, Decimal]],
    ledger_flows: list[dict[str, object]],
    account_id: str,
    funded_end: date,
) -> tuple[Decimal, Decimal] | str:
    """Recompute post-liquidation income from raw bytes after a funded_end.

    Returns (value at funded_end, income through the cutoff) or a failure
    reason: income is the residual value change after the last funded day that
    no classified flow explains — labelled post-liquidation income (dated,
    non-TWR, inside gain), never added a second time; the residual NAV it
    accumulates into is really held money and stays cutoff capital.
    """
    at_close = [point for point in observations if point[0] == funded_end]
    if not at_close:
        return "no available USD observation on the declared funded_end"
    later = [point for point in observations if funded_end < point[0] <= CUTOFF]
    if not later:
        return "no available USD observation after the declared funded_end"
    flows, _, flow_problem = classified_external_flows(
        ledger_flows, account_id, funded_end, CUTOFF
    )
    if flow_problem is not None:
        return flow_problem
    if any(amount > 0 for amount in flows.values()):
        return "funding after the declared funded_end requires a new funded epoch"
    income = later[-1][1] - at_close[-1][1] - sum(flows.values(), Decimal("0"))
    return at_close[-1][1], income


UNMAPPED_NAV_REASON = (
    "no provider-ledger NAV observation of this account on or before the cutoff carries "
    "an admitted (MAPPED) strategy mapping, so no admitted source date exists"
)


def accounts_without_admitted_mapping(ledger: dict[str, object]) -> set[str]:
    """Accounts with ledger NAV observations in the cutoff window and none MAPPED.

    V15: only observations dated on or before the cutoff count (a later MAPPED row cannot
    admit an account whose valued window is unmapped); a row that states no
    mapping_status is not admitted either. An account with no observation in the window
    is not judged here.
    """

    statuses: dict[str, list[str]] = defaultdict(list)
    for row in cast(list[dict[str, object]], ledger["nav_observations"]):
        observed_on = row.get("as_of_date")
        try:
            day = parse_date(str(observed_on))
        except ValueError:
            continue
        if day > CUTOFF:
            continue
        status = row.get("mapping_status")
        statuses[str(row["account_id"])].append(
            status if isinstance(status, str) and status else "MISSING"
        )
    return {account for account, found in statuses.items() if "MAPPED" not in found}


def latest_observations(
    ledger: dict[str, object],
) -> tuple[dict[str, list[tuple[date, Decimal]]], set[str]]:
    observations: dict[str, dict[date, Decimal]] = defaultdict(dict)
    conflicts: set[str] = set()
    for row in ledger["nav_observations"]:
        nav = row["nav"]
        observed_on = parse_date(row["as_of_date"])
        if (
            observed_on > CUTOFF
            or nav["status"] != "AVAILABLE"
            or nav["currency"] != "USD"
        ):
            continue
        account_id = row["account_id"]
        value = Decimal(nav["value"])
        prior = observations[account_id].get(observed_on)
        if prior is not None and prior != value:
            conflicts.add(account_id)
            continue
        observations[account_id][observed_on] = value
    return (
        {
            account_id: sorted(values.items())
            for account_id, values in observations.items()
            if account_id not in conflicts
        },
        conflicts,
    )


def paired_internal_flows(
    flows: list[dict[str, object]], flow_type: str
) -> tuple[set[str], dict[str, str]]:
    """Pair an internal-flow class one-to-one, retaining ambiguity as a failure."""
    candidates = [
        row
        for row in flows
        if row["flow_type"] == flow_type
        or (
            flow_type == "TRANSFER:INTERNAL:" and row["flow_type"].startswith(flow_type)
        )
        if row["amount"]["status"] == "AVAILABLE" and row["amount"]["currency"] == "USD"
    ]
    groups: dict[tuple[object, ...], list[dict[str, object]]] = defaultdict(list)
    for row in candidates:
        amount = Decimal(row["amount"]["value"])
        if flow_type == "ACCOUNT_TRANSFER":
            activity_id = row.get("provider_activity_id_sha256")
            if not activity_id:
                groups[("missing-identity", row["record_id"])].append(row)
                continue
            groups[
                (
                    row["event_date"],
                    abs(amount),
                    row.get("provider_id"),
                    row.get("generation_id"),
                    activity_id,
                )
            ].append(row)
        else:
            groups[(row["event_date"], abs(amount))].append(row)
    paired: set[str] = set()
    problems: dict[str, str] = {}
    for group in groups.values():
        if len(group) == 1:
            if flow_type == "ACCOUNT_TRANSFER":
                problems[group[0]["record_id"]] = (
                    "ACCOUNT_TRANSFER lacks a unique source-identity counterparty"
                )
            continue
        if len(group) != 2:
            reason = f"ambiguous or unbalanced {flow_type} transfer group"
            problems.update({row["record_id"]: reason for row in group})
            continue
        left, right = group
        left_amount = Decimal(left["amount"]["value"])
        right_amount = Decimal(right["amount"]["value"])
        if left["account_id"] == right["account_id"] or left_amount != -right_amount:
            reason = f"ambiguous or unbalanced {flow_type} transfer group"
            problems.update({row["record_id"]: reason for row in group})
            continue
        if flow_type == "ACCOUNT_TRANSFER" and not all(
            row.get("mapping_basis") or row.get("source_classification")
            for row in group
        ):
            problems.update(
                {
                    row[
                        "record_id"
                    ]: "ACCOUNT_TRANSFER lacks source-identity pairing assertion"
                    for row in group
                }
            )
            continue
        paired.update(row["record_id"] for row in group)
    return paired, problems


def classified_external_flows(
    ledger_flows: list[dict[str, object]], account_id: str, start: date, end: date
) -> tuple[dict[date, Decimal], int, str | None]:
    by_date: dict[date, Decimal] = defaultdict(Decimal)
    count = 0
    for row in ledger_flows:
        if row["account_id"] != account_id:
            continue
        recognized_on = parse_date(
            str(row.get("nav_recognition_date") or row["event_date"])
        )
        # The opening valuation already contains funding recognized on its
        # date.  Do not both start from it and add it as an investor flow.
        if recognized_on <= start or recognized_on > end:
            continue
        amount = row["amount"]
        if amount["status"] != "AVAILABLE" or amount["currency"] != "USD":
            return {}, 0, f"flow {row['record_id']} has no available USD amount"
        flow_type = row["flow_type"]
        if (
            flow_type in EXTERNAL_FLOW_TYPES
            or flow_type.startswith("TRANSFER:INTERNAL:")
            or flow_type == "ACCOUNT_TRANSFER"
        ):
            by_date[recognized_on] += Decimal(amount["value"])
            count += 1
        else:
            return {}, 0, f"unclassified flow type {flow_type} in {row['record_id']}"
    return dict(by_date), count, None


def window_observations(
    observations: Iterable[tuple[date, Decimal]], start: date, end: date | None
) -> list[tuple[date, Decimal]]:
    """Return only observations in one accepted funded-membership interval."""
    points = sorted(observations)
    interval = [
        point
        for point in points
        if point[0] >= start and (end is None or point[0] <= end)
    ]
    opening = next(
        (point for point in points if point[0] == start - timedelta(days=1)), None
    )
    if opening is not None and opening[1] > 0:
        return [opening, *interval]
    return interval


def composite_series(
    member_observations: dict[str, list[tuple[date, Decimal]]],
    member_flows: dict[str, dict[date, Decimal]],
) -> tuple[list[tuple[date, Decimal]], dict[date, Decimal]]:
    """Build a no-fill composite on the union of admitted member dates."""
    maps = {
        account_id: dict(points)
        for account_id, points in member_observations.items()
        if points
    }
    dates = sorted({observed_on for values in maps.values() for observed_on in values})
    if not dates:
        return [], {}
    first_date = dates[0]
    last_date = dates[-1]
    member_first = {account_id: min(values) for account_id, values in maps.items()}
    member_last = {account_id: max(values) for account_id, values in maps.items()}
    observations = [
        (
            observed_on,
            sum(
                values[observed_on]
                for account_id, values in maps.items()
                if observed_on in values
                and not (
                    observed_on == member_last[account_id]
                    and member_last[account_id] < last_date
                )
            ),
        )
        for observed_on in dates
    ]
    flows: dict[date, Decimal] = defaultdict(Decimal)
    for account_flows in member_flows.values():
        for occurred_on, amount in account_flows.items():
            if occurred_on in dates:
                flows[occurred_on] += amount
    for account_id, values in maps.items():
        first_observed = member_first[account_id]
        last_observed = member_last[account_id]
        account_flows = member_flows.get(account_id, {})
        if first_observed > first_date:
            flows[first_observed] += values[first_observed] - account_flows.get(
                first_observed, Decimal("0")
            )
        if last_observed < last_date:
            flows[last_observed] -= values[last_observed]
    return observations, dict(flows)


def quantbase_raw_observations(
    rows: Iterable[dict[str, str]], account_id: str
) -> list[tuple[date, Decimal]]:
    """Read the captured Quantbase daily value series from pricehistory bytes."""
    observations: list[tuple[date, Decimal]] = []
    for row in rows:
        observed_on = row.get("time")
        value = row.get("value")
        if not observed_on or value in (None, ""):
            raise ValueError("Quantbase pricehistory row lacks a time or value")
        parsed_on = datetime.strptime(observed_on, "%m/%d/%Y").date()
        if parsed_on <= CUTOFF:
            observations.append((parsed_on, Decimal(value)))
    return sorted(observations)


def quantbase_sleeve_flows(
    rows: Iterable[dict[str, str]],
    observations: list[tuple[date, Decimal]],
    opening_date: date | None = None,
) -> FlowData:
    """Place linked Quantbase fills at the BOD of their first later valuation period."""
    source_rows = list(rows)
    dates = sorted(observed_on for observed_on, _ in observations)
    filled_at_by_transaction: dict[str, list[datetime]] = defaultdict(list)
    for row in source_rows:
        if row.get("activity_type") != "order":
            continue
        transaction = row.get("transaction")
        filled_at = row.get("filled_at")
        if transaction and filled_at:
            filled_at_by_transaction[transaction].append(
                datetime.fromisoformat(filled_at.replace("Z", "+00:00"))
            )
    cash_flows: dict[date, Decimal] = defaultdict(Decimal)
    beginning_flows: dict[date, Decimal] = defaultdict(Decimal)
    count = 0
    for row in source_rows:
        if row.get("activity_type") != "filled_txn" or row.get("fund") != "107":
            continue
        if row.get("type") not in {"BUY", "SELL", "MAX_S"}:
            continue
        if (
            str(row.get("created_to_cover_fees", "")).strip().lower() == "true"
            or row.get("is_rebalance") == "True"
        ):
            continue
        amount = row.get("amount")
        if amount in (None, ""):
            raise ValueError("Quantbase sleeve fill lacks an amount")
        fill_timestamps = filled_at_by_transaction.get(str(row.get("id")), [])
        timestamp = max(fill_timestamps, default=None)
        if timestamp is None:
            event_timestamp = row.get("time") or row.get("filled_at")
            if not event_timestamp:
                raise ValueError("Quantbase sleeve fill lacks a fill timestamp")
            timestamp = datetime.fromisoformat(event_timestamp.replace("Z", "+00:00"))
        target_index = next(
            (
                index
                for index, observed_on in enumerate(dates)
                if observed_on > timestamp.date()
            ),
            None,
        )
        if target_index is None or target_index == 0:
            continue
        target = dates[target_index]
        if opening_date is not None and target <= opening_date:
            continue
        value = Decimal(amount)
        if row.get("type") in {"SELL", "MAX_S"} and value > 0:
            value = -value
        cash_flows[timestamp.date()] += value
        beginning_flows[dates[target_index - 1]] += value
        count += 1
    return FlowData(
        cash_flows=dict(cash_flows),
        twr_end_flows={},
        twr_beginning_flows=dict(beginning_flows),
        count=count,
    )


def matrixport_subscription_units(
    nav_rows: Iterable[dict[str, str]],
    bill_rows: Iterable[dict[str, str]],
    subscription_rows: Iterable[dict[str, str]] | None = None,
) -> list[tuple[date, Decimal]]:
    """Derive active units from confirmed subscriptions and recognition NAV."""
    nav_points = sorted(
        (
            datetime.fromisoformat(
                (row.get("day_timestamp_utc") or row["timestamp_utc"]).replace(
                    "Z", "+00:00"
                )
            ),
            Decimal(row["nav"]),
        )
        for row in nav_rows
        if (row.get("day_timestamp_utc") or row.get("timestamp_utc"))
        and row.get("nav") not in (None, "")
    )
    subscriptions: list[tuple[date, Decimal]] = []
    source_rows = subscription_rows
    if source_rows is not None:
        return [
            (
                datetime.fromisoformat(
                    row["confirm_nav_time_utc"].replace("Z", "+00:00")
                ).date(),
                Decimal(row["subscription_share"]),
            )
            for row in source_rows
        ]
    for row in bill_rows:
        if row.get("currency") != "USDT" or row.get("direction") != "2":
            continue
        timestamp = row.get("timestamp_utc") or row.get("timestamp")
        amount = row.get("amount")
        if not timestamp or amount in (None, ""):
            continue
        debit_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        confirmation = next(
            (point for point in nav_points if point[0] > debit_at), None
        )
        if confirmation is not None and confirmation[1] != 0:
            subscriptions.append(
                (confirmation[0].date(), Decimal(amount) / confirmation[1])
            )
    return subscriptions


def matrixport_raw_observations(
    nav_rows: Iterable[dict[str, str]],
    deposit_rows: Iterable[dict[str, str]],
    bill_rows: Iterable[dict[str, str]],
    account_id: str,
    fx_rates: dict[date, Decimal] | None = None,
    fx_unconverted: list[date] | None = None,
    raw_nav_dates: list[date] | None = None,
    fx_last_admitted_date: date = FX_LAST_ADMITTED_DATE,
    subscription_rows: Iterable[dict[str, str]] | None = None,
) -> list[tuple[date, Decimal]]:
    """Use raw Matrixport subscription units and USD-converted NAV, never ledger values.

    Unit-holding rows after the admitted FX boundary (R6 v2) are not converted:
    their dates are recorded in fx_unconverted and no value is emitted. A missing
    rate at or before the boundary remains a fail-closed error. Every raw NAV
    observation date at or before the cutoff is recorded in raw_nav_dates so the
    source_last_observation metric can report the last raw date even when the
    last converted value ends earlier at the FX boundary.
    """
    nav_rows = list(nav_rows)
    subscriptions = matrixport_subscription_units(
        nav_rows, bill_rows, subscription_rows
    )
    observations: list[tuple[date, Decimal]] = []
    for row in nav_rows:
        timestamp = row.get("day_timestamp_utc") or row.get("timestamp_utc")
        nav = row.get("nav")
        if not timestamp or nav in (None, ""):
            continue
        observed_on = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()
        if observed_on <= CUTOFF:
            if raw_nav_dates is not None:
                raw_nav_dates.append(observed_on)
            units = sum(
                (
                    subscription_units
                    for confirmed_on, subscription_units in subscriptions
                    if observed_on >= confirmed_on
                ),
                Decimal("0"),
            )
            rate = (fx_rates or {}).get(observed_on)
            if units == 0:
                continue
            if rate is None:
                if observed_on > fx_last_admitted_date:
                    if fx_unconverted is not None:
                        fx_unconverted.append(observed_on)
                    continue
                raise ValueError(
                    "no admitted USDT/USD close for unit-holding observation "
                    + observed_on.isoformat()
                )
            value = units * Decimal(nav) * rate
            if value != 0:
                observations.append((observed_on, value))
    return observations


def matrixport_raw_flows(
    deposit_rows: Iterable[dict[str, str]],
    observations: list[tuple[date, Decimal]],
    fx_rates: dict[date, Decimal] | None = None,
    opening_date: date | None = None,
    deposits_skipped: list[tuple[date, Decimal]] | None = None,
    event_date_fx: bool = False,
    allow_completed: bool = False,
    subscription_rows: Iterable[dict[str, str]] | None = None,
    fx_rates_by_currency: dict[str, dict[date, Decimal]] | None = None,
) -> tuple[dict[date, Decimal], int]:
    """Place raw confirmed wallet deposits and fund subscriptions into flows.

    A confirmed USDT/USDC deposit with no raw NAV point after it (raw
    observations stop at the admitted FX boundary) is not converted into a
    flow; it is recorded in deposits_skipped as (date, signed raw amount) for
    per-account disclosure, and flows values are unchanged. A subscription
    recognized on or before the opening accepted NAV is already embedded in
    that opening value and is excluded; later confirmed subscriptions are
    positive fund contributions at their NAV-recognition-date FX.
    """
    dates = sorted(observed_on for observed_on, _ in observations)
    flows: dict[date, Decimal] = defaultdict(Decimal)
    count = 0
    for row in deposit_rows:
        if subscription_rows is not None:
            # Native subscription details are the fund-flow authority; wallet
            # deposits remain custody-only and cannot substitute for a fund flow.
            continue
        if row.get("state") not in (
            {"confirmed", "completed"} if allow_completed else {"confirmed"}
        ) or row.get("currency") not in {
            "USDC",
            "USDT",
        }:
            continue
        timestamp = row.get("created_at_utc") or row.get("created_at")
        amount = row.get("amount")
        if not timestamp or amount in (None, ""):
            continue
        occurred_on = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()
        target = next(
            (observed_on for observed_on in dates if observed_on > occurred_on), None
        )
        if target is None:
            if deposits_skipped is not None and occurred_on <= CUTOFF:
                deposits_skipped.append((occurred_on, Decimal(amount)))
            continue
        if (not allow_completed and target == dates[0]) or (
            opening_date is not None and target <= opening_date and not allow_completed
        ):
            continue
        value = Decimal(amount)
        if row["currency"] == "USDT":
            rate_date = occurred_on if event_date_fx else target
            rate = (fx_rates or {}).get(rate_date)
            if rate is None:
                raise ValueError(
                    "no admitted USDT/USD close for deposit " + rate_date.isoformat()
                )
            value *= rate
        flows[target] += value
        count += 1
    if subscription_rows is not None:
        for row in subscription_rows:
            recognized_on = datetime.fromisoformat(
                row["confirm_nav_time_utc"].replace("Z", "+00:00")
            ).date()
            if opening_date is not None and recognized_on <= opening_date:
                continue
            currency = str(row["subscription_currency"])
            rate = (fx_rates_by_currency or {}).get(currency, {}).get(recognized_on)
            if rate is None and currency == "USDT":
                rate = (fx_rates or {}).get(recognized_on)
            if rate is None:
                raise ValueError("no admitted FX for subscription recognition")
            flows[recognized_on] += Decimal(row["subscription_amount"]) * rate
            count += 1
    return dict(flows), count


def stablecoin_usd_fx_rates(
    path: Path, ticker: str, fx_last_admitted_date: date = FX_LAST_ADMITTED_DATE
) -> dict[date, Decimal]:
    """Read one selected stablecoin/USD Close series from the raw FX capture."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        parse_date(str(row["Date"])[:10]): Decimal(str(row["Close"]))
        for row in payload["rows"]
        if row.get("Ticker") == ticker
        and row.get("Close") is not None
        and parse_date(str(row["Date"])[:10]) <= fx_last_admitted_date
    }


def usdt_usd_fx_rates(
    path: Path, fx_last_admitted_date: date = FX_LAST_ADMITTED_DATE
) -> dict[date, Decimal]:
    """Read daily USDT/USD closes from the selected raw FX capture."""
    return stablecoin_usd_fx_rates(path, "USDT-USD", fx_last_admitted_date)


def selected_fx_covers_custody_event_dates(
    rates_path: Path,
    deposit_rows: Iterable[dict[str, str]],
    bill_rows: Iterable[dict[str, str]],
    subscription_rows: Iterable[dict[str, str]] | None = None,
) -> None:
    """Require selected normalized FX rows for every raw custody event date."""
    with rates_path.open(encoding="utf-8-sig", newline="") as handle:
        admitted = {
            (row.get("date"), row.get("currency"), row.get("ticker"))
            for row in csv.DictReader(handle)
        }

    def utc_date(timestamp: str) -> str:
        return (
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .date()
            .isoformat()
        )

    required: list[tuple[str, str, str]] = []
    for row in deposit_rows:
        currency = row.get("currency")
        timestamp = row.get("created_at_utc") or row.get("created_at")
        if (
            row.get("state")
            in (
                {"confirmed", "completed", "success"}
                if subscription_rows is not None
                else {"confirmed"}
            )
            and currency in {"USDC", "USDT"}
            and timestamp
            and row.get("amount") not in (None, "")
        ):
            required.append((utc_date(timestamp), str(currency), f"{currency}-USD"))
    for row in bill_rows if subscription_rows is None else subscription_rows:
        timestamp = row.get("timestamp_utc") or row.get("timestamp")
        if subscription_rows is not None:
            timestamp = row.get("subscription_time_utc")
        if (
            (
                subscription_rows is not None
                or (row.get("currency") == "USDT" and row.get("direction") == "2")
            )
            and timestamp
            and (
                row.get("subscription_amount")
                if subscription_rows is not None
                else row.get("amount")
            )
            not in (None, "")
        ):
            required.append(
                (
                    utc_date(timestamp),
                    str(row.get("subscription_currency", "USDT")),
                    f"{row.get('subscription_currency', 'USDT')}-USD",
                )
            )
    missing = [
        item
        for item in required
        if item[0] <= CUTOFF.isoformat() and item not in admitted
    ]
    if missing:
        raise ValueError("selected FX rates lack a raw custody event date")


def raw_custody_capital(
    quantbase_rows: Iterable[dict[str, str]],
    matrixport_deposit_rows: Iterable[dict[str, str]],
    matrixport_bill_rows: Iterable[dict[str, str]],
    fx_rates: dict[str, dict[date, Decimal]],
    matrixport_subscription_rows: Iterable[dict[str, str]] | None = None,
    event_days: dict[str, dict[str, list[date]]] | None = None,
    fund_scoped_strategies: frozenset[str] = frozenset(),
) -> dict[str, dict[str, Decimal]]:
    """Derive R8 owner capital and unallocated cash from raw custody events.

    The two selected provider sources each fund exactly one governed sleeve.
    Owner deposits and withdrawals change invested capital; sleeve buys and
    redemptions only change the derived cash line.  Events are aggregated by
    date before the strict-negative-cash check, so no intraday order is inferred.

    For a fund-scoped strategy (amended PROVIDER_CUSTODY_PERIMETER) the account
    deposits/withdrawals are not the strategy's capital and no derived cash
    line exists, so the negative-cash check — a property of that line — is not
    applied; the gross allocation sum is reported for the caller to use as the
    strategy's invested capital.
    """
    events: dict[str, dict[date, dict[str, Decimal]]] = defaultdict(
        lambda: defaultdict(lambda: {"owner": Decimal("0"), "allocation": Decimal("0")})
    )

    def add(strategy_id: str, occurred_on: date, kind: str, amount: Decimal) -> None:
        if amount <= 0:
            raise ValueError("raw custody event amount must be positive")
        events[strategy_id][occurred_on][kind] += amount
        if event_days is not None:
            event_days.setdefault(strategy_id, {"owner": [], "allocation": []})[
                kind
            ].append(occurred_on)

    def event_date(timestamp: str) -> date:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("custody event timestamp lacks timezone")
        return parsed.astimezone(timezone.utc).date()

    admitted_transfer_rows: dict[str, dict[str, str]] = {}
    admitted_buy_rows: dict[str, dict[str, str]] = {}
    linked_provider_identity: str | None = None
    for row in quantbase_rows:
        if row.get("activity_type") == "transfer":
            if row.get("status") != "C":
                continue
            transfer_type = row.get("type")
            if transfer_type not in {"DEPOSIT", "WITHDRAWAL"}:
                raise ValueError("unsupported Quantbase owner transfer type")
            transfer_id = str(row.get("id") or "").strip()
            linked_identity = str(
                row.get(
                    "destination_account_id"
                    if transfer_type == "DEPOSIT"
                    else "origin_account_id"
                )
                or ""
            ).strip()
            if not transfer_id or not linked_identity:
                raise ValueError(
                    "completed Quantbase owner transfer lacks id or linked account"
                )
            previous = admitted_transfer_rows.get(transfer_id)
            if previous is not None:
                if previous != row:
                    raise ValueError("conflicting duplicate Quantbase transfer id")
                continue
            admitted_transfer_rows[transfer_id] = dict(row)
            if linked_provider_identity is None:
                linked_provider_identity = linked_identity
            elif linked_provider_identity != linked_identity:
                raise ValueError(
                    "completed Quantbase owner transfers have conflicting linked accounts"
                )
            timestamp = row.get("time") or row.get("filled_at") or row.get("created_at")
            amount = row.get("amount")
            amount_cents = row.get("amount_cents")
            if not timestamp or amount in (None, "") or amount_cents in (None, ""):
                raise ValueError(
                    "completed Quantbase owner transfer lacks timestamp, amount, or cents"
                )
            value = Decimal(str(amount))
            cents = Decimal(str(amount_cents))
            if value <= 0 or cents != cents.to_integral_value() or cents != value * 100:
                raise ValueError(
                    "completed Quantbase owner transfer amount/cents mismatch"
                )
            occurred_on = event_date(timestamp)
            if occurred_on > CUTOFF:
                continue
            if transfer_type == "DEPOSIT":
                add("volatis-strategy", occurred_on, "owner", value)
            else:
                add("volatis-strategy", occurred_on, "owner", value)
                events["volatis-strategy"][occurred_on]["owner"] -= value * 2
            continue
        timestamp = row.get("time") or row.get("filled_at") or row.get("created_at")
        amount = row.get("amount")
        if row.get("activity_type") == "journal" and row.get("status") in {
            "C",
            "COMPLETE",
        }:
            source_type = (
                (row.get("source_type") or row.get("type"))
                if row.get("status") == "COMPLETE"
                else row.get("type")
            )
            if source_type not in {"PARTNER_SUB_FEE", "AUM_FEE"}:
                raise ValueError("unknown completed Quantbase custody journal")
            continue
        if not timestamp or amount in (None, ""):
            continue
        occurred_on = event_date(timestamp)
        if occurred_on > CUTOFF:
            continue
        value = Decimal(str(amount))
        if row.get("activity_type") == "filled_txn" and row.get("fund") == "107":
            if row.get("type") == "BUY":
                buy_id = str(row.get("id") or "").strip()
                if buy_id:
                    previous_buy = admitted_buy_rows.get(buy_id)
                    if previous_buy is not None:
                        if previous_buy != row:
                            raise ValueError("conflicting duplicate Quantbase BUY id")
                        continue
                    admitted_buy_rows[buy_id] = dict(row)
                add("volatis-strategy", occurred_on, "allocation", abs(value))
            elif row.get("type") == "SELL":
                if str(row.get("created_to_cover_fees", "")).strip().lower() != "true":
                    raise ValueError("unsupported completed Quantbase SELL/redemption")

    for row in matrixport_deposit_rows:
        valid_states = (
            {"confirmed", "completed", "success"}
            if matrixport_subscription_rows is not None
            else {"confirmed"}
        )
        if row.get("state") not in valid_states or row.get("currency") not in {
            "USDC",
            "USDT",
        }:
            continue
        timestamp = row.get("created_at_utc") or row.get("created_at")
        amount = row.get("amount")
        if not timestamp or amount in (None, ""):
            raise ValueError("confirmed Matrixport deposit lacks timestamp or amount")
        occurred_on = event_date(timestamp)
        if occurred_on > CUTOFF:
            continue
        rate = fx_rates.get(str(row["currency"]), {}).get(occurred_on)
        if rate is None:
            raise ValueError("missing event-date FX for Matrixport deposit")
        add("robuxio-crypto", occurred_on, "owner", Decimal(str(amount)) * rate)

    if matrixport_subscription_rows is not None:
        for row in matrixport_bill_rows:
            if str(row.get("tx_type", "")).upper() in {
                "TRANSFER",
                "REDEMPTION",
                "WITHDRAWAL",
                "DEPOSIT",
            }:
                raise ValueError("unsupported native Matrixport wallet event")
        allocation_rows = matrixport_subscription_rows
    else:
        allocation_rows = matrixport_bill_rows
    for row in allocation_rows:
        if matrixport_subscription_rows is None:
            tx_type = str(row.get("tx_type", "")).strip()
            if not tx_type:
                raise ValueError("unsupported relevant Matrixport wallet event")
            raise ValueError("unsupported relevant Matrixport wallet event")
        # The raw subscription leg is the USDT direction-2 bill.  The USDC
        # direction-1/2 records are wallet conversion legs, not owner flows or
        # sleeve allocations; treating them as redemptions would invent cash.
        if matrixport_subscription_rows is None and (
            row.get("currency") != "USDT" or row.get("direction") != "2"
        ):
            continue
        timestamp = (
            row.get("subscription_time_utc")
            if matrixport_subscription_rows is not None
            else row.get("timestamp_utc") or row.get("timestamp")
        )
        amount = (
            row.get("subscription_amount")
            if matrixport_subscription_rows is not None
            else row.get("amount")
        )
        if not timestamp or amount in (None, ""):
            continue
        occurred_on = event_date(timestamp)
        if occurred_on > CUTOFF:
            continue
        currency = str(row.get("subscription_currency", "USDT"))
        rate = fx_rates[currency].get(occurred_on)
        if rate is None:
            raise ValueError("missing event-date FX for Matrixport subscription")
        value = Decimal(str(amount)) * rate
        add("robuxio-crypto", occurred_on, "allocation", value)

    result: dict[str, dict[str, Decimal]] = {}
    for strategy_id, dated_events in events.items():
        invested = Decimal("0")
        allocation = Decimal("0")
        cash = Decimal("0")
        for occurred_on in sorted(dated_events):
            owner = dated_events[occurred_on]["owner"]
            allocated = dated_events[occurred_on]["allocation"]
            invested += owner
            allocation += allocated
            cash += owner - allocated
            if cash < 0 and strategy_id not in fund_scoped_strategies:
                raise ValueError(
                    f"raw custody cash is negative for {strategy_id} on {occurred_on.isoformat()}"
                )
        result[strategy_id] = {
            "invested": invested,
            "cash": cash,
            "allocation": allocation,
        }
    return result


COVERAGE_METRIC_ID = "date.strategy_cash.coverage_end"
FUND_SCOPED_NO_CASH_LINE_REASON = (
    "the amended PROVIDER_CUSTODY_PERIMETER decision lists this strategy's "
    "provider as fund-scoped: invested capital is the gross fund/product "
    "allocations at fill dates and account deposits/withdrawals are not the "
    "strategy's capital, so no derived strategy cash line exists to measure"
)
FUND_SCOPED_NO_COVERAGE_REASON = (
    "no derived strategy cash line exists for this fund-scoped provider, so a "
    "strategy-cash coverage end has no meaning; the capital side is bound by "
    "the raw fund allocation captures admitted through the cutoff"
)
# Terminals that end a list endpoint naturally.  A discovery terminal
# (all_discovered_*_ids), single_response or catalog_disposition never carries a date.
NATURAL_LIST_TERMINALS = frozenset(
    {
        "empty_page",
        "short_page",
        "no_next_id",
        "structured_parameter_rejections_dispositioned",
    }
)
MATRIXPORT_COVERAGE_HOST = "GET https://mapi.matrixport.com"
MATRIXPORT_WALLET_PATHS = ("/mapi/v1/wallet/deposits", "/mapi/v1/wallet/withdrawals")
MATRIXPORT_TRADE_RECORD_PATH = "/strategy/api/v2/user/trade_record"
PLAIN_LIST_PARAMS = frozenset({"currency", "limit", "offset"})
WINDOW_START_KEYS = frozenset({"start_time"})
WINDOW_END_KEYS = frozenset({"end_time"})
# Every parameter this derivation reads is a time bound, paging, or a scope.  A scope
# parameter narrows WHAT a request returns (here the wallet currency), so only requests
# of one scope can be chained: an interval one currency did not observe is not filled by
# another currency's window (MX-1 (c): every request for the endpoint is judged, and a
# request over a different filter is a different list).
PAGING_PARAMS = frozenset({"limit", "offset"})
SCOPE_PARAMS = PLAIN_LIST_PARAMS - PAGING_PARAMS
CUSTODY_CAPTURE_PROVIDERS = {
    "volatis-strategy": "quantbase",
    "robuxio-crypto": "matrixport",
}


def fund_scoped_strategy_ids(providers: frozenset[str]) -> frozenset[str]:
    """Custody-captured strategies whose provider the contract lists as fund-scoped."""
    return frozenset(
        strategy_id
        for strategy_id, provider in CUSTODY_CAPTURE_PROVIDERS.items()
        if provider.upper() in providers
    )


def coverage_entity(strategy_id: str, members: set[str]) -> str:
    """``<strategy>|<provider>|<account>``; the account is ``-`` unless exactly one."""
    provider = CUSTODY_CAPTURE_PROVIDERS[strategy_id]
    account = next(iter(members)) if len(members) == 1 else "-"
    return f"{strategy_id}|{provider}|{account}"


def utc_day_of_ms(milliseconds: int) -> date:
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).date()


def request_scope(request: dict[str, object]) -> tuple[tuple[str, str], ...] | None:
    """The filters a list request narrows its result by, or None when unreadable.

    Two requests chain only when their scopes are equal.  The key is every
    parameter that is neither a time bound nor paging, with its value, sorted.
    """
    raw = request.get("safe_params")
    try:
        params = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None
    if not isinstance(params, dict):
        return None
    return tuple(
        sorted(
            (str(key), json.dumps(value, sort_keys=True))
            for key, value in params.items()
            if key in SCOPE_PARAMS
        )
    )


def request_time_window(request: dict[str, object]) -> tuple[int, int] | None | str:
    """The (start_ms, end_ms) a list request asked for.

    None means the request has no time parameter (a full-history list); the
    string "unbounded" marks a request whose time parameters this derivation
    cannot read, which proves nothing.
    """
    raw = request.get("safe_params")
    try:
        params = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return "unbounded"
    if not isinstance(params, dict):
        return "unbounded"
    unread = set(params) - PLAIN_LIST_PARAMS - WINDOW_START_KEYS - WINDOW_END_KEYS
    if unread:
        return "unbounded"
    if not set(params) & (WINDOW_START_KEYS | WINDOW_END_KEYS):
        return None
    end = params.get("end_time")
    start = params.get("start_time", 0)
    if (
        isinstance(end, bool)
        or not isinstance(end, int)
        or isinstance(start, bool)
        or not isinstance(start, int)
    ):
        return "unbounded"
    return start, end


def list_endpoint_coverage_end(
    coverage_rows: list[dict[str, str]],
    request_lines: list[str],
    path: str,
    first_event: date | None,
) -> date | None:
    """The date a captured list endpoint proves, or None when it proves no date.

    A coverage row extends a date only if (a) its status is
    complete_to_observed_boundary, (b) its terminal is a natural list terminal,
    (c) every request for the endpoint in the pinned request manifest is either
    a full-history list (then the boundary is the UTC date of the response
    instant of the coverage row) or a time window (then the windows must chain
    without a gap and the boundary is the end of the chain), and (d) the chain
    starts at or before the first event.  A detail-fetch row, a discovery
    terminal and a capture time on their own never carry a date.
    """
    endpoint = f"{MATRIXPORT_COVERAGE_HOST}{path}"
    rows = [row for row in coverage_rows if row.get("endpoint") == endpoint]
    if len(rows) != 1:
        return None
    row = rows[0]
    if (
        row.get("status") != "complete_to_observed_boundary"
        or row.get("terminal_reason") not in NATURAL_LIST_TERMINALS
    ):
        return None
    # The response instant of the coverage row bounds every observation of the
    # endpoint: a capture cannot have observed a moment after it was made, whatever
    # time a request asked for.  It is an upper bound only, never a data date.
    captured = row.get("updated_at_utc") or ""
    try:
        moment = datetime.fromisoformat(captured.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        return None
    capture_ms = int(moment.timestamp() * 1000)
    windows_by_scope: dict[tuple[tuple[str, str], ...], list[tuple[int, int]]] = {}
    unwindowed = 0
    for line in request_lines:
        if not line.strip():
            continue
        request = json.loads(line)
        if not isinstance(request, dict) or request.get("endpoint") != path:
            continue
        window = request_time_window(request)
        scope = request_scope(request)
        if window == "unbounded" or scope is None:
            return None
        if window is None:
            unwindowed += 1
        else:
            assert isinstance(window, tuple)
            start, end = window
            # A window that began after the response observed nothing; one that
            # ends after it observed only up to the response.
            if start <= capture_ms:
                windows_by_scope.setdefault(scope, []).append(
                    (start, min(end, capture_ms))
                )
            else:
                windows_by_scope.setdefault(scope, [])
    if unwindowed and windows_by_scope:
        return None
    if unwindowed:
        return moment.date()
    if not windows_by_scope:
        return None
    first_ms = (
        0
        if first_event is None
        else int(
            datetime(
                first_event.year,
                first_event.month,
                first_event.day,
                tzinfo=timezone.utc,
            ).timestamp()
            * 1000
        )
    )
    # Each scope is chained on its own, and the endpoint is proven only through the
    # smallest end of them: every scope the capture asked has to reach the first event.
    ends: list[int] = []
    for windows in windows_by_scope.values():
        segments: list[list[int]] = []
        for start, end in sorted(windows):
            if segments and start <= segments[-1][1] + 1:
                segments[-1][1] = max(segments[-1][1], end)
            else:
                segments.append([start, end])
        reached = [end for start, end in segments if start <= first_ms <= end]
        if not reached:
            return None
        ends.append(reached[0])
    return utc_day_of_ms(min(ends))


def quantbase_capture_end(ledger: dict[str, object], selection: object) -> date | None:
    """The UTC date of the selected Quantbase account-activity capture.

    The account-activity list is one unwindowed API response from which both
    the owner flows and the sleeve allocations are read, so one boundary serves
    both.  The 24 pinned roles carry no Quantbase capture-time artifact; the
    capture date is the generation date of the selected Quantbase generation
    named by the manifest's generation selection evidence.
    """
    if not isinstance(selection, dict):
        return None
    quantbase = selection.get("quantbase")
    generations = ledger.get("provider_generations")
    if not isinstance(quantbase, dict) or not isinstance(generations, list):
        return None
    matches = [
        item
        for item in generations
        if isinstance(item, dict)
        and item.get("provider_id") == "quantbase"
        and item.get("generation_id") == quantbase.get("generation_id")
    ]
    if len(matches) != 1:
        return None
    try:
        return parse_date(str(matches[0].get("generation_as_of_date")))
    except ValueError:
        return None


def _coverage_row_capture_ms(row: dict[str, str]) -> int | None:
    """The response instant of a coverage row in epoch milliseconds, or None.

    The instant is an upper bound on every observation of the endpoint the row
    covers: a capture cannot have observed a moment after it was made.
    """
    captured = row.get("updated_at_utc") or ""
    try:
        moment = datetime.fromisoformat(captured.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        return None
    return int(moment.timestamp() * 1000)


def _clip_request_windows(request_lines: list[str], bound_ms: int) -> list[str]:
    """The request manifest lines with every windowed end_time clipped to bound_ms.

    A declared end past the observing capture's response instant is internally
    inconsistent; the window proves only through the instant it was observed at.
    Unwindowed and unreadable lines pass through unchanged - their handling
    (full-history boundary or fail-closed) belongs to list_endpoint_coverage_end.
    """
    clipped: list[str] = []
    for line in request_lines:
        if not line.strip():
            clipped.append(line)
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            clipped.append(line)
            continue
        if not isinstance(request, dict):
            clipped.append(line)
            continue
        raw = request.get("safe_params")
        try:
            params = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            clipped.append(line)
            continue
        if isinstance(params, dict):
            end = params.get("end_time")
            if isinstance(end, int) and not isinstance(end, bool) and end > bound_ms:
                params["end_time"] = bound_ms
                request["safe_params"] = (
                    json.dumps(params) if isinstance(raw, str) else params
                )
                clipped.append(json.dumps(request))
                continue
        clipped.append(line)
    return clipped


def custody_coverage_end(
    strategy_id: str,
    event_days: dict[str, list[date]],
    ledger: dict[str, object],
    selection: object,
    coverage_rows: list[dict[str, str]] | None,
    request_lines: list[str] | None,
    base_coverage_rows: list[dict[str, str]] | None = None,
    base_request_lines: list[str] | None = None,
) -> date | str:
    """The unclamped coverage end of one custody cash line, from the raw captures.

    Owner flows and sleeve allocations are each proven through their own end and
    the line through the smaller one.  A fact type no capture proves past its
    last event ends at that last event (MX-2), never at a capture time.

    When a separately sha-pinned base-run capture set is supplied for matrixport,
    the trade_record windows of the base run are chained with the selected
    generation's under the same scope/first-event/tiling rules
    list_endpoint_coverage_end already applies (the base full-history window
    carries the first event; the incremental window continues the chain), so the
    capital_anchors boundary reaches the incremental end.  Absent the base set the
    single selected window is used exactly as before.
    """
    owner, allocation = event_days.get("owner", []), event_days.get("allocation", [])
    if not owner:
        return "the line has no owner-funding event, so no flow boundary applies"
    provider = CUSTODY_CAPTURE_PROVIDERS[strategy_id]
    claimed: dict[str, date | None]
    if provider == "quantbase":
        capture = quantbase_capture_end(ledger, selection)
        claimed = {"flows": capture, "capital_anchors": capture}
    else:
        rows, lines = coverage_rows or [], request_lines or []
        wallet = [
            list_endpoint_coverage_end(rows, lines, path, min(owner))
            for path in MATRIXPORT_WALLET_PATHS
        ]
        anchor_lines = lines
        if base_request_lines is not None and base_coverage_rows is not None:
            # The base run may contribute its request windows only when its own
            # trade_record coverage row is a single complete-to-observed-boundary
            # natural-terminal row (MX-1): a base capture that did not read the
            # list to its end proves no window.  The base row's own response
            # instant bounds the base windows - a base capture cannot have
            # observed a moment after it was made, whatever end_time its
            # manifest declares - and the selected generation's coverage row
            # still bounds the merged chain inside list_endpoint_coverage_end.
            endpoint = f"{MATRIXPORT_COVERAGE_HOST}{MATRIXPORT_TRADE_RECORD_PATH}"
            base_rows = [
                row for row in base_coverage_rows if row.get("endpoint") == endpoint
            ]
            if (
                len(base_rows) == 1
                and base_rows[0].get("status") == "complete_to_observed_boundary"
                and base_rows[0].get("terminal_reason") in NATURAL_LIST_TERMINALS
            ):
                base_capture_ms = _coverage_row_capture_ms(base_rows[0])
                if base_capture_ms is not None:
                    anchor_lines = [
                        *lines,
                        *_clip_request_windows(base_request_lines, base_capture_ms),
                    ]
        claimed = {
            "flows": None if None in wallet else min(cast(list[date], wallet)),
            "capital_anchors": list_endpoint_coverage_end(
                rows,
                anchor_lines,
                MATRIXPORT_TRADE_RECORD_PATH,
                min(allocation) if allocation else None,
            ),
        }
    ends: list[date] = []
    for fact_type, days in (("flows", owner), ("capital_anchors", allocation)):
        last = max(days) if days else None
        end = claimed[fact_type]
        if end is None:
            if last is None:
                continue
            end = last
        elif last is not None and end < last:
            return (
                f"the {fact_type} capture boundary {end.isoformat()} precedes its "
                f"last event {last.isoformat()}"
            )
        ends.append(end)
    return min(ends)


ACCOUNT_METRIC_IDS = (
    "capital.account.cutoff",
    "date.source_last_observation",
    "flows.account.net",
    "flows.account.count",
    "twr.account",
    "mwr.account",
)
FLOW_DEPENDENT_METRIC_IDS = (
    "flows.account.net",
    "flows.account.count",
    "twr.account",
    "mwr.account",
)
R6_PROVIDERS = {"QUANTBASE", "MATRIXPORT"}


def r6_ledger_tail_note(
    provider: str,
    ledger_points: list[tuple[date, Decimal]],
    raw_points: list[tuple[date, Decimal]],
) -> str:
    """Disclose ledger valuations after the last raw observation for R6 providers.

    For Matrixport the raw value series stops at the last FX-converted
    observation, so the note names that basis explicitly.
    """
    if provider not in R6_PROVIDERS or not raw_points:
        return ""
    raw_last = raw_points[-1][0]
    tail = [point for point in ledger_points if point[0] > raw_last]
    if not tail:
        return ""
    raw_basis = (
        "FX-converted raw observation"
        if provider == "MATRIXPORT"
        else "raw observation"
    )
    return (
        f"R6: {len(tail)} ledger USD valuation(s) from {tail[0][0].isoformat()} "
        f"to {tail[-1][0].isoformat()} follow the last {raw_basis} "
        f"{raw_last.isoformat()} and are cross-checks only, not expected values."
    )


def raw_provider_data(
    qe_root: Path,
    crosswalk_rows: list[dict[str, object]],
    membership_starts: dict[str, date] | None = None,
    paths: dict[str, Path] | None = None,
    raw_last_dates: dict[str, date] | None = None,
    fx_last_admitted_date: date = FX_LAST_ADMITTED_DATE,
    event_date_fx: bool = False,
) -> tuple[
    dict[str, list[tuple[date, Decimal]]],
    dict[str, FlowData],
    list[Path],
    dict[str, dict[str, object]],
    dict[str, str],
]:
    """Load the R6 raw-capture overrides, failing closed per account.

    A missing, unreadable, unparsable, or empty selected raw capture records a
    failure for the affected account's metrics; the caller must emit
    not_derivable and must not substitute ledger values. The fifth return value
    carries per-account disclosure notes (for example FX-unconverted raw dates).
    When raw_last_dates is given, it is populated with each successfully loaded
    account's last raw provider observation date at or before the cutoff
    (Quantbase pricehistory dates, Matrixport raw nav_daily dates) regardless
    of FX conversion, for the date.source_last_observation metric; a raw
    capture whose every observation is FX-unconverted still records its last
    raw date while its USD metrics fail closed.
    """
    paths = paths or default_input_paths()
    price_history_path = paths["quantbase_price_history"]
    activity_path = paths["quantbase_activity"]
    matrixport_nav_path = paths["matrixport_nav"]
    matrixport_deposits_path = paths["matrixport_deposits"]
    matrixport_bills_path = paths["matrixport_bills"]
    fx_path = paths["fx"]
    matrixport_subscription_path = paths.get("matrixport_subscription_details")
    providers = {
        str(row["account_id"]): str(row.get("provider", "")).upper()
        for row in crosswalk_rows
    }
    observations: dict[str, list[tuple[date, Decimal]]] = {}
    flows: dict[str, FlowData] = {}
    used: list[Path] = []
    failures: dict[str, dict[str, object]] = {}
    notes: dict[str, str] = {}
    membership_starts = membership_starts or {}
    quantbase_accounts = [
        account_id
        for account_id, provider in providers.items()
        if provider == "QUANTBASE"
    ]
    if quantbase_accounts:
        if len(quantbase_accounts) != 1:
            reason = (
                "R6 Quantbase raw capture supports exactly one account; "
                f"the crosswalk lists {len(quantbase_accounts)}"
            )
            for account_id in quantbase_accounts:
                failures[account_id] = {
                    "metrics": ACCOUNT_METRIC_IDS,
                    "reason": reason,
                }
        elif not (qe_root / price_history_path).is_file():
            failures[quantbase_accounts[0]] = {
                "metrics": ACCOUNT_METRIC_IDS,
                "reason": "R6 raw capture is missing: " + price_history_path.as_posix(),
            }
        else:
            account_id = quantbase_accounts[0]
            try:
                with (qe_root / price_history_path).open(
                    encoding="utf-8-sig", newline=""
                ) as handle:
                    values = quantbase_raw_observations(
                        csv.DictReader(handle), account_id
                    )
            except (OSError, ValueError) as error:
                failures[account_id] = {
                    "metrics": ACCOUNT_METRIC_IDS,
                    "reason": "R6 raw capture "
                    + price_history_path.as_posix()
                    + f" is unreadable or unparsable: {error}",
                }
            else:
                if not values:
                    failures[account_id] = {
                        "metrics": ACCOUNT_METRIC_IDS,
                        "reason": "R6 raw capture "
                        + price_history_path.as_posix()
                        + " yields no observations",
                    }
                else:
                    observations[account_id] = values
                    if raw_last_dates is not None:
                        raw_last_dates[account_id] = values[-1][0]
                    used.append(price_history_path)
                    if not (qe_root / activity_path).is_file():
                        failures[account_id] = {
                            "metrics": FLOW_DEPENDENT_METRIC_IDS,
                            "reason": "R6 raw activity capture is missing: "
                            + activity_path.as_posix(),
                        }
                    else:
                        try:
                            with (qe_root / activity_path).open(
                                encoding="utf-8-sig", newline=""
                            ) as handle:
                                flows[account_id] = quantbase_sleeve_flows(
                                    csv.DictReader(handle),
                                    values,
                                    membership_starts.get(account_id),
                                )
                        except (OSError, ValueError) as error:
                            failures[account_id] = {
                                "metrics": FLOW_DEPENDENT_METRIC_IDS,
                                "reason": "R6 raw activity capture "
                                + activity_path.as_posix()
                                + f" is unreadable or unparsable: {error}",
                            }
                        else:
                            used.append(activity_path)
    matrixport_accounts = [
        account_id
        for account_id, provider in providers.items()
        if provider == "MATRIXPORT"
    ]
    if matrixport_accounts:
        matrixport_paths = (
            matrixport_nav_path,
            matrixport_deposits_path,
            matrixport_bills_path,
            fx_path,
        )
        missing_paths = [
            path for path in matrixport_paths if not (qe_root / path).is_file()
        ]
        if len(matrixport_accounts) != 1:
            reason = (
                "R6 Matrixport raw capture supports exactly one account; "
                f"the crosswalk lists {len(matrixport_accounts)}"
            )
            for account_id in matrixport_accounts:
                failures[account_id] = {
                    "metrics": ACCOUNT_METRIC_IDS,
                    "reason": reason,
                }
        elif missing_paths:
            failures[matrixport_accounts[0]] = {
                "metrics": ACCOUNT_METRIC_IDS,
                "reason": "R6 raw capture is missing: "
                + ", ".join(path.as_posix() for path in missing_paths),
            }
        else:
            account_id = matrixport_accounts[0]
            try:
                with (
                    (qe_root / matrixport_nav_path).open(
                        encoding="utf-8-sig", newline=""
                    ) as nav_handle,
                    (qe_root / matrixport_deposits_path).open(
                        encoding="utf-8-sig", newline=""
                    ) as deposit_handle,
                    (qe_root / matrixport_bills_path).open(
                        encoding="utf-8-sig", newline=""
                    ) as bill_handle,
                ):
                    deposit_rows = list(csv.DictReader(deposit_handle))
                    bill_rows = list(csv.DictReader(bill_handle))
                    subscription_rows = None
                    if matrixport_subscription_path is not None:
                        with (qe_root / matrixport_subscription_path).open(
                            encoding="utf-8-sig", newline=""
                        ) as subscription_handle:
                            subscription_rows = list(
                                csv.DictReader(subscription_handle)
                            )
                    fx_rates = usdt_usd_fx_rates(
                        qe_root / fx_path, fx_last_admitted_date
                    )
                    fx_unconverted: list[date] = []
                    raw_nav_dates: list[date] = []
                    deposits_skipped: list[tuple[date, Decimal]] = []
                    values = matrixport_raw_observations(
                        csv.DictReader(nav_handle),
                        deposit_rows,
                        bill_rows,
                        account_id,
                        fx_rates,
                        fx_unconverted,
                        raw_nav_dates,
                        fx_last_admitted_date,
                        subscription_rows,
                    )
                    if not values and not raw_nav_dates:
                        raise ValueError(
                            "R6 raw capture "
                            + matrixport_nav_path.as_posix()
                            + " yields no observations"
                        )
                    if values:
                        opening_date = membership_starts.get(account_id)
                        if subscription_rows is not None and opening_date is not None:
                            accepted_window = window_observations(
                                values, opening_date, None
                            )
                            opening_date = (
                                accepted_window[0][0] if accepted_window else None
                            )
                        cash_flows, count = matrixport_raw_flows(
                            deposit_rows,
                            values,
                            fx_rates,
                            opening_date,
                            deposits_skipped,
                            event_date_fx,
                            subscription_rows is not None,
                            subscription_rows,
                            {
                                "USDC": stablecoin_usd_fx_rates(
                                    qe_root / fx_path, "USDC-USD", fx_last_admitted_date
                                ),
                                "USDT": fx_rates,
                            },
                        )
            except (OSError, ValueError, KeyError) as error:
                failures[account_id] = {
                    "metrics": ACCOUNT_METRIC_IDS,
                    "reason": f"R6 raw Matrixport capture is unreadable or "
                    f"unparsable: {error}",
                }
            else:
                if values:
                    observations[account_id] = values
                    if raw_last_dates is not None and raw_nav_dates:
                        raw_last_dates[account_id] = max(raw_nav_dates)
                    flows[account_id] = FlowData(
                        cash_flows=cash_flows,
                        twr_end_flows=cash_flows,
                        twr_beginning_flows={},
                        count=count,
                    )
                    note_segments = []
                    if fx_unconverted:
                        note_segments.append(
                            f"R6 v2: {len(fx_unconverted)} raw Matrixport NAV date(s) "
                            f"from {min(fx_unconverted).isoformat()} to "
                            f"{max(fx_unconverted).isoformat()} follow the admitted FX "
                            f"boundary {fx_last_admitted_date.isoformat()} and are "
                            "FX-unconverted; no converted value is emitted for them, "
                            "and date.source_last_observation reports the last raw "
                            "NAV date while USD capital as_of stays at the last "
                            "FX-converted date."
                        )
                    if deposits_skipped:
                        skipped = ", ".join(
                            f"{occurred_on.isoformat()} {money(amount)}"
                            for occurred_on, amount in sorted(deposits_skipped)
                        )
                        note_segments.append(
                            f"R6 v2: {len(deposits_skipped)} confirmed Matrixport "
                            "deposit(s) past the last FX-converted raw observation "
                            "have no admitted conversion target and are excluded "
                            "from flows; deposits_skipped (raw wallet amounts): "
                            f"{skipped}; flows.account.net/count remain the "
                            "admitted-converted values."
                        )
                    if note_segments:
                        notes[account_id] = " ".join(note_segments)
                    used.extend(matrixport_paths)
                elif raw_nav_dates:
                    # R6 v2: every raw NAV observation is FX-unconverted (or holds
                    # no units), so no USD value exists; the raw coverage and its
                    # provenance are retained independently of conversion while the
                    # USD metrics stay not-derivable.
                    if raw_last_dates is not None:
                        raw_last_dates[account_id] = max(raw_nav_dates)
                    if fx_unconverted:
                        reason = (
                            "R6 raw Matrixport capture has no FX-converted "
                            "observation at or before the admitted FX boundary "
                            + fx_last_admitted_date.isoformat()
                        )
                    else:
                        reason = (
                            "R6 raw Matrixport capture has no unit-holding "
                            "observation at or before the cutoff"
                        )
                    failures[account_id] = {
                        "metrics": (
                            "capital.account.cutoff",
                            "flows.account.net",
                            "flows.account.count",
                            "twr.account",
                            "mwr.account",
                        ),
                        "reason": reason,
                    }
                    notes[account_id] = (
                        f"R6 v2: {len(raw_nav_dates)} raw Matrixport NAV date(s) "
                        f"from {min(raw_nav_dates).isoformat()} to "
                        f"{max(raw_nav_dates).isoformat()} "
                        + (
                            f"follow the admitted FX boundary "
                            f"{fx_last_admitted_date.isoformat()} and are "
                            "FX-unconverted"
                            if fx_unconverted
                            else "hold no units"
                        )
                        + "; no converted value is emitted for them, and "
                        "date.source_last_observation reports the last raw "
                        "NAV date while USD capital, flows and returns stay "
                        "not-derivable."
                    )
                    used.extend(matrixport_paths)
    return observations, flows, used, failures, notes


def metric(
    metric_id: str,
    entity: str,
    value: object,
    unit: str,
    inputs_used: list[str],
    as_of: str = CUTOFF.isoformat(),
    window: str | None = None,
    notes: str = "",
) -> dict[str, object]:
    result: dict[str, object] = {
        "metric_id": metric_id,
        "entity": entity,
        "as_of": as_of,
        "value": value,
        "unit": unit,
        "inputs_used": inputs_used,
        "notes": notes,
    }
    if window is not None:
        result["window"] = window
    return result


def add_not_derivable(
    output: dict[str, object],
    metric_id: str,
    entity: str,
    reason: str,
    window: str | None = None,
) -> None:
    row: dict[str, object] = {
        "metric_id": metric_id,
        "entity": entity,
        "reason": reason,
    }
    if window is not None:
        row["window"] = window
    output["not_derivable"].append(row)


def v3_authority_paths(manifest: dict[str, object], qe_root: Path) -> dict[str, Path]:
    """Resolve and semantically bind the selected R6/R8 authority records."""
    entries = manifest["inputs"]
    by_family = {
        str(entry["family"]): resolve_input_path(qe_root, str(entry["path"]))
        for entry in entries
        if isinstance(entry, dict) and str(entry.get("family")) in V3_AUTHORITY_FAMILIES
    }
    try:
        contract = json.loads(by_family["financial_source_contract"].read_text("utf-8"))
        policy = json.loads(by_family["provider_capital_policy"].read_text("utf-8"))
        receipt_a = json.loads(
            by_family["provider_capital_owner_receipt_a"].read_text("utf-8")
        )
        receipt_b = json.loads(
            by_family["provider_capital_owner_receipt_b"].read_text("utf-8")
        )
        receipt_b_response = json.loads(
            by_family["provider_capital_owner_receipt_b_response"].read_text("utf-8")
        )
        generation = json.loads(by_family["fx_generation_manifest"].read_text("utf-8"))
    except (OSError, json.JSONDecodeError, KeyError) as error:
        raise ManifestError(f"v3 authority is unreadable: {error}") from error
    invested = next(
        (
            item
            for item in contract.get("financial_outcomes", [])
            if item.get("outcome_id") == "INVESTED_CAPITAL"
        ),
        None,
    )
    if not isinstance(invested, dict):
        raise ManifestError("v3 contract lacks INVESTED_CAPITAL authority")
    calculation = invested.get("calculation_contract", {})
    contract_policy = calculation.get("provider_custody_perimeter")
    contract_receipt = calculation.get("owner_decision_receipts", {}).get(
        "PROVIDER_CUSTODY_PERIMETER"
    )
    if not isinstance(contract_policy, dict):
        raise ManifestError("v3 contract lacks the provider custody decision")
    decision_keys = frozenset(contract_policy)
    fund_scoped_keys = decision_keys & FUND_SCOPED_DECISION_KEYS
    if fund_scoped_keys and fund_scoped_keys != FUND_SCOPED_DECISION_KEYS:
        raise ManifestError(
            "v3 provider custody decision carries an incomplete fund-scoped key set"
        )
    if decision_keys != POLICY_DECISION_KEYS | fund_scoped_keys:
        raise ManifestError(
            "v3 provider custody decision does not carry exactly the approved "
            "decision key set"
        )
    if fund_scoped_keys:
        fund_scoped_providers_value = contract_policy["fund_scoped_providers"]
        if (
            not isinstance(fund_scoped_providers_value, list)
            or not fund_scoped_providers_value
            or any(
                not isinstance(provider, str)
                or not provider
                or provider != provider.upper()
                for provider in fund_scoped_providers_value
            )
            or len(set(fund_scoped_providers_value)) != len(fund_scoped_providers_value)
            or not str(contract_policy["fund_scoped_treatment"]).strip()
            or not str(contract_policy["fund_scoped_effect"]).strip()
            or not str(contract_policy["amends_approval_evidence"]).strip()
        ):
            raise ManifestError(
                "v3 provider custody decision fund-scoped amendment is malformed"
            )
    if contract_policy != policy:
        raise ManifestError("v3 provider custody policy differs from selected contract")
    if not isinstance(contract_receipt, dict) or any(
        contract_receipt.get(key) != receipt_b.get(key)
        for key in (
            "decision_id",
            "authority_issue",
            "policy_owner_issue",
            "evidence_url",
            "comment_id",
            "digest_evidence_url",
            "digest_comment_id",
            "payload_sha256",
        )
    ):
        raise ManifestError(
            "v3 provider custody receipt differs from selected contract"
        )
    receipt_b_body = str(receipt_b_response.get("body", ""))
    marker = "<!-- financial-mvp-owner-decision-receipts-v1 -->"
    if receipt_b_body.count(marker) != 1 or receipt_b_body.count("```json") != 1:
        raise ManifestError(
            "v3 owner receipt B response lacks one canonical machine record"
        )
    try:
        fenced = receipt_b_body.split("```json", 1)[1].split("```", 1)[0].strip()
        receipt_machine = json.loads(fenced)
    except (IndexError, json.JSONDecodeError) as error:
        raise ManifestError("v3 owner receipt B machine record is not JSON") from error
    canonical_payload = {"decision": contract_policy}
    canonical_digest = hashlib.sha256(
        (
            json.dumps(
                canonical_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    if (
        str(receipt_b_response.get("id")) != str(contract_receipt["digest_comment_id"])
        or str(contract_receipt["evidence_url"]) not in receipt_b_body
        or contract_receipt.get("payload_sha256") != canonical_digest
        or receipt_machine.get("decision_payload_sha256", {}).get(
            "PROVIDER_CUSTODY_PERIMETER"
        )
        != canonical_digest
        or receipt_machine.get("schema_version") != 1
        or receipt_machine.get("authority_issue") != 330
        or receipt_machine.get("policy_owner_issue") != 318
        or receipt_machine.get("policy_id") != "PORT-INVESTED-CAPITAL-1"
        or receipt_machine.get("approval_evidence_url")
        != contract_receipt["evidence_url"]
        or receipt_machine.get("approved_on") != receipt_b.get("approved_on")
    ):
        raise ManifestError(
            "v3 owner receipt B response does not bind the selected payload"
        )
    receipt_a_body = str(receipt_a.get("body", ""))
    if str(receipt_a.get("id")) != str(contract_receipt["comment_id"]) or any(
        phrase not in receipt_a_body for phrase in RECEIPT_A_REQUIRED_PHRASES
    ):
        raise ManifestError(
            "v3 owner receipt A does not bind the selected custody policy body"
        )
    coverage = generation.get("coverage", {})
    if (
        generation.get("generation_id")
        != "fd6096d8faafd6271ca210e992ca22ab89356674f513af3a642f4cdf71c4d0aa"
        or generation.get("as_of_date") != CUTOFF.isoformat()
        or coverage.get("observed_end") != CUTOFF.isoformat()
        or coverage.get("status") != "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    ):
        raise ManifestError("v3 selected FX generation does not cover the cutoff")
    artifacts = generation.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise ManifestError("v3 selected FX generation lacks artifact records")
    for family, artifact_id in (
        ("fx_usdc_usd_daily", "raw/yfinance-history.json"),
        ("fx_usdt_usd_daily", "raw/yfinance-history.json"),
        ("fx_daily_rates", "rates.csv"),
    ):
        selected = by_family[family]
        expected = next(
            (
                item
                for item in artifacts
                if isinstance(item, dict) and item.get("artifact_id") == artifact_id
            ),
            None,
        )
        if (
            not isinstance(expected, dict)
            or expected.get("sha256") != file_sha256(selected)
            or int(expected.get("size_bytes", -1)) != selected.stat().st_size
        ):
            raise ManifestError(
                "v3 selected FX authority is not bound to its generation artifact"
            )
    with by_family["fx_daily_rates"].open(encoding="utf-8-sig", newline="") as handle:
        cutoff_tickers = {
            row.get("ticker")
            for row in csv.DictReader(handle)
            if row.get("date") == CUTOFF.isoformat()
        }
    if cutoff_tickers != {"USDC-USD", "USDT-USD"}:
        raise ManifestError("v3 selected rates lack exact cutoff USDC/USDT closes")
    native = native_matrixport_input_paths(manifest, qe_root)
    if native:
        declared_by_basename: dict[str, set[str]] = defaultdict(set)
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                declared_by_basename[Path(entry["path"]).name].add(entry["path"])

        def declared_artifact(basename: str) -> Path:
            candidates = declared_by_basename.get(basename, set())
            if len(candidates) != 1:
                raise ManifestError(
                    f"native Matrixport artifact {basename!r} must have one declared path"
                )
            return resolve_input_path(qe_root, next(iter(candidates)))

        generation_path = native["matrixport_generation_manifest"]
        subscription_path = native["matrixport_subscription_details"]
        catalog_path = native["matrixport_generation_catalog"]
        coverage_path = native["matrixport_owner_flow_coverage"]
        request_path = native["matrixport_owner_flow_request_manifest"]
        artifact_paths = {
            "csv/nav_daily.csv": declared_artifact("nav_daily.csv"),
            "csv/wallet_deposits.csv": declared_artifact("wallet_deposits.csv"),
            "csv/wallet_bills.csv": declared_artifact("wallet_bills.csv"),
            "csv/wallet_currency_dispositions.csv": declared_artifact(
                "wallet_currency_dispositions.csv"
            ),
            "csv/subscription_details.csv": subscription_path,
            "csv/coverage.csv": coverage_path,
            "reports/request_manifest.jsonl": request_path,
            "normalized/generation-catalog.json": catalog_path,
        }
        try:
            native_generation = json.loads(generation_path.read_text("utf-8"))
            catalog = json.loads(catalog_path.read_text("utf-8"))
            with artifact_paths["csv/nav_daily.csv"].open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                nav_rows = list(csv.DictReader(handle))
            with artifact_paths["csv/wallet_deposits.csv"].open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                deposit_rows = list(csv.DictReader(handle))
            with subscription_path.open(encoding="utf-8-sig", newline="") as handle:
                subscriptions = list(csv.DictReader(handle))
            with coverage_path.open(encoding="utf-8-sig", newline="") as handle:
                coverage_rows = list(csv.DictReader(handle))
            request_lines = request_path.read_text(encoding="utf-8").splitlines()
        except (OSError, json.JSONDecodeError, UnicodeError) as error:
            raise ManifestError(
                f"native Matrixport source is unreadable: {error}"
            ) from error
        selection = manifest.get("generation_selection_evidence", {})
        matrixport_selection = (
            selection.get("matrixport") if isinstance(selection, dict) else None
        )
        if not isinstance(matrixport_selection, dict):
            raise ManifestError("native Matrixport generation selection is missing")
        generation_id = native_generation.get("generation_id")
        source_run_id = catalog.get("source_run_id")
        if (
            native_generation.get("manifest_schema_version") != 1
            or native_generation.get("provider_id") not in (None, "matrixport")
            or not isinstance(generation_id, str)
            or matrixport_selection.get("generation_id") != generation_id
            or not isinstance(source_run_id, str)
            or matrixport_selection.get("source_run_id") != source_run_id
            or matrixport_selection.get("generation_manifest_sha256")
            != file_sha256(generation_path)
            or matrixport_selection.get("catalog_sha256") != file_sha256(catalog_path)
            or catalog.get("catalog_schema_version") != 2
            or catalog.get("provider_id") != "matrixport"
            or not isinstance(catalog.get("observed_boundary_date"), str)
            or catalog["observed_boundary_date"] <= CUTOFF.isoformat()
            or matrixport_selection.get("qualification") != "STAGED_COMPLETE"
            or matrixport_selection.get("finalized_through") != CUTOFF.isoformat()
            or not matrixport_selection.get("finality_evidence_sha256_verified_V")
        ):
            raise ManifestError(
                "native Matrixport generation/catalog selection is not bound"
            )
        artifacts = native_generation.get("artifacts")
        if not isinstance(artifacts, list):
            raise ManifestError("native Matrixport generation lacks artifacts")
        expected_artifacts = {
            "csv/nav_daily.csv": ("matrixport-canonical-csv", "1"),
            "csv/wallet_deposits.csv": ("matrixport-canonical-csv", "1"),
            "csv/wallet_bills.csv": ("matrixport-canonical-csv", "1"),
            "csv/wallet_currency_dispositions.csv": ("matrixport-canonical-csv", "1"),
            "csv/subscription_details.csv": ("matrixport-canonical-csv", "1"),
            "csv/coverage.csv": ("matrixport-canonical-csv", "1"),
            "reports/request_manifest.jsonl": ("matrixport-acceptance-jsonl", "1"),
            "normalized/generation-catalog.json": (
                "matrixport-generation-catalog",
                "2",
            ),
        }
        for artifact_id, (schema_name, schema_version) in expected_artifacts.items():
            artifact = next(
                (
                    item
                    for item in artifacts
                    if isinstance(item, dict) and item.get("artifact_id") == artifact_id
                ),
                None,
            )
            selected_path = artifact_paths[artifact_id]
            schema = artifact.get("schema") if isinstance(artifact, dict) else None
            if (
                not isinstance(artifact, dict)
                or not isinstance(schema, dict)
                or schema.get("name") != schema_name
                or schema.get("version") != schema_version
                or artifact.get("sha256") != file_sha256(selected_path)
                or artifact.get("size_bytes") != selected_path.stat().st_size
            ):
                raise ManifestError(
                    f"native Matrixport artifact is not bound: {artifact_id}"
                )
        required_subscription_fields = {
            "actual_subscription_amount",
            "confirm_nav_time_utc",
            "order_id",
            "product_id",
            "subscription_amount",
            "subscription_currency",
            "subscription_time_utc",
            "subscription_share",
            "confirm_nav",
            "status",
            "source_endpoint",
        }
        if not subscriptions or not required_subscription_fields.issubset(
            subscriptions[0]
        ):
            raise ManifestError("native Matrixport subscription schema is incomplete")
        required_nav_fields = {"day_timestamp_utc", "nav", "product_id"}
        if not nav_rows or not required_nav_fields.issubset(nav_rows[0]):
            raise ManifestError("native Matrixport NAV schema is incomplete")
        for row in subscriptions:
            try:
                subscribed = datetime.fromisoformat(
                    row["subscription_time_utc"].replace("Z", "+00:00")
                )
                confirmed = datetime.fromisoformat(
                    row["confirm_nav_time_utc"].replace("Z", "+00:00")
                )
                if (
                    not row["order_id"]
                    or not row["product_id"]
                    or confirmed <= subscribed
                    or row["subscription_currency"] not in {"USDC", "USDT"}
                    or Decimal(row["actual_subscription_amount"]) <= 0
                    or Decimal(row["subscription_amount"]) <= 0
                    or Decimal(row["actual_subscription_amount"])
                    != Decimal(row["subscription_amount"])
                    or Decimal(row["subscription_share"]) <= 0
                    or Decimal(row["confirm_nav"]) <= 0
                    or row["source_endpoint"] != "/strategy/api/v2/order"
                    or row["status"] != "300"
                ):
                    raise ValueError("invalid confirmed subscription")
                recognition_date = confirmed.astimezone(timezone.utc).date()
                matching_navs = [
                    nav
                    for nav in nav_rows
                    if nav.get("product_id") == row["product_id"]
                    and datetime.fromisoformat(
                        nav["day_timestamp_utc"].replace("Z", "+00:00")
                    )
                    .astimezone(timezone.utc)
                    .date()
                    == recognition_date
                    and Decimal(nav["nav"]) == Decimal(row["confirm_nav"])
                ]
                if not matching_navs:
                    raise ValueError(
                        "confirmation NAV is not bound to recognition-day NAV"
                    )
            except (KeyError, ValueError, DecimalException) as error:
                raise ManifestError(
                    f"native Matrixport subscription is invalid: {error}"
                ) from error
        expected_endpoints = {
            "GET https://mapi.matrixport.com/mapi/v1/wallet/deposits": 1,
            "GET https://mapi.matrixport.com/mapi/v1/wallet/withdrawals": 0,
        }
        selected_coverage: dict[str, dict[str, str]] = {}
        for row in coverage_rows:
            endpoint = row.get("endpoint")
            if endpoint not in expected_endpoints:
                continue
            if endpoint in selected_coverage:
                raise ManifestError(
                    "native Matrixport owner-flow coverage has duplicate endpoints"
                )
            selected_coverage[endpoint] = row
        if set(selected_coverage) != set(expected_endpoints):
            raise ManifestError(
                "native Matrixport owner-flow coverage lacks required endpoints"
            )
        for endpoint, row in selected_coverage.items():
            try:
                raw_rows = json.loads(row["details_json"])["raw_rows"]
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                raise ManifestError(
                    "native Matrixport coverage details are malformed"
                ) from error
            if (
                endpoint not in expected_endpoints
                or row.get("status") != "complete_to_observed_boundary"
                or row.get("terminal_reason")
                != "structured_parameter_rejections_dispositioned"
                or not row.get("updated_at_utc")
            ):
                raise ManifestError(
                    "native Matrixport owner-flow coverage is incomplete"
                )
            if endpoint.endswith("/withdrawals") and raw_rows != 0:
                raise ManifestError("native Matrixport withdrawals are not empty")
        required_deposit_fields = {
            "transaction_id",
            "state",
            "currency",
            "amount",
            "created_at_utc",
        }
        if not deposit_rows or not required_deposit_fields.issubset(deposit_rows[0]):
            raise ManifestError("native Matrixport deposit schema is incomplete")
        deposit_ids: set[str] = set()
        for row in deposit_rows:
            try:
                transaction_id = row["transaction_id"]
                timestamp = datetime.fromisoformat(
                    row["created_at_utc"].replace("Z", "+00:00")
                )
                amount = Decimal(row["amount"])
                if (
                    not transaction_id
                    or transaction_id in deposit_ids
                    or row["state"] not in {"confirmed", "completed", "success"}
                    or row["currency"] not in {"USDC", "USDT"}
                    or timestamp.tzinfo is None
                    or amount <= 0
                ):
                    raise ValueError("invalid native owner deposit")
                deposit_ids.add(transaction_id)
            except (KeyError, ValueError, DecimalException) as error:
                raise ManifestError(
                    f"native Matrixport owner deposit is invalid: {error}"
                ) from error
        if json.loads(
            selected_coverage[
                "GET https://mapi.matrixport.com/mapi/v1/wallet/deposits"
            ]["details_json"]
        )["raw_rows"] != len(deposit_rows):
            raise ManifestError(
                "native Matrixport deposit coverage count is not source-bound"
            )
        if not request_lines:
            raise ManifestError("native Matrixport request manifest is empty")
        for line in request_lines:
            try:
                request = json.loads(line)
            except json.JSONDecodeError as error:
                raise ManifestError(
                    "native Matrixport request manifest is malformed"
                ) from error
            digest = (
                request.get("request_hash", request.get("request_sha256"))
                if isinstance(request, dict)
                else None
            )
            if (
                not isinstance(request, dict)
                or not request.get("request_id")
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ManifestError(
                    "native Matrixport request manifest lacks request pins"
                )
        by_family.update(native)
    return by_family


def fund_scoped_providers_from_authority(
    authority_family_paths: dict[str, Path],
) -> frozenset[str]:
    """The contract-declared fund-scoped provider list (policy, never code).

    v3_authority_paths has already bound and digest-checked the decision body;
    this re-reads the bound contract bytes.  An absent contract path (only
    possible in tests that stub the authority layer) yields an empty set.
    """
    contract_path = authority_family_paths.get("financial_source_contract")
    if contract_path is None:
        return frozenset()
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    invested = next(
        (
            item
            for item in contract.get("financial_outcomes", [])
            if isinstance(item, dict) and item.get("outcome_id") == "INVESTED_CAPITAL"
        ),
        None,
    )
    if not isinstance(invested, dict):
        return frozenset()
    policy = invested.get("calculation_contract", {}).get(
        "provider_custody_perimeter", {}
    )
    if not isinstance(policy, dict):
        return frozenset()
    providers = policy.get("fund_scoped_providers", [])
    if not isinstance(providers, list):
        return frozenset()
    return frozenset(str(provider).upper() for provider in providers)


def derive(
    qe_root: Path,
    inputs_manifest: dict[str, object] | None = None,
    sleeve_inputs: Path | None = None,
) -> dict[str, object]:
    input_paths = default_input_paths()
    schema_v3 = False
    authority_paths: list[Path] = []
    authority_family_paths: dict[str, Path] = {}
    convention_notes = [
        "ITD is the only window emitted: contracts/portfolio-performance-policy-v1.json has no window-id field beyond its synthetic cases.",
        "TWR uses contracts/portfolio-performance-policy-v1.json:twr_cases[case_id=date_only_end_of_day]: (ending value - same-date external flow) / prior value, compounded and not annualized.",
        "MWR uses contracts/portfolio-performance-policy-v1.json:mwr_cases.day_count=ACTUAL_365 and an annualized XIRR; investor cash flows are the inverse of portfolio external flows.",
        "R4/R5: flows use nav_recognition_date when present; internal transfers remain account and strategy flows and net only at the portfolio aggregate.",
        "R1/R2 as amended by the OWNER_539 ruling: every included crosswalk row is a funded member epoch; an account ITD spans its tiled funded epochs (predecessor assignments included), a strategy ITD includes funded predecessor epochs as entering/exiting members, and a declared funded_end closes only the epoch's account-level TWR on its last funded day while the account flows and MWR keep the full window through the cutoff (later withdrawals and credits are real flows, never dropped) and later credits are labelled post-liquidation income (dated, non-TWR, inside gain; never added a second time); the closed account's remaining cutoff NAV stays cutoff capital at account, strategy and portfolio level, and the strategy ITD keeps linking the closed member's post-liquidation value periods.",
        "Strategy TWR uses a union of admitted member valuation dates with no source-date fill and treats entering/exiting member capital as a flow at its admitted boundary.",
        "Matrixport USDT values and flows use the selected raw USDT/USD Close series; the per-row fx_provenance note records its candidate-generation caveat.",
        "Reserve discovery inspects provider-ledger.json:capital_anchors only; no reserve/bucket identifier permits a reserve balance to be inferred.",
    ]
    if inputs_manifest is not None:
        input_paths = manifest_input_paths(inputs_manifest, qe_root)
        schema_v3 = inputs_manifest.get("schema") == INPUT_MANIFEST_SCHEMA_V3
        if schema_v3:
            input_paths.update(native_matrixport_input_paths(inputs_manifest, qe_root))
            authority_family_paths = v3_authority_paths(inputs_manifest, qe_root)
            if "fx_daily_rates" not in authority_family_paths:
                raise ManifestError("v3 selected FX daily-rates authority is missing")
            authority_paths = list(authority_family_paths.values())
        convention_notes.append(
            "Inputs were selected by the supplied qe-cp-acceptance-inputs manifest; "
            "every declared sha256/bytes pin was verified fail-closed before "
            "derivation, including declared entries this derivation does not read."
        )
    fx_last_admitted_date = CUTOFF if schema_v3 else FX_LAST_ADMITTED_DATE
    if schema_v3:
        convention_notes.append(
            "R6 v3: selected raw USDT/USD closes are admitted through the "
            "2026-09-17 cutoff; Matrixport owner flows use event-date FX."
        )
    fund_scoped_providers = fund_scoped_providers_from_authority(authority_family_paths)
    if schema_v3 and fund_scoped_providers:
        convention_notes.append(
            "R8 as amended by the fund-scoped PROVIDER_CUSTODY_PERIMETER "
            "decision: for the contract-listed fund-scoped providers a "
            "strategy's invested capital is the gross sum of allocations to "
            "its own fund/product at fill dates counted once, account "
            "deposits and withdrawals are not the strategy's capital, no "
            "derived cash line is built, and the stablecoin conversion "
            "between deposit and subscription is a cost, not capital."
        )
    paths = [input_paths["ledger"], input_paths["crosswalk"], *authority_paths]
    account_manifest_path = input_paths.get("account_manifest")
    if account_manifest_path is not None:
        paths.append(account_manifest_path)
    absolute_paths = [qe_root / path for path in paths]
    for path in absolute_paths:
        if not path.is_file():
            raise FileNotFoundError(path)
    ledger = json.loads((qe_root / input_paths["ledger"]).read_text(encoding="utf-8"))
    crosswalk = json.loads(
        (qe_root / input_paths["crosswalk"]).read_text(encoding="utf-8")
    )
    account_manifest = None
    if account_manifest_path is not None:
        account_manifest = json.loads(
            (qe_root / account_manifest_path).read_text(encoding="utf-8")
        )
    cutoff_mismatches = [
        label
        for label, value in (
            ("ledger", ledger.get("as_of_date")),
            ("crosswalk", crosswalk.get("as_of_date")),
            ("crosswalk calculation", crosswalk.get("calculation_cutoff_date")),
            *(
                (
                    (
                        "declared account-strategy manifest calculation",
                        account_manifest.get("calculation_cutoff_date"),
                    ),
                )
                if account_manifest is not None
                else ()
            ),
        )
        if value != CUTOFF.isoformat()
    ]
    if cutoff_mismatches:
        return {
            "schema": "qe-cp-derivation/1",
            "derivation": "b",
            "model": "independent-standalone-python",
            "code_commit": code_commit(),
            "inputs": [
                {
                    "path": output_input_path(qe_root, path),
                    "sha256": file_sha256(qe_root / path),
                    "bytes": (qe_root / path).stat().st_size,
                }
                for path in paths
            ],
            "convention_notes": [
                "R7: a selected input cutoff differs from the fixed 2026-09-17 cutoff."
            ],
            "metrics": [],
            "not_derivable": [
                {
                    "metric_id": "acceptance.input.cutoff",
                    "entity": "portfolio",
                    "reason": "input cutoff mismatch: " + ", ".join(cutoff_mismatches),
                }
            ],
        }
    account_strategies, excluded_accounts, crosswalk_problems = current_crosswalk(
        crosswalk["rows"]
    )
    excluded_accounts.update(excluded_account_ids(crosswalk["rows"]))
    membership_starts = current_membership_starts(crosswalk["rows"])
    active_strategy_ids = active_included_strategy_ids(crosswalk["rows"])
    account_epochs = included_epochs_by_account(crosswalk["rows"])
    member_epochs = strategy_member_epochs(crosswalk["rows"])
    sleeve_result: dict[str, object] | None = None
    derived_epochs: list[Epoch] = []
    derived_by_strategy: dict[str, list[Epoch]] = {}
    dropped_epoch_keys: set[tuple[str, str, str, str | None]] = set()
    epoch_records: list[dict[str, object]] = []
    epoch_parents: dict[str, str] = {}
    if sleeve_inputs is not None:
        # SPEC-COMBINED: this derivation's own sleeve epochs are the authority
        # for WHICH epochs are derived; derive them before the manifest
        # binding so the effective perimeter can be checked, and reuse the
        # block for the sleeve section appended at the end.
        sleeve_result = _derive_sleeve_block(qe_root, sleeve_inputs)
        epoch_records = cast(list[dict[str, object]], sleeve_result["epoch_records"])
        epoch_parents = {
            str(record["account_id"]): str(record["parent_account_id"])
            for record in epoch_records
        }
        derived_epochs = [epoch_from_record(record) for record in epoch_records]
        for derived_epoch in derived_epochs:
            derived_by_strategy.setdefault(derived_epoch.strategy_id, []).append(
                derived_epoch
            )
    epoch_closures: dict[tuple[str, date], dict[str, object]] = {}
    if account_manifest is not None:
        if sleeve_result is not None:
            epoch_closures, dropped_epoch_keys = effective_perimeter_closures(
                account_manifest,
                crosswalk["rows"],
                derived_epochs,
                epoch_parents,
            )
        else:
            epoch_closures = declared_epoch_closures(
                account_manifest, crosswalk["rows"]
            )
    elif sleeve_result is not None:
        # No declared manifest to bind: a derived epoch of an included
        # strategy replaces its parent's baseline epoch for that strategy.
        for derived_epoch in derived_epochs:
            parent = epoch_parents[derived_epoch.account_id]
            dropped_epoch_keys.update(
                _epoch_identity_key(
                    row["account_id"],
                    row["strategy_id"],
                    row["effective_start"],
                    row.get("effective_end"),
                )
                for row in crosswalk["rows"]
                if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH"
                and str(row["account_id"]) == parent
                and str(row["strategy_id"]) == derived_epoch.strategy_id
            )
    crosswalk_capital_failures = (
        set(
            active_included_strategy_ids(
                [
                    row
                    for row in crosswalk["rows"]
                    if str(row["account_id"]) in crosswalk_problems
                ]
            )
        )
        if schema_v3
        else set()
    )
    providers = {
        str(row["account_id"]): str(row.get("provider", "")).upper()
        for row in crosswalk["rows"]
    }
    ledger_observations, observation_conflicts = latest_observations(ledger)
    raw_last_dates: dict[str, date] = {}
    (
        raw_observations,
        raw_flow_data,
        raw_paths,
        raw_failures,
        raw_notes,
    ) = raw_provider_data(
        qe_root,
        crosswalk["rows"],
        membership_starts,
        input_paths,
        raw_last_dates,
        fx_last_admitted_date,
        schema_v3,
    )
    custody_capital: dict[str, dict[str, Decimal]] = {}
    custody_capital_error: str | None = None
    custody_event_days: dict[str, dict[str, list[date]]] = {}
    matrixport_coverage_rows: list[dict[str, str]] | None = None
    matrixport_request_lines: list[str] | None = None
    matrixport_base_coverage_rows: list[dict[str, str]] | None = None
    matrixport_base_request_lines: list[str] | None = None
    if schema_v3:
        try:
            with (qe_root / input_paths["quantbase_activity"]).open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                quantbase_events = list(csv.DictReader(handle))
            with (qe_root / input_paths["matrixport_deposits"]).open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                matrixport_deposits = list(csv.DictReader(handle))
            with (qe_root / input_paths["matrixport_bills"]).open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                matrixport_bills = list(csv.DictReader(handle))
            matrixport_subscriptions = None
            if "matrixport_subscription_details" in input_paths:
                with (qe_root / input_paths["matrixport_subscription_details"]).open(
                    encoding="utf-8-sig", newline=""
                ) as handle:
                    matrixport_subscriptions = list(csv.DictReader(handle))
            rates_authority = authority_family_paths.get("fx_daily_rates")
            selected_fx_covers_custody_event_dates(
                rates_authority,
                matrixport_deposits,
                matrixport_bills,
                matrixport_subscriptions,
            )
            custody_capital = raw_custody_capital(
                quantbase_events,
                matrixport_deposits,
                matrixport_bills,
                {
                    "USDC": stablecoin_usd_fx_rates(
                        qe_root / input_paths["fx"], "USDC-USD", CUTOFF
                    ),
                    "USDT": stablecoin_usd_fx_rates(
                        qe_root / input_paths["fx"], "USDT-USD", CUTOFF
                    ),
                },
                matrixport_subscriptions,
                custody_event_days,
                fund_scoped_strategy_ids(fund_scoped_providers),
            )
            if "matrixport_owner_flow_coverage" in input_paths:
                with (qe_root / input_paths["matrixport_owner_flow_coverage"]).open(
                    encoding="utf-8-sig", newline=""
                ) as handle:
                    matrixport_coverage_rows = list(csv.DictReader(handle))
                matrixport_request_lines = (
                    (qe_root / input_paths["matrixport_owner_flow_request_manifest"])
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
            if "matrixport_owner_flow_base_coverage" in input_paths:
                with (
                    qe_root / input_paths["matrixport_owner_flow_base_coverage"]
                ).open(encoding="utf-8-sig", newline="") as handle:
                    matrixport_base_coverage_rows = list(csv.DictReader(handle))
                matrixport_base_request_lines = (
                    (
                        qe_root
                        / input_paths["matrixport_owner_flow_base_request_manifest"]
                    )
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            custody_capital_error = f"R8 raw custody capital is not derivable: {error}"
    if schema_v3:
        for account_id in raw_observations:
            # R6 raw observations are authoritative.  Conflicts in the normalized
            # ledger are cross-check evidence, not a reason to suppress raw output.
            observation_conflicts.discard(account_id)
    observations = {**ledger_observations, **raw_observations}
    for account_id, failure in raw_failures.items():
        if "capital.account.cutoff" in failure["metrics"]:
            # R6 fails closed: ledger observations are cross-checks only and
            # must never substitute for an unusable raw capture.
            observations.pop(account_id, None)
    paths.extend(raw_paths)
    inputs = [
        {
            "path": output_input_path(qe_root, path),
            "sha256": file_sha256(qe_root / path),
            "bytes": (qe_root / path).stat().st_size,
        }
        for path in paths
    ]
    used_inputs = [output_input_path(qe_root, path) for path in paths]
    output: dict[str, object] = {
        "schema": "qe-cp-derivation/1",
        "derivation": "b",
        "model": "independent-standalone-python",
        "code_commit": code_commit(),
        "inputs": inputs,
        "convention_notes": convention_notes,
        "metrics": [],
        "not_derivable": [],
    }
    metrics: list[dict[str, object]] = output["metrics"]
    account_values: dict[str, Decimal] = {}
    account_value_dates: dict[str, date] = {}
    account_flow_data: dict[str, FlowData | None] = {}
    account_window_observations: dict[str, list[tuple[date, Decimal]]] = {}

    for account_id, reason in sorted(crosswalk_problems.items()):
        for metric_id in (
            "capital.account.cutoff",
            "date.source_last_observation",
            "flows.account.net",
            "flows.account.count",
            "twr.account",
            "mwr.account",
        ):
            add_not_derivable(
                output,
                metric_id,
                account_id,
                reason,
                "ITD" if metric_id.startswith(("flows", "twr", "mwr")) else None,
            )

    for account_id in sorted(account_strategies):
        provider = providers.get(account_id, "")
        note_parts = []
        if provider == "MATRIXPORT":
            note_parts.append(MATRIXPORT_FX_PROVENANCE)
        tail_note = r6_ledger_tail_note(
            provider,
            ledger_observations.get(account_id, []),
            raw_observations.get(account_id, []),
        )
        if tail_note:
            note_parts.append(tail_note)
        raw_note = raw_notes.get(account_id)
        if raw_note:
            note_parts.append(raw_note)
        account_notes = " ".join(note_parts)
        failure = raw_failures.get(account_id)
        if failure is not None and "capital.account.cutoff" in failure["metrics"]:
            for metric_id in failure["metrics"]:
                add_not_derivable(
                    output,
                    metric_id,
                    account_id,
                    str(failure["reason"]),
                    "ITD" if metric_id.startswith(("flows", "twr", "mwr")) else None,
                )
            if account_id in raw_last_dates:
                # R6 v2: raw NAV coverage is retained independently of USD
                # conversion; emit the verified raw date even when every
                # converted metric stays not-derivable.
                metrics.append(
                    metric(
                        "date.source_last_observation",
                        account_id,
                        raw_last_dates[account_id].isoformat(),
                        "date",
                        used_inputs,
                        raw_last_dates[account_id].isoformat(),
                        notes=account_notes,
                    )
                )
            continue
        if account_id in observation_conflicts:
            for metric_id in (
                "capital.account.cutoff",
                "date.source_last_observation",
                "flows.account.net",
                "flows.account.count",
                "twr.account",
                "mwr.account",
            ):
                add_not_derivable(
                    output,
                    metric_id,
                    account_id,
                    "conflicting available USD valuations on one observation date",
                    "ITD" if metric_id.startswith(("flows", "twr", "mwr")) else None,
                )
            continue
        epochs = account_epochs.get(account_id) or [
            (membership_starts[account_id], None, account_strategies[account_id])
        ]
        epoch_problem = epoch_window_problem(epochs)
        closure: dict[str, object] | None = None
        for epoch_start, _epoch_end, _epoch_strategy in epochs:
            candidate = epoch_closures.get((account_id, epoch_start))
            if candidate is not None:
                closure = candidate
        performance_end = (
            cast(date, closure["funded_end"]) if closure is not None else None
        )
        # The funded performance window spans every included epoch of the
        # account (the OWNER_539 predecessor ruling). A declared funded_end
        # closes only the epoch's account-level TWR on its last funded day;
        # the account flows and MWR run the full window through the cutoff
        # (later withdrawals and credits are real flows, never dropped), and
        # cutoff capital uses the full observation window (a liquidated
        # account's remaining NAV is really held money).
        # A broken epoch tiling falls back to the current interval for the
        # capital/date facts and fails the flow/return metrics closed below.
        window_start = membership_starts[account_id] if epoch_problem else epochs[0][0]
        all_observations = observations.get(account_id, [])
        account_observations = window_observations(all_observations, window_start, None)
        twr_observations = (
            window_observations(all_observations, window_start, performance_end)
            if performance_end is not None
            else account_observations
        )
        account_window_observations[account_id] = account_observations
        closure_failure: str | None = None
        if closure is not None:
            raw_closure = post_liquidation_income_from_raw(
                observations.get(account_id, []),
                ledger["flows"],
                account_id,
                cast(date, closure["funded_end"]),
            )
            if isinstance(raw_closure, str):
                closure_failure = (
                    "the declared funded_end closure cannot be reconciled to the "
                    f"raw bytes: {raw_closure}"
                )
            else:
                close_value, raw_income = raw_closure
                declared_income = closure.get("post_liquidation_income")
                if not isinstance(declared_income, Decimal) or declared_income.quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                ) != raw_income.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
                    closure_failure = (
                        "the declared post_liquidation_income "
                        f"{declared_income} does not reconcile to the raw bytes "
                        f"after the declared funded_end (derived {raw_income})"
                    )
                else:
                    residual = (
                        money(all_observations[-1][1]) if all_observations else "0.00"
                    )
                    note_parts.append(
                        f"R1/#539: the funded epoch's account-level TWR closed "
                        f"on its last funded day "
                        f"{cast(date, closure['funded_end']).isoformat()} while "
                        "the account flows and MWR keep the full window through "
                        "the cutoff (later withdrawals and credits are real "
                        f"flows, never dropped); the {money(raw_income)} of "
                        "income credited after liquidation (reconciled to the "
                        "declared post_liquidation_income) is labelled "
                        "post-liquidation income (dated, non-TWR, inside gain), "
                        f"never added a second time; the residual cutoff NAV "
                        f"{residual} is really held money and stays cutoff "
                        "capital at account, strategy and portfolio level, and "
                        "the strategy ITD keeps linking this member's "
                        "post-liquidation value periods."
                    )
                    account_notes = " ".join(note_parts)
        if not account_observations:
            for metric_id in (
                "capital.account.cutoff",
                "date.source_last_observation",
                "flows.account.net",
                "flows.account.count",
                "twr.account",
                "mwr.account",
            ):
                add_not_derivable(
                    output,
                    metric_id,
                    account_id,
                    "no available USD observation on or before cutoff",
                    "ITD" if metric_id.startswith(("flows", "twr", "mwr")) else None,
                )
            continue
        end_date, end_value = account_observations[-1]
        # USD-converted value rows are dated by the admitted value basis (the
        # last FX-converted/ledger-admitted observation), never the raw NAV
        # date past an FX boundary; only date.source_last_observation carries
        # the raw date.
        if closure is not None and closure_failure is None:
            # Liquidated before the cutoff: the account-level TWR window
            # closed at funded_end, but the money is really held — the
            # cutoff capital is the account's actual cutoff NAV (the labelled
            # post-liquidation income is inside that NAV, never added twice).
            cutoff_date, cutoff_value = all_observations[-1]
            account_values[account_id] = cutoff_value
            account_value_dates[account_id] = cutoff_date
        else:
            account_values[account_id] = end_value
            account_value_dates[account_id] = end_date
        source_date = raw_last_dates.get(
            account_id,
            all_observations[-1][0]
            if closure is not None and all_observations
            else end_date,
        )
        if closure_failure is not None:
            add_not_derivable(
                output, "capital.account.cutoff", account_id, closure_failure
            )
            account_values.pop(account_id, None)
            account_value_dates.pop(account_id, None)
        else:
            metrics.append(
                metric(
                    "capital.account.cutoff",
                    account_id,
                    money(account_values[account_id]),
                    "USD",
                    used_inputs,
                    account_value_dates[account_id].isoformat(),
                    notes=account_notes,
                )
            )
        metrics.append(
            metric(
                "date.source_last_observation",
                account_id,
                source_date.isoformat(),
                "date",
                used_inputs,
                source_date.isoformat(),
                notes=account_notes,
            )
        )
        if closure_failure is not None:
            account_flow_data[account_id] = None
            for metric_id in (
                "flows.account.net",
                "flows.account.count",
                "twr.account",
                "mwr.account",
            ):
                add_not_derivable(output, metric_id, account_id, closure_failure, "ITD")
            continue
        if epoch_problem is not None:
            account_flow_data[account_id] = None
            for metric_id in (
                "flows.account.net",
                "flows.account.count",
                "twr.account",
                "mwr.account",
            ):
                add_not_derivable(output, metric_id, account_id, epoch_problem, "ITD")
            continue
        if failure is not None and "flows.account.net" in failure["metrics"]:
            account_flow_data[account_id] = None
            for metric_id in failure["metrics"]:
                add_not_derivable(
                    output, metric_id, account_id, str(failure["reason"]), "ITD"
                )
            continue
        if account_id in raw_flow_data:
            flow_data = raw_flow_data[account_id]
            flow_problem = None
        else:
            # The aggregation interval is aligned with the emitted as_of
            # (the last valuation date): a flow recognized after it belongs
            # to a later cutoff period and must never be dated at end_date.
            cash_flows, flow_count, flow_problem = classified_external_flows(
                ledger["flows"], account_id, account_observations[0][0], end_date
            )
            flow_data = FlowData(
                cash_flows=cash_flows,
                twr_end_flows=cash_flows,
                twr_beginning_flows={},
                count=flow_count,
            )
        if flow_problem is not None:
            account_flow_data[account_id] = None
            for metric_id in (
                "flows.account.net",
                "flows.account.count",
                "twr.account",
                "mwr.account",
            ):
                add_not_derivable(output, metric_id, account_id, flow_problem, "ITD")
            continue
        account_flow_data[account_id] = flow_data
        metrics.append(
            metric(
                "flows.account.net",
                account_id,
                money(sum(flow_data.cash_flows.values(), Decimal("0"))),
                "USD",
                used_inputs,
                end_date.isoformat(),
                window="ITD",
                notes=account_notes,
            )
        )
        metrics.append(
            metric(
                "flows.account.count",
                account_id,
                str(flow_data.count),
                "count",
                used_inputs,
                end_date.isoformat(),
                window="ITD",
                notes=account_notes,
            )
        )
        # A declared funded_end closes only the TWR window: the return stops
        # on the last funded day while the flows above and the MWR below keep
        # the full window through the cutoff.
        twr_as_of = end_date
        twr_end_flows = flow_data.twr_end_flows
        twr_beginning_flows = flow_data.twr_beginning_flows
        if performance_end is not None:
            twr_as_of = twr_observations[-1][0]
            twr_end_flows = {
                occurred_on: amount
                for occurred_on, amount in flow_data.twr_end_flows.items()
                if occurred_on <= twr_as_of
            }
            twr_beginning_flows = {
                occurred_on: amount
                for occurred_on, amount in flow_data.twr_beginning_flows.items()
                if occurred_on <= twr_as_of
            }
        try:
            twr = time_weighted_return(
                twr_observations,
                twr_end_flows,
                twr_beginning_flows,
            )
        except ValueError as error:
            add_not_derivable(output, "twr.account", account_id, str(error), "ITD")
        else:
            metrics.append(
                metric(
                    "twr.account",
                    account_id,
                    ratio(twr),
                    "ratio",
                    used_inputs,
                    twr_as_of.isoformat(),
                    window="ITD",
                    notes=account_notes,
                )
            )
        if end_date < CUTOFF and any(
            occurred_on > end_date for occurred_on in flow_data.cash_flows
        ):
            mwr = None
        else:
            mwr_flows = [(account_observations[0][0], -account_observations[0][1])]
            mwr_flows.extend(
                (occurred_on, -amount)
                for occurred_on, amount in flow_data.cash_flows.items()
            )
            mwr_flows.append((end_date, end_value))
            mwr = xirr(mwr_flows)
        if mwr is None:
            add_not_derivable(
                output,
                "mwr.account",
                account_id,
                "ACTUAL_365 XIRR uniqueness or bracket not proved",
                "ITD",
            )
        else:
            metrics.append(
                metric(
                    "mwr.account",
                    account_id,
                    ratio(mwr),
                    "ratio",
                    used_inputs,
                    end_date.isoformat(),
                    window="ITD",
                    notes=account_notes,
                )
            )

    # Retained/excluded accounts are outside performance membership, but their
    # source-cutoff capital and observation date remain required output facts.
    for account_id in sorted(excluded_accounts):
        provider = providers.get(account_id, "")
        note_parts = []
        if provider == "MATRIXPORT":
            note_parts.append(MATRIXPORT_FX_PROVENANCE)
        tail_note = r6_ledger_tail_note(
            provider,
            ledger_observations.get(account_id, []),
            raw_observations.get(account_id, []),
        )
        if tail_note:
            note_parts.append(tail_note)
        raw_note = raw_notes.get(account_id)
        if raw_note:
            note_parts.append(raw_note)
        account_notes = " ".join(note_parts)
        if account_id in observation_conflicts:
            reason = "conflicting available USD valuations on one observation date"
            for metric_id in (
                "capital.account.cutoff",
                "date.source_last_observation",
                "flows.account.net",
                "flows.account.count",
                "twr.account",
                "mwr.account",
            ):
                add_not_derivable(
                    output,
                    metric_id,
                    account_id,
                    reason,
                    "ITD" if metric_id.startswith(("flows", "twr", "mwr")) else None,
                )
            continue
        account_observations = observations.get(account_id, [])
        if not account_observations:
            reason = "no available USD observation on or before cutoff"
            for metric_id in (
                "capital.account.cutoff",
                "date.source_last_observation",
                "flows.account.net",
                "flows.account.count",
                "twr.account",
                "mwr.account",
            ):
                add_not_derivable(
                    output,
                    metric_id,
                    account_id,
                    reason,
                    "ITD" if metric_id.startswith(("flows", "twr", "mwr")) else None,
                )
            continue
        end_date, end_value = account_observations[-1]
        source_date = raw_last_dates.get(account_id, end_date)
        metrics.append(
            metric(
                "capital.account.cutoff",
                account_id,
                money(end_value),
                "USD",
                used_inputs,
                end_date.isoformat(),
                notes=account_notes,
            )
        )
        metrics.append(
            metric(
                "date.source_last_observation",
                account_id,
                source_date.isoformat(),
                "date",
                used_inputs,
                source_date.isoformat(),
                notes=account_notes,
            )
        )
        for metric_id in (
            "flows.account.net",
            "flows.account.count",
            "twr.account",
            "mwr.account",
        ):
            add_not_derivable(
                output,
                metric_id,
                account_id,
                "excluded/retained account has no funded-membership performance interval",
                "ITD",
            )

    excluded_values: dict[str, Decimal] = {}
    missing_excluded = []
    for account_id in sorted(excluded_accounts):
        account_observations = observations.get(account_id, [])
        if account_observations:
            excluded_values[account_id] = account_observations[-1][1]
        else:
            missing_excluded.append(account_id)
    aggregate = aggregate_capital(
        {**account_values, **excluded_values},
        account_strategies,
        excluded_accounts,
        Decimal("0"),
    )
    missing_included = set(account_strategies).difference(account_values)
    custody_failures: set[str] = set()
    fund_scoped = fund_scoped_strategy_ids(fund_scoped_providers)
    if schema_v3:
        for strategy_id in ("volatis-strategy", "robuxio-crypto"):
            members = {
                account_id
                for account_id, assigned in account_strategies.items()
                if assigned == strategy_id
            }
            custody = custody_capital.get(strategy_id)
            if (
                custody_capital_error is not None
                or custody is None
                or strategy_id in crosswalk_capital_failures
                or len(members) != 1
                or members.intersection(missing_included)
            ):
                reason = custody_capital_error or (
                    "R8 raw custody funding is missing or the provider account mapping is ambiguous"
                )
                add_not_derivable(
                    output, "invested_capital.strategy.cutoff", strategy_id, reason
                )
                if strategy_id in fund_scoped:
                    # The strategy's capital is its fund NAV; a custody-event
                    # failure does not touch it, and the cash-line metrics are
                    # already meaningless under the fund-scoped treatment.
                    add_not_derivable(
                        output,
                        "capital.strategy_cash.cutoff",
                        strategy_id,
                        FUND_SCOPED_NO_CASH_LINE_REASON,
                    )
                    add_not_derivable(
                        output,
                        COVERAGE_METRIC_ID,
                        coverage_entity(strategy_id, members),
                        FUND_SCOPED_NO_COVERAGE_REASON,
                    )
                else:
                    custody_failures.add(strategy_id)
                    for metric_id in (
                        "capital.strategy_cash.cutoff",
                        "capital.strategy.cutoff",
                    ):
                        add_not_derivable(output, metric_id, strategy_id, reason)
                    add_not_derivable(
                        output,
                        COVERAGE_METRIC_ID,
                        coverage_entity(strategy_id, members),
                        reason,
                    )
                continue
            if strategy_id in fund_scoped:
                # Amended PROVIDER_CUSTODY_PERIMETER (fund_scoped_providers):
                # invested capital is the gross fund/product allocations at
                # fill dates, each counted once; account deposits/withdrawals
                # are not the strategy's capital; no derived cash line exists.
                metrics.append(
                    metric(
                        "invested_capital.strategy.cutoff",
                        strategy_id,
                        money(custody["allocation"]),
                        "USD",
                        used_inputs,
                        notes=(
                            "R8 fund-scoped (amended PROVIDER_CUSTODY_PERIMETER): "
                            "gross allocations to the strategy's own fund/product "
                            "at fill dates, each counted once, at event-date FX; "
                            "provider account deposits and withdrawals are not "
                            "the strategy's capital."
                        ),
                    )
                )
                add_not_derivable(
                    output,
                    "capital.strategy_cash.cutoff",
                    strategy_id,
                    FUND_SCOPED_NO_CASH_LINE_REASON,
                )
                add_not_derivable(
                    output,
                    COVERAGE_METRIC_ID,
                    coverage_entity(strategy_id, members),
                    FUND_SCOPED_NO_COVERAGE_REASON,
                )
                continue
            aggregate["strategies"][strategy_id] += custody["cash"]
            aggregate["systematic"] += custody["cash"]
            metrics.append(
                metric(
                    "invested_capital.strategy.cutoff",
                    strategy_id,
                    money(custody["invested"]),
                    "USD",
                    used_inputs,
                    notes="R8 derived from raw ACCOUNT_ONLY owner funding at event-date FX.",
                )
            )
            metrics.append(
                metric(
                    "capital.strategy_cash.cutoff",
                    strategy_id,
                    money(custody["cash"]),
                    "USD",
                    used_inputs,
                    notes="R8 derived raw provider-account cash; not an observed NAV.",
                )
            )
            selection = (
                inputs_manifest.get("generation_selection_evidence")
                if inputs_manifest is not None
                else None
            )
            coverage = custody_coverage_end(
                strategy_id,
                custody_event_days.get(strategy_id, {}),
                ledger,
                selection,
                matrixport_coverage_rows,
                matrixport_request_lines,
                base_coverage_rows=matrixport_base_coverage_rows,
                base_request_lines=matrixport_base_request_lines,
            )
            if isinstance(coverage, str):
                add_not_derivable(
                    output,
                    COVERAGE_METRIC_ID,
                    coverage_entity(strategy_id, members),
                    coverage,
                )
            else:
                metrics.append(
                    metric(
                        COVERAGE_METRIC_ID,
                        coverage_entity(strategy_id, members),
                        coverage.isoformat(),
                        "date",
                        used_inputs,
                        notes=(
                            "R8 unclamped coverage end from the raw captures: the "
                            "smaller of the owner-flow and allocation boundaries; "
                            "a list endpoint with a natural terminal and requests "
                            "that reach the date extends a boundary, a detail "
                            "fetch or capture time never does."
                        ),
                    )
                )
    for strategy_id in active_strategy_ids:
        members = {
            account_id
            for account_id, assigned in account_strategies.items()
            if assigned == strategy_id
        }
        if strategy_id in custody_failures:
            continue
        if strategy_id in crosswalk_capital_failures:
            add_not_derivable(
                output,
                "capital.strategy.cutoff",
                strategy_id,
                "one or more included account mappings is ambiguous at cutoff",
            )
        elif not members:
            add_not_derivable(
                output,
                "capital.strategy.cutoff",
                strategy_id,
                "no included member account remains after crosswalk conflict",
            )
        elif members.intersection(missing_included):
            add_not_derivable(
                output,
                "capital.strategy.cutoff",
                strategy_id,
                "one or more included member accounts lacks a cutoff value",
            )
        else:
            metrics.append(
                metric(
                    "capital.strategy.cutoff",
                    strategy_id,
                    money(aggregate["strategies"][strategy_id]),
                    "USD",
                    used_inputs,
                    max(
                        account_value_dates[member]
                        for member in members
                        if member in account_value_dates
                    ).isoformat(),
                )
            )
    if missing_included or custody_failures or crosswalk_capital_failures:
        add_not_derivable(
            output,
            "capital.portfolio.systematic.cutoff",
            "portfolio",
            "one or more included account mappings is ambiguous at cutoff"
            if crosswalk_capital_failures
            else "one or more included accounts lacks a cutoff value or required custody cash is not derivable",
        )
    else:
        metrics.append(
            metric(
                "capital.portfolio.systematic.cutoff",
                "portfolio",
                money(aggregate["systematic"]),
                "USD",
                used_inputs,
            )
        )
    if schema_v3:
        if (
            missing_included
            or custody_failures
            or crosswalk_capital_failures
            or aggregate["systematic"] <= 0
        ):
            reason = (
                "strategy weights require every included cutoff capital value and "
                "a positive systematic total"
            )
            for strategy_id in active_strategy_ids:
                add_not_derivable(output, "weight.strategy.cutoff", strategy_id, reason)
        else:
            for strategy_id in active_strategy_ids:
                metrics.append(
                    metric(
                        "weight.strategy.cutoff",
                        strategy_id,
                        ratio(
                            aggregate["strategies"][strategy_id]
                            / aggregate["systematic"]
                        ),
                        "ratio",
                        used_inputs,
                    )
                )
    if missing_excluded:
        add_not_derivable(
            output,
            "capital.retained_excluded.cutoff",
            "portfolio",
            "excluded/retained accounts lack cutoff values: "
            + ", ".join(missing_excluded),
        )
    else:
        metrics.append(
            metric(
                "capital.retained_excluded.cutoff",
                "portfolio",
                money(aggregate["retained_excluded"]),
                "USD",
                used_inputs,
            )
        )
    reserve_identified = has_reserve_or_bucket_identifier(ledger["capital_anchors"])
    reserve_reason = (
        "provider-ledger.json:capital_anchors was inspected; a reserve/bucket identifier was found but no cutoff reserve balance is supplied"
        if reserve_identified
        else "provider-ledger.json:capital_anchors was inspected; it does not identify a reserve/bucket entity"
    )
    add_not_derivable(
        output,
        "capital.reserve.cutoff",
        "reserve-capital" if schema_v3 else "reserve",
        reserve_reason,
    )
    add_not_derivable(
        output,
        "capital.portfolio.liquid.cutoff",
        "portfolio",
        "liquid capital requires the non-derivable reserve balance",
    )
    metrics.append(
        metric(
            "universe.active_strategy_ids",
            "portfolio",
            active_strategy_ids,
            "list",
            used_inputs,
        )
    )
    metrics.append(
        metric(
            "universe.active_strategy_count",
            "portfolio",
            str(len(active_strategy_ids)),
            "count",
            used_inputs,
        )
    )

    for strategy_id in active_strategy_ids:
        members = sorted(
            account_id
            for account_id, assigned in account_strategies.items()
            if assigned == strategy_id
        )
        epochs = member_epochs.get(strategy_id, [])
        if not members or not epochs:
            add_not_derivable(
                output,
                "twr.strategy",
                strategy_id,
                "no included member account remains after crosswalk conflict",
                "ITD",
            )
            add_not_derivable(
                output,
                "mwr.strategy",
                strategy_id,
                "no included member account remains after crosswalk conflict",
                "ITD",
            )
            continue
        if any(account_id not in account_values for account_id in members):
            add_not_derivable(
                output,
                "twr.strategy",
                strategy_id,
                "a member account lacks a cutoff value",
                "ITD",
            )
            add_not_derivable(
                output,
                "mwr.strategy",
                strategy_id,
                "a member account lacks a cutoff value",
                "ITD",
            )
            continue
        if any(account_flow_data.get(account_id) is None for account_id in members):
            add_not_derivable(
                output,
                "twr.strategy",
                strategy_id,
                "a member account has an unclassified flow",
                "ITD",
            )
            add_not_derivable(
                output,
                "mwr.strategy",
                strategy_id,
                "a member account has an unclassified flow",
                "ITD",
            )
            continue
        # Every funded member epoch enters the strategy series: current
        # memberships and funded predecessor epochs alike, each bounded by its
        # own crosswalk interval. A declared funded_end closes only the
        # account-level TWR window; the strategy still holds the
        # closed member's value, so the member's post-liquidation value
        # periods stay linked into the strategy series (the strategy-ledger
        # path's standing rule).
        member_windows: dict[tuple[str, date], list[tuple[date, Decimal]]] = {}
        member_flow_data: dict[tuple[str, date], FlowData] = {}
        member_problem: str | None = None
        for account_id, epoch_start, epoch_end in epochs:
            if account_id in crosswalk_problems:
                member_problem = (
                    "a funded member epoch belongs to an account with an "
                    "ambiguous crosswalk mapping"
                )
                break
            end_cap = CUTOFF
            if epoch_end is not None and epoch_end < end_cap:
                end_cap = epoch_end
            member_key = (account_id, epoch_start)
            if account_id in raw_flow_data:
                # R6 raw-capture accounts carry exactly one epoch; the account
                # window and raw flow data are the epoch window and flows.
                if len(account_epochs.get(account_id, [])) != 1:
                    member_problem = (
                        "an R6 raw-capture account carries more than one funded "
                        "epoch; per-epoch raw flow slicing is not defined"
                    )
                    break
                member_windows[member_key] = account_window_observations[account_id]
                member_flow_data[member_key] = cast(
                    FlowData, account_flow_data[account_id]
                )
                continue
            epoch_window = window_observations(
                observations.get(account_id, []), epoch_start, end_cap
            )
            if not epoch_window:
                member_problem = (
                    "a funded member epoch has no admitted observation in its interval"
                )
                break
            cash_flows, flow_count, flow_problem = classified_external_flows(
                ledger["flows"], account_id, epoch_window[0][0], epoch_window[-1][0]
            )
            if flow_problem is not None:
                member_problem = flow_problem
                break
            member_windows[member_key] = epoch_window
            member_flow_data[member_key] = FlowData(
                cash_flows=cash_flows,
                twr_end_flows=cash_flows,
                twr_beginning_flows={},
                count=flow_count,
            )
        if member_problem is not None:
            add_not_derivable(
                output, "twr.strategy", strategy_id, member_problem, "ITD"
            )
            add_not_derivable(
                output, "mwr.strategy", strategy_id, member_problem, "ITD"
            )
            continue
        strategy_observations, strategy_flows = composite_series(
            member_windows,
            {
                member_key: flow_data.twr_end_flows
                for member_key, flow_data in member_flow_data.items()
            },
        )
        strategy_beginning_flows: dict[date, Decimal] = defaultdict(Decimal)
        for flow_data in member_flow_data.values():
            for occurred_on, amount in flow_data.twr_beginning_flows.items():
                strategy_beginning_flows[occurred_on] += amount
        if len(strategy_observations) < 2:
            add_not_derivable(
                output,
                "twr.strategy",
                strategy_id,
                "fewer than two admitted strategy valuation dates",
                "ITD",
            )
        else:
            try:
                metrics.append(
                    metric(
                        "twr.strategy",
                        strategy_id,
                        ratio(
                            time_weighted_return(
                                strategy_observations,
                                strategy_flows,
                                dict(strategy_beginning_flows),
                            )
                        ),
                        "ratio",
                        used_inputs,
                        strategy_observations[-1][0].isoformat(),
                        window="ITD",
                    )
                )
            except ValueError as error:
                add_not_derivable(
                    output, "twr.strategy", strategy_id, str(error), "ITD"
                )
        # SPEC-COMBINED M2: the strategy MWR pools the funded-epoch cash flows
        # of the strategy's epochs in the EFFECTIVE perimeter, with the
        # terminal value the sum of those epochs' values at the cutoff.  A
        # dropped baseline epoch contributes nothing; a derived epoch
        # contributes its sleeve-section flows (INTERNAL_IN legs are
        # contributions, INTERNAL_OUT legs withdrawals) and its closing value.
        strategy_derived = derived_by_strategy.get(strategy_id, [])
        dropped_members = {
            (epoch_account, epoch_start)
            for epoch_account, epoch_start, epoch_end in epochs
            if _epoch_identity_key(
                epoch_account,
                strategy_id,
                epoch_start.isoformat(),
                epoch_end.isoformat() if epoch_end is not None else None,
            )
            in dropped_epoch_keys
        }
        mwr_flows: list[tuple[date, Decimal]] = []
        for member_key in member_windows:
            if member_key in dropped_members:
                continue
            epoch_window = member_windows[member_key]
            flow_data = member_flow_data[member_key]
            mwr_flows.append((epoch_window[0][0], -epoch_window[0][1]))
            mwr_flows.extend(
                (occurred_on, -amount)
                for occurred_on, amount in flow_data.cash_flows.items()
            )
            mwr_flows.append((epoch_window[-1][0], epoch_window[-1][1]))
        for derived_epoch in strategy_derived:
            first_day = derived_epoch.nav[0][0]
            last_day = derived_epoch.nav[-1][0]
            derived_flows: dict[date, Decimal] = {}
            for leg in derived_epoch.legs:
                if first_day < leg.recognition_day <= last_day:
                    derived_flows[leg.recognition_day] = (
                        derived_flows.get(leg.recognition_day, Decimal(0)) + leg.signed
                    )
            mwr_flows.append((first_day, -derived_epoch.nav[0][1]))
            mwr_flows.extend(
                (occurred_on, -amount)
                for occurred_on, amount in sorted(derived_flows.items())
            )
            mwr_flows.append((last_day, derived_epoch.nav[-1][1]))
        if not mwr_flows:
            add_not_derivable(
                output,
                "mwr.strategy",
                strategy_id,
                "the strategy has no funded epoch in the effective perimeter",
                "ITD",
            )
            continue
        mwr = xirr(mwr_flows)
        if mwr is None:
            add_not_derivable(
                output,
                "mwr.strategy",
                strategy_id,
                "ACTUAL_365 XIRR uniqueness or bracket not proved",
                "ITD",
            )
        else:
            metrics.append(
                metric(
                    "mwr.strategy",
                    strategy_id,
                    ratio(mwr),
                    "ratio",
                    used_inputs,
                    strategy_observations[-1][0].isoformat(),
                    window="ITD",
                )
            )
    unadmitted = accounts_without_admitted_mapping(ledger)
    withheld = [
        row
        for row in metrics
        if row["metric_id"] == "date.source_last_observation"
        and row["entity"] in unadmitted
    ]
    for row in withheld:
        metrics.remove(row)
        add_not_derivable(
            output,
            "date.source_last_observation",
            str(row["entity"]),
            UNMAPPED_NAV_REASON,
        )
    if sleeve_result is not None:
        # SPEC-HARNESS: append the derived-sleeve epoch metrics and replace the
        # residual parents' account-level return/flow metrics (their epochs are
        # replaced by the derived epochs); every other metric is untouched.
        # SPEC-COMBINED: the block was derived before the manifest binding so
        # the declared manifest could be checked against the effective epoch
        # perimeter (crosswalk INCLUDED epochs minus the epochs the manifest
        # dropped, plus these derived epochs, as exact four-part identities).
        residual_parents = set(sleeve_result["residual_parent_ids"])
        replaced = {
            "flows.account.net",
            "flows.account.count",
            "twr.account",
            "mwr.account",
        }
        output["metrics"] = [
            row
            for row in output["metrics"]
            if not (row["metric_id"] in replaced and row["entity"] in residual_parents)
        ]
        output["not_derivable"] = [
            row
            for row in output["not_derivable"]
            if not (row["metric_id"] in replaced and row["entity"] in residual_parents)
        ]
        output["metrics"].extend(sleeve_result["metrics"])
        output["not_derivable"].extend(sleeve_result["not_derivable"])
        output["convention_notes"].extend(sleeve_result["convention_notes"])
        if account_manifest is not None:
            output["convention_notes"].append(
                "SPEC-COMBINED: the declared account-strategy manifest was "
                "checked against the effective epoch perimeter (crosswalk "
                "INCLUDED epochs minus the epochs the manifest dropped, plus "
                "this derivation's derived-sleeve epochs) as exact sets of "
                "the four-part epoch identity (open end = null)."
            )
        output["convention_notes"].append(
            "SPEC-COMBINED M2: mwr.strategy pools the funded-epoch cash flows "
            "of the strategy's effective-perimeter epochs, with the terminal "
            "value the sum of those epochs' values at the cutoff; a derived "
            "epoch contributes its sleeve-section flows (INTERNAL_IN legs are "
            "contributions, INTERNAL_OUT legs withdrawals) and its closing "
            "value."
        )
        output["sleeve_derivation"] = sleeve_result["sleeve_derivation"]
    return output


# ======================================================================
# R2 derived sleeve epochs
# ======================================================================

"""R2 derived sleeve epochs (SPEC-HARNESS), derivation b.

Formerly sleeves.py: moved into derive.py per CORRECTION2.md (the
architecture rule forbids dynamic module loading); no behaviour change.
Stdlib only; decimal.Decimal, never float.  The SPEC-IBKR / SPEC-QB fact
derivations reuse the implementer's own verified standalone programs
(prev/derive_ibkr.py, prev/derive_qb.py); the epoch layer on top of the
facts is the new part (SPEC-HARNESS section 3).
"""

getcontext().prec = 50  # same as at the top of this module

SLEEVE_INPUTS_SCHEMA = "qe-cp-sleeve-inputs/1"
SLEEVE_DERIVATION_SCHEMA = "qe-cp-sleeve-derivation/1"

ROLES = (
    "crosswalk",
    "ibkr_catalog",
    "tag_map",
    "ibkr_declared_inputs",
    "quantbase_declared_inputs",
    "quantbase_accounts",
    "quantbase_account_activity",
    "quantbase_dashboard_crisis",
    "quantbase_dashboard_singleindex",
    "quantbase_dashboard_volatis",
    "quantbase_dashboard_total",
)

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

DIV_KINDS = (
    "CASH_ACTIVITY:Dividends",
    "CASH_ACTIVITY:Payment In Lieu Of Dividends",
    "CASH_ACTIVITY:Withholding Tax",
)

DUST = Decimal("1e-9")
RECON_TOL = Decimal("1e-6")
NOMARK_TOL = Decimal("1e-6")

XIRR_NOT_DERIVABLE_REASON = "ACTUAL_365 XIRR uniqueness or bracket not proved"


class SleeveInputError(ValueError):
    """A --sleeve-inputs selection failed closed verification (exit status 2)."""


def _fail(message: str) -> None:
    raise SleeveInputError(message)


def D(s: object) -> Decimal:
    return Decimal(str(s).strip())


def fmt(x: Decimal | None) -> str | None:
    if x is None:
        return None
    if not isinstance(x, Decimal):
        x = Decimal(x)
    return format(x, "f")


@dataclass(frozen=True)
class Engine:
    """The existing derivation's return engine and formatting conventions."""

    time_weighted_return: Callable[..., Decimal]
    xirr: Callable[..., Decimal | None]
    money: Callable[[Decimal], str]
    ratio: Callable[[Decimal], str]
    metric: Callable[..., dict[str, object]]
    not_derivable_row: Callable[..., dict[str, object]]


@dataclass(frozen=True)
class Leg:
    recognition_day: date
    event_day: date
    cls: str  # INTERNAL_IN | INTERNAL_OUT
    amount: Decimal  # positive magnitude; the class carries the direction

    @property
    def signed(self) -> Decimal:
        return self.amount if self.cls == "INTERNAL_IN" else -self.amount


@dataclass
class Epoch:
    account_id: str
    strategy_id: str
    effective_start: date
    effective_end: date | None
    nav: list[tuple[date, Decimal]]
    legs: list[Leg]


def epoch_from_record(record: dict[str, object]) -> Epoch:
    """Rebuild a derived sleeve epoch from its serializable in-memory record."""
    raw_end = record["effective_end"]
    return Epoch(
        account_id=str(record["account_id"]),
        strategy_id=str(record["strategy_id"]),
        effective_start=date.fromisoformat(str(record["effective_start"])),
        effective_end=None if raw_end is None else date.fromisoformat(str(raw_end)),
        nav=[
            (date.fromisoformat(str(day)), D(value))
            for day, value in record["nav"]  # type: ignore[union-attr]
        ],
        legs=[
            Leg(
                date.fromisoformat(str(leg["recognition_day"])),
                date.fromisoformat(str(leg["event_day"])),
                str(leg["class"]),
                D(leg["amount"]),
            )
            for leg in record["legs"]  # type: ignore[union-attr]
        ],
    )


# ---------------------------------------------------------------- sleeve-inputs


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SleeveInputError(f"duplicate key {key!r} in a JSON object")
        result[key] = value
    return result


def load_json_strict(path: Path) -> object:
    return json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object
    )


def load_sleeve_inputs(
    sleeve_inputs_path: Path,
    qe_root: Path,
    repo_root: Path,
    cutoff: date,
) -> dict[str, object]:
    """Validate a qe-cp-sleeve-inputs/1 file fail-closed and pin every role."""
    try:
        document = load_json_strict(sleeve_inputs_path)
    except (OSError, json.JSONDecodeError) as error:
        _fail(f"cannot read sleeve-inputs file {sleeve_inputs_path}: {error}")
    if not isinstance(document, dict):
        _fail("sleeve-inputs file is not a JSON object")
    if document.get("schema") != SLEEVE_INPUTS_SCHEMA:
        _fail(f"schema is not {SLEEVE_INPUTS_SCHEMA!r}")
    declared_cutoff = document.get("cutoff")
    if declared_cutoff != cutoff.isoformat():
        _fail(
            f"cutoff {declared_cutoff!r} differs from the cutoff {cutoff.isoformat()}"
        )
    inputs = document.get("inputs")
    if not isinstance(inputs, dict):
        _fail("sleeve-inputs file has no inputs object")
    order = list(inputs)
    seen: set[str] = set()
    resolved: dict[str, dict[str, object]] = {}
    for role in order:
        if role not in ROLES:
            _fail(f"unknown role {role!r}")
        if role in seen:
            _fail(f"duplicate role {role!r}")
        seen.add(role)
        entry = inputs[role]
        if not isinstance(entry, dict):
            _fail(f"role {role!r} is not an object")
        root = entry.get("root")
        raw_path = entry.get("path")
        pin = entry.get("sha256")
        if root not in ("qe", "repo"):
            _fail(f"role {role!r}: root must be 'qe' or 'repo'")
        if not isinstance(raw_path, str) or not raw_path:
            _fail(f"role {role!r}: missing path")
        if not isinstance(pin, str) or not SHA256_RE.match(pin):
            _fail(f"role {role!r}: bad sha256 form {pin!r}")
        # Input paths must stay inside their declared root (CORRECTION3.md):
        # an absolute path or one with a drive/anchor is rejected outright;
        # anything else is resolved (links followed) against the resolved
        # root and rejected when it lands outside it (a `..` escape or a
        # link pointing out).  A `..` segment that stays inside is allowed.
        candidate = PurePath(raw_path)
        if candidate.is_absolute() or candidate.drive or candidate.anchor:
            _fail(
                f"role {role!r}: path is absolute or has a drive/anchor: {raw_path!r}"
            )
        base = (qe_root if root == "qe" else repo_root).resolve()
        absolute = (base / raw_path).resolve()
        if not absolute.is_relative_to(base):
            _fail(f"role {role!r}: path escapes its declared root: {raw_path!r}")
        if not absolute.is_file():
            _fail(f"role {role!r}: file not found: {raw_path}")
        actual = _sha256_file(absolute)
        if actual.lower() != pin.lower():
            _fail(
                f"role {role!r}: sha256 mismatch for {raw_path} "
                f"(pinned {pin}, actual {actual})"
            )
        resolved[role] = {
            "path": raw_path,
            "root": root,
            "sha256": pin.lower(),
            "bytes": absolute.stat().st_size,
            "abspath": absolute,
        }
    for role in ROLES:
        if role not in resolved:
            _fail(f"missing role {role!r}")
    return {"order": order, "roles": resolved, "cutoff": cutoff}


def inputs_used_rows(selection: dict[str, object]) -> list[str]:
    """inputs_used entries: the sleeve input paths as given, with their pins."""
    order: list[str] = selection["order"]  # type: ignore[assignment]
    roles: dict[str, dict[str, object]] = selection["roles"]  # type: ignore[assignment]
    return [f"{roles[role]['path']} sha256:{roles[role]['sha256']}" for role in order]


def input_header_rows(selection: dict[str, object]) -> list[dict[str, object]]:
    """Document-level inputs rows in the style of the existing derivation."""
    order: list[str] = selection["order"]  # type: ignore[assignment]
    roles: dict[str, dict[str, object]] = selection["roles"]  # type: ignore[assignment]
    return [
        {
            "path": roles[role]["path"],
            "sha256": roles[role]["sha256"],
            "bytes": roles[role]["bytes"],
        }
        for role in order
    ]


def block_inputs(selection: dict[str, object]) -> dict[str, object]:
    order: list[str] = selection["order"]  # type: ignore[assignment]
    roles: dict[str, dict[str, object]] = selection["roles"]  # type: ignore[assignment]
    return {
        role: {"path": roles[role]["path"], "sha256": roles[role]["sha256"]}
        for role in order
    }


def _role_json(selection: dict[str, object], role: str) -> object:
    roles: dict[str, dict[str, object]] = selection["roles"]  # type: ignore[assignment]
    path = roles[role]["abspath"]
    try:
        return load_json_strict(path)  # type: ignore[arg-type]
    except (OSError, json.JSONDecodeError) as error:
        _fail(f"role {role!r}: cannot parse JSON: {error}")


def _role_csv_rows(selection: dict[str, object], role: str) -> list[dict[str, str]]:
    roles: dict[str, dict[str, object]] = selection["roles"]  # type: ignore[assignment]
    path = roles[role]["abspath"]
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:  # type: ignore[arg-type]
        return list(csv.DictReader(handle))


# ---------------------------------------------------------------- IBKR facts
# (SPEC-IBKR; adapted from this implementer's verified prev/derive_ibkr.py)


def _cut_ibkr_slice(
    catalog: dict[str, object], parent: str, first_day: str, last_day: str
) -> dict[str, object]:
    """The parent account's rows restricted to the declared window."""

    def in_window(day: str | None) -> bool:
        return day is not None and first_day <= day <= last_day

    transactions = []
    for row in catalog["transactions"]:  # type: ignore[index]
        if row.get("account_id") != parent:
            continue
        if str(row.get("kind", "")).startswith("TRADE:"):
            day = row.get("event_date")
        else:
            day = row.get("nav_recognition_date") or row.get("event_date")
        if in_window(day):
            transactions.append(row)
    return {
        "transactions": transactions,
        "positions": [
            row
            for row in catalog["positions"]  # type: ignore[index]
            if row.get("account_id") == parent and in_window(row.get("as_of_date"))
        ],
        "change_in_nav_observations": [
            row
            for row in catalog["change_in_nav_observations"]  # type: ignore[index]
            if row.get("account_id") == parent and in_window(row.get("accounting_date"))
        ],
        "corporate_actions": [
            row
            for row in catalog.get("corporate_actions", [])  # type: ignore[union-attr]
            if row.get("account_id") == parent
        ],
        "accounts": [
            row
            for row in catalog["accounts"]  # type: ignore[index]
            if row.get("account_id") == parent
        ],
    }


def _ibkr_facts(
    slc: dict[str, object],
    declared: dict[str, object],
    parent_entry: dict[str, object],
) -> dict[str, object]:
    refusals: list[str] = []
    assumptions: list[str] = []

    parent = str(parent_entry["parent_account_id"])
    unallocated = parent + "-unallocated"
    residual_id = str(declared["residual_id"])  # "other"
    sleeves_meta = parent_entry["sleeves"]
    REAL = [str(s["sleeve_id"]) for s in sleeves_meta]  # type: ignore[index]
    ALL = REAL + [residual_id]
    unmatched = parent_entry["unmatched"]  # type: ignore[index]
    fallback_default = unmatched["fallback_sleeve_id"]
    unheld_fallback = declared.get("unheld_fallback", {})
    aliases = declared.get("symbol_aliases", {}).get("aliases", {})  # type: ignore[union-attr]
    funding = declared["funding_events"]
    roles = declared["change_in_nav_components"]
    tol_day = D(declared["closure_tolerance"]["per_day"])  # type: ignore[index]
    tol_win = D(declared["closure_tolerance"]["window"])  # type: ignore[index]

    # ---------------- NAV days ----------------
    nav_rows = sorted(
        slc["change_in_nav_observations"],  # type: ignore[index]
        key=lambda r: r["accounting_date"],
    )
    nav_days = [r["accounting_date"] for r in nav_rows]
    if len(set(nav_days)) != len(nav_days):
        refusals.append("ChangeInNAV: duplicate accounting_date values")
    nav_set = set(nav_days)
    if not nav_days:
        _fail("ibkr_catalog: the parent account has no ChangeInNAV row in the window")
    declared_window = declared["window"]  # type: ignore[index]
    if (
        nav_days[0] != declared_window["first_day"]
        or nav_days[-1] != declared_window["last_day"]
    ):
        refusals.append(
            f"NAV window {nav_days[0]}..{nav_days[-1]} != declared "
            f"{declared_window['first_day']}..{declared_window['last_day']}"
        )
    for a, b in zip(nav_rows, nav_rows[1:]):
        if D(a["ending_nav"]) != D(b["opening_nav"]):
            refusals.append(
                f"ChangeInNAV break: {a['accounting_date']} ending {a['ending_nav']} "
                f"!= {b['accounting_date']} opening {b['opening_nav']}"
            )

    acct = {}
    for r in nav_rows:
        d = r["accounting_date"]
        comp = {c["source_component_key"]: D(c["value"]) for c in r["components"]}

        def role_sum(role: str) -> Decimal:
            tot = Decimal(0)
            for k in roles[role]:  # type: ignore[index]
                if k not in comp:
                    refusals.append(
                        f"ChangeInNAV {d}: listed component key '{k}' not stated"
                    )
                else:
                    tot += comp[k]
            return tot

        ext = role_sum("external_flow")
        acct[d] = {
            "external_flow": ext,
            "interest": role_sum("interest"),
            "fees": role_sum("fees"),
            "dividend_accrual": role_sum("dividend_accrual"),
            "account_pnl": D(r["ending_nav"]) - D(r["opening_nav"]) - ext,
        }

    # ---------------- positions ----------------
    book: dict[str, dict[str, Decimal]] = {}
    marks: dict[str, dict[str, Decimal]] = {}
    for p in slc["positions"]:  # type: ignore[index]
        d = p["as_of_date"]
        if d not in nav_set:
            refusals.append(f"position on non-NAV day {d} ({p['security_id']})")
            continue
        q = D(p["quantity"])
        sec = p["security_id"]
        if sec in book.setdefault(d, {}):
            refusals.append(f"position listed twice on {d}: {sec}")
        book[d][sec] = q
        mk = p.get("mark_price")
        if mk in (None, ""):
            if q != 0:
                refusals.append(f"non-zero position without mark_price on {d}: {sec}")
        else:
            marks.setdefault(d, {})[sec] = D(mk)
    for d in nav_days:
        book.setdefault(d, {})
        marks.setdefault(d, {})

    sec_kind = {}
    for p in slc["positions"]:  # type: ignore[index]
        sec_kind[p["security_id"]] = (p.get("asset_category"), p.get("multiplier"))

    # ---------------- trades ----------------
    trades_by_day: dict[str, list[dict[str, object]]] = {d: [] for d in nav_days}
    for t in slc["transactions"]:  # type: ignore[index]
        kind = str(t.get("kind", ""))
        if not kind.startswith("TRADE:"):
            continue
        d = t["event_date"]
        if d not in nav_set:
            refusals.append(f"trade on non-NAV day {d}: {t.get('record_id')}")
            continue
        bad = None
        if (
            t.get("quantity") in (None, "")
            or t.get("execution_price") in (None, "")
            or not t.get("commission")
            or t["commission"].get("value") in (None, "")
        ):
            bad = "missing quantity/execution_price/commission"
        elif t.get("amount", {}).get("currency") != "USD":
            bad = "amount not in USD"
        elif sec_kind.get(t["security_id"], ("STK", "1")) != ("STK", "1"):
            bad = "security is not a plain share"
        if bad:
            refusals.append(f"trade {t.get('record_id')} on {d}: {bad}")
            continue
        trades_by_day[d].append(
            {
                "day": d,
                "time": t.get("executed_time") or "",
                "sec": t["security_id"],
                "qty": D(t["quantity"]),
                "price": D(t["execution_price"]),
                "comm": D(t["commission"]["value"]),
                "ref": t.get("order_reference"),
            }
        )

    # ---------------- dividends ----------------
    divs_by_day: dict[str, list[dict[str, object]]] = {d: [] for d in nav_days}
    for t in slc["transactions"]:  # type: ignore[index]
        if t.get("kind") not in DIV_KINDS or not t.get("security_id"):
            continue
        d = t.get("nav_recognition_date") or t["event_date"]
        if d not in nav_set:
            refusals.append(f"dividend on non-NAV day {d}: {t.get('record_id')}")
            continue
        divs_by_day[d].append(
            {"sec": t["security_id"], "amount": D(t["amount"]["value"])}
        )

    # ---------------- tag resolution ----------------
    def resolve(ref: object, day: str) -> str | None:
        if not ref:
            return None
        ntag = re.sub(r"^[0-9]+-?", "", str(ref), count=1)
        if not ntag:
            return None
        hits = []
        for s in sleeves_meta:  # type: ignore[union-attr]
            if s["effective_start"] <= day and (
                s["effective_end"] is None or day <= s["effective_end"]
            ):
                for rule in s["tags"]:
                    m, pat = rule["match"], rule["pattern"]
                    ok = (
                        (m == "startswith" and ntag.startswith(pat))
                        or (m == "contains" and pat in ntag)
                        or (m == "exact" and ntag == pat)
                    )
                    if ok:
                        hits.append(s["sleeve_id"])
                        break
        if len(hits) > 1:
            refusals.append(f"tag '{ref}' on {day} matches two sleeves {hits}")
        return hits[0] if hits else None

    # ---------------- ledger ----------------
    holdings: dict[str, dict[str, Decimal]] = {s: {} for s in ALL}
    manual_assignments = []
    recon_gaps = []
    recon_scalings = []
    offsetting_residues = []
    unallocated_dividends = []
    facts: dict[tuple[str, str], dict[str, Decimal]] = {}

    alias_at: dict[str, list[tuple[str, str]]] = {}
    old_of: dict[str, str] = {}
    for old, a in aliases.items():  # type: ignore[union-attr]
        alias_at.setdefault(a["from_day"], []).append((old, a["to"]))
        old_of[a["to"]] = old

    def prev_mark(sec: str, pday: str) -> Decimal | None:
        m = marks.get(pday, {}).get(sec)
        o = sec
        while m is None and o in old_of:
            o = old_of[o]
            m = marks.get(pday, {}).get(o)
        return m

    prev_day = None
    for d in nav_days:
        for old, new in alias_at.get(d, []):
            for s in ALL:
                q = holdings[s].pop(old, Decimal(0))
                if q != 0:
                    holdings[s][new] = holdings[s].get(new, Decimal(0)) + q

        prev_qty = {
            s: {k: v for k, v in holdings[s].items() if abs(v) > DUST} for s in ALL
        }

        net_trade = {s: Decimal(0) for s in ALL}
        comm = {s: Decimal(0) for s in ALL}
        todays = trades_by_day[d]
        if any(t["time"] == "" for t in todays) and any(
            resolve(t["ref"], d) is None for t in todays
        ):
            refusals.append(
                f"{d}: day has both an execution with no executed_time and an "
                "UNASSIGNED execution; attribution order is ambiguous (continued "
                "with the spec order)"
            )
        keyed = []
        for t in todays:
            s = resolve(t["ref"], d)
            keyed.append(
                (
                    f"{d};{t['time']}",
                    0 if s else 1,
                    t["sec"],
                    t["qty"],
                    t["price"],
                    t["comm"],
                    s,
                    t,
                )
            )
        keyed.sort(key=lambda k: k[:6])

        for _, _, sec, qty, price, c, s, t in keyed:
            if s is not None:
                holdings[s][sec] = holdings[s].get(sec, Decimal(0)) + qty
                net_trade[s] += qty * price
                comm[s] += c
                continue
            holders = [h for h in REAL if holdings[h].get(sec, Decimal(0)) * qty < 0]
            if len(holders) == 1:
                h = holders[0]
                holdings[h][sec] += qty
                net_trade[h] += qty * price
                comm[h] += c
                manual_assignments.append(
                    {
                        "day": d,
                        "security_id": sec,
                        "quantity": fmt(qty),
                        "how": "assigned",
                        "sleeves": [h],
                    }
                )
            elif len(holders) > 1:
                tot = sum(abs(holdings[h][sec]) for h in holders)
                for h in sorted(holders):
                    share = abs(holdings[h][sec]) / tot
                    holdings[h][sec] += share * qty
                    net_trade[h] += share * qty * price
                    comm[h] += share * c
                manual_assignments.append(
                    {
                        "day": d,
                        "security_id": sec,
                        "quantity": fmt(qty),
                        "how": "split",
                        "sleeves": sorted(holders),
                    }
                )
            else:
                fb = unheld_fallback.get(sec, fallback_default)  # type: ignore[union-attr]
                if fb is None:
                    fb = residual_id
                holdings[fb][sec] = holdings[fb].get(sec, Decimal(0)) + qty
                net_trade[fb] += qty * price
                comm[fb] += c
                manual_assignments.append(
                    {
                        "day": d,
                        "security_id": sec,
                        "quantity": fmt(qty),
                        "how": "unheld",
                        "sleeves": [fb],
                    }
                )

        secs = set(book[d]) | {
            sec for s in ALL for sec, q in holdings[s].items() if q != 0
        }
        for sec in sorted(secs):
            total = sum((holdings[s].get(sec, Decimal(0)) for s in ALL), Decimal(0))
            actual = book[d].get(sec, Decimal(0))
            if abs(total - actual) <= RECON_TOL:
                if actual == 0:
                    held = {
                        s: holdings[s][sec]
                        for s in ALL
                        if abs(holdings[s].get(sec, Decimal(0))) > DUST
                    }
                    if held:
                        offsetting_residues.append(
                            {
                                "day": d,
                                "security_id": sec,
                                "quantities": {
                                    s: fmt(q) for s, q in sorted(held.items())
                                },
                            }
                        )
                        for s in held:
                            del holdings[s][sec]
                continue
            recon_gaps.append(
                {
                    "day": d,
                    "security_id": sec,
                    "derived": fmt(total),
                    "account": fmt(actual),
                }
            )
            if abs(total) > DUST and abs(actual) > DUST and total * actual > 0:
                factor = actual / total
                for s in ALL:
                    if sec in holdings[s]:
                        holdings[s][sec] *= factor
                recon_scalings.append(
                    {"day": d, "security_id": sec, "factor": fmt(factor)}
                )
            elif abs(total) > DUST and actual == 0:
                for s in ALL:
                    holdings[s].pop(sec, None)
            else:
                holdings[residual_id][sec] = holdings[residual_id].get(
                    sec, Decimal(0)
                ) + (actual - total)

        div_cash = {s: Decimal(0) for s in ALL}
        for dv in divs_by_day[d]:
            sec, amt = dv["sec"], dv["amount"]
            holders = [s for s in ALL if prev_qty[s].get(sec, Decimal(0)) > 0]
            basis = {s: prev_qty[s][sec] for s in holders}
            if not holders:
                holders = [s for s in ALL if holdings[s].get(sec, Decimal(0)) > 0]
                basis = {s: holdings[s][sec] for s in holders}
            if not holders:
                unallocated_dividends.append({"day": d, "amount": fmt(amt)})
                continue
            tot = sum(basis.values())
            for s in holders:
                div_cash[s] += amt * basis[s] / tot

        for s in ALL:
            value = Decimal(0)
            for sec, q in holdings[s].items():
                if abs(q) <= DUST:
                    continue
                m = marks[d].get(sec)
                if m is None:
                    if abs(q) <= NOMARK_TOL:
                        continue
                    refusals.append(
                        f"{d}: sleeve {s} holds {fmt(q)} of {sec} with no mark"
                    )
                    continue
                value += q * m
            pv = Decimal(0)
            if prev_day is not None:
                for sec, q in prev_qty[s].items():
                    m = prev_mark(sec, prev_day)
                    if m is None:
                        if abs(q) <= NOMARK_TOL:
                            continue
                        refusals.append(
                            f"{d}: sleeve {s} prev holding {fmt(q)} of {sec} has "
                            "no previous mark"
                        )
                        continue
                    pv += q * m
            nt, cm, dv_c = net_trade[s], comm[s], div_cash[s]
            facts[(s, d)] = {
                "value": value,
                "prev_value": pv,
                "net_trade": nt,
                "commission": cm,
                "dividend": dv_c,
                "price_pnl": value - pv - nt,
            }
        prev_day = d

    # ---------------- SPEC-IBKR 6: sleeve NAV rows ----------------
    sleeve_first: dict[str, str | None] = {}
    sleeve_exit: dict[str, str | None] = {}
    for s in REAL:
        days = sorted(funding.get(s, {}))  # type: ignore[union-attr]
        sleeve_first[s] = days[0] if days else None
        exits = [dd for dd in days if funding[s][dd]["kind"] == "EXIT"]  # type: ignore[index]
        sleeve_exit[s] = exits[-1] if exits else None
        for dd in days:
            if dd not in nav_set:
                refusals.append(
                    f"{s}: declared funding event on {dd} which is not a NAV day"
                )

    sleeve_days: dict[str, list[str]] = {}
    for s in REAL:
        if sleeve_first[s] is None:
            sleeve_days[s] = []
            continue
        last = sleeve_exit[s] or nav_days[-1]
        sleeve_days[s] = [d for d in nav_days if sleeve_first[s] <= d <= last]  # type: ignore[operator]

    rows: dict[str, list[dict[str, object]]] = {s: [] for s in REAL}
    nav_prev_map = {s: Decimal(0) for s in REAL}
    cum_alloc = {s: Decimal(0) for s in REAL}
    cum_alloc_alt = {s: Decimal(0) for s in REAL}

    for d in nav_days:
        active = [s for s in REAL if d in sleeve_days[s]]
        if not active:
            continue
        interim = {}
        for s in active:
            f = facts[(s, d)]
            kind = funding[s].get(d, {}).get("kind", "")  # type: ignore[index]
            flow_in = f["net_trade"] if kind == "IN" else Decimal(0)
            exposure = abs(f["prev_value"]) + max(f["net_trade"], Decimal(0))
            nav_prev = nav_prev_map[s]
            base = nav_prev + flow_in
            own_pnl = f["price_pnl"] + f["commission"] + f["dividend"]
            interim[s] = {
                "kind": kind,
                "flow_in": flow_in,
                "exposure": exposure,
                "base": base,
                "own_pnl": own_pnl,
                "f": f,
            }
        exp_sum = sum(interim[s]["exposure"] for s in active)
        base_sum = sum(max(interim[s]["base"], Decimal(0)) for s in active)
        for s in active:
            it = interim[s]
            f, kind = it["f"], it["kind"]
            nav_prev = nav_prev_map[s]
            nav_pre = nav_prev + it["flow_in"] + it["own_pnl"]
            if kind == "OUT":
                flow_out = f["net_trade"]
            elif kind == "EXIT":
                flow_out = -nav_pre
            elif kind == "SWEEP":
                flow_out = f["value"] - nav_pre
            else:
                flow_out = Decimal(0)
            nav_end = nav_pre + flow_out
            value = f["value"]
            if kind == "EXIT":
                nav_end = Decimal(0)
                value = Decimal(0)
            share = it["exposure"] / exp_sum if exp_sum != 0 else Decimal(0)
            share_alt = (
                max(it["base"], Decimal(0)) / base_sum if base_sum != 0 else Decimal(0)
            )
            a = acct[d]
            alloc_interest = a["interest"] * share
            alloc_fees = a["fees"] * share
            alloc_div = a["dividend_accrual"] * share
            alloc_total = alloc_interest + alloc_fees + alloc_div
            alloc_total_alt = (
                a["interest"] + a["fees"] + a["dividend_accrual"]
            ) * share_alt
            base = it["base"]
            r_own = it["own_pnl"] / base if base > 1 else Decimal(0)
            r_alloc = (it["own_pnl"] + alloc_total) / base if base > 1 else Decimal(0)
            r_alloc_alt = (
                (it["own_pnl"] + alloc_total_alt) / base if base > 1 else Decimal(0)
            )
            cum_alloc[s] += alloc_total
            cum_alloc_alt[s] += alloc_total_alt
            if kind == "EXIT":
                nav_end_after = Decimal(0)
                nav_end_after_alt = Decimal(0)
            else:
                nav_end_after = nav_end + cum_alloc[s]
                nav_end_after_alt = nav_end + cum_alloc_alt[s]
            rows[s].append(
                {
                    "day": d,
                    "event": kind,
                    "nav_prev": fmt(nav_prev),
                    "flow_in": fmt(it["flow_in"]),
                    "flow_out": fmt(flow_out),
                    "nav_end": fmt(nav_end),
                    "value": fmt(value),
                    "exposure": fmt(it["exposure"]),
                    "exposure_share": fmt(share),
                    "price_pnl": fmt(f["price_pnl"]),
                    "commission": fmt(f["commission"]),
                    "dividend": fmt(f["dividend"]),
                    "own_pnl": fmt(it["own_pnl"]),
                    "base": fmt(base),
                    "r_own": fmt(r_own),
                    "alloc_interest": fmt(alloc_interest),
                    "alloc_fees": fmt(alloc_fees),
                    "alloc_dividend_accrual": fmt(alloc_div),
                    "alloc_total": fmt(alloc_total),
                    "r_alloc": fmt(r_alloc),
                    "nav_end_after_alloc": fmt(nav_end_after),
                    "alloc_total_alt": fmt(alloc_total_alt),
                    "r_alloc_alt": fmt(r_alloc_alt),
                    "nav_end_after_alloc_alt": fmt(nav_end_after_alt),
                }
            )
            nav_prev_map[s] = nav_end

    cumulative = {}
    for s in REAL:
        rs = rows[s]
        if not rs:
            continue
        twr_o = Decimal(1)
        twr_a = Decimal(1)
        twr_alt = Decimal(1)
        for r in rs:
            twr_o *= 1 + D(r["r_own"])
            twr_a *= 1 + D(r["r_alloc"])
            twr_alt *= 1 + D(r["r_alloc_alt"])
        cumulative[s] = {
            "first_day": rs[0]["day"],
            "last_day": rs[-1]["day"],
            "rows": len(rs),
            "twr_own": fmt(twr_o - 1),
            "twr_after_alloc": fmt(twr_a - 1),
            "twr_after_alloc_alt": fmt(twr_alt - 1),
            "sum_flow_in": fmt(sum((D(r["flow_in"]) for r in rs), Decimal(0))),
            "sum_flow_out": fmt(sum((D(r["flow_out"]) for r in rs), Decimal(0))),
            "sum_own_pnl": fmt(sum((D(r["own_pnl"]) for r in rs), Decimal(0))),
            "sum_alloc_total": fmt(sum((D(r["alloc_total"]) for r in rs), Decimal(0))),
            "sum_alloc_total_alt": fmt(
                sum((D(r["alloc_total_alt"]) for r in rs), Decimal(0))
            ),
            "final_nav_end": rs[-1]["nav_end"],
            "final_nav_end_after_alloc": rs[-1]["nav_end_after_alloc"],
        }

    # ---------------- SPEC-IBKR 7: closure ----------------
    row_by = {(s, r["day"]): r for s in REAL for r in rows[s]}
    closure = []
    max_abs_unx = Decimal(0)
    win_unx = Decimal(0)
    for d in nav_days:
        active = [s for s in REAL if (s, d) in row_by]
        sop = sum(
            (
                D(row_by[(s, d)]["own_pnl"]) + D(row_by[(s, d)]["alloc_total"])
                for s in active
            ),
            Decimal(0),
        )
        resid_sleeves = [residual_id] + [s for s in REAL if (s, d) not in row_by]
        resid = Decimal(0)
        for s in resid_sleeves:
            f = facts[(s, d)]
            resid += (
                f["value"]
                - f["prev_value"]
                - f["net_trade"]
                + f["commission"]
                + f["dividend"]
            )
        resid += sum(
            (D(u["amount"]) for u in unallocated_dividends if u["day"] == d), Decimal(0)
        )
        unx = acct[d]["account_pnl"] - sop - resid
        ucf = Decimal(0)
        for s in active:
            ev = funding[s].get(d)  # type: ignore[index]
            if ev and ev.get("counterparty") == "UNALLOCATED_ACCOUNT_CASH":
                ucf += -(D(row_by[(s, d)]["flow_in"]) + D(row_by[(s, d)]["flow_out"]))
        closure.append(
            {
                "day": d,
                "account_pnl": fmt(acct[d]["account_pnl"]),
                "sleeves_own_plus_alloc": fmt(sop),
                "residual_and_post_exit": fmt(resid),
                "unexplained": fmt(unx),
                "unallocated_cash_flow": fmt(ucf),
            }
        )
        if abs(unx) > max_abs_unx:
            max_abs_unx = abs(unx)
        win_unx += unx

    closure_summary = {
        "max_abs_unexplained": fmt(max_abs_unx),
        "window_unexplained": fmt(win_unx),
        "per_day_tolerance_met": max_abs_unx <= tol_day,
        "window_tolerance_met": abs(win_unx) <= tol_win,
    }

    # ---------------- SPEC-IBKR 8: pairs ----------------
    pairs = []
    seq = 0
    for d in nav_days:
        active = [s for s in REAL if (s, d) in row_by]
        nets = {}
        for s in active:
            r = row_by[(s, d)]
            nets[s] = D(r["flow_in"]) + D(r["flow_out"])
        day_pairs = []
        greedy_ins, greedy_outs = [], []
        for s in sorted(active):
            if nets[s] == 0:
                continue
            ev = funding[s].get(d, {})  # type: ignore[index]
            if ev.get("counterparty") == "UNALLOCATED_ACCOUNT_CASH":
                if nets[s] > 0:
                    day_pairs.append(
                        (
                            unallocated,
                            parent + "-" + s,
                            "unallocated",
                            s,
                            nets[s],
                            "UNALLOCATED_CASH",
                            "DECLARED_UNALLOCATED",
                        )
                    )
                else:
                    day_pairs.append(
                        (
                            parent + "-" + s,
                            unallocated,
                            s,
                            "unallocated",
                            -nets[s],
                            "UNALLOCATED_CASH",
                            "DECLARED_UNALLOCATED",
                        )
                    )
            else:
                (greedy_ins if nets[s] > 0 else greedy_outs).append(s)
        rem_in = {s: nets[s] for s in greedy_ins}
        rem_out = {s: -nets[s] for s in greedy_outs}
        i = j = 0
        while i < len(greedy_ins) and j < len(greedy_outs):
            si, so = greedy_ins[i], greedy_outs[j]
            amt = min(rem_in[si], rem_out[so])
            day_pairs.append(
                (
                    parent + "-" + so,
                    parent + "-" + si,
                    so,
                    si,
                    amt,
                    "REALLOCATION",
                    "SLEEVE_PAIR",
                )
            )
            rem_in[si] -= amt
            rem_out[so] -= amt
            if rem_in[si] == 0:
                i += 1
            if rem_out[so] == 0:
                j += 1
        for s in sorted(rem_in):
            if rem_in[s] > 0:
                day_pairs.append(
                    (
                        unallocated,
                        parent + "-" + s,
                        "unallocated",
                        s,
                        rem_in[s],
                        "UNALLOCATED_CASH",
                        "UNALLOCATED_REMAINDER",
                    )
                )
        for s in sorted(rem_out):
            if rem_out[s] > 0:
                day_pairs.append(
                    (
                        parent + "-" + s,
                        unallocated,
                        s,
                        "unallocated",
                        rem_out[s],
                        "UNALLOCATED_CASH",
                        "UNALLOCATED_REMAINDER",
                    )
                )
        for out_acct, in_acct, fs, ts, amt, kind, pairing in day_pairs:
            seq += 1
            pairs.append(
                {
                    "day": d,
                    "sequence": seq,
                    "amount": fmt(amt),
                    "out_account": out_acct,
                    "in_account": in_acct,
                    "kind": kind,
                    "pairing": pairing,
                    "from_sleeve": fs,
                    "to_sleeve": ts,
                }
            )

    assumptions.extend(
        [
            "Execution ordering tie-break 'ascending security_id, quantity, "
            "execution_price, commission' is applied as decimal/string ascending "
            "sorts on the parsed values.",
            "Section 4.4 reconciliation iterates the union of the account book's "
            "securities and every sleeve-held security in ascending security_id "
            "order; dust of |q| <= 1e-9 left by a reconcile-to-zero is dropped via "
            "the offsetting-residue branch.",
            "prev_value missing-mark fallback walks the alias chain (new -> old) at "
            "the previous day's marks.",
            "Pairs (SPEC-IBKR 8): within a day, DECLARED_UNALLOCATED pairs are "
            "emitted first in ascending sleeve-id order, then greedy SLEEVE_PAIR "
            "reallocations in pairing order, then UNALLOCATED_REMAINDER pairs "
            "(remaining ins ascending, then remaining outs ascending).",
            "Pairs: from_sleeve/to_sleeve carry the sleeve id, or 'unallocated' "
            "for the perimeter pseudo-account '<parent>-unallocated'.",
            "Cumulative TWRs chain every row's rate including rows where the spec "
            "yields r = 0 (base <= 1), which contribute a factor of exactly 1.",
        ]
    )

    return {
        "window": {"first_day": nav_days[0], "last_day": nav_days[-1]},
        "sleeves": rows,
        "cumulative": cumulative,
        "closure": closure,
        "closure_summary": closure_summary,
        "pairs": pairs,
        "disclosures": {
            "manual_assignments": manual_assignments,
            "reconciliation_gaps": recon_gaps,
            "reconciliation_scalings": recon_scalings,
            "offsetting_residues": offsetting_residues,
            "unallocated_dividends": unallocated_dividends,
        },
        "refusals": refusals,
        "assumptions": assumptions,
    }


# ------------------------------------------------------------- Quantbase facts
# (SPEC-QB; adapted from this implementer's verified prev/derive_qb.py)


def _udt(s: str) -> datetime:
    return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_day_us(s: str) -> date:
    return datetime.strptime(s.strip(), "%m/%d/%Y").date()


def _load_dashboard_rows(
    rows: Iterable[dict[str, str]], name: str, refusals: list[str]
) -> dict[date, dict[str, Decimal]]:
    board: dict[date, dict[str, Decimal]] = {}
    for r in rows:
        d = _parse_day_us(r["time"])
        if d in board:
            refusals.append(f"dashboard {name}: duplicate day {d.isoformat()}")
        board[d] = {
            "value": D(r["value"]),
            "cost_basis": D(r["cost_basis"]),
            "profit_loss": D(r["profit_loss"]),
        }
    return board


def _qb_facts(
    declared: dict[str, object],
    dashboards: dict[str, list[dict[str, str]]],
    activity_rows: list[dict[str, str]],
    accounts_rows: list[dict[str, str]],
    cutoff: date,
) -> dict[str, object]:
    refusals: list[str] = []
    assumptions: list[str] = []

    parent = str(declared["parent_account_id"])
    unallocated = parent + "-unallocated"
    tol = {k: D(v) for k, v in declared["closure_tolerance"].items()}  # type: ignore[index]
    pairing_decl = declared["pairing"]  # type: ignore[index]
    window_seconds = D(pairing_decl["window_seconds"])
    exempt_days = {
        date.fromisoformat(k)
        for k in declared.get("nav_total_exempt_days", {})  # type: ignore[union-attr]
    }

    dash = {
        name: _load_dashboard_rows(rows, name, refusals)
        for name, rows in dashboards.items()
    }

    # dedupe on (activity_type, id); exact repeat = one row, conflicting = error
    seen: dict[tuple[str, str], dict[str, str]] = {}
    activity: list[dict[str, str]] = []
    for r in activity_rows:
        key = (r["activity_type"], r["id"])
        if key in seen:
            if dict(seen[key]) != dict(r):
                refusals.append(
                    f"conflicting repeat of (activity_type={key[0]}, id={key[1]})"
                )
            continue
        seen[key] = r
        activity.append(r)

    def row_day(r: dict[str, str]) -> date | None:
        at = r["activity_type"]
        if at == "filled_txn":
            return _udt(r["time"]).date()
        if at == "order":
            return _udt(r["filled_at"]).date() if r["filled_at"] else None
        if at in ("transfer", "journal"):
            return _udt(r["created_at"]).date()
        if at == "dividend":
            return _udt(r["date"]).date()
        return None

    kept, after_cutoff = [], 0
    for r in activity:
        d = row_day(r)
        if d is not None and d > cutoff:
            after_cutoff += 1
            continue
        kept.append(r)

    orders = [r for r in kept if r["activity_type"] == "order" and r["executed"] == "Y"]
    orders_by_txn: dict[str, list[dict[str, str]]] = {}
    for o in orders:
        orders_by_txn.setdefault(o["transaction"], []).append(o)

    txns = [
        r for r in kept if r["activity_type"] == "filled_txn" and r["filled"] == "Y"
    ]

    def fill_moment(txn: dict[str, str], include_fee_orders: bool) -> datetime | None:
        os_ = orders_by_txn.get(txn["id"], [])
        if not include_fee_orders:
            os_ = [o for o in os_ if o["created_to_cover_fees"] != "True"]
        if not os_:
            return None
        return max(_udt(o["filled_at"]) for o in os_)

    def txn_fill(txn: dict[str, str]) -> datetime | None:
        fee_txn = txn["created_to_cover_fees"] == "True"
        m = fill_moment(txn, include_fee_orders=fee_txn)
        if m is None:
            m = fill_moment(txn, include_fee_orders=True)
        if m is None:
            refusals.append(
                f"filled_txn {txn['id']} (fund {txn['fund']} {txn['type']}) has "
                "no executed orders"
            )
        return m

    funds_decl = declared["funds"]  # type: ignore[index]
    out_funds: dict[str, object] = {}
    all_flows: list[dict[str, object]] = []
    net_fills_by_fund: dict[str, Decimal] = {}
    fee_sells_by_fund: dict[str, list[dict[str, object]]] = {}

    for sleeve in ("crisis", "singleindex"):
        fd = funds_decl[sleeve]
        fund_id = fd["fund_id"]
        board = dash[fd["dashboard"]]
        first_nz = date.fromisoformat(fd["first_nonzero_day"])
        last_nz = date.fromisoformat(fd["last_nonzero_day"])
        exit_day = last_nz + timedelta(days=1)

        nz_days = [d for d in sorted(board) if board[d]["value"] != 0]
        if not nz_days:
            refusals.append(f"{sleeve}: dashboard has no non-zero value day")
        else:
            if nz_days[0] != first_nz:
                refusals.append(
                    f"{sleeve}: declared first_nonzero_day {first_nz} != bytes "
                    f"{nz_days[0]}"
                )
            if nz_days[-1] != last_nz:
                refusals.append(
                    f"{sleeve}: declared last_nonzero_day {last_nz} != bytes "
                    f"{nz_days[-1]}"
                )

        d = first_nz
        while d <= exit_day:
            if d not in board:
                refusals.append(
                    f"{sleeve}: dashboard gap at {d.isoformat()} inside the fund epoch"
                )
            d += timedelta(days=1)

        fund_txns = [
            t
            for t in txns
            if t["fund"] == fund_id and t["type"] in ("BUY", "SELL", "MAX_S")
        ]
        fills = []
        for t in fund_txns:
            m = txn_fill(t)
            fills.append(
                {
                    "txn": t,
                    "kind": t["type"],
                    "amount": D(t["amount"]),
                    "fee": t["created_to_cover_fees"] == "True",
                    "fill_at": m,
                    "fill_day": m.date() if m else None,
                    "time_utc": _udt(t["time"]),
                }
            )
        fills.sort(
            key=lambda f: f["fill_at"] or datetime.min.replace(tzinfo=timezone.utc)
        )

        net_fills = sum((f["amount"] for f in fills), Decimal(0))
        net_fills_by_fund[fund_id] = net_fills
        fee_sells = [f for f in fills if f["fee"]]
        fee_sells_by_fund[fund_id] = fee_sells
        nonfee = [f for f in fills if not f["fee"]]

        opening = None
        for f in nonfee:
            if f["kind"] == "BUY" and f["fill_day"] == first_nz:
                opening = f
                break
        if opening is None:
            refusals.append(
                f"{sleeve}: no purchase with fill day == first_nonzero_day {first_nz}"
            )

        flows_out = []
        for f in nonfee:
            if opening is not None and f["txn"]["id"] == opening["txn"]["id"]:
                rec = f["fill_day"]
            else:
                rec = f["fill_day"] + timedelta(days=1)
            f["recognition_day"] = rec
            flows_out.append(
                {
                    "fill_day": f["fill_day"].isoformat(),
                    "recognition_day": rec.isoformat(),
                    "txn_id": f["txn"]["id"],
                    "kind": f["kind"],
                    "amount": fmt(f["amount"]),
                    "fill_at_utc": _iso(f["fill_at"]) if f["fill_at"] else None,
                    "time_utc": _iso(f["time_utc"]),
                }
            )

        # Pairing flows are copied only after their recognition day is set
        # (SPEC-QB 2: fill day + 1, the opening purchase: its fill day).
        for f in nonfee:
            all_flows.append({**f, "sleeve": sleeve, "fund_id": fund_id})

        flow_in_by_day: dict[date, Decimal] = {}
        flow_out_by_day: dict[date, Decimal] = {}
        for f in nonfee:
            if f["kind"] == "BUY":
                flow_in_by_day[f["recognition_day"]] = (
                    flow_in_by_day.get(f["recognition_day"], Decimal(0)) + f["amount"]
                )
            else:
                flow_out_by_day[f["recognition_day"]] = (
                    flow_out_by_day.get(f["recognition_day"], Decimal(0)) + f["amount"]
                )

        rows_out = []
        nav_prev = Decimal(0)
        twr = Decimal(1)
        own_pnl_total = Decimal(0)
        d = first_nz
        while d <= exit_day:
            nav_end = board[d]["value"] if d in board else Decimal(0)
            fi = flow_in_by_day.get(d, Decimal(0))
            fo = flow_out_by_day.get(d, Decimal(0))
            own_pnl = nav_end - nav_prev - fi - fo
            r = (own_pnl / nav_prev) if nav_prev > 0 else None
            if r is not None:
                twr *= 1 + r
            own_pnl_total += own_pnl
            if d == first_nz:
                event = "OPEN"
            elif d == exit_day:
                event = "EXIT"
            elif fi > 0 and fo < 0:
                event = "BOTH"
            elif fi > 0:
                event = "IN"
            elif fo < 0:
                event = "OUT"
            else:
                event = ""
            rows_out.append(
                {
                    "day": d.isoformat(),
                    "event": event,
                    "nav_prev": fmt(nav_prev),
                    "flow_in": fmt(fi),
                    "flow_out": fmt(fo),
                    "nav_end": fmt(nav_end),
                    "own_pnl": fmt(own_pnl),
                    "interval_return": fmt(r),
                }
            )
            nav_prev = nav_end
            d += timedelta(days=1)

        cb_cut = board[cutoff]["cost_basis"] if cutoff in board else None
        if cb_cut is None:
            refusals.append(f"{sleeve}: dashboard has no cutoff-day row {cutoff}")
            cb_diff = None
        else:
            cb_diff = net_fills - cb_cut
            if abs(cb_diff) > tol["cost_basis"]:
                refusals.append(
                    f"{sleeve}: net fills {fmt(net_fills)} vs dashboard cost_basis "
                    f"{fmt(cb_cut)} differ by {fmt(cb_diff)} (> {fmt(tol['cost_basis'])})"
                )
        opening_value = board[first_nz]["value"]
        opening_purchase = opening["amount"] if opening else None
        if opening is not None:
            o_diff = opening_value - opening_purchase
            if abs(o_diff) > D(fd["opening_tolerance"]):
                refusals.append(
                    f"{sleeve}: opening value {fmt(opening_value)} vs opening "
                    f"purchase {fmt(opening_purchase)} differ by {fmt(o_diff)} "
                    f"(> {fd['opening_tolerance']})"
                )
        last_value = board[last_nz]["value"]
        exits = [f for f in nonfee if f["kind"] == "MAX_S"]
        exit_proceeds = -sum((f["amount"] for f in exits), Decimal(0))
        if last_value != 0:
            e_rel = abs(exit_proceeds - last_value) / abs(last_value)
            if e_rel > tol["exit_relative"]:
                refusals.append(
                    f"{sleeve}: MAX_S proceeds {fmt(exit_proceeds)} vs last "
                    f"non-zero value {fmt(last_value)} relative difference "
                    f"{fmt(e_rel)} (> {fmt(tol['exit_relative'])})"
                )

        out_funds[sleeve] = {
            "fund_id": fund_id,
            "first_day": first_nz.isoformat(),
            "last_nonzero_day": last_nz.isoformat(),
            "exit_recognition_day": exit_day.isoformat(),
            "rows": rows_out,
            "net_external_flow": fmt(sum((f["amount"] for f in nonfee), Decimal(0))),
            "fee_sells_net": fmt(sum((f["amount"] for f in fee_sells), Decimal(0))),
            "own_pnl_total": fmt(own_pnl_total),
            "twr": fmt(twr - 1),
            "flows": flows_out,
            "fee_sells": [
                {
                    "txn_id": f["txn"]["id"],
                    "fill_day": f["fill_day"].isoformat(),
                    "amount": fmt(f["amount"]),
                }
                for f in fee_sells
            ],
            "net_fills": fmt(net_fills),
            "dashboard_cost_basis": fmt(cb_cut),
            "cost_basis_difference": fmt(cb_diff),
            "opening_value": fmt(opening_value),
            "opening_purchase": fmt(opening_purchase),
            "exit_proceeds": fmt(exit_proceeds),
            "last_nonzero_value": fmt(last_value),
        }

    # ---- fund 107 (out of scope): fills for pairing + volatis_fills ----
    volatis_fills = []
    for t in txns:
        if t["fund"] != "107" or t["type"] not in ("BUY", "SELL", "MAX_S"):
            continue
        fee = t["created_to_cover_fees"] == "True"
        m_rule = fill_moment(t, include_fee_orders=fee) or fill_moment(
            t, include_fee_orders=True
        )
        m_all = fill_moment(t, include_fee_orders=True)
        rec = {
            "txn_id": t["id"],
            "kind": t["type"],
            "amount": fmt(D(t["amount"])),
            "fill_day_excluding_fee_orders": m_rule.date().isoformat()
            if m_rule
            else None,
            "fill_day_including_all_orders": m_all.date().isoformat()
            if m_all
            else None,
            "fee_sell": fee,
        }
        volatis_fills.append(rec)
        amt = D(t["amount"])
        net_fills_by_fund["107"] = net_fills_by_fund.get("107", Decimal(0)) + amt
        if not fee:
            all_flows.append(
                {
                    "txn": t,
                    "kind": t["type"],
                    "amount": amt,
                    "fee": False,
                    "fill_at": m_rule,
                    "fill_day": m_rule.date() if m_rule else None,
                    "time_utc": _udt(t["time"]),
                    "sleeve": None,
                    "fund_id": "107",
                }
            )
    volatis_fills.sort(
        key=lambda r: (r["fill_day_excluding_fee_orders"] or "", r["txn_id"])
    )

    # ---- closure 3: nav total ----
    total_board = dash[declared["total_dashboard"]]  # type: ignore[index]
    nav_days_total = sorted(d for d in total_board if d <= cutoff)
    max_abs = Decimal(0)
    exempt_report = []
    for d in nav_days_total:
        s = Decimal(0)
        for name in ("crisis", "singleindex", "volatis"):
            s += dash[name][d]["value"] if d in dash[name] else Decimal(0)
        diff = s - total_board[d]["value"]
        if d in exempt_days:
            exempt_report.append({"day": d.isoformat(), "difference": fmt(diff)})
        else:
            if abs(diff) > tol["nav_total"]:
                refusals.append(
                    f"nav_total: day {d.isoformat()} funds sum minus Total = "
                    f"{fmt(diff)} (> {fmt(tol['nav_total'])})"
                )
            if abs(diff) > max_abs:
                max_abs = abs(diff)

    # ---- closure 4: account cash ----
    deposits = Decimal(0)
    deposit_count = 0
    ignored_transfers = 0
    for r in kept:
        if r["activity_type"] != "transfer":
            continue
        if r["type"] == "DEPOSIT" and r["status"] == "C":
            deposits += D(r["amount"])
            deposit_count += 1
        elif r["type"] == "WITHDRAWAL" and r["status"] == "C":
            deposits -= D(r["amount"])
        else:
            ignored_transfers += 1
    aum_fees = Decimal(0)
    partner_fees = Decimal(0)
    for r in kept:
        if r["activity_type"] == "journal" and r["status"] == "COMPLETE":
            if r["type"] == "AUM_FEE":
                aum_fees += D(r["amount"])
            elif r["type"] == "PARTNER_SUB_FEE":
                partner_fees += D(r["amount"])
    dividends_rows = [r for r in kept if r["activity_type"] == "dividend"]
    dividends_total = sum((D(r["amount"]) for r in dividends_rows), Decimal(0))
    net_fund_flows = sum(net_fills_by_fund.values(), Decimal(0))

    settled = None
    for r in accounts_rows:
        if r["type"] == "alpaca":
            settled = D(r["settled_balance"])
    if settled is None:
        refusals.append("accounts.csv: no row with type=alpaca")

    derived_cash = deposits - net_fund_flows + dividends_total - aum_fees - partner_fees
    residual = derived_cash - settled if settled is not None else None
    cash_residual = declared["open_items"]["cash_residual"]  # type: ignore[index]
    declared_residual = D(cash_residual["amount"])
    residual_tol = D(cash_residual["tolerance"])
    if residual is not None and abs(residual - declared_residual) > residual_tol:
        refusals.append(
            f"cash closure: derived residual {fmt(residual)} vs declared "
            f"{fmt(declared_residual)} differ by {fmt(residual - declared_residual)} "
            f"(> {fmt(residual_tol)})"
        )

    # ---- closure 5: dividends attribution ----
    sym_funds: dict[str, set[str]] = {}
    for o in orders:
        if o["symbol"] and o["symbol"] != "$USD" and o["fund"]:
            sym_funds.setdefault(o["symbol"], set()).add(o["fund"])
    sleeve_by_fund = {funds_decl[s]["fund_id"]: s for s in funds_decl}
    attr_sleeves: dict[str, Decimal] = {}
    attr_counter: dict[str, Decimal] = {}
    unattributed = Decimal(0)
    div_list = []
    for r in dividends_rows:
        day = _udt(r["date"]).date()
        amt = D(r["amount"])
        fs = sym_funds.get(r["symbol"], set())
        if len(fs) == 1:
            fid = next(iter(fs))
            if fid in sleeve_by_fund:
                sl = sleeve_by_fund[fid]
                attr_sleeves[sl] = attr_sleeves.get(sl, Decimal(0)) + amt
                div_list.append(
                    {
                        "sleeve": sl,
                        "day": day.isoformat(),
                        "symbol": r["symbol"],
                        "amount": fmt(amt),
                        "after_exit": day
                        > date.fromisoformat(funds_decl[sl]["last_nonzero_day"]),
                    }
                )
            else:
                attr_counter[fid] = attr_counter.get(fid, Decimal(0)) + amt
        else:
            unattributed += amt
            if len(fs) > 1:
                refusals.append(
                    f"dividend {r['id']} ({r['symbol']} {r['date']}): traded by "
                    f"several funds {sorted(fs)}"
                )
    div_list.sort(key=lambda x: (x["sleeve"], x["day"], x["symbol"]))

    # ---- SPEC-QB 4: pairing ----
    out_of_scope = declared["out_of_scope_funds"]["107"]  # type: ignore[index]
    counterparty_account = str(out_of_scope["counterparty_account_id"])

    def flow_acct(f: dict[str, object]) -> str:
        if f["fund_id"] in sleeve_by_fund:
            return parent + "-" + sleeve_by_fund[f["fund_id"]]
        return counterparty_account

    def flow_side(f: dict[str, object]) -> str:
        return sleeve_by_fund.get(f["fund_id"], f["fund_id"])

    def recog_day(f: dict[str, object]) -> date:
        if "recognition_day" in f:
            return f["recognition_day"]  # type: ignore[return-value]
        return f["fill_day"] + timedelta(days=1)  # type: ignore[operator]

    outflows = [f for f in all_flows if f["amount"] < 0]  # type: ignore[operator]
    inflows = [f for f in all_flows if f["amount"] > 0]  # type: ignore[operator]

    cand: dict[int, list[int]] = {}
    for oi, o in enumerate(outflows):
        for ii, i in enumerate(inflows):
            if o["fund_id"] == i["fund_id"]:
                continue
            if o["fill_day"] != i["fill_day"]:
                continue
            if abs(D((o["time_utc"] - i["time_utc"]).total_seconds())) > window_seconds:  # type: ignore[operator]
                continue
            if abs(abs(o["amount"]) - abs(i["amount"])) > tol["pairing_amount"]:  # type: ignore[arg-type]
                continue
            cand.setdefault(oi, []).append(ii)

    in_cand: dict[int, list[int]] = {}
    for oi, iis in cand.items():
        for ii in iis:
            in_cand.setdefault(ii, []).append(oi)
    for oi, iis in cand.items():
        if len(iis) > 1:
            refusals.append(
                f"pairing: outflow txn {outflows[oi]['txn']['id']} has "
                f"{len(iis)} candidate inflows"
            )
    for ii, ois in in_cand.items():
        if len(ois) > 1:
            refusals.append(
                f"pairing: inflow txn {inflows[ii]['txn']['id']} has "
                f"{len(ois)} candidate outflows"
            )

    matched: dict[tuple[int, int], Decimal] = {}
    for oi, iis in cand.items():
        for ii in iis:
            if len(iis) == 1 and len(in_cand.get(ii, [])) == 1:
                matched[(oi, ii)] = min(
                    abs(outflows[oi]["amount"]),
                    abs(inflows[ii]["amount"]),  # type: ignore[arg-type]
                )

    pairs = []
    used_out: dict[int, Decimal] = {}
    used_in: dict[int, Decimal] = {}
    for (oi, ii), amt in matched.items():
        o, i = outflows[oi], inflows[ii]
        both_retired = o["fund_id"] in sleeve_by_fund and i["fund_id"] in sleeve_by_fund
        retired_side = o if o["fund_id"] in sleeve_by_fund else i
        pairs.append(
            {
                "event_day": o["fill_day"],
                "recognition_day": recog_day(retired_side),
                "amount": amt,
                "out": o,
                "in": i,
                "kind": "REALLOCATION",
                "pairing": "SLEEVE_PAIR" if both_retired else "COUNTERPARTY_PAIR",
                "_sort_fill": o["fill_at"],
            }
        )
        used_out[oi] = used_out.get(oi, Decimal(0)) + amt
        used_in[ii] = used_in.get(ii, Decimal(0)) + amt

    remainders = []
    for oi, o in enumerate(outflows):
        rem = abs(o["amount"]) - used_out.get(oi, Decimal(0))  # type: ignore[arg-type]
        if rem > 0:
            remainders.append(
                {
                    "event_day": o["fill_day"],
                    "recognition_day": recog_day(o),
                    "amount": rem,
                    "out": o,
                    "in": None,
                    "kind": "UNALLOCATED_CASH",
                    "pairing": "UNALLOCATED_REMAINDER",
                    "_sort_fill": o["fill_at"],
                    "_txn": o["txn"]["id"],
                }
            )
    for ii, i in enumerate(inflows):
        rem = abs(i["amount"]) - used_in.get(ii, Decimal(0))  # type: ignore[arg-type]
        if rem > 0:
            remainders.append(
                {
                    "event_day": i["fill_day"],
                    "recognition_day": recog_day(i),
                    "amount": rem,
                    "out": None,
                    "in": i,
                    "kind": "UNALLOCATED_CASH",
                    "pairing": "UNALLOCATED_REMAINDER",
                    "_sort_fill": i["fill_at"],
                    "_txn": i["txn"]["id"],
                }
            )

    def day_key(p: dict[str, object]) -> date:
        return p["event_day"] or date.min  # type: ignore[return-value]

    paired_sorted = sorted(
        pairs,
        key=lambda p: (
            day_key(p),
            p["_sort_fill"] or datetime.min.replace(tzinfo=timezone.utc),
        ),
    )
    rem_sorted = sorted(
        remainders,
        key=lambda p: (
            day_key(p),
            p["_sort_fill"] or datetime.min.replace(tzinfo=timezone.utc),
            p["_txn"],
        ),
    )
    by_day: dict[date, dict[str, list[dict[str, object]]]] = {}
    for p in paired_sorted + rem_sorted:
        by_day.setdefault(day_key(p), {"paired": [], "rem": []})
    for p in paired_sorted:
        by_day[day_key(p)]["paired"].append(p)
    for p in rem_sorted:
        by_day[day_key(p)]["rem"].append(p)

    pairs_out = []
    legs: list[dict[str, object]] = []
    for d in sorted(by_day):
        for p in by_day[d]["paired"] + by_day[d]["rem"]:
            o, i = p["out"], p["in"]
            out_acct = flow_acct(o) if o else unallocated
            in_acct = flow_acct(i) if i else unallocated
            pairs_out.append(
                {
                    "event_day": p["event_day"].isoformat(),
                    "recognition_day": p["recognition_day"].isoformat(),
                    "amount": fmt(p["amount"]),
                    "out_account": out_acct,
                    "in_account": in_acct,
                    "kind": p["kind"],
                    "pairing": p["pairing"],
                    "from": flow_side(o) if o else "unallocated",
                    "to": flow_side(i) if i else "unallocated",
                }
            )
            # One leg per side; each leg carries its own side's recognition day
            # (SPEC-QB 2: fill day + 1, opening purchase: fill day).
            legs.append(
                {
                    "account": out_acct,
                    "counterparty_account": in_acct,
                    "cls": "INTERNAL_OUT",
                    "amount": p["amount"],
                    "event_day": p["event_day"],
                    "recognition_day": recog_day(o) if o else p["event_day"],
                    "pairing": p["pairing"],
                }
            )
            legs.append(
                {
                    "account": in_acct,
                    "counterparty_account": out_acct,
                    "cls": "INTERNAL_IN",
                    "amount": p["amount"],
                    "event_day": p["event_day"],
                    "recognition_day": recog_day(i) if i else p["event_day"],
                    "pairing": p["pairing"],
                }
            )

    assumptions.extend(
        [
            "Cutoff filter: a row is 'dated after the cutoff' by its own date "
            "field (filled_txn: time; order: filled_at; transfer/journal: "
            "created_at; dividend: date), converted to the UTC date.",
            "Pairs: out_account/in_account use '<parent>-<sleeve>' for retired "
            "funds, the declared counterparty_account_id (the parent account "
            "itself) for fund 107, and '<parent>-unallocated' for the perimeter "
            "pseudo-account; from/to carry the sleeve id ('crisis'/"
            "'singleindex'), the fund id ('107'), or 'unallocated'.",
            "Pairs: recognition_day of a remainder pair is the retired fund's "
            "recognition day when a retired fund is a side, else (fund 107 only) "
            "fill day + 1, mirroring the reallocation rule.",
            "Pairs: within one event day, reallocations are ordered by the "
            "outflow's fill moment, then remainders by the flow's fill moment "
            "(ties by txn id); the spec fixes only 'paired pairs first, then "
            "remainders'.",
            "Exit-proceeds check: 'equal within 1%' is read as |proceeds - "
            "last_nonzero_value| / |last_nonzero_value| <= exit_relative.",
            "Nav-total closure is checked on every day of the Total dashboard up "
            "to the cutoff; a fund without a row that day counts as 0.",
            "Dividend day is the UTC date of the dividend row's 'date' field; "
            "after_exit compares it to the sleeve's last_nonzero_day.",
            "TWR chains only rows whose interval_return is not null "
            "(nav_prev > 0), i.e. the opening row contributes no factor.",
        ]
    )

    return {
        "funds": out_funds,
        "pairs": pairs_out,
        "legs": legs,
        "closure": {
            "nav_total": {
                "days_checked": len(nav_days_total),
                "max_abs_difference": fmt(max_abs),
                "exempt_days": exempt_report,
            },
            "cash": {
                "deposits": fmt(deposits),
                "deposit_count": deposit_count,
                "ignored_transfers": ignored_transfers,
                "net_fund_flows_by_fund": {
                    k: fmt(v) for k, v in sorted(net_fills_by_fund.items())
                },
                "net_fund_flows": fmt(net_fund_flows),
                "dividends": fmt(dividends_total),
                "aum_fees": fmt(aum_fees),
                "partner_subscription_fees": fmt(partner_fees),
                "derived_cash": fmt(derived_cash),
                "provider_settled_cash": fmt(settled),
                "residual": fmt(residual),
            },
            "dividends": {
                "total": fmt(dividends_total),
                "attributed_to_sleeves": {
                    k: fmt(v) for k, v in sorted(attr_sleeves.items())
                },
                "attributed_to_counterparty_funds": {
                    k: fmt(v) for k, v in sorted(attr_counter.items())
                },
                "unattributed": fmt(unattributed),
            },
            "rows_after_cutoff_excluded": after_cutoff,
        },
        "dividends": div_list,
        "volatis_fills": volatis_fills,
        "refusals": refusals,
        "assumptions": assumptions,
    }


# ---------------------------------------------------------------- epochs


def ibkr_epochs(
    facts: dict[str, object], parent_entry: dict[str, object]
) -> list[Epoch]:
    """SPEC-HARNESS 3: one epoch per real IBKR sleeve (never the residual)."""
    parent = str(parent_entry["parent_account_id"])
    pairs: list[dict[str, object]] = facts["pairs"]  # type: ignore[assignment]
    sleeve_rows: dict[str, list[dict[str, object]]] = facts["sleeves"]  # type: ignore[assignment]
    epochs = []
    for sleeve_meta in parent_entry["sleeves"]:  # type: ignore[index]
        sid = str(sleeve_meta["sleeve_id"])
        rows = sleeve_rows.get(sid, [])
        if not rows:
            continue
        account_id = f"{parent}-{sid}"
        nav = [
            (date.fromisoformat(str(r["day"])), D(r["nav_end_after_alloc"]))
            for r in rows
        ]
        legs: list[Leg] = []
        for pair in pairs:
            day = date.fromisoformat(str(pair["day"]))
            amount = D(pair["amount"])
            if pair["in_account"] == account_id:
                legs.append(Leg(day, day, "INTERNAL_IN", amount))
            if pair["out_account"] == account_id:
                legs.append(Leg(day, day, "INTERNAL_OUT", amount))
        last_event = str(rows[-1]["event"])
        effective_end = None
        if last_event == "EXIT":
            effective_end = date.fromisoformat(str(rows[-1]["day"]))
            cumulative_alloc = sum((D(r["alloc_total"]) for r in rows), Decimal(0))
            if cumulative_alloc != 0:
                legs.append(
                    Leg(
                        effective_end,
                        effective_end,
                        "INTERNAL_OUT" if cumulative_alloc > 0 else "INTERNAL_IN",
                        abs(cumulative_alloc),
                    )
                )
        legs.sort(
            key=lambda leg: (leg.recognition_day, leg.event_day, leg.cls, leg.amount)
        )
        epochs.append(
            Epoch(
                account_id=account_id,
                strategy_id=str(sleeve_meta["strategy_id"]),
                effective_start=nav[0][0],
                effective_end=effective_end,
                nav=nav,
                legs=legs,
            )
        )
    return epochs


def quantbase_epochs(
    facts: dict[str, object], parent_entry: dict[str, object], parent: str
) -> list[Epoch]:
    """SPEC-HARNESS 3 (as corrected): one epoch per retired Quantbase fund.

    Which ACCOUNT a leg sits on decides, not its counterparty: every leg on
    a retired fund's own account is an epoch leg of that fund, INCLUDING the
    retired side of a COUNTERPARTY_PAIR; only legs on fund 107's own account
    (the other side of such a pair, and fund 107's own activity) are never
    epoch legs (CORRECTION.md).
    """
    strategy_by_sleeve = {
        str(s["sleeve_id"]): str(s["strategy_id"])
        for s in parent_entry["sleeves"]  # type: ignore[index]
    }
    funds: dict[str, dict[str, object]] = facts["funds"]  # type: ignore[assignment]
    legs_all: list[dict[str, object]] = facts["legs"]  # type: ignore[assignment]
    epochs = []
    for sleeve in sorted(funds):
        fund = funds[sleeve]
        account_id = f"{parent}-{sleeve}"
        nav = [
            (date.fromisoformat(str(r["day"])), D(r["nav_end"]))
            for r in fund["rows"]  # type: ignore[index]
        ]
        # Epoch legs: every SPEC-QB 4 leg that sits on the fund's own
        # account, INCLUDING the retired side of a COUNTERPARTY_PAIR (the
        # account a leg sits on decides, not its counterparty; CORRECTION.md).
        # Legs on fund 107's own account never match this filter: their
        # account is the declared counterparty account, not the fund's.
        legs = [
            Leg(
                leg["recognition_day"],  # type: ignore[arg-type]
                leg["event_day"],  # type: ignore[arg-type]
                str(leg["cls"]),
                D(leg["amount"]),
            )
            for leg in legs_all
            if leg["account"] == account_id
        ]
        legs.sort(
            key=lambda leg: (leg.recognition_day, leg.event_day, leg.cls, leg.amount)
        )
        epochs.append(
            Epoch(
                account_id=account_id,
                strategy_id=strategy_by_sleeve[sleeve],
                effective_start=nav[0][0],
                effective_end=nav[-1][0],
                nav=nav,
                legs=legs,
            )
        )
    return epochs


def classify_universe(
    crosswalk_rows: list[dict[str, object]],
    parents: list[dict[str, object]],
    epochs: list[Epoch],
) -> dict[str, object]:
    """Residual parents, retained strategies, registry and epoch id lists."""
    included_strategy_by_account: dict[str, set[str]] = {}
    registry: set[str] = set()
    for row in crosswalk_rows:
        strategy_id = str(row["strategy_id"])
        registry.add(strategy_id)
        if str(row.get("product_epoch_disposition")) == "INCLUDED_PRODUCT_EPOCH":
            included_strategy_by_account.setdefault(str(row["account_id"]), set()).add(
                strategy_id
            )
    residual_parents = []
    for parent_entry in parents:
        parent = str(parent_entry["parent_account_id"])
        included = included_strategy_by_account.get(parent, set())
        if any(
            str(s["strategy_id"]) in included
            for s in parent_entry["sleeves"]  # type: ignore[index]
        ):
            residual_parents.append(parent)
    derived_strategies = {epoch.strategy_id for epoch in epochs}
    retained = sorted(derived_strategies - registry)
    registry |= derived_strategies
    epoch_ids = sorted(
        "{}|{}|{}|{}".format(
            epoch.account_id,
            epoch.strategy_id,
            epoch.effective_start.isoformat(),
            epoch.effective_end.isoformat()
            if epoch.effective_end is not None
            else "open",
        )
        for epoch in epochs
    )
    return {
        "residual_parent_ids": sorted(residual_parents),
        "retained_strategy_ids": retained,
        "retained_strategy_count": len(retained),
        "registry_strategy_count": len(registry),
        "derived_epoch_ids": epoch_ids,
        "derived_epoch_count": len(epochs),
    }


def epoch_metrics(
    epoch: Epoch,
    engine: Engine,
    cutoff: date,
    inputs_used: list[str],
    notes: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    """The section 4 metrics of one derived epoch through the existing engine."""
    metrics: list[dict[str, object]] = []
    not_derivable: list[dict[str, object]] = []
    first_day = epoch.nav[0][0]
    last_day = epoch.nav[-1][0]
    opening = epoch.nav[0][1]
    closing = epoch.nav[-1][1]
    capital_flows = [
        leg for leg in epoch.legs if first_day < leg.recognition_day <= last_day
    ]
    flows_by_day: dict[date, Decimal] = {}
    for leg in capital_flows:
        flows_by_day[leg.recognition_day] = (
            flows_by_day.get(leg.recognition_day, Decimal(0)) + leg.signed
        )
    flow_in_total = sum(
        (leg.amount for leg in capital_flows if leg.cls == "INTERNAL_IN"), Decimal(0)
    )
    flow_out_total = sum(
        (leg.amount for leg in capital_flows if leg.cls == "INTERNAL_OUT"), Decimal(0)
    )
    net_capital = opening + flow_in_total - flow_out_total
    net_flows = flow_in_total - flow_out_total

    metrics.append(
        engine.metric(
            "date.epoch.start",
            epoch.account_id,
            first_day.isoformat(),
            "date",
            inputs_used,
            cutoff.isoformat(),
            window="",
        )
    )
    metrics.append(
        engine.metric(
            "date.epoch.end",
            epoch.account_id,
            last_day.isoformat() if epoch.effective_end is not None else "open",
            "date",
            inputs_used,
            cutoff.isoformat(),
            window="",
        )
    )
    for metric_id, value in (
        ("capital.epoch.opening", opening),
        ("capital.epoch.net_capital", net_capital),
        ("capital.epoch.closing", closing),
    ):
        metrics.append(
            engine.metric(
                metric_id,
                epoch.account_id,
                engine.money(value),
                "USD",
                inputs_used,
                cutoff.isoformat(),
                window="",
            )
        )
    as_of = last_day.isoformat()
    metrics.append(
        engine.metric(
            "flows.account.net",
            epoch.account_id,
            engine.money(net_flows),
            "USD",
            inputs_used,
            as_of,
            window="ITD",
            notes=notes,
        )
    )
    metrics.append(
        engine.metric(
            "flows.account.count",
            epoch.account_id,
            str(len(capital_flows)),
            "count",
            inputs_used,
            as_of,
            window="ITD",
            notes=notes,
        )
    )
    summary: dict[str, object] = {
        "opening": opening,
        "net_capital": net_capital,
        "closing": closing,
        "flows_net": net_flows,
        "flows_count": len(capital_flows),
        "twr": None,
        "mwr": None,
    }
    try:
        twr = engine.time_weighted_return(epoch.nav, flows_by_day, {})
    except ValueError as error:
        not_derivable.append(
            engine.not_derivable_row("twr.account", epoch.account_id, str(error), "ITD")
        )
    else:
        summary["twr"] = twr
        metrics.append(
            engine.metric(
                "twr.account",
                epoch.account_id,
                engine.ratio(twr),
                "ratio",
                inputs_used,
                as_of,
                window="ITD",
                notes=notes,
            )
        )
    mwr_flows = [(first_day, -opening)]
    mwr_flows.extend(
        (occurred_on, -amount) for occurred_on, amount in sorted(flows_by_day.items())
    )
    mwr_flows.append((last_day, closing))
    mwr = engine.xirr(mwr_flows)
    if mwr is None:
        not_derivable.append(
            engine.not_derivable_row(
                "mwr.account", epoch.account_id, XIRR_NOT_DERIVABLE_REASON, "ITD"
            )
        )
    else:
        summary["mwr"] = mwr
        metrics.append(
            engine.metric(
                "mwr.account",
                epoch.account_id,
                engine.ratio(mwr),
                "ratio",
                inputs_used,
                as_of,
                window="ITD",
                notes=notes,
            )
        )
    return metrics, not_derivable, summary


# ---------------------------------------------------------------- top level


def derive_sleeves(
    *,
    sleeve_inputs_path: Path,
    qe_root: Path,
    repo_root: Path,
    cutoff: date,
    engine: Engine,
) -> dict[str, object]:
    """Run the whole SPEC-HARNESS sleeve derivation fail-closed."""
    selection = load_sleeve_inputs(sleeve_inputs_path, qe_root, repo_root, cutoff)

    crosswalk = _role_json(selection, "crosswalk")
    tag_map = _role_json(selection, "tag_map")
    ibkr_declared = _role_json(selection, "ibkr_declared_inputs")
    qb_declared = _role_json(selection, "quantbase_declared_inputs")
    catalog = _role_json(selection, "ibkr_catalog")

    # Content checks (fail closed, SPEC-HARNESS 2).
    schema_version = catalog.get("catalog_schema_version")  # type: ignore[union-attr]
    if not isinstance(schema_version, int) or schema_version < 7:
        _fail(f"ibkr_catalog: catalog schema {schema_version!r} is below 7")
    parents = tag_map.get("parents")  # type: ignore[union-attr]
    if not isinstance(parents, list):
        _fail("tag_map: no parents list")
    ibkr_parents = [p for p in parents if p.get("provider_id") == "ibkr"]
    qb_parents = [p for p in parents if p.get("provider_id") == "quantbase"]
    if len(ibkr_parents) != 1 or len(qb_parents) != 1:
        _fail(
            "tag_map: expected exactly one 'ibkr' and one 'quantbase' parent, "
            f"found {len(ibkr_parents)} and {len(qb_parents)}"
        )
    ibkr_parent_entry = ibkr_parents[0]
    qb_parent_entry = qb_parents[0]
    ibkr_window = ibkr_declared.get("window")  # type: ignore[union-attr]
    if ibkr_window.get("last_day") != cutoff.isoformat():
        _fail(
            f"ibkr declared window last day {ibkr_window.get('last_day')!r} "
            f"differs from the cutoff {cutoff.isoformat()}"
        )
    qb_cutoff = qb_declared.get("data_cutoff")  # type: ignore[union-attr]
    if qb_cutoff != cutoff.isoformat():
        _fail(
            f"quantbase data_cutoff {qb_cutoff!r} differs from the cutoff "
            f"{cutoff.isoformat()}"
        )

    # ---------------- facts ----------------
    ibkr_parent = str(ibkr_parent_entry["parent_account_id"])
    slc = _cut_ibkr_slice(
        catalog,  # type: ignore[arg-type]
        ibkr_parent,
        str(ibkr_window["first_day"]),
        str(ibkr_window["last_day"]),
    )
    ibkr = _ibkr_facts(slc, ibkr_declared, ibkr_parent_entry)  # type: ignore[arg-type]

    dashboards = {
        "crisis": _role_csv_rows(selection, "quantbase_dashboard_crisis"),
        "singleindex": _role_csv_rows(selection, "quantbase_dashboard_singleindex"),
        "volatis": _role_csv_rows(selection, "quantbase_dashboard_volatis"),
        "total": _role_csv_rows(selection, "quantbase_dashboard_total"),
    }
    qb = _qb_facts(
        qb_declared,  # type: ignore[arg-type]
        dashboards,
        _role_csv_rows(selection, "quantbase_account_activity"),
        _role_csv_rows(selection, "quantbase_accounts"),
        cutoff,
    )
    qb_parent = str(qb_parent_entry["parent_account_id"])

    # ---------------- epochs ----------------
    ibkr_epoch_list = ibkr_epochs(ibkr, ibkr_parent_entry)
    quantbase_epoch_list = quantbase_epochs(qb, qb_parent_entry, qb_parent)
    epochs = sorted(
        [*ibkr_epoch_list, *quantbase_epoch_list], key=lambda epoch: epoch.account_id
    )
    epoch_parents = {
        **{epoch.account_id: ibkr_parent for epoch in ibkr_epoch_list},
        **{epoch.account_id: qb_parent for epoch in quantbase_epoch_list},
    }

    inputs_used = inputs_used_rows(selection)
    notes_ibkr = (
        "R2 derived sleeve epoch (SPEC-HARNESS): NAV = nav_end_after_alloc "
        "(SPEC-IBKR 6, after allocated account costs); capital flows are the "
        "SPEC-IBKR 8 legs on the sleeve account plus the exit settlement leg, "
        "recognised same-day, end-of-day."
    )
    notes_qb = (
        "R2 derived sleeve epoch (SPEC-HARNESS, as corrected): NAV = the "
        "fund's nav_end (SPEC-QB 2) through the exit recognition day; capital "
        "flows are every SPEC-QB 4 leg on the fund's own account (including "
        "the retired side of a counterparty-107 pair), end-of-day at their "
        "recognition day."
    )
    metrics: list[dict[str, object]] = []
    not_derivable: list[dict[str, object]] = []
    summaries: dict[str, dict[str, object]] = {}
    for epoch in epochs:
        notes = notes_ibkr if epoch.account_id.startswith(ibkr_parent) else notes_qb
        rows, nd, summary = epoch_metrics(epoch, engine, cutoff, inputs_used, notes)
        metrics.extend(rows)
        not_derivable.extend(nd)
        summaries[epoch.account_id] = summary

    universe = classify_universe(
        crosswalk["rows"],  # type: ignore[index]
        [ibkr_parent_entry, qb_parent_entry],
        epochs,
    )
    universe_rows = (
        ("universe.derived_epoch_ids", universe["derived_epoch_ids"], "list"),
        ("universe.derived_epoch_count", str(universe["derived_epoch_count"]), "count"),
        ("universe.retained_strategy_ids", universe["retained_strategy_ids"], "list"),
        (
            "universe.retained_strategy_count",
            str(universe["retained_strategy_count"]),
            "count",
        ),
        (
            "universe.registry_strategy_count",
            str(universe["registry_strategy_count"]),
            "count",
        ),
        ("universe.residual_parent_ids", universe["residual_parent_ids"], "list"),
    )
    for metric_id, value, unit in universe_rows:
        metrics.append(
            engine.metric(
                metric_id,
                "portfolio",
                value,
                unit,
                inputs_used,
                cutoff.isoformat(),
                window="",
            )
        )

    epoch_blocks = []
    for epoch in epochs:
        summary = summaries[epoch.account_id]
        epoch_blocks.append(
            {
                "account_id": epoch.account_id,
                "strategy_id": epoch.strategy_id,
                "effective_start": epoch.effective_start.isoformat(),
                "effective_end": (
                    epoch.effective_end.isoformat()
                    if epoch.effective_end is not None
                    else None
                ),
                "opening": fmt(summary["opening"]),  # type: ignore[arg-type]
                "closing": fmt(summary["closing"]),  # type: ignore[arg-type]
                "net_capital": fmt(summary["net_capital"]),  # type: ignore[arg-type]
                "twr": fmt(summary["twr"]),  # type: ignore[arg-type]
                "mwr": fmt(summary["mwr"]),  # type: ignore[arg-type]
                "flows_net": fmt(summary["flows_net"]),  # type: ignore[arg-type]
                "flows_count": summary["flows_count"],
                "legs": [
                    {
                        "recognition_day": leg.recognition_day.isoformat(),
                        "event_day": leg.event_day.isoformat(),
                        "class": leg.cls,
                        "amount": fmt(leg.amount),
                    }
                    for leg in epoch.legs
                ],
                "nav": {day.isoformat(): fmt(value) for day, value in epoch.nav},
            }
        )

    assumptions = [
        "SPEC-HARNESS 2 names a command line '--cutoff'; this command line has "
        "no such option (only the two new options were added), so the check "
        "compares the sleeve-inputs cutoff against the derivation's fixed "
        "cutoff constant.",
        "Duplicate roles are detected by rejecting any duplicate JSON object "
        "key anywhere in the sleeve-inputs file (parsed with "
        "object_pairs_hook); JSON alone cannot otherwise represent a "
        "duplicate role.",
        "Section 4 header says as_of = cutoff and window = '' per epoch; the "
        "twr.account/mwr.account/flows.account.net/flows.account.count row "
        "defers to the existing conventions ('as existing', 'mirror them "
        "exactly'), so those four keep window 'ITD' and as_of = the epoch's "
        "last row day, while the new date.epoch.*/capital.epoch.* and "
        "universe.* rows carry as_of = cutoff and window ''.",
        "inputs_used of the sleeve metrics list every sleeve input as "
        "'<path as given in the file> sha256:<pin>'; the existing rows' style "
        "(plain path strings) has no place for a pin, so the pin is appended "
        "to the same string.",
        "SPEC-HARNESS 3 (Quantbase), per the orchestrator's correction "
        "(CORRECTION.md): which account a leg sits on decides, not its "
        "counterparty. Every leg on a retired fund's own account is an "
        "epoch leg of that fund, INCLUDING the retired side of a "
        "COUNTERPARTY_PAIR (e.g. the singleindex MAX_S exit proceeds of "
        "2025-05-05 re-used by fund 107 within 120 seconds: the OUT leg on "
        "the singleindex account is the epoch leg that takes the epoch's "
        "NAV to zero); only legs on fund 107's own account are never epoch "
        "legs. Every derived epoch therefore closes to a zero NAV on its "
        "last row through its own legs.",
        "IBKR epoch legs: the two legs of every SPEC-IBKR 8 pair are split by "
        "account; a pair never has both legs on the same sleeve account, so "
        "each pair contributes at most one epoch leg per sleeve.",
        "Epoch legs are emitted sorted by (recognition_day, event_day, class, "
        "amount); the spec fixes the set but not the emission order.",
        "A tag-map sleeve with no SPEC-IBKR 6 rows (no declared funding "
        "events) yields no derived epoch; all five real sleeves have rows "
        "here.",
        "root 'repo' resolves to the directory holding contracts/ (this "
        "tree's root, two levels above this file's parent's parent).",
    ]
    assumptions = (
        assumptions + ibkr["assumptions"] + qb["assumptions"]  # type: ignore[operator]
    )
    refusals = ibkr["refusals"] + qb["refusals"]  # type: ignore[operator]

    sleeve_derivation = {
        "schema": SLEEVE_DERIVATION_SCHEMA,
        "inputs": block_inputs(selection),
        "epochs": epoch_blocks,
        "closure": {
            "ibkr": ibkr["closure_summary"],
            "quantbase": qb["closure"],
        },
        "disclosures": {
            "ibkr": ibkr["disclosures"],
            "quantbase": {
                "dividends": qb["dividends"],
                "volatis_fills": qb["volatis_fills"],
            },
        },
        "assumptions": assumptions,
        "refusals": refusals,
    }

    convention_notes = [
        "R2 sleeve epochs (SPEC-HARNESS): a derived epoch is one IBKR sleeve "
        "(NAV = nav_end_after_alloc, first row day to EXIT day) or one retired "
        "Quantbase fund (NAV = nav_end, first non-zero day through the exit "
        "recognition day); opening = the first NAV observation and capital "
        "flows are the paired internal legs recognised after the first row "
        "day (plus the IBKR exit settlement leg releasing the cumulative "
        "allocated cost).",
        "Derived-epoch returns use this derivation's existing account-epoch "
        "engine unchanged: date-only end-of-day TWR (not the facts' "
        "beginning-of-day daily chaining) and ACTUAL_365 XIRR.",
        "Residual parents (a sleeve strategy_id equals an INCLUDED baseline "
        "crosswalk epoch of the same account): their account-level "
        "twr/mwr/flows metrics are replaced by the derived epochs; every "
        "other metric is untouched.",
    ]

    return {
        "metrics": metrics,
        "not_derivable": not_derivable,
        "sleeve_derivation": sleeve_derivation,
        "residual_parent_ids": universe["residual_parent_ids"],
        "inputs": input_header_rows(selection),
        "convention_notes": convention_notes,
        # SPEC-COMBINED: in-memory only (never serialized into the output
        # documents); the combined run checks the effective epoch perimeter
        # against the declared manifest and pools strategy MWR from these.
        # Serializable so the whole result stays JSON-renderable.
        "epoch_records": [
            {
                "account_id": epoch.account_id,
                "parent_account_id": epoch_parents[epoch.account_id],
                "strategy_id": epoch.strategy_id,
                "effective_start": epoch.effective_start.isoformat(),
                "effective_end": (
                    epoch.effective_end.isoformat()
                    if epoch.effective_end is not None
                    else None
                ),
                "nav": [[day.isoformat(), fmt(value)] for day, value in epoch.nav],
                "legs": [
                    {
                        "recognition_day": leg.recognition_day.isoformat(),
                        "event_day": leg.event_day.isoformat(),
                        "class": leg.cls,
                        "amount": fmt(leg.amount),
                    }
                    for leg in epoch.legs
                ],
            }
            for epoch in epochs
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qe-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--inputs",
        type=Path,
        default=None,
        help=(
            "acceptance-inputs manifest JSON selecting and pinning the inputs; "
            "when omitted, the built-in v1 input selection is used"
        ),
    )
    parser.add_argument(
        "--sleeve-inputs",
        type=Path,
        default=None,
        help=(
            "qe-cp-sleeve-inputs/1 file selecting and pinning the sleeve "
            "derivation inputs; the derived-sleeve epoch metrics are appended "
            "and a top-level sleeve_derivation block is added"
        ),
    )
    parser.add_argument(
        "--sleeves-only",
        action="store_true",
        help=(
            "skip every other derivation and write a document with only the "
            "sleeve metrics and the sleeve_derivation block "
            "(requires --sleeve-inputs)"
        ),
    )
    args = parser.parse_args()
    if args.sleeves_only and args.sleeve_inputs is None:
        parser.exit(2, "input error: --sleeves-only requires --sleeve-inputs\n")
    manifest = None
    if args.inputs is not None:
        try:
            manifest = load_inputs_manifest(args.inputs)
        except ManifestError as error:
            parser.exit(1, f"error: {error}\n")
    try:
        if args.sleeves_only:
            sleeve_result = _derive_sleeve_block(args.qe_root, args.sleeve_inputs)
            result = {
                "schema": "qe-cp-derivation/1",
                "derivation": "b",
                "model": "independent-standalone-python",
                "code_commit": code_commit(),
                "inputs": sleeve_result["inputs"],
                "convention_notes": sleeve_result["convention_notes"],
                "metrics": sleeve_result["metrics"],
                "not_derivable": sleeve_result["not_derivable"],
                "sleeve_derivation": sleeve_result["sleeve_derivation"],
            }
        else:
            result = derive(args.qe_root, manifest, sleeve_inputs=args.sleeve_inputs)
    except SleeveInputError as error:
        parser.exit(2, f"input error: {error}\n")
    except ManifestError as error:
        parser.exit(1, f"error: {error}\n")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
