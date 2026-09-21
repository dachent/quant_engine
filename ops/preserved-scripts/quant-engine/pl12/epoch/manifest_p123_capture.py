"""Discard the failed probes and manifest what the capture actually retained.

A capture directory is evidence only if every file in it is a response the
provider really returned. The empty bodies from the pre-header 400s, the script
bundles that came back on a network filter, and the duplicate written before
filenames were sanitized are not that, so they are removed rather than
manifested.

What remains is hashed into a capture manifest in the same shape the Robuxio
capture used, so the generation stager can verify it the same way.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

RAW = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio123/source-runs/capture-20260912-mvp2/raw'
)
ROOT = RAW.parent

DROP_EXACT = {
    'model_1764562_model.json',
    'model_1764562_perf-15y.json',
    'model_1764562_perf-50y.json',
    'r2g_1764562_dmtrue.json',
    'r2g_1764562_dmtrue.request.json',
    'p123_build29413_js_dist_angularjs_charts_js.json',
    'p123_build29413_js_dist_angularjs_highcharts_js.json',
}


def main():
    removed = []
    for path in sorted(RAW.iterdir()):
        if not path.is_file():
            continue
        if path.name in DROP_EXACT or path.stat().st_size == 0:
            path.unlink()
            removed.append(path.name)

    # The investment holdings responses came back byte-identical for every id,
    # which means the endpoint ignored the id and returned one account-wide
    # payload. Keep one copy under a name that says so.
    holdings = sorted(RAW.glob('p123_investment_*_holdings.json'))
    digests = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in holdings}
    if holdings and len(set(digests.values())) == 1:
        keeper = RAW / 'p123_invest_holdings_account_wide.json'
        holdings[0].replace(keeper)
        for path in holdings[1:]:
            path.unlink()
            removed.append(path.name)

    artifacts = []
    for path in sorted(RAW.iterdir()):
        if not path.is_file():
            continue
        content = path.read_bytes()
        artifacts.append({
            'relative_path': 'raw/' + path.name,
            'sha256': hashlib.sha256(content).hexdigest(),
            'observed_length': len(content),
        })

    manifest = {
        'capture': 'portfolio123-designer-model-and-strategy',
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'schema_version': 1,
        'source': 'https://www.portfolio123.com',
        'transport': 'CDP_AUTHENTICATED_BROWSER',
        'subjects': {
            'designer_model_port_uid': 1764562,
            'simulated_book_port_uid': 1853549,
            'live_book_port_uid': 1853536,
        },
        'artifacts': artifacts,
    }
    destination = ROOT / 'capture-manifest.json'
    destination.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding='utf-8')
    print('removed %d non-response files' % len(removed))
    for name in removed:
        print('   -', name)
    print('manifested %d artifacts -> %s' % (len(artifacts), destination.name))
    for row in artifacts:
        print('   %-56s %9d  %s' % (row['relative_path'], row['observed_length'], row['sha256'][:12]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
