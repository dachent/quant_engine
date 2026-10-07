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


# --------------------------------------- round 7: request scope (K4) and capture bound (K5)
#
# Astra rg7-astra-oracle-controls: derive_b merged every request window of an endpoint
# after dropping the request filters, so one currency chain filled another currency
# unobserved interval, and it took the REQUESTED end of a window as observed coverage
# without bounding it by the capture instant, so a request for the future proved a future
# date.  The ledger side (derive_a) reads admissions, which carry no scope: an admission
# that bridges scopes is the producer one, and the pair must DISAGREE when it does.

BASE_END = _ms("2026-09-03T00:00:00")
LAST_END = _ms("2026-09-18T00:00:00")
LATER_START = _ms("2026-08-04T00:00:00")
CAPTURE_DAY = day("2026-09-18")
FIRST_DAY = "2025-12-05"
WALLET_COVERAGE = [
    {**_cov(path), "details_json": json.dumps({"raw_rows": 1})} for path in WALLET
]


def _scoped(requests: list[str], a_end: str, *, trade_end: int = LAST_END) -> Any:
    """A overextended (or not) to ``a_end``, B from the raw requests."""

    return _matrixport(
        [
            _window("matrixport", "flows", FIRST_DAY, a_end),
            _window("matrixport", "capital_anchors", FIRST_DAY, a_end),
        ],
        FIRST_DAY,
        FIRST_DAY,
        FIRST_DAY,
        FIRST_DAY,
        [*WALLET_COVERAGE, _cov(TRADE, "empty_page")],
        [*requests, _req(TRADE, start_time=0, end_time=trade_end)],
    )


def _wallet(currency: str, start: int, end: int) -> list[str]:
    return [
        _req(path, currency=currency, start_time=start, end_time=end) for path in WALLET
    ]


def test_a_chain_of_one_currency_extends_the_flows_boundary() -> None:
    a, b = _scoped(
        [*_wallet("USDT", 0, BASE_END), *_wallet("USDT", LATER_START, LAST_END)],
        "2026-09-18",
    )

    assert (a, b) == (CAPTURE_DAY, CAPTURE_DAY)
    assert _status(a, b) == "AGREE"


def test_another_currency_window_never_fills_a_currency_hole() -> None:
    """USDT is observed to 09-03 and USDC from 08-04 to 09-18: the USDC window starts
    after the first event and must not bridge the USDT chain to 09-18.  The producer
    admission (a synthetic ledger window to 09-18) is overextended, so the pair breaks."""

    a, b = _scoped(
        [*_wallet("USDT", 0, BASE_END), *_wallet("USDC", LATER_START, LAST_END)],
        "2026-09-18",
    )

    assert a == CAPTURE_DAY
    assert b == day(FIRST_DAY)
    assert _status(a, b) == "DISAGREE"
    assert _status(a, b) not in ALLOWED_STATUSES


def test_the_unrelated_currency_requests_do_not_change_the_old_window() -> None:
    a, b = _scoped(_wallet("USDT", 0, BASE_END), "2026-09-03")

    assert (a, b) == (day("2026-09-03"), day("2026-09-03"))
    assert _status(a, b) == "AGREE"


def test_every_scope_must_reach_the_boundary_and_the_smallest_end_wins() -> None:
    """USDT proven to 09-18 and USDC, from the epoch, only to 09-03: the endpoint is
    proven only through the smaller one, whatever order the requests came in."""

    for requests in (
        [*_wallet("USDT", 0, LAST_END), *_wallet("USDC", 0, BASE_END)],
        [*_wallet("USDC", 0, BASE_END), *_wallet("USDT", 0, LAST_END)],
    ):
        a, b = _scoped(requests, "2026-09-03")

        assert (a, b) == (day("2026-09-03"), day("2026-09-03"))
        assert _status(a, b) == "AGREE"


def test_paging_parameters_do_not_split_a_scope() -> None:
    """Class sweep: the scope key is every request parameter that is neither a time bound
    nor paging.  ``limit`` and ``offset`` page one result and must not split a chain."""

    chain = [
        _req(path, currency="USDT", offset=offset, start_time=start, end_time=end)
        for path in WALLET
        for offset, (start, end) in ((0, (0, BASE_END)), (50, (LATER_START, LAST_END)))
    ]

    a, b = _scoped(chain, "2026-09-18")

    assert (a, b) == (CAPTURE_DAY, CAPTURE_DAY)


