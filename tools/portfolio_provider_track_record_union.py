"""Stage one provider track-record generation that serves several provider series.

The provider track-record store has a single CURRENT pointer, and the first
generation selected there carries one series. A second provider series (the
designer-model curve of a subscribed model) lives in its own candidate
generation, so selecting it would displace the first.

This tool builds a NEW create-only generation from verified source generations
and never touches a CURRENT pointer:

* the top-level ``returns.csv`` and ``raw/track-record-report.json`` of the
  primary member are byte-identical to the source, so every consumer and every
  pinned digest of that member keeps reading what it read before;
* every other member is carried under ``members/<member_id>/`` with its source
  manifest, source catalog, returns and raw payloads byte-for-byte;
* a member that publishes in a foreign currency also gets a
  ``returns-converted.csv`` derived with the governed fiat generation, under the
  lookup policy that generation carries (no fill, last observed close within
  the declared age), with the native and converted returns both shown;
* the catalog records closure checks (counts, dates, digests, the telescoping
  identity of the conversion) so nothing is admitted on trust.

The member selection, series, currency and conversion are data in a spec file,
not code.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date
from decimal import Decimal, localcontext
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Final

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    rename_directory_with_retry,
)


PRECISION: Final = 40

CATALOG_SCHEMA_VERSION: Final = 2
ADAPTER_VERSION: Final = "1"
SOURCE_ID: Final = "provider-track-record"
CONVERTED_FIELDS: Final = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "return_basis",
    "native_return",
    "native_currency",
    "target_currency",
    "fx_rate",
    "fx_observed_date",
    "fx_previous_rate",
    "fx_previous_observed_date",
    "native_days_spanned",
)
CONVERTED_BASIS_SUFFIX: Final = "|FX_CONVERTED"
IDENTITY_TOLERANCE: Final = Decimal("1e-20")


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    """The repository's canonical JSON encoding (same as the core contracts)."""

    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return (text + chr(10)).encode("utf-8")


class ProviderTrackRecordUnionError(ValueError):
    """A union specification or a source generation violates the contract."""


def _regular(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ProviderTrackRecordUnionError(f"{label} is not a regular file: {path}")
    return path.read_bytes()


def _json(content: bytes, label: str) -> Any:
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderTrackRecordUnionError(f"{label} is not JSON") from exc


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _day(value: object, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ProviderTrackRecordUnionError(f"{label} is not a canonical date") from exc


def _dec(value: object, label: str) -> Decimal:
    try:
        return Decimal(str(value))
    except ArithmeticError as exc:
        raise ProviderTrackRecordUnionError(f"{label} is not numeric") from exc


def _load_member(root: Path) -> dict[str, Any]:
    """Read one source generation and verify every artifact against its manifest."""

    root = root.resolve()
    manifest_bytes = _regular(root / "generation.json", "source manifest")
    manifest = _json(manifest_bytes, "source manifest")
    try:
        parsed = GenerationManifest.from_json_bytes(manifest_bytes)
    except ValueError as exc:
        raise ProviderTrackRecordUnionError(
            f"source manifest is invalid: {root.name}"
        ) from exc
    if (
        not isinstance(manifest, dict)
        or parsed.verification is not VerificationStatus.VERIFIED
        or parsed.generation_id != root.name
        or parsed.to_json_bytes() != manifest_bytes
    ):
        raise ProviderTrackRecordUnionError(
            f"source generation is not verified or differs from its directory: {root.name}"
        )
    raw_artifacts: dict[str, bytes] = {}
    for entry in manifest.get("artifacts") or []:
        relative = str(entry.get("artifact_id") or "")
        content = _regular(root / relative, f"source artifact {relative}")
        if _sha(content) != entry.get("sha256") or len(content) != entry.get(
            "size_bytes"
        ):
            raise ProviderTrackRecordUnionError(
                f"source artifact differs from its manifest: {root.name}/{relative}"
            )
        raw_artifacts[relative] = content
    for required in ("catalog.json", "returns.csv"):
        if required not in raw_artifacts:
            raise ProviderTrackRecordUnionError(
                f"source generation lacks {required}: {root.name}"
            )
    catalog = _json(raw_artifacts["catalog.json"], "source catalog")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("series"), dict):
        raise ProviderTrackRecordUnionError("source catalog series differs")
    return {
        "generation_id": root.name,
        "manifest_bytes": manifest_bytes,
        "manifest": manifest,
        "artifacts": raw_artifacts,
        "catalog": catalog,
    }


def _rows(content: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content.decode("utf-8"))))


def _compound(values: list[Decimal]) -> Decimal:
    level = Decimal(1)
    for value in values:
        level *= Decimal(1) + value
    return level - Decimal(1)


