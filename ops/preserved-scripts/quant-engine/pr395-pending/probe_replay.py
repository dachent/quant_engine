"""Exercise verify_selected_hwm_generation against the real r40 HWM artifacts."""
import sys, traceback
from pathlib import Path

QE = Path(r"C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine")
T = QE / ".codex-bootstrap/mvp2-execution"
REPO = T / "repo"
sys.path[:0] = [str(REPO / "tools")] + [str(p) for p in sorted((REPO / "packages").glob("*/src"))]

MV = QE / ".mvp2-t"
H = MV / "hwm30/out"
LG = MV / "pl11/generations/fba9c9412ec5b7520794f7babcfda5266d62a77ce5624df58f85776eb6635056"

SOURCE_PATHS = {
    "account_manifest_generation_path": MV / "ep12/account-manifest/generations/f93beeffe7611d51d41d700c8c8451e139fb91b490de12e0d82456f3d33d21b0/generation.json",
    "account_manifest_admission_policy_path": MV / "ep12/roles/account-strategy-admission-policy.json",
    "account_manifest_flow_classification_policy_path": REPO / "contracts/account-strategy-flow-classification-policy-v1.json",
    "provider_ledger_generation_path": LG / "generation.json",
    "baseline_generation_path": MV / "bl11/generations/7238806c391120ff8e59657ec666a66e32544839a314b16d1530191d78747640/generation.json",
}
BINDINGS = {
    "governed_result": H / "governed-hwm-result.json",
    "input_generation": H / "hwm-input-generation.json",
    "calendar_authority": H / "governed-calendar-authority.json",
    "provider_admission_receipt": LG / "admit-or-declare-receipt.json",
}
PRODUCT = REPO / "contracts/portfolio-financial-mvp-source-v1.json"
RETAINED = T / "fx-contract-simulation/portfolio-financial-mvp-source-v4.json"

for p in [*SOURCE_PATHS.values(), *BINDINGS.values(), PRODUCT, RETAINED]:
    if not p.is_file():
        raise SystemExit(f"missing input: {p}")

from portfolio_financial_outcomes import verify_selected_hwm_generation
import inspect
signature = inspect.signature(verify_selected_hwm_generation)
print("signature:", signature)

def attempt(label, **extra):
    try:
        resolved = verify_selected_hwm_generation(
            SOURCE_PATHS, contract_path=PRODUCT, bindings=BINDINGS, **extra
        )
        print(f"{label}: PASS -> {sorted(resolved)}")
    except Exception as exc:
        print(f"{label}: FAIL -> {type(exc).__name__}: {exc}")

attempt("product contract, repo policy (today)")
if "input_contract_path" in signature.parameters:
    from portfolio_invested_capital import governed_hwm_input_policy
    from portfolio_pipeline_materialize import HWM_GOVERNED_POLICY
    import json
    expected = json.loads(BINDINGS["governed_result"].read_bytes())["policy_sha256"]
    policy_bytes = governed_hwm_input_policy(
        HWM_GOVERNED_POLICY.read_bytes(), RETAINED.read_bytes(), expected
    )
    attempt(
        "retained contract, retargeted policy (fixed)",
        input_contract_path=RETAINED,
        policy_bytes=policy_bytes,
    )
