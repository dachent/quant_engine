from __future__ import annotations

import csv
from datetime import date
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_public_decomposition_sources.py"
SPEC = importlib.util.spec_from_file_location("portfolio_public_sources", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio public source normalizer")
sources = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sources
SPEC.loader.exec_module(sources)

RUN_STARTED_AT_UTC = "2026-03-17T11:55:00Z"
RUN_ENDED_AT_UTC = "2026-03-17T12:05:00Z"
EVALUATION_DATE = "2026-03-17"


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    return path


def _document(
    feed_id: str, url: str, sections: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "provider": "famafrench",
        "feed_id": feed_id,
        "source_url": url,
        "sections": sections,
    }


def _section(
    title: str, columns: list[str], rows: list[dict[str, object]]
) -> dict[str, object]:
    return {"title": title, "columns": columns, "rows": rows}


def _inputs(tmp_path: Path) -> tuple[Path, dict[str, Path], dict[str, Path]]:
    feeds = sources.EXPECTED_FEEDS
    urls = {key: f"https://example.test/{feed}.zip" for key, feed in feeds.items()}
    catalog = _write_json(
        tmp_path / "catalog.json",
        {
            "sources": [
                {"feed_id": feeds[key], "source_url": urls[key]} for key in feeds
            ]
        },
    )
    factor_columns = ["period", *sources.FACTOR_COLUMNS]
    factor_rows = [
        {
            "period": "20260102",
            "Mkt-RF": 1,
            "SMB": 2,
            "HML": 3,
            "RMW": 4,
            "CMA": 5,
            "RF": 0.01,
        },
        {
            "period": "20260105",
            "Mkt-RF": -1,
            "SMB": -2,
            "HML": -3,
            "RMW": -4,
            "CMA": -5,
            "RF": 0.02,
        },
    ]
    industry_names = [f"I{number:02d}" for number in range(1, 50)]
    industry_row: dict[str, object] = {"period": "20260102"}
    industry_row.update({name: number for number, name in enumerate(industry_names, 1)})
    industry_row["I49"] = None
    paths = {
        "us_factors": _write_json(
            tmp_path / "us-factors.json",
            _document(
                feeds["us_factors"],
                urls["us_factors"],
                [_section("Daily", factor_columns, factor_rows)],
            ),
        ),
        "us_momentum": _write_json(
            tmp_path / "us-momentum.json",
            _document(
                feeds["us_momentum"],
                urls["us_momentum"],
                [
                    _section(
                        "Daily",
                        ["period", "Mom"],
                        [
                            {"period": "20260102", "Mom": 6},
                            {"period": "20260106", "Mom": 99},
                        ],
                    )
                ],
            ),
        ),
        "europe_factors": _write_json(
            tmp_path / "eu-factors.json",
            _document(
                feeds["europe_factors"],
                urls["europe_factors"],
                [_section("Daily", factor_columns, factor_rows)],
            ),
        ),
        "europe_momentum": _write_json(
            tmp_path / "eu-momentum.json",
            _document(
                feeds["europe_momentum"],
                urls["europe_momentum"],
                [
                    _section(
                        "Daily",
                        ["period", "WML"],
                        [
                            {"period": "20260102", "WML": 7},
                            {"period": "20260105", "WML": 8},
                        ],
                    )
                ],
            ),
        ),
        "us_industries": _write_json(
            tmp_path / "industries.json",
            _document(
                feeds["us_industries"],
                urls["us_industries"],
                [
                    _section(
                        sources.INDUSTRY_TITLE,
                        ["period", *industry_names],
                        [industry_row],
                    )
                ],
            ),
        ),
    }
    manifest_paths: dict[str, Path] = {}
    for key, path in paths.items():
        content = path.read_bytes()
        csv_member = f"{key}.csv"
        csv_bytes = f"synthetic-raw-{key}".encode()
        raw_stream = io.BytesIO()
        with zipfile.ZipFile(raw_stream, "w") as archive:
            archive.writestr(csv_member, csv_bytes)
        raw = raw_stream.getvalue()
        request = {
            "feed_id": feeds[key],
            "format": "parsed",
            "provider": "famafrench",
            "source_url": urls[key],
        }
        adapter = {
            "id": "agent-market-data-terminal/famafrench",
            "version": "1.2.0",
        }
        response = {
            "captured_at_utc": "2026-03-17T12:00:00Z",
            "content_type": "application/zip",
            "etag": None,
            "final_url": urls[key],
            "last_modified": "Tue, 17 Mar 2026 12:00:00 GMT",
            "status": 200,
        }
        response_identity = {
            name: response[name]
            for name in (
                "content_type",
                "etag",
                "final_url",
                "last_modified",
                "status",
            )
        }
        raw_content_identity = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "zip_members": [
                {
                    "name": csv_member,
                    "sha256": hashlib.sha256(csv_bytes).hexdigest(),
                    "size_bytes": len(csv_bytes),
                }
            ],
        }
        raw_path = path.with_suffix(path.suffix + ".source.zip")
        raw_path.write_bytes(raw)
        raw_artifact = {"name": raw_path.name, **raw_content_identity}
        manifest_paths[key] = _write_json(
            path.with_suffix(path.suffix + ".manifest.json"),
            {
                "schema_version": 1,
                "source_run_id": hashlib.sha256(
                    sources._canonical_json(
                        {
                            "adapter": adapter,
                            "raw_artifact": raw_content_identity,
                            "request": request,
                            "response": response_identity,
                        }
                    )
                ).hexdigest(),
                "adapter": adapter,
                "request": request,
                "response": response,
                "coverage": {
                    "claim": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
                    "csv_member": csv_member,
                    "csv_member_sha256": hashlib.sha256(csv_bytes).hexdigest(),
                    "csv_member_size_bytes": len(csv_bytes),
                    "sections": [],
                },
                "raw_artifact": raw_artifact,
                "artifact": {
                    "name": path.name,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                },
            },
        )
    return catalog, paths, manifest_paths