def _member_closure(
    member_id: str, source: dict[str, Any], series_id: str
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Closure checks on the source returns against the source's own claims."""

    rows = _rows(source["artifacts"]["returns.csv"])
    series = source["catalog"]["series"]
    if not rows:
        raise ProviderTrackRecordUnionError(f"{member_id}: returns are empty")
    foreign = {row["series_id"] for row in rows} - {series_id}
    if foreign:
        raise ProviderTrackRecordUnionError(
            f"{member_id}: returns carry a foreign series {sorted(foreign)}"
        )
    days = [_day(row["date"], f"{member_id} date") for row in rows]
    if days != sorted(set(days)):
        raise ProviderTrackRecordUnionError(f"{member_id}: calendar is not ordered")
    values = [_dec(row["return"], f"{member_id} return") for row in rows]
    if any(value <= -1 for value in values):
        raise ProviderTrackRecordUnionError(f"{member_id}: a return is not above -1")
    declared = source["catalog"]["coverage"]
    if len(rows) != declared.get("return_observations") or len(rows) != series.get(
        "return_observations"
    ):
        raise ProviderTrackRecordUnionError(
            f"{member_id}: return count differs from the source catalog"
        )
    if (
        days[-1].isoformat() != declared.get("observed_end")
        or days[-1].isoformat() != series.get("observed_end")
        or (
            series.get("observed_start") is not None
            and days[0].isoformat() < str(series["observed_start"])
        )
    ):
        raise ProviderTrackRecordUnionError(
            f"{member_id}: dates differ from the source catalog"
        )
    layers: dict[str, int] = {}
    for row in rows:
        layers[row["evidence_layer"]] = layers.get(row["evidence_layer"], 0) + 1
    cumulative = _compound(values)
    checks: dict[str, Any] = {
        "calendar_strictly_increasing": True,
        "evidence_layer_counts": dict(sorted(layers.items())),
        "first_return_date": days[0].isoformat(),
        "last_return_date": days[-1].isoformat(),
        "returns_equal_source_catalog_count": len(rows),
        "cumulative_return_from_returns": format(cumulative.normalize(), "f"),
    }
    derived = series.get("derived_total_return")
    if derived is not None:
        checks["source_declared_total_return"] = str(derived)
        checks["cumulative_matches_source_total_return"] = abs(
            cumulative - _dec(derived, "source total return")
        ) <= Decimal("0.0001")
        if not checks["cumulative_matches_source_total_return"]:
            raise ProviderTrackRecordUnionError(
                f"{member_id}: cumulative return differs from the source total"
            )
    launch = series.get("provider_launch")
    if launch:
        prelaunch = [r for r in rows if r["evidence_layer"] == "BACKTEST"]
        live = [r for r in rows if r["evidence_layer"] == "PROVIDER_MODEL_LIVE"]
        if (
            len(prelaunch) != series.get("prelaunch_return_observations")
            or len(live) != series.get("model_live_return_observations")
            or any(r["date"] >= str(launch) for r in prelaunch)
            or any(r["date"] < str(launch) for r in live)
        ):
            raise ProviderTrackRecordUnionError(
                f"{member_id}: prelaunch/live split differs from the provider launch"
            )
        checks["provider_launch"] = str(launch)
        checks["prelaunch_last_date"] = prelaunch[-1]["date"] if prelaunch else None
        checks["model_live_first_date"] = live[0]["date"] if live else None
        checks["prelaunch_plus_live_equals_returns"] = True
    return rows, checks


def _convert(
    member_id: str,
    rows: list[dict[str, str]],
    level_start: date,
    native_currency: str,
    fx_root: Path,
) -> tuple[bytes, dict[str, Any]]:
    """Convert a native-currency daily return path with the governed fiat table."""

    from portfolio_fx_rates import FiatRateTable  # noqa: PLC0415

    table = FiatRateTable(fx_root.resolve())
    pairs = {
        (row["currency"], row["base_currency"])
        for row in csv.DictReader(
            io.StringIO((fx_root / "rates.csv").read_text(encoding="utf-8"))
        )
    }
    if pairs != {(native_currency, "USD")}:
        raise ProviderTrackRecordUnionError(
            f"{member_id}: the FX generation does not quote {native_currency} in USD"
        )
    previous_rate, previous_observed = table.rate_on(level_start)
    first_rate = previous_rate
    out: list[dict[str, str]] = []
    native_values: list[Decimal] = []
    converted_values: list[Decimal] = []
    lagged = 0
    pending = Decimal(1)
    pending_days = 0
    unconverted: list[str] = []
    for row in rows:
        day = _day(row["date"], f"{member_id} date")
        native = _dec(row["return"], f"{member_id} return")
        native_values.append(native)
        pending *= Decimal(1) + native
        pending_days += 1
        try:
            rate, observed = table.rate_on(day)
        except Exception:  # noqa: BLE001 - the lookup policy refused: never fill
            unconverted.append(row["date"])
            continue
        converted = pending * rate / previous_rate - Decimal(1)
        if observed != day:
            lagged += 1
        out.append(
            {
                "series_id": row["series_id"],
                "date": row["date"],
                "return": format(converted, "f"),
                "evidence_layer": row["evidence_layer"],
                "return_basis": row["return_basis"] + CONVERTED_BASIS_SUFFIX,
                "native_return": format((pending - Decimal(1)), "f"),
                "native_currency": native_currency,
                "target_currency": "USD",
                "fx_rate": format(rate, "f"),
                "fx_observed_date": observed.isoformat(),
                "fx_previous_rate": format(previous_rate, "f"),
                "fx_previous_observed_date": previous_observed.isoformat(),
                "native_days_spanned": str(pending_days),
            }
        )
        converted_values.append(converted)
        previous_rate, previous_observed = rate, observed
        pending, pending_days = Decimal(1), 0
    if pending_days:
        raise ProviderTrackRecordUnionError(
            f"{member_id}: trailing native days have no usable conversion rate"
        )
    native_cum = _compound(native_values)
    converted_cum = _compound(converted_values)
    expected = (Decimal(1) + native_cum) * previous_rate / first_rate - Decimal(1)
    residual = abs(converted_cum - expected)
    if residual > IDENTITY_TOLERANCE:
        raise ProviderTrackRecordUnionError(
            f"{member_id}: conversion does not telescope (residual {residual})"
        )
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CONVERTED_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(out)
    content = buffer.getvalue().encode("utf-8")
    return content, {
        "converted_cumulative_return": format(converted_cum.normalize(), "f"),
        "fx_generation_id": table.generation_id,
        "fx_policy": table.policy,
        "fx_rate_at_level_start": format(first_rate, "f"),
        "fx_rate_at_last_return": format(previous_rate, "f"),
        "level_start_date": level_start.isoformat(),
        "native_cumulative_return": format(native_cum.normalize(), "f"),
        "native_dates_without_a_usable_rate_merged_into_next_converted_row": unconverted,
        "native_rows": len(rows),
        "rows": len(out),
        "rows_using_an_earlier_observed_close": lagged,
        "telescoping_identity_residual": format(residual, "e"),
    }


def _spec(path: Path) -> dict[str, Any]:
    spec = _json(_regular(path, "union spec"), "union spec")
    if not isinstance(spec, dict) or not isinstance(spec.get("members"), list):
        raise ProviderTrackRecordUnionError("union spec members differ")
    primaries = [m for m in spec["members"] if m.get("role") == "PRIMARY"]
    if len(primaries) != 1:
        raise ProviderTrackRecordUnionError("union spec needs exactly one PRIMARY")
    ids = [str(m.get("member_id") or "") for m in spec["members"]]
    if len(set(ids)) != len(ids) or not all(ids):
        raise ProviderTrackRecordUnionError("union spec member ids differ")
    return spec


def _build(
    spec: dict[str, Any], as_of: date
) -> tuple[dict[Path, bytes], GenerationManifest, dict[str, Any]]:
    files: dict[Path, bytes] = {}
    member_entries: list[dict[str, Any]] = []
    starts: list[date] = []
    ends: list[date] = []
    fact_types: list[str] = []
    primary_id = ""
    primary_source: dict[str, Any] | None = None
    for entry in spec["members"]:
        member_id = str(entry["member_id"])
        role = str(entry["role"])
        series_id = str(entry["series_id"])
        source = _load_member(Path(str(entry["generation_root"])))
        rows, checks = _member_closure(member_id, source, series_id)
        prefix = Path("members") / member_id
        files[prefix / "source-generation.json"] = source["manifest_bytes"]
        for relative, content in source["artifacts"].items():
            target = "source-catalog.json" if relative == "catalog.json" else relative
            files[prefix / target] = content
        item: dict[str, Any] = {
            "checks": checks,
            "currency": str(
                entry.get("currency")
                or source["catalog"]["series"].get("currency")
                or ""
            ),
            "evidence_layer": str(source["catalog"].get("evidence_layer") or ""),
            "member_id": member_id,
            "mode_admission": source["catalog"].get("mode_admission"),
            "role": role,
            "series_id": series_id,
            "source_catalog_sha256": _sha(source["artifacts"]["catalog.json"]),
            "source_generation_id": source["generation_id"],
            "source_generation_manifest_sha256": _sha(source["manifest_bytes"]),
            "source_returns_sha256": _sha(source["artifacts"]["returns.csv"]),
            "source_selector_authority": source["catalog"].get("selector_authority"),
            "source_splice_policy": source["catalog"].get("splice_policy"),
        }
        starts.append(_day(checks["first_return_date"], "start"))
        ends.append(_day(checks["last_return_date"], "end"))
        fact_types.extend(source["manifest"]["coverage"].get("fact_types") or [])
        conversion = entry.get("conversion")
        if conversion is not None:
            level_start = _day(
                source["catalog"]["series"].get("observed_start"), "level start"
            )
            content, report = _convert(
                member_id,
                rows,
                level_start,
                item["currency"],
                Path(str(conversion["fx_generation_root"])),
            )
            files[prefix / "returns-converted.csv"] = content
            item["conversion"] = {
                **report,
                "artifact_id": f"members/{member_id}/returns-converted.csv",
                "artifact_sha256": _sha(content),
                "target_currency": "USD",
            }
        member_entries.append(item)
        if role == "PRIMARY":
            primary_id = member_id
            primary_source = source
    assert primary_source is not None
    files[Path("returns.csv")] = primary_source["artifacts"]["returns.csv"]
    for relative, content in primary_source["artifacts"].items():
        if relative.startswith("raw/"):
            files[Path(relative)] = content

    series = primary_source["catalog"]["series"]
    catalog: dict[str, Any] = {
        "as_of_date": as_of.isoformat(),
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "closure": {
            "primary_returns_byte_identical_to_source": True,
            "primary_source_generation_id": primary_source["generation_id"],
            "member_count": len(member_entries),
        },
        "evidence_layer": "PROVIDER_TRACK_RECORD",
        "generation_status": "CANDIDATE_PENDING_OWNER_SELECTION",
        "members": member_entries,
        "mode_admission": primary_source["catalog"].get("mode_admission"),
        "primary_member_id": primary_id,
        "returns_artifact_id": "returns.csv",
        "selector_authority": "OWNER_SELECTION_REQUIRED",
        "series": series,
        "source_id": SOURCE_ID,
        "splice_policy": (
            "EACH_MEMBER_KEEPS_ITS_OWN_SPLICE_POLICY; THE_TOP_LEVEL_RETURNS_ARE_THE_"
            "PRIMARY_MEMBER_ONLY; OTHER_MEMBERS_ARE_READ_BY_MEMBER_ID"
        ),
    }
    catalog_bytes = canonical_json_bytes(catalog)
    files[Path("catalog.json")] = catalog_bytes
    artifacts = [
        SourceArtifact(
            artifact_id=relative.as_posix(),
            sha256=_sha(content),
            size_bytes=len(content),
            as_of_date=as_of,
            schema_name=(
                "provider-track-record-union-catalog"
                if relative.as_posix() == "catalog.json"
                else "provider-track-record-union-artifact"
            ),
            schema_version="2" if relative.as_posix() == "catalog.json" else "1",
        )
        for relative, content in sorted(
            files.items(), key=lambda item: item[0].as_posix()
        )
    ]
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity(
            "portfolio-provider-track-record-union", ADAPTER_VERSION
        ),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                f"{len(member_entries)} provider series carried from verified "
                "source generations; the primary member's returns and report are "
                "byte-identical to its source"
            ),
            fact_types=tuple(sorted(set(fact_types))),
            observed_start=min(starts),
            observed_end=max(ends),
            known_gaps=(),
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    files[Path("generation.json")] = manifest.to_json_bytes()
    return files, manifest, catalog


