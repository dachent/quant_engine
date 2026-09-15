"""Bind the governed HWM and invested-capital results into the vertical stages.

`stage --role report|workbook|panel-candidate` reads them; the earlier roles do
not. The panel role reads them too and is staged by `qa_and_panel.sh`, which
already carries them.
"""
from pathlib import Path

BS = chr(92)
W = '$W'
L = '2efe39247ae7694f4e3e22089674a6305de005c289c06ca6bff00e0c3d36442a'
GOVERNED = [
    'governed=(',
    '  --hwm-governed-result "%s/hwm/as-held-composite-hwm.json"' % W,
    '  --hwm-governed-input-generation "%s/hwm/hwm-input-generation.json"' % W,
    '  --hwm-governed-calendar-authority "%s/hwm/calendar-authority.json"' % W,
    '  --hwm-governed-admission-receipt '
    '"%s/run2/stages/ledger/generations/%s/admit-or-declare-receipt.json"' % (W, L),
    '  --invested-capital-result "%s/capital/invested-capital.json"' % W,
    '  --invested-capital-input-generation '
    '"%s/capital/capital-input-generation.json"' % W,
    ')',
]

p = Path(r'C:/Dev/scratch/quant-engine/pl12/epoch/build_vertical.sh')
lines = p.read_text(encoding='utf-8').split('\n')
if any(line.startswith('governed=(') for line in lines):
    raise SystemExit('already patched')
start = [
    n for n, line in enumerate(lines)
    if line.startswith('for role in attribution analytics report workbook')
][0]
target = [
    n for n, line in enumerate(lines)
    if line.strip() == '--qa-dir "$W/qa2" --portfolio-root "$Q/dataPortfolio" ' + BS
][0]
lines[target] = (
    '    --qa-dir "$W/qa2" --portfolio-root "$Q/dataPortfolio" ' + BS
)
lines[target + 1:target + 1] = [
    '    "${governed[@]}" ' + BS,
]
lines[start:start] = GOVERNED + ['']
p.write_text('\n'.join(lines), encoding='utf-8')
print('\n'.join(lines[start:start + 20]))
