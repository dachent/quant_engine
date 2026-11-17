"""The broker's own AccountInformation dateClosed reaches the catalog, strictly."""

from __future__ import annotations

import dataclasses
from datetime import date
import importlib.util
from pathlib import Path
import sys

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


def _statement(tmp_path: Path, day: date, closed: str | None) -> object:
    text = helpers._xml(RAW, day, "100")
    plain = f'<AccountInformation accountId="{RAW}" currency="USD" />'
    assert plain in text
    if closed is not None:
        text = text.replace(
            plain,
            f'<AccountInformation accountId="{RAW}" currency="USD" dateClosed="{closed}" />',
        )
    path = tmp_path / f"s-{day.isoformat()}.xml"
    path.write_text(text, encoding="utf-8")
    return parse_flex_file(path, artifact_id=f"private/{path.name}").statements[0]


def test_an_empty_date_closed_is_not_a_closure_and_a_date_is(tmp_path: Path) -> None:
    open_ = _statement(tmp_path, date(2026, 1, 5), None)
    closed = _statement(tmp_path, date(2026, 1, 6), "20260105")
    assert open_.account_date_closed is None  # type: ignore[attr-defined]
    assert closed.account_date_closed == date(2026, 1, 5)  # type: ignore[attr-defined]
    assert gen._closed_accounts([open_], {RAW: "account-x"}) == []  # type: ignore[list-item]
    assert gen._closed_accounts([open_, closed], {RAW: "account-x"}) == [  # type: ignore[list-item]
        {"account_id": "account-x", "closed_date": "2026-01-05"}
    ]


def test_an_unparseable_date_closed_refuses(tmp_path: Path) -> None:
    with pytest.raises(FlexValidationError):
        _statement(tmp_path, date(2026, 1, 5), "not-a-date")


def test_conflicting_closure_dates_refuse_and_an_earlier_pull_without_one_does_not(
    tmp_path: Path,
) -> None:
    a = _statement(tmp_path, date(2026, 1, 5), "20260105")
    b = dataclasses.replace(
        _statement(tmp_path, date(2026, 1, 6), "20260105"),  # type: ignore[type-var]
        account_date_closed=date(2026, 1, 6),
    )
    with pytest.raises(gen.IbkrGenerationError, match="different account closure"):
        gen._closed_accounts([a, b], {RAW: "account-x"})  # type: ignore[list-item]
    later_open = _statement(tmp_path, date(2026, 1, 7), None)
    assert gen._closed_accounts([a, later_open], {RAW: "account-x"}) == [  # type: ignore[list-item]
        {"account_id": "account-x", "closed_date": "2026-01-05"}
    ]