def stage_candidate(spec_path: Path, output_root: Path, as_of: date) -> dict[str, Any]:
    with localcontext() as context:
        context.prec = PRECISION
        files, manifest, catalog = _build(_spec(spec_path), as_of)
    generations = output_root.resolve() / "generations"
    final = generations / manifest.generation_id
    if final.exists():
        if final.is_symlink() or not final.is_dir():
            raise ProviderTrackRecordUnionError("candidate generation path conflicts")
        for relative, content in files.items():
            path = final / relative
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise ProviderTrackRecordUnionError("candidate generation differs")
    else:
        generations.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".portfolio-provider-union-", dir=generations)
        )
        try:
            for relative, content in files.items():
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            rename_directory_with_retry(temporary, final)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    manifest_bytes = manifest.to_json_bytes()
    return {
        "catalog_sha256": _sha(files[Path("catalog.json")]),
        "generation_id": manifest.generation_id,
        "manifest_sha256": _sha(manifest_bytes),
        "members": [
            {
                "member_id": m["member_id"],
                "source_generation_id": m["source_generation_id"],
                "source_returns_sha256": m["source_returns_sha256"],
            }
            for m in catalog["members"]
        ],
        "published": False,
        "state": "STAGED_CANDIDATE_PENDING_OWNER_SELECTION",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--as-of-date", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = stage_candidate(
            args.spec, args.output_root, _day(args.as_of_date, "as-of date")
        )
    except ProviderTrackRecordUnionError as exc:
        print(f"portfolio-provider-track-record-union: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
