from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import date
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
LAST3 = "123"
AS_OF = date(2026, 1, 31)


def _statement_text(period: str = "January 1-31, 2026") -> str:
    return "\f".join(
        [
            "\n".join(
                [
                    "   CUST ROTH IRA",
                    "   Account Number   Statement Period",
                    f"   ****-*{LAST3}   {period}",
                    "Transactions - Summary",
                    "",
                    "   row",
                    "   $1.00  $0.00  $0.00  $0.00  $0.00  $0.00  $1.00",
                ]
            ),
            "page two",
        ]
    )


class _Pins:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.dir = tmp_path / "statements"
        self.dir.mkdir()
        label = zf.statement_label(PROVIDER, LAST3)
        self.name = f"{PROVIDER}-stmt-{label}-2026-01-31.pdf"
        (self.dir / self.name).write_bytes(b"synthetic pdf bytes")
        digest = hashlib.sha256((self.dir / self.name).read_bytes()).hexdigest()
        self.sums = tmp_path / "SHA256SUMS.txt"
        self.sums.write_bytes(f"{digest} *{self.name}\n".encode())
        self.mapping = tmp_path / "mapping.json"
        self.mapping.write_bytes(
            json.dumps(
                {
                    "schema_version": 1,
                    "accounts": [
                        {
                            "ledger_account_id": "account-" + "a" * 16,
                            "last3": LAST3,
                        }
                    ],
                }
            ).encode()
        )
        monkeypatch.setattr(
            zf,
            "_pdftotext",
            lambda path: _statement_text() if path.name == self.name else "",
        )

    def spec(self, **over: Any) -> dict[str, Any]:
        return {
            "provider_id": PROVIDER,
            "statements_dir": str(self.dir),
            "sums_path": str(self.sums),
            "sums_sha256": hashlib.sha256(self.sums.read_bytes()).hexdigest(),
            "mapping_path": str(self.mapping),
            "mapping_sha256": hashlib.sha256(self.mapping.read_bytes()).hexdigest(),
            **over,
        }


def test_the_ledger_derives_the_rows_from_the_retained_statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = _Pins(tmp_path, monkeypatch)
    account = "account-" + "a" * 16
    provider, rows, artifacts = ledger._zero_financing_rows(
        pins.spec(), {account}, {PROVIDER}, AS_OF
    )
    assert provider == PROVIDER
    assert [r["as_of_date"] for r in rows][:2] == ["2026-01-01", "2026-01-02"]
    assert len(rows) == 31
    first = rows[0]
    assert first["measurement_basis"] == "OBSERVED_ZERO_FINANCING_DAY"
    assert first["strategy_id"] is None and first["mapping_status"] == "UNMAPPED"
    assert first["source_page"] == 1
    # the row's statement hash is the hash the ledger itself computed from disk
    assert (
        first["source_artifact_sha256"]
        == hashlib.sha256(b"synthetic pdf bytes").hexdigest()
    )
    ids = {a.artifact_id for a in artifacts}
    assert f"zero-financing/{PROVIDER}/{pins.name}" in ids
    assert f"zero-financing/{PROVIDER}/SHA256SUMS" in ids
    assert f"zero-financing/{PROVIDER}/account-mapping" in ids


def test_every_pin_and_binding_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = _Pins(tmp_path, monkeypatch)
    account = "account-" + "a" * 16

    def call(spec: dict[str, Any], accounts: set[str] | None = None) -> Any:
        return ledger._zero_financing_rows(
            spec, accounts or {account}, {PROVIDER}, AS_OF
        )

    with pytest.raises(ledger.ProviderLedgerError, match="keys differ"):
        call({"provider_id": PROVIDER})
    with pytest.raises(ledger.ProviderLedgerError, match="does not declare"):
        call(pins.spec(provider_id="another"))
    with pytest.raises(ledger.ProviderLedgerError, match="sums identity differs"):
        call(pins.spec(sums_sha256="0" * 64))
    with pytest.raises(ledger.ProviderLedgerError, match="mapping identity differs"):
        call(pins.spec(mapping_sha256="0" * 64))
    # an account the provenance does not know is refused
    with pytest.raises(ledger.ProviderLedgerError, match="zero_financing_observations"):
        call(pins.spec(), {"another-account"})
    # a statement altered after pinning is refused (re-hashed from disk)
    (pins.dir / pins.name).write_bytes(b"altered")
    with pytest.raises(ledger.ProviderLedgerError, match="identity differs"):
        call(pins.spec())


