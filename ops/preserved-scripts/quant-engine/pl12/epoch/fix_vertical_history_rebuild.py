"""Give the vertical's history rebuild the same prehistory inputs as the standalone build.

The rebuild branch was written when the layered history was live-only, so it
passes no bundle and no backtests generation. Turning it on now would rebuild a
history with every admitted prehistory silently dropped -- and it has to be
turned on, because the generation records the tool digests of the commit it was
built at and the authority pin move changes one of them.
"""
from pathlib import Path

E = Path(r'C:/Dev/scratch/quant-engine/pl12/epoch')
path = E / 'build_vertical.sh'
raw = path.read_bytes()
BS = chr(92).encode()

old = b'--portfolio-fx-rates-manifest "$Q/dataPortfolio/portfolio-fx-rates/generations/9812f2536a16da1b1e64f9030cca1bef22519333a4a8e19f3cb11ab32d39bc6e/generation.json"   --output-root "$LH_ROOT" > "$CYCLE/layered-history.json"'
new = (
    b'--portfolio-fx-rates-manifest "$Q/dataPortfolio/portfolio-fx-rates/generations/9812f2536a16da1b1e64f9030cca1bef22519333a4a8e19f3cb11ab32d39bc6e/generation.json"'
    b'   --strategy-history-manifest "$HISTORY_BUNDLE/manifest.json"'
    b'   --strategy-history-returns "$HISTORY_BUNDLE/returns.csv"'
    b'   --strategy-backtests-generation "$BACKTESTS_GEN/generation.json"'
    b'   --output-root "$LH_ROOT" > "$CYCLE/layered-history.json"'
)
assert raw.count(old) == 1, raw.count(old)
raw = raw.replace(old, new)

# Resolve both from their published pointers rather than naming either here.
anchor = b'cd "$REPO"'
resolve = (
    b'HISTORY_BUNDLE="${HISTORY_BUNDLE:-'
    b'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/dataPortfolio'
    b'/source-runs/strategy-history-mvp2-20260911'
    b'/ff99e1b3095057fd2438f6b4085cdd803bd25c88d3f8955776581c7a8e9a551b}"' + b'\n'
    + b'BACKTESTS_ROOT="$Q/dataStrategyBacktests"' + b'\n'
    + b'BACKTESTS_ID="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding=' + chr(39).encode() + b'utf-8' + chr(39).encode() + b'))[' + chr(39).encode() + b'generation_id' + chr(39).encode() + b'])" "$BACKTESTS_ROOT/CURRENT.json")"' + b'\n'
    + b'BACKTESTS_GEN="$BACKTESTS_ROOT/generations/$BACKTESTS_ID"' + b'\n'
    + b'\n'
    + anchor
)
assert raw.count(anchor) == 1, raw.count(anchor)
raw = raw.replace(anchor, resolve, 1)
path.write_bytes(raw)
print('ok')
