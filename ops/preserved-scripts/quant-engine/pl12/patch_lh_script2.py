"""Point the accepted provider-live tables at the frozen history generation.

The builder resolves the accepted `p123-eu-provider-live` receipt to
`generations/<accepted id>/generation.json` and verifies the metadata,
performance and equity files against that manifest.  Those three come from the
frozen history generation; returns_daily and strategy_relationships come from
whatever generation is current.
"""
from pathlib import Path

path = Path(r'C:/Dev/scratch/quant-engine/pl12/build_layered_history.sh')
text = path.read_text(encoding='utf-8')

old_vars = 'P123_TABLES="$P123/generations/$P123_GEN/tables"\n'
new_vars = (
    'P123_TABLES="$P123/generations/$P123_GEN/tables"\n'
    '# The accepted p123-eu-provider-live receipt is frozen on the 2026-08-14\n'
    '# generation, so its three managed tables are read from there.\n'
    'P123_HISTORY_GEN="10215d7b46c89511f6e61f2fa0cac075dd06d2b0cc0a68bade384ad4a3182af1"\n'
    'P123_HISTORY_TABLES="$P123/generations/$P123_HISTORY_GEN/tables"\n'
)
assert text.count(old_vars) == 1
text = text.replace(old_vars, new_vars)

for name in ('har_managed_metadata', 'har_managed_performance', 'har_equity_curve'):
    old = f'"$P123_TABLES/{name}.parquet"'
    new = f'"$P123_HISTORY_TABLES/{name}.parquet"'
    assert text.count(old) == 1, (name, text.count(old))
    text = text.replace(old, new)

path.write_text(text, encoding='utf-8')
print('accepted history tables repointed')
