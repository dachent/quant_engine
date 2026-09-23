import hashlib, sys
from pathlib import Path
REPO = Path(r"C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/.codex-bootstrap/mvp2-execution/repo")
sys.path[:0] = [str(REPO / "tools")] + [str(p) for p in sorted((REPO / "packages").glob("*/src"))]
from portfolio_invested_capital import governed_hwm_input_policy
raw = (REPO / "contracts" / "portfolio-hwm-policy-v4.json").read_bytes()
retained = Path(REPO.parent / "fx-contract-simulation" / "portfolio-financial-mvp-source-v4.json").read_bytes()
EXPECT = "927a53e4e4a64d7e778363f027302685f0b81a8c1de1298a2c83d895c67e3653"
out = governed_hwm_input_policy(raw, retained, EXPECT)
got = hashlib.sha256(out).hexdigest()
print("repo policy sha :", hashlib.sha256(raw).hexdigest())
print("retargeted sha  :", got)
print("chain expects   :", EXPECT)
print("MATCH           :", got == EXPECT)
