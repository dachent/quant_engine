"""Make the Portfolio123 level transform observable, and decouple the fixtures.

Both remaining parsers survived reading `raw[0]` as a percentage because every
fixture set the marker to `level - 100`, which makes the two readings
algebraically identical, and because neither path ever published a value from
the transform - only counts.
"""
from pathlib import Path

# 1. Publish the transform's own result in the strategy-history summary.
history = Path('tools/portfolio_strategy_history.py')
text = history.read_text(encoding='utf-8')
old = '''        provider_summary = {
            "cash_added_semantics": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
            "equity_overlap_observations": overlap,
'''
new = '''        compounded = Decimal(1)
        for value in provider_returns:
            compounded *= Decimal(1) + value
        provider_summary = {
            # The transform previously published only counts, so nothing
            # observed whether the level or the leading marker produced the
            # series.  The compounded result is the transform's own answer.
            "cash_added_semantics": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
            "cumulative_return": format((compounded - Decimal(1)).normalize(), "f"),
            "equity_overlap_observations": overlap,
'''
assert text.count(old) == 1
history.write_bytes(text.replace(old, new).encode('utf-8'))
print('strategy history summary publishes cumulative_return')

# 2. Decouple the strategy-history fixture's marker from its level.
test_history = Path('tests/portfolio_analytics/test_portfolio_strategy_history.py')
text = test_history.read_text(encoding='utf-8')
old = '''                    ("data", json.dumps([[0, 100, 100], [1, 101, 101], [-1, 99, 99]])),'''
new = '''                    # Real Portfolio123 markers are small day codes unrelated
                    # to the level.  Setting the marker to ``level - 100``
                    # makes reading it as a percentage arithmetically identical
                    # to reading the level, which is how the marker survived as
                    # the emitted series.  These markers keep the two apart.
                    ("data", json.dumps([[0, 100, 100], [1, 101, 101], [3, 99, 99]])),'''
assert text.count(old) == 1
text = text.replace(old, new)

old_assert = '''    assert dispositions["EU [P123]"]["provider_live"] == {
        "cash_added_semantics": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
        "equity_overlap_observations": 3,
'''
new_assert = '''    assert dispositions["EU [P123]"]["provider_live"] == {
        "cash_added_semantics": "UNRESOLVED_NOT_USED_IN_RETURN_FORMULA",
        # Levels 100 -> 101 -> 99 compound to -1%.  Reading the leading markers
        # 0/1/3 as percentages instead compounds to +3%.
        "cumulative_return": "-0.01",
        "equity_overlap_observations": 3,
'''
assert text.count(old_assert) == 1
text = text.replace(old_assert, new_assert)
test_history.write_bytes(text.encode('utf-8'))
print('strategy history fixture and assertion updated')

# 3. Decouple the layered-history fixture's marker from its level.
test_layered = Path('tests/portfolio_analytics/test_portfolio_layered_history.py')
text = test_layered.read_text(encoding='utf-8')
old = '''    data = [
        [0.0, 100.0, 100.0],
        [1.0, 101.0, 101.0],
        [2.0, 102.0, 99.0],
        [3.0, 103.0, 100.0],
        [4.0, 104.0, 101.0],
        [5.0, 105.0, 102.0],
        [6.0, 106.0, 103.0],
    ]
'''
new = '''    # Real Portfolio123 markers are small day codes unrelated to the level.
    # Setting the marker to ``level - 100`` makes reading it as a percentage
    # arithmetically identical to reading the level, which is how the marker
    # survived as the emitted return series.  These markers keep the two apart.
    data = [
        [0.0, 100.0, 100.0],
        [1.0, 101.0, 101.0],
        [3.0, 102.0, 99.0],
        [1.0, 103.0, 100.0],
        [1.0, 104.0, 101.0],
        [3.0, 105.0, 102.0],
        [1.0, 106.0, 103.0],
    ]
'''
assert text.count(old) == 1
test_layered.write_bytes(text.replace(old, new).encode('utf-8'))
print('layered history fixture updated')
