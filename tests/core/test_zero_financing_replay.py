"""Derived zero-financing days from a complete wallet ledger (F2, Matrixport).

Synthetic fixtures only: invented currencies, amounts and one account; no provider values.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date
from typing import Any

import pytest

import zero_financing_replay as rp
import zero_financing_statements as zf

PROVIDER = "matrixport"
ACCOUNT = "account-" + "a" * 16
FIRST = date(2026, 3, 3)
CUTOFF = date(2026, 3, 9)
BODY = json.dumps(
    {"code": 12000007, "data": None, "message": "non-prime brokerage users Forbidden"}
).encode()
BODY_SHA = hashlib.sha256(BODY).hexdigest()
BILL_HEADER = [
    "amount",
    "balance",
    "currency",
    "direction",
    "timestamp_ms",
    "timestamp_utc",
    "tx_type",
]


def _csv(header: list[str], rows: list[list[str]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode()


def _bill(
    amount: str, balance: str, cur: str, direction: str, hour: int, kind: str
) -> list[str]:
    return [
        amount,
        balance,
        cur,
        direction,
        str(1772500000000 + hour * 3600000),
        f"2026-03-03T{hour:02d}:00:00+00:00",
        kind,
    ]


def _bills(rows: list[list[str]] | None = None) -> bytes:
    default = [
        _bill("100", "100", "AAA", "1", 1, "2001"),
        _bill("100", "0", "AAA", "2", 2, "2008"),
        _bill("99", "99", "BBB", "1", 3, "2557"),
        _bill("98", "1", "BBB", "2", 4, "2855"),
    ]
    return _csv(BILL_HEADER, default if rows is None else rows)


def _balances(extra: list[list[str]] | None = None) -> bytes:
    return _csv(
        ["balance", "currency", "frozen_balance"],
        [["0", "AAA", "0"], ["1", "BBB", "0"], ["0", "CCC", "0"], *(extra or [])],
    )


def _coverage(**over: str) -> bytes:
    base = {
        "/wallet/bills": ("complete_to_observed_boundary", "no_next_id"),
        "/wallet/balance": ("complete_to_observed_boundary", "single_response"),
        "/wallet/deposits": ("complete_to_observed_boundary", "x"),
        "/wallet/withdrawals": ("complete_to_observed_boundary", "x"),
    }
    rows = [
        [
            f"GET https://host/mapi/v1{suffix}",
            over.get(suffix, status),
            over.get(suffix + "r", reason),
        ]
        for suffix, (status, reason) in base.items()
    ]
    return _csv(["endpoint", "status", "terminal_reason"], rows)


def _manifest(
    status: str = "403",
    sha: str = BODY_SHA,
    balance_at: str = "2026-03-10T01:00:00+00:00",
) -> bytes:
    return _csv(
        ["path", "status_code", "response_sha256", "requested_at_utc"],
        [
            ["/credit/agency/v1/accounts", status, sha, "2026-03-10T00:30:00+00:00"],
            ["/mapi/v1/wallet/balance", "200", "0" * 64, balance_at],
        ],
    )


def _files(**over: bytes) -> dict[str, bytes]:
    files = {
        "request_manifest.csv": _manifest(),
        "wallet_bills.csv": _bills(),
        "wallet_balances.csv": _balances(),
        "coverage.csv": _coverage(),
        "credit-accounts-response.bin": BODY,
    }
    files.update(over)
    return files


def _build(files: dict[str, bytes] | None = None, **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "provider_id": PROVIDER,
        "account_id": ACCOUNT,
        "first_day": FIRST,
        "cutoff": CUTOFF,
        "credit_response_sha256": BODY_SHA,
    }
    args.update(over)
    return rp.build(files or _files(), **args)


def test_a_closing_ledger_yields_one_derived_zero_row_per_calendar_day() -> None:
    artifact = _build()
    rows = zf.validate_rows(artifact, {ACCOUNT})
    # weekends included: a day the provider published no NAV for is covered too
    assert [r["as_of_date"] for r in rows] == [f"2026-03-{d:02d}" for d in range(3, 10)]
    assert {r["verdict_reason"] for r in rows} == {rp.VERDICT}
    assert rp.VERDICT.startswith("DERIVED_")
    assert all(r["amount"]["value"] == "0" for r in rows)
    assert rows[0]["source_artifact_sha256"] == hashlib.sha256(_bills()).hexdigest()
    assert BODY_SHA[:12] in rows[0]["source_field"]
    assert "12000007" in rows[0]["source_field"]
    assert artifact["closure"]["replay_end_state"] == {"AAA": "0", "BBB": "1"}


@pytest.mark.parametrize("kind", ["3001", "loan", "9999"])
def test_a_bill_of_a_type_not_proven_non_credit_refuses(kind: str) -> None:
    rows = [_bill("100", "100", "AAA", "1", 1, kind)]
    with pytest.raises(rp.ReplayError, match="not proven non-credit"):
        _build(_files(**{"wallet_bills.csv": _bills(rows)}))


def test_a_bill_whose_reported_balance_differs_from_the_replay_refuses() -> None:
    rows = [
        _bill("100", "100", "AAA", "1", 1, "2001"),
        _bill("100", "5", "AAA", "2", 2, "2008"),
    ]
    with pytest.raises(rp.ReplayError, match="does not close"):
        _build(_files(**{"wallet_bills.csv": _bills(rows)}))


def test_a_replay_that_does_not_reach_the_observed_wallet_refuses() -> None:
    with pytest.raises(rp.ReplayError, match="does not reach the observed wallet"):
        _build(_files(**{"wallet_balances.csv": _balances([["7", "DDD", "0"]])}))
    bad = _csv(
        ["balance", "currency", "frozen_balance"],
        [["0", "AAA", "0"], ["2", "BBB", "0"]],
    )
    with pytest.raises(rp.ReplayError, match="does not reach the observed wallet"):
        _build(_files(**{"wallet_balances.csv": bad}))


def test_a_negative_or_frozen_observed_balance_refuses() -> None:
    for row in (["-1", "ZZZ", "0"], ["0", "ZZZ", "3"]):
        with pytest.raises(rp.ReplayError, match="negative or frozen"):
            _build(_files(**{"wallet_balances.csv": _balances([row])}))


def test_a_coverage_gap_refuses() -> None:
    with pytest.raises(rp.ReplayError, match="coverage of /wallet/bills"):
        _build(_files(**{"coverage.csv": _coverage(**{"/wallet/bills": "partial"})}))
    with pytest.raises(rp.ReplayError, match="last page"):
        _build(
            _files(**{"coverage.csv": _coverage(**{"/wallet/billsr": "page_limit"})})
        )
    with pytest.raises(rp.ReplayError, match="coverage of /wallet/deposits"):
        _build(_files(**{"coverage.csv": _coverage(**{"/wallet/deposits": "partial"})}))


def test_a_wallet_balance_read_before_the_cutoff_refuses() -> None:
    stale = _manifest(balance_at="2026-03-08T00:00:00+00:00")
    with pytest.raises(rp.ReplayError, match="before the cutoff"):
        _build(_files(**{"request_manifest.csv": stale}))


def test_a_changed_403_body_or_status_refuses() -> None:
    altered = BODY.replace(b"Forbidden", b"Forbidden ")
    with pytest.raises(rp.ReplayError, match="body hash differs"):
        _build(_files(**{"credit-accounts-response.bin": altered}))
    other = json.dumps({"code": 0, "data": [], "message": "ok"}).encode()
    sha = hashlib.sha256(other).hexdigest()
    with pytest.raises(rp.ReplayError, match="not 12000007"):
        _build(
            _files(
                **{
                    "credit-accounts-response.bin": other,
                    "request_manifest.csv": _manifest(sha=sha),
                }
            ),
            credit_response_sha256=sha,
        )
    with pytest.raises(rp.ReplayError, match="not 403"):
        _build(_files(**{"request_manifest.csv": _manifest(status="200")}))
    with pytest.raises(rp.ReplayError, match="body hash differs"):
        _build(_files(**{"request_manifest.csv": _manifest(sha="1" * 64)}))


def test_a_window_before_the_first_bill_or_after_the_cutoff_refuses() -> None:
    with pytest.raises(rp.ReplayError, match="before the first wallet bill"):
        _build(first_day=date(2026, 3, 2))
    with pytest.raises(rp.ReplayError, match="cutoff precedes"):
        _build(cutoff=date(2026, 3, 2))


def test_a_missing_input_file_refuses() -> None:
    files = _files()
    del files["coverage.csv"]
    with pytest.raises(rp.ReplayError, match="missing input files"):
        _build(files)


def test_the_refusal_needs_both_the_code_and_the_non_prime_message() -> None:
    for body in (
        {"code": 1, "data": None, "message": "non-prime brokerage users Forbidden"},
        {"code": 12000007, "data": None, "message": "denied"},
    ):
        raw = json.dumps(body).encode()
        sha = hashlib.sha256(raw).hexdigest()
        with pytest.raises(rp.ReplayError, match="not 12000007"):
            _build(
                _files(
                    **{
                        "credit-accounts-response.bin": raw,
                        "request_manifest.csv": _manifest(sha=sha),
                    }
                ),
                credit_response_sha256=sha,
            )


def test_a_replayed_currency_absent_from_the_observed_wallet_refuses() -> None:
    only_aaa = _csv(["balance", "currency", "frozen_balance"], [["0", "AAA", "0"]])
    with pytest.raises(rp.ReplayError, match="absent from the observed wallet"):
        _build(_files(**{"wallet_balances.csv": only_aaa}))
