from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import shutil
import sys
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


def _inputs(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
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
    return catalog, paths


def _csv_rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8"))))


def test_build_uses_exact_dates_decimal_returns_and_explicit_missing(
    tmp_path: Path,
) -> None:
    catalog, paths = _inputs(tmp_path)
    outputs, manifest = sources.build_bundle(catalog, paths)

    us_rows = _csv_rows(outputs["us_factors_daily.csv"])
    assert us_rows == [
        ["date", "mkt_rf", "smb", "hml", "rmw", "cma", "rf", "mom"],
        [
            "2026-01-02",
            "0.01",
            "0.02",
            "0.03",
            "0.04",
            "0.05",
            "0.0001",
            "0.06",
        ],
    ]
    europe_rows = _csv_rows(outputs["europe_factors_daily.csv"])
    assert [row[0] for row in europe_rows[1:]] == ["2026-01-02", "2026-01-05"]
    industry_rows = _csv_rows(outputs["us_49_industries_value_weighted_daily.csv"])
    assert industry_rows[1][-1] == ""

    output_manifest = {item["path"]: item for item in manifest["outputs"]}
    assert output_manifest["us_factors_daily.csv"]["factor_only_dates_dropped"] == 1
    assert output_manifest["us_factors_daily.csv"]["momentum_only_dates_dropped"] == 1
    industry_manifest = output_manifest["us_49_industries_value_weighted_daily.csv"]
    assert industry_manifest["missing_cells"] == 1
    assert manifest["policies"]["date_join"] == "EXACT_INTERSECTION_NO_FILL"


def test_bundle_is_deterministic_and_hashes_every_output(tmp_path: Path) -> None:
    catalog, paths = _inputs(tmp_path)
    first_outputs, first_manifest = sources.build_bundle(catalog, paths)
    second_outputs, second_manifest = sources.build_bundle(catalog, paths)
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
    for index, (key, path) in enumerate(paths.items()):
        relocated_paths[key] = relocated / f"source-{index}.json"
        shutil.copyfile(path, relocated_paths[key])
    relocated_outputs, relocated_manifest = sources.build_bundle(
        relocated_catalog, relocated_paths
    )
    assert relocated_outputs == first_outputs
    assert relocated_manifest == first_manifest


def test_rejects_duplicate_dates_and_catalog_url_drift(tmp_path: Path) -> None:
    catalog, paths = _inputs(tmp_path)
    value = json.loads(paths["us_factors"].read_text(encoding="utf-8"))
    value["sections"][0]["rows"].append(dict(value["sections"][0]["rows"][0]))
    _write_json(paths["us_factors"], value)
    with pytest.raises(sources.PublicSourceError, match="duplicate date"):
        sources.build_bundle(catalog, paths)

    catalog, paths = _inputs(tmp_path)
    value = json.loads(paths["europe_momentum"].read_text(encoding="utf-8"))
    value["source_url"] = "https://wrong.example.test/feed.zip"
    _write_json(paths["europe_momentum"], value)
    with pytest.raises(sources.PublicSourceError, match="does not match"):
        sources.build_bundle(catalog, paths)


def test_write_is_immutable(tmp_path: Path) -> None:
    catalog, paths = _inputs(tmp_path)
    root = tmp_path / "bundles"
    result = sources.write_bundle(catalog, paths, root)
    destination = Path(result["destination"])
    assert (destination / "source-manifest.json").is_file()
    with pytest.raises(sources.PublicSourceError, match="already exists"):
        sources.write_bundle(catalog, paths, root)
