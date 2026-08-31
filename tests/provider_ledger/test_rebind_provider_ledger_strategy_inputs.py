"""Behavioral tests for rebinding provider-ledger returns to layered history."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from quant_engine.core.contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "tools" / "rebind_provider_ledger_strategy_inputs.py"


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location(
        "rebind_provider_ledger_strategy_inputs", RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_raw_generation(tmp_path: Path) -> tuple[Path, Path, bytes]:
    """Construct a raw receipt independently of the layered-history producer."""

    generation = tmp_path / ("a" * 64)
    generation.mkdir()
    returns = (
        b"series_id,date,return,evidence_layer,strategy_version,segment_id,"
        b"source_id,source_generation_id,mode_admission,return_basis,return_type,"
        b"calendar,frequency\n"
        b"Example,2026-08-13,0.01,ACCOUNT_LIVE,v1,example:live,ledger,"
        + (b"b" * 64)
        + b",LIVE_ONLY,NET,TOTAL,WEEKDAY,DAILY\n"
    )
    returns_path = generation / "returns.csv"
    returns_path.write_bytes(returns)
    manifest = {
        "generation_id": generation.name,
        "outputs": {
            "returns.csv": {
                "artifact_id": "layered-history/returns.csv",
                "sha256": hashlib.sha256(returns).hexdigest(),
                "size_bytes": len(returns),
            }
        },
    }
    (generation / "generation.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return generation, returns_path, returns


def _template(contract_path: Path) -> dict[str, object]:
    contract_sha256 = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    return {
        "as_of_date": "2026-08-16",
        "candidate_output_root": "candidate/provider-ledger",
        "financial_mvp": {
            "contract_path": str(contract_path.resolve()),
            "contract_sha256": contract_sha256,
            "registry_path": "authority/registry.json",
            "registry_sha256": "2" * 64,
            "schema_path": "authority/schema.json",
            "schema_sha256": "3" * 64,
        },
        "provider": "synthetic",
        "provider_selection": {
            "mode": "IMMUTABLE_GENERATIONS",
            "providers": ["MATRIXPORT", "QUANTBASE"],
        },
        "schema_version": 2,
        "strategy_inputs": {
            "returns_path": "stale/returns.csv",
            "returns_sha256": "0" * 64,
            "strategy_evidence_path": "unchanged/evidence.json",
            "strategy_evidence_sha256": "1" * 64,
        },
    }


def _accepted_verifier(generation: Path, contract: Path) -> dict[str, object]:
    """Minimal verifier stand-in: hash binding remains exercised by this tool."""

    assert generation.is_dir()
    assert contract.is_file()
    return {"generation_id": generation.name}


def test_rebinds_only_declared_returns_identity_from_a_raw_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if stale fields remain or any non-returns template value changes."""

    runner = _load_runner()
    generation, returns_path, returns = _write_raw_generation(tmp_path)
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    template = _template(contract)
    template_path = tmp_path / "provider-ledger-spec.json"
    template_path.write_text(json.dumps(template), encoding="utf-8")
    output = tmp_path / "provider-ledger-spec-rebound.json"
    monkeypatch.setattr(runner, "_verify_layered_history_generation", _accepted_verifier)

    result = runner.rebind_provider_ledger_strategy_inputs(
        template_path=template_path,
        layered_generation_dir=generation,
        financial_mvp_contract_path=contract,
        output_path=output,
    )

    rebound = json.loads(output.read_text(encoding="utf-8"))
    assert result == {
        "layered_history_generation_id": generation.name,
        "output_path": str(output),
        "returns_sha256": hashlib.sha256(returns).hexdigest(),
        "returns_size_bytes": len(returns),
    }
    expected = json.loads(json.dumps(template))
    expected["strategy_inputs"] = {
        "returns_path": str(returns_path.resolve()),
        "returns_sha256": hashlib.sha256(returns).hexdigest(),
        "strategy_evidence_path": "unchanged/evidence.json",
        "strategy_evidence_sha256": "1" * 64,
    }
    assert rebound == expected
    assert output.read_bytes() == canonical_json_bytes(rebound)


