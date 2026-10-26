"""Period returns with an explicit annualisation basis (#594, PANEL-VIS-003).

Synthetic rows only.  The producer function and the independent verifier derive
the same schema-10 fields from the daily link path, annualised values exist only
from 365 days, and no accepted value changes.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPOSITORY_ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def producer() -> Any:
    return _load("_period_producer_594", "tools/account_strategy_manifest.py")


@pytest.fixture(scope="module")
def verifier() -> Any:
    return _load("_period_verifier_594", "tools/verify_account_manifest_generation.py")


def _links(start: date, days: int, *, weekends: bool) -> list[dict[str, str]]:
    """One link per observation day; factor 1.001 on even days, 0.9995 on odd."""

    links: list[dict[str, str]] = []
    previous = start
    for offset in range(1, days + 1):
        current = start + timedelta(days=offset)
        if not weekends and current.weekday() >= 5:
            continue
        factor = Decimal("1.001") if offset % 2 == 0 else Decimal("0.9995")
        links.append(
            {
                "start_date": previous.isoformat(),
                "end_date": current.isoformat(),
                "factor": format(factor, "f"),
            }
        )
        previous = current
    return links


def _twr(links: list[dict[str, str]]) -> str:
    wealth = Decimal(1)
    for link in links:
        wealth *= Decimal(link["factor"])
    return format(wealth - 1, "f")


def _row(
    links: list[dict[str, str]], *, mwr: str | None = "0.3", mwr_days: int = 40
) -> dict[str, Any]:
    first = date.fromisoformat(links[0]["start_date"])
    last = date.fromisoformat(links[-1]["end_date"])
    row: dict[str, Any] = {
        "status": "OK",
        "start": first.isoformat(),
        "end": last.isoformat(),
        "opening_capital_date": first.isoformat(),
        "observation_trace": {"links": links},
        "observation_linked_return": _twr(links),
        "annualized_return_on_net_capital": "0.1",
        "mwr_xirr": mwr,
        "mwr_opening_date": first.isoformat(),
        "mwr_terminal_date": (first + timedelta(days=mwr_days)).isoformat(),
    }
    return row


def test_short_window_has_no_annualised_twr_and_states_the_basis(
    producer: Any,
) -> None:
    links = _links(date(2026, 8, 1), 40, weekends=False)
    fields = producer._period_return_fields(_row(links))
    assert fields["window_days"] == 40
    assert fields["return_close_date"] == "2026-09-10"
    assert fields["twr_annualised"] is None
    basis = fields["annualisation_basis"]
    assert basis["twr"]["reported_basis"] == "CUMULATIVE"
    assert basis["twr"]["annualised_status"] == "SUPPRESSED_UNDER_365D"
    assert basis["mwr_xirr"]["reported_basis"] == "ANNUALISED_FROM_DAYS"
    assert basis["mwr_xirr"]["window_days"] == 40
    assert fields["mwr_window_equivalent"] is not None
    expected = (Decimal("1.3") ** (Decimal(40) / Decimal(365)) - 1).quantize(
        Decimal("1e-20")
    )
    assert Decimal(str(fields["mwr_window_equivalent"])).quantize(
        Decimal("1e-20")
    ) == expected
    assert fields["periods_per_year"] == 252
    assert fields["periods_per_year_basis"] == "DAILY_TRADING"


def test_year_or_longer_window_annualises_geometrically_and_never_earlier(
    producer: Any,
) -> None:
    short = producer._period_return_fields(
        _row(_links(date(2025, 9, 1), 364, weekends=True))
    )
    assert short["window_days"] == 364
    assert short["twr_annualised"] is None
    long_links = _links(date(2025, 9, 1), 400, weekends=True)
    row = _row(long_links, mwr_days=400)
    fields = producer._period_return_fields(row)
    assert fields["window_days"] == 400
    expected = (Decimal(1) + Decimal(row["observation_linked_return"])) ** (
        Decimal(365) / Decimal(400)
    ) - 1
    assert abs(Decimal(str(fields["twr_annualised"])) - expected) < Decimal("1e-25")
    assert fields["annualisation_basis"]["twr"]["annualised_status"] == "AVAILABLE"
    assert fields["annualisation_basis"]["mwr_xirr"]["reported_basis"] == "ANNUALISED"
    assert fields["periods_per_year"] == 365
    assert fields["periods_per_year_basis"] == "DAILY_CALENDAR"


def test_calendar_year_returns_multiply_back_to_the_cumulative_return(
    producer: Any,
) -> None:
    links = _links(date(2025, 11, 3), 120, weekends=False)
    row = _row(links)
    fields = producer._period_return_fields(row)
    assert set(fields["calendar_year_twr"]) == {"2025", "2026"}
    product = Decimal(1)
    for value in fields["calendar_year_twr"].values():
        product *= 1 + Decimal(str(value))
    assert abs(product - 1 - Decimal(row["observation_linked_return"])) < Decimal(
        "1e-25"
    )
    assert fields["ytd_twr"] == fields["calendar_year_twr"]["2026"]
    assert fields["ytd_window"]["end"] == fields["return_close_date"]
    # The epoch opened in November: neither year covers its whole period, the
    # 2026 window opens at the last 2025 observation.
    assert fields["calendar_year_windows"]["2025"]["covers_period"] is False
    assert fields["calendar_year_windows"]["2026"]["covers_period"] is True
    assert fields["calendar_year_windows"]["2026"]["start"] < "2026-01-04"


def test_period_fields_never_change_an_accepted_value(producer: Any) -> None:
    row = _row(_links(date(2026, 8, 1), 40, weekends=False))
    before = dict(row)
    fields = producer._period_return_fields(row)
    assert row == before
    assert not set(fields) & set(before)
    assert set(fields) == set(producer.PERIOD_RETURN_FIELDS)


def test_independent_verifier_derivation_equals_the_producer(
    producer: Any, verifier: Any
) -> None:
    for weekends, days, mwr_days in ((False, 40, 40), (True, 400, 400)):
        links = _links(date(2025, 9, 1), days, weekends=weekends)
        row = _row(links, mwr_days=mwr_days)
        numeric = {
            "observation_trace": {
                "links": [
                    {**link, "factor": Decimal(link["factor"])} for link in links
                ]
            },
            "observation_linked_return": Decimal(row["observation_linked_return"]),
            "annualized_return_on_net_capital": Decimal("0.1"),
            "end": row["end"],
            "opening_capital_date": row["opening_capital_date"],
        }
        mwr = {
            "mwr_xirr": row["mwr_xirr"],
            "mwr_opening_date": row["mwr_opening_date"],
            "mwr_terminal_date": row["mwr_terminal_date"],
        }
        expected = verifier._independent_period_fields(numeric, mwr)
        derived = producer.json_ready(producer._period_return_fields(row))
        assert derived == expected


def test_manifest_schema_ten_requires_the_fields_and_nine_forbids_them(
    producer: Any,
) -> None:
    import json

    schema = json.loads(
        (
            REPOSITORY_ROOT
            / "schemas"
            / "portfolio"
            / "account-strategy-manifest.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["properties"]["schema_version"]["enum"] == [7, 8, 9, 10]
    conditions = [
        entry
        for entry in schema["allOf"]
        if entry.get("if", {}).get("properties", {}).get("schema_version")
        in ({"const": 10}, {"enum": [7, 8, 9]})
    ]
    assert len(conditions) == 2
    required = set(
        conditions[0]["then"]["properties"]["rows"]["items"]["then"]["required"]
    )
    assert required == set(producer.PERIOD_RETURN_FIELDS)
    assert producer._CURRENT_MANIFEST_SCHEMA_VERSION == 10
    assert 9 in producer._CURRENT_MANIFEST_FAMILY
