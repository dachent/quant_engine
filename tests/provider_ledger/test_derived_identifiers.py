"""Identifiers the provider ledger derives instead of storing."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

from tools import provider_ledger as ledger


def test_csv_provider_accounts_follow_the_provenance_alias_rule() -> None:
    for provider_id, account_id in ledger._CSV_FACT_ACCOUNT.items():
        alias = provider_id.capitalize()
        digest = hashlib.sha256(alias.encode("utf-8")).hexdigest()
        assert account_id == f"account-{digest[:16]}"
    assert len(set(ledger._CSV_FACT_ACCOUNT.values())) == len(ledger._CSV_FACT_ACCOUNT)


def test_institution_account_id_is_the_alias_hash_prefix() -> None:
    digest = hashlib.sha256(b"Synthetic Institution | Synthetic Account").hexdigest()
    assert (
        ledger._institution_account_id("Synthetic Institution | Synthetic Account")
        == f"account-{digest[:16]}"
    )


def _row(fund: str, title: str = "") -> dict[str, str]:
    return {"fund": fund, "fund_title": title}


def test_quantbase_fund_is_the_one_titled_like_the_dashboard_series() -> None:
    rows = [
        _row("11", "Synthetic Retired Fund"),
        _row("12", ledger._QUANTBASE_FUND_TITLE),
        _row("12"),
        _row("13"),
        _row(""),
    ]
    assert ledger._quantbase_fund_id(rows) == "12"


def test_quantbase_fund_of_a_capture_without_titles_is_its_single_fund() -> None:
    assert ledger._quantbase_fund_id([_row("12"), _row("12"), _row("")]) == "12"


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [_row("")],
        [_row("12"), _row("13")],
        [
            _row("12", ledger._QUANTBASE_FUND_TITLE),
            _row("13", ledger._QUANTBASE_FUND_TITLE),
        ],
        [_row("12", "Synthetic Retired Fund"), _row("13")],
    ],
)
def test_quantbase_fund_identity_fails_closed_when_it_is_not_unique(
    rows: list[dict[str, str]],
) -> None:
    with pytest.raises(ledger.ProviderLedgerError, match="QUANTBASE_FUND_IDENTITY"):
        ledger._quantbase_fund_id(rows)


def _disclosure_script():
    path = (
        Path(__file__).resolve().parents[2]
        / "ops/mvp2-release/scripts/build_disclosure_inputs.py"
    )
    spec = importlib.util.spec_from_file_location("build_disclosure_inputs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_disclosure_entries_are_read_from_the_evidence_headers() -> None:
    script = _disclosure_script()
    account = "account-" + "ab" * 8
    other = "account-" + "cd" * 8
    text = (
        f"=== strategy-a LABEL 1 {account}  legs=5 (external=2)\n"
        "committed TWR  1.0 %\n"
        f"=== strategy-b LABEL 2 {other}  legs=3 (external=1)\n"
    )
    assert script.entries_from_evidence(text) == (
        ("strategy-a", account),
        ("strategy-b", other),
    )
    with pytest.raises(script.Refused):
        script.entries_from_evidence("no sleeve headers here\n")
    with pytest.raises(script.Refused):
        script.entries_from_evidence(text + text)
