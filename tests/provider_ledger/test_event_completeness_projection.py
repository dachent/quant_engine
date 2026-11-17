"""The ledger evaluates a pinned completeness declaration and records the verdict."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "tpl_admit_or_declare", HERE / "test_admit_or_declare.py"
)
assert _SPEC is not None and _SPEC.loader is not None
admit = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("tpl_admit_or_declare", admit)
_SPEC.loader.exec_module(admit)
ledger = admit.ledger

PROVIDER = "ibkr"


def _pin(tmp_path: Path, account: str, **over: Any) -> dict[str, Any]:
    entry = {
        "account_id": account,
        "fact_type": "flows",
        "declared_start": "2026-01-05",
        "declared_end": "2026-01-09",
        "closure": "IBKR_CHANGE_IN_NAV_FLOW_TIE_OUT",
        "evidence_refs": [{"path": "C:/evidence/flex.xml", "sha256": "a" * 64}],
        "ruling": "owner ruling 2026-11-17",
    }
    entry.update(over)
    raw = json.dumps({"schema_version": 1, "declarations": [entry]}).encode()
    path = tmp_path / "declaration.json"
    path.write_bytes(raw)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def _catalog(staged: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    generation = tmp_path / "staged" / "generations" / str(staged["generation_id"])
    return json.loads((generation / "provider-ledger.json").read_bytes())


def test_a_declaration_without_closure_evidence_is_recorded_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = f"private-account-{sorted(ledger._PROVIDER_CATALOGS).index(PROVIDER) + 1}"
    pin = _pin(tmp_path, account)
    _a, _b, staged, spec_path, _c = admit._stage_world(
        tmp_path, monkeypatch, {"event_completeness_declaration": pin}
    )
    catalog = _catalog(staged, tmp_path)
    rows = catalog["event_completeness"]
    assert [(r["account_id"], r["fact_type"]) for r in rows] == [(account, "flows")]
    # the synthetic IBKR generation has no statement change-in-NAV rows: the closure refuses
    assert rows[0]["status"] == "CLOSURE_FAILED"
    assert any(
        c["check"] == "STATEMENT_ROW_ON_EVERY_NAV_DAY" and c["status"] == "FAIL"
        for c in rows[0]["checks"]
    )
    ledger.verify_candidate(spec_path, str(staged["generation_id"]))


def test_without_a_declaration_the_catalog_has_no_completeness_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _a, _b, staged, _spec_path, _c = admit._stage_world(tmp_path, monkeypatch, {})
    assert "event_completeness" not in _catalog(staged, tmp_path)


def test_a_tampered_or_malformed_declaration_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = f"private-account-{sorted(ledger._PROVIDER_CATALOGS).index(PROVIDER) + 1}"
    tampered = tmp_path / "tampered"
    malformed = tmp_path / "malformed"
    tampered.mkdir()
    malformed.mkdir()
    pin = _pin(tampered, account)
    Path(pin["path"]).write_bytes(Path(pin["path"]).read_bytes() + b" ")
    with pytest.raises(ledger.ProviderLedgerError, match="identity differs"):
        admit._stage_world(
            tampered, monkeypatch, {"event_completeness_declaration": pin}
        )
    bad = _pin(malformed, account, ruling=" ")
    with pytest.raises(ledger.ProviderLedgerError, match="invalid"):
        admit._stage_world(
            malformed, monkeypatch, {"event_completeness_declaration": bad}
        )
