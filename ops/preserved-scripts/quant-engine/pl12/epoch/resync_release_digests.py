"""Move every digest that restates the contract onto the current one.

Four places name the contract by digest: the HWM policy's product authority,
the frozen contract and source-segment digests in the contract test, the
generated views, and the release spec's `financial_mvp` reference. They are
separate checks of the same fact, so they move together or the release stops at
whichever one is read first.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
CONTRACT = REPO / 'contracts/portfolio-financial-mvp-source-v1.json'
POLICY = REPO / 'contracts/portfolio-hwm-policy-v4.json'
TEST = REPO / 'tests/core/test_portfolio_financial_mvp_contract.py'


def semantic(document):
    return hashlib.sha256(
        json.dumps(
            document, ensure_ascii=True, separators=(',', ':'), sort_keys=True
        ).encode('utf-8')
        + b'\n'
    ).hexdigest()


def main():
    raw = CONTRACT.read_bytes()
    document = json.loads(raw)
    digests = {
        'raw': hashlib.sha256(raw).hexdigest(),
        'semantic': semantic(document),
        'segments': semantic({'source_segments': document['source_segments']}),
    }

    body = POLICY.read_bytes()
    newline = b'\r\n' if b'\r\n' in body else b'\n'
    policy = json.loads(body)
    authority = policy['product_authority']
    previous = (authority['raw_sha256'], authority['semantic_digest'])
    authority['raw_sha256'] = digests['raw']
    authority['semantic_digest'] = digests['semantic']
    POLICY.write_bytes(
        (json.dumps(policy, indent=1, ensure_ascii=False) + '\n')
        .encode('utf-8')
        .replace(b'\n', newline)
    )
    print('hwm policy', previous[1][:12], '->', digests['semantic'][:12])

    text = TEST.read_bytes()
    moved = 0
    for old, new in (
        (previous[1], digests['semantic']),
        (previous[0], digests['raw']),
    ):
        if old != new and old.encode() in text:
            text = text.replace(old.encode(), new.encode())
            moved += 1
    TEST.write_bytes(text)
    print('test digest references moved', moved)

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / 'tools/portfolio_financial_mvp_contract.py'),
            'generate',
            '--contract',
            str(CONTRACT),
            '--output-root',
            str(REPO / 'generated/financial-mvp'),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={**__import__('os').environ, 'PYTHONPATH': '.'},
    )
    if result.returncode != 0:
        raise SystemExit('views did not regenerate: ' + result.stderr.strip()[-400:])
    print(result.stdout.strip())
    print('source_segments digest', digests['segments'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
