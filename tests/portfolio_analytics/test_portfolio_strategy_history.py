from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import pyarrow as pa
import pyarrow.parquet as pq
import xlsxwriter


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_strategy_history.py"
SPEC = importlib.util.spec_from_file_location("portfolio_strategy_history", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load portfolio strategy-history builder")
history = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = history
SPEC.loader.exec_module(history)


def _grizzly(path: Path) -> None:
    workbook = xlsxwriter.Workbook(path)
    sheet = workbook.add_worksheet("Simulation")
    sheet.write_row(0, 0, ["date", "day_ret"])
    start = datetime(2010, 1, 1)
    for index in range(1_001):
        sheet.write_datetime(index + 1, 0, start + timedelta(days=index))
        sheet.write_number(index + 1, 1, 0.001 if index else 0)
    workbook.close()


def _grizzly_original_futures(path: Path) -> None:
    workbook = xlsxwriter.Workbook(path)
    sheet = workbook.add_worksheet("Sheet1")
    sheet.write_row(
        0,
        0,
        [
            "Date (EST)",
            "Action",
            "SPX",
            "Prev Price",
            "Profit / Loss",
            "PnL %",
            "Model",
            "Buy & Hold",
        ],
    )
    start = datetime(2010, 1, 1)
    model = 100.0
    for index in range(100):
        model *= 1.001
        sheet.write_row(
            index + 1,
            0,
            [
                start + timedelta(days=index),
                "BUY" if index % 2 == 0 else "SELL",
                1_000 + index,
                999 + index,
                1,
                0.001,
                model,
                100 + index,
            ],
        )
    workbook.close()


def _tt(path: Path) -> None:
    slugs = [
        "tt-hokey-pokey",
        "tt-mach-3-v3",
        "tt-mean-kitty-v2",
        "tt-quick-change-v2",
        "tt-vix-spritz-aggro-v3",
        "tt-hot-sauce",
    ]
    workbook = xlsxwriter.Workbook(path)
    index_sheet = workbook.add_worksheet("Index")
    index_sheet.write_row(0, 4, ["strategy", "period", "allocation"])
    for index, slug in enumerate(slugs, start=1):
        index_sheet.write_row(index, 4, [slug, 2, 1 / 6])
    raw = workbook.add_worksheet("raw_ttdata")
    raw.write_row(0, 0, ["Date", *[f"Current ({slug})" for slug in slugs]])
    start = datetime(2010, 1, 1)
    levels = [1_000.0] * 6
    for index in range(1_002):
        raw.write_datetime(index + 1, 0, start + timedelta(days=index))
        if index:
            levels = [
                level * (1 + 0.001 * (offset + 1))
                for offset, level in enumerate(levels)
            ]
        raw.write_row(index + 1, 1, levels)
    workbook.close()


def _qsi_appendix(path: Path) -> None:
    workbook = xlsxwriter.Workbook(path)
    sheet = workbook.add_worksheet("Stratum 3x")
    sheet.write_row(0, 0, ["Fund Ticker", "Fund Name", "Fund Role", "Alternatives"])
    start = datetime(2025, 1, 3)
    for index in range(30):
        sheet.write(
            index * 0 + 0,
            index + 4,
            (start + timedelta(days=7 * index)).strftime("%Y-%m-%d"),
        )
    sheet.write(4, 0, "Stratum 3x Return")
    sheet.write_row(5, 0, ["SPXL", "Synthetic", "US equity beta", None])
    for index in range(30):
        sheet.write_number(4, index + 4, 0.01)
        sheet.write_number(5, index + 4, 1.0)
    workbook.close()


def _qsi_subscription_archive(path: Path) -> None:
    embedded = io.BytesIO()
    workbook = xlsxwriter.Workbook(embedded, {"in_memory": True})
    sheet = workbook.add_worksheet("Sheet 1")
    sheet.write_row(
        0,
        0,
        [
            "Week Start Date",
            "Week End Date",
            "Bench Returns (SPY)",
            "Stratum Returns (With Slippage)",
            "Stratum Returns (No Slippage)",
            "Bench Growth (SPY)",
            "Stratum Growth (With Slippage)",
            "Stratum Label",
            "Weight Allocations",
        ],
    )
    start = datetime(2013, 10, 21)
    for index in range(500):
        week_start = start + timedelta(days=7 * index)
        sheet.write_datetime(index + 1, 0, week_start)
        sheet.write_datetime(index + 1, 1, week_start + timedelta(days=4))
        sheet.write_number(index + 1, 2, 0.005)
        sheet.write_number(index + 1, 3, 0.01)
        sheet.write_number(index + 1, 4, 0.01)
        sheet.write_number(index + 1, 5, 1.0)
        sheet.write_number(index + 1, 6, 1.0)
        sheet.write(index + 1, 7, "SPXL")
        sheet.write(index + 1, 8, "1")
    workbook.close()
    original = ZipFile(io.BytesIO(embedded.getvalue()))
    repaired = io.BytesIO()
    with original, ZipFile(repaired, "w", ZIP_DEFLATED) as rewritten:
        for name in original.namelist():
            content = original.read(name)
            if name == "xl/worksheets/sheet1.xml":
                text = content.decode("utf-8")
                start_index = text.index("<dimension ")
                end_index = text.index("/>", start_index) + 2
                content = (
                    text[:start_index] + '<dimension ref="A1"/>' + text[end_index:]
                ).encode("utf-8")
            rewritten.writestr(name, content)
    member = (
        "Quanta 72 Paid Sub Files/Backtest Folder/Backtest Tradelog/"
        "Stratum_vs_SPY_Weekly_Returns 2013 to 2024.xlsx"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(member, repaired.getvalue())


def _qsi_current_archive(path: Path) -> None:
    embedded = io.BytesIO()
    workbook = xlsxwriter.Workbook(embedded, {"in_memory": True})
    sheet = workbook.add_worksheet("Stratum Data Combined")
    sheet.write_row(
        0,
        0,
        [
            "Week Ending Date",
            "Stratum 3x Returns",
            "Stratum 3x Returns (with slippage)",
            "Stratum 3x Label",
            "allocation_reference",
        ],
    )
    start = datetime(2025, 1, 3)
    for index in range(30):
        sheet.write_datetime(index + 1, 0, start + timedelta(days=7 * index))
        sheet.write_number(index + 1, 1, 0.011)
        sheet.write_number(index + 1, 2, 0.01)
        sheet.write(index + 1, 3, "SPXL / BIL")
        sheet.write(index + 1, 4, "0.75 / 0.25")
    workbook.close()
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "Quanta 72 Paid Sub Files/Weekly_File_Drops/stratum_data_combined.xlsx",
            embedded.getvalue(),
        )


def _qsi_prices(path: Path) -> None:
    path.mkdir()
    start = datetime(2013, 10, 18)
    end = datetime(2025, 8, 1)
    gross = 1.01 ** (1 / 5)
    level = 100.0
    rows = []
    observed = start
    while observed <= end:
        if observed.weekday() < 5:
            rows.append((observed.strftime("%Y-%m-%d"), level))
            level *= gross
        observed += timedelta(days=1)
    with (path / "SPXL.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = history.csv.writer(handle, lineterminator="\n")
        writer.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
        for observed, close in reversed(rows):
            writer.writerow([observed, close, close, close, close, 1_000])


def _p123_generation(path: Path) -> None:
    tables = path / "tables"
    tables.mkdir(parents=True)
    exact_id = "exact-eu-model"
    files = {
        "har_managed_metadata.parquet": pa.Table.from_pylist(
            [
                {
                    "name": "Based on EU multifactor microcap model",
                    "assetName": "EU multifactor microcap model",
                    "assetUid": exact_id,
                    "portfolioUid": "exact-eu-live-book",
                    "isAccountLive": "True",
                    "currency": "USD",
                    "inceptionDt": "2025-11-12",
                    "strategyTotalHoldCount": "13",
                    "posCnt": "13",
                },
                {
                    "name": "Based on EU multifactor microcap model",
                    "assetName": "EU multifactor microcap model",
                    "assetUid": exact_id,
                    "portfolioUid": "exact-eu-live-book",
                    "isAccountLive": "True",
                    "currency": "USD",
                    "inceptionDt": "2025-11-12",
                    "strategyTotalHoldCount": "13",
                    "posCnt": "13",
                },
                {
                    "name": "Based on EU multifactor microcap model",
                    "assetName": "EU multifactor microcap model",
                    "assetUid": exact_id,
                    "portfolioUid": "exact-eu-live-book",
                    "isAccountLive": "True",
                    "currency": "USD",
                    "inceptionDt": "2025-11-12",
                    "strategyTotalHoldCount": "13",
                    "posCnt": "13",
                },
            ]
        ),
        "har_equity_curve.parquet": pa.Table.from_pylist(
            [
                {
                    "_entity_id": exact_id,
                    "Date": "2025-11-13",
                    "Total Equity": "100",
                    "Cash Added": "100",
                    "# Positions": "13",
                },
                {
                    "_entity_id": exact_id,
                    "Date": "2025-11-14",
                    "Total Equity": "101",
                    "Cash Added": "101",
                    "# Positions": "13",
                },
                {
                    "_entity_id": exact_id,
                    "Date": "2025-11-17",
                    "Total Equity": "99",
                    "Cash Added": "99",
                    "# Positions": "13",
                },
                {
                    "_entity_id": "different-eu-book",
                    "Date": "1999-01-01",
                    "# Positions": "100",
                    "Total Equity": "100",
                    "Cash Added": "0",
                },
            ]
        ),
        "har_managed_performance.parquet": pa.Table.from_pylist(
            [
                {"_entity_id": exact_id, "field": field, "value": value}
                for field, value in (
                    # Real Portfolio123 markers are small day codes unrelated
                    # to the level.  Setting the marker to ``level - 100``
                    # makes reading it as a percentage arithmetically identical
                    # to reading the level, which is how the marker survived as
                    # the emitted series.  These markers keep the two apart.
                    ("data", json.dumps([[0, 100, 100], [1, 101, 101], [3, 99, 99]])),
                    ("date", "2025-11-13"),
                    ("dateLast", "2025-11-17"),
                    (
                        "panels",
                        json.dumps(
                            [
                                {
                                    "graphs": [
                                        {"idx": 0, "s": "Strategy"},
                                        {"idx": 1, "s": "Total Value"},
                                    ]
                                }
                            ]
                        ),
                    ),
                )
            ]
        ),
        "returns_daily.parquet": pa.table(
            {
                "entity_id": pa.array([], type=pa.string()),
                "period_date": pa.array([], type=pa.string()),
                "model_return": pa.array([], type=pa.string()),
            }
        ),
        "strategy_registry.parquet": pa.Table.from_pylist(
            [
                {
                    "entity_id": "different-eu-book",
                    "canonical_name": "Multifactor Europe - Different Book",
                }
            ]
        ),
    }
    artifacts = []
    for name, table in files.items():
        target = tables / name
        pq.write_table(table, target)
        digest, size = history._sha256(target)
        artifacts.append(
            {
                "artifact_id": f"tables/{name}",
                "sha256": digest,
                "size_bytes": size,
            }
        )
    (path / "generation.json").write_text(
        json.dumps(
            {
                "artifacts": artifacts,
                "generation_id": "e" * 64,
                "verification": "VERIFIED",
            }
        ),
        encoding="utf-8",
    )


def _bundle_inputs(tmp_path: Path) -> dict[str, Path]:
    """The minimum retained-source set `build_bundle` requires."""
    grizzly = tmp_path / "grizzly.xlsx"
    tt = tmp_path / "tt.xlsx"
    qsi_archive = tmp_path / "qsi.zip"
    qsi_appendix = tmp_path / "qsi.xlsx"
    qsi_prices = tmp_path / "qsi-prices"
    volatis = tmp_path / "volatis.csv"
    _grizzly(grizzly)
    _tt(tt)
    _qsi_subscription_archive(qsi_archive)
    _qsi_appendix(qsi_appendix)
    _qsi_prices(qsi_prices)
    volatis.write_text(
        "\n".join(
            [
                "time,value,cost_basis,profit_loss",
                "10/15/2024,990,1000,-0.01",
                "10/16/2024,1000,1000,0",
                "10/17/2024,1010,1000,0.01",
                "08/04/2026,1100,1000,0.1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "grizzly_workbook": grizzly,
        "tt_workbook": tt,
        "qsi_subscription_archive": qsi_archive,
        "qsi_appendix_workbook": qsi_appendix,
        "qsi_price_root": qsi_prices,
        "volatis_pricehistory_csv": volatis,
    }


def test_build_bundle_wires_all_retained_strategy_histories(tmp_path: Path) -> None:
    grizzly = tmp_path / "grizzly.xlsx"
    tt = tmp_path / "tt.xlsx"
    qsi_archive = tmp_path / "qsi.zip"
    qsi_appendix = tmp_path / "qsi.xlsx"
    qsi_prices = tmp_path / "qsi-prices"
    volatis = tmp_path / "volatis.csv"
    p123 = tmp_path / "p123-generation"
    _grizzly(grizzly)
    _tt(tt)
    _qsi_subscription_archive(qsi_archive)
    _qsi_appendix(qsi_appendix)
    _qsi_prices(qsi_prices)
    _p123_generation(p123)
    volatis.write_text(
        "time,value,cost_basis,profit_loss\n"
        "10/15/2024,990,1000,-0.01\n"
        "10/16/2024,1000,1000,0\n"
        "10/17/2024,1010,1000,0.01\n"
        "08/04/2026,1100,1000,0.1\n",
        encoding="utf-8",
    )

    returns_content, manifest_content = history.build_bundle(
        grizzly_workbook=grizzly,
        tt_workbook=tt,
        qsi_subscription_archive=qsi_archive,
        qsi_appendix_workbook=qsi_appendix,
        qsi_price_root=qsi_prices,
        volatis_pricehistory_csv=volatis,
        portfolio123_generation_root=p123,
        as_of_date="2026-08-03",
    )
    manifest = json.loads(manifest_content)
    rows = list(history.csv.DictReader(io.StringIO(returns_content.decode("utf-8"))))

    assert manifest["verification"] == "VERIFIED"
    assert manifest["builder"] == "portfolio-strategy-history"
    assert manifest["builder_version"] == "1.7.0"
    assert {item["series_id"] for item in manifest["series"]} == {
        "GrizzlyBears VTMA",
        "QSI",
        "TT Portfolio",
    }
    assert {row["evidence_layer"] for row in rows} == {"BACKTEST"}
    assert (
        len([row for row in rows if row["series_id"] == "GrizzlyBears VTMA"]) == 1_001
    )
    assert len([row for row in rows if row["series_id"] == "TT Portfolio"]) == 1_001
    qsi_rows = [row for row in rows if row["series_id"] == "QSI"]
    assert len(qsi_rows) == 2_650
    qsi_manifest = next(
        item for item in manifest["series"] if item["series_id"] == "QSI"
    )
    assert qsi_manifest["exact_weekly_observations"] == 530
    assert (
        qsi_manifest["daily_reconstruction_validation"]["price_shape_correlation"]
        > 0.999
    )
    dispositions = {
        item["series_id"]: item for item in manifest["history_dispositions"]
    }
    assert dispositions["EU [P123]"]["status"] == (
        "CAPTURED_EXACT_CONNECTED_MODEL_PROVIDER_LIVE_TRANSFORM_DERIVED_NOT_ADMITTED"
    )
    assert dispositions["EU [P123]"]["required_for_analysis"] is True
    assert dispositions["EU [P123]"]["exact_equity_curve_rows"] == 3
    assert dispositions["EU [P123]"]["provider_live"] == {
        "cash_added_semantics": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
        # Levels 100 -> 101 -> 99 compound to -1%.  Reading the leading markers
        # 0/1/3 as percentages instead compounds to +3%.
        "cumulative_return": "-0.01",
        "equity_overlap_observations": 3,
        "observed_end": "2025-11-17",
        "observed_start": "2025-11-14",
        "raw_observations": 3,
        "return_observations": 2,
        "transform_id": "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1",
    }
    assert (
        dispositions["EU [P123]"]["retained_but_not_admitted"][0]["return_observations"]
        == 1
    )
    assert dispositions["Volatis Strategy"]["status"] == (
        "CAPTURED_PROVIDER_LIVE_NOT_BACKTEST"
    )
    assert dispositions["Volatis Strategy"]["history_status"] == (
        "UNAVAILABLE_BACKTEST_NOT_PROVED"
    )
    assert dispositions["Volatis Strategy"]["observed_end"] == "2024-10-17"
    assert dispositions["Volatis Strategy"]["provider_live_return_observations"] == 3
    assert dispositions["Volatis Strategy"]["source_observed_end"] == "2026-08-04"
    assert dispositions["Volatis Strategy"]["source_rows_excluded_after_as_of"] == 1
    echotrade = dispositions["Growth at Market Pivot Points [EchoTrade]"]
    assert echotrade["required_for_analysis"] is False
    assert echotrade["history_status"] == "RETAINED_REJECTED_HISTORY_DEPRECATED"
    assert echotrade["status"] == "DEPRECATED_RETAINED_REJECTED_HISTORY_BY_GOVERNANCE"
    assert echotrade["strategy_version"] == (
        "echotrade-deprecated-retained-rejected-history:2026-08-29"
    )
    assert echotrade["reason"] == (
        "EchoTrade is deprecated; retained event-only history is rejected "
        "as non-daily account-live evidence."
    )
    assert (
        dispositions["[Composer] - The Holy Grail (Buy Copy)"]["required_for_analysis"]
        is False
    )


def test_p123_conflicting_connected_model_metadata_fails_closed(
    tmp_path: Path,
) -> None:
    generation = tmp_path / "p123-generation"
    _p123_generation(generation)
    metadata_path = generation / "tables" / "har_managed_metadata.parquet"
    metadata = pq.read_table(metadata_path).to_pylist()
    conflicting = dict(metadata[0])
    conflicting["portfolioUid"] = "conflicting-live-book"
    pq.write_table(pa.Table.from_pylist([*metadata, conflicting]), metadata_path)

    digest, size = history._sha256(metadata_path)
    manifest_path = generation / "generation.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = next(
        row
        for row in manifest["artifacts"]
        if row["artifact_id"] == "tables/har_managed_metadata.parquet"
    )
    artifact["sha256"] = digest
    artifact["size_bytes"] = size
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        history.StrategyHistoryError,
        match="connected EU model metadata is not unique",
    ):
        history._p123_history_dispositions(generation)


def test_grizzly_lineage_separates_futures_design_from_live_spxl(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original-futures.xlsx"
    spx = tmp_path / "using-spx.xlsx"
    spy = tmp_path / "using-spy.xlsx"
    spxl = tmp_path / "using-spxl.xlsx"
    _grizzly_original_futures(original)
    for path in (spx, spy, spxl):
        _grizzly(path)

    rows, definition = history._grizzly(
        spxl,
        original_design_workbook=original,
        spx_workbook=spx,
        spy_workbook=spy,
    )

    lineage = definition["implementation_lineage"]
    layers = {item["layer_id"]: item for item in lineage["layers"]}
    assert len(rows) == 1_001
    assert lineage["design_analysis_binding"] == "FUTURES_DESIGN"
    assert lineage["current_live_implementation"]["instrument"] == "SPXL"
    assert layers["FUTURES_DESIGN"]["instrument"] == "S&P_500_FUTURES"
    assert layers["SPY_BACKTEST"]["status"].startswith("ACCEPTED_COMPARATIVE")
    assert layers["SPXL_BACKTEST"]["status"] == (
        "ADMITTED_CURRENT_PORTFOLIO_IMPLEMENTATION_HISTORY"
    )
    assert "FUTURES_DESIGN" in definition["compatibility_basis"]


def test_blank_qsi_history_fails_loud(tmp_path: Path) -> None:
    path = tmp_path / "blank.xlsx"
    workbook = xlsxwriter.Workbook(path)
    sheet = workbook.add_worksheet("Stratum 3x")
    sheet.write(0, 0, "Week Ending Date")
    workbook.close()

    with pytest.raises(history.StrategyHistoryError, match="return row is missing"):
        history._qsi_appendix_weeks(path)


def test_qsi_current_archive_is_newer_overlap_authority(tmp_path: Path) -> None:
    current = tmp_path / "qsi-current.zip"
    _qsi_current_archive(current)

    weeks, archive_source, member_source = history._qsi_current_archive_weeks(current)
    prior = [
        {
            **row,
            "exact_return": row["exact_return"] - history.Decimal("0.001"),
        }
        for row in weeks
    ]
    revision = history._qsi_revision_validation(prior, weeks, "test")

    assert len(weeks) == 30
    assert weeks[0]["weights"] == {
        "SPXL": history.Decimal("0.75"),
        "BIL": history.Decimal("0.25"),
    }
    assert archive_source["source_id"] == "qsi-current-paid-subscription-archive"
    assert member_source["source_id"] == "qsi-current-exact-weekly-model-path"
    assert revision["accepted_authority"] == "NEWER_CURRENT_ARCHIVE"
    assert revision["overlap_observations"] == 30
    assert revision["max_abs_return_revision"] == pytest.approx(0.001)


def test_each_series_is_capped_at_its_own_live_inception(tmp_path: Path) -> None:
    """Prehistory is admitted per strategy, not against one global boundary.

    `portfolio_target_series` rejects a series whose last row reaches that
    strategy's own account-live start, so capping every series at a single
    as-of discards evidence that is still admissible for the later-starting
    strategies. A name that is not in this bundle is recorded, not fatal: the
    caller supplies the whole product's inception map.
    """
    tt_path = tmp_path / "tt.xlsx"
    _tt(tt_path)
    uncapped, definition = history._tt(tt_path)
    last = date.fromisoformat(max(row["date"] for row in uncapped))
    boundary = last - timedelta(days=3)

    returns, manifest = history.build_bundle(
        **_bundle_inputs(tmp_path),
        as_of_date="2026-08-03",
        live_inceptions={
            definition["series_id"]: boundary.isoformat(),
            "Series That Is Not In This Bundle": "2020-01-01",
        },
    )
    payload = json.loads(manifest)
    assert payload["live_inceptions_applied"] == {
        definition["series_id"]: boundary.isoformat()
    }
    assert payload["live_inceptions_not_in_bundle"] == [
        "Series That Is Not In This Bundle"
    ]
    capped = [
        row
        for row in history.csv.DictReader(io.StringIO(returns.decode("utf-8")))
        if row["series_id"] == definition["series_id"]
    ]
    assert capped
    assert max(date.fromisoformat(row["date"]) for row in capped) < boundary
    entry = next(
        item
        for item in payload["series"]
        if item["series_id"] == definition["series_id"]
    )
    assert entry["live_inception"] == boundary.isoformat()
    assert entry["return_observations"] == len(capped)
    assert entry["observed_end"] == capped[-1]["date"]
    assert entry["source_rows_excluded_at_or_after_live_inception"] > 0
    assert entry["coverage_disposition"] == (
        "COMPLETE_STRICTLY_BEFORE_ACCOUNT_LIVE_INCEPTION"
    )


def test_readers_do_not_admit_rows_after_the_as_of_date(tmp_path: Path) -> None:
    """Both readers used to emit rows the bundle's own guard then rejected.

    The TT sheet is a live API export that runs to the pull date, and the QSI
    appendix carries weeks past the declared boundary. Neither was truncated on
    the path this build uses, so `build_bundle` failed with "history contains
    observations after as_of_date" and no bundle could be produced at all.
    """
    tt_path = tmp_path / "tt.xlsx"
    _tt(tt_path)
    uncapped, _definition = history._tt(tt_path)
    # The reader still requires a substantial history, so trim only a little.
    boundary = date.fromisoformat(max(row["date"] for row in uncapped)) - timedelta(
        days=2
    )
    capped, _definition = history._tt(tt_path, as_of_date=boundary)
    assert capped
    assert len(capped) < len(uncapped)
    assert max(date.fromisoformat(row["date"]) for row in capped) <= boundary

    archive = tmp_path / "qsi.zip"
    appendix = tmp_path / "qsi.xlsx"
    prices = tmp_path / "qsi-prices"
    _qsi_subscription_archive(archive)
    _qsi_appendix(appendix)
    _qsi_prices(prices)
    qsi_uncapped, _definition = history._qsi(archive, appendix, prices)
    qsi_boundary = date.fromisoformat(
        max(row["date"] for row in qsi_uncapped)
    ) - timedelta(days=14)
    qsi_capped, _definition = history._qsi(
        archive, appendix, prices, as_of_date=qsi_boundary
    )
    assert qsi_capped
    assert len(qsi_capped) < len(qsi_uncapped)
    assert max(date.fromisoformat(row["date"]) for row in qsi_capped) <= qsi_boundary


def test_grizzly_manual_live_file_is_validated_as_combined_subset(
    tmp_path: Path,
) -> None:
    blended = tmp_path / "grizzly-all.csv"
    live = tmp_path / "grizzly-live.csv"
    header = "Date (ET),Action,SPX,Prev Price,Profit / Loss,PnL %,Model,Buy & Hold\n"
    start = datetime(2020, 1, 2, 15)
    combined_rows = []
    for index in range(102):
        action = "Sell" if index % 2 == 0 else "Rebuy"
        observed = start + timedelta(days=index)
        pnl = "1" if action == "Sell" else ""
        combined_rows.append(
            f"{observed:%Y-%m-%d %H:%M} ET,{action},100,99,1,{pnl},100,100\n"
        )
    blended.write_text(header + "".join(combined_rows), encoding="utf-8")
    initialization = "2020-01-01 15:00 ET,Buy,100,99,1,,100,100\n"
    live.write_text(header + initialization + "".join(combined_rows), encoding="utf-8")

    rows, validation, sources = history._grizzly_manual_histories(blended, live)

    assert len(rows) == 102
    assert validation["exact_live_overlap_observations"] == 102
    assert validation["live_initialization_rows_not_repeated_in_all_history"] == 1
    assert validation["realized_model_return_observations"] == 51
    assert validation["status"] == "CONSISTENT_EXCEPT_LIVE_INITIALIZATION_ROW"
    assert {item["source_id"] for item in sources} == {
        "grizzlybulls-all-backtest-live-trade-history",
        "grizzlybulls-live-only-trade-history",
    }


def _eu_curve(start, days, *, step="0.01"):
    """A provider equity curve: one continuous level path, no layer marker."""

    level = history.Decimal("100000")
    ordered = []
    for index in range(days):
        if index:
            level *= history.Decimal(1) + history.Decimal(step)
        ordered.append((start + timedelta(days=index), (level, history.Decimal(0))))
    return ordered


EU_ARTIFACT = {"sha256": "a" * 64, "size_bytes": 4096}


def _eu_backtest(ordered):
    return history._p123_eu_backtest(
        ordered,
        generation_id="b" * 64,
        exact_ids={"1764562", "1897754"},
        strategy_name="EU multifactor microcap model",
        equity_artifact=EU_ARTIFACT,
    )


def test_eu_prehistory_stops_strictly_before_the_provider_launch() -> None:
    launch = history.P123_EU_PROVIDER_LAUNCH
    ordered = _eu_curve(launch - timedelta(days=5), 11)

    rows, definition = _eu_backtest(ordered)

    assert definition["series_id"] == "EU [P123]"
    assert definition["provider_launch"] == launch.isoformat()
    # Five observations before the launch yield four returns, and the last one
    # lands the day before it: the launch day itself is live, never prehistory.
    assert definition["observed_end"] == (launch - timedelta(days=1)).isoformat()
    assert definition["return_observations"] == 4
    assert definition["source_rows_excluded_at_or_after_provider_launch"] == 6
    assert {row["evidence_layer"] for row in rows} == {"BACKTEST"}
    assert all(row["return"] == "0.01" for row in rows)
    assert {row["strategy_version"] for row in rows} == {definition["strategy_version"]}
    assert definition["source_artifacts"][0]["sha256"] == EU_ARTIFACT["sha256"]


def test_eu_prehistory_is_absent_when_the_capture_starts_after_the_launch() -> None:
    """The subscribed copy's curve begins when the copy was created."""

    ordered = _eu_curve(history.P123_EU_PROVIDER_LAUNCH + timedelta(days=30), 40)

    assert _eu_backtest(ordered) is None


def test_a_launch_on_a_non_trading_day_admits_only_earlier_sessions() -> None:
    launch = history.P123_EU_PROVIDER_LAUNCH
    ordered = [
        item for item in _eu_curve(launch - timedelta(days=4), 9) if item[0] != launch
    ]

    rows, definition = _eu_backtest(ordered)

    assert definition["observed_end"] < launch.isoformat()
    assert all(row["date"] < launch.isoformat() for row in rows)


def test_eu_prehistory_needs_two_observations_to_form_a_return() -> None:
    assert (
        _eu_backtest(_eu_curve(history.P123_EU_PROVIDER_LAUNCH - timedelta(days=1), 5))
        is None
    )


DESIGNER_VERSION = "p123-eu-connected-model:" + "c" * 64


def _designer_generation(root: Path, rows: list[tuple[str, str, str]]) -> Path:
    """A staged designer-model generation: returns bound by a manifest digest."""

    body = "series_id,date,return,evidence_layer,return_basis\n" + "".join(
        "EU multifactor microcap model,%s,%s,%s,"
        "PROVIDER_PUBLISHED_MODEL_EQUITY_CURVE\n" % row
        for row in rows
    )
    payload = body.encode("utf-8")
    generation_id = "d" * 64
    generation = root / generation_id
    generation.mkdir(parents=True)
    (generation / "returns.csv").write_bytes(payload)
    (generation / "generation.json").write_text(
        json.dumps(
            {
                "generation_id": generation_id,
                "verification": "VERIFIED",
                "artifacts": [
                    {
                        "artifact_id": "returns.csv",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size_bytes": len(payload),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (generation / "catalog.json").write_text(
        json.dumps(
            {
                "series": {
                    "currency": "EUR",
                    "provider_launch": "2024-02-18",
                    "provider_revision_dates": ["2025-09-06"],
                }
            }
        ),
        encoding="utf-8",
    )
    return generation


def test_designer_curve_admits_only_the_pre_launch_segment(tmp_path: Path) -> None:
    generation = _designer_generation(
        tmp_path,
        [
            ("2024-02-15", "0.01", "BACKTEST"),
            ("2024-02-16", "0.02", "BACKTEST"),
            ("2024-02-19", "0.03", "PROVIDER_MODEL_LIVE"),
            ("2024-02-20", "0.04", "PROVIDER_MODEL_LIVE"),
        ],
    )

    rows, definition = history._p123_eu_designer_curve(generation, DESIGNER_VERSION)

    assert [row["date"] for row in rows] == ["2024-02-15", "2024-02-16"]
    assert {row["evidence_layer"] for row in rows} == {"BACKTEST"}
    assert {row["series_id"] for row in rows} == {"EU [P123]"}
    assert definition["return_observations"] == 2
    assert definition["provider_model_live_observations_not_admitted"] == 2
    assert definition["observed_end"] == "2024-02-16"
    assert definition["provider_launch"] == "2024-02-18"
    assert definition["strategy_version"] == DESIGNER_VERSION


def test_designer_curve_carries_the_currency_and_revision_dates(
    tmp_path: Path,
) -> None:
    """The model publishes in EUR while the owner holds it in a USD account."""

    generation = _designer_generation(
        tmp_path,
        [("2024-02-15", "0.01", "BACKTEST"), ("2024-02-16", "0.02", "BACKTEST")],
    )

    _rows, definition = history._p123_eu_designer_curve(generation, DESIGNER_VERSION)

    assert definition["provider_currency"] == "EUR"
    assert definition["provider_revision_dates"] == ["2025-09-06"]
    assert definition["implementation_reconciliation_status"] == (
        "MODEL_TO_ACCOUNT_RECONCILIATION_RETAINED_SEPARATELY"
    )


def test_designer_curve_refuses_returns_that_differ_from_the_manifest(
    tmp_path: Path,
) -> None:
    generation = _designer_generation(
        tmp_path,
        [("2024-02-15", "0.01", "BACKTEST"), ("2024-02-16", "0.02", "BACKTEST")],
    )
    (generation / "returns.csv").write_text("series_id,date\n", encoding="utf-8")

    with pytest.raises(
        history.StrategyHistoryError, match="differ from the generation"
    ):
        history._p123_eu_designer_curve(generation, DESIGNER_VERSION)


def test_designer_curve_refuses_an_unverified_generation(tmp_path: Path) -> None:
    generation = _designer_generation(
        tmp_path,
        [("2024-02-15", "0.01", "BACKTEST"), ("2024-02-16", "0.02", "BACKTEST")],
    )
    manifest = json.loads((generation / "generation.json").read_text(encoding="utf-8"))
    manifest["verification"] = "UNVERIFIED"
    (generation / "generation.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(history.StrategyHistoryError, match="not verified"):
        history._p123_eu_designer_curve(generation, DESIGNER_VERSION)


def test_designer_curve_refuses_prehistory_that_reaches_the_launch(
    tmp_path: Path,
) -> None:
    """A row on the launch day is live evidence, so admitting it would relabel."""

    generation = _designer_generation(
        tmp_path,
        [("2024-02-16", "0.01", "BACKTEST"), ("2024-02-18", "0.02", "BACKTEST")],
    )

    with pytest.raises(
        history.StrategyHistoryError, match="reaches the provider launch"
    ):
        history._p123_eu_designer_curve(generation, DESIGNER_VERSION)


def test_designer_curve_refuses_an_unexpected_evidence_layer(tmp_path: Path) -> None:
    generation = _designer_generation(
        tmp_path,
        [("2024-02-15", "0.01", "BACKTEST"), ("2024-02-16", "0.02", "ACCOUNT_LIVE")],
    )

    with pytest.raises(history.StrategyHistoryError, match="unexpected evidence layer"):
        history._p123_eu_designer_curve(generation, DESIGNER_VERSION)
