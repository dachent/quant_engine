"""Normalize retained strategy histories into one governed analysis bundle.

The bundle binds current intended strategy definitions to historical model or
backtest returns without claiming that the account implemented every model
trade exactly. Implementation reconciliation remains a separate status.

This tool is deliberately local-only. It reads retained XLSX/JSON artifacts,
performs no provider call, and emits hash-bound CSV/JSON artifacts.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import statistics
from typing import Any, Final, Iterable, Mapping, Sequence
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from quant_engine.core.contracts import canonical_json_bytes


RETURN_FIELDS: Final = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "strategy_version",
)
SCHEMA_VERSION: Final = 1
BUILDER_VERSION: Final = "1.6.0"
# The provider publishes the EU model's launch date on its page and the owner
# states it as the date the model went live. Everything the provider shows
# before it is simulated design history, not a live track record, which is the
# split this builder has to make: the equity curve is one continuous path and
# nothing in its bytes marks where simulation ends.
P123_EU_PROVIDER_LAUNCH: Final = date(2024, 2, 18)
_SHEET_NS: Final = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
_SLUG = re.compile(r"\((tt-[^)]+)\)\s*$")


class StrategyHistoryError(ValueError):
    """A retained history source or transformation violates the contract."""


def _date_value(value: object, field: str) -> date:
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise StrategyHistoryError(f"{field} must be a canonical date") from exc
    return parsed


def _sha256(path: Path) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise StrategyHistoryError(f"source must be a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _decimal(value: object, name: str) -> Decimal:
    try:
        accepted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise StrategyHistoryError(f"{name} must be decimal") from exc
    if not accepted.is_finite():
        raise StrategyHistoryError(f"{name} must be finite")
    return accepted


def _canonical_return(value: Decimal) -> str:
    if value <= -1:
        raise StrategyHistoryError("period return must be greater than -1")
    rendered = format(value.normalize(), "f")
    return "0" if rendered in {"-0", ""} else rendered


def _excel_date(value: object, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return (datetime(1899, 12, 30) + timedelta(days=float(value))).date()
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise StrategyHistoryError(f"{name} is not a supported date")


def _column_index(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    if not letters:
        raise StrategyHistoryError("XLSX cell reference is invalid")
    result = 0
    for character in letters.upper():
        result = result * 26 + ord(character) - ord("A") + 1
    return result - 1


def _xlsx_rows_from_archive(
    archive: ZipFile, sheet_name: str
) -> list[list[object | None]]:
    """Read worksheet XML without trusting the frequently-wrong dimension tag."""

    try:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
    except (KeyError, ElementTree.ParseError) as exc:
        raise StrategyHistoryError("XLSX workbook metadata is invalid") from exc
    targets = {
        item.attrib["Id"]: item.attrib["Target"]
        for item in relationships.findall(f"{{{_PKG_REL_NS}}}Relationship")
    }
    target: str | None = None
    for item in workbook.findall(f".//{{{_SHEET_NS}}}sheet"):
        if item.attrib.get("name") == sheet_name:
            relation = item.attrib.get(f"{{{_REL_NS}}}id")
            target = targets.get(str(relation))
            break
    if target is None:
        raise StrategyHistoryError(f"XLSX sheet is missing: {sheet_name}")
    raw_target = PurePosixPath(target.lstrip("/"))
    sheet_path = (
        raw_target
        if raw_target.parts and raw_target.parts[0] == "xl"
        else PurePosixPath("xl") / raw_target
    )
    shared: list[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        for item in root.findall(f"{{{_SHEET_NS}}}si"):
            shared.append(
                "".join(
                    str(node.text or "")
                    for node in item.findall(f".//{{{_SHEET_NS}}}t")
                )
            )
    try:
        root = ElementTree.fromstring(archive.read(str(sheet_path)))
    except (KeyError, ElementTree.ParseError) as exc:
        raise StrategyHistoryError(f"XLSX sheet is invalid: {sheet_name}") from exc
    result: list[list[object | None]] = []
    for raw_row in root.findall(f".//{{{_SHEET_NS}}}sheetData/{{{_SHEET_NS}}}row"):
        indexed: dict[int, object | None] = {}
        for cell in raw_row.findall(f"{{{_SHEET_NS}}}c"):
            index = _column_index(str(cell.attrib.get("r", "")))
            kind = cell.attrib.get("t")
            value_node = cell.find(f"{{{_SHEET_NS}}}v")
            if kind == "inlineStr":
                value: object | None = "".join(
                    str(node.text or "")
                    for node in cell.findall(f".//{{{_SHEET_NS}}}t")
                )
            elif value_node is None or value_node.text is None:
                value = None
            elif kind == "s":
                try:
                    value = shared[int(value_node.text)]
                except (IndexError, ValueError) as exc:
                    raise StrategyHistoryError(
                        "XLSX shared string reference is invalid"
                    ) from exc
            elif kind in {"str", "e"}:
                value = value_node.text
            elif kind == "b":
                value = value_node.text == "1"
            else:
                try:
                    numeric = float(value_node.text)
                except ValueError:
                    value = value_node.text
                else:
                    value = int(numeric) if numeric.is_integer() else numeric
            indexed[index] = value
        width = max(indexed, default=-1) + 1
        result.append([indexed.get(index) for index in range(width)])
    return result


def _xlsx_rows(path: Path, sheet_name: str) -> list[list[object | None]]:
    """Read cached XLSX cell values using only the Python standard library."""

    try:
        archive = ZipFile(path)
    except (OSError, BadZipFile) as exc:
        raise StrategyHistoryError(f"XLSX source is not readable: {path}") from exc
    with archive:
        return _xlsx_rows_from_archive(archive, sheet_name)


def _xlsx_rows_content(content: bytes, sheet_name: str) -> list[list[object | None]]:
    """Read an XLSX embedded in a retained provider archive."""

    try:
        archive = ZipFile(io.BytesIO(content))
    except BadZipFile as exc:
        raise StrategyHistoryError("embedded XLSX source is not readable") from exc
    with archive:
        return _xlsx_rows_from_archive(archive, sheet_name)


def _cell(row: Sequence[object | None], index: int) -> object | None:
    return row[index] if index < len(row) else None


def _version(prefix: str, payload: Mapping[str, object]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(dict(payload))).hexdigest()
    return f"{prefix}:{digest}"


def _return_rows(
    series_id: str,
    version: str,
    values: Iterable[tuple[date, Decimal]],
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[date] = set()
    for observed, period_return in sorted(values):
        if observed in seen:
            raise StrategyHistoryError(f"duplicate history date: {series_id}")
        seen.add(observed)
        result.append(
            {
                "series_id": series_id,
                "date": observed.isoformat(),
                "return": _canonical_return(period_return),
                "evidence_layer": "BACKTEST",
                "strategy_version": version,
            }
        )
    if not result:
        raise StrategyHistoryError(f"history is empty: {series_id}")
    return result


def _definition(
    *,
    series_id: str,
    version: str,
    rows: Sequence[Mapping[str, str]],
    history_status: str,
    compatibility_basis: str,
    frequency: str,
    implementation_status: str,
    method: str,
    source: Mapping[str, object],
    coverage_disposition: str = "COMPLETE_WITHIN_RETAINED_SOURCE_BOUNDARY",
) -> dict[str, object]:
    return {
        "compatibility_basis": compatibility_basis,
        "coverage_disposition": coverage_disposition,
        "frequency": frequency,
        "history_status": history_status,
        "implementation_reconciliation_status": implementation_status,
        "method": method,
        "observed_end": rows[-1]["date"],
        "observed_start": rows[0]["date"],
        "required_for_analysis": True,
        "return_observations": len(rows),
        "series_id": series_id,
        "source_artifacts": [dict(source)],
        "strategy_version": version,
    }


def _source_identity(path: Path, source_id: str) -> dict[str, object]:
    digest, size = _sha256(path)
    return {
        "filename": path.name,
        "sha256": digest,
        "size_bytes": size,
        "source_id": source_id,
    }


def _grizzly_simulation_dates(path: Path, label: str) -> list[date]:
    rows = _xlsx_rows(path, "Simulation")
    if not rows:
        raise StrategyHistoryError(f"Grizzly {label} simulation sheet is empty")
    headers = {str(value).strip(): index for index, value in enumerate(rows[0])}
    if not {"date", "day_ret"} <= set(headers):
        raise StrategyHistoryError(f"Grizzly {label} simulation headers differ")
    dates = [
        _excel_date(_cell(row, headers["date"]), f"Grizzly {label} date")
        for row in rows[1:]
        if _cell(row, headers["date"]) is not None
        and _cell(row, headers["day_ret"]) is not None
    ]
    if len(dates) < 1_000 or dates != sorted(set(dates)):
        raise StrategyHistoryError(
            f"Grizzly {label} simulation dates are short, duplicate, or unordered"
        )
    return dates


def _grizzly_implementation_lineage(
    *,
    original_design_workbook: Path,
    spx_workbook: Path,
    spy_workbook: Path,
    spxl_workbook: Path,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    original_source = _source_identity(
        original_design_workbook, "grizzlybulls-original-futures-design-trade-log"
    )
    spx_source = _source_identity(spx_workbook, "grizzlybulls-spx-research-backtest")
    spy_source = _source_identity(spy_workbook, "grizzlybulls-spy-research-backtest")
    spxl_source = _source_identity(
        spxl_workbook, "grizzlybulls-spxl-current-implementation-backtest"
    )

    original_rows = _xlsx_rows(original_design_workbook, "Sheet1")
    if not original_rows:
        raise StrategyHistoryError("Grizzly original futures design sheet is empty")
    original_headers = {
        str(value).strip(): index for index, value in enumerate(original_rows[0])
    }
    required_original = {"Date (EST)", "Action", "SPX", "Model"}
    if not required_original <= set(original_headers):
        raise StrategyHistoryError("Grizzly original futures design headers differ")
    original_dates = [
        _excel_date(
            _cell(row, original_headers["Date (EST)"]),
            "Grizzly original futures design date",
        )
        for row in original_rows[1:]
        if _cell(row, original_headers["Date (EST)"]) is not None
        and _cell(row, original_headers["Model"]) is not None
    ]
    if not original_dates or original_dates != sorted(original_dates):
        raise StrategyHistoryError(
            "Grizzly original futures design dates are empty or unordered"
        )

    variant_dates = {
        "SPX": _grizzly_simulation_dates(spx_workbook, "SPX"),
        "SPY": _grizzly_simulation_dates(spy_workbook, "SPY"),
        "SPXL": _grizzly_simulation_dates(spxl_workbook, "SPXL"),
    }
    if not (variant_dates["SPX"] == variant_dates["SPY"] == variant_dates["SPXL"]):
        raise StrategyHistoryError(
            "Grizzly SPX, SPY, and SPXL research backtests do not share one calendar"
        )

    layers = [
        {
            "instrument": "S&P_500_FUTURES",
            "layer_id": "FUTURES_DESIGN",
            "observed_end": original_dates[-1].isoformat(),
            "observed_start": original_dates[0].isoformat(),
            "observation_count": len(original_dates),
            "role": "ORIGINAL_DESIGN_AND_BACKTEST",
            "source_artifact": original_source,
            "status": "ACCEPTED_DESIGN_EVIDENCE_NOT_PORTFOLIO_RETURN_SERIES",
            "underlying_signal_reference": "SPX",
        }
    ]
    for instrument, source in (
        ("SPX", spx_source),
        ("SPY", spy_source),
        ("SPXL", spxl_source),
    ):
        layers.append(
            {
                "instrument": instrument,
                "layer_id": f"{instrument}_BACKTEST",
                "observed_end": variant_dates[instrument][-1].isoformat(),
                "observed_start": variant_dates[instrument][0].isoformat(),
                "observation_count": len(variant_dates[instrument]),
                "role": (
                    "SELECTED_CURRENT_IMPLEMENTATION_BACKTEST"
                    if instrument == "SPXL"
                    else "RESEARCH_IMPLEMENTATION_BACKTEST"
                ),
                "source_artifact": source,
                "status": (
                    "ADMITTED_CURRENT_PORTFOLIO_IMPLEMENTATION_HISTORY"
                    if instrument == "SPXL"
                    else "ACCEPTED_COMPARATIVE_RESEARCH_NOT_PORTFOLIO_RETURN_SERIES"
                ),
            }
        )
    return (
        {
            "authority_basis": (
                "OWNER_CONFIRMED_ORIGINAL_FUTURES_DESIGN_WITH_RETAINED_"
                "FUTURES_SPX_SPY_SPXL_WORKBOOKS"
            ),
            "current_live_implementation": {
                "instrument": "SPXL",
                "return_authority": "ACCOUNT_LIVE",
                "trade_reconciliation": "SEPARATE_GOVERNED_CONTRACT",
            },
            "design_analysis_binding": "FUTURES_DESIGN",
            "layers": layers,
            "portfolio_return_series_binding": (
                "SPXL_BACKTEST_STRICTLY_BEFORE_LIVE_THEN_ACCOUNT_LIVE_SPXL"
            ),
            "status": "VERIFIED_DISTINCT_DESIGN_RESEARCH_AND_LIVE_LAYERS",
        },
        [original_source, spx_source, spy_source],
    )


def _content_identity(
    content: bytes, source_id: str, filename: str
) -> dict[str, object]:
    return {
        "filename": filename,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "source_id": source_id,
    }


def _verified_generation_artifact(
    generation_root: Path, artifact_id: str
) -> tuple[Path, dict[str, object], dict[str, object]]:
    manifest_path = generation_root / "generation.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise StrategyHistoryError("Portfolio123 generation manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrategyHistoryError(
            "Portfolio123 generation manifest is invalid"
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("verification") != "VERIFIED":
        raise StrategyHistoryError("Portfolio123 generation is not verified")
    artifacts = {
        str(item.get("artifact_id")): item
        for item in manifest.get("artifacts", [])
        if isinstance(item, dict)
    }
    expected = artifacts.get(artifact_id)
    if expected is None:
        raise StrategyHistoryError(
            f"Portfolio123 generation lacks artifact: {artifact_id}"
        )
    path = generation_root / Path(artifact_id)
    digest, size = _sha256(path)
    if (digest, size) != (expected.get("sha256"), expected.get("size_bytes")):
        raise StrategyHistoryError(
            f"Portfolio123 generation artifact differs: {artifact_id}"
        )
    return path, dict(expected), manifest


def _p123_history_dispositions(
    generation_root: Path,
) -> tuple[list[dict[str, object]], tuple[list[dict[str, str]], dict[str, object]] | None]:
    """Classify the exact EU capture and retain deliberate non-history decisions.

    Also returns the EU backtest part -- the prehistory strictly before the
    provider's own launch -- in the same ``(rows, definition)`` shape the
    vendor-workbook readers use, or ``None`` when the capture holds no such
    history.

    Provider entity identifiers are used only in memory to test exact capture.
    They are never written to the bundle. Similar European books are inventoried
    by name and coverage but cannot be substituted for the connected 13-position
    model without an explicit version relationship.
    """

    try:
        import pyarrow.parquet as parquet  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - installed project dependency
        raise StrategyHistoryError(
            "pyarrow is required for Portfolio123 proof"
        ) from exc

    metadata_path, metadata_artifact, manifest = _verified_generation_artifact(
        generation_root, "tables/har_managed_metadata.parquet"
    )
    equity_path, equity_artifact, equity_manifest = _verified_generation_artifact(
        generation_root, "tables/har_equity_curve.parquet"
    )
    performance_path, performance_artifact, performance_manifest = (
        _verified_generation_artifact(
            generation_root, "tables/har_managed_performance.parquet"
        )
    )
    daily_path, daily_artifact, daily_manifest = _verified_generation_artifact(
        generation_root, "tables/returns_daily.parquet"
    )
    registry_path, registry_artifact, registry_manifest = _verified_generation_artifact(
        generation_root, "tables/strategy_registry.parquet"
    )
    generation_id = str(manifest.get("generation_id", ""))
    if (
        len(generation_id) != 64
        or equity_manifest.get("generation_id") != generation_id
        or performance_manifest.get("generation_id") != generation_id
        or daily_manifest.get("generation_id") != generation_id
        or registry_manifest.get("generation_id") != generation_id
    ):
        raise StrategyHistoryError("Portfolio123 generation identity differs")

    metadata = parquet.read_table(metadata_path).to_pylist()
    connected_rows = [
        row
        for row in metadata
        if "eu multifactor microcap" in str(row.get("name") or "").casefold()
        and str(row.get("isAccountLive") or "").casefold() == "true"
    ]
    connected_by_signature: dict[str, dict[str, object]] = {}
    for row in connected_rows:
        signature = {
            field: row.get(field)
            for field in (
                "assetName",
                "assetUid",
                "currency",
                "inceptionDt",
                "isAccountLive",
                "name",
                "portfolioUid",
                "posCnt",
                "strategyTotalHoldCount",
            )
        }
        key = hashlib.sha256(canonical_json_bytes(signature)).hexdigest()
        connected_by_signature[key] = row
    if len(connected_by_signature) != 1:
        raise StrategyHistoryError(
            "Portfolio123 connected EU model metadata is not unique"
        )
    exact = next(iter(connected_by_signature.values()))
    exact_ids = {
        str(exact.get(field) or "").strip()
        for field in ("assetUid", "portfolioUid")
        if str(exact.get(field) or "").strip()
    }
    if not exact_ids:
        raise StrategyHistoryError("Portfolio123 connected EU model lacks identity")

    equity = parquet.read_table(equity_path).to_pylist()
    daily = parquet.read_table(
        daily_path, columns=["entity_id", "period_date", "model_return"]
    ).to_pylist()
    exact_equity = [
        row
        for row in equity
        if str(row.get("_entity_id") or "") in exact_ids
        and row.get("Total Equity") not in (None, "")
    ]
    exact_daily = [row for row in daily if str(row.get("entity_id") or "") in exact_ids]
    provider_summary: dict[str, object] | None = None
    if exact_equity:
        observations: dict[date, tuple[Decimal, Decimal]] = {}
        for row in exact_equity:
            try:
                observed = date.fromisoformat(str(row.get("Date") or ""))
            except ValueError as exc:
                raise StrategyHistoryError(
                    "Portfolio123 exact EU provider date is invalid"
                ) from exc
            equity_value = _decimal(
                row.get("Total Equity"), "Portfolio123 exact EU equity"
            )
            cash_added = _decimal(
                row.get("Cash Added") or 0, "Portfolio123 exact EU cash added"
            )
            if equity_value <= 0:
                raise StrategyHistoryError(
                    "Portfolio123 exact EU equity must be positive"
                )
            value = (equity_value, cash_added)
            if observed in observations and observations[observed] != value:
                raise StrategyHistoryError(
                    "Portfolio123 exact EU curve has conflicting duplicate dates"
                )
            observations[observed] = value
        ordered = sorted(observations.items())
        if len(ordered) < 2:
            raise StrategyHistoryError(
                "Portfolio123 exact EU provider history is insufficient"
            )
        performance_rows = [
            row
            for row in parquet.read_table(performance_path).to_pylist()
            if str(row.get("_entity_id") or "") in exact_ids
        ]
        captures: dict[str, dict[str, str]] = {}
        by_entity: dict[str, dict[str, str]] = {}
        for row in performance_rows:
            entity_id = str(row.get("_entity_id") or "")
            field = str(row.get("field") or "")
            performance_value = str(row.get("value") or "")
            prior_field = by_entity.setdefault(entity_id, {}).get(field)
            if prior_field is not None and prior_field != performance_value:
                raise StrategyHistoryError(
                    "Portfolio123 exact EU performance has conflicting captures"
                )
            by_entity[entity_id][field] = performance_value
        for fields in by_entity.values():
            if not {"data", "date", "dateLast", "panels"} <= set(fields):
                continue
            capture_signature = hashlib.sha256(
                canonical_json_bytes(
                    {key: fields[key] for key in ("data", "date", "dateLast", "panels")}
                )
            ).hexdigest()
            captures[capture_signature] = fields
        if len(captures) != 1:
            raise StrategyHistoryError(
                "Portfolio123 exact EU performance is not unique"
            )
        fields = next(iter(captures.values()))
        try:
            performance_start = date.fromisoformat(fields["date"])
            performance_end = date.fromisoformat(fields["dateLast"])
            data = json.loads(fields["data"])
            panels = json.loads(fields["panels"])
        except (ValueError, json.JSONDecodeError) as exc:
            raise StrategyHistoryError(
                "Portfolio123 exact EU performance is invalid"
            ) from exc
        if not isinstance(data, list) or not isinstance(panels, list):
            raise StrategyHistoryError(
                "Portfolio123 exact EU performance shape differs"
            )
        graphs = [
            graph
            for panel in panels
            if isinstance(panel, dict)
            for graph in panel.get("graphs", [])
            if isinstance(graph, dict)
        ]
        if not any(
            graph.get("idx") == 0 and graph.get("s") == "Strategy" for graph in graphs
        ) or not any(
            graph.get("idx") == 1 and graph.get("s") == "Total Value"
            for graph in graphs
        ):
            raise StrategyHistoryError(
                "Portfolio123 exact EU performance selector differs"
            )
        calendar: list[date] = []
        observed = performance_start
        while observed <= performance_end:
            if observed.weekday() < 5:
                calendar.append(observed)
            observed += timedelta(days=1)
        if len(calendar) != len(data):
            raise StrategyHistoryError(
                "Portfolio123 exact EU performance calendar differs"
            )
        parsed: list[tuple[date, Decimal, Decimal, Decimal]] = []
        for observed, raw in zip(calendar, data, strict=True):
            if not isinstance(raw, list) or len(raw) != 3:
                raise StrategyHistoryError(
                    "Portfolio123 exact EU performance observation differs"
                )
            # Each observation is [marker, Strategy, Total Value]: ``panels`` names
            # the two graphs, Strategy at idx 0 and Total Value at idx 1, and the
            # leading element is a marker outside that indexing.  Reading the
            # marker as a percentage collapsed every return onto the three values
            # the marker takes.  The level is already a wealth index, so period
            # returns are its ratios and its base cancels.
            level = _decimal(raw[1], "Portfolio123 exact EU strategy level")
            total_value = _decimal(raw[2], "Portfolio123 exact EU total value")
            if level <= 0 or total_value < 0:
                raise StrategyHistoryError(
                    "Portfolio123 exact EU performance contains invalid values"
                )
            parsed.append((observed, level, level, total_value))
        overlap = 0
        for observed, _level, curve_level, curve_total in parsed:
            if observed not in observations:
                continue
            overlap += 1
            if observations[observed] != (curve_level, curve_total):
                raise StrategyHistoryError(
                    "Portfolio123 exact EU performance and equity differ"
                )
        if overlap < 2:
            raise StrategyHistoryError(
                "Portfolio123 exact EU performance/equity overlap is insufficient"
            )
        provider_returns = [
            current[1] / previous[1] - Decimal(1)
            for previous, current in zip(parsed, parsed[1:], strict=False)
        ]
        if not provider_returns or any(value <= -1 for value in provider_returns):
            raise StrategyHistoryError(
                "Portfolio123 exact EU provider return is invalid"
            )
        compounded = Decimal(1)
        for provider_return in provider_returns:
            compounded *= Decimal(1) + provider_return
        provider_summary = {
            # The transform previously published only counts, so nothing
            # observed whether the level or the leading marker produced the
            # series.  The compounded result is the transform's own answer.
            "cash_added_semantics": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
            "cumulative_return": format((compounded - Decimal(1)).normalize(), "f"),
            "equity_overlap_observations": overlap,
            "observed_end": parsed[-1][0].isoformat(),
            "observed_start": parsed[1][0].isoformat(),
            "raw_observations": len(parsed),
            "return_observations": len(provider_returns),
            "transform_id": "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1",
        }

    eu_backtest: tuple[list[dict[str, str]], dict[str, object]] | None = None
    prehistory_disposition: dict[str, object] = {
        "provider_launch": P123_EU_PROVIDER_LAUNCH.isoformat(),
        "status": "NO_EXACT_MODEL_CAPTURE_BEFORE_PROVIDER_LAUNCH",
    }
    if provider_summary is not None:
        eu_backtest = _p123_eu_backtest(
            ordered,
            generation_id=generation_id,
            exact_ids=exact_ids,
            strategy_name=str(exact.get("assetName") or ""),
            equity_artifact=equity_artifact,
        )
        if eu_backtest is None:
            # The designer model the owner subscribes to states its launch on
            # the provider's page, and its own history runs from well before
            # it. What this generation captured is the SUBSCRIBED COPY, whose
            # curve begins when the copy was created, plus several similarly
            # named European books that carry long curves but no captured
            # relationship to the subscribed model. Splicing one of those in
            # would be substituting a different book for the model, which is
            # the substitution this builder exists to refuse -- so the gap is
            # recorded as a capture gap rather than filled.
            prehistory_disposition.update(
                {
                    "exact_model_observed_start": ordered[0][0].isoformat(),
                    "exact_model_observed_end": ordered[-1][0].isoformat(),
                    "required_capture": (
                        "DESIGNER_MODEL_HISTORY_AND_LAUNCH_SNAPSHOT_FOR_THE_"
                        "SUBSCRIBED_MODEL"
                    ),
                    "reason": (
                        "The captured provider history for the exact connected "
                        "model begins after the provider's stated launch, so no "
                        "pre-launch prehistory exists to admit. Long European "
                        "curves captured for other books are not substitutes "
                        "without a captured version relationship."
                    ),
                }
            )
        else:
            prehistory_disposition.update(
                {
                    "observed_end": eu_backtest[1]["observed_end"],
                    "observed_start": eu_backtest[1]["observed_start"],
                    "return_observations": eu_backtest[1]["return_observations"],
                    "status": "ADMITTED_STRICTLY_BEFORE_PROVIDER_LAUNCH",
                }
            )

    registry = {
        str(row.get("entity_id") or ""): str(row.get("canonical_name") or "")
        for row in parquet.read_table(registry_path).to_pylist()
    }
    alternatives: list[dict[str, object]] = []
    for entity_id, name in sorted(registry.items(), key=lambda item: item[1]):
        folded = name.casefold()
        if "europe" not in folded and "eu investor" not in folded:
            continue
        rows = [
            row
            for row in equity
            if str(row.get("_entity_id") or "") == entity_id
            and (
                row.get("Total Equity")
                or row.get("100 EUR Ret")
                or row.get("100 USD Ret")
            )
        ]
        if not rows:
            continue
        dates = sorted(str(row.get("Date") or "") for row in rows if row.get("Date"))
        positions = sorted(
            {
                str(row.get("# Positions") or "")
                for row in rows
                if str(row.get("# Positions") or "").strip()
            }
        )
        alternatives.append(
            {
                "canonical_name": name,
                "observed_end": dates[-1] if dates else None,
                "observed_start": dates[0] if dates else None,
                "position_count_examples": positions[:5],
                "return_observations": len(rows),
                "status": "RETAINED_NOT_ADMITTED_DIFFERENT_MODEL_BOOK",
            }
        )

    source_artifacts = [
        {
            "artifact_id": artifact_id,
            "sha256": artifact["sha256"],
            "size_bytes": artifact["size_bytes"],
        }
        for artifact_id, artifact in (
            ("tables/har_managed_metadata.parquet", metadata_artifact),
            ("tables/har_managed_performance.parquet", performance_artifact),
            ("tables/har_equity_curve.parquet", equity_artifact),
            ("tables/returns_daily.parquet", daily_artifact),
            ("tables/strategy_registry.parquet", registry_artifact),
        )
    ]
    eu_version = _version(
        "p123-eu-connected-model",
        {
            "generation_id": generation_id,
            "provider_entity_ids": sorted(exact_ids),
            "strategy_name": str(exact.get("assetName") or ""),
        },
    )
    if provider_summary is None:
        exact_disposition: dict[str, object] = {
            "action": "CAPTURE_EXACT_CONNECTED_MODEL_PROVIDER_LIVE_HISTORY",
            "exact_equity_curve_rows": 0,
            "exact_normalized_daily_rows": len(exact_daily),
            "history_status": "UNAVAILABLE_EXACT_PROVIDER_LIVE_NOT_CAPTURED",
            "implementation_reconciliation_status": "BLOCKED_HISTORY_CAPTURE",
            "reason": (
                "The exact connected live EU model is identified, but its provider-live "
                "equity curve is not captured. Different European books remain "
                "inadmissible substitutes."
            ),
            "status": "NOT_CAPTURED_EXACT_CONNECTED_MODEL_PROVIDER_LIVE",
        }
    else:
        exact_disposition = {
            "action": "ROUTE_CANDIDATE_PROVIDER_PERFORMANCE_TRANSFORM_TO_303",
            "exact_equity_curve_rows": len(exact_equity),
            "exact_normalized_daily_rows": len(exact_daily),
            "history_status": (
                "CAPTURED_EXACT_VERSION_PROVIDER_LIVE_RETURN_TRANSFORM_DERIVED_NOT_ADMITTED"
            ),
            "implementation_reconciliation_status": (
                "BLOCKED_ADMIT_OR_DECLARE; SEPARATE_FROM_ACCOUNT_LIVE"
            ),
            "provider_live": provider_summary,
            "reason": (
                "The exact connected-model provider-managed Strategy performance level "
                "supports a deterministic candidate daily-return derivation. Cash Added "
                "reconciles to the equity curve but its financial interpretation is not "
                "approved and is not used in the candidate formula. No provider-live "
                "return is admitted before #303; the IBKR ACCOUNT_LIVE path is unchanged."
            ),
            "status": (
                "CAPTURED_EXACT_CONNECTED_MODEL_PROVIDER_LIVE_TRANSFORM_DERIVED_NOT_ADMITTED"
            ),
        }
    return [
        {
            **exact_disposition,
            "connected_model_currency": str(exact.get("currency") or ""),
            "backtest_prehistory": prehistory_disposition,
            "connected_model_holding_count": int(
                str(exact.get("strategyTotalHoldCount") or exact.get("posCnt") or 0)
            ),
            "connected_model_inception": str(exact.get("inceptionDt") or ""),
            "required_for_analysis": True,
            "retained_but_not_admitted": alternatives,
            "series_id": "EU [P123]",
            "source_artifacts": source_artifacts,
            "strategy_version": eu_version,
        },
        {
            "action": "NONE",
            "history_status": "RETAINED_REJECTED_HISTORY_DEPRECATED",
            "implementation_reconciliation_status": "NOT_APPLICABLE_DEPRECATED",
            "reason": (
                "EchoTrade is deprecated; retained event-only history is rejected "
                "as non-daily account-live evidence."
            ),
            "required_for_analysis": False,
            "series_id": "Growth at Market Pivot Points [EchoTrade]",
            "status": "DEPRECATED_RETAINED_REJECTED_HISTORY_BY_GOVERNANCE",
            "strategy_version": (
                "echotrade-deprecated-retained-rejected-history:2026-08-29"
            ),
        },
        {
            "action": "NONE_UNLESS_MATERIALITY_CHANGES",
            "history_status": "UNAVAILABLE_IMMATERIAL_HISTORY_NOT_CONNECTED",
            "implementation_reconciliation_status": "NOT_APPLICABLE_IMMATERIAL",
            "reason": (
                "Composer is below the materiality boundary and its history is not "
                "connected to the governed strategy path."
            ),
            "required_for_analysis": False,
            "series_id": "[Composer] - The Holy Grail (Buy Copy)",
            "status": "IMMATERIAL_HISTORY_NOT_CONNECTED_BY_GOVERNANCE",
            "strategy_version": "composer-immaterial-unconnected:2026-08-08",
        },
    ], eu_backtest


def _robuxio_track_record(
    generation_root: Path, strategy_version: str
) -> tuple[list[dict[str, str]], dict[str, object]]:
    """Read the governed provider track-record generation as design history.

    The generation retains the provider's published return path with its own
    manifest digest, so admitting it is a digest check plus a relabel of the
    evidence layer -- no return is recomputed here.
    """

    manifest_path = generation_root / "generation.json"
    returns_path = generation_root / "returns.csv"
    for candidate in (manifest_path, returns_path):
        if candidate.is_symlink() or not candidate.is_file():
            raise StrategyHistoryError(
                f"Robuxio track-record artifact is not a regular file: {candidate.name}"
            )
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get("verification") != "VERIFIED":
        raise StrategyHistoryError("Robuxio track-record generation is not verified")
    if str(manifest.get("generation_id")) != generation_root.name:
        raise StrategyHistoryError(
            "Robuxio track-record generation identity differs from its directory"
        )
    artifacts = {
        str(row.get("artifact_id")): row for row in manifest.get("artifacts") or []
    }
    returns_bytes = returns_path.read_bytes()
    artifact = artifacts.get("returns.csv")
    if (
        not isinstance(artifact, dict)
        or artifact.get("sha256") != hashlib.sha256(returns_bytes).hexdigest()
        or artifact.get("size_bytes") != len(returns_bytes)
    ):
        raise StrategyHistoryError(
            "Robuxio track-record returns differ from the generation manifest"
        )

    rows: list[dict[str, str]] = []
    reader = csv.DictReader(io.StringIO(returns_bytes.decode("utf-8")))
    for row in reader:
        if row.get("evidence_layer") != "PROVIDER_TRACK_RECORD":
            raise StrategyHistoryError(
                "Robuxio track-record carries an unexpected evidence layer"
            )
        observed = _date_value(row.get("date"), "Robuxio track-record date")
        value = _decimal(row.get("return"), "Robuxio track-record return")
        if value <= -1:
            raise StrategyHistoryError("Robuxio track-record return is invalid")
        rows.append(
            {
                "series_id": "Robuxio Crypto (High Sharpe High Vol)",
                "date": observed.isoformat(),
                "return": format(value.normalize(), "f"),
                "evidence_layer": "BACKTEST",
                "strategy_version": strategy_version,
            }
        )
    if len(rows) < 2:
        raise StrategyHistoryError("Robuxio track-record history is insufficient")
    dates = [row["date"] for row in rows]
    if dates != sorted(set(dates)):
        raise StrategyHistoryError("Robuxio track-record calendar is not ordered")
    definition: dict[str, object] = {
        "compatibility_basis": "OWNER_LAYER_RULING_SITE_SERIES_IS_PRODUCT_DESIGN_HISTORY",
        "coverage_disposition": "COMPLETE_WITHIN_RETAINED_CAPTURE_BOUNDARY",
        "frequency": "DAILY_CONTINUOUS",
        "history_status": "ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
        "implementation_reconciliation_status": (
            "MODEL_TO_ACCOUNT_RECONCILIATION_RETAINED_SEPARATELY"
        ),
        "method": "PROVIDER_PUBLISHED_CUMULATIVE_RETURN_TO_DAILY_RETURN",
        "observed_end": dates[-1],
        "observed_start": dates[0],
        "required_for_analysis": True,
        "return_observations": len(rows),
        "series_id": "Robuxio Crypto (High Sharpe High Vol)",
        "source_artifacts": [
            {
                "artifact_id": "returns.csv",
                "sha256": artifact["sha256"],
                "size_bytes": artifact["size_bytes"],
                "source_id": "robuxio-published-track-record-generation",
            }
        ],
        "strategy_version": strategy_version,
    }
    return rows, definition


def _p123_eu_designer_curve(
    generation_root: Path, strategy_version: str
) -> tuple[list[dict[str, str]], dict[str, object]]:
    """Read the governed designer-model generation as the EU design history.

    The generation retains the provider's own published curve for the model the
    owner subscribes to, split at the launch the provider states on its own
    page, with a manifest digest over the returns. Admitting it is a digest
    check plus a relabel of the pre-launch segment -- no return is recomputed.

    The model publishes in EUR and the owner holds it in a USD account, and the
    two series differ by about one percent a day over their overlap, which is
    implementation and currency together. That is why this is design history in
    its own layer and not a longer version of the owner's series.
    """

    manifest_path = generation_root / "generation.json"
    returns_path = generation_root / "returns.csv"
    catalog_path = generation_root / "catalog.json"
    for candidate in (manifest_path, returns_path, catalog_path):
        if candidate.is_symlink() or not candidate.is_file():
            raise StrategyHistoryError(
                f"EU designer-model artifact is not a regular file: {candidate.name}"
            )
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get("verification") != "VERIFIED":
        raise StrategyHistoryError("EU designer-model generation is not verified")
    if str(manifest.get("generation_id")) != generation_root.name:
        raise StrategyHistoryError(
            "EU designer-model generation identity differs from its directory"
        )
    artifacts = {
        str(row.get("artifact_id")): row for row in manifest.get("artifacts") or []
    }
    returns_bytes = returns_path.read_bytes()
    artifact = artifacts.get("returns.csv")
    if (
        not isinstance(artifact, dict)
        or artifact.get("sha256") != hashlib.sha256(returns_bytes).hexdigest()
        or artifact.get("size_bytes") != len(returns_bytes)
    ):
        raise StrategyHistoryError(
            "EU designer-model returns differ from the generation manifest"
        )
    catalog = json.loads(catalog_path.read_bytes())
    series = catalog.get("series")
    if not isinstance(series, dict):
        raise StrategyHistoryError("EU designer-model catalog series differs")

    rows: list[dict[str, str]] = []
    model_live = 0
    reader = csv.DictReader(io.StringIO(returns_bytes.decode("utf-8")))
    for row in reader:
        layer = row.get("evidence_layer")
        if layer == "PROVIDER_MODEL_LIVE":
            model_live += 1
            continue
        if layer != "BACKTEST":
            raise StrategyHistoryError(
                "EU designer-model carries an unexpected evidence layer"
            )
        observed = _date_value(row.get("date"), "EU designer-model date")
        value = _decimal(row.get("return"), "EU designer-model return")
        if value <= -1:
            raise StrategyHistoryError("EU designer-model return is invalid")
        rows.append(
            {
                "series_id": "EU [P123]",
                "date": observed.isoformat(),
                "return": format(value.normalize(), "f"),
                "evidence_layer": "BACKTEST",
                "strategy_version": strategy_version,
            }
        )
    if len(rows) < 2:
        raise StrategyHistoryError("EU designer-model history is insufficient")
    dates = [row["date"] for row in rows]
    if dates != sorted(set(dates)):
        raise StrategyHistoryError("EU designer-model calendar is not ordered")
    provider_launch = str(series.get("provider_launch") or "")
    if not provider_launch or dates[-1] >= provider_launch:
        raise StrategyHistoryError(
            "EU designer-model prehistory reaches the provider launch"
        )
    definition: dict[str, object] = {
        "compatibility_basis": "OWNER_LAYER_RULING_DESIGNER_MODEL_IS_PRODUCT_DESIGN_HISTORY",
        "coverage_disposition": "COMPLETE_STRICTLY_BEFORE_PROVIDER_LAUNCH",
        "frequency": "DAILY_TRADING",
        "history_status": "ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
        "implementation_reconciliation_status": (
            "MODEL_TO_ACCOUNT_RECONCILIATION_RETAINED_SEPARATELY"
        ),
        "method": "PROVIDER_PUBLISHED_MODEL_EQUITY_CURVE_TO_DAILY_RETURN",
        "observed_end": dates[-1],
        "observed_start": dates[0],
        "provider_currency": str(series.get("currency") or ""),
        "provider_launch": provider_launch,
        "provider_model_live_observations_not_admitted": model_live,
        "provider_revision_dates": list(series.get("provider_revision_dates") or []),
        "required_for_analysis": True,
        "return_observations": len(rows),
        "series_id": "EU [P123]",
        "source_artifacts": [
            {
                "artifact_id": "returns.csv",
                "sha256": artifact["sha256"],
                "size_bytes": artifact["size_bytes"],
                "source_id": "portfolio123-designer-model-curve-generation",
            }
        ],
        "strategy_version": strategy_version,
    }
    return rows, definition


def _p123_eu_backtest(
    ordered: Sequence[tuple[date, tuple[Decimal, Decimal]]],
    *,
    generation_id: str,
    exact_ids: Iterable[str],
    strategy_name: str,
    equity_artifact: Mapping[str, object],
) -> tuple[list[dict[str, str]], dict[str, object]] | None:
    """Admit the EU prehistory strictly before the provider's own launch.

    The provider publishes one continuous equity curve from 1999. Only the part
    after its launch is a live track record; the rest is the design simulated
    before the model existed, which is exactly what LIVE_PLUS_BACKTEST admits
    and LIVE_ONLY must not see. The boundary is the last observation strictly
    before the published launch date, so a launch that falls on a non-trading
    day cannot pull a live session into the prehistory.

    Returns are level ratios. The one funding event in the path is the
    simulation's opening capital on its first day, which no ratio spans, so no
    flow adjustment applies.
    """

    prehistory = [item for item in ordered if item[0] < P123_EU_PROVIDER_LAUNCH]
    if len(prehistory) < 2:
        return None
    version = _version(
        "p123-eu-connected-model",
        {
            "generation_id": generation_id,
            "provider_entity_ids": sorted(exact_ids),
            "strategy_name": strategy_name,
        },
    )
    rows: list[dict[str, str]] = []
    for (_previous_day, previous), (observed, current) in zip(
        prehistory, prehistory[1:], strict=False
    ):
        period_return = current[0] / previous[0] - Decimal(1)
        if period_return <= -1:
            raise StrategyHistoryError("Portfolio123 exact EU backtest return is invalid")
        rows.append(
            {
                "series_id": "EU [P123]",
                "date": observed.isoformat(),
                "return": format(period_return.normalize(), "f"),
                "evidence_layer": "BACKTEST",
                "strategy_version": version,
            }
        )
    definition: dict[str, object] = {
        "compatibility_basis": "EXACT_CONNECTED_MODEL_EQUITY_CURVE_IDENTITY",
        "coverage_disposition": "COMPLETE_STRICTLY_BEFORE_PROVIDER_LAUNCH",
        "frequency": "DAILY_TRADING",
        "history_status": "ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
        "implementation_reconciliation_status": (
            "MODEL_TO_ACCOUNT_RECONCILIATION_RETAINED_SEPARATELY"
        ),
        "method": "PROVIDER_EQUITY_CURVE_LEVEL_TO_RETURN",
        "observed_end": rows[-1]["date"],
        "observed_start": rows[0]["date"],
        "provider_launch": P123_EU_PROVIDER_LAUNCH.isoformat(),
        "required_for_analysis": True,
        "return_observations": len(rows),
        "series_id": "EU [P123]",
        "source_artifacts": [
            {
                "artifact_id": "tables/har_equity_curve.parquet",
                "sha256": equity_artifact["sha256"],
                "size_bytes": equity_artifact["size_bytes"],
                "source_id": "portfolio123-exact-connected-eu-equity-curve",
            }
        ],
        "source_rows_excluded_at_or_after_provider_launch": len(ordered) - len(prehistory),
        "strategy_version": version,
    }
    return rows, definition


def _grizzly_trade_history(
    path: Path, source_id: str
) -> tuple[list[dict[str, Any]], dict[str, object]]:
    source = _source_identity(path, source_id)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {
                "Date (ET)",
                "Action",
                "SPX",
                "Prev Price",
                "Profit / Loss",
                "PnL %",
                "Model",
                "Buy & Hold",
            }
            if not required <= set(reader.fieldnames or ()):
                raise StrategyHistoryError("Grizzly trade-history headers differ")
            raw_rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise StrategyHistoryError("Grizzly trade history is invalid") from exc
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        timestamp = str(raw.get("Date (ET)") or "").strip()
        if timestamp.endswith(" ET"):
            timestamp = timestamp[:-3]
        try:
            observed = datetime.strptime(timestamp, "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise StrategyHistoryError(
                "Grizzly trade-history timestamp is invalid"
            ) from exc
        action = str(raw.get("Action") or "").strip().upper()
        if action not in {"BUY", "REBUY", "SELL"}:
            raise StrategyHistoryError("Grizzly trade-history action differs")
        model = _decimal(raw.get("Model"), "Grizzly model level")
        spx = _decimal(raw.get("SPX"), "Grizzly SPX level")
        if model <= 0 or spx <= 0:
            raise StrategyHistoryError("Grizzly trade-history level is invalid")
        pnl_text = str(raw.get("PnL %") or "").strip()
        rows.append(
            {
                "action": action,
                "buy_and_hold": _decimal(
                    raw.get("Buy & Hold"), "Grizzly buy-and-hold level"
                ),
                "model": model,
                "observed": observed,
                "pnl_fraction": (
                    _decimal(pnl_text, "Grizzly PnL percent") / Decimal(100)
                    if pnl_text
                    else None
                ),
                "spx": spx,
            }
        )
    if len(rows) < 100 or rows != sorted(rows, key=lambda item: item["observed"]):
        raise StrategyHistoryError(
            "Grizzly trade history is unexpectedly short or unsorted"
        )
    if len({item["observed"] for item in rows}) != len(rows):
        raise StrategyHistoryError("Grizzly trade-history timestamps overlap")
    return rows, source


def _grizzly_manual_histories(
    blended_path: Path, live_path: Path
) -> tuple[list[dict[str, Any]], dict[str, object], list[dict[str, object]]]:
    blended, blended_source = _grizzly_trade_history(
        blended_path, "grizzlybulls-all-backtest-live-trade-history"
    )
    live, live_source = _grizzly_trade_history(
        live_path, "grizzlybulls-live-only-trade-history"
    )
    by_timestamp = {item["observed"]: item for item in blended}
    exact_overlap = [
        item for item in live if by_timestamp.get(item["observed"]) == item
    ]
    missing = [item for item in live if item["observed"] not in by_timestamp]
    differing = [
        item
        for item in live
        if item["observed"] in by_timestamp and by_timestamp[item["observed"]] != item
    ]
    if (
        differing
        or len(missing) > 1
        or (missing and (missing[0] != live[0] or missing[0]["action"] != "BUY"))
        or live[-1]["observed"] != blended[-1]["observed"]
        or len(exact_overlap) != len(live) - len(missing)
    ):
        raise StrategyHistoryError(
            "Grizzly all-history and live-only files are inconsistent"
        )
    sell_rows = [item for item in blended if item["action"] == "SELL"]
    if len(sell_rows) < 50 or any(item["pnl_fraction"] is None for item in sell_rows):
        raise StrategyHistoryError(
            "Grizzly realized model-return evidence is incomplete"
        )
    validation = {
        "all_history_observations": len(blended),
        "all_history_observed_start": blended[0]["observed"].isoformat(),
        "exact_live_overlap_observations": len(exact_overlap),
        "last_trade_timestamp": blended[-1]["observed"].isoformat(),
        "live_initialization_rows_not_repeated_in_all_history": len(missing),
        "live_only_observations": len(live),
        "live_only_observed_start": live[0]["observed"].isoformat(),
        "realized_model_return_observations": len(sell_rows),
        "status": "CONSISTENT_EXCEPT_LIVE_INITIALIZATION_ROW",
    }
    return blended, validation, [blended_source, live_source]


def _yahoo_price_extension(
    path: Path, source_id: str
) -> tuple[dict[str, dict[date, Decimal]], dict[str, object]]:
    source = _source_identity(path, source_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrategyHistoryError("Yahoo price extension is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("provider") != "yahoo"
        or payload.get("transport") != "yfinance"
        or payload.get("dataset") != "history"
    ):
        raise StrategyHistoryError("Yahoo price-extension authority differs")
    prices: dict[str, dict[date, Decimal]] = {}
    for raw in payload.get("rows", []):
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("Ticker") or "").strip().upper()
        raw_date = raw.get("Date")
        raw_price = raw.get("Adj Close") or raw.get("Close")
        if not ticker or raw_date is None or raw_price is None:
            continue
        observed = _excel_date(str(raw_date)[:10], f"{ticker} extension date")
        accepted = _decimal(raw_price, f"{ticker} adjusted close")
        if accepted <= 0:
            raise StrategyHistoryError("Yahoo price extension contains invalid prices")
        prices.setdefault(ticker, {})[observed] = accepted
    if not prices:
        raise StrategyHistoryError("Yahoo price extension is empty")
    return prices, source


def _grizzly_proxy_extension(
    *,
    exact: Sequence[tuple[date, Decimal]],
    manual: Sequence[Mapping[str, Any]],
    price_root: Path,
    price_extension_json: Path,
    as_of_date: date,
) -> tuple[list[tuple[date, Decimal]], dict[str, object], list[dict[str, object]]]:
    price_source = _source_identity(
        price_root / "SPXL.csv", "grizzlybears-spxl-retained-price-history"
    )
    with (price_root / "SPXL.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        reader = csv.DictReader(handle)
        if not {"Date", "Close"} <= set(reader.fieldnames or ()):
            raise StrategyHistoryError("Grizzly SPXL price-history headers differ")
        prices = {
            _excel_date(row["Date"], "Grizzly SPXL price date"): _decimal(
                row["Close"], "Grizzly SPXL close"
            )
            for row in reader
            if row.get("Date") and row.get("Close")
        }
    extension, extension_source = _yahoo_price_extension(
        price_extension_json, "grizzlybears-spxl-yahoo-price-extension"
    )
    prices.update(extension.get("SPXL", {}))
    calendar = sorted(observed for observed in prices if observed <= as_of_date)
    if not calendar:
        raise StrategyHistoryError("Grizzly SPXL trading calendar is empty")
    actions = sorted(
        (
            item["observed"],
            str(item["action"]),
        )
        for item in manual
        if isinstance(item.get("observed"), datetime)
    )
    invested = False
    for observed, action in actions:
        if observed.date() >= calendar[0]:
            break
        invested = action != "SELL"
    effective: dict[date, list[tuple[datetime, str]]] = {}
    for observed, action in actions:
        raw_day = observed.date() + (
            timedelta(days=1) if observed.hour >= 16 else timedelta(0)
        )
        matches = [item for item in calendar if item >= raw_day]
        if matches:
            effective.setdefault(matches[0], []).append((observed, action))
    states: dict[date, bool] = {}
    for observed in calendar:
        for _, action in sorted(effective.get(observed, [])):
            invested = action != "SELL"
        states[observed] = invested
    proxy: dict[date, Decimal] = {}
    for previous, observed in zip(calendar, calendar[1:], strict=False):
        proxy[observed] = (
            prices[observed] / prices[previous] - Decimal(1)
            if states[previous]
            else Decimal(0)
        )
    exact_map = dict(exact)
    overlap = sorted(set(exact_map) & set(proxy))
    if len(overlap) < 250:
        raise StrategyHistoryError("Grizzly signal-price overlap is insufficient")
    predicted = [float(proxy[item]) for item in overlap]
    actual = [float(exact_map[item]) for item in overlap]
    correlation = _correlation(predicted, actual)
    if correlation < 0.85:
        raise StrategyHistoryError("Grizzly signal-price overlap validation failed")
    exact_end = max(exact_map)
    extended = [
        (observed, proxy[observed])
        for observed in calendar
        if exact_end < observed <= as_of_date and observed in proxy
    ]
    if not extended or extended[-1][0] != max(calendar):
        raise StrategyHistoryError("Grizzly signal-price extension is incomplete")
    errors = [
        predicted_value - actual_value
        for predicted_value, actual_value in zip(predicted, actual, strict=True)
    ]
    validation = {
        "exact_provider_daily_observed_end": exact_end.isoformat(),
        "extension_observed_end": extended[-1][0].isoformat(),
        "extension_observed_start": extended[0][0].isoformat(),
        "extension_return_observations": len(extended),
        "overlap_correlation": correlation,
        "overlap_mae": statistics.fmean(map(abs, errors)),
        "overlap_observations": len(overlap),
        "status": "VALIDATED_SIGNAL_STATE_TIMES_SPXL_EXTENSION",
    }
    return extended, validation, [price_source, extension_source]


def _grizzly(
    path: Path,
    *,
    original_design_workbook: Path | None = None,
    spx_workbook: Path | None = None,
    spy_workbook: Path | None = None,
    blended_trade_history: Path | None = None,
    live_trade_history: Path | None = None,
    price_root: Path | None = None,
    price_extension_json: Path | None = None,
    as_of_date: date | None = None,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    source = _source_identity(path, "grizzlybulls-current-model-workbook")
    lineage_inputs = (original_design_workbook, spx_workbook, spy_workbook)
    if any(item is not None for item in lineage_inputs) and any(
        item is None for item in lineage_inputs
    ):
        raise StrategyHistoryError(
            "Grizzly lineage requires original futures, SPX, and SPY workbooks"
        )
    implementation_lineage: dict[str, object] | None = None
    lineage_sources: list[dict[str, object]] = []
    if all(item is not None for item in lineage_inputs):
        assert original_design_workbook is not None
        assert spx_workbook is not None
        assert spy_workbook is not None
        implementation_lineage, lineage_sources = _grizzly_implementation_lineage(
            original_design_workbook=original_design_workbook,
            spx_workbook=spx_workbook,
            spy_workbook=spy_workbook,
            spxl_workbook=path,
        )
    rows = _xlsx_rows(path, "Simulation")
    if not rows:
        raise StrategyHistoryError("Grizzly simulation sheet is empty")
    headers = {str(value).strip(): index for index, value in enumerate(rows[0])}
    if not {"date", "day_ret"} <= set(headers):
        raise StrategyHistoryError("Grizzly simulation headers differ")
    values: list[tuple[date, Decimal]] = []
    for row in rows[1:]:
        raw_date = _cell(row, headers["date"])
        raw_return = _cell(row, headers["day_ret"])
        if raw_date is None or raw_return is None:
            continue
        values.append(
            (
                _excel_date(raw_date, "Grizzly date"),
                _decimal(raw_return, "Grizzly day_ret"),
            )
        )
    if len(values) < 1_000:
        raise StrategyHistoryError("Grizzly retained simulation is unexpectedly short")
    extra_sources: list[dict[str, object]] = []
    manual_validation: dict[str, object] | None = None
    extension_validation: dict[str, object] | None = None
    requested = (
        blended_trade_history,
        live_trade_history,
        price_root,
        price_extension_json,
        as_of_date,
    )
    if any(item is not None for item in requested):
        if any(item is None for item in requested):
            raise StrategyHistoryError(
                "Grizzly manual extension requires both trade files, prices, and as-of date"
            )
        assert blended_trade_history is not None
        assert live_trade_history is not None
        assert price_root is not None
        assert price_extension_json is not None
        assert as_of_date is not None
        manual, manual_validation, manual_sources = _grizzly_manual_histories(
            blended_trade_history, live_trade_history
        )
        extended, extension_validation, price_sources = _grizzly_proxy_extension(
            exact=values,
            manual=manual,
            price_root=price_root,
            price_extension_json=price_extension_json,
            as_of_date=as_of_date,
        )
        values.extend(extended)
        extra_sources.extend([*manual_sources, *price_sources])
    version = _version(
        "grizzlybears-vtma-current-model",
        {
            "method": "Simulation.date/day_ret plus signal-state SPXL extension",
            "source_sha256": [
                source["sha256"],
                *[item["sha256"] for item in lineage_sources],
                *[item["sha256"] for item in extra_sources],
            ],
            "strategy_definition": (
                "VIX-TA-Macro Advanced: original futures design; "
                "current live SPXL implementation"
                if implementation_lineage is not None
                else "VIX-TA-Macro Advanced using SPXL"
            ),
        },
    )
    accepted = _return_rows("GrizzlyBears VTMA", version, values)
    return accepted, _definition(
        series_id="GrizzlyBears VTMA",
        version=version,
        rows=accepted,
        history_status=(
            "ACCEPTED_EXACT_VERSION_PLUS_GOVERNED_SIGNAL_PRICE_EXTENSION"
            if extra_sources
            else "ACCEPTED_EXACT_VERSION_HISTORY"
        ),
        compatibility_basis=(
            "FUTURES_DESIGN_WITH_SPX_SPY_SPXL_RESEARCH_VARIANTS_PLUS_"
            "OFFICIAL_SIGNAL_HISTORY_AND_LIVE_SPXL_ASSIGNMENT"
            if implementation_lineage is not None and extra_sources
            else "FUTURES_DESIGN_WITH_SPX_SPY_SPXL_RESEARCH_VARIANTS_AND_"
            "LIVE_SPXL_ASSIGNMENT"
            if implementation_lineage is not None
            else "CURRENT_MODEL_WORKBOOK_PLUS_OFFICIAL_SIGNAL_HISTORY_AND_"
            "LIVE_SPXL_ASSIGNMENT"
            if extra_sources
            else "CURRENT_MODEL_WORKBOOK_AND_LIVE_STRATEGY_ASSIGNMENT"
        ),
        frequency="DAILY_TRADING",
        implementation_status="MODEL_TO_ACCOUNT_RECONCILIATION_RETAINED_SEPARATELY",
        method=(
            "CACHED_SIMULATION_DAY_RET_THEN_SIGNAL_STATE_TIMES_SPXL_ADJUSTED_CLOSE"
            if extra_sources
            else "CACHED_SIMULATION_DAY_RET"
        ),
        source=source,
    ) | {
        "source_artifacts": [source, *lineage_sources, *extra_sources],
        **(
            {"implementation_lineage": implementation_lineage}
            if implementation_lineage is not None
            else {}
        ),
        **(
            {"manual_trade_history_validation": manual_validation}
            if manual_validation is not None
            else {}
        ),
        **(
            {"signal_price_extension_validation": extension_validation}
            if extension_validation is not None
            else {}
        ),
    }


def _tt(
    path: Path, *, as_of_date: date | None = None
) -> tuple[list[dict[str, str]], dict[str, object]]:
    source = _source_identity(path, "turingtrader-current-api-workbook")
    index_rows = _xlsx_rows(path, "Index")
    components: list[tuple[str, Decimal]] = []
    for row in index_rows[1:]:
        slug = str(_cell(row, 4) or "").strip()
        period = _cell(row, 5)
        raw_weight = _cell(row, 6)
        if not slug or raw_weight is None or period != 2:
            continue
        components.append((slug, _decimal(raw_weight, "TT component weight")))
    if len(components) != 6 or abs(
        sum((weight for _, weight in components), Decimal(0)) - Decimal(1)
    ) > Decimal("0.000000001"):
        raise StrategyHistoryError("TT current portfolio must contain six unit weights")

    raw = _xlsx_rows(path, "raw_ttdata")
    if not raw:
        raise StrategyHistoryError("TT return sheet is empty")
    component_columns: dict[str, int] = {}
    for index, value in enumerate(raw[0]):
        match = _SLUG.search(str(value or ""))
        if match and match.group(1) not in component_columns:
            component_columns[match.group(1)] = index
    if set(component_columns) != {slug for slug, _ in components}:
        missing = sorted({slug for slug, _ in components} - set(component_columns))
        raise StrategyHistoryError(
            f"TT current component histories are missing: {missing}"
        )
    previous: dict[str, Decimal] = {}
    values: list[tuple[date, Decimal]] = []
    for row in raw[1:]:
        raw_date = _cell(row, 0)
        if raw_date is None:
            continue
        levels: dict[str, Decimal] = {}
        for slug, _ in components:
            raw_level = _cell(row, component_columns[slug])
            if raw_level is None:
                break
            level = _decimal(raw_level, f"TT {slug} index")
            if level <= 0:
                raise StrategyHistoryError(f"TT {slug} index must be positive")
            levels[slug] = level
        if len(levels) != len(components):
            continue
        if set(previous) == set(levels):
            portfolio_return = sum(
                (
                    weight * (levels[slug] / previous[slug] - Decimal(1))
                    for slug, weight in components
                ),
                start=Decimal(0),
            )
            values.append((_excel_date(raw_date, "TT date"), portfolio_return))
        previous = levels
    # This guard exists to catch a truncated or broken workbook, so it reads
    # the source history rather than the admitted window.
    if len(values) < 1_000:
        raise StrategyHistoryError("TT current-six history is unexpectedly short")
    if as_of_date is not None:
        # The TT sheet is a live API export and runs to the day it was pulled.
        # Rows after the declared as-of date are provenance, not admitted
        # returns, exactly as the QSI reader already treats them; emitting them
        # made the bundle's own post-as-of guard reject the whole build.
        values = [item for item in values if item[0] <= as_of_date]
    definition = {
        "components": [
            {"allocation": str(weight), "slug": slug} for slug, weight in components
        ],
        "method": "DAILY_FIXED_WEIGHT_COMPONENT_RETURN_SUM",
        "source_sha256": source["sha256"],
    }
    version = _version("tt-current-six-equal-weight", definition)
    accepted = _return_rows("TT Portfolio", version, values)
    return accepted, _definition(
        series_id="TT Portfolio",
        version=version,
        rows=accepted,
        history_status="ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
        compatibility_basis="CURRENT_SIX_COMPONENT_SLUGS_AND_PUBLISHED_EQUAL_WEIGHTS",
        frequency="DAILY_TRADING",
        implementation_status="MODEL_TO_ACCOUNT_RECONCILIATION_RETAINED_SEPARATELY",
        method="DAILY_FIXED_WEIGHT_COMPONENT_RETURN_SUM",
        source=source,
    )


def _qsi_archive_weeks(
    path: Path,
) -> tuple[list[dict[str, Any]], dict[str, object], dict[str, object]]:
    archive_source = _source_identity(path, "qsi-paid-subscription-archive")
    try:
        with ZipFile(path) as archive:
            matches = [
                name
                for name in archive.namelist()
                if name.endswith("Stratum_vs_SPY_Weekly_Returns 2013 to 2024.xlsx")
            ]
            if len(matches) != 1:
                raise StrategyHistoryError(
                    "QSI subscription archive must contain one weekly-return workbook"
                )
            member_name = matches[0]
            content = archive.read(member_name)
    except (OSError, BadZipFile, KeyError) as exc:
        raise StrategyHistoryError("QSI subscription archive is invalid") from exc
    member_source = _content_identity(
        content,
        "qsi-2013-2024-exact-weekly-model-path",
        member_name,
    )
    rows = _xlsx_rows_content(content, "Sheet 1")
    if not rows:
        raise StrategyHistoryError("QSI embedded weekly-return workbook is empty")
    headers = {str(value or "").strip(): index for index, value in enumerate(rows[0])}
    required = {
        "Week Start Date",
        "Week End Date",
        "Stratum Returns (With Slippage)",
        "Stratum Returns (No Slippage)",
        "Stratum Label",
        "Weight Allocations",
    }
    if not required <= set(headers):
        raise StrategyHistoryError("QSI embedded weekly-return headers differ")
    weeks: list[dict[str, Any]] = []
    for row in rows[1:]:
        raw_start = _cell(row, headers["Week Start Date"])
        raw_end = _cell(row, headers["Week End Date"])
        raw_target = _cell(row, headers["Stratum Returns (With Slippage)"])
        raw_validation = _cell(row, headers["Stratum Returns (No Slippage)"])
        if None in {raw_start, raw_end, raw_target, raw_validation}:
            continue
        labels = (
            str(_cell(row, headers["Stratum Label"]) or "").replace("/", " ").split()
        )
        allocations = [
            _decimal(item.strip(), "QSI model allocation")
            for item in str(_cell(row, headers["Weight Allocations"]) or "").split("/")
            if item.strip()
        ]
        if not labels or len(labels) != len(allocations):
            raise StrategyHistoryError("QSI model labels and allocations differ")
        weights: dict[str, Decimal] = {}
        for ticker, allocation in zip(labels, allocations, strict=True):
            weights[ticker] = weights.get(ticker, Decimal(0)) + allocation
        weeks.append(
            {
                "end": _excel_date(raw_end, "QSI backtest week end"),
                "exact_return": _decimal(raw_target, "QSI with-slippage return"),
                "price_validation_return": _decimal(
                    raw_validation, "QSI no-slippage return"
                ),
                "start": _excel_date(raw_start, "QSI backtest week start"),
                "weights": weights,
            }
        )
    if len(weeks) < 500:
        raise StrategyHistoryError("QSI embedded weekly-return history is too short")
    return weeks, archive_source, member_source


def _qsi_current_archive_weeks(
    path: Path,
) -> tuple[list[dict[str, Any]], dict[str, object], dict[str, object]]:
    archive_source = _source_identity(path, "qsi-current-paid-subscription-archive")
    try:
        with ZipFile(path) as archive:
            matches = [
                name
                for name in archive.namelist()
                if name.endswith("Weekly_File_Drops/stratum_data_combined.xlsx")
            ]
            if len(matches) != 1:
                raise StrategyHistoryError(
                    "QSI current archive must contain one combined weekly-data workbook"
                )
            member_name = matches[0]
            content = archive.read(member_name)
    except (OSError, BadZipFile, KeyError) as exc:
        raise StrategyHistoryError(
            "QSI current subscription archive is invalid"
        ) from exc
    member_source = _content_identity(
        content,
        "qsi-current-exact-weekly-model-path",
        member_name,
    )
    rows = _xlsx_rows_content(content, "Stratum Data Combined")
    if not rows:
        raise StrategyHistoryError("QSI current combined weekly-data workbook is empty")
    headers = {str(value or "").strip(): index for index, value in enumerate(rows[0])}
    required = {
        "Week Ending Date",
        "Stratum 3x Returns",
        "Stratum 3x Returns (with slippage)",
        "Stratum 3x Label",
        "allocation_reference",
    }
    if not required <= set(headers):
        raise StrategyHistoryError("QSI current weekly-data headers differ")
    weeks: list[dict[str, Any]] = []
    for row in rows[1:]:
        raw_end = _cell(row, headers["Week Ending Date"])
        raw_exact = _cell(row, headers["Stratum 3x Returns (with slippage)"])
        raw_validation = _cell(row, headers["Stratum 3x Returns"])
        if None in {raw_end, raw_exact, raw_validation}:
            continue
        labels = (
            str(_cell(row, headers["Stratum 3x Label"]) or "").replace("/", " ").split()
        )
        allocations = [
            _decimal(item.strip(), "QSI current model allocation")
            for item in str(_cell(row, headers["allocation_reference"]) or "").split(
                "/"
            )
            if item.strip()
        ]
        if not labels or len(labels) != len(allocations):
            raise StrategyHistoryError(
                "QSI current model labels and allocations differ"
            )
        weights: dict[str, Decimal] = {}
        for ticker, allocation in zip(labels, allocations, strict=True):
            weights[ticker] = weights.get(ticker, Decimal(0)) + allocation
        end = _excel_date(raw_end, "QSI current week end")
        weeks.append(
            {
                "end": end,
                "exact_return": _decimal(raw_exact, "QSI current slippage return"),
                "price_validation_return": _decimal(
                    raw_validation, "QSI current no-slippage return"
                ),
                "start": end - timedelta(days=4),
                "weights": weights,
            }
        )
    if len(weeks) < 26:
        raise StrategyHistoryError("QSI current exact weekly history is insufficient")
    ends = [item["end"] for item in weeks]
    if len(set(ends)) != len(ends):
        raise StrategyHistoryError("QSI current weekly history overlaps itself")
    return weeks, archive_source, member_source


def _qsi_revision_validation(
    prior: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    label: str,
) -> dict[str, object]:
    prior_by_end = {item["end"]: item for item in prior}
    current_by_end = {item["end"]: item for item in current}
    overlap = sorted(set(prior_by_end) & set(current_by_end))
    if not overlap:
        raise StrategyHistoryError(f"QSI {label} overlap is unavailable")
    return_differences: list[float] = []
    weight_differences: list[float] = []
    for observed in overlap:
        prior_row = prior_by_end[observed]
        current_row = current_by_end[observed]
        prior_return = prior_row["exact_return"]
        current_return = current_row["exact_return"]
        prior_weights = prior_row["weights"]
        current_weights = current_row["weights"]
        if (
            not isinstance(prior_return, Decimal)
            or not isinstance(current_return, Decimal)
            or not isinstance(prior_weights, dict)
            or not isinstance(current_weights, dict)
        ):
            raise StrategyHistoryError(f"QSI {label} overlap evidence is invalid")
        return_differences.append(float(current_return - prior_return))
        symbols = set(prior_weights) | set(current_weights)
        weight_differences.append(
            max(
                (
                    abs(
                        float(
                            current_weights.get(symbol, Decimal(0))
                            - prior_weights.get(symbol, Decimal(0))
                        )
                    )
                    for symbol in symbols
                ),
                default=0.0,
            )
        )
    return {
        "accepted_authority": "NEWER_CURRENT_ARCHIVE",
        "exact_weight_matches": sum(value < 1e-12 for value in weight_differences),
        "max_abs_return_revision": max(map(abs, return_differences)),
        "max_abs_weight_revision": max(weight_differences),
        "mean_abs_return_revision": statistics.fmean(map(abs, return_differences)),
        "observed_end": overlap[-1].isoformat(),
        "observed_start": overlap[0].isoformat(),
        "overlap_observations": len(overlap),
        "status": "OVERLAP_VALIDATED_NEWER_AUTHORITY_ACCEPTED",
    }


def _qsi_appendix_weeks(
    path: Path,
) -> tuple[list[dict[str, Any]], dict[str, object]]:
    source = _source_identity(path, "qsi-current-3x-appendix")
    rows = _xlsx_rows(path, "Stratum 3x")
    if not rows:
        raise StrategyHistoryError("QSI 3x history sheet is empty")
    header = rows[0]
    metric = next(
        (row for row in rows if str(_cell(row, 0) or "") == "Stratum 3x Return"),
        None,
    )
    if metric is None:
        raise StrategyHistoryError("QSI 3x return row is missing")
    ticker_rows = [
        row
        for row in rows
        if str(_cell(row, 0) or "").strip() not in {"Fund Ticker", "Stratum 3x Return"}
        and str(_cell(row, 2) or "").strip() != "Metric"
    ]
    weeks: list[dict[str, Any]] = []
    for index in range(4, max(len(header), len(metric))):
        raw_date = _cell(header, index)
        raw_return = _cell(metric, index)
        if raw_date is None or raw_return is None:
            continue
        weights = {
            str(_cell(row, 0)).strip(): _decimal(
                _cell(row, index) or 0, "QSI current allocation"
            )
            for row in ticker_rows
            if _decimal(_cell(row, index) or 0, "QSI current allocation") != 0
        }
        end = _excel_date(raw_date, "QSI current week end")
        accepted_return = _decimal(raw_return, "QSI current 3x return")
        weeks.append(
            {
                "end": end,
                "exact_return": accepted_return,
                "price_validation_return": accepted_return,
                "start": end - timedelta(days=4),
                "weights": weights,
            }
        )
    if len(weeks) < 26:
        raise StrategyHistoryError("QSI retained current history is insufficient")
    return weeks, source


def _qsi_prices(
    root: Path,
    tickers: Sequence[str],
    extension_json: Path | None = None,
) -> tuple[dict[str, dict[date, Decimal]], list[dict[str, object]]]:
    prices: dict[str, dict[date, Decimal]] = {}
    sources: list[dict[str, object]] = []
    for ticker in sorted(set(tickers)):
        path = root / f"{ticker}.csv"
        source = _source_identity(path, f"qsi-price-history-{ticker.lower()}")
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if not {"Date", "Close"} <= set(reader.fieldnames or ()):
                    raise StrategyHistoryError(
                        f"QSI price history headers differ: {ticker}"
                    )
                values = {
                    _excel_date(row["Date"], f"QSI {ticker} price date"): _decimal(
                        row["Close"], f"QSI {ticker} close"
                    )
                    for row in reader
                    if row.get("Date") and row.get("Close")
                }
        except (OSError, UnicodeDecodeError) as exc:
            raise StrategyHistoryError(
                f"QSI price history is not readable: {ticker}"
            ) from exc
        if not values or any(value <= 0 for value in values.values()):
            raise StrategyHistoryError(f"QSI price history is invalid: {ticker}")
        prices[ticker] = values
        sources.append(source)
    if extension_json is not None:
        extension, extension_source = _yahoo_price_extension(
            extension_json, "qsi-yahoo-price-extension"
        )
        missing = sorted(set(tickers) - set(extension))
        if missing:
            raise StrategyHistoryError(
                f"QSI price extension lacks requested tickers: {missing}"
            )
        for ticker in tickers:
            prices[ticker].update(extension[ticker])
        sources.append(extension_source)
    return prices, sources


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        raise StrategyHistoryError("QSI validation correlation is unavailable")
    if max(left) - min(left) < 1e-14 or max(right) - min(right) < 1e-14:
        if max(abs(a - b) for a, b in zip(left, right, strict=True)) < 1e-12:
            return 1.0
        raise StrategyHistoryError("QSI validation correlation is invalid")
    try:
        return statistics.correlation(left, right)
    except statistics.StatisticsError as exc:
        raise StrategyHistoryError("QSI validation correlation is invalid") from exc


def _qsi_daily_path(
    weeks: Sequence[Mapping[str, Any]],
    prices: Mapping[str, Mapping[date, Decimal]],
) -> tuple[list[tuple[date, Decimal]], dict[str, object]]:
    calendar = sorted(prices.get("SPXL", {}))
    if not calendar:
        raise StrategyHistoryError("QSI reference trading calendar is missing")
    daily: list[tuple[date, Decimal]] = []
    predicted_weekly: list[float] = []
    validation_weekly: list[float] = []
    exact_anchor_errors: list[float] = []
    seen_dates: set[date] = set()
    for week in sorted(weeks, key=lambda item: str(item["end"])):
        start = week["start"]
        end = week["end"]
        weights = week["weights"]
        if not isinstance(start, date) or not isinstance(end, date):
            raise StrategyHistoryError("QSI weekly interval is invalid")
        if not isinstance(weights, dict) or not weights:
            raise StrategyHistoryError("QSI weekly weights are missing")
        anchors = [observed for observed in calendar if observed < start]
        if not anchors:
            raise StrategyHistoryError("QSI weekly price anchor is missing")
        anchor = anchors[-1]
        trading_days = [observed for observed in calendar if anchor < observed <= end]
        if not trading_days or trading_days[-1] != end:
            raise StrategyHistoryError(f"QSI week end lacks price evidence: {end}")
        total_weight = sum(weights.values(), Decimal(0))
        if abs(total_weight - Decimal(1)) > Decimal("0.01"):
            raise StrategyHistoryError(
                f"QSI weekly allocations do not sum to one: {end}"
            )
        cash_residual = Decimal(1) - total_weight
        levels: list[Decimal] = []
        for observed in trading_days:
            level = cash_residual
            for ticker, weight in weights.items():
                ticker_prices = prices.get(str(ticker), {})
                if anchor not in ticker_prices or observed not in ticker_prices:
                    raise StrategyHistoryError(
                        f"QSI price evidence is missing: {ticker} {observed}"
                    )
                level += weight * ticker_prices[observed] / ticker_prices[anchor]
            if level <= 0:
                raise StrategyHistoryError("QSI reconstructed level is invalid")
            levels.append(level)
        raw_weekly = levels[-1] - Decimal(1)
        exact_return = week["exact_return"]
        validation_return = week["price_validation_return"]
        if not isinstance(exact_return, Decimal) or not isinstance(
            validation_return, Decimal
        ):
            raise StrategyHistoryError("QSI weekly return evidence is invalid")
        scale = math.pow(
            float((Decimal(1) + exact_return) / levels[-1]),
            1 / len(levels),
        )
        if not math.isfinite(scale) or scale <= 0:
            raise StrategyHistoryError("QSI daily anchor adjustment is invalid")
        previous = Decimal(1)
        compounded = Decimal(1)
        for observed, level in zip(trading_days, levels, strict=True):
            if observed in seen_dates:
                raise StrategyHistoryError(f"QSI daily date overlaps: {observed}")
            seen_dates.add(observed)
            adjusted = level / previous * Decimal(str(scale)) - Decimal(1)
            if adjusted <= -1:
                raise StrategyHistoryError("QSI reconstructed return is invalid")
            daily.append((observed, adjusted))
            compounded *= Decimal(1) + adjusted
            previous = level
        predicted_weekly.append(float(raw_weekly))
        validation_weekly.append(float(validation_return))
        exact_anchor_errors.append(float(compounded - Decimal(1) - exact_return))
    errors = [
        predicted - actual
        for predicted, actual in zip(predicted_weekly, validation_weekly, strict=True)
    ]
    exact_weekly_anchor_max_abs_error = max(map(abs, exact_anchor_errors))
    price_shape_correlation = _correlation(predicted_weekly, validation_weekly)
    price_shape_mae = statistics.fmean(map(abs, errors))
    validation: dict[str, object] = {
        "exact_weekly_anchor_max_abs_error": exact_weekly_anchor_max_abs_error,
        "price_shape_correlation": price_shape_correlation,
        "price_shape_mae": price_shape_mae,
        "price_shape_max_abs_error": max(map(abs, errors)),
        "weekly_observations": len(weeks),
    }
    if price_shape_correlation < 0.98:
        raise StrategyHistoryError("QSI price-shape validation correlation is too low")
    if price_shape_mae > 0.005:
        raise StrategyHistoryError("QSI price-shape validation error is too high")
    if exact_weekly_anchor_max_abs_error > 1e-12:
        raise StrategyHistoryError("QSI exact weekly anchoring failed")
    return daily, validation


def _qsi(
    subscription_archive: Path,
    appendix_workbook: Path,
    price_root: Path,
    *,
    current_subscription_archive: Path | None = None,
    price_extension_json: Path | None = None,
    as_of_date: date | None = None,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    historical, archive_source, embedded_source = _qsi_archive_weeks(
        subscription_archive
    )
    current, appendix_source = _qsi_appendix_weeks(appendix_workbook)
    revision_validation: dict[str, object] | None = None
    current_archive_sources: list[dict[str, object]] = []
    source_observed_end: date | None = None
    excluded_after_as_of = 0
    if current_subscription_archive is not None:
        latest, latest_archive_source, latest_member_source = (
            _qsi_current_archive_weeks(current_subscription_archive)
        )
        revision_validation = {
            "historical_archive_overlap": _qsi_revision_validation(
                historical, latest, "historical archive"
            ),
            "prior_appendix_overlap": _qsi_revision_validation(
                current, latest, "prior appendix"
            ),
        }
        source_observed_end = max(item["end"] for item in latest)
        accepted_as_of = as_of_date or source_observed_end
        excluded_after_as_of = sum(item["end"] > accepted_as_of for item in latest)
        first_latest = min(item["end"] for item in latest)
        weeks = [
            *[item for item in historical if item["end"] < first_latest],
            *[item for item in latest if item["end"] <= accepted_as_of],
        ]
        current_archive_sources = [latest_archive_source, latest_member_source]
    else:
        weeks = [*historical, *current]
        # The appendix carries weeks past the declared as-of date. They are
        # provenance, not admitted returns, the same way the current archive's
        # later weeks are on the branch above; admitting them made the bundle
        # reject its own output against the post-as-of guard.
        if as_of_date is not None:
            excluded_after_as_of = sum(item["end"] > as_of_date for item in weeks)
            weeks = [item for item in weeks if item["end"] <= as_of_date]
    ends = [week["end"] for week in weeks]
    if len(set(ends)) != len(ends):
        raise StrategyHistoryError("QSI weekly histories overlap")
    tickers = sorted(
        {str(ticker) for week in weeks for ticker in dict(week["weights"])}
    )
    prices, price_sources = _qsi_prices(
        price_root, tickers, extension_json=price_extension_json
    )
    daily, validation = _qsi_daily_path(weeks, prices)
    sources = [
        archive_source,
        embedded_source,
        appendix_source,
        *current_archive_sources,
        *price_sources,
    ]
    version = _version(
        "qsi-stratum-current-3x",
        {
            "method": "EXACT_WEEKLY_ENDPOINT_ANCHORED_DAILY_MODEL_PATH",
            "source_sha256": [source["sha256"] for source in sources],
            "strategy_definition": (
                "CURRENT_STRATUM_3X_MODULES_AND_WEIGHTS_FROM_202608_ARCHIVE"
                if current_subscription_archive is not None
                else "CURRENT_STRATUM_3X_MODULES_AND_WEIGHTS"
            ),
        },
    )
    accepted = _return_rows("QSI", version, daily)
    definition = _definition(
        series_id="QSI",
        version=version,
        rows=accepted,
        history_status="ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
        compatibility_basis=(
            "EXACT_2013_2019_RETAINED_MODEL_PATH_PLUS_REVISED_CURRENT_2020_2026_3X_PATH"
            if current_subscription_archive is not None
            else "EXACT_2013_2024_MODEL_RETURN_TRADELOG_PLUS_CURRENT_2025_3X_ALLOCATION_AND_RETURN_PATH"
        ),
        frequency="DAILY_EXACT_WEEKLY_ENDPOINT_ANCHORED",
        implementation_status="MODEL_TO_ACCOUNT_RECONCILIATION_RETAINED_SEPARATELY",
        method="PRICE_SHAPE_RECONSTRUCTION_ANCHORED_TO_EXACT_WEEKLY_MODEL_RETURNS",
        source=archive_source,
        coverage_disposition=(
            "COMPLETE_THROUGH_DECLARED_AS_OF_WITH_LATER_SOURCE_ROWS_EXCLUDED"
            if excluded_after_as_of
            else "COMPLETE_WITHIN_RETAINED_2013_2025_MODEL_BOUNDARY"
        ),
    )
    definition["daily_reconstruction_validation"] = validation
    definition["exact_weekly_observations"] = len(weeks)
    definition["exact_weekly_observed_start"] = min(ends).isoformat()
    definition["exact_weekly_observed_end"] = max(ends).isoformat()
    definition["source_artifacts"] = sources
    # Excluding source rows is a provenance fact on every branch, not only when
    # a current subscription archive supplied them. Recording it solely on that
    # branch let the appendix path drop weeks silently.
    definition["source_rows_excluded_after_as_of"] = excluded_after_as_of
    if revision_validation is not None:
        assert source_observed_end is not None
        definition["history_revision_validation"] = revision_validation
        definition["source_observed_end"] = source_observed_end.isoformat()
    return accepted, definition


def _volatis_disposition(path: Path, *, accepted_as_of: date) -> dict[str, object]:
    source = _source_identity(path, "quantbase-volatis-pricehistory")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not {"time", "value", "cost_basis"} <= set(reader.fieldnames or ()):
                raise StrategyHistoryError("Volatis pricehistory headers differ")
            raw_rows = list(reader)
    except (OSError, UnicodeDecodeError) as exc:
        raise StrategyHistoryError("Volatis pricehistory source is invalid") from exc
    values: list[tuple[date, Decimal]] = []
    source_observed_end: date | None = None
    excluded_after_as_of = 0
    previous_value: Decimal | None = None
    previous_basis: Decimal | None = None
    for raw in raw_rows:
        observed = _excel_date(raw.get("time"), "Volatis date")
        value = _decimal(raw.get("value"), "Volatis value")
        basis = _decimal(raw.get("cost_basis"), "Volatis cost basis")
        if value <= 0 or basis <= 0:
            continue
        source_observed_end = (
            observed
            if source_observed_end is None
            else max(source_observed_end, observed)
        )
        if observed > accepted_as_of:
            excluded_after_as_of += 1
            continue
        if previous_value is None or previous_basis is None:
            period_return = value / basis - Decimal(1)
        else:
            external_flow = basis - previous_basis
            period_return = (value - external_flow) / previous_value - Decimal(1)
        values.append((observed, period_return))
        previous_value, previous_basis = value, basis
    if len(values) < 2:
        raise StrategyHistoryError("Volatis provider history is insufficient")
    version = _version(
        "volatis-quantbase-provider-path",
        {
            "fund": "Volatis Strategy",
            "method": "FLOW_ADJUSTED_PRICEHISTORY",
            "source_sha256": source["sha256"],
        },
    )
    disposition: dict[str, object] = {
        "action": "USE_ACCOUNT_LIVE_ADAPTER; DO_NOT_ADMIT_AS_BACKTEST",
        "history_status": "UNAVAILABLE_BACKTEST_NOT_PROVED",
        "implementation_reconciliation_status": "SAME_PROVIDER_ACCOUNT_PATH",
        "observed_end": values[-1][0].isoformat(),
        "observed_start": values[0][0].isoformat(),
        "provider_live_return_observations": len(values),
        "reason": (
            "The Quantbase price history is the current provider/account path, not a "
            "backtest. The account-live adapter owns its flow-adjusted returns; no "
            "duplicate economic return series is emitted by the backtest bundle."
        ),
        "required_for_analysis": True,
        "series_id": "Volatis Strategy",
        "source_artifacts": [source],
        "status": "CAPTURED_PROVIDER_LIVE_NOT_BACKTEST",
        "strategy_version": version,
    }
    if excluded_after_as_of:
        assert source_observed_end is not None
        disposition["coverage_disposition"] = (
            "COMPLETE_THROUGH_DECLARED_AS_OF_WITH_LATER_SOURCE_ROWS_EXCLUDED"
        )
        disposition["source_observed_end"] = source_observed_end.isoformat()
        disposition["source_rows_excluded_after_as_of"] = excluded_after_as_of
    return disposition


def build_bundle(
    *,
    grizzly_workbook: Path,
    grizzly_original_design_workbook: Path | None = None,
    grizzly_spx_workbook: Path | None = None,
    grizzly_spy_workbook: Path | None = None,
    grizzly_blended_trade_history: Path | None = None,
    grizzly_live_trade_history: Path | None = None,
    tt_workbook: Path,
    qsi_subscription_archive: Path,
    qsi_current_subscription_archive: Path | None = None,
    qsi_appendix_workbook: Path,
    qsi_price_root: Path,
    price_extension_json: Path | None = None,
    volatis_pricehistory_csv: Path,
    as_of_date: str,
    portfolio123_generation_root: Path | None = None,
    robuxio_track_record_generation: Path | None = None,
    robuxio_strategy_version: str | None = None,
    p123_eu_designer_model_generation: Path | None = None,
    p123_eu_strategy_version: str | None = None,
    live_inceptions: Mapping[str, str] | None = None,
) -> tuple[bytes, bytes]:
    try:
        accepted_as_of = date.fromisoformat(as_of_date)
    except ValueError as exc:
        raise StrategyHistoryError("as_of_date is invalid") from exc
    volatis_disposition = _volatis_disposition(
        volatis_pricehistory_csv, accepted_as_of=accepted_as_of
    )
    robuxio_part: tuple[list[dict[str, str]], dict[str, object]] | None = None
    if (robuxio_track_record_generation is None) != (robuxio_strategy_version is None):
        raise StrategyHistoryError(
            "Robuxio track-record generation and product version are one input"
        )
    if robuxio_track_record_generation is not None:
        robuxio_part = _robuxio_track_record(
            robuxio_track_record_generation, str(robuxio_strategy_version)
        )
    p123_eu_designer_part: tuple[list[dict[str, str]], dict[str, object]] | None = (
        None
    )
    if (p123_eu_designer_model_generation is None) != (
        p123_eu_strategy_version is None
    ):
        raise StrategyHistoryError(
            "EU designer-model generation and strategy version are one input"
        )
    if p123_eu_designer_model_generation is not None:
        p123_eu_designer_part = _p123_eu_designer_curve(
            p123_eu_designer_model_generation, str(p123_eu_strategy_version)
        )
    p123_dispositions: list[dict[str, object]] = []
    p123_eu_part: tuple[list[dict[str, str]], dict[str, object]] | None = None
    if portfolio123_generation_root is not None:
        p123_dispositions, p123_eu_part = _p123_history_dispositions(
            portfolio123_generation_root
        )
    if p123_eu_designer_part is not None:
        # The copy-derived prehistory only ever existed as a fallback for a
        # capture that had not been taken. Now that the designer model the
        # copy follows is a governed generation, it is the authority for the
        # design layer and the fallback is not used alongside it.
        p123_eu_part = p123_eu_designer_part
    parts = (
        _grizzly(
            grizzly_workbook,
            original_design_workbook=grizzly_original_design_workbook,
            spx_workbook=grizzly_spx_workbook,
            spy_workbook=grizzly_spy_workbook,
            blended_trade_history=grizzly_blended_trade_history,
            live_trade_history=grizzly_live_trade_history,
            price_root=(
                qsi_price_root if grizzly_blended_trade_history is not None else None
            ),
            price_extension_json=(
                price_extension_json
                if grizzly_blended_trade_history is not None
                else None
            ),
            as_of_date=(
                accepted_as_of if grizzly_blended_trade_history is not None else None
            ),
        ),
        _tt(tt_workbook, as_of_date=accepted_as_of),
        _qsi(
            qsi_subscription_archive,
            qsi_appendix_workbook,
            qsi_price_root,
            current_subscription_archive=qsi_current_subscription_archive,
            price_extension_json=price_extension_json,
            as_of_date=accepted_as_of,
        ),
        *((p123_eu_part,) if p123_eu_part is not None else ()),
        *((robuxio_part,) if robuxio_part is not None else ()),
    )
    rows = [row for accepted, _ in parts for row in accepted]
    definitions = [definition for _accepted, definition in parts]

    # Prehistory is admitted per strategy, strictly before that strategy's own
    # account-live inception (`portfolio_target_series` rejects a series whose
    # last row reaches its live start). Capping every series at one global
    # as-of instead discards evidence that is admissible for the later-starting
    # strategies, so each boundary is applied to its own series and recorded.
    boundaries = {
        series_id: _date_value(value, f"live inception {series_id}")
        for series_id, value in (live_inceptions or {}).items()
    }
    # A caller supplies the whole product's inception map; this bundle carries
    # only the series whose history comes from these retained vendor files, and
    # the rest are sourced elsewhere. Names outside it are recorded, not fatal.
    present = {item["series_id"] for item in definitions}
    not_applicable = sorted(set(boundaries) - present)
    boundaries = {k: v for k, v in boundaries.items() if k in present}
    if boundaries:
        kept: list[dict[str, str]] = []
        excluded_by_series: dict[str, int] = {}
        for row in rows:
            boundary = boundaries.get(row["series_id"])
            if boundary is not None and date.fromisoformat(row["date"]) >= boundary:
                excluded_by_series[row["series_id"]] = (
                    excluded_by_series.get(row["series_id"], 0) + 1
                )
                continue
            kept.append(row)
        rows = kept
        for definition in definitions:
            series_id = definition["series_id"]
            boundary = boundaries.get(series_id)
            if boundary is None:
                continue
            series_rows = [item for item in rows if item["series_id"] == series_id]
            if not series_rows:
                raise StrategyHistoryError(
                    f"live inception excludes the whole history: {series_id}"
                )
            definition["live_inception"] = boundary.isoformat()
            definition["observed_end"] = series_rows[-1]["date"]
            definition["return_observations"] = len(series_rows)
            definition["source_rows_excluded_at_or_after_live_inception"] = (
                excluded_by_series.get(series_id, 0)
            )
            if excluded_by_series.get(series_id):
                definition["coverage_disposition"] = (
                    "COMPLETE_STRICTLY_BEFORE_ACCOUNT_LIVE_INCEPTION"
                )

    rows.sort(key=lambda item: (item["series_id"], item["date"]))
    if any(date.fromisoformat(row["date"]) > accepted_as_of for row in rows):
        raise StrategyHistoryError("history contains observations after as_of_date")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=RETURN_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    returns_content = output.getvalue().encode("utf-8")
    manifest: dict[str, object] = {
        "as_of_date": accepted_as_of.isoformat(),
        "builder": "portfolio-strategy-history",
        "builder_version": BUILDER_VERSION,
        "returns_artifact": {
            "artifact_id": "strategy-history/returns.csv",
            "sha256": hashlib.sha256(returns_content).hexdigest(),
            "size_bytes": len(returns_content),
        },
        "schema_version": SCHEMA_VERSION,
        "live_inceptions_applied": {
            series_id: boundary.isoformat()
            for series_id, boundary in sorted(boundaries.items())
        },
        "live_inceptions_not_in_bundle": not_applicable,
        "series": definitions,
        "verification": "VERIFIED",
    }
    history_dispositions = [volatis_disposition]
    history_dispositions.extend(p123_dispositions)
    manifest["history_dispositions"] = history_dispositions
    manifest["bundle_id"] = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    return returns_content, canonical_json_bytes(manifest)


def _write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise StrategyHistoryError(f"existing output differs: {path}")
        return
    with path.open("xb") as handle:
        handle.write(content)


def write_bundle(output_root: Path, **kwargs: Any) -> dict[str, object]:
    returns_content, manifest_content = build_bundle(**kwargs)
    manifest = json.loads(manifest_content)
    destination = output_root / str(manifest["bundle_id"])
    _write_exact(destination / "returns.csv", returns_content)
    _write_exact(destination / "manifest.json", manifest_content)
    return {
        "bundle_id": manifest["bundle_id"],
        "destination": str(destination),
        "return_observations": sum(
            int(item["return_observations"]) for item in manifest["series"]
        ),
        "series": {
            str(item["series_id"]): {
                "coverage_disposition": item["coverage_disposition"],
                "observed_end": item["observed_end"],
                "observed_start": item["observed_start"],
                "return_observations": item["return_observations"],
                "strategy_version": item["strategy_version"],
            }
            for item in manifest["series"]
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grizzly-workbook", type=Path, required=True)
    parser.add_argument("--grizzly-original-design-workbook", type=Path)
    parser.add_argument("--grizzly-spx-workbook", type=Path)
    parser.add_argument("--grizzly-spy-workbook", type=Path)
    parser.add_argument("--grizzly-blended-trade-history", type=Path)
    parser.add_argument("--grizzly-live-trade-history", type=Path)
    parser.add_argument("--tt-workbook", type=Path, required=True)
    parser.add_argument("--qsi-subscription-archive", type=Path, required=True)
    parser.add_argument("--qsi-current-subscription-archive", type=Path)
    parser.add_argument("--qsi-appendix-workbook", type=Path, required=True)
    parser.add_argument("--qsi-price-root", type=Path, required=True)
    parser.add_argument("--price-extension-json", type=Path)
    parser.add_argument("--volatis-pricehistory-csv", type=Path, required=True)
    parser.add_argument("--portfolio123-generation-root", type=Path)
    parser.add_argument("--robuxio-track-record-generation", type=Path)
    parser.add_argument("--robuxio-strategy-version")
    parser.add_argument("--p123-eu-designer-model-generation", type=Path)
    parser.add_argument("--p123-eu-strategy-version")
    parser.add_argument("--as-of-date", required=True)
    parser.add_argument(
        "--live-inception",
        action="append",
        default=[],
        metavar="SERIES=YYYY-MM-DD",
        help=(
            "Account-live inception for one series. Its prehistory is admitted "
            "strictly before this date, which is what the layered-history "
            "consumer requires; repeat per series."
        ),
    )
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = write_bundle(
            args.output_root,
            live_inceptions=dict(
                (part.strip() for part in item.split("=", 1))
                for item in args.live_inception
            ),
            grizzly_workbook=args.grizzly_workbook,
            grizzly_original_design_workbook=args.grizzly_original_design_workbook,
            grizzly_spx_workbook=args.grizzly_spx_workbook,
            grizzly_spy_workbook=args.grizzly_spy_workbook,
            grizzly_blended_trade_history=args.grizzly_blended_trade_history,
            grizzly_live_trade_history=args.grizzly_live_trade_history,
            tt_workbook=args.tt_workbook,
            qsi_subscription_archive=args.qsi_subscription_archive,
            qsi_current_subscription_archive=args.qsi_current_subscription_archive,
            qsi_appendix_workbook=args.qsi_appendix_workbook,
            qsi_price_root=args.qsi_price_root,
            price_extension_json=args.price_extension_json,
            volatis_pricehistory_csv=args.volatis_pricehistory_csv,
            portfolio123_generation_root=args.portfolio123_generation_root,
            robuxio_track_record_generation=args.robuxio_track_record_generation,
            robuxio_strategy_version=args.robuxio_strategy_version,
            p123_eu_designer_model_generation=(
                args.p123_eu_designer_model_generation
            ),
            p123_eu_strategy_version=args.p123_eu_strategy_version,
            as_of_date=args.as_of_date,
        )
    except StrategyHistoryError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
