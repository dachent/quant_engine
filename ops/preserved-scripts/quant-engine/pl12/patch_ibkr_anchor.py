from pathlib import Path

path = Path('tools/provider_ledger.py')
text = path.read_text(encoding='utf-8')

# 1. Declare the fact so the class can materialize.
old_facts = '''    "ibkr": (
        "corporate_actions",
        "flows",
        "nav_observations",
        "positions",
        "transactions",
    ),'''
new_facts = '''    "ibkr": (
        "capital_anchors",
        "corporate_actions",
        "flows",
        "nav_observations",
        "positions",
        "transactions",
    ),'''
assert text.count(old_facts) == 1
text = text.replace(old_facts, new_facts)

# 2. The producer, beside the projection it feeds.
producer = '''_IBKR_CAPITAL_FLOW_TYPES: Final = (
    "DEPOSIT_OR_WITHDRAWAL",
    "TRANSFER:ACATS:IN",
    "TRANSFER:INTERNAL:IN",
    "TRANSFER:INTERNAL:OUT",
)


def _ibkr_capital_anchors(
    generation_id: str,
    catalog: Mapping[str, Any],
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Net each statement account's capital events on their own event date.

    A single flow row is not a capital event.  IBKR credits a deposit advance
    against a pending wire and then reverses it: both rows carry the same event
    date, and reading either one alone makes money that never funded the account
    look like its opening capital.  Small end-of-month adjustments offset the
    same way.  Netting the day is what distinguishes capital from bookkeeping.

    Internal transfers between the owner's own statement accounts are capital
    events for the receiving account even though they are not external capital
    for the household: strategies are mapped to accounts, so an account funded
    by its sibling is funded.  The external/internal distinction stays on the
    flow rows, which carry ``capital_disposition``.

    An account whose capital history contains an amount-less event -- an
    in-kind ACATS transfer -- has no arithmetic that could net its days, so it
    yields no anchors at all rather than a series with a hole in it.
    """

    flow_rows = _objects(catalog.get("flows") or (), "ibkr.flows")
    nav_rows = _objects(catalog.get("nav_observations") or (), "ibkr.nav_observations")
    nav_by_account_day: dict[tuple[str, date], Decimal] = {}
    for row in nav_rows:
        account_id = _text(row.get("account_id"), "ibkr nav account_id")
        day = _day(row.get("as_of_date"), "ibkr nav as_of_date")
        value = _observed_decimal(
            _object(row.get("nav"), "ibkr nav").get("value"), "ibkr nav value"
        )
        if value is None:
            continue
        key = (account_id, day)
        if nav_by_account_day.setdefault(key, value) != value:
            raise ProviderLedgerError("ibkr account NAV has conflicting duplicates")

    by_day: dict[str, dict[date, list[Mapping[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    unquantified: set[str] = set()
    for row in flow_rows:
        if row.get("flow_type") not in _IBKR_CAPITAL_FLOW_TYPES:
            continue
        if row.get("provider_account_scope") != "STATEMENT_ACCOUNT":
            continue
        account_id = _text(row.get("account_id"), "ibkr flow account_id")
        raw_amount = row.get("amount")
        amount = (
            None
            if not isinstance(raw_amount, Mapping)
            else _observed_decimal(raw_amount.get("value"), "ibkr flow amount")
        )
        if amount is None:
            unquantified.add(account_id)
            continue
        day = _day(row.get("event_date"), "ibkr flow event_date")
        by_day[account_id][day].append(row)

    anchors: list[dict[str, Any]] = []
    for account_id in sorted(by_day):
        if account_id in unquantified:
            continue
        opened = False
        for day in sorted(by_day[account_id]):
            rows = by_day[account_id][day]
            currencies = {
                _text(
                    _object(item["amount"], "ibkr flow amount").get("currency"),
                    "ibkr flow currency",
                )
                for item in rows
            }
            if len(currencies) != 1:
                raise ProviderLedgerError(
                    "ibkr capital events mix currencies on one event date"
                )
            currency = currencies.pop()
            net = sum(
                (
                    _decimal(
                        _object(item["amount"], "ibkr flow amount")["value"],
                        "ibkr flow amount",
                    )
                    for item in rows
                ),
                Decimal(0),
            )
            if net == 0:
                # An advance and its reversal, or offsetting adjustments.
                continue
            if net < 0:
                if not opened:
                    raise ProviderLedgerError(
                        "ibkr account returns capital before it is funded"
                    )
                # Capital returned is carried by the flow rows, which declare
                # their own external or internal disposition.
                continue
            recognition_day = max(
                _day(
                    item.get("nav_recognition_date") or item["event_date"],
                    "ibkr flow nav_recognition_date",
                )
                for item in rows
            )
            strategy_id, mapping_status = _mapped_strategy_for_account(
                account_id, recognition_day, accounts, strategies, assignments
            )
            source_record_id = f"{account_id}:{day.isoformat()}"
            anchor: dict[str, Any] = {
                "account_id": account_id,
                "amount": {
                    "currency": currency,
                    "status": "AVAILABLE",
                    "value": format(net.normalize(), "f"),
                },
                "capital_type": (
                    "OPENING_CAPITAL_ANCHOR"
                    if not opened
                    else "STRATEGY_CAPITAL_ALLOCATION"
                ),
                "event_date": day.isoformat(),
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_id": generation_id,
                "mapping_basis": "NET_STATEMENT_ACCOUNT_CAPITAL_ON_PROVIDER_EVENT_DATE",
                "mapping_status": mapping_status,
                "nav_recognition_date": recognition_day.isoformat(),
                "provider_id": "ibkr",
                "record_id": f"ibkr-capital-anchor:{source_record_id}",
                "source_artifact_id": _text(
                    rows[0].get("source_artifact_id"), "ibkr flow source_artifact_id"
                ),
                "source_record_id": source_record_id,
                "strategy_id": strategy_id,
            }
            if not opened:
                opening_nav = nav_by_account_day.get((account_id, recognition_day))
                if opening_nav is None or opening_nav <= 0:
                    raise ProviderLedgerError(
                        "IBKR_OPENING_NAV_UNAVAILABLE: the account holds no "
                        "positive NAV on the day its capital is recognized"
                    )
                anchor["opening_nav"] = {
                    "currency": currency,
                    "status": "AVAILABLE",
                    "value": format(opening_nav.normalize(), "f"),
                }
                anchor["nav_source_artifact_id"] = "normalized/ibkr-catalog.json"
                opened = True
            anchors.append(anchor)
    anchors.sort(key=lambda item: (str(item["event_date"]), str(item["record_id"])))
    return anchors


def _project_account_facts(
'''
old_project = '''def _project_account_facts(
'''
assert text.count(old_project) == 1
text = text.replace(old_project, producer)