def _csv_rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8"))))


def _set_capture_time(manifests: dict[str, Path], captured_at_utc: str) -> None:
    for path in manifests.values():
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["response"]["captured_at_utc"] = captured_at_utc
        _write_json(path, manifest)


def _set_source_dates(
    paths: dict[str, Path],
    manifests: dict[str, Path],
    dates: dict[str, list[str]],
) -> None:
    for key, path in paths.items():
        document = json.loads(path.read_text(encoding="utf-8"))
        rows = document["sections"][0]["rows"]
        assert len(rows) == len(dates[key])
        for row, period in zip(rows, dates[key], strict=True):
            row["period"] = period
        _write_json(path, document)
        content = path.read_bytes()
        manifest = json.loads(manifests[key].read_text(encoding="utf-8"))
        manifest["artifact"]["sha256"] = hashlib.sha256(content).hexdigest()
        manifest["artifact"]["size_bytes"] = len(content)
        _write_json(manifests[key], manifest)


def _build_bundle(
    catalog: Path, paths: dict[str, Path], manifests: dict[str, Path]
) -> tuple[dict[str, bytes], dict[str, object]]:
    return sources.build_bundle(
        catalog,
        paths,
        manifests,
        RUN_STARTED_AT_UTC,
        RUN_ENDED_AT_UTC,
        EVALUATION_DATE,
    )


def _write_bundle(
    catalog: Path,
    paths: dict[str, Path],
    manifests: dict[str, Path],
    root: Path,
) -> dict[str, object]:
    return sources.write_bundle(
        catalog,
        paths,
        manifests,
        root,
        RUN_STARTED_AT_UTC,
        RUN_ENDED_AT_UTC,
        EVALUATION_DATE,
    )


