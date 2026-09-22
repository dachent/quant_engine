"""Producer-side tests for the production artifact inventory.

The release consumers resolve the selected admission policy from this inventory, because they
cannot resolve it from the epoch-manifest generation: the generation stores a canonicalised
*projection* of the policy, and the captured policy is pretty-printed, which no generation
artifact may be. A consumer test that fabricates the row cannot tell whether the producer emits
it, so the producer is exercised here directly.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from quant_engine.core.contracts import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "mvp2-release" / "scripts" / "build_financial_artifacts.py"

# A pretty-printed policy, exactly like the captured pin. Canonicalising it changes the bytes,
# which is the whole reason the input cannot travel inside the generation.
POLICY = b'{\n  "policy_id": "epoch-admission",\n  "admissions": []\n}\n'


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "build_financial_artifacts", MODULE_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_root(tmp_path: Path, policy: bytes = POLICY) -> tuple[Path, Path, dict]:
    policy_path = tmp_path / "mapped-epoch-admission-current-v10-dated.json"
    policy_path.write_bytes(policy)
    pin = {
        "path": str(policy_path),
        "sha256": hashlib.sha256(policy).hexdigest(),
        "size_bytes": len(policy),
    }
    checkpoint = tmp_path / "run" / "preparation" / "checkpoints"
    checkpoint.mkdir(parents=True)
    (checkpoint / "epoch.json").write_bytes(
        json.dumps(
            {"input_identity": {"inputs": {"epoch_admission_policy": pin}}}
        ).encode()
    )
    return tmp_path / "run", policy_path, pin


def test_importing_the_inventory_producer_does_not_parse_argv():
    """It has to be importable, or the producer side of this seam cannot be tested at all.

    The roots used to be resolved at module scope, so importing the module parsed `sys.argv`
    and exited. That is exactly why the inventory had no producer test.
    """

    module = _load_module()
    assert hasattr(module, "admission_policy_row")
    assert hasattr(module, "main")


def test_admission_policy_row_names_the_pin_preparation_verified(tmp_path):
    """The emitted node carries the selected input's own bytes, non-canonical and all."""

    module = _load_module()
    run, policy_path, pin = _run_root(tmp_path)

    # The property that makes the generation unable to carry this input.
    assert canonical_json_bytes(json.loads(POLICY)) != POLICY

    row = module.admission_policy_row(run)
    assert row["node_id"] == "financial-account-manifest-admission-policy"
    assert row["kind"] == "ARTIFACT"
    assert Path(row["path"]) == policy_path
    assert row["sha256"] == pin["sha256"]
    assert row["size_bytes"] == pin["size_bytes"]
    assert row["depends_on"] == []


def test_admission_policy_row_refuses_a_file_that_drifted_from_the_pin(tmp_path):
    """A row that silently disagreed with the pin would bind the wrong policy."""

    module = _load_module()
    run, policy_path, _pin = _run_root(tmp_path)
    policy_path.write_bytes(POLICY + b"\n")

    with pytest.raises(SystemExit, match="differs from the pin"):
        module.admission_policy_row(run)
