"""Derive the strategy-attribution spec for the baseline this cycle stages.

`portfolio_attribution.py` refuses a spec whose `as_of_date` differs from the
baseline's, and the retained spec is dated 2026-08-14. The published
generations show the same move already made once: `3364fbaf` is dated
2026-08-16 against the 2026-08-16 baseline while the retained spec still says
2026-08-14, so the spec's date tracks the baseline it is computed against, not
the last date in the attribution return capture.

Nothing else in the spec moves: the same return source, comparisons and
benchmark assignments, hash-pinned as they are.
"""
import json
import sys
from pathlib import Path


def main(spec_path: str, baseline_generation: str, out_path: str) -> int:
    source = Path(spec_path)
    raw = source.read_bytes()
    spec = json.loads(raw)
    baseline = json.loads(
        (Path(baseline_generation) / 'portfolio-baseline.json').read_text(
            encoding='utf-8'
        )
    )
    as_of = baseline['as_of_date']
    if spec['as_of_date'] == as_of:
        print('spec already matches the baseline:', as_of)
        payload = raw
    else:
        print('spec as_of', spec['as_of_date'], '->', as_of)
        spec['as_of_date'] = as_of
        payload = (
            json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
        ).encode('utf-8')
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)
    print('written:', out)
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
