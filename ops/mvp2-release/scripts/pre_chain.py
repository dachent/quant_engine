"""Pre-pass: layered history -> dependence -> strategy attribution against the staged baseline.

Reproduces exactly what the attended preparation does for its history/dependence stages
(same functions, same arguments) so the retained attribution binds the same authorities.
usage (cwd = repo): python pre_chain.py <cycle> <selected-inputs> <baseline-gid> <evaluated-on> <release-commit>
"""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path[:0] = ["tools"]
cyc = Path(sys.argv[1])
selected_inputs = Path(sys.argv[2])
baseline_gid = sys.argv[3]
evaluated_on = sys.argv[4]
commit = sys.argv[5]

import portfolio_attended_preparation as prep  # noqa: E402
import portfolio_current_dependence as dependence  # noqa: E402

repo = prep.ROOT
contract = repo / "contracts" / "portfolio-financial-mvp-source-v1.json"
selection = json.loads(selected_inputs.read_text(encoding="utf-8"))
files = selection["files"]
authorities = prep._selected_history_authorities(files)  # noqa: SLF001

work = cyc / "pre"
history_root_out = work / "history"
if not history_root_out.exists():
    history_source = prep._rebuild_layered_history(  # noqa: SLF001
        selection, history_root_out, commit, contract, authorities
    )
else:
    history_source = next((history_root_out).iterdir())
    print("reusing history", history_source)
print("HISTORY", history_source)

baseline_source = cyc / "baseline" / "generations" / baseline_gid
ledger_root = Path(selection["builder_kwargs"]["provider_ledger_manifest_path"]).parent
selected = {
    "schema_version": 1,
    "history_generation_root": str(history_source.resolve()),
    "allocation_generation_root": str(baseline_source.resolve()),
    "provider_ledger_generation_root": str(ledger_root.resolve()),
    "financial_input_contract_path": str(contract.resolve()),
    "provider_admission_receipt_path": str(
        authorities["provider_admission_receipt_path"].resolve()
    ),
    "portfolio123_version_evidence_path": str(
        authorities["portfolio123_version_evidence_path"].resolve()
    ),
}
selected_path = work / "dependence-selected-inputs.json"
selected_path.parent.mkdir(parents=True, exist_ok=True)
selected_path.write_text(
    json.dumps(selected, indent=1, sort_keys=True), encoding="utf-8", newline="\n"
)
result = dependence.write_source(
    work / "dependence",
    contract_path=contract,
    policy_path=repo / "contracts" / "portfolio-dependence-policy-v1.json",
    history_generation_root=history_source,
    allocation_generation_root=baseline_source,
    evaluated_on=date.fromisoformat(evaluated_on),
    selected_inputs_path=selected_path,
)
dependence_root = Path(str(result["destination"]))
dependence.verify_packaged_source(
    dependence_root,
    contract_path=contract,
    policy_path=repo / "contracts" / "portfolio-dependence-policy-v1.json",
    selected_inputs_path=selected_path,
)
print("DEPENDENCE", dependence_root)
done = subprocess.run(
    [
        sys.executable,
        "tools/portfolio_attribution.py",
        "stage",
        "--baseline-root",
        str(cyc / "baseline"),
        "--baseline-generation-id",
        baseline_gid,
        "--dependence-source-root",
        str(dependence_root),
        "--dependence-selected-inputs",
        str(selected_path),
        "--spec",
        str(cyc / "attribution-spec.json"),
        "--output-root",
        str(cyc / "attribution"),
    ],
    capture_output=True,
    text=True,
)
print(done.returncode, done.stdout[-600:], done.stderr[-800:])
raise SystemExit(done.returncode)
