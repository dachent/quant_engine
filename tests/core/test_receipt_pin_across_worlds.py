from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
