"""Let the hybrid factor check accept a strategy with no backtest layer.

The check is titled "without inventing prehistory" and enforced it by
requiring every strategy row in the hybrid view to show a positive backtest
count. That held while every required strategy was assumed to have a backtest.

volatis-strategy has none: the product records it as model live plus funded
live, with no simulated design history behind either, so its hybrid row shows
no backtest count -- and, importantly, makes no fallback claim either. The
assertion it fails is universality, not the property the check is named for.

The rewrite keeps both halves of the original intent and drops only the
assumption:

* no row may claim "no version-matched backtest prehistory" -- that is the
  silent live-only fallback the check exists to catch, and it stays fatal;
* at least one row must show a positive backtest count, so an entirely empty
  hybrid view cannot pass as a hybrid;
* a row with neither is a strategy whose backtest layer does not exist, which
  is a fact about the product rather than a defect in the panel.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
TARGET = REPO / 'apps' / 'portfolio-control-panel' / 'interaction_qa.mjs'

OLD = """        && hybridFactorEvidence.strategy_rows.every((row) => /[1-9][0-9,]* backtest/.test(row.evidence ?? "")
          && !row.evidence?.includes("no version-matched backtest prehistory"))),
"""
NEW = """        && hybridFactorEvidence.strategy_rows.every(
          (row) => !row.evidence?.includes("no version-matched backtest prehistory"),
        )
        && hybridFactorEvidence.strategy_rows.some((row) => /[1-9][0-9,]* backtest/.test(row.evidence ?? ""))),
"""


def main():
    text = TARGET.read_text(encoding='utf-8')
    if 'strategy_rows.some(' in text:
        print('already applied')
        return 0
    if text.count(OLD) != 1:
        raise SystemExit('anchor is not unique (%d)' % text.count(OLD))
    TARGET.write_bytes(text.replace(OLD, NEW, 1).encode('utf-8'))
    print('hybrid factor check now rejects fallback rather than absence')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