def test_a_hand_written_artifact_cannot_stand_in_for_the_statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill test (F1): the old spec shape (an artifact file with an invented statement
    hash) is refused; nothing but the retained, re-hashed statements yields rows."""

    pins = _Pins(tmp_path, monkeypatch)
    forged = {"path": str(tmp_path / "zero.json"), "sha256": "a" * 64, "size_bytes": 1}
    with pytest.raises(ledger.ProviderLedgerError, match="keys differ"):
        ledger._zero_financing_rows(forged, {"account-" + "a" * 16}, {PROVIDER}, AS_OF)
    assert pins.spec()  # the only accepted shape is the pins


def test_a_staged_candidate_refuses_an_account_the_provider_does_not_observe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = _Pins(tmp_path, monkeypatch)
    monkeypatch.setattr(
        ledger,
        "_zero_financing_rows",
        lambda *_a, **_k: (
            PROVIDER,
            [{"account_id": "not-observed", "as_of_date": "2026-01-05"}],
            [],
        ),
    )
    with pytest.raises(ledger.ProviderLedgerError, match="does not observe"):
        admit._stage_world(
            tmp_path, monkeypatch, {"zero_financing_observations": pins.spec()}
        )


def test_a_staged_candidate_carries_rows_of_an_observed_account_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = _Pins(tmp_path, monkeypatch)
    observed = (
        f"private-account-{sorted(ledger._PROVIDER_CATALOGS).index(PROVIDER) + 1}"
    )
    row = {
        "account_id": observed,
        "as_of_date": "2026-01-05",
        "amount": {"currency": "USD", "status": "AVAILABLE", "value": "0"},
        "classification": zf.CLASSIFICATION,
        "measurement_basis": zf.BASIS,
        "provider_id": PROVIDER,
        "source_artifact_id": "zero-financing/ibkr/x.pdf",
        "source_artifact_sha256": "a" * 64,
        "source_field": "Statement account type",
        "source_page": 1,
        "verdict_reason": "RETIREMENT_ACCOUNT_NO_MARGIN_FEATURE",
        "evidence_layer": "ACCOUNT_LIVE",
        "generation_id": "zero-financing-x",
        "mapping_status": "UNMAPPED",
        "mapping_reason": "ACCOUNT_LEVEL_OBSERVATION",
        "record_id": "ibkr:zero-financing:synthetic-ibkr:2026-01-05",
        "source_record_id": "zero-financing:synthetic-ibkr:2026-01-05",
        "strategy_id": None,
    }
    late = {
        **row,
        "as_of_date": "2099-01-01",
        "record_id": "ibkr:late",
        "source_record_id": "late",
    }
    monkeypatch.setattr(
        ledger, "_zero_financing_rows", lambda *_a, **_k: (PROVIDER, [row, late], [])
    )
    _a, _b, staged, spec_path, _c = admit._stage_world(
        tmp_path, monkeypatch, {"zero_financing_observations": pins.spec()}
    )
    generation = tmp_path / "staged" / "generations" / str(staged["generation_id"])
    catalog = json.loads((generation / "provider-ledger.json").read_bytes())
    zero = [
        r
        for r in catalog["financing_liability_observations"]
        if r.get("measurement_basis") == "OBSERVED_ZERO_FINANCING_DAY"
    ]
    # the row past the provider's financial cutoff is not projected
    assert [r["as_of_date"] for r in zero] == ["2026-01-05"]
    ledger.verify_candidate(spec_path, str(staged["generation_id"]))


def _with_captures(pins: "_Pins", tmp_path: Path, **over: Any) -> dict[str, Any]:
    directory = tmp_path / "captures"
    directory.mkdir()
    label = zf.statement_label(PROVIDER, LAST3)
    raw = {
        "history.json": (
            json.dumps({"brokerageTransactions": [], "bookmark": None}).encode(),
            "history",
        ),
        "balances.json": (
            json.dumps(
                {
                    "brokerageAccountList": [
                        {
                            "accountId": f"****{LAST3}",
                            "cashInvestments": {"marginBalance": 0.0},
                        }
                    ]
                }
            ).encode(),
            "balances",
        ),
        "balances-page.txt": (b"Balance Subject to Interest   $0.00 ", "balances"),
    }
    for raw_name, (content, _kind) in raw.items():
        (directory / raw_name).write_bytes(content)
    body = {
        "schema_version": 1,
        "provider_id": PROVIDER,
        "account_last3": LAST3,
        "captured_at_utc": "2026-02-03T12:00:00Z",
        "capture_date": "2026-02-03",
        "window_start": "2026-01-20",
        "window_end": "2026-02-03",
        "balances": {
            "margin_loan_balance": "0.00",
            "balance_subject_to_interest": "0.00",
        },
        "transactions_reported_count": 0,
        "transactions": [],
        "transactions_complete": True,
        "raw_files": [
            {
                "name": raw_name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "kind": kind,
            }
            for raw_name, (content, kind) in raw.items()
        ],
    }
    body.update(over)
    name = f"{PROVIDER}-capture-{label}.json"
    (directory / name).write_text(json.dumps(body), encoding="utf-8")
    sums = tmp_path / "CAPTURE-SUMS.txt"
    lines = [
        f"{hashlib.sha256((directory / n).read_bytes()).hexdigest()} *{n}"
        for n in (name, *raw)
    ]
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return pins.spec(
        captures_dir=str(directory),
        captures_sums_path=str(sums),
        captures_sums_sha256=hashlib.sha256(sums.read_bytes()).hexdigest(),
    )


def test_pinned_captures_enter_the_manifest_and_are_hash_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = _Pins(tmp_path, monkeypatch)
    account = "account-" + "a" * 16
    spec = _with_captures(pins, tmp_path)
    _provider, rows, artifacts = ledger._zero_financing_rows(
        spec, {account}, {PROVIDER}, AS_OF
    )
    ids = {a.artifact_id for a in artifacts}
    assert f"zero-financing/{PROVIDER}/captures/SHA256SUMS" in ids
    assert any(i.endswith("history.json") for i in ids)
    assert len(rows) == 31  # as_of is the statement end: the capture adds no later day
    with pytest.raises(ledger.ProviderLedgerError, match="capture sums identity"):
        ledger._zero_financing_rows(
            {**spec, "captures_sums_sha256": "0" * 64}, {account}, {PROVIDER}, AS_OF
        )
    (tmp_path / "captures" / "history.json").write_bytes(b"altered")
    with pytest.raises(ledger.ProviderLedgerError, match="identity differs"):
        ledger._zero_financing_rows(spec, {account}, {PROVIDER}, AS_OF)


def test_a_capture_of_another_account_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = _Pins(tmp_path, monkeypatch)
    spec = _with_captures(pins, tmp_path, account_last3="999")
    with pytest.raises(ledger.ProviderLedgerError, match="capture account differs"):
        ledger._zero_financing_rows(spec, {"account-" + "a" * 16}, {PROVIDER}, AS_OF)


def test_the_generation_id_binds_mapping_captures_and_extractor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill test (N4): a different mapping, capture set or extractor is a different
    generation, not the same id over different rows."""

    pins = _Pins(tmp_path, monkeypatch)
    account = "account-" + "a" * 16
    plain = pins.spec()
    with_captures = _with_captures(pins, tmp_path)

    def generation(spec: dict[str, Any]) -> str:
        _provider, rows, _artifacts = ledger._zero_financing_rows(
            spec, {account}, {PROVIDER}, AS_OF
        )
        return str(rows[0]["generation_id"])

    base = generation(plain)
    assert generation(with_captures) != base
    monkeypatch.setattr(zf, "extractor_identity", lambda: "pdftotext version 99")
    assert generation(plain) != base
