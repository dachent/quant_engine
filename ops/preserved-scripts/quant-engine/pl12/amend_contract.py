"""Repin the Portfolio123 current generation in the product contract as text.

Text-level edit: a json round-trip reformats the whole 4,934-line file.
"""
from pathlib import Path

OLD_GEN = '058354e3c3b4e6b20692616f962af55733e3ab8be3fbc5f81e24af6dd0ec6d45'
NEW_GEN = '10215d7b46c89511f6e61f2fa0cac075dd06d2b0cc0a68bade384ad4a3182af1'
OLD_MANIFEST = 'b1e6ed8e6b447fff4116900b73e712496d7a6edf00fa729f5f81f896471b7a53'
NEW_MANIFEST = 'e8870a4ff2a375d2c514975ac933c858ab7afe580eb9736fad04c1a2cf6f0907'
OLD_POINTER = '92f1f7af811227a93988875016d73eb7d07cafcce5b17df7c923bce71039e8a1'
NEW_POINTER = 'd0d3ebc232d66518654a63dce3ca343d33b9600b539a82ae4c4fd8ea69649ba1'
OLD_AS_OF = '"as_of": "2026-09-02"'
NEW_AS_OF = '"as_of": "2026-08-14"'

path = Path('contracts/portfolio-financial-mvp-source-v1.json')
lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
counts = {'gen': 0, 'manifest': 0, 'pointer': 0, 'as_of': 0}
# The as_of that belongs to a repinned generation sits in the same JSON object,
# at most two lines below its manifest hash (a pointer_sha256 line may sit
# between them in the census lane).
window = 0
for index, line in enumerate(lines):
    updated = line
    if OLD_GEN in updated:
        updated = updated.replace(OLD_GEN, NEW_GEN)
        counts['gen'] += 1
    if OLD_POINTER in updated:
        updated = updated.replace(OLD_POINTER, NEW_POINTER)
        counts['pointer'] += 1
    if OLD_MANIFEST in updated:
        updated = updated.replace(OLD_MANIFEST, NEW_MANIFEST)
        counts['manifest'] += 1
        window = 2
    elif window > 0:
        window -= 1
        if OLD_AS_OF in updated:
            updated = updated.replace(OLD_AS_OF, NEW_AS_OF)
            counts['as_of'] += 1
            window = 0
    lines[index] = updated

assert counts == {'gen': 12, 'manifest': 5, 'pointer': 2, 'as_of': 3}, counts
path.write_text(''.join(lines), encoding='utf-8')
print(counts)
