"""The two oracle derivations of date.strategy_cash.coverage_end, side by side.

SYNTHETIC: every window, event and request below is invented to have the shape of a
real case. derive_a reads the pinned ledger's admission windows, derive_b reads the
raw capture coverage; the pair must agree exactly. The A/B comparator marks a pair
that does not agree DISAGREE, and the expected-baseline assembler admits only AGREE
or BOTH_NOT_DERIVABLE rows, so a disagreement never reaches the validator as a date.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "acceptance"))

from derive_a import derive as derive_a  # noqa: E402
from derive_a.sources import Admission  # noqa: E402

ALLOWED_STATUSES = frozenset({"AGREE", "BOTH_NOT_DERIVABLE"})


def _load_b() -> Any:
    path = REPOSITORY_ROOT / "acceptance" / "derive_b" / "derive.py"
    spec = importlib.util.spec_from_file_location("derive_b_pair", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    ambient = __import__("decimal").getcontext().copy()
    try:
        spec.loader.exec_module(module)
    finally:
        __import__("decimal").setcontext(ambient)
    return module


derive_b = _load_b()


def day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def _ms(text: str) -> int:
    moment = dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc)
    return int(moment.timestamp() * 1000)


def _window(provider: str, fact_type: str, start: str, end: str) -> Admission:
    return Admission(provider, "acct", fact_type, day(start), day(end))


def _facts(first_flow: str | None, first_allocation: str | None) -> Any:
    zero = Decimal(0)
    return derive_a.CustodyFacts(
        invested=zero,
        allocations=zero,
        cash=zero,
        events=2,
        last_event=None,
        inputs=(),
        notes=(),
        first_flow=day(first_flow) if first_flow else None,
        first_allocation=day(first_allocation) if first_allocation else None,
    )


def _cov(
    path: str, terminal: str = "structured_parameter_rejections_dispositioned"
) -> dict[str, str]:
    return {
        "endpoint": f"GET https://mapi.matrixport.com{path}",
        "status": "complete_to_observed_boundary",
        "terminal_reason": terminal,
        "updated_at_utc": "2026-09-18T18:42:59.174000+00:00",
    }


def _req(path: str, **params: object) -> str:
    return json.dumps(
        {"endpoint": path, "safe_params": json.dumps({"limit": 50, **params})}
    )


WALLET = ("/mapi/v1/wallet/deposits", "/mapi/v1/wallet/withdrawals")
TRADE = "/strategy/api/v2/user/trade_record"


def _status(a: dt.date | str, b: dt.date | str) -> str:
    """The comparator's rule for a date row: both values and equal, else not agreed."""

    if isinstance(a, str) and isinstance(b, str):
        return "BOTH_NOT_DERIVABLE"
    if isinstance(a, str) or isinstance(b, str):
        return "ONE_NOT_DERIVABLE"
    return "AGREE" if a == b else "DISAGREE"


def _matrixport(
    windows: list[Admission],
    first_flow: str,
    first_allocation: str,
    last_flow: str,
    last_allocation: str,
    coverage: list[dict[str, str]],
    requests: list[str],
) -> tuple[dt.date | str, dt.date | str]:
    a = derive_a._custody_coverage_end(
        windows, "matrixport", "acct", _facts(first_flow, first_allocation)
    )
    b = derive_b.custody_coverage_end(
        "robuxio-crypto",
        {
            "owner": [day(first_flow), day(last_flow)],
            "allocation": [day(first_allocation), day(last_allocation)],
        },
        {},
        None,
        coverage,
        requests,
    )
    return a, b


def test_robuxio_shape_both_derivations_end_at_the_last_anchor() -> None:
    # Flows proven through 09-18 by the wallet lists; the only anchor window is the
    # one anchor, and the trade_record window of the capture starts after it.
    a, b = _matrixport(
        [
            _window("matrixport", "flows", "2025-12-05", "2026-09-18"),
            _window("matrixport", "capital_anchors", "2025-12-05", "2025-12-05"),
        ],
        "2025-12-05",
        "2025-12-05",
        "2025-12-05",
        "2025-12-05",
        [_cov(WALLET[0]), _cov(WALLET[1]), _cov(TRADE, "empty_page")],
        [
            _req(WALLET[0], currency="USDT"),
            _req(WALLET[1], currency="USDT"),
            _req(
                TRADE,
                start_time=_ms("2026-08-04T05:10:29"),
                end_time=_ms("2026-09-18T18:42:48"),
            ),
        ],
    )
    assert (a, b) == (day("2025-12-05"), day("2025-12-05"))
    assert _status(a, b) == "AGREE"


def test_a_chain_of_windows_back_to_the_first_anchor_extends_both() -> None:
    a, b = _matrixport(
        [
            _window("matrixport", "flows", "2025-12-05", "2026-09-18"),
            _window("matrixport", "capital_anchors", "2025-12-05", "2026-09-18"),
        ],
        "2025-12-05",
        "2025-12-05",
        "2025-12-05",
        "2025-12-05",
        [_cov(WALLET[0]), _cov(WALLET[1]), _cov(TRADE, "short_page")],
        [
            _req(WALLET[0], currency="USDT"),
            _req(WALLET[1], currency="USDT"),
            _req(TRADE, start_time=0, end_time=_ms("2026-09-03T05:10:29")),
            _req(
                TRADE,
                start_time=_ms("2026-08-04T05:10:29"),
                end_time=_ms("2026-09-18T18:42:48"),
            ),
        ],
    )
    assert (a, b) == (day("2026-09-18"), day("2026-09-18"))
    assert _status(a, b) == "AGREE"


