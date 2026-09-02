from __future__ import annotations

from datetime import datetime, timedelta
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


def _qsi_appendix(path: Path, start: datetime = datetime(2025, 1, 3)) -> None:
    workbook = xlsxwriter.Workbook(path)
    sheet = workbook.add_worksheet("Stratum 3x")
    sheet.write_row(0, 0, ["Fund Ticker", "Fund Name", "Fund Role", "Alternatives"])
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


_QSI_BACKTEST_MEMBER = (
    "Quanta 72 Paid Sub Files/Backtest Folder/Backtest Tradelog/"
    "Stratum_vs_SPY_Weekly_Returns 2013 to 2024.xlsx"
)
_QSI_WEEKLY_MEMBER = (
    "Quanta 72 Paid Sub Files/Weekly_File_Drops/stratum_data_combined.xlsx"
)


def _qsi_backtest_span_payload() -> bytes:
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
    return repaired.getvalue()


def _qsi_weekly_drops_payload(
    *,
    start: datetime = datetime(2025, 1, 3),
    count: int = 30,
    dates: list[datetime] | None = None,
    label: str = "SPXL / BIL",
    allocation: str = "0.75 / 0.25",
    exact: float = 0.01,
    validation: float = 0.011,
) -> bytes:
    observed = (
        dates
        if dates is not None
        else [start + timedelta(days=7 * index) for index in range(count)]
    )
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
    for index, week_end in enumerate(observed):
        sheet.write_datetime(index + 1, 0, week_end)
        sheet.write_number(index + 1, 1, validation)
        sheet.write_number(index + 1, 2, exact)
        sheet.write(index + 1, 3, label)
        sheet.write(index + 1, 4, allocation)
    workbook.close()
    return embedded.getvalue()


def _qsi_subscription_archive(path: Path) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(_QSI_BACKTEST_MEMBER, _qsi_backtest_span_payload())


def _qsi_current_archive(path: Path) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(_QSI_WEEKLY_MEMBER, _qsi_weekly_drops_payload())


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
                    ("data", json.dumps([[0, 100, 100], [1, 101, 101], [-1, 99, 99]])),
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
    assert manifest["builder_version"] == "1.5.0"
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


def test_qsi_archive_binds_backtest_span_role(tmp_path: Path) -> None:
    path = tmp_path / "qsi.zip"
    _qsi_subscription_archive(path)

    weeks_by_role, archive_source, member_sources = history._qsi_archive_weeks(path)

    assert archive_source["source_id"] == "qsi-paid-subscription-archive"
    assert archive_source["archive_roles"] == ["BACKTEST_SPAN"]
    assert (
        archive_source["archive_role_schema_version"] == "qsi-archive-role-binding-v1"
    )
    assert set(weeks_by_role) == {"BACKTEST_SPAN"}
    weeks = weeks_by_role["BACKTEST_SPAN"]
    assert len(weeks) == 500
    assert set(weeks[0]) == {
        "end",
        "exact_return",
        "price_validation_return",
        "start",
        "weights",
    }
    member = member_sources["BACKTEST_SPAN"]
    assert member["source_id"] == "qsi-2013-2024-exact-weekly-model-path"
    assert member["archive_role"] == "BACKTEST_SPAN"


def test_qsi_archive_binds_weekly_drops_role(tmp_path: Path) -> None:
    path = tmp_path / "qsi-2026.zip"
    _qsi_current_archive(path)

    weeks_by_role, archive_source, member_sources = history._qsi_archive_weeks(path)

    assert archive_source["archive_roles"] == ["WEEKLY_DROPS"]
    assert set(weeks_by_role) == {"WEEKLY_DROPS"}
    weeks = weeks_by_role["WEEKLY_DROPS"]
    assert len(weeks) == 30
    assert weeks[0]["weights"] == {
        "SPXL": history.Decimal("0.75"),
        "BIL": history.Decimal("0.25"),
    }
    assert weeks[0]["start"] == weeks[0]["end"] - timedelta(days=4)
    member = member_sources["WEEKLY_DROPS"]
    assert member["source_id"] == "qsi-current-exact-weekly-model-path"
    assert member["archive_role"] == "WEEKLY_DROPS"


