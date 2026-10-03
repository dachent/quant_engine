"""Independent, stdlib-only acceptance metric derivation (derivation b)."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, DecimalException, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Iterable


getcontext().prec = 50

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
NATIVE_MATRIXPORT_FAMILIES = frozenset(
    {
        "matrixport_generation_manifest",
        "matrixport_subscription_details",
        "matrixport_generation_catalog",
        "matrixport_owner_flow_coverage",
        "matrixport_owner_flow_request_manifest",
    }
)


class ManifestError(ValueError):
    """An inputs manifest or a declared input fails closed verification."""


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
    for entry in entries:
        if not isinstance(entry, dict):
            raise ManifestError("inputs manifest entry is not a JSON object")
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ManifestError("inputs manifest entry lacks a path")
        paths_by_basename[Path(raw_path).name].add(raw_path)
    resolved: dict[str, Path] = {}
    for role, basename in INPUT_ROLE_BASENAMES.items():
        candidates = paths_by_basename.get(basename, set())
        if len(candidates) != 1:
            raise ManifestError(
                f"inputs manifest must declare exactly one distinct {basename} "
                f"path for role {role}; found {len(candidates)}"
            )
        resolved[role] = Path(next(iter(candidates)))
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
    """Read this checkout's commit without invoking repository tooling."""
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
) -> dict[str, dict[str, Decimal]]:
    """Derive R8 owner capital and unallocated cash from raw custody events.

    The two selected provider sources each fund exactly one governed sleeve.
    Owner deposits and withdrawals change invested capital; sleeve buys and
    redemptions only change the derived cash line.  Events are aggregated by
    date before the strict-negative-cash check, so no intraday order is inferred.
    """
    events: dict[str, dict[date, dict[str, Decimal]]] = defaultdict(
        lambda: defaultdict(lambda: {"owner": Decimal("0"), "allocation": Decimal("0")})
    )

    def add(strategy_id: str, occurred_on: date, kind: str, amount: Decimal) -> None:
        if amount <= 0:
            raise ValueError("raw custody event amount must be positive")
        events[strategy_id][occurred_on][kind] += amount

    def event_date(timestamp: str) -> date:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("custody event timestamp lacks timezone")
        return parsed.astimezone(timezone.utc).date()

    admitted_transfer_rows: dict[str, dict[str, str]] = {}
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
                row.get("source_type")
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
            if cash < 0:
                raise ValueError(
                    f"raw custody cash is negative for {strategy_id} on {occurred_on.isoformat()}"
                )
        result[strategy_id] = {
            "invested": invested,
            "cash": cash,
            "allocation": allocation,
        }
    return result


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
    if (
        str(receipt_a.get("id")) != str(contract_receipt["comment_id"])
        or "PROVIDER_CUSTODY_PERIMETER" not in receipt_a_body
        or "each deposit adds and each withdrawal subtracts" not in receipt_a_body
        or "cash line below zero is an error" not in receipt_a_body
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


def derive(
    qe_root: Path,
    inputs_manifest: dict[str, object] | None = None,
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
        "R1/R2: account and strategy ITD uses only the current funded-membership crosswalk interval; historical predecessor assignments are excluded.",
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
    paths = [input_paths["ledger"], input_paths["crosswalk"], *authority_paths]
    absolute_paths = [qe_root / path for path in paths]
    for path in absolute_paths:
        if not path.is_file():
            raise FileNotFoundError(path)
    ledger = json.loads((qe_root / input_paths["ledger"]).read_text(encoding="utf-8"))
    crosswalk = json.loads(
        (qe_root / input_paths["crosswalk"]).read_text(encoding="utf-8")
    )
    cutoff_mismatches = [
        label
        for label, value in (
            ("ledger", ledger.get("as_of_date")),
            ("crosswalk", crosswalk.get("as_of_date")),
            ("crosswalk calculation", crosswalk.get("calculation_cutoff_date")),
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
        account_observations = window_observations(
            observations.get(account_id, []), membership_starts[account_id], None
        )
        account_window_observations[account_id] = account_observations
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
        account_values[account_id] = end_value
        # USD-converted value rows are dated by the admitted value basis (the
        # last FX-converted/ledger-admitted observation), never the raw NAV
        # date past an FX boundary; only date.source_last_observation carries
        # the raw date.
        account_value_dates[account_id] = end_date
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
        try:
            twr = time_weighted_return(
                account_observations,
                flow_data.twr_end_flows,
                flow_data.twr_beginning_flows,
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
                    end_date.isoformat(),
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
                or len(members) != 1
                or members.intersection(missing_included)
            ):
                custody_failures.add(strategy_id)
                reason = custody_capital_error or (
                    "R8 raw custody funding is missing or the provider account mapping is ambiguous"
                )
                for metric_id in (
                    "invested_capital.strategy.cutoff",
                    "capital.strategy_cash.cutoff",
                    "capital.strategy.cutoff",
                ):
                    add_not_derivable(output, metric_id, strategy_id, reason)
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
    for strategy_id in active_strategy_ids:
        members = {
            account_id
            for account_id, assigned in account_strategies.items()
            if assigned == strategy_id
        }
        if strategy_id in custody_failures:
            continue
        if not members:
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
    if missing_included or custody_failures:
        add_not_derivable(
            output,
            "capital.portfolio.systematic.cutoff",
            "portfolio",
            "one or more included accounts lacks a cutoff value or required custody cash is not derivable",
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
        if missing_included or custody_failures or aggregate["systematic"] <= 0:
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
        if not members:
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
        strategy_observations, strategy_flows = composite_series(
            {
                account_id: account_window_observations[account_id]
                for account_id in members
            },
            {
                account_id: account_flow_data[account_id].twr_end_flows
                for account_id in members
            },
        )
        strategy_beginning_flows: dict[date, Decimal] = defaultdict(Decimal)
        for account_id in members:
            for occurred_on, amount in account_flow_data[
                account_id
            ].twr_beginning_flows.items():
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
        mwr_flows: list[tuple[date, Decimal]] = []
        for account_id in members:
            account_observations = account_window_observations[account_id]
            flow_data = account_flow_data[account_id]
            mwr_flows.append((account_observations[0][0], -account_observations[0][1]))
            mwr_flows.extend(
                (occurred_on, -amount)
                for occurred_on, amount in flow_data.cash_flows.items()
            )
            mwr_flows.append((account_observations[-1][0], account_observations[-1][1]))
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
    return output


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
    args = parser.parse_args()
    manifest = None
    if args.inputs is not None:
        try:
            manifest = load_inputs_manifest(args.inputs)
        except ManifestError as error:
            parser.exit(1, f"error: {error}\n")
    try:
        result = derive(args.qe_root, manifest)
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
