"""Reviewed Portfolio123 browser routes for HAR-first acquisition."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import re
from typing import Any

from quant_engine.web_contracts.contracts import (
    EndpointContract,
    ProviderContract,
    SemanticEffect,
    WebContractError,
)


_ORIGIN = "https://www.portfolio123.com"
_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*")


def _positive_query(
    required: frozenset[str],
) -> Callable[[Mapping[str, Any]], None]:
    def validate(value: Mapping[str, Any]) -> None:
        if set(value) != required or any(
            _POSITIVE_INTEGER.fullmatch(str(value[key])) is None for key in required
        ):
            raise WebContractError("Portfolio123 query differs from the reviewed shape")

    return validate


def _endpoint(
    capability: str,
    path: str,
    *,
    path_pattern: str | None = None,
    query_keys: frozenset[str] = frozenset(),
) -> EndpointContract:
    return EndpointContract(
        capability=capability,
        method="GET",
        origin=_ORIGIN,
        path=path,
        path_pattern=path_pattern,
        query_keys=query_keys,
        validate_query=_positive_query(query_keys) if query_keys else None,
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header="cookie",
    )


PORTFOLIO123_CONTRACT = ProviderContract(
    provider="portfolio123",
    endpoints=(
        _endpoint(
            "strategy.summary",
            "/port_summary.jsp",
            query_keys=frozenset({"portid"}),
        ),
        _endpoint(
            "strategy.trading-system",
            "/port_wiz.jsp",
            query_keys=frozenset({"edituid"}),
        ),
        _endpoint(
            "strategy.transactions",
            "/p123/DownloadPortTrans",
            query_keys=frozenset({"portid"}),
        ),
        _endpoint(
            "strategy.holdings",
            "/p123/DownloadPortHoldings",
            query_keys=frozenset({"portid"}),
        ),
        _endpoint(
            "strategy.performance",
            "/p123/DownloadPortPerf",
            query_keys=frozenset({"portid"}),
        ),
        _endpoint(
            "strategy.period-performance",
            "/p123/DownloadPortPeriodPerformance",
            query_keys=frozenset({"bench", "period", "portid"}),
        ),
        _endpoint(
            "strategy.period-performance-distribution",
            "/p123/DownloadPortPeriodPerformanceDist",
            query_keys=frozenset({"bench", "period", "portid"}),
        ),
        _endpoint(
            "strategy.ranking-system",
            "/app/ranking-system/{ranking_id}",
            path_pattern=r"/app/ranking-system/[1-9][0-9]*",
        ),
        _endpoint(
            "strategy.resource",
            "/app/investment/resource/{strategy_id}",
            path_pattern=r"/app/investment/resource/[1-9][0-9]*",
        ),
        _endpoint(
            "strategy.holdings-spa",
            "/app/investment/holdings/{strategy_id}",
            path_pattern=r"/app/investment/holdings/[1-9][0-9]*",
        ),
        _endpoint(
            "strategy.performance-spa",
            "/spr/strategy/portmodel/{strategy_id}/perf",
            path_pattern=r"/spr/strategy/portmodel/[1-9][0-9]*/perf",
        ),
        _endpoint(
            "account.resource",
            "/app/account/resource/{account_id}",
            path_pattern=r"/app/account/resource/[1-9][0-9]*",
        ),
        _endpoint(
            "account.positions",
            "/app/account/positions/{account_id}",
            path_pattern=r"/app/account/positions/[1-9][0-9]*",
        ),
    ),
    sensitive_fields=frozenset(
        {
            "account",
            "accountid",
            "accountnumber",
            "email",
            "owner",
            "userid",
            "username",
        }
    ),
    api_fallback=False,
)
