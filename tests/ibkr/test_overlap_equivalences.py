"""Governed overlap equivalences: exact, derived, history wins, and nothing else is relaxed."""

from __future__ import annotations

import copy
from datetime import date
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from quant_engine.ibkr import generation as gen
from quant_engine.ibkr.flex import parse_flex_file

import ibkr_overlap_equivalences as tool

HERE = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "tibkr_generation_helpers", HERE / "test_ibkr_generation.py"
)
assert _SPEC is not None and _SPEC.loader is not None
helpers = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("tibkr_generation_helpers", helpers)
_SPEC.loader.exec_module(helpers)

ACCOUNT = "SYNTH-RAW"
DAY = date(2026, 1, 8)


def _cash(currency: str, mtd: str | None, ytd: str | None, *, ending: str = "0") -> str:
    level = "BaseCurrency" if currency == "BASE_SUMMARY" else "Currency"
    parts = [
        f'accountId="{ACCOUNT}" currency="{currency}" levelOfDetail="{level}"',
        'fromDate="20260108" toDate="20260108"',
        f'endingCash="{ending}"',
    ]
    if mtd is not None:
        parts.append(f'brokerInterestMTD="{mtd}"')
    if ytd is not None:
        parts.append(f'brokerInterestYTD="{ytd}"')
    return "<CashReportCurrency " + " ".join(parts) + " />"


def _accrual(currency: str, accrued: str, reversal: str) -> str:
    return (
        f'<InterestAccrualsCurrency accountId="{ACCOUNT}" currency="{currency}" '
        f'fromDate="20260108" toDate="20260108" interestAccrued="{accrued}" '
        f'accrualReversal="{reversal}" />'
    )


def _statement(
    tmp_path: Path,
    name: str,
    *,
    cash: list[str],
    accruals: list[str] | None = None,
    nav: str = "107",
) -> Any:
    sections = "<CashReport>" + "".join(cash) + "</CashReport>"
    if accruals is not None:
        sections += "<InterestAccruals>" + "".join(accruals) + "</InterestAccruals>"
    path = tmp_path / name
    path.write_text(
        helpers._xml(ACCOUNT, DAY, nav, native_sections=sections), encoding="utf-8"
    )
    return parse_flex_file(path, artifact_id=f"private/{name}").statements[0]


def _entry(**over: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "raw_account_id": ACCOUNT,
        "report_date": DAY.isoformat(),
        "family": "CASH_REPORT_PERIOD_AGGREGATES",
        "currency": "USD",
        "level_of_detail": "Currency",
        "history": {"broker_interest_mtd": "-0.09", "broker_interest_ytd": "-0.09"},
        "refresh": {"broker_interest_mtd": None, "broker_interest_ytd": None},
        "reason": "one-day pull cannot carry period-to-date interest",
    }
    entry.update(over)
    return entry


def _merge(
    tmp_path: Path,
    history: Any,
    refresh: Any,
    listed: list[dict[str, Any]],
) -> list[Any]:
    """Run the builder's own merge over two real parsed statements."""

    h, r = tmp_path / "h.xml", tmp_path / "r.xml"
    # a statement file round-trips through the parser; reuse the originals' bytes
    h.write_bytes(history["bytes"])
    r.write_bytes(refresh["bytes"])
    sources = [
        ("private/h.xml", "PRIMARY_FLEX_ACTIVITY", h),
        ("private/r.xml", "REFRESH_FLEX_ACTIVITY", r),
    ]
    statements, _accounts, _count = gen._statements(
        sources, gen._parse_overlap_equivalences(listed)
    )
    return statements


