"""Report which p123-eu flow leg is not linked to an observed NAV interval."""
import json
from pathlib import Path

G = Path(
    r'C:/Dev/scratch/quant-engine/pl12/epoch/account-manifest/generations'
    r'/1e59f762034ada9400b1c4b53ca79ead477397b5ca6d665a603b2bca0dc7ee0e'
)
ACCOUNT = 'account-d69701da9143c430'
STRATEGY = 'p123-eu'

classified = json.loads((G / 'authority/classified-flows.json').read_text('utf-8'))
inputs = json.loads((G / 'authority/calculation-inputs.json').read_text('utf-8'))

print('classified keys:', sorted(classified))
events = [
    row
    for row in classified.get('events', [])
    if row.get('account_id') == ACCOUNT
]
print('events for account:', len(events))
for row in sorted(events, key=lambda r: (str(r.get('nav_recognition_date')),
                                         str(r.get('record_id')))):
    print(' ', row.get('event_date'), '->', row.get('nav_recognition_date'),
          row.get('classification'), row.get('amount', {}).get('value'),
          row.get('strategy_id'), row.get('record_id'))

print()
print('inputs keys:', sorted(inputs)[:20])
navs = [
    row
    for row in inputs.get('nav_observations', [])
    if row.get('account_id') == ACCOUNT
]
days = sorted(str(row['as_of_date']) for row in navs)
print('nav days:', len(days), days[:3], days[-3:] if days else None)
gaps = []
for a, b in zip(days, days[1:]):
    from datetime import date
    da, db = date.fromisoformat(a), date.fromisoformat(b)
    if (db - da).days > 1:
        gaps.append((a, b, (db - da).days))
print('gaps over one day:', gaps[:20])
