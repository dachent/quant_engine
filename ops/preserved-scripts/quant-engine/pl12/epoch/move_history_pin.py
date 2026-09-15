"""Move the #319 history authority pin onto HEAD's contract.

The pin names the commit whose contract is the accepted authority, plus that
document's raw and semantic digests. Re-accepting is a two-step move by
construction -- the commit has to exist before it can be named -- so this reads
HEAD, computes both digests from the committed bytes, and rewrites the three
constants together. Rewriting any one of them alone would leave the authority
naming a document it cannot verify.
"""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
sys.path[:0] = [str(REPO / 'tools')]
CONTRACT_PATH = 'contracts/portfolio-financial-mvp-source-v1.json'
TOOL = REPO / 'tools/portfolio_financial_mvp_contract.py'


def main():
    from portfolio_financial_mvp_contract import contract_digest

    sha = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()
    raw = subprocess.run(
        ['git', 'show', '%s:%s' % (sha, CONTRACT_PATH)], cwd=REPO, capture_output=True
    ).stdout
    if not raw:
        raise SystemExit('contract is not committed at HEAD')

    # The pin is satisfied by any commit carrying these exact contract bytes,
    # so a pin that already names such a commit is left where it is. Moving it
    # to HEAD regardless would make every pass dirty the tool that holds it.
    text = TOOL.read_text(encoding='utf-8')
    current = re.search(
        r'_HISTORY_AUTHORITY_COMMIT: Final = "([0-9a-f]+)"', text
    )
    if current:
        pinned = subprocess.run(
            ['git', 'show', '%s:%s' % (current.group(1), CONTRACT_PATH)],
            cwd=REPO,
            capture_output=True,
        )
        if pinned.returncode == 0 and pinned.stdout == raw:
            print('authority pin already names an identical contract; unchanged')
            return 0
    values = {
        '_HISTORY_AUTHORITY_COMMIT': sha,
        '_HISTORY_AUTHORITY_RAW_SHA256': hashlib.sha256(raw).hexdigest(),
        '_HISTORY_AUTHORITY_SEMANTIC_SHA256': contract_digest(json.loads(raw)),
    }
    text = TOOL.read_bytes().decode('utf-8')
    for name, value in values.items():
        pattern = re.compile(
            r'(%s: Final = \(?\s*")[0-9a-f]+(")' % re.escape(name)
        )
        text, count = pattern.subn(lambda m: m.group(1) + value + m.group(2), text)
        if count != 1:
            raise SystemExit('pin constant is not uniquely present: %s' % name)
        print(name, value)
    TOOL.write_bytes(text.encode('utf-8'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