def test_build_uses_exact_dates_decimal_returns_and_explicit_missing(
    tmp_path: Path,
) -> None:
    catalog, paths, manifests = _inputs(tmp_path)
    outputs, manifest = _build_bundle(catalog, paths, manifests)

    us_rows = _csv_rows(outputs["us_factors_daily.csv"])
    assert us_rows == [
        ["date", "rf", "mkt_rf", "smb", "hml", "rmw", "cma", "mom"],
        [
            "2026-01-02",
            "0.0001",
            "0.01",
            "0.02",
            "0.03",
            "0.04",
            "0.05",
            "0.06",
        ],
    ]
    europe_rows = _csv_rows(outputs["europe_factors_daily.csv"])
    assert [row[0] for row in europe_rows[1:]] == ["2026-01-02", "2026-01-05"]
    industry_rows = _csv_rows(outputs["us_49_industries_value_weighted_daily.csv"])
    assert industry_rows[0][0:2] == ["date", "rf"]
    assert industry_rows[1][0:2] == ["2026-01-02", "0.0001"]
    assert industry_rows[1][-1] == ""

    output_manifest = {item["path"]: item for item in manifest["outputs"]}
    assert manifest["builder_version"] == "0.3.0"
    receipt_hashes = {
        key: hashlib.sha256(path.read_bytes()).hexdigest()
        for key, path in manifests.items()
    }
    acquisition_run_payload = {
        "evaluation_date": EVALUATION_DATE,
        "run_started_at_utc": RUN_STARTED_AT_UTC,
        "run_ended_at_utc": RUN_ENDED_AT_UTC,
        "receipt_manifest_sha256s": receipt_hashes,
    }
    assert manifest["schema_version"] == 2
    assert manifest["acquisition_run"] == {
        "run_id": hashlib.sha256(
            sources._canonical_json(acquisition_run_payload)
        ).hexdigest(),
        "evaluation_date": EVALUATION_DATE,
        "run_started_at_utc": RUN_STARTED_AT_UTC,
        "run_ended_at_utc": RUN_ENDED_AT_UTC,
        "acquisition_requirement": "EVERY_SOURCE_CAPTURED_WITHIN_CURRENT_RUN",
        "capture_window_first_utc": "2026-03-17T12:00:00Z",
        "capture_window_last_utc": "2026-03-17T12:00:00Z",
        "receipt_manifest_sha256s": receipt_hashes,
    }
    assert manifest["freshness_policy"] == {
        "policy_id": "KENNETH_FRENCH_MONTHLY_DAILY_45D_V1",
        "observation_cadence": "DAILY",
        "publication_cadence": "MONTHLY",
        "deadline_anchor": "CALENDAR_MONTH_END",
        "publication_lag_days": 45,
        "calendar": "CALENDAR_DAY",
        "deadline_inclusive": True,
        "boundary_rule": "LATEST_PROVIDER_VALID_DAY_IN_ELIGIBLE_MONTH",
        "receipt_rule": "CAPTURED_WITHIN_CURRENT_RUN",
    }
    assert output_manifest["us_factors_daily.csv"]["freshness"] == {
        "state": "CURRENT",
        "eligible_month": "2026-01",
        "eligible_month_end": "2026-01-31",
        "publication_deadline": "2026-03-17",
        "provider_available_end": "2026-01-02",
        "accepted_observation_end": "2026-01-02",
    }
    assert output_manifest["us_factors_daily.csv"]["factor_only_dates_dropped"] == 1
    assert output_manifest["us_factors_daily.csv"]["momentum_only_dates_dropped"] == 1
    industry_manifest = output_manifest["us_49_industries_value_weighted_daily.csv"]
    assert industry_manifest["missing_cells"] == 1
    assert manifest["policies"]["date_join"] == "EXACT_INTERSECTION_NO_FILL"


def test_bundle_is_deterministic_and_hashes_every_output(tmp_path: Path) -> None:
    catalog, paths, manifests = _inputs(tmp_path)
    first_outputs, first_manifest = _build_bundle(catalog, paths, manifests)
    second_outputs, second_manifest = _build_bundle(catalog, paths, manifests)
    assert first_outputs == second_outputs
    assert first_manifest == second_manifest
    for item in first_manifest["outputs"]:
        data = first_outputs[item["path"]]
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
        assert item["bytes"] == len(data)

    relocated = tmp_path / "relocated"
    relocated.mkdir()
    relocated_catalog = relocated / "renamed-catalog.json"
    shutil.copyfile(catalog, relocated_catalog)
    relocated_paths: dict[str, Path] = {}
    relocated_manifests: dict[str, Path] = {}
    for key, path in paths.items():
        source_root = relocated / key
        source_root.mkdir()
        relocated_paths[key] = source_root / path.name
        shutil.copyfile(path, relocated_paths[key])
        relocated_manifests[key] = source_root / manifests[key].name
        shutil.copyfile(
            manifests[key],
            relocated_manifests[key],
        )
        raw_name = json.loads(manifests[key].read_text(encoding="utf-8"))[
            "raw_artifact"
        ]["name"]
        shutil.copyfile(manifests[key].parent / raw_name, source_root / raw_name)
    relocated_outputs, relocated_manifest = _build_bundle(
        relocated_catalog, relocated_paths, relocated_manifests
    )
    assert relocated_outputs == first_outputs
    assert relocated_manifest == first_manifest


