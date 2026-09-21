from pathlib import Path

path = Path(r'C:/Dev/scratch/quant-engine/pl12/build_layered_history.sh')
text = path.read_text(encoding='utf-8')
old = '  --output-root "$OUT"\n'
new = (
    '  --portfolio-fx-rates-manifest '
    '"$Q/dataPortfolio/portfolio-fx-rates/generations/'
    '9812f2536a16da1b1e64f9030cca1bef22519333a4a8e19f3cb11ab32d39bc6e/generation.json" \\\n'
    '  --output-root "$OUT"\n'
)
assert text.count(old) == 1, text.count(old)
path.write_text(text.replace(old, new), encoding='utf-8')
print('fx manifest arg added')
