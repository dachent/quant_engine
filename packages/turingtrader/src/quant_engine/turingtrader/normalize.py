"""Pure normalization of schema-valid payloads into canonical rows.

Every function here is a pure function of the raw response bytes: no clocks,
no environment, no network.  ``None`` replaces the provider's blank/dash
sentinels for numeric cells.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from .errors import NormalizationError


def _require_number(value: Any, location: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NormalizationError(f"{location} must be a number")
    return value


def _parse_stringified_array(value: Any, location: str) -> list[float | int]:
    if not isinstance(value, str):
        raise NormalizationError(f"{location} must be a stringified JSON array")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise NormalizationError(f"{location} is not parseable JSON") from error
    if not isinstance(parsed, list):
        raise NormalizationError(f"{location} must decode to a JSON array")
    return [
        _require_number(item, f"{location}[{index}]")
        for index, item in enumerate(parsed)
    ]


def _blankable_number(value: Any, location: str) -> float | int | None:
    if isinstance(value, str):
        if value in ("", "---"):
            return None
        raise NormalizationError(f"{location} has an unexpected string sentinel")
    return _require_number(value, location)


def _normalize_portfolio_comp(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, entry in enumerate(payload["strategies"]):
        location = f"strategies[{index}]"
        rows.append(
            {
                "slug": entry["slug"],
                "name": entry["name"],
                "cagr": entry["cagr"],
                "sharpe": entry["sharpe"],
                "max-drawdown": entry["max-drawdown"],
                "cagr-5th": _parse_stringified_array(
                    entry["cagr-5th"], f"{location}.cagr-5th"
                ),
                "cagr-95th": _parse_stringified_array(
                    entry["cagr-95th"], f"{location}.cagr-95th"
                ),
            }
        )
    return rows


def _normalize_performance_chart(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"date": point["date"], "nav": point["nav"]} for point in payload["series"]]


def _normalize_performance_table(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            key: payload[key]
            for key in ("cagr", "stdev", "maxdd", "sharpe", "beta", "ulcer", "martin")
        }
    ]


def _normalize_holdings_history(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, pair in enumerate(payload["rows"]):
        datetime_text, signal = pair
        date_text = datetime_text[:10]
        try:
            datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError:
            raise NormalizationError(
                f"rows[{index}] has an invalid calendar date: {date_text!r}"
            ) from None
        rows.append({"date": date_text, "signal": signal})
    return rows


def _normalize_portfolio_holdings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, entry in enumerate(payload["holdings"]):
        location = f"holdings[{index}]"
        rows.append(
            {
                "symbol": entry["symbol"],
                "allocation": _blankable_number(
                    entry["allocation"], f"{location}.allocation"
                ),
                "price": _blankable_number(entry["price"], f"{location}.price"),
            }
        )
    return rows


_NORMALIZERS = {
    "portfolio-comp.json": _normalize_portfolio_comp,
    "performance-chart.json": _normalize_performance_chart,
    "performance-table.json": _normalize_performance_table,
    "holdings-history.json": _normalize_holdings_history,
    "portfolio-holdings.json": _normalize_portfolio_holdings,
}


def normalize(object_name: str, payload: Any) -> list[dict[str, Any]]:
    """Normalize one schema-valid payload; pure bytes-to-rows semantics."""
    try:
        normalizer = _NORMALIZERS[object_name]
    except KeyError:
        raise NormalizationError(f"no normalizer for object {object_name!r}") from None
    if not isinstance(payload, dict):
        raise NormalizationError(f"{object_name} payload must be an object")
    return normalizer(payload)
