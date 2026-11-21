"""EUR -> USD conversion of provider layers with a governed same-date EURUSD series.

Synthetic series only (no owner values).  Each test states what must fail before the module.
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import factor_lens_currency as flc

D = Decimal
FX_CSV = (
    "Ticker,Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits\n"
    "EURUSD=X,2026-01-02,1.10,1.11,1.09,1.1000,1.1000,0,0.0,0.0\n"
    "EURUSD=X,2026-01-05,1.10,1.12,1.10,1.1100,1.1100,0,0.0,0.0\n"
    "EURUSD=X,2026-01-06,1.11,1.12,1.10,1.0989,1.0989,0,0.0,0.0\n"
    "EURUSD=X,2026-01-08,1.10,1.12,1.10,1.1200,1.1200,0,0.0,0.0\n"
)


def _levels(tmp_path: Path, text: str = FX_CSV, sha: str | None = None) -> flc.FxLevels:
    path = tmp_path / "fx.csv"
    path.write_bytes(text.encode("utf-8"))
    digest = sha or hashlib.sha256(text.encode("utf-8")).hexdigest()
    return flc.load_fx_levels(path, expected_sha256=digest, pair="EURUSD=X")


def test_the_fx_file_is_pinned_and_strict(tmp_path: Path) -> None:
    """Fails before: no loader.  A wrong hash, ticker, duplicate or non-positive close refuses."""

    assert _levels(tmp_path).closes[date(2026, 1, 5)] == D("1.1100")
    with pytest.raises(flc.CurrencyError, match="identity"):
        _levels(tmp_path, sha="0" * 64)
    with pytest.raises(flc.CurrencyError, match="ticker"):
        _levels(tmp_path, FX_CSV.replace("EURUSD=X,2026-01-06", "GBPUSD=X,2026-01-06"))
    with pytest.raises(flc.CurrencyError, match="duplicate"):
        _levels(tmp_path, FX_CSV + "EURUSD=X,2026-01-08,1,1,1,1.12,1.12,0,0,0\n")
    with pytest.raises(flc.CurrencyError, match="positive"):
        _levels(tmp_path, FX_CSV.replace("1.0989,1.0989", "0,0"))


def test_a_level_carries_the_last_close_over_a_gap(tmp_path: Path) -> None:
    levels = _levels(tmp_path)
    assert levels.level(date(2026, 1, 7)) == D("1.0989")  # no bar on the 7th
    with pytest.raises(flc.CurrencyError, match="before"):
        levels.level(date(2026, 1, 1))


def test_conversion_compounds_the_return_with_the_same_date_fx_return(
    tmp_path: Path,
) -> None:
    """(1+r)(1+fx_ret)-1 on the period each return covers; round-trips to 1e-12."""

    levels = _levels(tmp_path)
    eur = [(date(2026, 1, 5), D("0.02")), (date(2026, 1, 6), D("-0.01"))]
    usd = flc.convert_returns(eur, levels, base_date=date(2026, 1, 2))
    fx1 = D("1.1100") / D("1.1000") - 1
    fx2 = D("1.0989") / D("1.1100") - 1
    assert usd[0][1] == (1 + D("0.02")) * (1 + fx1) - 1
    assert usd[1][1] == (1 + D("-0.01")) * (1 + fx2) - 1
    back = flc.unconvert_returns(usd, levels, base_date=date(2026, 1, 2))
    for (_, original), (_, restored) in zip(eur, back, strict=True):
        assert abs(original - restored) < D("1e-12")


def test_conversion_refuses_unordered_duplicate_or_early_rows(tmp_path: Path) -> None:
    levels = _levels(tmp_path)
    ok = [(date(2026, 1, 5), D("0.01"))]
    flc.convert_returns(ok, levels, base_date=date(2026, 1, 2))
    for rows, base in (
        (
            [(date(2026, 1, 6), D("0.01")), (date(2026, 1, 5), D("0.01"))],
            date(2026, 1, 2),
        ),
        (
            [(date(2026, 1, 5), D("0.01")), (date(2026, 1, 5), D("0.01"))],
            date(2026, 1, 2),
        ),
        (ok, date(2026, 1, 5)),  # base must precede the first return
        ([(date(2026, 1, 5), D("-1"))], date(2026, 1, 2)),  # a return of -100% or worse
        ([], date(2026, 1, 2)),
    ):
        with pytest.raises(flc.CurrencyError):
            flc.convert_returns(rows, levels, base_date=base)


def test_the_layer_currency_comes_from_the_provider_catalog_and_unknown_refuses() -> (
    None
):
    """Fails before: no reader.  Missing, blank, lowercase or unsupported currencies refuse."""

    assert flc.provider_currency({"series": {"currency": "EUR"}}) == "EUR"
    assert flc.provider_currency({"series": {"currency": "USD"}}) == "USD"
    for catalog in (
        {},
        {"series": {}},
        {"series": {"currency": ""}},
        {"series": {"currency": None}},
        {"series": {"currency": "eur"}},
        {"series": {"currency": "GBP"}},
        {"series": "EUR"},
    ):
        with pytest.raises(flc.CurrencyError, match="currency"):
            flc.provider_currency(catalog)


def test_an_unconverted_usd_labelled_eur_layer_is_refused(tmp_path: Path) -> None:
    """The defect: a EUR provider layer carrying the label USD with no conversion evidence."""

    levels = _levels(tmp_path)
    rows = [(date(2026, 1, 5), D("0.02"))]
    with pytest.raises(flc.CurrencyError, match="unconverted"):
        flc.admit_layer("BACKTEST", rows, provider_currency="EUR", label_currency="USD")
    # converted, it is admitted as USD with its evidence
    converted = flc.admit_layer(
        "BACKTEST",
        rows,
        provider_currency="EUR",
        label_currency="USD",
        fx=levels,
        base_date=date(2026, 1, 2),
    )
    assert converted.currency == "USD" and converted.native_currency == "EUR"
    assert converted.status == flc.CONVERTED
    assert converted.fx_source_sha256 == levels.sha256
    assert converted.rows != tuple(rows)
    # a EUR layer honestly labelled EUR is carried as EUR, never as USD
    native = flc.admit_layer(
        "MODEL_LIVE", rows, provider_currency="EUR", label_currency="EUR"
    )
    assert native.currency == "EUR" and native.status == flc.NATIVE_NOT_CONVERTED
    # a label that matches neither is refused
    with pytest.raises(flc.CurrencyError, match="label"):
        flc.admit_layer("BACKTEST", rows, provider_currency="EUR", label_currency="GBP")


def test_the_account_layer_is_usd_and_never_converted(tmp_path: Path) -> None:
    levels = _levels(tmp_path)
    rows = [(date(2026, 1, 5), D("0.02"))]
    layer = flc.admit_layer(
        "ACCOUNT_LIVE",
        rows,
        provider_currency="USD",
        label_currency="USD",
        fx=levels,
        base_date=date(2026, 1, 2),
    )
    assert layer.rows == tuple(rows) and layer.status == flc.NATIVE_USD
    assert layer.fx_source_sha256 is None
    with pytest.raises(flc.CurrencyError, match="account"):
        flc.admit_layer(
            "ACCOUNT_LIVE", rows, provider_currency="EUR", label_currency="USD"
        )


def test_a_usd_provider_layer_is_not_converted_even_when_fx_is_supplied(
    tmp_path: Path,
) -> None:
    levels = _levels(tmp_path)
    rows = [(date(2026, 1, 5), D("0.02"))]
    layer = flc.admit_layer(
        "BACKTEST",
        rows,
        provider_currency="USD",
        label_currency="USD",
        fx=levels,
        base_date=date(2026, 1, 2),
    )
    assert layer.rows == tuple(rows) and layer.status == flc.NATIVE_USD


def test_a_splice_needs_every_layer_in_usd_and_provider_layers_converted(
    tmp_path: Path,
) -> None:
    """Model-live to account-live seam: the EU provider layers are converted before the
    USD account layer is chained on; an unconverted EUR layer in the chain refuses."""

    levels = _levels(tmp_path)
    kw = {"fx": levels, "base_date": date(2026, 1, 2)}
    backtest = flc.admit_layer(
        "BACKTEST",
        [(date(2026, 1, 5), D("0.02"))],
        provider_currency="EUR",
        label_currency="USD",
        **kw,
    )
    live = flc.admit_layer(
        "MODEL_LIVE",
        [(date(2026, 1, 6), D("0.01"))],
        provider_currency="EUR",
        label_currency="USD",
        **kw,
    )
    account = flc.admit_layer(
        "ACCOUNT_LIVE",
        [(date(2026, 1, 8), D("0.03"))],
        provider_currency="USD",
        label_currency="USD",
    )
    chain = flc.splice_rows([backtest, live, account])
    assert [d for d, _ in chain] == [
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 8),
    ]
    eur_only = flc.admit_layer(
        "MODEL_LIVE",
        [(date(2026, 1, 6), D("0.01"))],
        provider_currency="EUR",
        label_currency="EUR",
    )
    with pytest.raises(flc.CurrencyError, match="splice"):
        flc.splice_rows([backtest, eur_only, account])
    with pytest.raises(flc.CurrencyError, match="overlap"):
        flc.splice_rows([backtest, backtest])
    with pytest.raises(flc.CurrencyError, match="splice"):
        flc.splice_rows([])