def test_a_hole_between_flow_windows_ends_both_at_the_first_segment() -> None:
    a, b = _matrixport(
        [
            _window("matrixport", "flows", "2025-12-05", "2026-09-03"),
            _window("matrixport", "flows", "2026-09-05", "2026-09-18"),
            _window("matrixport", "capital_anchors", "2025-12-05", "2026-09-18"),
        ],
        "2025-12-05",
        "2025-12-05",
        "2025-12-05",
        "2025-12-05",
        [_cov(WALLET[0]), _cov(WALLET[1]), _cov(TRADE, "short_page")],
        [
            _req(WALLET[0], start_time=0, end_time=_ms("2026-09-03T05:10:29")),
            _req(
                WALLET[0],
                start_time=_ms("2026-09-05T05:10:29"),
                end_time=_ms("2026-09-18T18:42:48"),
            ),
            _req(WALLET[1], start_time=0, end_time=_ms("2026-09-03T05:10:29")),
            _req(
                WALLET[1],
                start_time=_ms("2026-09-05T05:10:29"),
                end_time=_ms("2026-09-18T18:42:48"),
            ),
            _req(TRADE, start_time=0, end_time=_ms("2026-09-18T18:42:48")),
        ],
    )
    assert (a, b) == (day("2026-09-03"), day("2026-09-03"))
    assert _status(a, b) == "AGREE"


def test_volatis_covers_the_cutoff_in_both_derivations() -> None:
    windows = [
        _window("quantbase", "flows", "2024-07-20", "2026-09-18"),
        _window("quantbase", "capital_anchors", "2024-07-20", "2026-09-18"),
    ]
    a = derive_a._custody_coverage_end(
        windows, "quantbase", "acct", _facts("2024-07-20", "2024-10-13")
    )
    ledger = {
        "provider_generations": [
            {
                "provider_id": "quantbase",
                "generation_id": "q",
                "generation_as_of_date": "2026-09-18",
            }
        ]
    }
    b = derive_b.custody_coverage_end(
        "volatis-strategy",
        {
            "owner": [day("2024-07-20"), day("2025-11-08")],
            "allocation": [day("2024-10-13"), day("2026-01-15")],
        },
        ledger,
        {"quantbase": {"generation_id": "q"}},
        None,
        None,
    )
    assert (a, b) == (day("2026-09-18"), day("2026-09-18"))
    assert a > day("2026-09-17")  # unclamped: it follows the cutoff
    assert _status(a, b) == "AGREE"


def test_a_stale_ledger_and_a_fresh_capture_disagree_and_are_never_admitted() -> None:
    # The d4d24f ledger ended the Quantbase anchors at the last anchor (2026-01-15)
    # while the capture is dated 2026-09-18: the two derivations differ, the comparator
    # says DISAGREE and the assembler admits no such row.
    stale = [
        _window("quantbase", "flows", "2024-07-20", "2026-09-18"),
        _window("quantbase", "capital_anchors", "2024-07-20", "2026-01-15"),
    ]
    a = derive_a._custody_coverage_end(
        stale, "quantbase", "acct", _facts("2024-07-20", "2024-10-13")
    )
    ledger = {
        "provider_generations": [
            {
                "provider_id": "quantbase",
                "generation_id": "q",
                "generation_as_of_date": "2026-09-18",
            }
        ]
    }
    b = derive_b.custody_coverage_end(
        "volatis-strategy",
        {
            "owner": [day("2024-07-20"), day("2025-11-08")],
            "allocation": [day("2024-10-13"), day("2026-01-15")],
        },
        ledger,
        {"quantbase": {"generation_id": "q"}},
        None,
        None,
    )
    assert (a, b) == (day("2026-01-15"), day("2026-09-18"))
    assert _status(a, b) == "DISAGREE"
    assert _status(a, b) not in ALLOWED_STATUSES


@pytest.mark.parametrize("other_side", ["a date", "no date either"])
def test_one_derivation_without_a_date_is_not_agreement(other_side: str) -> None:
    # No capital_anchors window: derive_a has no segment, so no date.
    windows = [_window("quantbase", "flows", "2024-07-20", "2026-09-18")]
    a = derive_a._custody_coverage_end(
        windows, "quantbase", "acct", _facts("2024-07-20", "2024-10-13")
    )
    assert isinstance(a, str)
    b: dt.date | str = day("2026-09-18") if other_side == "a date" else a
    status = _status(a, b)
    if other_side == "a date":
        assert status == "ONE_NOT_DERIVABLE"
        assert status not in ALLOWED_STATUSES
    else:
        assert status == "BOTH_NOT_DERIVABLE"
        assert status in ALLOWED_STATUSES
