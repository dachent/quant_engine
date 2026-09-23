from pathlib import Path

BS = chr(92)
L = '2efe39247ae7694f4e3e22089674a6305de005c289c06ca6bff00e0c3d36442a'
p = Path(r'C:/Dev/scratch/quant-engine/pl12/epoch/qa_and_panel.sh')
lines = p.read_text(encoding='utf-8').split('\n')
i = [
    n for n, line in enumerate(lines)
    if line.startswith('  --qa-dir "$QA" --portfolio-root')
][0]
lines[i] = '  --qa-dir "$QA" --portfolio-root "$Q/dataPortfolio" ' + BS
extra = [
    '  --hwm-governed-result "$W/hwm/as-held-composite-hwm.json" ' + BS,
    '  --hwm-governed-input-generation "$W/hwm/hwm-input-generation.json" ' + BS,
    '  --hwm-governed-calendar-authority "$W/hwm/calendar-authority.json" ' + BS,
    '  --hwm-governed-admission-receipt '
    '"$W/run2/stages/ledger/generations/' + L + '/admit-or-declare-receipt.json" ' + BS,
    '  --invested-capital-result "$W/capital/invested-capital.json" ' + BS,
    '  --invested-capital-input-generation '
    '"$W/capital/capital-input-generation.json" ' + BS,
    '  > "$CYCLE/stage-panel.json"',
]
lines[i + 1:i + 1] = extra
p.write_text('\n'.join(lines), encoding='utf-8')
print('\n'.join(lines[i - 2:i + 9]))