def test_the_scope_key_covers_every_parameter_the_oracle_reads() -> None:
    """Every parameter ``request_time_window`` reads is time, paging or scope, so a new
    readable parameter cannot slip past the scope key unclassified."""

    readable = (
        derive_b.PLAIN_LIST_PARAMS
        | derive_b.WINDOW_START_KEYS
        | derive_b.WINDOW_END_KEYS
    )
    classified = (
        derive_b.PAGING_PARAMS
        | derive_b.SCOPE_PARAMS
        | derive_b.WINDOW_START_KEYS
        | derive_b.WINDOW_END_KEYS
    )
    assert readable == classified
    assert derive_b.SCOPE_PARAMS == frozenset({"currency"})


def test_a_requested_end_after_the_capture_proves_only_the_capture_day() -> None:
    """A request for the future cannot prove the future empty: the observed end is
    bounded by the response instant (09-18T18:42:59Z), never taken from the request."""

    future = _ms("2026-10-07T00:00:00")
    requests = [
        _req(path, currency="USDT", start_time=0, end_time=future) for path in WALLET
    ]

    a, b = _scoped(requests, "2026-10-07", trade_end=future)

    assert b == CAPTURE_DAY
    assert a == day("2026-10-07")  # the unbounded ledger window of the producer
    assert _status(a, b) == "DISAGREE"


def test_a_window_inside_the_capture_is_not_clamped() -> None:
    a, b = _scoped(_wallet("USDT", 0, LAST_END), "2026-09-18")

    assert (a, b) == (CAPTURE_DAY, CAPTURE_DAY)


def test_a_window_wholly_after_the_capture_observes_nothing() -> None:
    """The chain to 09-03 is real; the 10-01..10-07 request began after the response."""

    requests = [
        *_wallet("USDT", 0, BASE_END),
        *_wallet("USDT", _ms("2026-10-01T00:00:00"), _ms("2026-10-07T00:00:00")),
    ]

    a, b = _scoped(requests, "2026-09-03")

    assert (a, b) == (day("2026-09-03"), day("2026-09-03"))


def test_a_windowed_capture_without_a_readable_capture_instant_proves_nothing() -> None:
    broken = [{**row, "updated_at_utc": "not a time"} for row in WALLET_COVERAGE]

    end = derive_b.list_endpoint_coverage_end(
        broken, _wallet("USDT", 0, LAST_END), WALLET[0], day(FIRST_DAY)
    )

    assert end is None


def test_derive_a_bounds_an_admission_by_the_ledger_generation_date() -> None:
    """The ledger side of K5: an admission ending after the date of the provider
    generation that states it is bounded by that generation date, never believed."""

    windows = [
        _window("matrixport", "flows", FIRST_DAY, "2026-10-07"),
        _window("matrixport", "capital_anchors", FIRST_DAY, "2026-10-07"),
    ]
    facts = _facts(FIRST_DAY, FIRST_DAY)

    unbounded = derive_a._custody_coverage_end(windows, "matrixport", "acct", facts)
    bounded = derive_a._custody_coverage_end(
        windows, "matrixport", "acct", facts, generation_end=CAPTURE_DAY
    )

    assert unbounded == day("2026-10-07")
    assert bounded == CAPTURE_DAY


def test_a_pair_that_both_bound_their_end_agree_on_the_capture_day() -> None:
    future = _ms("2026-10-07T00:00:00")
    windows = [
        _window("matrixport", "flows", FIRST_DAY, "2026-10-07"),
        _window("matrixport", "capital_anchors", FIRST_DAY, "2026-10-07"),
    ]
    a = derive_a._custody_coverage_end(
        windows,
        "matrixport",
        "acct",
        _facts(FIRST_DAY, FIRST_DAY),
        generation_end=CAPTURE_DAY,
    )
    b = derive_b.custody_coverage_end(
        "robuxio-crypto",
        {"owner": [day(FIRST_DAY)], "allocation": [day(FIRST_DAY)]},
        {},
        None,
        [*WALLET_COVERAGE, _cov(TRADE, "empty_page")],
        [
            *[
                _req(path, currency="USDT", start_time=0, end_time=future)
                for path in WALLET
            ],
            _req(TRADE, start_time=0, end_time=future),
        ],
    )

    assert (a, b) == (CAPTURE_DAY, CAPTURE_DAY)
    assert _status(a, b) == "AGREE"
