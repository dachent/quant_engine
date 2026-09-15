"""A strategy with no backtest layer is available in hybrid, not unready.

LIVE_PLUS_BACKTEST means live plus backtest where a backtest exists. For a
strategy the product has ruled has none -- volatis-strategy is model live plus
funded live -- the hybrid view is simply its live view, and calling that
REQUIRED_NOT_READY held the whole mode there behind a layer that will never
arrive.

The builder and its verifier both decide this, so both learn the same rule from
the same place: the correction that declares the layer does not exist.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
TARGET = REPO / 'tools' / 'portfolio_target_series.py'

HELPER = '''

def _strategies_without_a_backtest_layer(
    corrections: Mapping[str, Mapping[str, object]]
) -> set[str]:
    """Strategies whose BACKTEST layer the product records as non-existent.

    This is not the same as a backtest that has not been captured or proved: it
    is a ruling that none exists to capture. Such a strategy is complete in
    LIVE_PLUS_BACKTEST the moment its live layer is, because the mode admits
    prehistory where prehistory exists and there is none to admit.
    """

    without: set[str] = set()
    for row in corrections.values():
        if not isinstance(row, Mapping):
            continue
        if row.get("evidence_layer") != "BACKTEST":
            continue
        existence = row.get("history_existence")
        if not isinstance(existence, Mapping):
            continue
        if existence.get("status") == "DOES_NOT_EXIST":
            without.add(str(row.get("strategy_id")))
    return without
'''

BUILDER_OLD = '''        if (
            exclusion_reason is None
            and hybrid_state == "AVAILABLE"
            and not backtest_rows
        ):
            hybrid_state = "REQUIRED_NOT_READY"
'''
BUILDER_NEW = '''        if (
            exclusion_reason is None
            and hybrid_state == "AVAILABLE"
            and not backtest_rows
            and strategy_id not in no_backtest_layer
        ):
            hybrid_state = "REQUIRED_NOT_READY"
'''

VERIFIER_OLD = '''        if (
            exclusion_reason is None
            and hybrid_status == "AVAILABLE"
            and not admitted_backtest
        ):
            hybrid_status = "REQUIRED_NOT_READY"
'''
VERIFIER_NEW = '''        if (
            exclusion_reason is None
            and hybrid_status == "AVAILABLE"
            and not admitted_backtest
            and strategy_id not in no_backtest_layer
        ):
            hybrid_status = "REQUIRED_NOT_READY"
'''


def main():
    text = TARGET.read_text(encoding='utf-8')
    if '_strategies_without_a_backtest_layer' in text:
        print('already applied')
        return 0
    if '\r\n' in text:
        raise SystemExit('refusing to edit a file that already carries CRLF')

    anchor = '\n\ndef build_layered_history_generation(\n'
    if text.count(anchor) != 1:
        raise SystemExit('helper anchor is not unique')
    text = text.replace(anchor, HELPER + anchor, 1)

    for label, old, new in (
        ('builder', BUILDER_OLD, BUILDER_NEW),
        ('verifier', VERIFIER_OLD, VERIFIER_NEW),
    ):
        if text.count(old) != 1:
            raise SystemExit('%s anchor is not unique (%d)' % (label, text.count(old)))
        text = text.replace(old, new, 1)
        print('applied:', label)

    TARGET.write_bytes(text.encode('utf-8'))
    print('wrote', TARGET.name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
