"""Net IBKR capital on the date NAV recognises it, not the date it was entered.

Netting by `event_date` collapsed a deposit advance against a reversal that
NAV did not see for another eight days: the +3,572.46 advance recognises
2025-11-24, when the account's NAV rose 4,763.28 -> 8,417.94, while its
-3,572.46 reversal recognises 2025-12-02. Treating the pair as one zero-net
day hid real capital for 11-24..12-01, and collapsed the 2026-02-23/24
round trip into a single +129.74. Both are visible in NAV.

Grouping on the recognition date reproduces what NAV shows, so a day whose net
is negative is capital genuinely returned and is emitted rather than dropped.
"""
from pathlib import Path

path = Path('tools/provider_ledger.py')
text = path.read_text(encoding='utf-8')

old_doc = '''    A single flow row is not a capital event.  IBKR credits a deposit advance
    against a pending wire and then reverses it: both rows carry the same event
    date, and reading either one alone makes money that never funded the account
    look like its opening capital.  Small end-of-month adjustments offset the
    same way.  Netting the day is what distinguishes capital from bookkeeping.
'''
new_doc = '''    A single flow row is not a capital event, and the date a row is entered is
    not the date its capital exists.  IBKR credits a deposit advance against a
    pending wire and reverses it later: the advance and its reversal share an
    event date but recognise eight days apart, and the account's NAV rose by
    the advance on the day it recognised.  Netting the recognition date is what
    reproduces the capital NAV actually holds; netting the entry date hides it.
    Small end-of-month adjustments still offset, because they recognise
    together.
'''
assert text.count(old_doc) == 1
text = text.replace(old_doc, new_doc)

old_group = '''    by_day: dict[str, dict[date, list[tuple[Mapping[str, Any], str, Decimal]]]] = (
        defaultdict(lambda: defaultdict(list))
    )'''
new_group = '''    # Keyed by recognition date; each entry keeps its own event date so the
    # anchor can still report when the capital was committed.
    by_day: dict[
        str, dict[date, list[tuple[Mapping[str, Any], str, Decimal, date]]]
    ] = defaultdict(lambda: defaultdict(list))'''
assert text.count(old_group) == 1
text = text.replace(old_group, new_group)

old_append = '''        currency = _text(raw_amount.get("currency"), "ibkr flow currency")
        day = _day(row.get("event_date"), "ibkr flow event_date")
        by_day[account_id][day].append((row, currency, amount))
'''
new_append = '''        currency = _text(raw_amount.get("currency"), "ibkr flow currency")
        event_day = _day(row.get("event_date"), "ibkr flow event_date")
        recognition_day = _day(
            row.get("nav_recognition_date") or row["event_date"],
            "ibkr flow nav_recognition_date",
        )
        if recognition_day < event_day:
            raise ProviderLedgerError(
                "ibkr capital event recognises before it is entered"
            )
        by_day[account_id][recognition_day].append(
            (row, currency, amount, event_day)
        )
'''
assert text.count(old_append) == 1
text = text.replace(old_append, new_append)

old_net = '''            rows = by_day[account_id][day]
            currencies = {currency for _row, currency, _amount in rows}
            if len(currencies) != 1:
                raise ProviderLedgerError(
                    "ibkr capital events mix currencies on one event date"
                )
            currency = currencies.pop()
            net = sum((amount for _row, _currency, amount in rows), Decimal(0))
'''
new_net = '''            rows = by_day[account_id][day]
            currencies = {currency for _row, currency, _amount, _event in rows}
            if len(currencies) != 1:
                raise ProviderLedgerError(
                    "ibkr capital events mix currencies on one recognition date"
                )
            currency = currencies.pop()
            net = sum(
                (amount for _row, _currency, amount, _event in rows), Decimal(0)
            )
'''
assert text.count(old_net) == 1
text = text.replace(old_net, new_net)

old_negative = '''            if net < 0:
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
                for item, _currency, _amount in rows
            )
'''
new_negative = '''            if net < 0 and not opened:
                raise ProviderLedgerError(
                    "ibkr account returns capital before it is funded"
                )
            # The day's capital is committed once its last component is entered,
            # and recognised on the grouping date.
            recognition_day = day
            event_day = max(event for _row, _currency, _amount, event in rows)
'''
assert text.count(old_negative) == 1
text = text.replace(old_negative, new_negative)

old_record = '''            source_record_id = f"{account_id}:{day.isoformat()}"'''
new_record = '''            source_record_id = f"{account_id}:{recognition_day.isoformat()}"'''
assert text.count(old_record) == 1
text = text.replace(old_record, new_record)

old_event = '''                "event_date": day.isoformat(),'''
new_event = '''                "event_date": event_day.isoformat(),'''
assert text.count(old_event) == 1
text = text.replace(old_event, new_event)

old_source_artifact = '''                "source_artifact_id": _text(
                    rows[0][0].get("source_artifact_id"),
                    "ibkr flow source_artifact_id",
                ),'''
new_source_artifact = '''                "source_artifact_id": _text(
                    rows[0][0].get("source_artifact_id"),
                    "ibkr flow source_artifact_id",
                ),'''
assert text.count(old_source_artifact) == 1

old_open = '''            if not opened:
                opening_nav = nav_by_account_day.get((account_id, recognition_day))'''
new_open = '''            if not opened and net > 0:
                opening_nav = nav_by_account_day.get((account_id, recognition_day))'''
assert text.count(old_open) == 1
text = text.replace(old_open, new_open)

old_sort = '''    anchors.sort(key=lambda item: (str(item["event_date"]), str(item["record_id"])))'''
new_sort = '''    anchors.sort(
        key=lambda item: (str(item["nav_recognition_date"]), str(item["record_id"]))
    )'''
assert text.count(old_sort) == 1
text = text.replace(old_sort, new_sort)

path.write_bytes(text.encode('utf-8'))
print('ibkr anchors now net on the recognition date')
