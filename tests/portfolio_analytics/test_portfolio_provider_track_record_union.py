from __future__ import annotations

import csv
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
)
from quant_engine.core.contracts import canonical_json_bytes


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_provider_track_record_union.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_provider_track_record_union", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load the provider track-record union stager")
union = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = union
SPEC.loader.exec_module(union)
UnionError = union.ProviderTrackRecordUnionError

AS_OF = date(2025, 2, 28)
FIELDS = ("series_id", "date", "return", "evidence_layer", "return_basis")
TINY = Decimal("1e-20")


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _csv(rows: list[dict[str, str]], fields: tuple[str, ...]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _generation(
    parent: Path, files: dict[str, bytes], *, fact: str, start: date, end: date
) -> Path:
    artifacts = tuple(
        SourceArtifact(
            artifact_id=name,
            sha256=_sha(content),
            size_bytes=len(content),
            as_of_date=AS_OF,
            schema_name="fixture",
            schema_version="1",
        )
        for name, content in sorted(files.items())
    )
    manifest = GenerationManifest.build(
        as_of_date=AS_OF,
        adapter=AdapterIdentity("fixture", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="fixture",
            fact_types=(fact,),
            observed_start=start,
            observed_end=end,
            known_gaps=(),
        ),
        artifacts=artifacts,
        verification=VerificationStatus.VERIFIED,
    )
    root = parent / manifest.generation_id
    for name, content in {**files, "generation.json": manifest.to_json_bytes()}.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return root


def _days(start: date, count: int) -> list[date]:
    return [start + timedelta(days=index) for index in range(count)]


def _primary(tmp_path: Path) -> Path:
    days = _days(date(2024, 1, 2), 5)
    rows = [
        {
            "series_id": "Alpha",
            "date": day.isoformat(),
            "return": "0.01",
            "evidence_layer": "PROVIDER_TRACK_RECORD",
            "return_basis": "BASIS",
        }
        for day in days
    ]
    catalog = canonical_json_bytes(
        {
            "evidence_layer": "PROVIDER_TRACK_RECORD",
            "mode_admission": {"LIVE_ONLY": "EXCLUDE_COMPARISON_ONLY"},
            "coverage": {
                "observed_start": "2024-01-01",
                "observed_end": days[-1].isoformat(),
                "return_observations": 5,
            },
            "series": {
                "series_id": "Alpha",
                "observed_start": "2024-01-01",
                "observed_end": days[-1].isoformat(),
                "return_observations": 5,
                "derived_total_return": format(Decimal("1.01") ** 5 - 1, "f"),
            },
        }
    )
    return _generation(
        tmp_path / "primary",
        {
            "catalog.json": catalog,
            "returns.csv": _csv(rows, FIELDS),
            "raw/report.json": b'{"report": true}',
        },
        fact="PRIMARY_FACT",
        start=date(2024, 1, 2),
        end=days[-1],
    )


def _model(tmp_path: Path, *, live_bad: bool = False) -> Path:
    days = _days(date(2024, 1, 2), 6)
    layers = ["BACKTEST"] * 3 + ["PROVIDER_MODEL_LIVE"] * 3
    rows = [
        {
            "series_id": "Beta",
            "date": day.isoformat(),
            "return": "0.02",
            "evidence_layer": layer,
            "return_basis": "BASIS",
        }
        for day, layer in zip(days, layers, strict=True)
    ]
    catalog = canonical_json_bytes(
        {
            "evidence_layer": "PROVIDER_PUBLISHED_MODEL_CURVE",
            "mode_admission": {"LIVE_ONLY": "EXCLUDE_COMPARISON_ONLY"},
            "coverage": {
                "observed_start": "2024-01-01",
                "observed_end": days[-1].isoformat(),
                "return_observations": 6,
            },
            "series": {
                "series_id": "Beta",
                "currency": "EUR",
                "observed_start": "2024-01-01",
                "observed_end": days[-1].isoformat(),
                "return_observations": 6,
                "provider_launch": "2024-01-05",
                "prelaunch_return_observations": 3,
                "model_live_return_observations": 4 if live_bad else 3,
            },
        }
    )
    return _generation(
        tmp_path / "model",
        {"catalog.json": catalog, "returns.csv": _csv(rows, FIELDS)},
        fact="MODEL_FACT",
        start=date(2024, 1, 2),
        end=days[-1],
    )


def _fx(tmp_path: Path, omit: tuple[str, ...] = ()) -> Path:
    policy = {
        "policy_id": "fx-fiat-lookup-v1",
        "session_date_convention": "LOCAL_DATE_IN_PROVIDER_DECLARED_EXCHANGE_TIMEZONE",
        "null_close": "OMIT_ROW_NEVER_FILL",
        "lookup_rule": "LAST_OBSERVED_CLOSE_ON_OR_BEFORE_DATE_WITHIN_MAX_AGE",
        "max_lookup_age_days": 2,
    }
    rows = []
    for index, day in enumerate(_days(date(2024, 1, 1), 8)):
        if day.isoformat() in omit:
            continue
        rows.append(
            {
                "date": day.isoformat(),
                "currency": "EUR",
                "base_currency": "USD",
                "rate": format(Decimal("1.10") + Decimal(index) / 100, "f"),
                "provider": "fixture",
                "ticker": "FX",
                "field": "Close",
                "timing": "SESSION",
            }
        )
    files = {
        "catalog.json": canonical_json_bytes({"lookup_policy": policy}),
        "policy.json": json.dumps(policy).encode("utf-8"),
        "rates.csv": _csv(rows, tuple(rows[0])),
    }
    return _generation(
        tmp_path / "fx",
        files,
        fact="FX_FACT",
        start=date(2024, 1, 1),
        end=date(2024, 1, 8),
    )


def _spec(tmp_path: Path, omit: tuple[str, ...] = (), live_bad: bool = False) -> Path:
    spec = {
        "members": [
            {
                "member_id": "primary",
                "role": "PRIMARY",
                "series_id": "Alpha",
                "generation_root": str(_primary(tmp_path)),
            },
            {
                "member_id": "model",
                "role": "MEMBER",
                "series_id": "Beta",
                "generation_root": str(_model(tmp_path, live_bad=live_bad)),
                "conversion": {"fx_generation_root": str(_fx(tmp_path, omit))},
            },
        ]
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def _converted(root: Path) -> list[dict[str, str]]:
    text = (root / "members/model/returns-converted.csv").read_text(encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def test_union_keeps_the_primary_byte_identical_and_never_writes_current(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    result = union.stage_candidate(spec, tmp_path / "out", AS_OF)
    root = tmp_path / "out" / "generations" / result["generation_id"]
    primary = Path(json.loads(spec.read_text())["members"][0]["generation_root"])
    assert (root / "returns.csv").read_bytes() == (primary / "returns.csv").read_bytes()
    assert (root / "raw/report.json").read_bytes() == b'{"report": true}'
    assert not (tmp_path / "out" / "CURRENT.json").exists()
    catalog = json.loads((root / "catalog.json").read_bytes())
    assert catalog["generation_status"] == "CANDIDATE_PENDING_OWNER_SELECTION"
    assert [m["member_id"] for m in catalog["members"]] == ["primary", "model"]
    manifest = json.loads((root / "generation.json").read_bytes())
    listed = {a["artifact_id"]: a for a in manifest["artifacts"]}
    for relative, artifact in listed.items():
        assert _sha((root / relative).read_bytes()) == artifact["sha256"]
    assert "members/model/returns.csv" in listed
    assert "members/model/returns-converted.csv" in listed


def test_restaging_is_byte_identical(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    first = union.stage_candidate(spec, tmp_path / "out", AS_OF)
    second = union.stage_candidate(spec, tmp_path / "out", AS_OF)
    assert first == second


def test_conversion_telescopes_and_shows_both_currencies(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    result = union.stage_candidate(spec, tmp_path / "out", AS_OF)
    root = tmp_path / "out" / "generations" / result["generation_id"]
    rows = _converted(root)
    assert rows[0]["native_currency"] == "EUR"
    assert rows[0]["target_currency"] == "USD"
    expected = Decimal("1.02") * Decimal("1.11") / Decimal("1.10") - 1
    assert abs(Decimal(rows[0]["return"]) - expected) < TINY
    level = Decimal(1)
    for row in rows:
        level *= 1 + Decimal(row["return"])
    assert abs(level - Decimal("1.02") ** 6 * Decimal("1.16") / Decimal("1.10")) < TINY
    catalog = json.loads((root / "catalog.json").read_bytes())
    conversion = catalog["members"][1]["conversion"]
    assert conversion["fx_policy"]["null_close"] == "OMIT_ROW_NEVER_FILL"
    assert conversion["rows"] == 6
    assert conversion["native_rows"] == 6


def test_dates_without_a_usable_rate_merge_into_the_next_row_never_filled(
    tmp_path: Path,
) -> None:
    omit = ("2024-01-03", "2024-01-04", "2024-01-05", "2024-01-06")
    spec = _spec(tmp_path, omit=omit)
    result = union.stage_candidate(spec, tmp_path / "out", AS_OF)
    root = tmp_path / "out" / "generations" / result["generation_id"]
    catalog = json.loads((root / "catalog.json").read_bytes())
    conversion = catalog["members"][1]["conversion"]
    key = "native_dates_without_a_usable_rate_merged_into_next_converted_row"
    assert conversion[key] == ["2024-01-05", "2024-01-06"]
    merged = [r for r in _converted(root) if r["native_days_spanned"] != "1"]
    assert [r["date"] for r in merged] == ["2024-01-07"]
    assert merged[0]["native_days_spanned"] == "3"
    assert Decimal(conversion["telescoping_identity_residual"]) < TINY


def test_refuses_a_source_artifact_that_differs_from_its_manifest(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    primary = Path(json.loads(spec.read_text())["members"][0]["generation_root"])
    (primary / "returns.csv").write_bytes(b"tampered")
    with pytest.raises(UnionError, match="differs from its manifest"):
        union.stage_candidate(spec, tmp_path / "out", AS_OF)


def test_refuses_a_prelaunch_live_split_that_contradicts_the_catalog(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path, live_bad=True)
    with pytest.raises(UnionError, match="prelaunch/live split"):
        union.stage_candidate(spec, tmp_path / "out", AS_OF)


def test_refuses_a_spec_without_exactly_one_primary(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    data = json.loads(spec.read_text())
    data["members"][1]["role"] = "PRIMARY"
    spec.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(UnionError, match="exactly one PRIMARY"):
        union.stage_candidate(spec, tmp_path / "out", AS_OF)


def test_refuses_a_series_that_is_not_the_declared_one(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    data = json.loads(spec.read_text())
    data["members"][0]["series_id"] = "Other"
    spec.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(UnionError, match="foreign series"):
        union.stage_candidate(spec, tmp_path / "out", AS_OF)
