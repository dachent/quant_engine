"""Repin the Portfolio123 current generation onto the union generation.

Text-level, byte-exact substitutions: a json round-trip would reformat the
whole 4,934-line contract. The three retired hashes are unique to the retired
generation, and the hashes wrapped across source lines are matched separately.
"""
from pathlib import Path

SUBSTITUTIONS = {
    # generation id
    '058354e3c3b4e6b20692616f962af55733e3ab8be3fbc5f81e24af6dd0ec6d45':
        '80ad1dcab09a909032a4f174666ac130359c1585b67dec9a5ebe2182aac5eef8',
    # generation.json manifest hash
    'b1e6ed8e6b447fff4116900b73e712496d7a6edf00fa729f5f81f896471b7a53':
        '8da15634a52fbd51a95a9d610c15eb558e5d1abf4e85631c522fc98d0f9a5ffb',
    # CURRENT.json pointer hash
    '92f1f7af811227a93988875016d73eb7d07cafcce5b17df7c923bce71039e8a1':
        '79024a424f05eae38fcb57c0db4853359e2beaf9103a3c385436433c54422431',
    # the generation id as wrapped across two source lines in the rebind table
    '"058354e3c3b4e6b20692616f962af55733e3ab8be3fbc5f8"\n                "1e24af6dd0ec6d45':
        '"80ad1dcab09a909032a4f174666ac130359c1585b67dec9a5"\n                "ebe2182aac5eef8',
}

TARGETS = (
    'contracts/portfolio-financial-mvp-source-v1.json',
    'tools/portfolio_financial_mvp_contract.py',
    'tests/core/test_portfolio_financial_mvp_contract.py',
    'tests/provider_ledger/test_provider_ledger.py',
)

for name in TARGETS:
    path = Path(name)
    text = path.read_text(encoding='utf-8')
    counts = {}
    for old, new in SUBSTITUTIONS.items():
        counts[old[:12]] = text.count(old)
        text = text.replace(old, new)
    path.write_bytes(text.encode('utf-8').replace(b'\r\n', b'\n'))
    print(name, counts)
