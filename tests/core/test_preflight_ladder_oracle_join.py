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


# ---- real-shaped manifests: the join identity is the ledger content sha256 ----

RUN_GENERATION = "d4d24f4cf839cfc14992d8e0af492096ddf05904d1bbc024da6fbfc3cb2d7308"
RUN_SHA = "b270c693bb8e957f1bc0f767707f4c5bd0d0f901cb14132108954a606c037f50"
FROZEN_GENERATION = "3b1445eee410890d1e0d9259ad7543011959208f6091e059eb4daa7c1c8c5a1f"
FROZEN_SHA = "ba5232ac" + "0" * 56
REAL_PHASE = {
    "provider_ledger_generation_id": RUN_GENERATION,
    "provider_ledger_catalog": {
        "path": "C:/Dev/q/out/generations/" + RUN_GENERATION + "/provider-ledger.json",
        "sha256": RUN_SHA,
        "size_bytes": 1,
    },
    "status": "PASS",
}


def _real_manifest(path, ledger_paths, sha=RUN_SHA):
    path.write_bytes(
        _json(
            {
                "schema": "qe-cp-acceptance-inputs/3",
                "cutoff": "2026-09-17",
                "inputs": [{"path": p, "sha256": sha, "bytes": 1} for p in ledger_paths]
                + [{"path": "x/account_activity.csv", "sha256": "f" * 64}],
            }
        )
    )
    return path


PRESERVED = "audit/state/reports/plan-evidence/v6-ledger-d4d24f/provider-ledger.json"


def test_real_shaped_preserved_copy_with_matching_sha_is_accepted(tmp_path):
    manifest = _real_manifest(tmp_path / "m.json", [PRESERVED])
    result = materializer._attended_oracle_ledger_join(
        REAL_PHASE, manifest, declares_acceptance=True
    )
    assert result["status"] == "MATCH"
    assert result["sha256"] == RUN_SHA
    assert result["generation_id_checked"] is False


def test_generation_folder_matching_is_checked(tmp_path):
    manifest = _real_manifest(
        tmp_path / "m.json",
        [
            f"dataPortfolio/provider-ledger/generations/{RUN_GENERATION}/provider-ledger.json"
        ],
    )
    result = materializer._attended_oracle_ledger_join(
        REAL_PHASE, manifest, declares_acceptance=True
    )
    assert result["status"] == "MATCH" and result["generation_id_checked"] is True


def test_sha_difference_refuses_even_for_a_preserved_copy(tmp_path):
    manifest = _real_manifest(tmp_path / "m.json", [PRESERVED], sha="a" * 64)
    with pytest.raises(
        materializer.MaterializationError, match="oracle ledger identity differs"
    ):
        materializer._attended_oracle_ledger_join(
            REAL_PHASE, manifest, declares_acceptance=True
        )


def test_generation_folder_difference_refuses_although_sha_is_equal(tmp_path):
    manifest = _real_manifest(
        tmp_path / "m.json", [f"x/{'9' * 64}/provider-ledger.json"]
    )
    with pytest.raises(
        materializer.MaterializationError,
        match="oracle ledger identity differs.*" + "9" * 64 + ".*" + RUN_GENERATION,
    ):
        materializer._attended_oracle_ledger_join(
            REAL_PHASE, manifest, declares_acceptance=True
        )


def test_manifest_naming_two_ledgers_refuses(tmp_path):
    manifest = _real_manifest(
        tmp_path / "m.json", [PRESERVED, f"x/{RUN_GENERATION}/provider-ledger.json"]
    )
    with pytest.raises(materializer.MaterializationError, match="more than one ledger"):
        materializer._attended_oracle_ledger_join(
            REAL_PHASE, manifest, declares_acceptance=True
        )


def test_frozen_old_manifest_still_refuses_against_the_v6_run(tmp_path):
    manifest = _real_manifest(
        tmp_path / "m.json",
        [
            f"dataPortfolio/provider-ledger/generations/{FROZEN_GENERATION}/provider-ledger.json"
        ],
        sha=FROZEN_SHA,
    )
    with pytest.raises(
        materializer.MaterializationError, match="oracle ledger identity differs"
    ):
        materializer._attended_oracle_ledger_join(
            REAL_PHASE, manifest, declares_acceptance=True
        )


def test_receipt_with_only_a_manifest_sha_is_refused_naming_the_manifest(tmp_path):
    receipt = tmp_path / "parent-execution-receipt.json"
    receipt.write_bytes(
        _json(
            {"schema": "qe-cp-parent-execution-receipt/1", "manifest_sha256": "7" * 64}
        )
    )
    with pytest.raises(
        materializer.MaterializationError, match="acceptance inputs MANIFEST"
    ):
        materializer._attended_oracle_ledger_join(
            REAL_PHASE, receipt, declares_acceptance=True
        )


def test_help_text_says_the_flag_takes_the_manifest():
    action = next(
        a
        for a in materializer._parser()
        ._subparsers._group_actions[0]
        .choices["attended"]
        ._actions
        if "--oracle-manifest" in a.option_strings
    )
    assert "inputs MANIFEST" in action.help and "only if it embeds" in action.help
    text = (ROOT / "docs" / "attended-generation-runbook.md").read_text(
        encoding="utf-8"
    )
    assert "only if it embeds" in text
