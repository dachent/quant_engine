from pathlib import Path

path = Path('tools/provider_ledger.py')
text = path.read_text(encoding='utf-8')

# 1. New helper, placed beside the FX total it complements.
anchor = '''def _matrixport_flows(
    generation_id: str,
'''
helper = '''def _matrixport_daily_native_totals(
    manifest: GenerationManifest,
    run_root: Path,
) -> dict[date, dict[str, Decimal]]:
    """Total each day's active native holdings in their own currency.

    The owner-selected FX total proves the account's USD value.  Proving that
    one subscription funded the whole account has to happen in the currency the
    subscription was denominated in, where the comparison is exact and no rate
    stands between the two figures.
    """

    artifact_id = "csv/daily_holdings.csv"
    artifact = _find_manifest_artifact(
        manifest, artifact_id, "matrixport daily holdings"
    )
    rows = _verified_csv_rows(
        _exact_file(run_root, Path(artifact_id), "matrixport daily holdings"),
        artifact.sha256,
        {"currency", "native_value", "share", "utc_date"},
        "matrixport daily holdings",
    )
    totals: dict[date, dict[str, Decimal]] = defaultdict(dict)
    incomplete_days: set[date] = set()
    with localcontext() as context:
        context.prec = 80
        for row in rows:
            day = _day(row.get("utc_date"), "matrixport daily holdings utc_date")
            share = _observed_decimal(
                row.get("share"), "matrixport daily holdings share"
            )
            if share is None:
                raise ProviderLedgerError(
                    "MATRIXPORT_ACCOUNT_COMPONENT_INVALID: active share is absent"
                )
            if share == 0:
                continue
            native = _observed_decimal(
                row.get("native_value"), "matrixport native holding value"
            )
            if native is None:
                incomplete_days.add(day)
                continue
            currency = _text(row.get("currency"), "matrixport holding currency")
            totals[day][currency] = totals[day].get(currency, Decimal(0)) + native
    for day in incomplete_days:
        totals.pop(day, None)
    return dict(totals)


'''
assert text.count(anchor) == 1
text = text.replace(anchor, helper + anchor)

# 2. Accept the owner-selected FX generation in the anchor producer.
old_signature = '''def _matrixport_capital_anchors(
    generation_id: str,
    manifest: GenerationManifest,
    catalog: Mapping[str, Any],
    root: Path,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:'''
new_signature = '''def _matrixport_capital_anchors(
    generation_id: str,
    manifest: GenerationManifest,
    catalog: Mapping[str, Any],
    root: Path,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
    fx_rates: tuple[
        str,
        Mapping[tuple[str, date], Decimal],
        tuple[SourceArtifact, SourceArtifact],
    ]
    | None = None,
) -> list[dict[str, Any]]:'''
assert text.count(old_signature) == 1
text = text.replace(old_signature, new_signature)

# 3. Gate on the same evidence the NAV series is already built from.
old_gate = '''    source_backed_dates = _matrixport_source_backed_usd_dates(manifest, run_root)

    def timestamp_day(value: object, name: str) -> date:'''
new_gate = '''    # Every retained Robuxio holding is USDT labelled
    # ``assumed_stablecoin_parity``, so the provider-reported USD route
    # qualifies no day at all and the one subscription was silently skipped.
    # The NAV series does not depend on that route: it converts each native
    # holding by the owner-selected daily close.  The anchor now rests on the
    # same evidence.
    fx_daily_totals: dict[date, tuple[Decimal, list[dict[str, Any]]]] | None = None
    native_totals: dict[date, dict[str, Decimal]] = {}
    source_backed_dates: set[date] = set()
    if fx_rates is None:
        source_backed_dates = _matrixport_source_backed_usd_dates(manifest, run_root)
    else:
        fx_daily_totals = _matrixport_daily_fx_totals(manifest, run_root, fx_rates)
        native_totals = _matrixport_daily_native_totals(manifest, run_root)

    def timestamp_day(value: object, name: str) -> date:'''
assert text.count(old_gate) == 1
text = text.replace(old_gate, new_gate)

old_skip = '''        if recognition_day not in source_backed_dates:
            continue
        if recognition_day < event_day:'''
new_skip = '''        qualified = (
            recognition_day in source_backed_dates
            if fx_daily_totals is None
            else recognition_day in fx_daily_totals
        )
        if not qualified:
            continue
        if recognition_day < event_day:'''
assert text.count(old_skip) == 1
text = text.replace(old_skip, new_skip)

# 4. Value the opening NAV through the owner-selected conversion.
old_opening = '''        if opening:
            matched_nav_rows = nav_rows_by_day.get(recognition_day, [])'''
new_opening = '''        if opening and fx_daily_totals is not None:
            subscription_currency = _text(
                row.get("subscription_currency"),
                "matrixport subscription currency",
            )
            recognition_holdings = native_totals.get(recognition_day, {})
            if set(recognition_holdings) != {subscription_currency}:
                raise ProviderLedgerError(
                    "MATRIXPORT_OPENING_NAV_UNAVAILABLE: the recognition day's "
                    "active holdings are not the subscription currency alone"
                )
            if recognition_holdings[subscription_currency].quantize(
                Decimal("0.01")
            ) != amount.quantize(Decimal("0.01")):
                raise ProviderLedgerError("MATRIXPORT_OPENING_NAV_SUBSCRIPTION_MISMATCH")
            opening_value, valuation_fx = fx_daily_totals[recognition_day]
            if opening_value <= 0:
                raise ProviderLedgerError(
                    "MATRIXPORT_OPENING_NAV_UNAVAILABLE: the owner-selected "
                    "conversion is non-positive"
                )
            anchor["opening_nav"] = {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": format(opening_value.normalize(), "f"),
            }
            anchor["nav_source_artifact_id"] = "csv/daily_holdings.csv"
            anchor["valuation_fx"] = valuation_fx
        elif opening:
            matched_nav_rows = nav_rows_by_day.get(recognition_day, [])'''
assert text.count(old_opening) == 1
text = text.replace(old_opening, new_opening)

# 5. Wire the generation through at the call site.
old_call = '''                    _matrixport_capital_anchors(
                        generation_id,
                        manifest,
                        catalog,
                        root,
                        accounts,
                        strategies,
                        assignments,
                    )'''
new_call = '''                    _matrixport_capital_anchors(
                        generation_id,
                        manifest,
                        catalog,
                        root,
                        accounts,
                        strategies,
                        assignments,
                        fx_rates=fx_rates,
                    )'''
assert text.count(old_call) == 1
text = text.replace(old_call, new_call)

path.write_bytes(text.encode('utf-8'))
print('provider_ledger.py patched')
