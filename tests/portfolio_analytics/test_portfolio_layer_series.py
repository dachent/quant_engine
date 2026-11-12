"""Kill tests for the Robuxio NET/GAV split and the Grizzly replica/vendor split.

Synthetic fixtures only: no vendor value, account value or real file is used.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys

import pytest
import xlsxwriter

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import portfolio_layer_series as layers  # noqa: E402
import series_label  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "portfolio_strategy_history_layers_under_test",
    TOOLS / "portfolio_strategy_history.py",
)
assert _SPEC is not None and _SPEC.loader is not None
history = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = history
_SPEC.loader.exec_module(history)

INCEPTION = date(2025, 8, 14)


def _nav_csv(path: Path, *, gross_column: bool = True) -> Path:
    """A NAV file whose pct column repeats the gross path while balance is net."""

    lines = ["date,daily_change_percent,balance"]
    balance = Decimal("1000")
    day = date(2025, 8, 1)
    for index in range(20):
        gross_pct = Decimal("1.0") + Decimal(index) / Decimal(10)
        # the net balance grows by less than the printed column says
        balance *= Decimal(1) + (gross_pct - Decimal("0.3")) / Decimal(100)
        lines.append(f"{day.isoformat()},{gross_pct},{balance}")
        day += timedelta(days=1)
    path.write_bytes(("\n".join(lines) + "\n").encode())
    return path


def _generation(root: Path) -> Path:
    rows = ["series_id,date,return,evidence_layer,return_basis"]
    day = date(2025, 8, 1)
    for index in range(20):
        gross = (Decimal("1.0") + Decimal(index) / Decimal(10)) / Decimal(100)
        rows.append(f"Robuxio,{day.isoformat()},{gross},PROVIDER_TRACK_RECORD,FIXTURE")
        day += timedelta(days=1)
    content = ("\n".join(rows) + "\n").encode()
    generation = root / "gen-abc"
    generation.mkdir(parents=True)
    (generation / "returns.csv").write_bytes(content)
    manifest = {
        "verification": "VERIFIED",
        "generation_id": "gen-abc",
        "artifacts": [
            {
                "artifact_id": "returns.csv",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        ],
    }
    (generation / "generation.json").write_text(json.dumps(manifest))
    return generation


def _rob_rows(rows: list[dict[str, str]]) -> dict[str, Decimal]:
    return {row["date"]: Decimal(row["return"]) for row in rows}


# ---------------------------------------------------------------- Robuxio NET


def test_net_return_is_the_balance_chain_not_the_pct_column(tmp_path: Path) -> None:
    nav = _nav_csv(tmp_path / "nav.csv")
    values, facts = layers.net_returns_from_nav(nav.read_bytes(), before=INCEPTION)
    parsed = layers.parse_nav_csv(nav.read_bytes())
    for (day, net), previous, current in zip(values, parsed, parsed[1:], strict=False):
        assert day == current[0]
        assert net == current[2] / previous[2] - 1
        # kill: reading the pct column as the return would give the gross path
        assert net != current[1] / 100
    assert facts["daily_change_percent_disagrees_with_balance_days"] == 19
    assert facts["net_return_method"].startswith("NAV_BALANCE_CHAIN")


def test_net_history_ends_strictly_before_model_live_inception(tmp_path: Path) -> None:
    nav = _nav_csv(tmp_path / "nav.csv")
    values, _ = layers.net_returns_from_nav(nav.read_bytes(), before=INCEPTION)
    assert max(day for day, _ in values) == INCEPTION - timedelta(days=1)
    assert all(day < INCEPTION for day, _ in values)
    with pytest.raises(layers.LayerSeriesError, match="no history"):
        layers.net_returns_from_nav(nav.read_bytes(), before=date(2025, 8, 2))


@pytest.mark.parametrize(
    "content, message",
    [
        (b"date,pct,balance\n2025-01-01,0,1\n2025-01-02,0,1\n", "header"),
        (
            b"date,daily_change_percent,balance\n2025-01-02,0,1\n2025-01-01,0,1\n",
            "unordered",
        ),
        (
            b"date,daily_change_percent,balance\n2025-01-01,0,1\n2025-01-02,0,0\n",
            "non-positive",
        ),
        (b"date,daily_change_percent,balance\n2025-01-01,0,1\n", "too short"),
    ],
)
def test_nav_file_is_refused_when_malformed(content: bytes, message: str) -> None:
    with pytest.raises(layers.LayerSeriesError, match=message):
        layers.parse_nav_csv(content)


def test_robuxio_without_the_nav_file_is_labelled_gross(tmp_path: Path) -> None:
    generation = _generation(tmp_path)
    [(rows, definition)] = history._robuxio_series(generation, "v1")
    (label,) = definition["series_labels"]
    assert label["basis"]["cost_basis"] == "GROSS"
    assert series_label.check_block(definition) == []
    assert "gross" in label["label_text"]
    assert len(rows) == 20


def test_robuxio_nav_makes_net_the_research_series_and_gav_a_separate_one(
    tmp_path: Path,
) -> None:
    generation = _generation(tmp_path)
    nav = _nav_csv(tmp_path / "nav.csv")
    parts = history._robuxio_series(
        generation, "v1", net_nav_csv=nav, model_live_inception=INCEPTION
    )
    by_id = {definition["series_id"]: (rows, definition) for rows, definition in parts}
    assert set(by_id) == {
        "Robuxio Crypto (High Sharpe High Vol)",
        "Robuxio Crypto (High Sharpe High Vol) [GAV gross]",
    }
    net_rows, net_definition = by_id["Robuxio Crypto (High Sharpe High Vol)"]
    gav_rows, gav_definition = by_id[
        "Robuxio Crypto (High Sharpe High Vol) [GAV gross]"
    ]
    assert net_definition["series_labels"][0]["basis"]["cost_basis"] == "NET"
    assert gav_definition["series_labels"][0]["basis"]["cost_basis"] == "GROSS"
    assert gav_definition["required_for_analysis"] is False
    for definition in (net_definition, gav_definition):
        assert series_label.check_block(definition) == []
    # never chained: the net line is the balance chain, not the gross path
    net, gav = _rob_rows(net_rows), _rob_rows(gav_rows)
    # the NAV file has no return on its base day; every other day is shared
    assert set(gav) - set(net) == {"2025-08-01"}
    assert all(net[day] != gav[day] for day in net)
    # both layers stop strictly before model-live inception
    assert max(net) == max(gav) == "2025-08-13"
    assert all(row["evidence_layer"] == "BACKTEST" for row in net_rows + gav_rows)
    assert all(
        row["series_id"] == "Robuxio Crypto (High Sharpe High Vol) [GAV gross]"
        for row in gav_rows
    )


def test_robuxio_nav_without_the_generation_is_refused(tmp_path: Path) -> None:
    inputs = _bundle_inputs(tmp_path)
    with pytest.raises(history.StrategyHistoryError, match="GAV series"):
        history.build_bundle(
            **inputs,
            as_of_date="2026-08-03",
            robuxio_net_nav_csv=_nav_csv(tmp_path / "nav.csv"),
        )


# -------------------------------------------------------------------- Grizzly


def _simulation(path: Path) -> None:
    workbook = xlsxwriter.Workbook(path)
    sheet = workbook.add_worksheet("Simulation")
    sheet.write_row(0, 0, ["date", "day_ret"])
    start = datetime(2010, 1, 1)
    for index in range(1_001):
        sheet.write_datetime(index + 1, 0, start + timedelta(days=index))
        sheet.write_number(index + 1, 1, 0.001 if index else 0)
    workbook.close()


def _bundle_inputs(tmp_path: Path) -> dict[str, Path]:
    # build_bundle needs the other retained sources; reuse the strategy-history
    # test's fixtures instead of duplicating them.
    module_path = Path(__file__).with_name("test_portfolio_strategy_history.py")
    spec = importlib.util.spec_from_file_location("_sh_fixtures", module_path)
    assert spec is not None and spec.loader is not None
    fixtures = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fixtures
    spec.loader.exec_module(fixtures)
    return fixtures._bundle_inputs(tmp_path)


def _trade_files(tmp_path: Path) -> tuple[Path, Path, list[Decimal], int]:
    header = "Date (ET),Action,SPX,Prev Price,Profit / Loss,PnL %,Model,Buy & Hold"
    blended = [header]
    levels: list[Decimal] = []
    model = Decimal("100")
    start = datetime(2010, 1, 4, 11, 0)
    all_rows = []
    for index in range(130):
        model *= Decimal("1.01") if index % 3 else Decimal("0.995")
        levels.append(model)
        action = "BUY" if index % 2 == 0 else "SELL"
        stamp = (start + timedelta(days=index)).strftime("%Y-%m-%d %H:%M ET")
        all_rows.append(f"{stamp},{action},1000,999,1,0.1,{model},100")
    boundary = 25
    live = [header, *all_rows[boundary:]]
    blended = [header, *all_rows]
    (tmp_path / "blended.csv").write_text("\n".join(blended) + "\n")
    (tmp_path / "live.csv").write_text("\n".join(live) + "\n")
    return tmp_path / "blended.csv", tmp_path / "live.csv", levels, boundary


def test_vendor_basis_is_its_own_1x_series_ending_before_vendor_live(
    tmp_path: Path,
) -> None:
    blended, live, levels, boundary = _trade_files(tmp_path)
    rows, definition = history._grizzly_vendor_basis(blended, live)

    assert definition["series_id"] == "GrizzlyBears VTMA [vendor 1x]"
    assert definition["series_id"] != "GrizzlyBears VTMA"
    (label,) = definition["series_labels"]
    assert label["basis"]["leverage_factor"] == 1
    assert label["entity_role"] == "VENDOR_SERIES"
    assert definition["display_name"] == "Vendor basis 1x (vendor-stated)"
    assert series_label.check_block(definition) == []
    # the line is the vendor's model level ratio, event to event
    growth = Decimal(1)
    for row in rows:
        growth *= 1 + Decimal(row["return"])
    assert abs(growth - levels[boundary - 1] / levels[0]) < Decimal("1e-20")
    # events from the vendor's first live-only record are not backtest
    assert max(row["date"] for row in rows) < definition["vendor_live_boundary"][:10]


def test_vendor_basis_needs_both_trade_files(tmp_path: Path) -> None:
    inputs = _bundle_inputs(tmp_path)
    with pytest.raises(history.StrategyHistoryError, match="trade files"):
        history.build_bundle(
            **inputs, as_of_date="2026-08-03", grizzly_vendor_basis_series=True
        )


def test_replica_is_labelled_3x_hypothetical_and_not_vendor_basis(
    tmp_path: Path,
) -> None:
    path = tmp_path / "spxl.xlsx"
    _simulation(path)
    rows, definition = history._grizzly(path)
    (label,) = definition["series_labels"]
    assert label["basis"]["leverage_factor"] == 3
    assert label["entity_role"] == "VENDOR_REPLICA"
    assert definition["display_name"] == "3x SPXL replica (hypothetical)"
    assert definition["replica_of"]["instrument"] == "SPXL"
    assert definition["replica_of"]["vendor_basis_series_id"] == (
        "GrizzlyBears VTMA [vendor 1x]"
    )
    assert "3x replica" in label["label_text"]
    assert label["layer"] == "BACKTEST"
    assert series_label.check_block(definition) == []
    assert len(definition["segment_labels"]) == 1
    assert len(rows) == 1_001


def test_derived_tail_has_its_own_segment_label() -> None:
    rows = [
        {"date": (date(2025, 5, 1) + timedelta(days=i)).isoformat()} for i in range(10)
    ]
    definition = history._label_grizzly_replica(
        {"series_id": "GrizzlyBears VTMA"},
        rows,
        exact_end=date(2025, 5, 6),
        derived_window=(date(2025, 5, 7), date(2025, 5, 10)),
    )
    exact, derived = definition["segment_labels"]
    assert exact["window"] == {
        "first": "2025-05-01",
        "last": "2025-05-06",
        "observations": 6,
    }
    assert derived["window"]["first"] == "2025-05-07"
    assert derived["window"]["observations"] == 4
    assert derived["basis"]["return_basis"] == (
        "DERIVED_SIGNAL_STATE_TIMES_SPXL_ADJ_CLOSE_NOT_VENDOR_PUBLISHED"
    )
    assert exact["basis"]["return_basis"] != derived["basis"]["return_basis"]
    assert "DERIVED" in definition["series_labels"][0]["basis"]["return_basis"]


# --------------------------------------------------------- bundle integration


def test_labels_follow_the_live_inception_cap_through_build_bundle(
    tmp_path: Path,
) -> None:
    inputs = _bundle_inputs(tmp_path)
    cap = date(2012, 1, 1)
    returns, manifest = history.build_bundle(
        **inputs,
        as_of_date="2026-08-03",
        live_inceptions={"GrizzlyBears VTMA": cap.isoformat()},
    )
    payload = json.loads(manifest)
    definition = next(
        item for item in payload["series"] if item["series_id"] == "GrizzlyBears VTMA"
    )
    kept = [
        row
        for row in csv.DictReader(io.StringIO(returns.decode()))
        if row["series_id"] == "GrizzlyBears VTMA"
    ]
    window = definition["series_labels"][0]["window"]
    assert window == {
        "first": kept[0]["date"],
        "last": kept[-1]["date"],
        "observations": len(kept),
    }
    assert date.fromisoformat(window["last"]) < cap
    segment = definition["segment_labels"][0]["window"]
    assert segment["last"] == kept[-1]["date"]
    assert segment["observations"] == len(kept)


def test_bundle_without_the_opt_in_inputs_adds_no_series(tmp_path: Path) -> None:
    returns, manifest = history.build_bundle(
        **_bundle_inputs(tmp_path), as_of_date="2026-08-03"
    )
    ids = {item["series_id"] for item in json.loads(manifest)["series"]}
    assert not any("[GAV" in item or "[vendor 1x]" in item for item in ids)


# ------------------------------------------- model-live layer survives the boundary


def test_nav_file_stopping_far_before_model_live_is_refused(tmp_path: Path) -> None:
    """Kill test (Grok 8): a chain that ends months early is not 'strictly before'."""

    nav = _nav_csv(tmp_path / "nav.csv")
    with pytest.raises(layers.LayerSeriesError, match="before model-live inception"):
        layers.net_returns_from_nav(nav.read_bytes(), before=date(2025, 11, 1))
    layers.require_reaches(date(2025, 8, 10), INCEPTION, "x")
    with pytest.raises(layers.LayerSeriesError):
        layers.require_reaches(date(2025, 8, 1), INCEPTION, "x")


def test_a_segment_label_can_name_the_model_live_layer() -> None:
    label = layers.series_label(
        entity_id="matrixport product NAV",
        entity_role="PRODUCT_NAV",
        first="2025-08-14",
        last="2025-09-01",
        observations=10,
        return_basis="PRODUCT_NAV_BALANCE_CHAIN",
        cost_basis="NET",
        layer="MODEL_LIVE",
    )
    assert label.layer == "MODEL_LIVE"
    assert (
        layers.series_label(
            entity_id="e",
            entity_role="VENDOR_SERIES",
            first="2025-01-01",
            last="2025-01-02",
            observations=2,
            return_basis="b",
            cost_basis="NET",
        ).layer
        == "BACKTEST"
    )
    with pytest.raises(layers.LayerSeriesError):
        layers.series_label(
            entity_id="e",
            entity_role="VENDOR_SERIES",
            first="2025-01-01",
            last="2025-01-02",
            observations=2,
            return_basis="b",
            cost_basis="NET",
            layer="PROVIDER_LIVE",
        )


def _mixed_rows() -> list[dict[str, str]]:
    return [
        *(
            {"date": f"2025-08-{day:02d}", "evidence_layer": "BACKTEST"}
            for day in range(1, 14)
        ),
        *(
            {"date": f"2025-08-{day:02d}", "evidence_layer": "MODEL_LIVE"}
            for day in range(14, 21)
        ),
    ]


def test_refresh_keeps_a_model_live_segment_apart_from_the_backtest_one() -> None:
    """Kill test (Gemini 4): refitting never folds MODEL_LIVE rows into BACKTEST."""

    backtest = layers.series_label(
        entity_id="e",
        entity_role="VENDOR_SERIES",
        first="2025-08-01",
        last="2025-08-13",
        observations=13,
        return_basis="b",
        cost_basis="NET",
    )
    model = layers.series_label(
        entity_id="e",
        entity_role="PRODUCT_NAV",
        first="2025-08-14",
        last="2025-08-20",
        observations=7,
        return_basis="b",
        cost_basis="NET",
        layer="MODEL_LIVE",
    )
    definition = layers.attach_label(
        {"series_id": "s"}, backtest, segments=[backtest, model]
    )
    layers.refresh_labels(definition, _mixed_rows())
    first, second = definition["segment_labels"]
    assert (first["layer"], first["window"]["last"]) == ("BACKTEST", "2025-08-13")
    assert first["window"]["observations"] == 13
    assert (second["layer"], second["window"]["first"]) == ("MODEL_LIVE", "2025-08-14")
    assert second["window"]["observations"] == 7
    assert definition["series_labels"][0]["window"]["last"] == "2025-08-20"


def test_account_boundary_never_deletes_a_model_live_labelled_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill test (Grok 8): the boundary cuts hypothetical rows, not the model-live NAV."""

    rows = [
        {
            "series_id": "Robuxio Crypto (High Sharpe High Vol)",
            "date": (date(2025, 11, 20) + timedelta(days=index)).isoformat(),
            "return": "0.001",
            "evidence_layer": "MODEL_LIVE",
            "strategy_version": "v1",
        }
        for index in range(30)
    ]
    definition = history._definition(
        series_id="Robuxio Crypto (High Sharpe High Vol)",
        version="v1",
        rows=rows,
        history_status="ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
        compatibility_basis="TEST",
        frequency="DAILY_CONTINUOUS",
        implementation_status="TEST",
        method="TEST",
        source={"sha256": "0" * 64, "size_bytes": 1, "source_id": "test"},
    )
    model = layers.series_label(
        entity_id="matrixport product NAV",
        entity_role="PRODUCT_NAV",
        first=rows[0]["date"],
        last=rows[-1]["date"],
        observations=len(rows),
        return_basis="PRODUCT_NAV_BALANCE_CHAIN",
        cost_basis="NET",
        layer="MODEL_LIVE",
    )
    layers.attach_label(definition, model, segments=[model])
    monkeypatch.setattr(
        history, "_robuxio_series", lambda *args, **kwargs: [(rows, definition)]
    )
    inputs = _bundle_inputs(tmp_path)
    returns, manifest = history.build_bundle(
        **inputs,
        as_of_date="2026-08-03",
        robuxio_track_record_generation=tmp_path,
        robuxio_strategy_version="v1",
        live_inceptions={"Robuxio Crypto (High Sharpe High Vol)": "2025-12-07"},
    )
    kept = [
        row
        for row in csv.DictReader(io.StringIO(returns.decode()))
        if row["series_id"] == "Robuxio Crypto (High Sharpe High Vol)"
    ]
    assert len(kept) == 30
    assert {row["evidence_layer"] for row in kept} == {"MODEL_LIVE"}
    payload = json.loads(manifest)
    out = next(
        item
        for item in payload["series"]
        if item["series_id"] == "Robuxio Crypto (High Sharpe High Vol)"
    )
    assert out["series_labels"][0]["layer"] == "MODEL_LIVE"
    assert out["series_labels"][0]["window"]["observations"] == 30


def test_vendor_basis_label_states_the_per_day_compounding(tmp_path: Path) -> None:
    """Kill test (Grok 7): the line compounds same-day events, so the label says so."""

    blended, live, _levels, _boundary = _trade_files(tmp_path)
    _rows, definition = history._grizzly_vendor_basis(blended, live)
    assert "COMPOUNDED_PER_CALENDAR_DAY" in definition["method"]
    assert "COMPOUNDED_PER_CALENDAR_DAY" in (
        definition["series_labels"][0]["basis"]["return_basis"]
    )
    assert "BETWEEN_TRADE_EVENTS" not in json.dumps(definition)
