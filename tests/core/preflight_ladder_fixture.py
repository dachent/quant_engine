"""Fixtures shared by the preflight-ladder tests (one test file per rung)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

import pytest

import check_retained_authority_pins as pins

REFUSAL_SECONDS = 60.0
ROOT = Path(__file__).resolve().parents[2]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def make_ledger(root: Path, tag: str) -> Path:
    root.mkdir(parents=True)
    (root / "generation.json").write_bytes(f"manifest-{tag}".encode())
    (root / "provider-ledger.json").write_bytes(f"catalog-{tag}".encode())
    return root


def ledger_rows(ledger: Path) -> list[dict[str, str]]:
    return [
        {
            "schema_name": "generation-manifest",
            "sha256": _sha((ledger / "generation.json").read_bytes()),
            "source_key": "provider-ledger-generation",
        },
        {
            "schema_name": "provider-ledger-catalog",
            "sha256": _sha((ledger / "provider-ledger.json").read_bytes()),
            "source_key": "provider-ledger-generation",
        },
        {
            "schema_name": "custody-authority",
            "sha256": "e" * 64,
            "source_key": "selected-custody-authority",
        },
    ]


def make_baseline(root: Path, ledger: Path) -> Path:
    """A retained baseline (or allocation) generation directory."""
    root.mkdir(parents=True)
    (root / "portfolio-baseline.json").write_bytes(
        _json({"source_artifacts": ledger_rows(ledger)})
    )
    return root


def make_attribution(root: Path, baseline: Path) -> Path:
    root.mkdir(parents=True)
    (root / "strategy-attribution.json").write_bytes(
        _json(
            {
                "baseline": {
                    "generation_id": baseline.name,
                    "catalog_sha256": _sha(
                        (baseline / "portfolio-baseline.json").read_bytes()
                    ),
                }
            }
        )
    )
    return root


def build_good(tmp_path: Path) -> dict[str, Path]:
    ledger = make_ledger(tmp_path / "ledger" / ("a" * 64), "v6")
    baseline = make_baseline(tmp_path / "baseline" / ("b" * 64), ledger)
    return {
        "ledger": ledger,
        "baseline": baseline,
        "allocation": make_baseline(tmp_path / "allocation" / ("c" * 64), ledger),
        "attribution": make_attribution(tmp_path / "attr" / ("d" * 64), baseline),
        "dependence": ledger,
    }


def _matrix(good: dict[str, Path], **override: Path) -> list[dict[str, str]]:
    retained = {role: path for role, path in good.items() if role != "ledger"}
    retained.update(override)
    return pins.check_ledger_pin_matrix(good["ledger"], retained=retained)


def _refuses(callable_, match: str) -> float:
    started = time.monotonic()
    with pytest.raises(Exception, match=match):
        callable_()
    elapsed = time.monotonic() - started
    assert elapsed < REFUSAL_SECONDS
    return elapsed
