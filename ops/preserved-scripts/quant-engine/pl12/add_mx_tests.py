from pathlib import Path

path = Path('tests/provider_ledger/test_provider_ledger.py')
text = path.read_text(encoding='utf-8')

old_name = '''def test_matrixport_parity_derived_opening_nav_does_not_admit_capital_anchor(
    tmp_path: Path,
) -> None:
    run_id = "matrixport-source"'''
new_name = '''def test_matrixport_parity_derived_opening_nav_does_not_admit_capital_anchor(
    tmp_path: Path,
) -> None:
    """Without the owner-selected FX generation there is no USD basis at all."""

    run_id = "matrixport-source"'''
assert text.count(old_name) == 1
text = text.replace(old_name, new_name)

addition = '''def _matrixport_subscription_fixture(
    tmp_path: Path,
    *,
    holdings_value: str = "49891.55",
) -> tuple[object, dict[str, object]]:
    """One Robuxio subscription whose only holding is parity-labelled USDT."""

    run_id = "matrixport-source"
    csv_root = tmp_path / "output" / run_id / "csv"
    csv_root.mkdir(parents=True)
    subscription_path = csv_root / "subscription_details.csv"
    subscription_path.write_text(
        "actual_subscription_amount,confirm_nav_time_utc,order_id,product_id,"
        "subscription_amount,subscription_currency,subscription_time_utc\\n"
        "49891.55,2025-12-06T00:00:00+00:00,7402822649690198016,7361599360326447104,"
        "49891.55,USDT,2025-12-05T21:34:17.414000+00:00\\n",
        encoding="utf-8-sig",
    )
    totals_path = csv_root / "daily_portfolio_totals.csv"
    totals_path.write_text(
        "utc_date,portfolio_value_usd_observed,portfolio_value_usd_strict,"
        "strict_complete\\n2025-12-06,49891.55,49891.55,true\\n",
        encoding="utf-8-sig",
    )
    holdings_path, quality_path = _write_matrixport_valuation_evidence(
        totals_path,
        day="2025-12-06",
        value=holdings_value,
        valuation_source="assumed_stablecoin_parity",
    )
    manifest = _make_two_artifact_manifest(
        {
            "csv/daily_portfolio_totals.csv": totals_path,
            "csv/daily_holdings.csv": holdings_path,
            "csv/subscription_details.csv": subscription_path,
            "csv/valuation_quality_issues.csv": quality_path,
        }
    )
    catalog: dict[str, object] = {
        "catalog_schema_version": 2,
        "source_run_id": run_id,
    }
    return manifest, catalog


_MATRIXPORT_ANCHOR_ASSIGNMENTS = {
    "account-d2e5821e608e25f1": [
        {
            "effective_start": "2025-12-06",
            "mapping_status": "MAPPED",
            "strategy_id": "robuxio-crypto",
        }
    ]
}


def test_matrixport_capital_anchor_opens_on_the_selected_daily_close(
    tmp_path: Path,
) -> None:
    """The anchor rests on the evidence the NAV series already rests on.

    Every retained Robuxio holding is USDT labelled
    ``assumed_stablecoin_parity``, so gating the anchor on a provider-reported
    USD value dropped the only subscription the account has.  The owner-selected
    daily close converts the same holdings the NAV observations use, so the
    opening NAV is that day's account total and carries the rate that made it.
    """

    manifest, catalog = _matrixport_subscription_fixture(tmp_path)
    fx_generation_id = "f" * 64
    fx = (
        fx_generation_id,
        {("USDT", date(2025, 12, 6)): Decimal("1.0003390312194824")},
        (),
    )

    anchors = ledger._matrixport_capital_anchors(
        "gen-1",
        manifest,
        catalog,
        tmp_path,
        {"account-d2e5821e608e25f1"},
        {"robuxio-crypto"},
        _MATRIXPORT_ANCHOR_ASSIGNMENTS,
        fx_rates=fx,
    )

    assert len(anchors) == 1
    anchor = anchors[0]
    assert anchor["capital_type"] == "OPENING_CAPITAL_ANCHOR"
    assert anchor["amount"] == {
        "currency": "USDT",
        "status": "AVAILABLE",
        "value": "49891.55",
    }
    assert anchor["event_date"] == "2025-12-05"
    assert anchor["nav_recognition_date"] == "2025-12-06"
    assert anchor["nav_source_artifact_id"] == "csv/daily_holdings.csv"
    assert anchor["opening_nav"] == {
        "currency": "USD",
        "status": "AVAILABLE",
        "value": "49908.46479303836713371922176",
    }
    assert anchor["valuation_fx"] == [
        {
            "base_currency": "USD",
            "currency": "USDT",
            "date": "2025-12-06",
            "field": "Close",
            "generation_id": fx_generation_id,
            "provider": "yahoo",
            "rate": "1.0003390312194824",
            "source_artifact_id": "rates.csv",
            "timing": "UTC_DAILY_CLOSE",
        }
    ]


def test_matrixport_capital_anchor_requires_the_whole_account_in_native_units(
    tmp_path: Path,
) -> None:
    """A subscription smaller than the day's holdings is not the whole account.

    The comparison has to run in the subscription's own currency.  Comparing
    the native amount against an FX-converted total would pass or fail on the
    rate rather than on whether this subscription funded the account.
    """

    manifest, catalog = _matrixport_subscription_fixture(
        tmp_path, holdings_value="60000"
    )
    fx = ("f" * 64, {("USDT", date(2025, 12, 6)): Decimal("1")}, ())

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="MATRIXPORT_OPENING_NAV_SUBSCRIPTION_MISMATCH",
    ):
        ledger._matrixport_capital_anchors(
            "gen-1",
            manifest,
            catalog,
            tmp_path,
            {"account-d2e5821e608e25f1"},
            {"robuxio-crypto"},
            _MATRIXPORT_ANCHOR_ASSIGNMENTS,
            fx_rates=fx,
        )


def test_matrixport_capital_anchor_fails_closed_without_a_selected_rate(
    tmp_path: Path,
) -> None:
    """No owner-selected close for the recognition day means no anchor at all."""

    manifest, catalog = _matrixport_subscription_fixture(tmp_path)
    fx = ("f" * 64, {("USDT", date(2025, 12, 5)): Decimal("1.0002319812774658")}, ())

    with pytest.raises(
        ledger.ProviderLedgerError,
        match="portfolio FX lacks USDT/USD on 2025-12-06",
    ):
        ledger._matrixport_capital_anchors(
            "gen-1",
            manifest,
            catalog,
            tmp_path,
            {"account-d2e5821e608e25f1"},
            {"robuxio-crypto"},
            _MATRIXPORT_ANCHOR_ASSIGNMENTS,
            fx_rates=fx,
        )


'''

marker = '''def test_matrixport_pre_funding_assignment_cannot_be_promoted_to_owner_account() -> (
'''
assert text.count(marker) == 1
text = text.replace(marker, addition + marker, 1)

path.write_bytes(text.encode('utf-8'))
print('tests added')
