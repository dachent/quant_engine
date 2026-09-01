from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quant_engine.matrixport import governance as governance_module
from quant_engine.matrixport.catalog import catalog_rows
from quant_engine.matrixport.governance import TERMS_SHA256, TERMS_VERSION
from quant_engine.matrixport.util import canonical_json


@pytest.fixture(autouse=True)
def canonical_governance_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "canonical-governance"
    monkeypatch.setattr(governance_module, "_canonical_state_root", lambda: root)
    return root


@pytest.fixture
def synthetic_live_read_evidence(
    tmp_path: Path,
) -> Callable[..., Path]:
    evidence_root = tmp_path / "synthetic-live-read-evidence"
    evidence_root.mkdir()
    sequence = itertools.count()
    catalog_sha256 = hashlib.sha256(
        canonical_json(catalog_rows()).encode("utf-8")
    ).hexdigest()

    def write(
        *,
        scope: Iterable[tuple[str, str]],
        max_requests: int,
        operation: str = "extract",
        mode: str | None = "full",
    ) -> Path:
        now = datetime.now(timezone.utc)
        document = {
            "schema_version": 1,
            "operation": operation,
            "mode": mode,
            "scope": {
                "endpoints": [
                    {"method": method, "path": path} for method, path in scope
                ]
            },
            "review": {
                "identity": "synthetic-test-reviewer",
                "reference": "https://github.com/dachent/quant_engine/issues/374#issuecomment-1",
            },
            "authority": {
                "status": "CURRENT",
                "issued_at": (now - timedelta(minutes=1)).isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
            },
            "terms": {"version": TERMS_VERSION, "sha256": TERMS_SHA256},
            "catalog": {"sha256": catalog_sha256},
            "metering": {"max_requests": max_requests, "retry_count": 0},
        }
        path = evidence_root / f"synthetic-live-read-{next(sequence)}.json"
        path.write_text(canonical_json(document) + "\n", encoding="utf-8")
        return path

    return write
