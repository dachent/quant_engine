"""Resync every digest the contract's bytes feed, after a contract edit.

The contract is the sole handwritten authority; four places restate a digest of
it and must move together: the HWM policy's product authority, the identity
proof, the source-segment proof, and the generated views.
"""
import hashlib
import json
import re
import sys
from pathlib import Path

CONTRACT = Path('contracts/portfolio-financial-mvp-source-v1.json')
HWM = Path('contracts/portfolio-hwm-policy-v4.json')
TEST = Path('tests/core/test_portfolio_financial_mvp_contract.py')

raw_bytes = CONTRACT.read_bytes()
contract = json.loads(raw_bytes)


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
semantic = hashlib.sha256(canonical(contract)).hexdigest()
segments = hashlib.sha256(
    canonical({"source_segments": contract["source_segments"]})
).hexdigest()
print('raw     ', raw_sha256)
print('semantic', semantic)
print('segments', segments)

policy = json.loads(HWM.read_text(encoding='utf-8'))
old_raw = policy['product_authority']['raw_sha256']
old_semantic = policy['product_authority']['semantic_digest']
text = HWM.read_text(encoding='utf-8')
text = text.replace(old_raw, raw_sha256).replace(old_semantic, semantic)
HWM.write_bytes(text.encode('utf-8').replace(b'\r\n', b'\n'))

test_text = TEST.read_text(encoding='utf-8')
replacements = 0
for old, new in ((old_semantic, semantic),):
    replacements += test_text.count(old)
    test_text = test_text.replace(old, new)
# The source-segment proof appears twice: once for the legacy contract and once
# for the same contract with a synthetic epoch source appended.
old_segments = sys.argv[1] if len(sys.argv) > 1 else None
if old_segments is None:
    candidates = {
        match
        for match in re.findall(r'"([0-9a-f]{64})"', test_text)
        if test_text.count(match) == 2
    }
    if len(candidates) != 1:
        raise SystemExit(f'cannot identify the segment proof: {sorted(candidates)}')
    old_segments = candidates.pop()
replacements += test_text.count(old_segments)
test_text = test_text.replace(old_segments, segments)
TEST.write_bytes(test_text.encode('utf-8'))
print('test pins replaced:', replacements, '(segments was', old_segments[:12] + ')')
