"""Check whether the owner's EU instance holds what the designer model holds.

The contract asserts EXACT_MATCH between the owner's connected model version
and the designer model whose curve is admitted as its backtest. The subscription
payload calls itself a FOLLOW, but that is the provider's own word for it. If
the two really are one specification, the positions on the same day are the
same names; if the copy re-specifies anything, they are not.
"""
import json
from pathlib import Path

RAW = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio123/source-runs/capture-20260912-mvp2/raw'
)


def tickers(node, found=None):
    """Collect anything that looks like a position ticker from a payload."""
    found = set() if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {'ticker', 'symbol'} and isinstance(value, str) and value.strip():
                found.add(value.strip().upper())
            else:
                tickers(value, found)
    elif isinstance(node, list):
        for item in node:
            tickers(item, found)
    return found


def main():
    model = json.loads((RAW / 'r2g_1764562_holdings.json').read_text(encoding='utf-8'))
    model_names = tickers(model)
    print('designer model positions: %d' % len(model_names))
    print('  %s' % ', '.join(sorted(model_names)))

    path = RAW / 'p123_invest_holdings_account_wide.json'
    if not path.is_file():
        print('\naccount holdings payload absent')
        return 0
    account = json.loads(path.read_text(encoding='utf-8'))

    # The account payload covers every investment, so the EU rows are the ones
    # whose group names the model.
    def groups(node, label=None, out=None):
        out = {} if out is None else out
        if isinstance(node, dict):
            name = node.get('assetName') or node.get('name') or label
            if isinstance(name, str) and 'microcap model' in name.lower():
                out.setdefault(name, set()).update(tickers(node))
            for value in node.values():
                groups(value, name if isinstance(name, str) else label, out)
        elif isinstance(node, list):
            for item in node:
                groups(item, label, out)
        return out

    matched = groups(account)
    if not matched:
        print('\nno EU-labelled group in the account holdings payload')
        print('all account tickers: %d' % len(tickers(account)))
        overlap = model_names & tickers(account)
        print('overlap with the model: %d of %d' % (len(overlap), len(model_names)))
        print('  %s' % ', '.join(sorted(overlap)))
        return 0

    for name, names in matched.items():
        print('\naccount group %r: %d positions' % (name[:60], len(names)))
        print('  same    : %s' % ', '.join(sorted(model_names & names)))
        print('  model   only: %s' % ', '.join(sorted(model_names - names)))
        print('  account only: %s' % ', '.join(sorted(names - model_names)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