# 3. Dispatch before the catalog collection lookup, which has no such key.
old_dispatch = '''    for source_fact in _ACCOUNT_FACTS[provider_id]:
        target_fact = "flows" if source_fact == "cash_flows" else source_fact
        if provider_id in _CSV_FACT_ACCOUNT and source_fact in {'''
new_dispatch = '''    for source_fact in _ACCOUNT_FACTS[provider_id]:
        target_fact = "flows" if source_fact == "cash_flows" else source_fact
        if provider_id == "ibkr" and source_fact == "capital_anchors":
            rows = _ibkr_capital_anchors(
                generation_id, catalog, accounts, strategies, assignments
            )
            projections[target_fact].extend(rows)
            for row in rows:
                if row["mapping_status"] == "UNMAPPED":
                    exceptions.append(
                        {
                            "account_in_provenance": row["account_id"] in accounts,
                            "effective_date": row["event_date"],
                            "fact_type": target_fact,
                            "provider_id": provider_id,
                            "record_id": row["record_id"],
                            "status": "UNMAPPED",
                        }
                    )
            continue
        if provider_id in _CSV_FACT_ACCOUNT and source_fact in {'''
assert text.count(old_dispatch) == 1
text = text.replace(old_dispatch, new_dispatch)

path.write_bytes(text.encode('utf-8'))
print('ibkr capital anchor producer added')
