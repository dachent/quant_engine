"""Deterministic Portfolio123 browser capture plans.

This module constructs reviewed URLs only.  Browser attachment, session handling,
capture, redaction, and publication remain responsibilities of Web Contract
Harvester and the governing issue.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode


_ORIGIN = "https://www.portfolio123.com"


class CapturePlanError(ValueError):
    """A requested browser target is outside the reviewed capture shape."""


@dataclass(frozen=True)
class CaptureTarget:
    """One reviewed browser navigation target."""

    capability: str
    url: str


def _identifier(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CapturePlanError("Portfolio123 identifiers must be positive integers")
    return value


def _url(path: str, **query: int | str) -> str:
    return f"{_ORIGIN}{path}?{urlencode(query)}" if query else f"{_ORIGIN}{path}"


def shared_strategy_capture_targets(strategy_id: int) -> tuple[CaptureTarget, ...]:
    """Return the fixed strategy pages and complete download targets."""

    value = _identifier(strategy_id)
    return (
        CaptureTarget("strategy.summary", _url("/port_summary.jsp", portid=value)),
        CaptureTarget("strategy.trading-system", _url("/port_wiz.jsp", editUid=value)),
        CaptureTarget(
            "strategy.transactions",
            _url("/p123/DownloadPortTrans", portid=value),
        ),
        CaptureTarget(
            "strategy.holdings",
            _url("/p123/DownloadPortHoldings", portid=value),
        ),
        CaptureTarget(
            "strategy.performance",
            _url("/p123/DownloadPortPerf", portid=value),
        ),
        CaptureTarget(
            "strategy.period-performance",
            _url(
                "/p123/DownloadPortPeriodPerformance",
                portid=value,
                bench=24262,
                period=1,
            ),
        ),
        CaptureTarget(
            "strategy.period-performance-distribution",
            _url(
                "/p123/DownloadPortPeriodPerformanceDist",
                portid=value,
                bench=24262,
                period=1,
            ),
        ),
    )


def managed_strategy_capture_targets(strategy_id: int) -> tuple[CaptureTarget, ...]:
    """Return reviewed SPA navigation targets for a managed strategy."""

    value = _identifier(strategy_id)
    base = "/app/investment/details"
    return (
        CaptureTarget("strategy.summary", _url(base, id=value)),
        CaptureTarget("strategy.performance", _url(base, id=value, t="performance")),
        CaptureTarget("strategy.holdings", _url(base, id=value, t="holdings")),
        CaptureTarget("strategy.transactions", _url(base, id=value, t="transactions")),
        CaptureTarget("strategy.statistics", _url(base, id=value, t="stats")),
    )


def account_capture_targets(account_id: int) -> tuple[CaptureTarget, ...]:
    """Return reviewed SPA navigation targets for one configured account."""

    value = _identifier(account_id)
    base = "/app/account/details"
    return (
        CaptureTarget("account.summary", _url(base, id=value)),
        CaptureTarget("account.performance", _url(base, id=value, t="performance")),
        CaptureTarget("account.holdings", _url(base, id=value, t="holdings")),
        CaptureTarget("account.transactions", _url(base, id=value, t="transactions")),
        CaptureTarget("account.statistics", _url(base, id=value, t="stats")),
    )
