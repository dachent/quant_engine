"""Let the hybrid version gate skip a lens that does not model the strategy.

The gate reads, for every eligible strategy, the hybrid series composition and
requires it to name the registry's version. A lens that never models that
strategy still emits a row for it -- `_unavailable_factor_row`, which carries no
`series_composition` at all -- so the gate demanded a composition that cannot
exist and failed with `must be an object`.

Every required strategy is method-scoped out of at least one lens (p123-eu is
routed to europe-economic alone), so this could not pass in any real two-mode
run. It went unnoticed because the hybrid panel path had never been exercised.

The skip is deliberately narrow. It applies only to a row with no model at all,
which therefore has no exposures and cannot have consumed backtest history. A
lens that does model the strategy must still carry a composition naming the
eligible version, which is what the gate is for. The blocked-strategy branch
below already tolerates an absent composition the same way.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
TARGET = REPO / 'tools' / 'portfolio_control_panel.py'

OLD = '''                        if strategy_name in eligible_versions:
                            composition = _object(
                                series.get("series_composition"),
                                f"hybrid decomposition composition {strategy_name}",
                            )
'''
NEW = '''                        if strategy_name in eligible_versions:
                            # A lens the strategy is method-scoped out of still
                            # emits a row for it, with no model and no
                            # composition. There is no hybrid version to check
                            # because the lens decomposed nothing, and a row
                            # without a model cannot have consumed backtest
                            # history.
                            if series.get("model_id") is None and _text(
                                series.get("status"), "hybrid decomposition status"
                            ) == "UNAVAILABLE":
                                continue
                            composition = _object(
                                series.get("series_composition"),
                                f"hybrid decomposition composition {strategy_name}",
                            )
'''


def main():
    text = TARGET.read_text(encoding='utf-8')
    if 'method-scoped out of' in text:
        print('already applied')
        return 0
    if '\r\n' in text:
        raise SystemExit('refusing to edit a file that already carries CRLF')
    if text.count(OLD) != 1:
        raise SystemExit('anchor is not unique (%d)' % text.count(OLD))
    TARGET.write_bytes(text.replace(OLD, NEW, 1).encode('utf-8'))
    print('hybrid gate now skips a lens that models nothing for the strategy')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
