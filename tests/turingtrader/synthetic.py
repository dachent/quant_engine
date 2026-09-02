"""Synthetic, fully offline fixtures for the TuringTrader capture tests."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Mapping

from quant_engine.turingtrader.endpoints import ENDPOINTS


SLUG = "tt-synthetic-alpha"
SLUG_TWO = "tt-synthetic-beta"
RUN_DATE_TEXT = "2026-09-02"

TOKEN_VALUES = {
    "public": "synthetic" + "-not-a-real-token-1",
    "allochist": "synthetic" + "-not-a-real-token-2",
    "holdings": "synthetic" + "-not-a-real-token-3",
}


def portfolio_comp_payload() -> dict[str, object]:
    return {
        "strategies": [
            {
                "slug": SLUG,
                "name": "Synthetic Alpha",
                "cagr": 0.123,
                "sharpe": 1.05,
                "max-drawdown": -0.18,
                "cagr-5th": json.dumps([0.02, 0.03]),
                "cagr-95th": json.dumps([0.21, 0.24]),
            }
        ]
    }


def performance_chart_payload(end_date: str = "2026-09-01") -> dict[str, object]:
    end = date.fromisoformat(end_date)
    return {
        "series": [
            {"date": (end - timedelta(days=2)).isoformat(), "nav": 100.0},
            {"date": (end - timedelta(days=1)).isoformat(), "nav": 100.7},
            {"date": end_date, "nav": 101.2},
        ]
    }


def performance_table_payload() -> dict[str, object]:
    return {
        "cagr": 0.123,
        "stdev": 0.14,
        "maxdd": -0.18,
        "sharpe": 1.05,
        "beta": 0.87,
        "ulcer": 3.1,
        "martin": 2.4,
    }


def holdings_history_payload(
    end_datetime: str = "2026-09-01T00:00:00",
) -> dict[str, object]:
    return {
        "rows": [
            ["2026-08-01T00:00:00", "SYNTH=55.00%, SYNTH2=45.00%"],
            [end_datetime, "SYNTH=100.00%"],
        ]
    }


def portfolio_holdings_payload() -> dict[str, object]:
    return {
        "holdings": [
            {"symbol": "SYNTH", "allocation": 55.0, "price": 12.34},
            {"symbol": "SYNTH2", "allocation": "---", "price": ""},
        ]
    }


def payload_bytes(
    *, chart_end: str = "2026-09-01", history_end: str = "2026-09-01T00:00:00"
) -> dict[str, bytes]:
    return {
        "portfolio-comp.json": json.dumps(portfolio_comp_payload()).encode("utf-8"),
        "performance-chart.json": json.dumps(
            performance_chart_payload(chart_end)
        ).encode("utf-8"),
        "performance-table.json": json.dumps(performance_table_payload()).encode(
            "utf-8"
        ),
        "holdings-history.json": json.dumps(
            holdings_history_payload(history_end)
        ).encode("utf-8"),
        "portfolio-holdings.json": json.dumps(portfolio_holdings_payload()).encode(
            "utf-8"
        ),
    }


class FakeFetch:
    """Offline fetch seam: maps endpoint object names to canned bytes."""

    def __init__(self, payloads: Mapping[str, bytes], *, status: int = 200) -> None:
        self._payloads = dict(payloads)
        self._status = status
        self.urls: list[str] = []

    def __call__(self, url: str) -> tuple[int, bytes]:
        self.urls.append(url)
        for endpoint in ENDPOINTS:
            if endpoint.object_name in url:
                return self._status, self._payloads[endpoint.object_name]
        raise AssertionError(f"unexpected URL: {url}")


def write_config(path: Path, **overrides: object) -> Path:
    document: dict[str, object] = {
        "version": 1,
        "tokens": dict(TOKEN_VALUES),
    }
    document.update(overrides)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path