def test_qsi_archive_binds_both_roles(tmp_path: Path) -> None:
    path = tmp_path / "qsi-both.zip"
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(_QSI_BACKTEST_MEMBER, _qsi_backtest_span_payload())
        archive.writestr(_QSI_WEEKLY_MEMBER, _qsi_weekly_drops_payload())

    weeks_by_role, archive_source, member_sources = history._qsi_archive_weeks(path)

    assert archive_source["archive_roles"] == ["BACKTEST_SPAN", "WEEKLY_DROPS"]
    assert set(member_sources) == {"BACKTEST_SPAN", "WEEKLY_DROPS"}
    assert len(weeks_by_role["BACKTEST_SPAN"]) == 500
    assert len(weeks_by_role["WEEKLY_DROPS"]) == 30


def test_qsi_archive_without_known_member_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "qsi-unknown.zip"
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("Quanta 72 Paid Sub Files/readme.txt", b"unrelated")

    with pytest.raises(
        history.StrategyHistoryError, match="no known weekly model member"
    ):
        history._qsi_archive_weeks(path)


def test_qsi_archive_ambiguous_members_fail_closed(tmp_path: Path) -> None:
    backtest = tmp_path / "qsi-ambiguous-backtest.zip"
    with ZipFile(backtest, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "A/Stratum_vs_SPY_Weekly_Returns 2013 to 2024.xlsx",
            _qsi_backtest_span_payload(),
        )
        archive.writestr(
            "B/Stratum_vs_SPY_Weekly_Returns 2013 to 2024.xlsx",
            _qsi_backtest_span_payload(),
        )
    with pytest.raises(
        history.StrategyHistoryError, match="ambiguous BACKTEST_SPAN members"
    ):
        history._qsi_archive_weeks(backtest)

    weekly = tmp_path / "qsi-ambiguous-weekly.zip"
    with ZipFile(weekly, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "A/Weekly_File_Drops/stratum_data_combined.xlsx",
            _qsi_weekly_drops_payload(),
        )
        archive.writestr(
            "B/Weekly_File_Drops/stratum_data_combined.xlsx",
            _qsi_weekly_drops_payload(),
        )
    with pytest.raises(
        history.StrategyHistoryError, match="ambiguous WEEKLY_DROPS members"
    ):
        history._qsi_archive_weeks(weekly)


def test_qsi_weekly_drops_output_shape_matches_producer_shape(tmp_path: Path) -> None:
    path = tmp_path / "qsi-current.zip"
    _qsi_current_archive(path)
    legacy = tmp_path / "qsi.zip"
    _qsi_subscription_archive(legacy)

    via_loader = history._qsi_archive_weeks(path)[0]["WEEKLY_DROPS"]
    via_current, _, _ = history._qsi_current_archive_weeks(path)
    via_backtest = history._qsi_archive_weeks(legacy)[0]["BACKTEST_SPAN"]

    assert via_loader == via_current
    assert set(via_loader[0]) == set(via_backtest[0])
    for week in via_loader:
        assert isinstance(week["end"], history.date)
        assert isinstance(week["start"], history.date)
        assert isinstance(week["exact_return"], history.Decimal)
        assert isinstance(week["price_validation_return"], history.Decimal)
        assert isinstance(week["weights"], dict)


