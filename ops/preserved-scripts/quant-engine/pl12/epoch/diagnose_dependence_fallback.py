"""Explain why hybrid dependence carries no backtest rows.

`_evidence_mode_returns` keeps only rows whose `strategy_version` equals the
one its spec states for that series, then takes BACKTEST rows from what
survives. If the layered history's backtest rows carry a different version
string from the spec's, every one of them is dropped and the mode falls back to
`AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY` -- which reads as
available while being live-only.

This compares the two sides per series so the cause is named rather than
inferred.
"""
import csv
import json
from pathlib import Path

Q = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine')
DEPENDENCE = Q / (
    'dataPortfolio/rollforward/2026-09-10-mvp2-live-only/dependence'
    '/e8be271bb564a39655d77b941cc9cf000e02e626c1aac3bfa1bece871a853f44'
)
HISTORY = Q / (
    'dataPortfolio/rollforward/2026-09-10-mvp2-live-only/layered-history'
    '/115650bf2cb24a16893641ef3a455a066a31beaef5228fd5c6b77a6b8f3540a0'
)


def main():
    spec = json.loads((DEPENDENCE / 'spec.json').read_text(encoding='utf-8'))
    wanted = {}
    for key in ('evidence_mode_series', 'series', 'strategies'):
        rows = spec.get(key)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            for row in rows:
                series_id = row.get('series_id')
                version = row.get('strategy_version')
                if series_id and version:
                    wanted[series_id] = version
            if wanted:
                print('spec versions read from %r' % key)
                break
    if not wanted:
        print('no per-series strategy_version in the dependence spec; keys: %s'
              % sorted(spec))
        return 1

    carried = {}
    path = HISTORY / 'live-plus-backtest.csv'
    with path.open(newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            if row.get('evidence_layer') != 'BACKTEST':
                continue
            carried.setdefault(row['series_id'], set()).add(row['strategy_version'])

    print('\n%-42s %-9s %s' % ('series', 'backtest', 'spec version == backtest version'))
    for series_id in sorted(set(wanted) | set(carried)):
        versions = carried.get(series_id, set())
        want = wanted.get(series_id)
        if not versions:
            print('%-42s %-9s (no backtest rows)' % (series_id[:42], 0))
            continue
        match = want in versions
        print('%-42s %-9d %s' % (series_id[:42], len(versions), match))
        if not match:
            print('    spec      : %s' % want)
            for version in sorted(versions):
                print('    carried   : %s' % version)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
