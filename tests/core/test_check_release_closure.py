"""The release closure checker must see exactly the gap N1 was: a companion the
release's own frozen recipe pins by sha256, but which its own `inputs.zip` never
captured -- while not reporting a governance ledger's dangling historical
references as if they were the same kind of gap.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "check_release_closure.py"
SPEC = importlib.util.spec_from_file_location("check_release_closure", MODULE_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("unable to load the release closure checker")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _seal_release(
    release_dir: Path, files: dict[str, bytes], *, archive_name: str = "inputs.zip"
) -> None:
    """Write ``inputs.zip`` + ``inputs.manifest.json`` exactly as a real release does."""
    release_dir.mkdir(parents=True, exist_ok=True)
    archive_path = release_dir / archive_name
    with zipfile.ZipFile(archive_path, "w") as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    manifest = {
        "archive": archive_name,
        "schema_version": 1,
        "entries": len(files),
        "files": [
            {"path": path, "sha256": _sha256(data), "size_bytes": len(data)}
            for path, data in files.items()
        ],
    }
    (release_dir / "inputs.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def _pin(data: bytes, *, path: str = "") -> dict[str, Any]:
    return {"path": path, "sha256": _sha256(data), "size_bytes": len(data)}


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MODULE_PATH), *args], capture_output=True, text=True
    )


def test_missing_finality_evidence_is_reported(tmp_path: Path) -> None:
    """The exact shape of N1's Schwab gap: `run_observation` resolves, but what it
    points at (the binding evidence) was never captured."""
    missing_evidence = b'{"status": "PASS_NATIVE_CAPTURE_BINDING"}'
    run_observation = json.dumps(
        {"providers": {"schwab": {"finality_evidence": _pin(missing_evidence)}}}
    ).encode("utf-8")
    spec = json.dumps({"run_observation": _pin(run_observation)}).encode("utf-8")
    runner = json.dumps({"provider_ledger_spec": _pin(spec)}).encode("utf-8")

    release_dir = tmp_path / "release"
    _seal_release(
        release_dir,
        {
            "runner-input-config.json": runner,
            "spec.json": spec,
            "run-observation.json": run_observation,
            # `missing_evidence` itself is deliberately never captured.
        },
    )

    report = checker.walk(release_dir)

    assert report["missing_count"] == 1
    assert report["missing"][0]["sha256"] == _sha256(missing_evidence)
    assert report["missing"][0]["declaring_document"] == "run-observation.json"


def test_missing_provider_catalog_field_is_reported(tmp_path: Path) -> None:
    """The exact shape of N1's Alpaca/Fidelity gap: a flat `catalog_sha256` field,
    not a nested pin dict -- `iter_pins` alone cannot see it."""
    spec = json.dumps(
        {"providers": {"alpaca": {"catalog_sha256": "0" * 64, "qualification": "X"}}}
    ).encode("utf-8")
    runner = json.dumps({"provider_ledger_spec": _pin(spec)}).encode("utf-8")

    release_dir = tmp_path / "release"
    _seal_release(
        release_dir,
        {"runner-input-config.json": runner, "spec.json": spec},
    )

    report = checker.walk(release_dir)

    assert report["missing_count"] == 1
    assert report["missing"][0]["sha256"] == "0" * 64
    assert report["missing"][0]["declaring_document"] == "spec.json"


def test_fully_captured_recipe_chain_is_clean(tmp_path: Path) -> None:
    """The paired-good case: every companion the chain pins is actually captured."""
    evidence = b'{"status": "PASS_NATIVE_CAPTURE_BINDING"}'
    run_observation = json.dumps(
        {
            "providers": {
                "schwab": {"finality_evidence": _pin(evidence)},
                "alpaca": {"catalog_sha256": _sha256(b"alpaca catalog bytes")},
            }
        }
    ).encode("utf-8")
    spec = json.dumps({"run_observation": _pin(run_observation)}).encode("utf-8")
    runner = json.dumps({"provider_ledger_spec": _pin(spec)}).encode("utf-8")

    release_dir = tmp_path / "release"
    _seal_release(
        release_dir,
        {
            "runner-input-config.json": runner,
            "spec.json": spec,
            "run-observation.json": run_observation,
            "evidence/schwab-native-nav-capture-binding.json": evidence,
            "dataAlpaca/alpaca-generation.json": b"alpaca catalog bytes",
        },
    )

    report = checker.walk(release_dir)

    assert report["missing_count"] == 0
    assert report["missing"] == []


def test_supplement_resolves_a_pin_the_archive_lacks(tmp_path: Path) -> None:
    """The promotion path: a gap the archive has is closed by `inputs-supplement/`,
    never by re-sealing `inputs.zip`."""
    evidence = b'{"status": "PASS_NATIVE_CAPTURE_BINDING"}'
    run_observation = json.dumps(
        {"providers": {"schwab": {"finality_evidence": _pin(evidence)}}}
    ).encode("utf-8")
    spec = json.dumps({"run_observation": _pin(run_observation)}).encode("utf-8")
    runner = json.dumps({"provider_ledger_spec": _pin(spec)}).encode("utf-8")

    release_dir = tmp_path / "release"
    _seal_release(
        release_dir,
        {
            "runner-input-config.json": runner,
            "spec.json": spec,
            "run-observation.json": run_observation,
        },
    )

    report_before = checker.walk(release_dir)
    assert report_before["missing_count"] == 1

    supplement_dir = release_dir / "inputs-supplement"
    supplement_dir.mkdir()
    (supplement_dir / "evidence").mkdir()
    (
        supplement_dir / "evidence" / "schwab-native-nav-capture-binding.json"
    ).write_bytes(evidence)
    (supplement_dir / "MANIFEST.json").write_text(
        json.dumps(
            [
                {
                    "path_in_recipe": "evidence/schwab-native-nav-capture-binding.json",
                    "sha256": _sha256(evidence),
                    "bytes": len(evidence),
                    "recovered_from": "test fixture",
                }
            ]
        ),
        encoding="utf-8",
    )

    report_after = checker.walk(release_dir)
    assert report_after["missing_count"] == 0


def test_governance_ledger_subtree_is_not_reported(tmp_path: Path) -> None:
    """`retained_verified_generations` is a history ledger, not a companion this
    run reads -- a dangling reference inside it is not a closure gap (mirrors
    `check_retained_authority_pins.py` excluding published generations)."""
    runner = json.dumps(
        {
            "retained_verified_generations": {
                "attribution": {
                    "manifest": _pin(b"some prior cycle's attribution manifest")
                }
            }
        }
    ).encode("utf-8")

    release_dir = tmp_path / "release"
    _seal_release(release_dir, {"runner-input-config.json": runner})

    report = checker.walk(release_dir)

    assert report["missing_count"] == 0


def test_missing_portfolio_fx_rates_field_is_reported(tmp_path: Path) -> None:
    """Fix round 1 (Kimi r0, Critical): the top-level `portfolio_fx_rates` object pins
    an FX generation through the same flat-field shape as `providers.*` -- and was
    invisible to the walk before this fix (`tools/provider_ledger.py:2942-2949`)."""
    spec = json.dumps(
        {
            "portfolio_fx_rates": {
                "catalog_sha256": "1" * 64,
                "manifest_sha256": "2" * 64,
                "generation_id": "3" * 64,
                "root": "C:\\fake\\portfolio-fx-rates",
                "selection": "OWNER_SELECTED_YAHOO_DAILY_CLOSE_FOR_MATRIXPORT",
            }
        }
    ).encode("utf-8")
    runner = json.dumps({"provider_ledger_spec": _pin(spec)}).encode("utf-8")

    release_dir = tmp_path / "release"
    _seal_release(release_dir, {"runner-input-config.json": runner, "spec.json": spec})

    report = checker.walk(release_dir)

    missing_shas = {item["sha256"] for item in report["missing"]}
    assert missing_shas == {"1" * 64, "2" * 64}
    # `generation_id` is deliberately not a pin (see module docstring): it never appears.
    assert "3" * 64 not in missing_shas


def test_captured_portfolio_fx_rates_field_is_clean(tmp_path: Path) -> None:
    """Paired good case for the fix above: the generation the field pins is captured."""
    catalog = b"fx catalog bytes"
    manifest = b"fx manifest bytes"
    spec = json.dumps(
        {
            "portfolio_fx_rates": {
                "catalog_sha256": _sha256(catalog),
                "manifest_sha256": _sha256(manifest),
                "generation_id": "3" * 64,
                "root": "C:\\fake\\portfolio-fx-rates",
                "selection": "OWNER_SELECTED_YAHOO_DAILY_CLOSE_FOR_MATRIXPORT",
            }
        }
    ).encode("utf-8")
    runner = json.dumps({"provider_ledger_spec": _pin(spec)}).encode("utf-8")

    release_dir = tmp_path / "release"
    _seal_release(
        release_dir,
        {
            "runner-input-config.json": runner,
            "spec.json": spec,
            "dataPortfolio/portfolio-fx-rates/generations/3.../catalog.json": catalog,
            "dataPortfolio/portfolio-fx-rates/generations/3.../generation.json": manifest,
        },
    )

    report = checker.walk(release_dir)

    assert report["missing_count"] == 0


def test_missing_financial_mvp_field_is_reported(tmp_path: Path) -> None:
    """Fix round 1 (Kimi r0, Important): `financial_mvp.{contract,registry,schema}_sha256`
    (`tools/provider_ledger.py:2148-2158`) are also flat sha256 pins and were likewise
    invisible before this fix -- checked, not merely documented as excluded."""
    spec = json.dumps(
        {
            "financial_mvp": {
                "contract_path": "C:\\fake\\contract.json",
                "contract_sha256": "4" * 64,
                "registry_path": "C:\\fake\\registry.json",
                "registry_sha256": "5" * 64,
                "schema_path": "C:\\fake\\schema.json",
                "schema_sha256": "6" * 64,
            }
        }
    ).encode("utf-8")
    runner = json.dumps({"provider_ledger_spec": _pin(spec)}).encode("utf-8")

    release_dir = tmp_path / "release"
    _seal_release(release_dir, {"runner-input-config.json": runner, "spec.json": spec})

    report = checker.walk(release_dir)

    missing_shas = {item["sha256"] for item in report["missing"]}
    assert missing_shas == {"4" * 64, "5" * 64, "6" * 64}


def test_corrupted_document_is_reported_distinctly_from_missing(tmp_path: Path) -> None:
    """Fix round 1 (Kimi r0, Important): resolution must re-hash content, not trust the
    manifest's declared sha256 -> path mapping. A closure member whose bytes were
    swapped after sealing must fail, not silently resolve."""
    real_spec = json.dumps({"purpose": "the real spec"}).encode("utf-8")
    swapped_spec = json.dumps({"purpose": "different bytes entirely"}).encode("utf-8")
    runner = json.dumps({"provider_ledger_spec": _pin(real_spec)}).encode("utf-8")

    release_dir = tmp_path / "release"
    _seal_release(
        release_dir, {"runner-input-config.json": runner, "spec.json": real_spec}
    )
    # Rewrite the archive member's bytes without updating inputs.manifest.json's
    # declared sha256 for it -- exactly what a corrupted or hand-edited zip looks like.
    archive_path = release_dir / "inputs.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("runner-input-config.json", runner)
        zf.writestr("spec.json", swapped_spec)

    report = checker.walk(release_dir)

    assert report["missing_count"] == 0, report["missing"]
    assert report["corrupt_count"] == 1
    assert report["corrupt"][0]["sha256"] == _sha256(real_spec)
    assert report["corrupt"][0]["declaring_document"] == "runner-input-config.json"


def test_main_exits_nonzero_when_a_document_is_corrupted(tmp_path: Path) -> None:
    real_spec = json.dumps({"purpose": "the real spec"}).encode("utf-8")
    swapped_spec = json.dumps({"purpose": "different bytes entirely"}).encode("utf-8")
    runner = json.dumps({"provider_ledger_spec": _pin(real_spec)}).encode("utf-8")
    release_dir = tmp_path / "release"
    _seal_release(
        release_dir, {"runner-input-config.json": runner, "spec.json": real_spec}
    )
    with zipfile.ZipFile(release_dir / "inputs.zip", "w") as zf:
        zf.writestr("runner-input-config.json", runner)
        zf.writestr("spec.json", swapped_spec)

    result = _run_cli(str(release_dir))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "CORRUPT" in result.stdout


def test_unreadable_archive_member_does_not_crash_the_walk(tmp_path: Path) -> None:
    """Fix round 1 (Kimi r0, Minor): `zipfile.read` can raise more than `KeyError` for
    a corrupt or unsupported member; the walk must report it, not traceback."""
    runner = json.dumps({"provider_ledger_spec": _pin(b"never opened")}).encode("utf-8")
    release_dir = tmp_path / "release"
    _seal_release(release_dir, {"runner-input-config.json": runner})

    closure = checker._Closure(
        release_dir,
        archive_name=checker.DEFAULT_ARCHIVE_NAME,
        manifest_name=checker.DEFAULT_MANIFEST_NAME,
        supplement_dirname=checker.DEFAULT_SUPPLEMENT_DIRNAME,
        supplement_manifest_name=checker.DEFAULT_SUPPLEMENT_MANIFEST_NAME,
    )
    try:

        def _raise(_name: str) -> bytes:
            raise zipfile.BadZipFile("simulated corrupt member")

        closure._zip.read = _raise  # type: ignore[method-assign]
        sha = next(iter(closure._by_sha))
        assert closure.read(sha) is None
    finally:
        closure.close()


def test_no_recipe_entrypoint_raises(tmp_path: Path) -> None:
    release_dir = tmp_path / "release"
    _seal_release(release_dir, {"unrelated.json": b"{}"})

    with pytest.raises(checker.ReleaseClosureError):
        checker.walk(release_dir)


def test_main_exits_nonzero_when_a_pin_is_missing(tmp_path: Path) -> None:
    evidence = b'{"status": "PASS_NATIVE_CAPTURE_BINDING"}'
    run_observation = json.dumps(
        {"providers": {"schwab": {"finality_evidence": _pin(evidence)}}}
    ).encode("utf-8")
    spec = json.dumps({"run_observation": _pin(run_observation)}).encode("utf-8")
    runner = json.dumps({"provider_ledger_spec": _pin(spec)}).encode("utf-8")
    release_dir = tmp_path / "release"
    _seal_release(
        release_dir,
        {
            "runner-input-config.json": runner,
            "spec.json": spec,
            "run-observation.json": run_observation,
        },
    )

    result = _run_cli(str(release_dir))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "MISSING" in result.stdout
    assert _sha256(evidence) in result.stdout


def test_main_exits_zero_when_closure_is_complete(tmp_path: Path) -> None:
    """Fix round 1 (Kimi r0, Minor): assert against a walk that actually resolved a
    pin, not a vacuous one with zero pins checked -- a regression that made pin
    collection yield nothing would still pass the old version of this test."""
    contract = b"the contract bytes"
    runner = json.dumps(
        {"financial_input_contract": _pin(contract, path="contract.json")}
    ).encode("utf-8")
    release_dir = tmp_path / "release"
    _seal_release(
        release_dir,
        {"runner-input-config.json": runner, "contract.json": contract},
    )

    result = _run_cli(str(release_dir))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["pins_checked"] > 0
    assert payload["missing_count"] == 0
    assert payload["corrupt_count"] == 0
