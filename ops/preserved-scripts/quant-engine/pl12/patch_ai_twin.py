"""Resolve the AI live/simulation twin in the model id space it lives in.

Portfolio123 keeps two identifier spaces.  `har_managed_performance` and
`har_managed_metadata` are keyed by the managed portfolio id, which is what the
provider-live return route needs: entity 1913284 is the AI Factor managed
portfolio, and compounding its levels reproduces Portfolio123's own published
statistics.  `strategy_relationships` and `returns_daily` are keyed by the
model id, which is what the live/simulation twin route needs: 1913284's
`assetUid` is 1853536, whose extended twin 1853549 carries the backtest.

One evidence field cannot serve both spaces, so the provider block may now name
the model id explicitly.
"""
from pathlib import Path

path = Path('tools/portfolio_target_series.py')
text = path.read_text(encoding='utf-8')

old = '''    provider_entity_id = str(provider.get("entity_id", "")).strip()
    if not provider_entity_id:
        raise TargetSeriesError("Portfolio123 AI provider identity is absent")
    twins: set[str] = set()
'''
new = '''    # Portfolio123 keeps two identifier spaces.  har_managed_performance is
    # keyed by the managed portfolio id, which the provider-live return route
    # needs; strategy_relationships and returns_daily are keyed by the model id
    # behind it, which the live/simulation twin route needs.  A managed
    # portfolio therefore names its model explicitly rather than being looked
    # up in a table it does not appear in.
    provider_entity_id = str(
        provider.get("model_entity_id") or provider.get("entity_id", "")
    ).strip()
    if not provider_entity_id:
        raise TargetSeriesError("Portfolio123 AI provider identity is absent")
    twins: set[str] = set()
'''
assert text.count(old) == 1
path.write_bytes(text.replace(old, new).encode('utf-8'))
print('twin resolution now accepts the model id')
