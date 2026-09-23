"""Toggle the raw[0] misreading back on, to prove the new tests catch it."""
import sys
from pathlib import Path

HISTORY = Path('tools/portfolio_strategy_history.py')
TARGET = Path('tools/portfolio_target_series.py')

HISTORY_FIXED = '''            level = _decimal(raw[1], "Portfolio123 exact EU strategy level")
            total_value = _decimal(raw[2], "Portfolio123 exact EU total value")
            if level <= 0 or total_value < 0:
                raise StrategyHistoryError(
                    "Portfolio123 exact EU performance contains invalid values"
                )
            parsed.append((observed, level, level, total_value))
'''
HISTORY_BUGGY = '''            level = Decimal(1) + _decimal(
                raw[0], "Portfolio123 exact EU strategy level"
            ) / Decimal(100)
            total_value = _decimal(raw[2], "Portfolio123 exact EU total value")
            if level <= 0 or total_value < 0:
                raise StrategyHistoryError(
                    "Portfolio123 exact EU performance contains invalid values"
                )
            parsed.append((observed, level, _decimal(raw[1], "x"), total_value))
'''

TARGET_FIXED = '''        level = _positive_decimal(raw[1], "Portfolio123 strategy performance level")
        total_value = _decimal(raw[2], "Portfolio123 total value")
        if total_value < 0:
            raise TargetSeriesError("Portfolio123 total value is invalid")
        parsed.append((day, level, level, total_value))
'''
TARGET_BUGGY = '''        level = Decimal(1) + _decimal(raw[0], "Portfolio123 strategy performance") / Decimal(100)
        total_value = _decimal(raw[2], "Portfolio123 total value")
        if total_value < 0:
            raise TargetSeriesError("Portfolio123 total value is invalid")
        parsed.append((day, level, _positive_decimal(raw[1], "x"), total_value))
'''

pairs = ((HISTORY, HISTORY_FIXED, HISTORY_BUGGY), (TARGET, TARGET_FIXED, TARGET_BUGGY))
mode = sys.argv[1]
for path, fixed, buggy in pairs:
    text = path.read_text(encoding='utf-8')
    old, new = (fixed, buggy) if mode == 'break' else (buggy, fixed)
    assert text.count(old) == 1, (path.name, mode, text.count(old))
    path.write_bytes(text.replace(old, new).encode('utf-8'))
print(mode, 'applied')
