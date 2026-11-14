from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import zero_financing_statements as zf

HERE = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "tpl_admit_or_declare", HERE / "test_admit_or_declare.py"
)
assert _SPEC is not None and _SPEC.loader is not None
admit = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("tpl_admit_or_declare", admit)
_SPEC.loader.exec_module(admit)
ledger = admit.ledger

ACCOUNT = "synthetic-ibkr"
PROVIDER = "ibkr"


def _artifact(path: Path, days: list[str], account: str = ACCOUNT) -> dict[str, Any]:
    rows = [
        {
            "account_id": account,
            "as_of_date": day,
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "0"},
            "classification": zf.CLASSIFICATION,
            "measurement_basis": zf.BASIS,
            "provider_id": PROVIDER,
            "source_artifact_id": "schwab-statements/schwab-stmt-aaaaaaaa-2026-01-31.pdf",
            "source_artifact_sha256": "a" * 64,
            "source_field": "LOAN_ZERO_NO_INTEREST_NO_MOVEMENT",
            "source_page": 1,
        }
        for day in days
    ]
    raw = zf.canonical(
        {
            "schema_version": 1,
            "artifact_id": zf.ARTIFACT_ID,
            "provider_id": PROVIDER,
            "rows": rows,
            "unobserved_periods": [],
        }
    )
    path.write_bytes(raw)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def test_the_spec_key_projects_validated_rows_and_refuses_the_rest(
    tmp_path: Path,
) -> None:
    ref = _artifact(tmp_path / "zero.json", ["2026-01-05", "2026-01-06"])
    rows = ledger._zero_financing_rows(ref, {ACCOUNT}, {PROVIDER})
    assert [r["as_of_date"] for r in rows] == ["2026-01-05", "2026-01-06"]
    first = rows[0]
    assert first["measurement_basis"] == "OBSERVED_ZERO_FINANCING_DAY"
    assert first["strategy_id"] is None and first["mapping_status"] == "UNMAPPED"
    assert first["source_artifact_sha256"] == "a" * 64
    assert first["record_id"].endswith(":2026-01-05")
    with pytest.raises(ledger.ProviderLedgerError, match="identity differs"):
        ledger._zero_financing_rows({**ref, "sha256": "0" * 64}, {ACCOUNT}, {PROVIDER})
    with pytest.raises(ledger.ProviderLedgerError, match="identity differs"):
        ledger._zero_financing_rows({**ref, "size_bytes": 1}, {ACCOUNT}, {PROVIDER})
    with pytest.raises(ledger.ProviderLedgerError, match="does not declare"):
        ledger._zero_financing_rows(ref, {ACCOUNT}, {"another-provider"})
    with pytest.raises(ledger.ProviderLedgerError, match="keys differ"):
        ledger._zero_financing_rows({"path": ref["path"]}, {ACCOUNT}, {PROVIDER})
    # an account the provenance does not know is refused, never projected
    with pytest.raises(ledger.ProviderLedgerError, match="zero_financing_observations"):
        ledger._zero_financing_rows(ref, {"another-account"}, {PROVIDER})
    # not an artifact at all
    junk = tmp_path / "junk.json"
    junk.write_bytes(b"{}")
    bad = {
        "path": str(junk),
        "sha256": hashlib.sha256(b"{}").hexdigest(),
        "size_bytes": 2,
    }
    with pytest.raises(ledger.ProviderLedgerError, match="zero_financing_observations"):
        ledger._zero_financing_rows(bad, {ACCOUNT}, {PROVIDER})


def test_a_staged_candidate_carries_the_zero_rows_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _artifact(tmp_path / "zero.json", ["2026-01-05", "2026-01-06", "2026-01-07"])
    _a, _b, staged, spec_path, _c = admit._stage_world(
        tmp_path, monkeypatch, {"zero_financing_observations": ref}
    )
    generation = tmp_path / "staged" / "generations" / str(staged["generation_id"])
    catalog = json.loads((generation / "provider-ledger.json").read_bytes())
    zero = [
        r
        for r in catalog["financing_liability_observations"]
        if r.get("measurement_basis") == "OBSERVED_ZERO_FINANCING_DAY"
    ]
    assert [r["as_of_date"] for r in zero] == ["2026-01-05", "2026-01-06", "2026-01-07"]
    ledger.verify_candidate(spec_path, str(staged["generation_id"]))
