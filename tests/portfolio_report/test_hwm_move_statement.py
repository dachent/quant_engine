"""The HWM member sentence splits the index move from the paths, never from wording.

Synthetic index paths only. The helpers come from the sleeve perimeter test module.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import re
from typing import Any

from test_sleeve_perimeter_disclosures import (
    PARENT,
    _closing_ledger,
    _compose,
    _facts,
    _rows,
    _sources,
)

DAYS = ["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14"]


def _path(values: list[str], days: list[str] | None = None) -> list[dict[str, str]]:
    return [
        {"as_of": d, "index": v, "hwm": v, "drawdown": "0"}
        for d, v in zip(days or DAYS, values, strict=True)
    ]


def _patches(date: str | None, account: str = PARENT) -> list[dict[str, Any]]:
    """The derived-patch index the report builds from the pinned finality evidence."""

    if date is None:
        return []
    return [
        {
            "accounts": {account: frozenset({date})},
            "value_basis": {"basis": "DERIVED_FROM_OFFICIAL_CLOSES"},
        }
    ]


def _sentence(
    tmp_path: Path,
    prior: list[list[str]] | None,
    now: list[dict[str, str]],
    clock: list[dict[str, Any]] | None,
    before: str | None = None,
) -> str:
    sources = _sources(tmp_path, _facts(), _closing_ledger())
    values = {
        "strategy_id": "product",
        "strategy_capital_usd": "250",
        "aggregate_capital_usd": "800",
        "hwm_current_index": before or (prior[-1][1] if prior else "100"),
    }
    sources["predecessor"] = {
        "status": "RESOLVED",
        "release_id": "r" * 64,
        "panel_generation_id": "p" * 64,
        "values": values,
        **({"hwm_index_path": prior} if prior is not None else {}),
    }
    return next(
        i["value"]
        for i in _compose(
            sources,
            _rows(),
            hwm_current_index=now[-1]["index"],
            hwm_index_path=now,
            derived_patches=clock,
        )["items"]
        if i["item"] == "HWM member series"
    )


def test_a_move_confined_to_a_derived_patch_date_is_attributed_to_it(
    tmp_path: Path,
) -> None:
    prior = [
        [d, v] for d, v in zip(DAYS, ["100", "101", "102", "103", "104"], strict=True)
    ]
    now = _path(["100", "101", "102", "103", "105.5"])
    text = _sentence(tmp_path, prior, now, _patches("2026-09-14"))
    assert (
        "The index is 105.5000 against 104.0000 in the previous release (+1.50, +1.44%)"
        in text
    )
    assert (
        "of this +1.50 is the restatement of the previous release's own last date 2026-09-14"
        in text
    )
    assert "and +0.00 is the movement from 2026-09-14 to 2026-09-14" in text
    assert "differ on 1 of 5 common dates" in text
    assert "2026-09-14 (DERIVED_FROM_OFFICIAL_CLOSES)" in text
    assert "attributed to that NAV revision" in text
    assert "not attributed beyond" not in text


def test_a_move_on_other_dates_is_not_attributed_to_the_revision(
    tmp_path: Path,
) -> None:
    prior = [
        [d, v] for d, v in zip(DAYS, ["100", "101", "102", "103", "104"], strict=True)
    ]
    now = _path(["100", "101.5", "102.5", "103.5", "105.5"])
    text = _sentence(tmp_path, prior, now, _patches("2026-09-14"))
    assert "differ on 4 of 5 common dates (first 2026-09-09, last 2026-09-14)" in text
    assert "attributed to that NAV revision" not in text
    assert "not attributed beyond this timing" in text


def test_a_clock_without_a_patch_on_the_date_attributes_nothing(tmp_path: Path) -> None:
    prior = [
        [d, v] for d, v in zip(DAYS, ["100", "101", "102", "103", "104"], strict=True)
    ]
    now = _path(["100", "101", "102", "103", "105.5"])
    for clock in (None, _patches(None), _patches("2026-09-11")):
        text = _sentence(tmp_path, prior, now, clock)
        assert "attributed to that NAV revision" not in text
        assert "not attributed beyond this timing" in text


def test_the_two_parts_always_sum_to_the_printed_move(tmp_path: Path) -> None:
    """An earlier predecessor date: restatement plus extension equals the move, from the paths."""

    prior_days = DAYS[:3]
    prior = [[d, v] for d, v in zip(prior_days, ["100", "101", "102.25"], strict=True)]
    now = _path(["100", "101.5", "103", "104", "105.5"])
    text = _sentence(tmp_path, prior, now, _patches("2026-09-14"))
    move = Decimal("105.5") - Decimal("102.25")
    assert f"({move:+.2f}," in text
    numbers = [
        Decimal(n)
        for n in re.findall(
            r"of this ([+-]\d+\.\d\d) is the restatement.*? and ([+-]\d+\.\d\d) is the movement",
            text,
        )[0]
    ]
    assert sum(numbers) == move
    assert numbers == [Decimal("0.75"), Decimal("2.50")]
    assert "differ on 2 of 3 common dates" in text


def test_paths_that_do_not_end_on_the_printed_figures_give_no_split(
    tmp_path: Path,
) -> None:
    prior = [
        [d, v] for d, v in zip(DAYS, ["100", "101", "102", "103", "104"], strict=True)
    ]
    now = _path(["100", "101", "102", "103", "105.5"])
    text = _sentence(tmp_path, prior, now, _patches("2026-09-14"), before="99")
    assert "restatement" not in text
    assert text.endswith("the move is not attributed beyond this timing.")


def test_without_a_predecessor_path_the_old_unattributed_wording_remains(
    tmp_path: Path,
) -> None:
    now = _path(["100", "101", "102", "103", "105.5"])
    text = _sentence(tmp_path, None, now, _patches("2026-09-14"), before="104")
    assert "restatement" not in text
    assert "not attributed beyond this timing" in text


def test_the_predecessor_path_is_read_from_the_pinned_data_file(tmp_path: Path) -> None:
    import sleeve_disclosures as disclosures

    document = {
        "hwm_governed_proof": {
            "current_index": "104",
            "index_path": _path(["100", "101", "102", "103", "104"]),
        }
    }
    got = disclosures._predecessor_index_path(
        document, {"hwm_current_index": "/hwm_governed_proof/current_index"}
    )
    assert got is not None and got[-1] == ["2026-09-14", "104"] and len(got) == 5
    named = disclosures._predecessor_index_path(
        document, {"hwm_index_path": "/hwm_governed_proof/index_path"}
    )
    assert named == got
    assert (
        disclosures._predecessor_index_path(
            {"hwm_governed_proof": {}},
            {"hwm_current_index": "/hwm_governed_proof/current_index"},
        )
        is None
    )


def test_a_patch_on_another_account_or_an_earlier_date_attributes_nothing(
    tmp_path: Path,
) -> None:
    """The scope is the governed patch's own account and dates, never the provider's data date."""

    prior = [
        [d, v] for d, v in zip(DAYS, ["100", "101", "102", "103", "104"], strict=True)
    ]
    now = _path(["100", "101", "102", "103", "105.5"])
    for clock in (
        _patches(
            "2026-09-14", account="account-unrelated"
        ),  # right date, wrong account
        _patches("2026-09-11"),  # right account, other date
        _patches("2026-09-14", account="account-unrelated") + _patches("2026-09-11"),
    ):
        text = _sentence(tmp_path, prior, now, clock)
        assert "attributed to that NAV revision" not in text
        assert "not attributed beyond this timing" in text
    # the member's own sleeve account counts as well as its parent
    from test_sleeve_perimeter_disclosures import S1, S2

    for member in (S1, S2):
        text = _sentence(tmp_path, prior, now, _patches("2026-09-14", account=member))
        assert ("attributed to that NAV revision" in text) == (
            member == _member(tmp_path)
        )


def _member(tmp_path: Path) -> str:
    """The HWM member account the fixture's sentence names."""

    text = _sentence(tmp_path, None, _path(["1", "1", "1", "1", "1"]), None, before="1")
    return re.search(r"sleeve series (\S+) from", text).group(1)  # type: ignore[union-attr]