def test_qsi_weekly_drops_non_monotone_or_duplicate_dates_fail_closed(
    tmp_path: Path,
) -> None:
    start = datetime(2025, 1, 3)
    ordered = [start + timedelta(days=7 * index) for index in range(30)]
    unordered = tmp_path / "qsi-unordered.zip"
    shuffled = [ordered[1], ordered[0], *ordered[2:]]
    with ZipFile(unordered, "w", ZIP_DEFLATED) as archive:
        archive.writestr(_QSI_WEEKLY_MEMBER, _qsi_weekly_drops_payload(dates=shuffled))
    with pytest.raises(history.StrategyHistoryError, match="not monotone"):
        history._qsi_archive_weeks(unordered)

    duplicated = tmp_path / "qsi-duplicated.zip"
    repeated = [ordered[0], ordered[0], *ordered[2:]]
    with ZipFile(duplicated, "w", ZIP_DEFLATED) as archive:
        archive.writestr(_QSI_WEEKLY_MEMBER, _qsi_weekly_drops_payload(dates=repeated))
    with pytest.raises(history.StrategyHistoryError, match="overlaps itself"):
        history._qsi_archive_weeks(duplicated)


def _qsi_weekly_drops_end_to_end(tmp_path: Path) -> tuple[Path, Path, Path]:
    archive = tmp_path / "qsi-2026.zip"
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        bundle.writestr(
            _QSI_WEEKLY_MEMBER,
            _qsi_weekly_drops_payload(
                start=datetime(2023, 1, 6),
                label="SPXL",
                allocation="1",
                exact=0.01,
                validation=0.01,
            ),
        )
    appendix = tmp_path / "qsi-appendix.xlsx"
    _qsi_appendix(appendix, start=datetime(2023, 3, 3))
    prices = tmp_path / "qsi-prices"
    _qsi_prices(prices)
    return archive, appendix, prices


def test_qsi_weekly_drops_only_archive_bring_lane_current(tmp_path: Path) -> None:
    archive, appendix, prices = _qsi_weekly_drops_end_to_end(tmp_path)

    accepted, definition = history._qsi(
        archive, appendix, prices, as_of_date=history.date(2026, 8, 3)
    )

    assert accepted
    assert definition["exact_weekly_observations"] == 30
    assert definition["exact_weekly_observed_end"] == "2023-07-28"
    revision = definition["history_revision_validation"]
    assert "historical_archive_overlap" not in revision
    assert revision["prior_appendix_overlap"]["status"] == (
        "OVERLAP_VALIDATED_NEWER_AUTHORITY_ACCEPTED"
    )
    artifacts = {item["source_id"]: item for item in definition["source_artifacts"]}
    assert artifacts["qsi-paid-subscription-archive"]["archive_roles"] == [
        "WEEKLY_DROPS"
    ]


def test_qsi_combined_archive_binds_both_roles_end_to_end(tmp_path: Path) -> None:
    archive, appendix, prices = _qsi_weekly_drops_end_to_end(tmp_path)
    with ZipFile(archive, "a", ZIP_DEFLATED) as bundle:
        bundle.writestr(_QSI_BACKTEST_MEMBER, _qsi_backtest_span_payload())

    accepted, definition = history._qsi(
        archive, appendix, prices, as_of_date=history.date(2026, 8, 3)
    )

    assert accepted
    revision = definition["history_revision_validation"]
    historical_overlap = revision["historical_archive_overlap"]
    assert historical_overlap["status"] == "OVERLAP_VALIDATED_NEWER_AUTHORITY_ACCEPTED"
    assert historical_overlap["overlap_observations"] > 0
    assert historical_overlap["max_abs_return_revision"] == 0
    expected_historical = sum(
        (datetime(2013, 10, 25) + timedelta(days=7 * index)).date()
        < history.date(2023, 1, 6)
        for index in range(500)
    )
    assert definition["exact_weekly_observations"] == expected_historical + 30
    artifacts = {item["source_id"]: item for item in definition["source_artifacts"]}
    assert artifacts["qsi-paid-subscription-archive"]["archive_roles"] == [
        "BACKTEST_SPAN",
        "WEEKLY_DROPS",
    ]


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
