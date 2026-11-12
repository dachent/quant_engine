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


def test_headline_never_from_backtest_or_short_window() -> None:
    assert sl.headline_allowed({sl.ACCOUNT_LIVE}, 400)
    assert sl.headline_allowed({sl.MODEL_LIVE, sl.ACCOUNT_LIVE}, 400)
    assert not sl.headline_allowed({sl.BACKTEST, sl.ACCOUNT_LIVE}, 6000)
    assert not sl.headline_allowed({sl.ACCOUNT_LIVE}, 364)
    assert not sl.headline_allowed({sl.COPY_RECORD}, 400)
    assert not sl.headline_allowed(set(), 400)


def test_one_series_per_block() -> None:
    one = {"series_labels": [_label().to_dict()]}
    assert sl.check_block(one) == []
    assert sl.check_block({"series_labels": [_label().to_dict()] * 2})
    assert sl.check_block({})


def test_forbidden_wording() -> None:
    assert sl.forbidden_wording("Portfolio123 provider-live book: 2026")
    assert not sl.forbidden_wording(sl.LABEL_TEXT[sl.MODEL_LIVE])