def _pair(
    tmp_path: Path,
    history_cash: list[str],
    refresh_cash: list[str],
    *,
    history_accruals: list[str] | None = None,
    refresh_accruals: list[str] | None = None,
    refresh_nav: str = "107",
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    for name, cash, accruals, nav in (
        ("h-src.xml", history_cash, history_accruals, "107"),
        ("r-src.xml", refresh_cash, refresh_accruals, refresh_nav),
    ):
        sections = "<CashReport>" + "".join(cash) + "</CashReport>"
        if accruals is not None:
            sections += "<InterestAccruals>" + "".join(accruals) + "</InterestAccruals>"
        (tmp_path / name).write_text(
            helpers._xml(ACCOUNT, DAY, nav, native_sections=sections), encoding="utf-8"
        )
    return (
        {"bytes": (tmp_path / "h-src.xml").read_bytes()},
        {"bytes": (tmp_path / "r-src.xml").read_bytes()},
    )


HISTORY_CASH = [_cash("USD", "-0.09", "-0.09", ending="5")]
REFRESH_CASH = [_cash("USD", None, None, ending="5")]


def test_a_listed_period_aggregate_difference_is_accepted_and_history_wins(
    tmp_path: Path,
) -> None:
    history, refresh = _pair(tmp_path, HISTORY_CASH, REFRESH_CASH)
    merged = _merge(tmp_path, history, refresh, [_entry()])
    assert len(merged) == 1
    kept = merged[0].cash_reports[0]
    assert str(kept.broker_interest_mtd) == "-0.09"  # the accepted history is kept


def test_an_unlisted_difference_still_refuses(tmp_path: Path) -> None:
    history, refresh = _pair(tmp_path, HISTORY_CASH, REFRESH_CASH)
    with pytest.raises(
        gen.IbkrGenerationError, match="no declared overlap equivalence names"
    ):
        _merge(tmp_path, history, refresh, [])
    # listing some other tuple does not cover this one
    with pytest.raises(gen.IbkrGenerationError):
        _merge(tmp_path, history, refresh, [_entry(currency="EUR")])


def test_a_listed_tuple_with_a_changed_value_refuses(tmp_path: Path) -> None:
    history, refresh = _pair(tmp_path, HISTORY_CASH, REFRESH_CASH)
    wrong = _entry(
        history={"broker_interest_mtd": "-0.10", "broker_interest_ytd": "-0.09"}
    )
    with pytest.raises(gen.IbkrGenerationError):
        _merge(tmp_path, history, refresh, [wrong])
    wrong_refresh = _entry(
        refresh={"broker_interest_mtd": "0", "broker_interest_ytd": None}
    )
    with pytest.raises(gen.IbkrGenerationError):
        _merge(tmp_path, history, refresh, [wrong_refresh])


def test_a_difference_outside_the_two_families_refuses_even_with_tuples_listed(
    tmp_path: Path,
) -> None:
    history, refresh = _pair(tmp_path, HISTORY_CASH, REFRESH_CASH, refresh_nav="999")
    with pytest.raises(gen.IbkrGenerationError, match="conflicts with accepted"):
        _merge(tmp_path, history, refresh, [_entry()])
    # a changed non-aggregate cash figure is not the aggregate family either
    history, refresh = _pair(
        tmp_path, HISTORY_CASH, [_cash("USD", None, None, ending="6")]
    )
    with pytest.raises(gen.IbkrGenerationError, match="conflicts with accepted"):
        _merge(tmp_path, history, refresh, [_entry()])


def test_a_tuple_for_an_account_or_day_outside_the_overlap_refuses(
    tmp_path: Path,
) -> None:
    history, refresh = _pair(tmp_path, HISTORY_CASH, REFRESH_CASH)
    for stray in (
        _entry(raw_account_id="OTHER"),
        _entry(report_date="2026-01-07"),
    ):
        with pytest.raises(gen.IbkrGenerationError, match="matches no difference"):
            _merge(tmp_path, history, refresh, [_entry(), stray])
    # and a tuple for an overlap that carries no difference at all
    same, same_refresh = _pair(tmp_path, HISTORY_CASH, HISTORY_CASH)
    with pytest.raises(gen.IbkrGenerationError, match="matches no difference"):
        _merge(tmp_path, same, same_refresh, [_entry()])


def test_an_interest_accrual_equivalence_needs_an_identical_net(tmp_path: Path) -> None:
    history_rows = [_accrual("SEK", "-7.74", "2.13")]
    refresh_rows = [_accrual("SEK", "0", "-5.61")]
    history, refresh = _pair(
        tmp_path,
        HISTORY_CASH,
        HISTORY_CASH,
        history_accruals=history_rows,
        refresh_accruals=refresh_rows,
    )
    entry = {
        **_entry(),
        "family": "INTEREST_ACCRUAL_DAY",
        "currency": "",
        "level_of_detail": "",
        "history": [
            {"currency": "SEK", "interest_accrued": "-7.74", "accrual_reversal": "2.13"}
        ],
        "refresh": [
            {"currency": "SEK", "interest_accrued": "0", "accrual_reversal": "-5.61"}
        ],
    }
    assert len(_merge(tmp_path, history, refresh, [entry])) == 1
    # the statement pair differs in net: refused by the builder even with a matching tuple
    history, refresh = _pair(
        tmp_path,
        HISTORY_CASH,
        HISTORY_CASH,
        history_accruals=[_accrual("SEK", "-7.74", "2.13")],
        refresh_accruals=[_accrual("SEK", "0", "-5.00")],
    )
    with pytest.raises(gen.IbkrGenerationError, match="conflicts with accepted"):
        _merge(tmp_path, history, refresh, [entry])
    # and a LISTED tuple whose own net differs never parses
    bad = copy.deepcopy(entry)
    bad["refresh"][0]["accrual_reversal"] = "-5.00"
    with pytest.raises(gen.IbkrGenerationError, match="identical net"):
        gen._parse_overlap_equivalences([bad])


def test_a_history_only_row_must_hold_nothing_but_period_aggregates(
    tmp_path: Path,
) -> None:
    aggregates_only = _cash("EUR", "-0.35", "-0.35")
    history, refresh = _pair(
        tmp_path, [*HISTORY_CASH, aggregates_only], HISTORY_CASH[:1]
    )
    row_entry = _entry(
        family="CASH_REPORT_AGGREGATES_ONLY_ROW",
        currency="EUR",
        history={"broker_interest_mtd": "-0.35", "broker_interest_ytd": "-0.35"},
        refresh=None,
    )
    # the USD row also loses nothing here: only the EUR row is history-only
    history, refresh = _pair(
        tmp_path,
        [*HISTORY_CASH, aggregates_only],
        [_cash("USD", "-0.09", "-0.09", ending="5")],
    )
    assert len(_merge(tmp_path, history, refresh, [row_entry])) == 1
    # a history-only row that carries a balance is real data the pull lost: refused
    with_balance = _cash("EUR", "-0.35", "-0.35", ending="12")
    history, refresh = _pair(
        tmp_path,
        [*HISTORY_CASH, with_balance],
        [_cash("USD", "-0.09", "-0.09", ending="5")],
    )
    with pytest.raises(gen.IbkrGenerationError, match="conflicts with accepted"):
        _merge(tmp_path, history, refresh, [row_entry])
    # a row only the refresh carries is refused too
    history, refresh = _pair(
        tmp_path,
        [_cash("USD", "-0.09", "-0.09", ending="5")],
        [*HISTORY_CASH, _cash("EUR", None, None)],
    )
    with pytest.raises(gen.IbkrGenerationError):
        _merge(tmp_path, history, refresh, [row_entry])


def test_the_listing_is_strict() -> None:
    good = _entry()
    assert gen._parse_overlap_equivalences([good])
    for bad in (
        {**good, "extra": 1},
        {k: v for k, v in good.items() if k != "reason"},
        {**good, "family": "ANYTHING"},
        {**good, "history": {"broker_interest_mtd": 1, "broker_interest_ytd": None}},
        {**good, "report_date": "not-a-date"},
        {**good, "family": "CASH_REPORT_AGGREGATES_ONLY_ROW"},  # refresh must be null
    ):
        with pytest.raises(gen.IbkrGenerationError):
            gen._parse_overlap_equivalences([bad])
    with pytest.raises(gen.IbkrGenerationError, match="listed twice"):
        gen._parse_overlap_equivalences([good, copy.deepcopy(good)])
    assert gen._parse_overlap_equivalences(None) == {}


def test_the_generator_derives_exactly_what_the_builder_then_accepts(
    tmp_path: Path,
) -> None:
    primary, refresh = tmp_path / "p.xml", tmp_path / "r.xml"
    # the primary of the day carries the aggregates; the refresh of the same day does not
    for path, cash in ((primary, HISTORY_CASH), (refresh, REFRESH_CASH)):
        path.write_text(
            helpers._xml(
                ACCOUNT,
                DAY,
                "107",
                native_sections="<CashReport>" + "".join(cash) + "</CashReport>",
            ),
            encoding="utf-8",
        )
    sources = [
        ("private/p.xml", "PRIMARY_FLEX_ACTIVITY", primary),
        ("private/r.xml", "REFRESH_FLEX_ACTIVITY", refresh),
    ]
    entries = tool.derive_from_sources(sources)
    assert [(e["family"], e["currency"]) for e in entries] == [
        ("CASH_REPORT_PERIOD_AGGREGATES", "USD")
    ]
    # deterministic bytes, and the builder's merge accepts exactly this list and nothing less
    first = tool.canonical_bytes(entries)
    assert first == tool.canonical_bytes(tool.derive_from_sources(sources))
    statements, _accounts, count = gen._statements(
        sources, gen._parse_overlap_equivalences(json.loads(first))
    )
    assert count == 1 and len(statements) == 1
    with pytest.raises(gen.IbkrGenerationError):
        gen._statements(sources, {})
