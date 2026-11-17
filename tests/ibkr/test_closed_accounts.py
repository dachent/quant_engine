"""The broker's own AccountInformation dateClosed reaches the catalog, checked against the data."""

from __future__ import annotations

import dataclasses
from datetime import date
import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest

from quant_engine.ibkr import generation as gen
from quant_engine.ibkr.flex import FlexValidationError, parse_flex_file

HERE = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "tibkr_generation_helpers2", HERE / "test_ibkr_generation.py"
)
assert _SPEC is not None and _SPEC.loader is not None
helpers = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("tibkr_generation_helpers2", helpers)
_SPEC.loader.exec_module(helpers)

RAW = "SYNTH-RAW"
XWALK = {RAW: "account-x"}


def _statement(
    tmp_path: Path,
    day: date,
    closed: str | None,
    *,
    nav: str = "0",
    generated: date | None = None,
) -> Any:
    text = helpers._xml(RAW, day, nav)
    plain = f'<AccountInformation accountId="{RAW}" currency="USD" />'
    assert plain in text
    if closed is not None:
        text = text.replace(
            plain,
            f'<AccountInformation accountId="{RAW}" currency="USD" dateClosed="{closed}" />',
        )
    path = tmp_path / f"s-{day.isoformat()}-{nav}.xml"
    path.write_text(text, encoding="utf-8")
    statement = parse_flex_file(path, artifact_id=f"private/{path.name}").statements[0]
    # no activity on a closed account's statement; the helper's fixture rows are dropped
    return dataclasses.replace(
        statement,
        positions=(),
        trades=(),
        cash_transactions=(),
        transfers=(),
        corporate_actions=(),
        when_generated=generated,
    )


def test_an_empty_date_closed_is_not_a_closure_and_a_date_is(tmp_path: Path) -> None:
    open_ = _statement(tmp_path, date(2026, 1, 5), None)
    closed = _statement(tmp_path, date(2026, 1, 6), "20260105")
    assert open_.account_date_closed is None
    assert closed.account_date_closed == date(2026, 1, 5)
    assert gen._closed_accounts([open_], XWALK) == []
    assert gen._closed_accounts([open_, closed], XWALK) == [
        {"account_id": "account-x", "closed_date": "2026-01-05"}
    ]


@pytest.mark.parametrize(
    "value", ["not-a-date", "20260611junk", "2026-0611", "20260611;1"]
)
def test_an_unparseable_or_padded_date_closed_refuses(
    tmp_path: Path, value: str
) -> None:
    with pytest.raises(FlexValidationError):
        _statement(tmp_path, date(2026, 1, 5), value)


def test_conflicting_closure_dates_refuse(tmp_path: Path) -> None:
    a = _statement(tmp_path, date(2026, 1, 5), "20260105")
    b = dataclasses.replace(
        _statement(tmp_path, date(2026, 1, 6), "20260105"),
        account_date_closed=date(2026, 1, 6),
    )
    with pytest.raises(gen.IbkrGenerationError, match="different account closure"):
        gen._closed_accounts([a, b], XWALK)


def test_an_earlier_pull_without_the_date_is_not_a_contradiction(
    tmp_path: Path,
) -> None:
    closed = _statement(
        tmp_path, date(2026, 1, 5), "20260105", generated=date(2026, 9, 1)
    )
    early = _statement(
        tmp_path, date(2026, 1, 4), None, nav="100", generated=date(2026, 1, 5)
    )
    assert gen._closed_accounts([early, closed], XWALK) == [
        {"account_id": "account-x", "closed_date": "2026-01-05"}
    ]


def test_a_statement_pulled_after_the_closure_must_state_it(tmp_path: Path) -> None:
    closed = _statement(
        tmp_path, date(2026, 1, 5), "20260105", generated=date(2026, 9, 1)
    )
    later = _statement(tmp_path, date(2026, 1, 6), None, generated=date(2026, 9, 1))
    with pytest.raises(gen.IbkrGenerationError, match="pulled after the stated"):
        gen._closed_accounts([closed, later], XWALK)


def test_an_undated_post_closure_statement_without_a_pull_day_refuses(
    tmp_path: Path,
) -> None:
    closed = _statement(
        tmp_path, date(2026, 1, 5), "20260105", generated=date(2026, 9, 1)
    )
    unknown_pull = _statement(tmp_path, date(2026, 1, 6), None, generated=None)
    with pytest.raises(gen.IbkrGenerationError, match="pulled after the stated"):
        gen._closed_accounts([closed, unknown_pull], XWALK)


def test_value_or_activity_after_the_stated_closure_refuses(tmp_path: Path) -> None:
    closed = _statement(tmp_path, date(2026, 1, 5), "20260105")
    funded = _statement(tmp_path, date(2026, 1, 6), "20260105", nav="5000")
    with pytest.raises(gen.IbkrGenerationError, match="activity or value after"):
        gen._closed_accounts([closed, funded], XWALK)
    traded = dataclasses.replace(
        _statement(tmp_path, date(2026, 1, 7), "20260105"),
        trades=(object(),),  # type: ignore[arg-type]
    )
    with pytest.raises(gen.IbkrGenerationError, match="activity or value after"):
        gen._closed_accounts([closed, traded], XWALK)
    # sub-cent residual interest after the closure date is settled value, not activity
    residual = _statement(tmp_path, date(2026, 1, 6), "20260105", nav="0.000632")
    assert gen._closed_accounts([closed, residual], XWALK)


def test_a_closure_date_outside_the_statements_of_the_account_refuses(
    tmp_path: Path,
) -> None:
    after_all = _statement(tmp_path, date(2026, 1, 5), "20260301")
    with pytest.raises(gen.IbkrGenerationError, match="outside the statements"):
        gen._closed_accounts([after_all], XWALK)
    before_all = _statement(tmp_path, date(2026, 1, 5), "20251201")
    with pytest.raises(gen.IbkrGenerationError, match="outside the statements"):
        gen._closed_accounts([before_all], XWALK)
