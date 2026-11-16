from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import portfolio_pipeline_materialize as materialize
import portfolio_attended_preparation as attended


def _world(root: Path, name: str, observation: bytes, evidence: bytes) -> Path:
    world = root / name
    generation = world / "ledger" / "generations" / ("a" * 64)
    generation.mkdir(parents=True)
    (world / "c").mkdir()
    (world / "c" / "run-observation.json").write_bytes(observation)
    (world / "finality.json").write_bytes(evidence)
    return generation


def test_a_receipt_built_in_world_a_pins_resolve_in_world_b(tmp_path: Path) -> None:
    """Kill test (#907 item 5): the receipt names files, the consumer's pin resolves
    them against the world it runs in (attended preparation re-opens the pin)."""

    evidence = b'{"e": 1}'
    evidence_ref = {
        "path": "finality.json",
        "sha256": hashlib.sha256(evidence).hexdigest(),
        "size_bytes": len(evidence),
    }
    observation = json.dumps(
        {
            "providers": {
                "ibkr": {
                    "finality_evidence": {
                        **evidence_ref,
                        "path": "/world-A/finality.json",
                    }
                }
            }
        }
    ).encode()
    receipt_observation = {
        "path": "run-observation.json",
        "sha256": hashlib.sha256(observation).hexdigest(),
        "size_bytes": len(observation),
        "providers": [{"provider_id": "ibkr", "finality_evidence": evidence_ref}],
    }
    generation_b = _world(tmp_path, "world-B", observation, evidence)
    # the (stale) original observation names world A, the receipt names nothing
    provenance = materialize._source_provenance(receipt_observation, generation_b)
    pinned = attended._pin(
        {key: provenance[key] for key in ("path", "sha256", "size_bytes")}, "obs"
    )
    assert pinned == (tmp_path / "world-B" / "c" / "run-observation.json").resolve()


def test_a_raw_hash_run_observation_receipt_is_still_readable(tmp_path: Path) -> None:
    """Kill test (round-2, Kimi): receipts written before the normalized identity (release
    A, main f327b0ec) pin the observation by the raw bytes' hash and size.  The locator
    and the matcher keep reading them; a tampered file is still refused."""

    import hashlib
    import json

    observation = {
        "financial_cutoff": "2026-11-06",
        "providers": {
            "ibkr": {
                "root": "C:/Users/Someone/_quant_engine/dataIBKR",
                "generation_id": "a" * 64,
                "finality_evidence": {
                    "path": "C:/x/finality.json",
                    "sha256": "e" * 64,
                    "size_bytes": 10,
                },
            }
        },
    }
    raw = json.dumps(observation).encode("utf-8")
    raw_sha, raw_size = hashlib.sha256(raw).hexdigest(), len(raw)
    generation = tmp_path / "ledger" / "generations" / ("g" * 64)
    generation.mkdir(parents=True)
    (tmp_path / "run-observation.json").write_bytes(raw)
    found = materialize._locate_run_observation(
        None, generation, "run-observation.json", raw_sha
    )
    assert found == tmp_path / "run-observation.json"
    assert materialize._observation_matching_receipt(found, raw_sha, raw_size) == (
        observation
    )
    with pytest.raises(materialize.MaterializationError):
        materialize._observation_matching_receipt(found, "0" * 64, raw_size)
