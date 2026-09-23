"""Build the schema-1 selected HWM input generation from this cycle's artifacts.

Every path here is an artifact this cycle actually produced. Nothing synthetic.
"""
import json
import sys
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
sys.path[:0] = [str(REPO / 'tools')] + [str(p) for p in REPO.glob('packages/*/src')]

Q = Path(r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine')
LIVE = Q / 'dataPortfolio/rollforward/2026-09-10-mvp2-live-only'
RUN = Path(r'C:/Dev/scratch/quant-engine/pl12/run2')
OUT = Path(r'C:/Dev/scratch/quant-engine/pl12/hwm')

EPOCH_ID = '0c60863bd29dfa30325b5d2b517b49bf7725a0eefca668581773cd817be28203'
LEDGER_ID = '2efe39247ae7694f4e3e22089674a6305de005c289c06ca6bff00e0c3d36442a'
BASELINE_ID = '72afa574dabdeeaa83dbc519310727a671dfcca4f48265f8469ffffce3603f26'

from portfolio_hwm_selected_inputs import build_selected_hwm_inputs  # noqa: E402

OUT.mkdir(parents=True, exist_ok=True)
generation = build_selected_hwm_inputs(
    account_manifest_generation_path=(
        Q / 'dataEpochManifest' / EPOCH_ID / 'generation.json'
    ),
    account_manifest_admission_policy_path=(
        LIVE / 'epoch-inputs/mapped-epoch-admission.json'
    ),
    account_manifest_flow_classification_policy_path=(
        REPO / 'contracts/account-strategy-flow-classification-policy-v1.json'
    ),
    provider_ledger_generation_path=(
        RUN / 'stages/ledger/generations' / LEDGER_ID / 'generation.json'
    ),
    baseline_generation_path=(
        RUN / 'stages/baseline/generations' / BASELINE_ID / 'generation.json'
    ),
    financial_contract_path=REPO / 'contracts/portfolio-financial-mvp-source-v1.json',
    calendar_authority_path=OUT / 'calendar-authority.json',
    provider_admission_path=(
        RUN / 'stages/ledger/generations' / LEDGER_ID / 'admit-or-declare-receipt.json'
    ),
    hwm_policy_path=REPO / 'contracts/portfolio-hwm-policy-v4.json',
)
path = OUT / 'hwm-input-generation.json'
path.write_text(json.dumps(generation, indent=1, sort_keys=True), encoding='utf-8')
print('memberships', len(generation['memberships']))
print('component_periods', len(generation['component_periods']))
print('written', path)