def test_monthly_publication_lag_accepts_june_on_august_17(tmp_path: Path) -> None:
    catalog, paths, manifests = _inputs(tmp_path)
    _set_source_dates(
        paths,
        manifests,
        {
            "us_factors": ["20260630", "20260701"],
            "us_momentum": ["20260630", "20260701"],
            "europe_factors": ["20260630", "20260701"],
            "europe_momentum": ["20260630", "20260701"],
            "us_industries": ["20260630"],
        },
    )
    _set_capture_time(manifests, "2026-08-17T12:00:00Z")

    _outputs, manifest = sources.build_bundle(
        catalog,
        paths,
        manifests,
        "2026-08-17T11:55:00Z",
        "2026-08-17T12:05:00Z",
        "2026-08-17",
    )

    outputs_by_name = {output["path"]: output for output in manifest["outputs"]}
    for output in outputs_by_name.values():
        assert output["freshness"]["state"] == "CURRENT"
        assert output["freshness"]["eligible_month"] == "2026-06"
        assert output["freshness"]["publication_deadline"] == "2026-08-14"
        assert output["freshness"]["accepted_observation_end"] == "2026-06-30"
        assert output["last_date"] == "2026-06-30"
    assert (
        outputs_by_name["us_factors_daily.csv"]["freshness"]["provider_available_end"]
        == "2026-07-01"
    )
    assert (
        outputs_by_name["europe_factors_daily.csv"]["freshness"][
            "provider_available_end"
        ]
        == "2026-07-01"
    )
    assert (
        outputs_by_name["us_49_industries_value_weighted_daily.csv"]["freshness"][
            "provider_available_end"
        ]
        == "2026-06-30"
    )
    for data in _outputs.values():
        assert _csv_rows(data)[-1][0] == "2026-06-30"


def test_publication_deadline_changes_required_month_on_day_45() -> None:
    assert sources._required_observation_month_end(date(2026, 8, 13)).isoformat() == (
        "2026-05-31"
    )
    assert sources._required_observation_month_end(date(2026, 8, 14)).isoformat() == (
        "2026-06-30"
    )
    assert sources._required_observation_month_end(date(2026, 9, 13)).isoformat() == (
        "2026-06-30"
    )
    assert sources._required_observation_month_end(date(2026, 9, 14)).isoformat() == (
        "2026-07-31"
    )
    assert sources._required_observation_month_end(date(2024, 3, 15)).isoformat() == (
        "2023-12-31"
    )
    assert sources._required_observation_month_end(date(2024, 3, 16)).isoformat() == (
        "2024-01-31"
    )


def test_publication_lag_fails_after_required_month_is_missing(
    tmp_path: Path,
) -> None:
    catalog, paths, manifests = _inputs(tmp_path)
    _set_source_dates(
        paths,
        manifests,
        {
            "us_factors": ["20260528", "20260529"],
            "us_momentum": ["20260528", "20260529"],
            "europe_factors": ["20260528", "20260529"],
            "europe_momentum": ["20260528", "20260529"],
            "us_industries": ["20260529"],
        },
    )
    _set_capture_time(manifests, "2026-08-17T12:00:00Z")

    with pytest.raises(
        sources.PublicSourceError, match="REQUIRED_PUBLICATION_MONTH_ABSENT: 2026-06"
    ):
        sources.build_bundle(
            catalog,
            paths,
            manifests,
            "2026-08-17T11:55:00Z",
            "2026-08-17T12:05:00Z",
            "2026-08-17",
        )


