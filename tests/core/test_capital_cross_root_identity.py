"""A financial artifact must not depend on the directory it was built in.

Two pipeline runs over identical inputs, differing only in output root, produced
different `report_id` and `panel_id`. Nothing numerical differed: the capital
input generation embedded absolute locators, so its bytes and therefore its
sha256 changed with the run root, and that hash flowed into the report, whose
hash is the panel identity.

`portfolio_pipeline_materialize` refuses an existing output root
("attended output root already exists; use --resume"), so every fresh run has a
new root by construction. Cross-root identity is therefore not an optional
nicety -- without it, acceptance experiment 2's "same financial artifacts and
accepted pointers" can never be satisfied by any two runs.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str) -> Any:
    module_name = f"_cross_root_{name}"
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "tools" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


capital = _load("portfolio_invested_capital")


def _build_under(root: Path) -> tuple[Path, dict[str, Any]]:
    """Write one bound source set and its portable generation under `root`."""

    root.mkdir(parents=True)
    ledger_dir = root / "generations" / ("a" * 64)
    ledger_dir.mkdir(parents=True)
    ledger = ledger_dir / "provider-ledger.json"
    ledger.write_bytes(b'{"as_of_date":"2026-09-17"}')
    spec = root / "provider-spec.json"
    spec.write_bytes(b'{"spec":1}')

    generation = {
        "schema_version": 1,
        "authority_profile": "SYNTHETIC",
        "source_bindings": {
            "provider_ledger": {
                **capital._file_binding(ledger)[0],  # noqa: SLF001
                "generation_id": ledger.parent.name,
            },
            "provider_ledger_spec": capital._file_binding(spec)[0],  # noqa: SLF001
        },
    }
    path = root / "capital-input-generation.json"
    path.write_bytes(capital._canonical(generation))  # noqa: SLF001
    capital.write_local_sidecar(
        path, {"provider_ledger": ledger, "provider_ledger_spec": spec}
    )
    return path, generation


def test_input_generation_bytes_are_identical_under_two_roots(
    tmp_path: Path,
) -> None:
    """Identical inputs under different roots must produce identical bytes."""

    first, _ = _build_under(tmp_path / "run-one")
    second, _ = _build_under(tmp_path / "run-two-with-a-much-longer-name")

    assert first.read_bytes() == second.read_bytes(), (
        "the capital input generation differs between two roots, so every "
        "digest over it -- and the report and panel identities downstream -- "
        "depends on where the run happened"
    )


def test_the_generation_carries_no_absolute_locator(tmp_path: Path) -> None:
    """The guard must actually refuse what the producer must not emit."""

    path, generation = _build_under(tmp_path / "run")
    capital.assert_no_absolute_locators(
        generation, field_name="capital input generation"
    )

    poisoned = json.loads(path.read_bytes())
    poisoned["source_bindings"]["provider_ledger"]["path"] = str(path.resolve())
    with pytest.raises(ValueError):
        capital.assert_no_absolute_locators(
            poisoned, field_name="capital input generation"
        )


def test_locators_live_in_the_sidecar_and_resolve(tmp_path: Path) -> None:
    """The sidecar carries the machine-local truth, and only the sidecar."""

    path, _ = _build_under(tmp_path / "run")
    sidecar = capital.local_sidecar_path(path)
    assert sidecar.is_file()
    assert sidecar.name.endswith(".local.json")

    locators = capital.read_local_sidecar(path)
    assert set(locators) == {"provider_ledger", "provider_ledger_spec"}
    for located in locators.values():
        assert Path(located).is_absolute()
        assert Path(located).is_file()


def test_a_missing_sidecar_is_named_rather_than_crashing(tmp_path: Path) -> None:
    """A reader without locators must say so, not fail on a KeyError."""

    path, _ = _build_under(tmp_path / "run")
    capital.local_sidecar_path(path).unlink()
    with pytest.raises(
        capital.CapitalValidationError, match="locator sidecar is absent"
    ):
        capital.read_local_sidecar(path)
