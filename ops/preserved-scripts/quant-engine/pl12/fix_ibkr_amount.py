from pathlib import Path

path = Path('tools/provider_ledger.py')
text = path.read_text(encoding='utf-8')

old = '''    by_day: dict[str, dict[date, list[Mapping[str, Any]]]] = defaultdict(
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
'''
new = '''    by_day: dict[str, dict[date, list[tuple[Mapping[str, Any], str, Decimal]]]] = (
        defaultdict(lambda: defaultdict(list))
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
            else _observed_decimal(raw_amount, "ibkr flow amount")
        )
        if amount is None:
            # An in-kind ACATS transfer, or a money observation the provider
            # declares unavailable.  Either way this account has a capital
            # event no arithmetic can net.
            unquantified.add(account_id)
            continue
        currency = _text(raw_amount.get("currency"), "ibkr flow currency")
        day = _day(row.get("event_date"), "ibkr flow event_date")
        by_day[account_id][day].append((row, currency, amount))
'''
assert text.count(old) == 1
text = text.replace(old, new)

old_net = '''            rows = by_day[account_id][day]
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
'''
new_net = '''            rows = by_day[account_id][day]
            currencies = {currency for _row, currency, _amount in rows}
            if len(currencies) != 1:
                raise ProviderLedgerError(
                    "ibkr capital events mix currencies on one event date"
                )
            currency = currencies.pop()
            net = sum((amount for _row, _currency, amount in rows), Decimal(0))
'''
assert text.count(old_net) == 1
text = text.replace(old_net, new_net)

old_recognition = '''            recognition_day = max(
                _day(
                    item.get("nav_recognition_date") or item["event_date"],
                    "ibkr flow nav_recognition_date",
                )
                for item in rows
            )'''
new_recognition = '''            recognition_day = max(
                _day(
                    item.get("nav_recognition_date") or item["event_date"],
                    "ibkr flow nav_recognition_date",
                )
                for item, _currency, _amount in rows
            )'''
assert text.count(old_recognition) == 1
text = text.replace(old_recognition, new_recognition)

old_source = '''                "source_artifact_id": _text(
                    rows[0].get("source_artifact_id"), "ibkr flow source_artifact_id"
                ),'''
new_source = '''                "source_artifact_id": _text(
                    rows[0][0].get("source_artifact_id"),
                    "ibkr flow source_artifact_id",
                ),'''
assert text.count(old_source) == 1
text = text.replace(old_source, new_source)

path.write_bytes(text.encode('utf-8'))
print('amount handling fixed')
