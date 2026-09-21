"""Pass the published Robuxio track record into the history bundle build.

The generation is read from its own published CURRENT pointer and the product
version from the contract, so neither is typed into the driver.
"""
from pathlib import Path

E = Path(r'C:/Dev/scratch/quant-engine/pl12/epoch')
path = E / 'build_strategy_history.sh'
raw = path.read_bytes()
nl = b'\r\n' if b'\r\n' in raw else b'\n'
BS = chr(92).encode()

old = b'  --portfolio123-generation-root "$P123_ROOT" ' + BS + nl
new = (
    old
    + b'  --robuxio-track-record-generation "$ROBUXIO_GEN" ' + BS + nl
    + b'  --robuxio-strategy-version "$ROBUXIO_VERSION" ' + BS + nl
)
assert raw.count(old) == 1, raw.count(old)
raw = raw.replace(old, new)

anchor = b'args=()' + nl
assert raw.count(anchor) == 1
resolve = (
    b'ROBUXIO_ROOT="$Q/dataProviderTrackRecord"' + nl
    + b'ROBUXIO_ID="$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1],encoding=' + chr(39).encode() + b'utf-8' + chr(39).encode() + b'))[' + chr(39).encode() + b'generation_id' + chr(39).encode() + b'])" "$ROBUXIO_ROOT/CURRENT.json")"' + nl
    + b'ROBUXIO_GEN="$ROBUXIO_ROOT/generations/$ROBUXIO_ID"' + nl
    + b'ROBUXIO_VERSION="$("$PY" -c "' + nl
    + b'import json' + nl
    + b"contract = json.load(open('contracts/portfolio-financial-mvp-source-v1.json', encoding='utf-8'))" + nl
    + b"row = next(r for r in contract['strategies'] if r['strategy_id'] == 'robuxio-crypto')" + nl
    + b"print(row['strategy_version']['version_ref'])" + nl
    + b'")"' + nl
    + nl
    + anchor
)
raw = raw.replace(anchor, resolve, 1)
path.write_bytes(raw)
print('ok')
