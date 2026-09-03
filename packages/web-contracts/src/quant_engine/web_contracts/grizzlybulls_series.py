"""Grizzly Bulls series normalization, coverage, drift, replay, and bridge seam.

Normalization is a pure function of persisted raw capture bytes. The two
governed histories — all-history (the blended backtest-plus-live span) and
live-only (the subscriber live span) — are separately identified series;
concatenating distinct identities is a contract violation, never silent.

Everything here is offline. The owner-supplied offline bridge CSVs are
deployment-local; the comparison seam takes already-parsed rows, and its
fixtures are synthetic.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType

from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.web_contracts.contracts import WebContractError
from quant_engine.web_contracts.extract import (
    SessionEnvelope,
    canonical_extraction,
    sanitize_response,
)
from quant_engine.web_contracts.grizzlybulls import (
    GRIZZLYBULLS_CONTRACT,
    STRATEGY_SERIES_MAP,
    extract_grizzlybulls_har,
    governed_series_id,
    route_grizzlybulls_entries,
)


class SeriesIdentity(StrEnum):
    """The two separately governed Grizzly Bulls histories."""

    ALL_HISTORY = "all-history"
    LIVE_ONLY = "live-only"


# Which series identity each reviewed capability carries. The website model
# detail table is the blended span from 2009-04-20 (owner-verified), i.e.
# all-history. No cataloged public route currently carries the live-only
# history; the provider's new trade-data endpoint is pending route review
# under the governing issue, and the resulting coverage gap is reported, never
# backfilled from the other identity.
CAPABILITY_SERIES_IDENTITY: Mapping[str, SeriesIdentity] = MappingProxyType(
    {
        "models.detail": SeriesIdentity.ALL_HISTORY,
    }
)

_DETAIL_SLUG = re.compile(r"/_next/data/[^/]+/models/([a-z0-9-]+)\.json")
_BRIDGE_ACTIONS = frozenset({"BUY", "REBUY", "SELL"})
BRIDGE_REQUIRED_HEADERS = frozenset(
    {"Action", "Buy & Hold", "Date (ET)", "Model", "PnL %", "SPX"}
)

CAPTURE_FILENAME = "capture.json"
COVERAGE_FILENAME = "coverage.json"
MANIFEST_FILENAME = "manifest.json"
NORMALIZED_FILENAME = "normalized.json"
RAW_FILENAME = "raw.har"
RUN_FILENAME = "run.json"


@dataclass(frozen=True)
class TradeObservation:
    """One normalized trade-table row."""

    observed: str
    action: str
    spx: float | None

    def to_dict(self) -> dict[str, object]:
        return {"action": self.action, "observed": self.observed, "spx": self.spx}


@dataclass(frozen=True)
class NormalizedSeries:
    """One normalized series with an explicit, separate history identity."""

    series_id: str
    strategy_slug: str
    identity: SeriesIdentity
    source_capability: str
    rows: tuple[TradeObservation, ...]

    @property
    def series_key(self) -> str:
        return f"{self.series_id}/{self.identity.value}"

    @property
    def observed_start(self) -> str:
        return self.rows[0].observed

    @property
    def observed_end(self) -> str:
        return self.rows[-1].observed

    def to_dict(self) -> dict[str, object]:
        return {
            "identity": self.identity.value,
            "observed_end": self.observed_end,
            "observed_start": self.observed_start,
            "row_count": len(self.rows),
            "rows": [row.to_dict() for row in self.rows],
            "series_id": self.series_id,
            "series_key": self.series_key,
            "source_capability": self.source_capability,
            "strategy_slug": self.strategy_slug,
        }

    def content_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()


@dataclass(frozen=True)
class CoverageRecord:
    """Freshness and coverage of one series in one capture run."""

    series_key: str
    source_capability: str
    observed_start: str
    observed_end: str
    row_count: int
    content_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "content_sha256": self.content_sha256,
            "observed_end": self.observed_end,
            "observed_start": self.observed_start,
            "row_count": self.row_count,
            "series_key": self.series_key,
            "source_capability": self.source_capability,
        }


@dataclass(frozen=True)
class DriftEntry:
    """One recorded content-drift fact; never silently accepted."""

    series_key: str
    field: str
    baseline: str
    candidate: str

    def to_dict(self) -> dict[str, str]:
        return {
            "baseline": self.baseline,
            "candidate": self.candidate,
            "field": self.field,
            "series_key": self.series_key,
        }


@dataclass(frozen=True)
class RunComparison:
    """Content drift between two coverage baselines."""

    drift: tuple[DriftEntry, ...]

    def require_clean(self) -> None:
        if self.drift:
            raise WebContractError(
                "content drift is recorded and must be reviewed, never accepted"
            )


def _series_identity(capability: str) -> SeriesIdentity:
    identity = CAPABILITY_SERIES_IDENTITY.get(capability)
    if identity is None:
        raise WebContractError(f"capability {capability!r} carries no reviewed series")
    return identity


def _trade_rows(payload: Mapping[str, object]) -> tuple[TradeObservation, ...]:
    props = payload.get("pageProps")
    if not isinstance(props, dict):
        raise WebContractError("normalized payload is not a reviewed detail page")
    sheet = props.get("sheetData")
    if not isinstance(sheet, list) or not sheet or not isinstance(sheet[0], list):
        raise WebContractError("normalized payload is not a reviewed detail page")
    header = [str(column) for column in sheet[0]]
    rows: list[TradeObservation] = []
    for row in sheet[1:]:
        if not isinstance(row, list):
            raise WebContractError("normalized trade row is invalid")
        cells = dict(zip(header, row, strict=True))
        try:
            observed = (
                datetime.strptime(str(cells["Date"]), "%m/%d/%Y").date().isoformat()
            )
        except ValueError as exc:
            raise WebContractError("normalized trade date is invalid") from exc
        action = str(cells["Action"]).strip().upper()
        spx = cells.get("SPX")
        if isinstance(spx, bool) or not isinstance(spx, (int, float, type(None))):
            raise WebContractError("normalized trade level is invalid")
        rows.append(
            TradeObservation(
                observed=observed,
                action=action,
                spx=None if spx is None else float(spx),
            )
        )
    unique = sorted(
        set(rows), key=lambda item: (item.observed, item.action, item.spx or 0.0)
    )
    if not unique:
        raise WebContractError("normalized series has no trade rows")
    return tuple(unique)


def normalize_models_detail(
    strategy_slug: str, payload: Mapping[str, object]
) -> NormalizedSeries:
    """Normalize one sanitized model-detail payload, fail-closed on mapping."""

    return NormalizedSeries(
        series_id=governed_series_id(strategy_slug),
        strategy_slug=strategy_slug,
        identity=_series_identity("models.detail"),
        source_capability="models.detail",
        rows=_trade_rows(payload),
    )


def _extract_and_normalize(
    document: Mapping[str, object], *, session: SessionEnvelope
) -> tuple[NormalizedSeries, ...]:
    # Run the full fail-closed extraction first; a document it rejects can
    # never be normalized.
    extract_grizzlybulls_har(document, session=session)
    series: list[NormalizedSeries] = []
    for routed in route_grizzlybulls_entries(document):
        if routed.capability not in CAPABILITY_SERIES_IDENTITY:
            continue
        slug = _DETAIL_SLUG.fullmatch(routed.path)
        if slug is None:
            raise WebContractError("series route does not identify a strategy slug")
        response = routed.entry.get("response")
        if not isinstance(response, dict):
            raise WebContractError("series response is missing")
        content = response.get("content")
        if not isinstance(content, dict) or not isinstance(content.get("text"), str):
            raise WebContractError("series response body is missing")
        payload = json.loads(content["text"])
        sanitized = sanitize_response(payload, GRIZZLYBULLS_CONTRACT)
        if not isinstance(sanitized, dict):
            raise WebContractError("series payload is not an object")
        series.append(normalize_models_detail(slug.group(1), sanitized))
    return tuple(sorted(series, key=lambda item: item.series_key))


def normalize_document(
    document: Mapping[str, object], *, session: SessionEnvelope
) -> tuple[NormalizedSeries, ...]:
    """Normalize a raw HAR document into separately identified series."""

    return _extract_and_normalize(document, session=session)


def combine_series(parts: Sequence[NormalizedSeries]) -> NormalizedSeries:
    """Combine fragments of one series; distinct identities never concatenate."""

    keys = {item.series_key for item in parts}
    if len(keys) != 1 or not parts:
        raise WebContractError(
            "concatenating distinct series identities is a contract violation"
        )
    first = parts[0]
    rows = tuple(
        sorted(
            {row for item in parts for row in item.rows},
            key=lambda item: (item.observed, item.action, item.spx or 0.0),
        )
    )
    return NormalizedSeries(
        series_id=first.series_id,
        strategy_slug=first.strategy_slug,
        identity=first.identity,
        source_capability=first.source_capability,
        rows=rows,
    )


def build_coverage(series: Sequence[NormalizedSeries]) -> tuple[CoverageRecord, ...]:
    """Return the per-series freshness/coverage record of one capture run."""

    keys: set[str] = set()
    coverage: list[CoverageRecord] = []
    for item in series:
        if item.series_key in keys:
            raise WebContractError("capture run carries a duplicate series identity")
        keys.add(item.series_key)
        coverage.append(
            CoverageRecord(
                series_key=item.series_key,
                source_capability=item.source_capability,
                observed_start=item.observed_start,
                observed_end=item.observed_end,
                row_count=len(item.rows),
                content_sha256=item.content_sha256(),
            )
        )
    return tuple(sorted(coverage, key=lambda item: item.series_key))


def required_series_keys() -> tuple[str, ...]:
    """Series identities every accepted capture run must cover."""

    return tuple(
        f"{series_id}/{identity.value}"
        for series_id in sorted(set(STRATEGY_SERIES_MAP.values()))
        for identity in SeriesIdentity
    )


def coverage_gaps(coverage: Sequence[CoverageRecord]) -> tuple[str, ...]:
    """Required series identities absent from the run; a gap is never filled."""

    observed = {item.series_key for item in coverage}
    return tuple(key for key in required_series_keys() if key not in observed)


def compare_coverage(
    baseline: Sequence[CoverageRecord], candidate: Sequence[CoverageRecord]
) -> RunComparison:
    """Record content drift between two runs; nothing is silently accepted."""

    base = {item.series_key: item for item in baseline}
    other = {item.series_key: item for item in candidate}
    drift: list[DriftEntry] = []
    for key in sorted(base.keys() | other.keys()):
        before = base.get(key)
        after = other.get(key)
        if before is None or after is None:
            drift.append(
                DriftEntry(
                    series_key=key,
                    field="presence",
                    baseline="present" if before is not None else "absent",
                    candidate="present" if after is not None else "absent",
                )
            )
            continue
        for field in (
            "content_sha256",
            "observed_end",
            "observed_start",
            "row_count",
        ):
            baseline_value = str(before.to_dict()[field])
            candidate_value = str(after.to_dict()[field])
            if baseline_value != candidate_value:
                drift.append(
                    DriftEntry(
                        series_key=key,
                        field=field,
                        baseline=baseline_value,
                        candidate=candidate_value,
                    )
                )
    return RunComparison(drift=tuple(drift))


def _write_once(path: Path, content: bytes) -> str:
    if path.exists():
        raise WebContractError(f"refusing to overwrite persisted evidence: {path}")
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def persist_capture_run(
    document: Mapping[str, object], *, session: SessionEnvelope, run_dir: Path
) -> dict[str, object]:
    """Persist raw bytes and derived outputs create-only, with hash manifest."""

    session.validate()
    raw = canonical_json_bytes(document)
    records = extract_grizzlybulls_har(document, session=session)
    series = _extract_and_normalize(document, session=session)
    coverage = build_coverage(series)
    gaps = coverage_gaps(coverage)
    run_dir.mkdir(parents=True, exist_ok=False)
    hashes = {
        "capture_sha256": _write_once(
            run_dir / CAPTURE_FILENAME, canonical_extraction(records)
        ),
        "coverage_sha256": _write_once(
            run_dir / COVERAGE_FILENAME,
            canonical_json_bytes(
                {
                    "coverage": [item.to_dict() for item in coverage],
                    "gaps": list(gaps),
                }
            ),
        ),
        "normalized_sha256": _write_once(
            run_dir / NORMALIZED_FILENAME,
            canonical_json_bytes({"series": [item.to_dict() for item in series]}),
        ),
        "raw_sha256": _write_once(run_dir / RAW_FILENAME, raw),
    }
    _write_once(
        run_dir / RUN_FILENAME,
        canonical_json_bytes(
            {
                "captured_at": session.captured_at.isoformat(),
                "expires_at": session.expires_at.isoformat(),
                "provider": GRIZZLYBULLS_CONTRACT.provider,
            }
        ),
    )
    manifest: dict[str, object] = {
        **hashes,
        "gaps": list(gaps),
        "series_keys": [item.series_key for item in coverage],
    }
    _write_once(run_dir / MANIFEST_FILENAME, canonical_json_bytes(manifest))
    return manifest


@dataclass(frozen=True)
class ReplayResult:
    """Proof that replay re-derived the persisted outputs bit-identically."""

    manifest: Mapping[str, object]
    replayed: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {"manifest": dict(self.manifest), "replayed": dict(self.replayed)}


def _read_manifest(run_dir: Path) -> dict[str, object]:
    try:
        manifest = json.loads((run_dir / MANIFEST_FILENAME).read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise WebContractError("capture run manifest is unreadable") from exc
    if not isinstance(manifest, dict):
        raise WebContractError("capture run manifest is invalid")
    return manifest


def replay_capture_run(run_dir: Path) -> ReplayResult:
    """Re-derive outputs from persisted raw bytes and verify identical hashes."""

    manifest = _read_manifest(run_dir)
    for filename, field in (
        (CAPTURE_FILENAME, "capture_sha256"),
        (COVERAGE_FILENAME, "coverage_sha256"),
        (NORMALIZED_FILENAME, "normalized_sha256"),
        (RAW_FILENAME, "raw_sha256"),
    ):
        try:
            persisted = (run_dir / filename).read_bytes()
        except OSError as exc:
            raise WebContractError("persisted capture run is unreadable") from exc
        if hashlib.sha256(persisted).hexdigest() != manifest.get(field):
            raise WebContractError("persisted capture run was altered")
    raw = (run_dir / RAW_FILENAME).read_bytes()
    try:
        run = json.loads((run_dir / RUN_FILENAME).read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise WebContractError("persisted capture run is unreadable") from exc
    if not isinstance(run, dict):
        raise WebContractError("persisted capture run metadata is invalid")
    try:
        session = SessionEnvelope(
            captured_at=datetime.fromisoformat(str(run["captured_at"])),
            expires_at=datetime.fromisoformat(str(run["expires_at"])),
        )
        document = json.loads(raw)
    except (KeyError, ValueError) as exc:
        raise WebContractError("persisted capture run metadata is invalid") from exc
    if not isinstance(document, dict):
        raise WebContractError("persisted raw capture is not a HAR document")
    records = extract_grizzlybulls_har(document, session=session)
    series = _extract_and_normalize(document, session=session)
    coverage = build_coverage(series)
    gaps = coverage_gaps(coverage)
    replayed = {
        "capture_sha256": hashlib.sha256(canonical_extraction(records)).hexdigest(),
        "coverage_sha256": hashlib.sha256(
            canonical_json_bytes(
                {
                    "coverage": [item.to_dict() for item in coverage],
                    "gaps": list(gaps),
                }
            )
        ).hexdigest(),
        "normalized_sha256": hashlib.sha256(
            canonical_json_bytes({"series": [item.to_dict() for item in series]})
        ).hexdigest(),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
    }
    for field, digest in replayed.items():
        if manifest.get(field) != digest:
            raise WebContractError("replay diverged from persisted outputs")
    return ReplayResult(manifest=manifest, replayed=replayed)


@dataclass(frozen=True)
class BridgeObservation:
    """One parsed offline-bridge trade row (synthetic fixtures only in Git)."""

    observed: str
    action: str
    spx: float | None


@dataclass(frozen=True)
class ValueDifference:
    observed: str
    action: str
    provider_spx: float | None
    bridge_spx: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "bridge_spx": self.bridge_spx,
            "observed": self.observed,
            "provider_spx": self.provider_spx,
        }


@dataclass(frozen=True)
class BridgeComparison:
    """Like-identity comparison against the offline bridge; drift is recorded."""

    series_key: str
    matched: int
    provider_only: tuple[str, ...]
    bridge_only: tuple[str, ...]
    value_differences: tuple[ValueDifference, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "bridge_only": list(self.bridge_only),
            "matched": self.matched,
            "provider_only": list(self.provider_only),
            "series_key": self.series_key,
            "value_differences": [item.to_dict() for item in self.value_differences],
        }


def parse_bridge_rows(
    rows: Iterable[Mapping[str, str]], *, identity: SeriesIdentity
) -> tuple[BridgeObservation, ...]:
    """Parse deployment-local bridge rows; the identity is declared, not inferred."""

    parsed: list[BridgeObservation] = []
    for row in rows:
        if not BRIDGE_REQUIRED_HEADERS <= set(row):
            raise WebContractError("bridge row headers differ from the reviewed shape")
        timestamp = row["Date (ET)"].strip()
        if timestamp.endswith(" ET"):
            timestamp = timestamp[:-3]
        try:
            observed = datetime.strptime(timestamp, "%Y-%m-%d %H:%M").date()
        except ValueError as exc:
            raise WebContractError("bridge row timestamp is invalid") from exc
        action = row["Action"].strip().upper()
        if action not in _BRIDGE_ACTIONS:
            raise WebContractError("bridge row action differs from the reviewed shape")
        spx_text = row["SPX"].strip()
        try:
            spx = float(spx_text) if spx_text else None
        except ValueError as exc:
            raise WebContractError("bridge row level is invalid") from exc
        parsed.append(
            BridgeObservation(observed=observed.isoformat(), action=action, spx=spx)
        )
    if not parsed:
        raise WebContractError("bridge rows are empty")
    return tuple(parsed)


def compare_with_bridge(
    series: NormalizedSeries,
    bridge_rows: Sequence[BridgeObservation],
    *,
    bridge_identity: SeriesIdentity,
) -> BridgeComparison:
    """Compare like-identified series; content differences are recorded."""

    if series.identity is not bridge_identity:
        raise WebContractError("bridge comparison requires matching series identity")
    provider = {(row.observed, row.action): row.spx for row in series.rows}
    bridge = {(row.observed, row.action): row.spx for row in bridge_rows}
    matched = 0
    differences: list[ValueDifference] = []
    for key in sorted(provider.keys() & bridge.keys()):
        if provider[key] == bridge[key]:
            matched += 1
        else:
            differences.append(
                ValueDifference(
                    observed=key[0],
                    action=key[1],
                    provider_spx=provider[key],
                    bridge_spx=bridge[key],
                )
            )
    return BridgeComparison(
        series_key=series.series_key,
        matched=matched,
        provider_only=tuple(
            f"{observed} {action}"
            for observed, action in sorted(provider.keys() - bridge.keys())
        ),
        bridge_only=tuple(
            f"{observed} {action}"
            for observed, action in sorted(bridge.keys() - provider.keys())
        ),
        value_differences=tuple(differences),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Replay a persisted capture run offline and print the hash proof."""

    parser = argparse.ArgumentParser(prog="grizzlybulls-series")
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser(
        "replay", help="re-derive outputs from persisted raw bytes and verify hashes"
    )
    replay.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    if args.command == "replay":
        result = replay_capture_run(args.run_dir)
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
