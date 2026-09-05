"""Rebind a provider-ledger strategy-input spec to verified layered returns.

The provider-ledger template remains the authority for every field other than
the exact location and digest of its layered-history ``returns.csv`` input.
This tool never builds, publishes, or overwrites an artifact.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

from quant_engine.core.contracts import canonical_json_bytes


_RETURNS_ARTIFACT_ID = "layered-history/returns.csv"
_RETURNS_OUTPUT_NAME = "returns.csv"


class ProviderLedgerStrategyInputsRebindError(ValueError):
    """The supplied template cannot bind to the verified layered return file."""


def _object_from_path(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ProviderLedgerStrategyInputsRebindError(f"{label} is unreadable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderLedgerStrategyInputsRebindError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise ProviderLedgerStrategyInputsRebindError(f"{label} must be an object")
    return value


def _load_layered_verifier() -> Callable[[Path, Path], Mapping[str, object]]:
    source = Path(__file__).with_name("portfolio_target_series.py")
    module_name = "_portfolio_target_series_for_provider_ledger_rebind"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history verifier is unavailable"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, ValueError) as exc:
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history verifier is unavailable"
        ) from exc
    verifier = getattr(module, "verify_layered_history_generation", None)
    if not callable(verifier):
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history verifier is unavailable"
        )
    return verifier


def _verify_layered_history_generation(
    generation_dir: Path, financial_mvp_contract_path: Path
) -> Mapping[str, object]:
    verifier = _load_layered_verifier()
    try:
        result = verifier(generation_dir, financial_mvp_contract_path)
    except (OSError, ValueError) as exc:
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history verification failed"
        ) from exc
    if not isinstance(result, Mapping):
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history verification result is invalid"
        )
    generation_id = result.get("generation_id")
    if not isinstance(generation_id, str) or generation_id != generation_dir.name:
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history verification identity differs"
        )
    return result


def _declared_returns(
    generation_dir: Path, verification: Mapping[str, object]
) -> tuple[Path, str, int]:
    if generation_dir.is_symlink() or not generation_dir.is_dir():
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history generation is unreadable"
        )
    generation_id = verification.get("generation_id")
    if generation_id != generation_dir.name:
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history generation identity differs"
        )
    manifest = _object_from_path(
        generation_dir / "generation.json", "layered history generation manifest"
    )
    if manifest.get("generation_id") != generation_id:
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history generation manifest identity differs"
        )
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history returns declaration is invalid"
        )
    declared = outputs.get(_RETURNS_OUTPUT_NAME)
    if not isinstance(declared, dict):
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history returns declaration is invalid"
        )
    expected_hash = declared.get("sha256")
    expected_size = declared.get("size_bytes")
    if (
        declared.get("artifact_id") != _RETURNS_ARTIFACT_ID
        or not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_hash)
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
    ):
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history returns declaration is invalid"
        )
    returns_path = generation_dir / _RETURNS_OUTPUT_NAME
    if returns_path.is_symlink() or not returns_path.is_file():
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history returns artifact is unreadable"
        )
    try:
        generation_root = generation_dir.resolve(strict=True)
        resolved_returns = returns_path.resolve(strict=True)
        content = returns_path.read_bytes()
    except OSError as exc:
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history returns artifact is unreadable"
        ) from exc
    if resolved_returns.parent != generation_root:
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history returns artifact path differs"
        )
    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != expected_hash or len(content) != expected_size:
        raise ProviderLedgerStrategyInputsRebindError(
            "layered history returns artifact identity differs"
        )
    return resolved_returns, actual_hash, len(content)


def _require_financial_mvp_contract_binding(
    template: Mapping[str, Any], financial_mvp_contract_path: Path
) -> None:
    binding = template.get("financial_mvp")
    if not isinstance(binding, dict):
        raise ProviderLedgerStrategyInputsRebindError(
            "financial MVP contract binding differs"
        )
    declared_path = binding.get("contract_path")
    declared_sha256 = binding.get("contract_sha256")
    if (
        not isinstance(declared_path, str)
        or not declared_path
        or not isinstance(declared_sha256, str)
        or len(declared_sha256) != 64
        or any(character not in "0123456789abcdef" for character in declared_sha256)
        or financial_mvp_contract_path.is_symlink()
        or not financial_mvp_contract_path.is_file()
    ):
        raise ProviderLedgerStrategyInputsRebindError(
            "financial MVP contract binding differs"
        )
    try:
        declared_resolved = Path(declared_path).resolve(strict=True)
        passed_resolved = financial_mvp_contract_path.resolve(strict=True)
        actual_sha256 = hashlib.sha256(
            financial_mvp_contract_path.read_bytes()
        ).hexdigest()
    except OSError as exc:
        raise ProviderLedgerStrategyInputsRebindError(
            "financial MVP contract binding differs"
        ) from exc
    if declared_resolved != passed_resolved or actual_sha256 != declared_sha256:
        raise ProviderLedgerStrategyInputsRebindError(
            "financial MVP contract binding differs"
        )


def _candidate_payload(
    template: Mapping[str, Any], returns_path: Path, returns_sha256: str
) -> dict[str, Any]:
    strategy_inputs = template.get("strategy_inputs")
    if not isinstance(strategy_inputs, dict):
        raise ProviderLedgerStrategyInputsRebindError(
            "provider-ledger strategy_inputs is invalid"
        )
    returns_path_value = strategy_inputs.get("returns_path")
    returns_hash_value = strategy_inputs.get("returns_sha256")
    if not isinstance(returns_path_value, str) or not isinstance(
        returns_hash_value, str
    ):
        raise ProviderLedgerStrategyInputsRebindError(
            "provider-ledger strategy_inputs is invalid"
        )
    candidate = dict(template)
    rebound_inputs = dict(strategy_inputs)
    rebound_inputs["returns_path"] = str(returns_path)
    rebound_inputs["returns_sha256"] = returns_sha256
    candidate["strategy_inputs"] = rebound_inputs
    return candidate


def _reject_output_alias(output_path: Path, inputs: Sequence[Path]) -> None:
    try:
        resolved_output = output_path.resolve(strict=False)
    except OSError as exc:
        raise ProviderLedgerStrategyInputsRebindError("output path is invalid") from exc
    for input_path in inputs:
        try:
            resolved_input = input_path.resolve(strict=False)
            if resolved_output == resolved_input or (
                input_path.is_dir() and resolved_output.is_relative_to(resolved_input)
            ):
                raise ProviderLedgerStrategyInputsRebindError("output aliases an input")
        except OSError as exc:
            raise ProviderLedgerStrategyInputsRebindError(
                "input path is invalid"
            ) from exc


def _write_new(path: Path, payload: Mapping[str, Any]) -> None:
    content = canonical_json_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(content)
    except FileExistsError as exc:
        raise ProviderLedgerStrategyInputsRebindError(
            "provider-ledger candidate already exists"
        ) from exc


def rebind_provider_ledger_strategy_inputs(
    *,
    template_path: Path,
    layered_generation_dir: Path,
    financial_mvp_contract_path: Path,
    output_path: Path,
) -> dict[str, object]:
    """Create a new spec bound to one already-verified layered return artifact."""

    _reject_output_alias(
        output_path,
        (template_path, layered_generation_dir, financial_mvp_contract_path),
    )
    template = _object_from_path(template_path, "provider-ledger template")
    _require_financial_mvp_contract_binding(template, financial_mvp_contract_path)
    verification = _verify_layered_history_generation(
        layered_generation_dir, financial_mvp_contract_path
    )
    returns_path, returns_sha256, returns_size_bytes = _declared_returns(
        layered_generation_dir, verification
    )
    _reject_output_alias(output_path, (returns_path,))
    candidate = _candidate_payload(template, returns_path, returns_sha256)
    _write_new(output_path, candidate)
    return {
        "layered_history_generation_id": str(verification["generation_id"]),
        "output_path": str(output_path),
        "returns_sha256": returns_sha256,
        "returns_size_bytes": returns_size_bytes,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--layered-generation-dir", type=Path, required=True)
    parser.add_argument("--financial-mvp-contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = rebind_provider_ledger_strategy_inputs(
            template_path=args.template,
            layered_generation_dir=args.layered_generation_dir,
            financial_mvp_contract_path=args.financial_mvp_contract,
            output_path=args.output,
        )
    except (OSError, ValueError) as exc:
        print(f"provider-ledger-strategy-inputs-rebind: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
