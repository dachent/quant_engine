"""The ledger evaluates a pinned completeness declaration and records the verdict."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

HERE = Path(__file__).parent
SCHEMA = HERE.parents[1] / "schemas/portfolio/provider-ledger.schema.json"
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
    evidence = tmp_path / "evidence.bin"
    evidence.write_bytes(b"cited evidence bytes")
    entry = {
        "account_id": account,
        "fact_type": "flows",
        "declared_start": "2026-01-05",
        "declared_end": "2026-01-09",
        "closure": "IBKR_CHANGE_IN_NAV_FLOW_TIE_OUT",
        "evidence_refs": [
            {
                "path": str(evidence),
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            }
        ],
        "citation": "owner ruling 2026-11-17",
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
    bad = _pin(malformed, account, citation=" ")
    with pytest.raises(ledger.ProviderLedgerError, match="invalid"):
        admit._stage_world(
            malformed, monkeypatch, {"event_completeness_declaration": bad}
        )


def test_a_cited_evidence_file_must_exist_with_the_cited_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = f"private-account-{sorted(ledger._PROVIDER_CATALOGS).index(PROVIDER) + 1}"
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    pin = _pin(wrong, account)
    (wrong / "evidence.bin").write_bytes(b"other bytes")
    with pytest.raises(ledger.ProviderLedgerError, match="evidence differs"):
        admit._stage_world(wrong, monkeypatch, {"event_completeness_declaration": pin})
    absent = tmp_path / "absent"
    absent.mkdir()
    pin = _pin(absent, account)
    (absent / "evidence.bin").unlink()
    with pytest.raises(ledger.ProviderLedgerError):
        admit._stage_world(absent, monkeypatch, {"event_completeness_declaration": pin})


def test_a_typed_outcome_reaches_the_catalog_with_its_verified_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = f"private-account-{sorted(ledger._PROVIDER_CATALOGS).index(PROVIDER) + 1}"
    pin = _pin(
        tmp_path, account, fact_type="positions", closure="CAPTURED_NOT_ADMITTED"
    )
    _a, _b, staged, spec_path, _c = admit._stage_world(
        tmp_path, monkeypatch, {"event_completeness_declaration": pin}
    )
    row = _catalog(staged, tmp_path)["event_completeness"][0]
    assert row["status"] == "CAPTURED_NOT_ADMITTED" and row["fact_type"] == "positions"
    ledger.verify_candidate(spec_path, str(staged["generation_id"]))


def test_a_provider_catalog_cannot_name_another_providers_account() -> None:
    catalog = {"accounts": [{"account_id": "account-own"}]}
    own = ledger._own_account_items(
        "ibkr",
        catalog,
        [{"account_id": "account-own", "closed_date": "2026-01-01"}],
        "account_id",
    )
    assert len(own) == 1
    with pytest.raises(ledger.ProviderLedgerError, match="outside its own accounts"):
        ledger._own_account_items(
            "ibkr", catalog, [{"account_id": "account-other"}], "account_id"
        )
    with pytest.raises(ledger.ProviderLedgerError, match="outside its own accounts"):
        ledger._own_account_items("schwab", catalog, ["account-other"], None)


@pytest.mark.parametrize(
    "closure", ["IBKR_CHANGE_IN_NAV_FLOW_TIE_OUT", "CAPTURED_NOT_ADMITTED"]
)
def test_a_built_catalog_with_event_completeness_validates_against_the_published_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closure: str
) -> None:
    account = f"private-account-{sorted(ledger._PROVIDER_CATALOGS).index(PROVIDER) + 1}"
    pin = _pin(tmp_path, account, closure=closure)
    _a, _b, staged, _spec_path, _c = admit._stage_world(
        tmp_path, monkeypatch, {"event_completeness_declaration": pin}
    )
    catalog = _catalog(staged, tmp_path)
    assert catalog["event_completeness"]
    # the synthetic world is not a full seven-provider catalog, so the published schema is applied to the
    # key the producer adds (every other key is covered by the baseline's full-catalog validation)
    published = json.loads(SCHEMA.read_text(encoding="utf-8"))
    schema = {
        **published["properties"]["event_completeness"],
        "$defs": published["$defs"],
    }
    validator = Draft202012Validator(schema)
    validator.validate(catalog["event_completeness"])
    # the rows are closed: a field the producer does not emit is refused
    catalog["event_completeness"][0]["unexpected"] = 1
    with pytest.raises(Exception, match="unexpected"):
        validator.validate(catalog["event_completeness"])
