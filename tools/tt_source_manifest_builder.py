"""Build, verify and compatibility-check the TuringTrader source manifest.

Nothing here reads a provider or a credential.  The input is a retained
public-endpoint capture already on disk (a JSON capture directory with its
``capture-manifest.json``, optionally a second directory holding the plain-text
``last-update`` / ``last-rebalance`` objects).  Every derived field comes from
re-hashed capture bytes through the governed producer
``quant_engine.turingtrader.source_manifest``; this tool adds the gates that the
producer does not enforce (status, hash, inventory, slug set, freshness, price
tape coverage) and records the capture provenance inside the manifest.

Subcommands:

* ``build``: create-only output directory; the manifest is written last.
* ``verify``: independent re-hash and shape check of a built output directory.
* ``read-compat``: feed the manifest to the existing reconciliation and
  attribution readers and report acceptance.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Final, Mapping, Sequence

from quant_engine.turingtrader import SourceManifestError, build_source_manifest


COMPONENT_ENDPOINTS: Final = (
    "performance-chart.json",
    "performance-table.json",
    "holdings-history.json",
    "portfolio-holdings.json",
)
METADATA_ENDPOINTS: Final = ("last-rebalance.txt", "last-update.txt")
BUILDER: Final = "tt-source-manifest-builder"
BUILDER_VERSION: Final = "1.0.0"
PROVENANCE_KEY: Final = "capture_provenance"
MANIFEST_NAME: Final = "tt-source-manifest.json"
VIEW_DIR: Final = "capture-view"
CAPTURE_MANIFEST: Final = "capture-manifest.json"
GLOBAL_OBJECT: Final = "portfolio-comp.json"
VANE_OBJECT: Final = "tt-market-vane__status.json"
GOVERNED_SLUGS: Final = (
    "tt-hokey-pokey",
    "tt-mach-3-v3",
    "tt-mean-kitty-v2",
    "tt-quick-change-v2",
    "tt-vix-spritz-aggro-v3",
    "tt-hot-sauce",
)
SHAPE_NATIVE: Final = "NATIVE_HASHED_CAPTURE"
SHAPE_LEGACY: Final = "LEGACY_CAPTURE_WITHOUT_OBJECT_HASHES"
#: Declared data (recorded in every manifest), not a classifier.  A row whose
#: weights sum to 100% within this band is complete.  Published weights carry two
#: decimals, so the band is wider than per-position rounding but far narrower
#: than a dropped position.
DEFAULT_ALLOCATION_SUM_TOLERANCE: Final = 0.005
#: A short row is explained by a dropped position when the missing mass equals
#: that position's weight in the neighbouring complete row (or the live
#: portfolio-holdings for the latest row) within this band.
DEFAULT_EXPLAIN_TOLERANCE: Final = 0.02
CLASS_SYMBOL_DROP: Final = "SYMBOL_DROP"
CLASS_LIVE_OVERRIDE: Final = "LIVE_HOLDINGS_OVERRIDE"
CLASS_EXIT_TRANSITION: Final = "EXIT_TRANSITION"
CLASS_UNRESOLVED_DROP: Final = "UNRESOLVED_DROP"
CLASS_OUT_OF_SCOPE: Final = "OUT_OF_SCOPE_BEFORE_INCEPTION"
CLASS_LIVE_SYMBOL_MISSING: Final = "LIVE_SYMBOL_MISSING"
CLASS_ROUNDING_OR_CASH: Final = "ROUNDING_OR_CASH"
CLASS_OVER_ALLOCATION: Final = "OVER_ALLOCATION"
TRUNCATION_CLASSES: Final = (
    CLASS_SYMBOL_DROP,
    CLASS_LIVE_SYMBOL_MISSING,
    CLASS_LIVE_OVERRIDE,
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


class BuilderError(ValueError):
    """The capture or the manifest violates a source-manifest gate."""


@dataclass(frozen=True)
class _Record:
    filename: str
    data: bytes
    http_status: int | None
    etag: str | None
    last_modified_utc: str | None
    url: str | None
    source: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class _Capture:
    role: str
    captured_at_utc: str
    manifest_sha256: str
    manifest_size_bytes: int
    records: dict[str, _Record]


def _canonical(value: object) -> bytes:
    """Canonical JSON identical to the producer and both readers."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _any_date(value: object, label: str) -> date:
    text = str(value or "").strip()
    for pattern in ("%m/%d/%Y", "%m/%d/%Y %I:%M:%S %p", "%B %d, %Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise BuilderError(f"{label} date is invalid") from exc


def _target_symbols(
    components: Sequence[Mapping[str, Any]], start: date, end: date
) -> list[str]:
    """Symbols held at the interval start or traded inside it (producer rule)."""
    symbols: set[str] = set()
    for component in components:
        prior: Mapping[str, Any] | None = None
        within: list[Mapping[str, Any]] = []
        for snapshot in component["dated_holdings"]["snapshots"]:
            observed = date.fromisoformat(snapshot["observed_date"])
            if observed <= start:
                prior = snapshot
            elif observed <= end:
                within.append(snapshot)
        if prior is None:
            raise BuilderError("dated holdings lack a state at the interval start")
        for snapshot in (prior, *within):
            symbols.update(str(symbol) for symbol in snapshot["weights"])
    return sorted(symbols)


def _json_file(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuilderError(f"{label} is invalid: {path.name}") from exc


def _regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise BuilderError(f"source must be a regular file: {path}")
    return path.read_bytes()


def _utc(value: object, label: str) -> str:
    text = str(value or "").strip()
    try:
        observed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BuilderError(f"{label} timestamp is invalid") from exc
    if observed.tzinfo is None:
        raise BuilderError(f"{label} timestamp lacks a timezone")
    return observed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _http_date(value: object, label: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        observed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError) as exc:
        raise BuilderError(f"{label} last-modified header is invalid") from exc
    if observed.tzinfo is None:
        raise BuilderError(f"{label} last-modified header lacks a timezone")
    return observed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _day(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BuilderError(f"{label} must be YYYY-MM-DD") from exc


def _filename(row: Mapping[str, Any]) -> str:
    name = str(row.get("object") or "")
    slug = str(row.get("slug") or "")
    filename = name.replace("/", "__") if not slug else f"{slug}__{name}"
    if not _FILENAME.fullmatch(filename) or ".." in filename:
        raise BuilderError(f"capture object name is invalid: {filename!r}")
    return filename


def _load_capture(root: Path, role: str, *, legacy: bool) -> _Capture:
    if root.is_symlink() or not root.is_dir():
        raise BuilderError(f"capture directory is absent: {root}")
    manifest_path = root / CAPTURE_MANIFEST
    manifest_bytes = _regular(manifest_path)
    if re.search(rb"token=|[\"']token[\"']\s*:", manifest_bytes, re.IGNORECASE):
        raise BuilderError("capture manifest carries a token; it must be redacted")
    payload = _json_file(manifest_path, "capture manifest")
    if not isinstance(payload, dict) or not isinstance(payload.get("objects"), list):
        raise BuilderError("capture manifest shape differs")
    stamp = payload.get("captured_at_utc")
    if stamp is None and legacy:
        stamp = payload.get("captured_at")
    if stamp is None:
        # A single-object capture may stamp each object instead of the file.
        stamps = [
            _utc(row.get("captured_at_utc"), "capture object")
            for row in payload["objects"]
            if isinstance(row, dict)
        ]
        if stamps:
            stamp = max(stamps)
    captured_at = _utc(stamp, "capture")
    records: dict[str, _Record] = {}
    for row in payload["objects"]:
        if not isinstance(row, dict):
            raise BuilderError("capture inventory row differs")
        filename = _filename(row)
        if filename in records:
            raise BuilderError(f"capture inventory repeats an object: {filename}")
        data = _regular(root / filename)
        claimed_status = row.get("http_status")
        claimed_sha = row.get("sha256")
        if not legacy:
            if claimed_status != 200 or isinstance(claimed_status, bool):
                raise BuilderError(f"capture object is not HTTP 200: {filename}")
            if not isinstance(claimed_sha, str) or not _SHA256.fullmatch(claimed_sha):
                raise BuilderError(f"capture object lacks a sha256: {filename}")
        elif claimed_status not in (None, 200):
            raise BuilderError(f"capture object is not HTTP 200: {filename}")
        if claimed_sha is not None and hashlib.sha256(data).hexdigest() != claimed_sha:
            raise BuilderError(f"capture object sha256 differs: {filename}")
        if row.get("bytes") != len(data):
            raise BuilderError(f"capture object size differs: {filename}")
        if "text" in row and data.decode("utf-8", errors="replace") != row["text"]:
            raise BuilderError(f"capture object text differs: {filename}")
        url = row.get("url")
        if url is not None and (not isinstance(url, str) or "token" in url.lower()):
            raise BuilderError(f"capture object url carries a token: {filename}")
        etag = row.get("etag")
        records[filename] = _Record(
            filename=filename,
            data=data,
            http_status=200 if claimed_status == 200 else None,
            etag=str(etag) if etag else None,
            last_modified_utc=_http_date(row.get("last_modified"), filename),
            url=url,
            source=role,
        )
    if not legacy:
        unlisted = sorted(
            entry.name
            for entry in root.iterdir()
            if entry.name != CAPTURE_MANIFEST and entry.name not in records
        )
        if unlisted:
            raise BuilderError(f"capture directory holds unlisted entries: {unlisted}")
    return _Capture(
        role=role,
        captured_at_utc=captured_at,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        manifest_size_bytes=len(manifest_bytes),
        records=records,
    )


def _expected(slugs: Sequence[str]) -> tuple[frozenset[str], frozenset[str]]:
    json_names = {GLOBAL_OBJECT}
    txt_names: set[str] = set()
    for slug in slugs:
        json_names.update(f"{slug}__{name}" for name in COMPONENT_ENDPOINTS)
        txt_names.update(f"{slug}__{name}" for name in METADATA_ENDPOINTS)
    return frozenset(json_names), frozenset(txt_names)


def _slugs(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return GOVERNED_SLUGS
    slugs = tuple(part.strip() for part in raw.split(",") if part.strip())
    if (
        len(slugs) != 6
        or len(set(slugs)) != 6
        or any(not _SLUG.fullmatch(slug) for slug in slugs)
    ):
        raise BuilderError("--slugs must list six unique component slugs")
    return slugs


def _gather(
    captures: Sequence[_Capture],
    capture_dir: Path,
    slugs: Sequence[str],
    *,
    legacy: bool,
) -> dict[str, _Record]:
    records: dict[str, _Record] = {}
    for capture in captures:
        for name, record in capture.records.items():
            if name in records:
                raise BuilderError(f"object appears in two captures: {name}")
            records[name] = record
    json_names, txt_names = _expected(slugs)
    allowed = json_names | txt_names | {VANE_OBJECT}
    if legacy:
        for name in sorted(allowed - set(records)):
            path = capture_dir / name
            if path.is_file() and not path.is_symlink():
                records[name] = _Record(
                    filename=name,
                    data=path.read_bytes(),
                    http_status=None,
                    etag=None,
                    last_modified_utc=None,
                    url=None,
                    source="json",
                )
    unexpected = sorted(set(records) - allowed)
    if unexpected:
        raise BuilderError(
            f"capture holds objects outside the governed slug set: {unexpected}"
        )
    missing = sorted((json_names | txt_names) - set(records))
    if missing:
        raise BuilderError(f"capture lacks required objects: {missing}")
    return records


def _view_manifest(records: Mapping[str, _Record]) -> bytes:
    objects = []
    for name in sorted(records):
        record = records[name]
        objects.append(
            {
                "bytes": len(record.data),
                "etag": record.etag,
                "http_status": record.http_status,
                "last_modified_utc": record.last_modified_utc,
                "object": name,
                "sha256": record.sha256,
                "source": record.source,
            }
        )
    return _canonical({"builder": BUILDER, "objects": objects})


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)


def _tape_ends(tape: Path, symbols: Sequence[str]) -> tuple[dict[str, date], list[str]]:
    """Last observed date per wanted symbol present in the tape, and the absent."""
    payload = _json_file(tape, "TT adjusted price tape")
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise BuilderError("TT adjusted price tape shape differs")
    wanted = {"BRK-B" if value == "BRK.B" else value for value in symbols}
    ends: dict[str, date] = {}
    for row in payload["rows"]:
        if not isinstance(row, dict) or str(row.get("Ticker") or "") not in wanted:
            continue
        observed = _any_date(row.get("Date"), "TT adjusted price row")
        ticker = str(row["Ticker"])
        if ticker not in ends or observed > ends[ticker]:
            ends[ticker] = observed
    return ends, sorted(wanted - set(ends))


def _preliminary(
    view: Path, slugs: Sequence[str], version: str, inception: str
) -> dict[str, Any]:
    weights = {slug: 1.0 / len(slugs) for slug in slugs}
    payload: dict[str, Any] = json.loads(
        build_source_manifest(
            view,
            portfolio_id="tt-portfolio",
            portfolio_version=version,
            live_inception=inception,
            selected_components=slugs,
            selected_component_weights=weights,
        )
    )
    return payload


def _chart_last(payload: Mapping[str, Any]) -> date:
    return max(
        date.fromisoformat(row["performance"]["observed_end"])
        for row in payload["selected_components"]
    )


def _holdings_validation(
    components: Sequence[Mapping[str, Any]],
    tolerance: float,
    explain: float,
    inception: str,
    citations: Mapping[str, str],
) -> dict[str, Any]:
    """Classify allocation rows that do not sum to 100%; never alter a row.

    Every class is derived from the data, never from a size cut-off.

    * Rows dated before the component's inception anchor (its last snapshot on
      or before ``inception``) are ``OUT_OF_SCOPE_BEFORE_INCEPTION``: they cannot
      reach any input and are not defects.
    * A short row whose symbols are a strict subset of the nearest complete prior
      row and whose missing mass equals the dropped weight is a dropped
      position.  If the dropped symbols are back in the next complete row the
      row is ``SYMBOL_DROP`` (truncation, bracketed); if the next near-complete
      row (within the explain band) lacks them the position really exited and the mass is cash until the next
      row (``EXIT_TRANSITION``, not truncation); with no next complete row it is
      ``UNRESOLVED_DROP``.
    * The latest short row whose date equals the component's last-rebalance
      marker, with complete live portfolio-holdings containing every row symbol,
      is ``LIVE_HOLDINGS_OVERRIDE``: the live holdings are the post-rebalance
      state and win over the older complete row.  Without the marker, live
      holdings that explain the missing mass give ``LIVE_SYMBOL_MISSING``.
    * Any other short row is ``ROUNDING_OR_CASH``; a row above 100% is
      ``OVER_ALLOCATION``.
    """
    start = date.fromisoformat(inception)
    flags: list[dict[str, Any]] = []
    counts: dict[str, dict[str, int]] = {}
    for row in components:
        slug = str(row["slug"])
        snaps = row["dated_holdings"]["snapshots"]
        sums = [sum(s["weights"].values()) for s in snaps]
        done = [abs(total - 1.0) <= tolerance for total in sums]
        live = {str(i["symbol"]): i["weight"] for i in row["current_holdings"]}
        live_ok = all(w is not None for w in live.values()) and (
            abs(sum(float(w) for w in live.values() if w is not None) - 1.0)
            <= tolerance
        )
        before = [
            snap["observed_date"]
            for snap in snaps
            if date.fromisoformat(snap["observed_date"]) <= start
        ]
        anchor = before[-1] if before else snaps[0]["observed_date"]
        marker = str(row.get("last_rebalance") or "")
        for i, snap in enumerate(snaps):
            if done[i]:
                continue
            symbols = set(snap["weights"])
            shortfall = 1.0 - sums[i]
            prior = next((j for j in range(i - 1, -1, -1) if done[j]), None)
            after = next((j for j in range(i + 1, len(snaps)) if done[j]), None)
            neighbours: list[tuple[str, Mapping[str, Any]]] = []
            if prior is not None:
                neighbours.append(("LAST_COMPLETE_ROW", snaps[prior]))
            if after is not None:
                neighbours.append(("NEXT_COMPLETE_ROW", snaps[after]))
            # Bracket evidence: the next row that is itself near-complete (within
            # the explain band), so one rounding-short row does not hide it.
            after_ev = next(
                (j for j in range(i + 1, len(snaps)) if sums[j] >= 1.0 - explain),
                None,
            )
            latest = i == len(snaps) - 1
            extra: dict[str, Any] = {}
            cls = CLASS_ROUNDING_OR_CASH
            if snap["observed_date"] < anchor:
                cls = CLASS_OUT_OF_SCOPE
            elif shortfall < 0:
                cls = CLASS_OVER_ALLOCATION
            else:
                if prior is not None:
                    base = snaps[prior]["weights"]
                    dropped = set(base) - symbols
                    if (
                        dropped
                        and symbols < set(base)
                        and abs(shortfall - sum(base[k] for k in dropped)) <= explain
                    ):
                        if after_ev is None:
                            cls = CLASS_UNRESOLVED_DROP
                        elif dropped <= set(snaps[after_ev]["weights"]):
                            cls = CLASS_SYMBOL_DROP
                        else:
                            cls = CLASS_EXIT_TRANSITION
                        extra["dropped_symbols"] = sorted(dropped)
                if (
                    latest
                    and live_ok
                    and symbols <= set(live)
                    and marker == snap["observed_date"]
                    and cls
                    in (
                        CLASS_UNRESOLVED_DROP,
                        CLASS_EXIT_TRANSITION,
                        CLASS_SYMBOL_DROP,
                        CLASS_ROUNDING_OR_CASH,
                    )
                ):
                    cls = CLASS_LIVE_OVERRIDE
                elif cls == CLASS_ROUNDING_OR_CASH and latest and live_ok:
                    absent = set(live) - symbols
                    if absent and (
                        abs(shortfall - sum(float(live[k]) for k in absent)) <= explain
                    ):
                        cls = CLASS_LIVE_SYMBOL_MISSING
            corroborated = False
            fallback: dict[str, Any]
            if cls == CLASS_LIVE_OVERRIDE:
                hashes = row["endpoint_hashes"]
                superseded = (
                    {
                        "kind": neighbours[0][0],
                        "observed_date": neighbours[0][1]["observed_date"],
                    }
                    if neighbours
                    else None
                )
                fallback = {
                    "kind": "LIVE_HOLDINGS_OVERRIDE",
                    "sources": {
                        "last_rebalance_marker": {
                            "filename": hashes["last-rebalance.txt"]["filename"],
                            "sha256": hashes["last-rebalance.txt"]["sha256"],
                            "value": marker,
                        },
                        "live_portfolio_holdings": {
                            "filename": hashes["portfolio-holdings.json"]["filename"],
                            "sha256": hashes["portfolio-holdings.json"]["sha256"],
                        },
                    },
                    "superseded_fallback": superseded,
                    "why_older_row_lost": (
                        f"the row is dated {snap['observed_date']}, the same day as "
                        f"the last-rebalance marker; the live portfolio-holdings are "
                        f"complete, contain every symbol of the row and are the "
                        f"post-rebalance state, so they win over the older complete "
                        f"row"
                    ),
                }
                if slug in citations:
                    fallback["citation"] = citations[slug]
            elif cls == CLASS_LIVE_SYMBOL_MISSING and live_ok and symbols <= set(live):
                fallback = {"kind": "LIVE_PORTFOLIO_HOLDINGS"}
            elif neighbours:
                kind, nb = neighbours[0]
                fallback = {"kind": kind, "observed_date": nb["observed_date"]}
                corroborated = (
                    live_ok
                    and set(live) == set(nb["weights"])
                    and all(
                        abs(float(live[k]) - nb["weights"][k]) <= tolerance
                        for k in nb["weights"]
                    )
                )
            elif live_ok:
                fallback = {"kind": "LIVE_PORTFOLIO_HOLDINGS"}
            else:
                fallback = {"kind": "NONE"}
            flags.append(
                {
                    "class": cls,
                    **extra,
                    "fallback": fallback,
                    "fallback_corroborated_by_live_portfolio_holdings": corroborated,
                    "flag": (
                        "TRUNCATED_ROW"
                        if cls in TRUNCATION_CLASSES
                        else "ALLOCATION_SUM_OFF"
                    ),
                    "observed_date": snap["observed_date"],
                    "slug": slug,
                }
            )
            by_class = counts.setdefault(slug, {})
            by_class[cls] = by_class.get(cls, 0) + 1
    return {
        "allocation_sum_tolerance": tolerance,
        "counts_by_class": counts,
        "explain_tolerance": explain,
        "flags": flags,
        "live_inception": inception,
        "override_citations": dict(citations),
    }


def _sealed(payload: dict[str, Any]) -> bytes:
    unsigned = {k: v for k, v in payload.items() if k != "source_manifest_id"}
    payload["source_manifest_id"] = hashlib.sha256(_canonical(unsigned)).hexdigest()
    return _canonical(payload)


def _provenance(
    captures: Sequence[_Capture],
    records: Mapping[str, _Record],
    *,
    legacy: bool,
    min_capture_date: str,
    slugs: Sequence[str],
    chart_last: date,
    vane: Mapping[str, Any],
    gap: Mapping[str, Any] | None,
    holdings_rows: Mapping[str, Any],
    workbook_sha: str,
    portfolio_version: str,
    live_inception: str,
) -> dict[str, Any]:
    return {
        "builder": BUILDER,
        "caller_supplied": {
            "component_weights": {
                "basis": "EQUAL_1_OVER_N",
                "owner_workbook_filename": "TuringTrader Strategies Data byAPI.xlsx",
                "owner_workbook_sha256": workbook_sha,
                "source": "owner workbook dgst_tt_portfolio allocation column",
            },
            "live_inception": live_inception,
            "portfolio_version": portfolio_version,
        },
        "builder_version": BUILDER_VERSION,
        "capture_shape": SHAPE_LEGACY if legacy else SHAPE_NATIVE,
        "captures": [
            {
                "captured_at_utc": item.captured_at_utc,
                "manifest_sha256": item.manifest_sha256,
                "manifest_size_bytes": item.manifest_size_bytes,
                "objects": len(item.records),
                "role": item.role,
            }
            for item in captures
        ],
        "chart_last_date": chart_last.isoformat(),
        "holdings_row_validation": dict(holdings_rows),
        "governed_slug_set": tuple(slugs) == GOVERNED_SLUGS,
        "market_vane": dict(vane),
        "min_capture_date": min_capture_date,
        "objects": {
            name: {
                "bytes": len(record.data),
                "etag": record.etag,
                "http_status": record.http_status,
                "last_modified_utc": record.last_modified_utc,
                "sha256": record.sha256,
                "source": record.source,
            }
            for name, record in sorted(records.items())
        },
        "price_tape_gap": None if gap is None else dict(gap),
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    legacy = bool(args.accept_legacy_capture_shape)
    slugs = _slugs(args.slugs)
    min_day = _day(args.min_capture_date, "--min-capture-date")
    inception = _day(args.live_inception, "--live-inception").isoformat()
    version = str(args.portfolio_version or "").strip()
    if not version:
        raise BuilderError("--portfolio-version is required")
    tolerance = float(args.allocation_sum_tolerance)
    explain = float(args.explain_tolerance)
    if not (0.0 <= tolerance < 0.5 and 0.0 <= explain < 0.5):
        raise BuilderError("allocation tolerances must be in [0, 0.5)")
    citations: dict[str, str] = {}
    for item in args.override_citation or []:
        slug_name, _, text = str(item).partition("=")
        if not slug_name.strip() or not text.strip():
            raise BuilderError("--override-citation must be SLUG=TEXT")
        citations[slug_name.strip()] = text.strip()
    workbook_sha = str(args.owner_workbook_sha256 or "")
    if not _SHA256.fullmatch(workbook_sha):
        raise BuilderError("--owner-workbook-sha256 must be a sha256")
    out_dir = Path(args.out_dir)
    partial = out_dir.with_name(out_dir.name + ".partial")
    if out_dir.exists() or partial.exists():
        raise BuilderError(f"output already exists: {out_dir}")
    capture_dir = Path(args.capture_dir)
    captures = [_load_capture(capture_dir, "json", legacy=legacy)]
    if args.txt_capture_dir:
        captures.append(_load_capture(Path(args.txt_capture_dir), "txt", legacy=legacy))
    if args.vane_capture_dir:
        captures.append(
            _load_capture(Path(args.vane_capture_dir), "vane", legacy=legacy)
        )
    for item in captures:
        if _day(item.captured_at_utc[:10], "capture date") < min_day:
            raise BuilderError(
                f"{item.role} capture is older than the minimum date: "
                f"{item.captured_at_utc} < {min_day}"
            )
    records = _gather(captures, capture_dir, slugs, legacy=legacy)
    comp = records[GLOBAL_OBJECT]
    if comp.last_modified_utc is not None and (
        _day(comp.last_modified_utc[:10], "last-modified") < min_day
    ):
        raise BuilderError(
            f"portfolio-comp last-modified is older than the minimum date: "
            f"{comp.last_modified_utc}"
        )
    if comp.last_modified_utc is None and not legacy:
        raise BuilderError("portfolio-comp.json lacks a last-modified header")
    vane_day = _day(args.min_market_vane_date or args.min_capture_date, "vane date")
    if VANE_OBJECT in records:
        vane_prov: dict[str, Any] = {
            "captured_at_utc": next(
                c for c in captures if c.role == records[VANE_OBJECT].source
            ).captured_at_utc,
            "source_last_modified_utc": records[VANE_OBJECT].last_modified_utc,
            "source": "CAPTURE_INVENTORY",
        }
    else:
        if not args.market_vane_status or not args.market_vane_captured_at:
            raise BuilderError(
                "capture lacks the market-vane status; pass --market-vane-status "
                "and --market-vane-captured-at"
            )
        vane_path = Path(args.market_vane_status)
        records[VANE_OBJECT] = _Record(
            filename=VANE_OBJECT,
            data=_regular(vane_path),
            http_status=None,
            etag=None,
            last_modified_utc=None,
            url=None,
            source="explicit",
        )
        vane_prov = {
            "captured_at_utc": _utc(args.market_vane_captured_at, "market-vane"),
            "source": "EXPLICIT_FILE",
        }
    if _day(str(vane_prov["captured_at_utc"])[:10], "vane capture") < vane_day:
        raise BuilderError(
            "market-vane status is older than the minimum date: "
            f"{vane_prov['captured_at_utc']} < {vane_day}"
        )
    vane_prov["min_date"] = vane_day.isoformat()

    partial.mkdir(parents=True)
    try:
        view = partial / VIEW_DIR
        for name, record in records.items():
            _write_new(view / name, record.data)
        _write_new(view / CAPTURE_MANIFEST, _view_manifest(records))
        base = _preliminary(view, slugs, version, inception)
        chart_last = _chart_last(base)
        if _day(inception, "--live-inception") > chart_last:
            raise BuilderError("live inception is after the last chart date")
        tape_src = Path(args.price_tape)
        tape_man_src = Path(args.price_tape_manifest)
        tape_bytes = _regular(tape_src)
        tape_man_bytes = _regular(tape_man_src)
        _write_new(partial / tape_src.name, tape_bytes)
        _write_new(partial / tape_man_src.name, tape_man_bytes)
        symbols = _target_symbols(
            base["selected_components"], date.fromisoformat(inception), chart_last
        )
        ends, absent = _tape_ends(partial / tape_src.name, symbols)
        if not ends:
            raise BuilderError(
                "TT adjusted price tape holds none of the target symbols"
            )
        tape_end = min(ends.values())
        gap: dict[str, Any] | None = None
        target_end = chart_last
        if tape_end < chart_last or absent:
            label = str(args.price_tape_gap_label or "").strip()
            if not label:
                raise BuilderError(
                    f"price tape ends {tape_end} (chart ends {chart_last}) and lacks "
                    f"symbols {absent}; pass --price-tape-gap-label to accept the gap"
                )
            target_end = min(tape_end, chart_last)
            # A symbol the tape never carries limits the target to the days
            # before any holdings snapshot first needs it.
            first_needed = [
                date.fromisoformat(snap["observed_date"])
                for row in base["selected_components"]
                for snap in row["dated_holdings"]["snapshots"]
                if date.fromisoformat(snap["observed_date"])
                > date.fromisoformat(inception)
                and {"BRK.B" if a == "BRK-B" else a for a in absent}
                & set(snap["weights"])
            ]
            if first_needed:
                target_end = min(
                    target_end, date.fromordinal(min(first_needed).toordinal() - 1)
                )
            gap = {
                "absent_symbols_through_chart_end": absent,
                "chart_last_date": chart_last.isoformat(),
                "label": label,
                "tape_observed_end": target_end.isoformat(),
            }
        target_symbols = _target_symbols(
            base["selected_components"], date.fromisoformat(inception), target_end
        )
        weights = {slug: 1.0 / len(slugs) for slug in slugs}
        payload: dict[str, Any] = json.loads(
            build_source_manifest(
                view,
                portfolio_id="tt-portfolio",
                portfolio_version=version,
                live_inception=inception,
                selected_components=slugs,
                selected_component_weights=weights,
                price_tape=partial / tape_src.name,
                price_tape_manifest=partial / tape_man_src.name,
                target_price_symbols=target_symbols,
                target_start=inception,
                target_end=target_end.isoformat(),
            )
        )
        payload[PROVENANCE_KEY] = _provenance(
            captures,
            records,
            legacy=legacy,
            min_capture_date=min_day.isoformat(),
            slugs=slugs,
            chart_last=chart_last,
            vane=vane_prov,
            gap=gap,
            holdings_rows=_holdings_validation(
                payload["selected_components"],
                tolerance,
                explain,
                inception,
                citations,
            ),
            workbook_sha=workbook_sha,
            portfolio_version=version,
            live_inception=inception,
        )
        content = _sealed(payload)
        _write_new(partial / MANIFEST_NAME, content)
        verify_dir(partial)
        os.replace(partial, out_dir)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return {
        "manifest": str(out_dir / MANIFEST_NAME),
        "manifest_sha256": hashlib.sha256(content).hexdigest(),
        "manifest_size_bytes": len(content),
        "source_manifest_id": payload["source_manifest_id"],
        "chart_last_date": chart_last.isoformat(),
        "price_tape_target_end": target_end.isoformat(),
        "price_tape_gap": gap,
    }


def _identity_of(path: Path) -> dict[str, object]:
    data = _regular(path)
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def verify_dir(out_dir: Path) -> dict[str, Any]:
    out_dir = Path(out_dir)
    manifest_path = out_dir / MANIFEST_NAME
    raw = _regular(manifest_path)
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or raw != _canonical(manifest):
        raise BuilderError("manifest is not canonical JSON")
    provenance = manifest.get(PROVENANCE_KEY)
    if not isinstance(provenance, dict) or provenance.get("builder") != BUILDER:
        raise BuilderError("manifest lacks builder provenance")
    unsigned = {k: v for k, v in manifest.items() if k != "source_manifest_id"}
    if hashlib.sha256(_canonical(unsigned)).hexdigest() != manifest.get(
        "source_manifest_id"
    ):
        raise BuilderError("manifest identity differs")
    view = out_dir / VIEW_DIR
    view_manifest = _json_file(view / CAPTURE_MANIFEST, "capture view manifest")
    listed: dict[str, Mapping[str, Any]] = {}
    for row in view_manifest.get("objects", []):
        listed[str(row["object"])] = row
        data = _regular(view / str(row["object"]))
        if hashlib.sha256(data).hexdigest() != row.get("sha256") or len(
            data
        ) != row.get("bytes"):
            raise BuilderError(f"capture view object differs: {row['object']}")
    on_disk = {p.name for p in view.iterdir()} - {CAPTURE_MANIFEST}
    if on_disk != set(listed):
        raise BuilderError("capture view holds unlisted or missing objects")
    if manifest["capture_manifest"] != _identity_of(view / CAPTURE_MANIFEST):
        raise BuilderError("capture manifest identity differs")
    recorded = provenance.get("objects")
    expected_objects = {
        name: {
            key: row.get(key)
            for key in (
                "bytes",
                "etag",
                "http_status",
                "last_modified_utc",
                "sha256",
                "source",
            )
        }
        for name, row in listed.items()
    }
    if recorded != expected_objects:
        raise BuilderError("provenance objects differ from the re-hashed capture")
    legacy = provenance.get("capture_shape") == SHAPE_LEGACY
    if provenance.get("capture_shape") not in (SHAPE_NATIVE, SHAPE_LEGACY):
        raise BuilderError("capture shape is not recognised")
    if not legacy and any(
        row.get("http_status") != 200
        for name, row in listed.items()
        if row.get("source") != "explicit"
    ):
        raise BuilderError("a capture object is not HTTP 200")
    selected = manifest.get("selected_components")
    if not isinstance(selected, list):
        raise BuilderError("selected components differ")
    slugs = tuple(str(row.get("slug") or "") for row in selected)
    if (tuple(slugs) == GOVERNED_SLUGS) != bool(provenance.get("governed_slug_set")):
        raise BuilderError("governed slug set flag differs")
    json_names, txt_names = _expected(slugs)
    if not (json_names | txt_names) <= set(listed) or set(listed) - (
        json_names | txt_names | {VANE_OBJECT}
    ):
        raise BuilderError("capture view inventory differs from the slug set")
    min_day = _day(str(provenance.get("min_capture_date")), "min_capture_date")
    for item in provenance.get("captures", []):
        if _day(str(item["captured_at_utc"])[:10], "capture date") < min_day:
            raise BuilderError("capture is older than the recorded minimum date")
    price = manifest.get("price_tape")
    if not isinstance(price, dict):
        raise BuilderError("manifest lacks the price tape binding")
    target = price["target"]
    chart_last = _chart_last(manifest)
    if provenance.get("chart_last_date") != chart_last.isoformat():
        raise BuilderError("recorded chart last date differs")
    gap = provenance.get("price_tape_gap")
    if gap is None:
        if target["observed_end"] != chart_last.isoformat():
            raise BuilderError("price tape target ends before the chart without a gap")
    elif not (
        isinstance(gap, dict)
        and gap.get("label")
        and gap.get("chart_last_date") == chart_last.isoformat()
        and gap.get("tape_observed_end") == target["observed_end"]
        and target["observed_end"] < chart_last.isoformat()
    ):
        raise BuilderError("price tape gap record differs")
    # Replay the derivation from the re-hashed view and compare bytes.
    base = {k: v for k, v in manifest.items() if k != PROVENANCE_KEY}
    rebuilt = build_source_manifest(
        view,
        portfolio_id=str(manifest["portfolio"]["portfolio_id"]),
        portfolio_version=str(manifest["portfolio"]["portfolio_version"]),
        live_inception=str(manifest["portfolio"]["live_inception"]),
        selected_components=slugs,
        selected_component_weights={
            str(row["slug"]): float(row["portfolio_weight"]) for row in selected
        },
        price_tape=out_dir / str(price["artifact"]["filename"]),
        price_tape_manifest=out_dir / str(price["source_run_manifest"]["filename"]),
        target_price_symbols=list(target["symbols"]),
        target_start=str(target["observed_start"]),
        target_end=str(target["observed_end"]),
    )
    if _sealed(base) != rebuilt:
        raise BuilderError("manifest replay differs from the re-hashed capture")
    validation = provenance.get("holdings_row_validation")
    if not isinstance(validation, dict) or validation != _holdings_validation(
        selected,
        float(validation.get("allocation_sum_tolerance", -1.0)),
        float(validation.get("explain_tolerance", -1.0)),
        str(validation.get("live_inception", "")),
        dict(validation.get("override_citations", {})),
    ):
        raise BuilderError("holdings row validation differs from the manifest rows")
    weights = [float(row["portfolio_weight"]) for row in selected]
    if any(abs(w - 1.0 / len(slugs)) > 1e-12 for w in weights):
        raise BuilderError("component weights are not the published equal weights")
    return {
        "verified": True,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "source_manifest_id": manifest["source_manifest_id"],
        "slugs": list(slugs),
        "objects": len(listed),
        "capture_shape": provenance["capture_shape"],
        "chart_last_date": chart_last.isoformat(),
        "price_tape_gap": gap,
        "holdings_counts_by_class": validation["counts_by_class"],
    }


def read_compat(manifest: Path, components: Sequence[str]) -> dict[str, Any]:
    """Run both existing readers; return per-reader acceptance."""
    import portfolio_implementation_reconciliation as reconciliation
    import portfolio_implementation_return_attribution as attribution

    result: dict[str, Any] = {"components": list(components)}
    readers: Sequence[tuple[str, Any, type[Exception]]] = (
        (
            "reconciliation",
            reconciliation._read_tt_source_manifest,
            reconciliation.ImplementationReconciliationError,
        ),
        (
            "attribution",
            attribution._read_tt_source_manifest,
            attribution.ReturnAttributionError,
        ),
    )
    accepted = True
    for name, reader, error in readers:
        try:
            loaded = reader(manifest, list(components))
        except error as exc:
            accepted = False
            result[name] = {"accepted": False, "rejected_because": str(exc)}
        else:
            result[name] = {
                "accepted": True,
                "source_manifest_id": loaded["source_manifest_id"],
            }
    result["accepted"] = accepted
    result["holdings_counts_by_class"] = (
        _json_file(manifest, "manifest")
        .get(PROVENANCE_KEY, {})
        .get("holdings_row_validation", {})
        .get("counts_by_class", {})
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build a create-only manifest directory")
    b.add_argument("--capture-dir", required=True)
    b.add_argument("--txt-capture-dir")
    b.add_argument("--out-dir", required=True)
    b.add_argument("--min-capture-date", required=True, help="YYYY-MM-DD")
    b.add_argument("--portfolio-version", required=True)
    b.add_argument("--live-inception", required=True, help="YYYY-MM-DD")
    b.add_argument("--price-tape", required=True)
    b.add_argument("--price-tape-manifest", required=True)
    b.add_argument("--price-tape-gap-label")
    b.add_argument("--market-vane-status")
    b.add_argument("--market-vane-captured-at")
    b.add_argument("--min-market-vane-date")
    b.add_argument("--slugs", help="comma-separated six slugs (non-governed set)")
    b.add_argument(
        "--allocation-sum-tolerance",
        type=float,
        default=DEFAULT_ALLOCATION_SUM_TOLERANCE,
        help="fraction of 1.0 within which a holdings row is complete",
    )
    b.add_argument(
        "--explain-tolerance",
        type=float,
        default=DEFAULT_EXPLAIN_TOLERANCE,
        help="fraction within which a dropped position explains a short row",
    )
    b.add_argument(
        "--owner-workbook-sha256",
        required=True,
        help="sha256 of the owner workbook the caller-supplied labels come from",
    )
    b.add_argument(
        "--override-citation",
        action="append",
        help="SLUG=TEXT citing outside evidence for a live-holdings override",
    )
    b.add_argument("--vane-capture-dir", help="capture dir holding the market vane")
    b.add_argument("--accept-legacy-capture-shape", action="store_true")
    v = sub.add_parser("verify", help="re-hash and shape-check a built directory")
    v.add_argument("--out-dir", required=True)
    r = sub.add_parser("read-compat", help="feed the manifest to the existing readers")
    r.add_argument("--out-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            report = build(args)
        elif args.command == "verify":
            report = verify_dir(Path(args.out_dir))
        else:
            manifest_path = Path(args.out_dir) / MANIFEST_NAME
            manifest = _json_file(manifest_path, "manifest")
            slugs = [str(row["slug"]) for row in manifest["selected_components"]]
            report = read_compat(manifest_path, slugs)
    except (BuilderError, SourceManifestError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("accepted", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
