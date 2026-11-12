from __future__ import annotations

import pytest

from tools import series_label as sl


def _label(**kw):
    base = dict(
        layer=sl.MODEL_LIVE,
        entity_id="1853536",
        entity_role="MODEL_BOOK",
        first="2025-05-12",
        last="2026-11-06",
        observations=390,
        currency="USD",
        return_basis="P123_RETURN_INDEX",
    )
    base.update(kw)
    return sl.SeriesLabel(**base)


def test_the_copy_cannot_carry_a_model_or_account_layer_and_cannot_be_a_series() -> (
    None
):
    with pytest.raises(sl.SeriesLabelError):
        _label(entity_id="1913284", entity_role="COPY")  # copy role on MODEL_LIVE
    with pytest.raises(sl.SeriesLabelError):
        _label(layer=sl.ACCOUNT_LIVE)  # model book role on the account layer
    copy = _label(layer=sl.COPY_RECORD, entity_id="1913284", entity_role="COPY")
    with pytest.raises(sl.SeriesLabelError, match="never enter a series"):
        sl.require_series(copy)
    assert sl.check_block({"series_labels": [copy.to_dict()]})


def test_label_round_trips_and_fixed_wording_is_enforced() -> None:
    label = _label()
    assert sl.label_from_dict(label.to_dict()) == label
    tampered = label.to_dict()
    tampered["label_text"] = "Portfolio123 provider-live book"
    with pytest.raises(sl.SeriesLabelError, match="fixed wording"):
        sl.label_from_dict(tampered)


def test_replica_and_gross_labels_say_so() -> None:
    rep = _label(
        layer=sl.BACKTEST,
        entity_id="grizzlybears-vtma",
        entity_role="VENDOR_REPLICA",
        leverage_factor=3,
    )
    assert "3x replica" in rep.label_text
    gross = _label(
        layer=sl.BACKTEST,
        entity_id="robuxio-gav",
        entity_role="VENDOR_SERIES",
        cost_basis="GROSS",
    )
    assert "gross of fees" in gross.label_text


def _seg(layer, role, first, last, **kw):
    return _label(layer=layer, entity_role=role, first=first, last=last, **kw)


def test_headline_never_from_backtest_or_short_window() -> None:
    live = _seg(sl.ACCOUNT_LIVE, "ACCOUNT", "2025-01-01", "2026-02-01")
    model = _seg(sl.MODEL_LIVE, "MODEL_BOOK", "2025-01-01", "2026-02-01")
    back = _seg(sl.BACKTEST, "SIM_TWIN", "2009-01-01", "2024-12-31")
    short = _seg(sl.ACCOUNT_LIVE, "ACCOUNT", "2026-01-01", "2026-06-01")
    copy = _seg(sl.COPY_RECORD, "COPY", "2025-01-01", "2026-02-01")
    assert sl.headline_allowed([live])
    assert sl.headline_allowed([model, live])
    assert not sl.headline_allowed([back, live])  # backtest anywhere in the stitch
    assert not sl.headline_allowed([short])
    assert not sl.headline_allowed([copy])
    assert not sl.headline_allowed([])


def test_one_series_per_block() -> None:
    one = {"series_labels": [_label().to_dict()]}
    assert sl.check_block(one) == []
    assert sl.check_block({"series_labels": [_label().to_dict()] * 2})
    assert sl.check_block({})


def test_forbidden_wording_anywhere_in_a_block_is_refused() -> None:
    block = {
        "series_labels": [_label().to_dict()],
        "note": ["Portfolio123 provider-live book CAGR"],
    }
    assert any("forbidden wording" in p for p in sl.check_block(block))


def test_forbidden_wording() -> None:
    assert sl.forbidden_wording("Portfolio123 provider-live book: 2026")
    assert not sl.forbidden_wording(sl.LABEL_TEXT[sl.MODEL_LIVE])


def test_research_series_and_my_live_series_are_never_mixed() -> None:
    assert sl.MODE_LABELS["LIVE_PLUS_BACKTEST"] == (
        "Research series (backtest + model live)"
    )
    assert sl.MODE_LABELS["LIVE_ONLY"] == "My live"
    # Account never in the research series; model live never in my live.
    assert not sl.kind_allows("LIVE_PLUS_BACKTEST", sl.ACCOUNT_LIVE)
    assert not sl.kind_allows("LIVE_ONLY", sl.MODEL_LIVE)
    assert not sl.kind_allows("LIVE_ONLY", sl.BACKTEST)
    # The copy is in no series kind, with or without a model-live layer.
    for mode in sl.MODE_LABELS:
        assert not sl.kind_allows(mode, sl.COPY_RECORD)
        assert not sl.kind_allows_without_model_live(mode, sl.COPY_RECORD)
    # Strategies with no model-live source keep backtest then account, labelled.
    assert sl.kind_allows_without_model_live("LIVE_PLUS_BACKTEST", sl.ACCOUNT_LIVE)