def test_rejects_receipt_outside_current_run_window(tmp_path: Path) -> None:
    catalog, paths, manifests = _inputs(tmp_path)

    with pytest.raises(sources.PublicSourceError, match="outside the current run"):
        sources.build_bundle(
            catalog,
            paths,
            manifests,
            "2026-03-17T12:01:00Z",
            "2026-03-17T12:05:00Z",
            "2026-03-17",
        )


def test_current_run_window_includes_both_exact_endpoints(tmp_path: Path) -> None:
    catalog, paths, manifests = _inputs(tmp_path)
    _set_capture_time(manifests, RUN_STARTED_AT_UTC)
    _build_bundle(catalog, paths, manifests)

    _set_capture_time(manifests, RUN_ENDED_AT_UTC)
    _build_bundle(catalog, paths, manifests)


def test_rejects_duplicate_dates_and_catalog_url_drift(tmp_path: Path) -> None:
    catalog, paths, manifests = _inputs(tmp_path)
    value = json.loads(paths["us_factors"].read_text(encoding="utf-8"))
    value["sections"][0]["rows"].append(dict(value["sections"][0]["rows"][0]))
    _write_json(paths["us_factors"], value)
    with pytest.raises(sources.PublicSourceError, match="duplicate date"):
        source_manifest = json.loads(manifests["us_factors"].read_text())
        changed = paths["us_factors"].read_bytes()
        source_manifest["artifact"]["sha256"] = hashlib.sha256(changed).hexdigest()
        source_manifest["artifact"]["size_bytes"] = len(changed)
        _write_json(manifests["us_factors"], source_manifest)
        _build_bundle(catalog, paths, manifests)

    catalog, paths, manifests = _inputs(tmp_path)
    value = json.loads(paths["europe_momentum"].read_text(encoding="utf-8"))
    value["source_url"] = "https://wrong.example.test/feed.zip"
    _write_json(paths["europe_momentum"], value)
    with pytest.raises(sources.PublicSourceError, match="does not match"):
        _build_bundle(catalog, paths, manifests)


def test_rejects_missing_or_tampered_source_run_manifest(tmp_path: Path) -> None:
    catalog, paths, manifests = _inputs(tmp_path)
    with pytest.raises(sources.PublicSourceError, match="source manifest keys"):
        _build_bundle(
            catalog,
            paths,
            {key: path for key, path in manifests.items() if key != "us_factors"},
        )

    manifest = json.loads(manifests["us_factors"].read_text(encoding="utf-8"))
    manifest["source_run_id"] = "0" * 64
    _write_json(manifests["us_factors"], manifest)
    with pytest.raises(sources.PublicSourceError, match="source-run identity differs"):
        _build_bundle(catalog, paths, manifests)

    tamper_root = tmp_path / "raw-tamper"
    tamper_root.mkdir()
    catalog, paths, manifests = _inputs(tamper_root)
    manifest = json.loads(manifests["us_factors"].read_text(encoding="utf-8"))
    raw_path = manifests["us_factors"].parent / manifest["raw_artifact"]["name"]
    raw_path.write_bytes(b"tampered")
    with pytest.raises(sources.PublicSourceError, match="raw source identity differs"):
        _build_bundle(catalog, paths, manifests)


def test_rejects_invalid_capture_timestamp(tmp_path: Path) -> None:
    catalog, paths, manifests = _inputs(tmp_path)
    manifest = json.loads(manifests["us_factors"].read_text(encoding="utf-8"))
    manifest["response"]["captured_at_utc"] = "not-a-dateZ"
    _write_json(manifests["us_factors"], manifest)

    with pytest.raises(sources.PublicSourceError, match="not RFC3339 UTC"):
        _build_bundle(catalog, paths, manifests)


def test_write_is_immutable(tmp_path: Path) -> None:
    catalog, paths, manifests = _inputs(tmp_path)
    root = tmp_path / "bundles"
    result = _write_bundle(catalog, paths, manifests, root)
    destination = Path(result["destination"])
    assert (destination / "source-manifest.json").is_file()
    with pytest.raises(sources.PublicSourceError, match="already exists"):
        _write_bundle(catalog, paths, manifests, root)
