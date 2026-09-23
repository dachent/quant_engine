"""Read the Portfolio123 strategy level in the two remaining parsers.

`provider_ledger` was corrected to read `raw[1]`; the target-series and
strategy-history paths still read `raw[0]` as a percentage, so the same
hash-bound artifact produced two disagreeing return series.
"""
from pathlib import Path

TUPLE_NOTE = """        # Each observation is [marker, Strategy, Total Value]: ``panels`` names
        # the two graphs, Strategy at idx 0 and Total Value at idx 1, and the
        # leading element is a marker outside that indexing.  Reading the
        # marker as a percentage collapsed every return onto the three values
        # the marker takes.  The level is already a wealth index, so period
        # returns are its ratios and its base cancels.
"""

target = Path('tools/portfolio_target_series.py')
text = target.read_text(encoding='utf-8')
old = '''        strategy_level = _decimal(raw[0], "Portfolio123 strategy performance")
        total_equity = _positive_decimal(raw[1], "Portfolio123 total value")
        cash_added = _decimal(raw[2], "Portfolio123 cash added")
        wealth = Decimal(1) + strategy_level / Decimal(100)
        if wealth <= 0:
            raise TargetSeriesError("Portfolio123 strategy performance is invalid")
        parsed.append((day, wealth, total_equity, cash_added))
'''
new = TUPLE_NOTE + '''        level = _positive_decimal(
            raw[1], "Portfolio123 strategy performance level"
        )
        total_value = _decimal(raw[2], "Portfolio123 total value")
        if total_value < 0:
            raise TargetSeriesError("Portfolio123 total value is invalid")
        parsed.append((day, level, level, total_value))
'''
assert text.count(old) == 1
text = text.replace(old, new)

old_overlap = '''    for day, _wealth, total_equity, cash_added in parsed:
        if day not in equity_by_day:
            continue
        overlap += 1
        if equity_by_day[day] != (total_equity, cash_added):
            raise TargetSeriesError("Portfolio123 performance and equity paths differ")
'''
new_overlap = '''    for day, _level, curve_level, curve_total in parsed:
        if day not in equity_by_day:
            continue
        overlap += 1
        if equity_by_day[day] != (curve_level, curve_total):
            raise TargetSeriesError("Portfolio123 performance and equity paths differ")
'''
assert text.count(old_overlap) == 1
text = text.replace(old_overlap, new_overlap)
target.write_bytes(text.encode('utf-8'))
print('portfolio_target_series.py patched')

history = Path('tools/portfolio_strategy_history.py')
text = history.read_text(encoding='utf-8')
old = '''            wealth = Decimal(1) + _decimal(
                raw[0], "Portfolio123 exact EU strategy performance"
            ) / Decimal(100)
            total_equity = _decimal(raw[1], "Portfolio123 exact EU total value")
            cash_added = _decimal(raw[2], "Portfolio123 exact EU cash added")
            if wealth <= 0 or total_equity <= 0:
                raise StrategyHistoryError(
                    "Portfolio123 exact EU performance contains invalid values"
                )
            parsed.append((observed, wealth, total_equity, cash_added))
'''
new = '\n'.join('    ' + line if line else line for line in TUPLE_NOTE.splitlines()) + '''
            level = _decimal(raw[1], "Portfolio123 exact EU strategy level")
            total_value = _decimal(raw[2], "Portfolio123 exact EU total value")
            if level <= 0 or total_value < 0:
                raise StrategyHistoryError(
                    "Portfolio123 exact EU performance contains invalid values"
                )
            parsed.append((observed, level, level, total_value))
'''
assert text.count(old) == 1
text = text.replace(old, new)

old_overlap = '''        for observed, _wealth, total_equity, cash_added in parsed:
            if observed not in observations:
                continue
            overlap += 1
            if observations[observed] != (total_equity, cash_added):
                raise StrategyHistoryError(
                    "Portfolio123 exact EU performance and equity differ"
                )
'''
new_overlap = '''        for observed, _level, curve_level, curve_total in parsed:
            if observed not in observations:
                continue
            overlap += 1
            if observations[observed] != (curve_level, curve_total):
                raise StrategyHistoryError(
                    "Portfolio123 exact EU performance and equity differ"
                )
'''
assert text.count(old_overlap) == 1
text = text.replace(old_overlap, new_overlap)
history.write_bytes(text.encode('utf-8'))
print('portfolio_strategy_history.py patched')
