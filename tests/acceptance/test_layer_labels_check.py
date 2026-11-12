"""Row family layers.* (sprint-p123): check id layers.series_blocks and its three rows."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

from acceptance.validate import checks
from tools.series_label import BACKTEST, MODEL_LIVE, SeriesLabel


def _label(layer: str = MODEL_LIVE, first: str = "2025-05-12") -> dict[str, Any]:
    role = "SIM_TWIN" if layer == BACKTEST else "MODEL_BOOK"
    return SeriesLabel(
        layer, "book-1", role, first, "2026-06-01", 52, "USD", "NET_TWR"
    ).to_dict()


def _rows(report: dict[str, Any]) -> list[str]:
    ctx = checks.Context(
        gen=SimpleNamespace(report=report, panel={}),  # type: ignore[arg-type]
        cutoff=date(2026, 5, 11),
        cutoff_source="test",
    )
    return [f.check_id for f in checks.check_layer_labels(ctx)]


def _block(**extra: Any) -> dict[str, Any]:
    return {"series_points": [1], "series_labels": [_label()], **extra}


def test_a_payload_without_series_blocks_is_clean() -> None:
    assert _rows({"strategies": [{"id": "x"}]}) == []


def test_a_labelled_single_series_passes() -> None:
    assert _rows({"a": [_block()]}) == []


def test_a_block_without_a_label_fails() -> None:
    assert _rows({"a": {"series_points": [1]}}) == ["layers.series_label"]


def test_a_block_with_two_series_fails() -> None:
    block = _block()
    block["series_labels"].append(_label(BACKTEST, "2024-01-01"))
    assert _rows({"a": block}) == ["layers.one_series"]


def test_a_headline_over_backtest_fails_and_over_model_live_passes() -> None:
    bad = {"series_points": [1], "series_labels": [_label(BACKTEST, "2020-01-01")]}
    bad["annualized_headline"] = 0.12
    assert _rows({"a": bad}) == ["layers.backtest_headline"]
    assert _rows({"a": _block(annualized_headline=0.1)}) == []
    short = _block(annualized_headline=0.1)
    short["series_labels"][0]["window"]["first"] = "2026-03-01"
    assert _rows({"a": short}) == ["layers.backtest_headline"]


def test_a_block_with_labels_and_no_points_is_checked() -> None:
    """The history bundle's definitions carry labels and no series_points."""

    assert _rows({"a": {"series_labels": [_label()]}}) == []
    assert _rows({"a": {"series_labels": []}}) == ["layers.series_label"]
    two = {"series_labels": [_label(), _label(BACKTEST, "2024-01-01")]}
    assert _rows({"a": two}) == ["layers.one_series"]


def test_the_annualised_keys_the_report_emits_are_judged_too() -> None:
    for key in (
        "account_live_cagr",
        "annualized_return_on_net_capital",
        "cagr",
    ):
        bad = {"series_labels": [_label(BACKTEST, "2020-01-01")], key: 0.12}
        assert _rows({"a": bad}) == ["layers.backtest_headline"], key
        assert _rows({"a": {"series_labels": [_label()], key: 0.1}}) == [], key


def test_a_backtest_segment_beside_a_model_live_series_label_blocks_the_headline() -> (
    None
):
    block = {
        "series_labels": [_label(MODEL_LIVE, "2025-05-12")],
        "segment_labels": [_label(BACKTEST, "2020-01-01"), _label()],
        "cagr": 0.1,
    }
    assert _rows({"a": block}) == ["layers.backtest_headline"]
    block["segment_labels"] = [_label()]
    assert _rows({"a": block}) == []