def test_rejects_a_mutated_declared_returns_artifact_after_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if the rebinder trusted a verifier result but not declared bytes."""

    runner = _load_runner()
    generation, returns_path, _returns = _write_raw_generation(tmp_path)
    returns_path.write_bytes(returns_path.read_bytes() + b"tampered\n")
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    template_path = tmp_path / "provider-ledger-spec.json"
    template_path.write_text(json.dumps(_template(contract)), encoding="utf-8")
    monkeypatch.setattr(runner, "_verify_layered_history_generation", _accepted_verifier)

    with pytest.raises(runner.ProviderLedgerStrategyInputsRebindError, match="identity differs"):
        runner.rebind_provider_ledger_strategy_inputs(
            template_path=template_path,
            layered_generation_dir=generation,
            financial_mvp_contract_path=contract,
            output_path=tmp_path / "provider-ledger-spec-rebound.json",
        )


def test_rejects_an_output_path_that_aliases_the_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if the tool could overwrite the policy template it is rebinding."""

    runner = _load_runner()
    generation, _returns_path, _returns = _write_raw_generation(tmp_path)
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    template_path = tmp_path / "provider-ledger-spec.json"
    template_path.write_text(json.dumps(_template(contract)), encoding="utf-8")
    monkeypatch.setattr(runner, "_verify_layered_history_generation", _accepted_verifier)

    with pytest.raises(runner.ProviderLedgerStrategyInputsRebindError, match="aliases an input"):
        runner.rebind_provider_ledger_strategy_inputs(
            template_path=template_path,
            layered_generation_dir=generation,
            financial_mvp_contract_path=contract,
            output_path=template_path,
        )


def test_rejects_an_output_path_that_aliases_declared_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if rebinding could overwrite the verified source artifact."""

    runner = _load_runner()
    generation, returns_path, _returns = _write_raw_generation(tmp_path)
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    template_path = tmp_path / "provider-ledger-spec.json"
    template_path.write_text(json.dumps(_template(contract)), encoding="utf-8")
    monkeypatch.setattr(runner, "_verify_layered_history_generation", _accepted_verifier)

    with pytest.raises(runner.ProviderLedgerStrategyInputsRebindError, match="aliases an input"):
        runner.rebind_provider_ledger_strategy_inputs(
            template_path=template_path,
            layered_generation_dir=generation,
            financial_mvp_contract_path=contract,
            output_path=returns_path,
        )


def test_rejects_an_output_path_inside_the_source_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if a candidate could add unverified files to its source generation."""

    runner = _load_runner()
    generation, _returns_path, _returns = _write_raw_generation(tmp_path)
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    template_path = tmp_path / "provider-ledger-spec.json"
    template_path.write_text(json.dumps(_template(contract)), encoding="utf-8")
    monkeypatch.setattr(runner, "_verify_layered_history_generation", _accepted_verifier)

    with pytest.raises(runner.ProviderLedgerStrategyInputsRebindError, match="aliases an input"):
        runner.rebind_provider_ledger_strategy_inputs(
            template_path=template_path,
            layered_generation_dir=generation,
            financial_mvp_contract_path=contract,
            output_path=generation / "candidate.json",
        )


def test_rejects_a_different_contract_than_the_template_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would fail if a valid layered history could be rebound to another contract."""

    runner = _load_runner()
    generation, _returns_path, _returns = _write_raw_generation(tmp_path)
    template_contract = tmp_path / "template-contract.json"
    template_contract.write_text('{"product":"template"}', encoding="utf-8")
    other_contract = tmp_path / "other-contract.json"
    other_contract.write_text('{"product":"template"}', encoding="utf-8")
    template_path = tmp_path / "provider-ledger-spec.json"
    template_path.write_text(
        json.dumps(_template(template_contract)), encoding="utf-8"
    )
    monkeypatch.setattr(runner, "_verify_layered_history_generation", _accepted_verifier)

    with pytest.raises(
        runner.ProviderLedgerStrategyInputsRebindError,
        match="financial MVP contract binding differs",
    ):
        runner.rebind_provider_ledger_strategy_inputs(
            template_path=template_path,
            layered_generation_dir=generation,
            financial_mvp_contract_path=other_contract,
            output_path=tmp_path / "provider-ledger-spec-rebound.json",
        )
