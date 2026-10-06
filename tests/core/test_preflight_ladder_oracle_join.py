"""Rung (d): the oracle ledger identity joins the run ledger identity."""

from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

import portfolio_pipeline_materialize as materializer
from preflight_ladder_fixture import (
    REFUSAL_SECONDS,
    ROOT,
    _json,
    _sha,
    make_ledger,
)

# ---------------------------------------------------------------- (d) oracle join


def _source_phase(ledger: Path) -> dict[str, object]:
    return {
        "provider_ledger_generation_id": ledger.name,
        "provider_ledger_catalog": {
            "path": str(ledger / "provider-ledger.json"),
            "sha256": _sha((ledger / "provider-ledger.json").read_bytes()),
        },
        "status": "PASS",
    }


def _oracle_manifest(path: Path, ledger: Path) -> Path:
    catalog = ledger / "provider-ledger.json"
    path.write_bytes(
        _json(
            {
                "schema": "qe-cp-acceptance-inputs/2",
                "cutoff": "2026-09-17",
                "inputs": [
                    {
                        "path": f"dataPortfolio/provider-ledger/generations/{ledger.name}/provider-ledger.json",
                        "sha256": _sha(catalog.read_bytes()),
                        "bytes": catalog.stat().st_size,
                    },
                    {"path": "x/account_activity.csv", "sha256": "f" * 64},
                ],
            }
        )
    )
    return path


def test_oracle_join_passes_when_the_ledgers_are_the_same(good, tmp_path):
    manifest = _oracle_manifest(tmp_path / "oracle.json", good["ledger"])
    result = materializer._attended_oracle_ledger_join(
        _source_phase(good["ledger"]), manifest, declares_acceptance=True
    )
    assert result["status"] == "MATCH"


def test_oracle_join_accepts_a_receipt_embedding_the_manifest(good, tmp_path):
    manifest = _oracle_manifest(tmp_path / "oracle.json", good["ledger"])
    receipt = tmp_path / "parent-execution-receipt.json"
    receipt.write_bytes(_json({"manifest": json.loads(manifest.read_bytes())}))
    result = materializer._attended_oracle_ledger_join(
        _source_phase(good["ledger"]), receipt, declares_acceptance=True
    )
    assert result["status"] == "MATCH"


def test_oracle_join_refuses_a_different_ledger_naming_both(good, tmp_path):
    old = make_ledger(tmp_path / "old-ledger" / ("9" * 64), "v5")
    manifest = _oracle_manifest(tmp_path / "oracle.json", old)
    started = time.monotonic()
    with pytest.raises(
        materializer.MaterializationError,
        match=r"oracle ledger identity differs.*" + "9" * 12 + r".*" + "a" * 12,
    ):
        materializer._attended_oracle_ledger_join(
            _source_phase(good["ledger"]), manifest, declares_acceptance=True
        )
    assert time.monotonic() - started < REFUSAL_SECONDS


def test_oracle_join_refuses_an_unrecognised_document(good, tmp_path):
    bogus = tmp_path / "receipt.json"
    bogus.write_bytes(_json({"hello": 1}))
    with pytest.raises(materializer.MaterializationError, match="no ledger input"):
        materializer._attended_oracle_ledger_join(
            _source_phase(good["ledger"]), bogus, declares_acceptance=True
        )


def test_oracle_join_not_checked_is_loud_for_an_acceptance_run(good, capsys):
    result = materializer._attended_oracle_ledger_join(
        _source_phase(good["ledger"]), None, declares_acceptance=True
    )
    assert result["status"] == "NOT_CHECKED"
    assert "NOT_CHECKED" in capsys.readouterr().err


def test_oracle_join_is_silent_for_a_run_that_declares_no_acceptance(good, capsys):
    result = materializer._attended_oracle_ledger_join(
        _source_phase(good["ledger"]), None, declares_acceptance=False
    )
    assert result is None
    assert capsys.readouterr().err == ""


def test_oracle_flags_are_parsed():
    args = materializer._parser().parse_args(
        [
            "attended",
            "--repo-root", "r", "--python", "p", "--workspace-root", "w",
            "--output-root", "o", "--retained-cache-root", "c",
            "--requested-as-of", "2026-09-17", "--source-run", "s",
            "--oracle-manifest", "m.json", "--declares-acceptance",
        ]
    )  # fmt: skip
    assert args.oracle_manifest == Path("m.json")
    assert args.declares_acceptance is True


def test_runbook_names_the_oracle_join():
    text = (ROOT / "docs" / "attended-generation-runbook.md").read_text(
        encoding="utf-8"
    )
    assert "--oracle-manifest" in text and "NOT_CHECKED" in text
