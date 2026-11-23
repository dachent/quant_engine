"""The ledger side of the derived zero-financing days (F2, Matrixport): the pins, the
one-observed-account rule and the projection beside the statement-derived rows.

Synthetic fixtures only (the reader's own fixtures are reused)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).parent


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


admit = _load("tzr_admit_or_declare", HERE / "test_admit_or_declare.py")
projection = _load(
    "tzr_zero_financing_projection", HERE / "test_zero_financing_projection.py"
)
fixtures = _load(
    "tzr_replay_fixtures", HERE.parent / "core" / "test_zero_financing_replay.py"
)
ledger = admit.ledger
AS_OF = fixtures.CUTOFF
ACCOUNT = fixtures.ACCOUNT


def _pin(tmp_path: Path, **over: bytes) -> dict[str, Any]:
    files = fixtures._files(**over)
    pinned: dict[str, Any] = {}
    for name, raw in files.items():
        path = tmp_path / name
        path.write_bytes(raw)
        pinned[name] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    return {
        "provider_id": fixtures.PROVIDER,
        "files": pinned,
        "credit_response_sha256": fixtures.BODY_SHA,
    }


def _facts(*accounts: str) -> dict[str, Any]:
    return {
        fixtures.PROVIDER: {
            "nav_observations": [
                {"account_id": a, "as_of_date": "2026-03-03"} for a in accounts
            ]
        }
    }


def _call(ref: dict[str, Any], accounts: tuple[str, ...] = (ACCOUNT,)) -> Any:
    return ledger._zero_financing_replay_rows(
        ref,
        {fixtures.PROVIDER},
        _facts(*accounts),
        AS_OF,
        {a: fixtures.FIRST for a in accounts},
    )


def test_the_ledger_derives_one_row_per_day_from_the_pinned_files(
    tmp_path: Path,
) -> None:
    provider, rows, artifacts = _call(_pin(tmp_path))
    assert provider == fixtures.PROVIDER
    assert len(rows) == 7
    first = rows[0]
    assert first["measurement_basis"] == "OBSERVED_ZERO_FINANCING_DAY"
    assert first["verdict_reason"].startswith("DERIVED_")
    assert first["strategy_id"] is None and first["mapping_status"] == "UNMAPPED"
    assert (
        first["source_artifact_sha256"] == hashlib.sha256(fixtures._bills()).hexdigest()
    )
    ids = {a.artifact_id for a in artifacts}
    assert ids == {
        f"zero-financing/{fixtures.PROVIDER}/replay/{n}"
        for n in (
            "request_manifest.csv",
            "wallet_bills.csv",
            "wallet_balances.csv",
            "coverage.csv",
            "credit-accounts-response.bin",
        )
    }
    assert first["source_artifact_id"] in ids  # the manifest knows the row's source


def test_every_pin_and_the_403_body_hash_is_enforced(tmp_path: Path) -> None:
    ref = _pin(tmp_path)
    with pytest.raises(ledger.ProviderLedgerError, match="keys differ"):
        _call({**ref, "extra": 1})
    with pytest.raises(ledger.ProviderLedgerError, match="undeclared provider"):
        _call({**ref, "provider_id": "another"})
    changed = json.loads(json.dumps(ref))
    changed["files"]["wallet_bills.csv"]["sha256"] = "0" * 64
    with pytest.raises(ledger.ProviderLedgerError, match="identity differs"):
        _call(changed)
    with pytest.raises(ledger.ProviderLedgerError, match="body hash differs"):
        _call({**ref, "credit_response_sha256": "1" * 64})
    # a pinned file altered on disk after pinning is refused (re-hashed from disk)
    Path(ref["files"]["coverage.csv"]["path"]).write_bytes(b"altered")
    with pytest.raises(ledger.ProviderLedgerError, match="identity differs"):
        _call(ref)


def test_a_non_closing_ledger_or_a_loan_bill_is_refused_by_the_ledger(
    tmp_path: Path,
) -> None:
    loan = fixtures._bills([fixtures._bill("100", "100", "AAA", "1", 1, "3001")])
    with pytest.raises(ledger.ProviderLedgerError, match="not proven non-credit"):
        _call(_pin(tmp_path, **{"wallet_bills.csv": loan}))


def test_the_provider_must_have_exactly_one_observed_account(tmp_path: Path) -> None:
    ref = _pin(tmp_path)
    with pytest.raises(
        ledger.ProviderLedgerError, match="exactly one observed account"
    ):
        _call(ref, ())
    with pytest.raises(
        ledger.ProviderLedgerError, match="exactly one observed account"
    ):
        _call(ref, (ACCOUNT, "account-" + "b" * 16))


def test_a_staged_candidate_projects_replay_rows_beside_the_statement_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = projection._Pins(tmp_path, monkeypatch)
    observed = f"private-account-{sorted(ledger._PROVIDER_CATALOGS).index(projection.PROVIDER) + 1}"

    def row(day: str, reason: str) -> dict[str, Any]:
        return {
            "account_id": observed,
            "as_of_date": day,
            "amount": {"currency": "USD", "status": "AVAILABLE", "value": "0"},
            "classification": "ZERO_FINANCING_OBSERVED",
            "measurement_basis": "OBSERVED_ZERO_FINANCING_DAY",
            "provider_id": projection.PROVIDER,
            "source_artifact_id": "zero-financing/x/y",
            "source_artifact_sha256": "a" * 64,
            "source_field": "f",
            "source_page": 1,
            "verdict_reason": reason,
            "evidence_layer": "ACCOUNT_LIVE",
            "generation_id": "zero-financing-x",
            "mapping_status": "UNMAPPED",
            "mapping_reason": "ACCOUNT_LEVEL_OBSERVATION",
            "record_id": f"p:{day}:{reason}",
            "source_record_id": f"{day}:{reason}",
            "strategy_id": None,
        }

    monkeypatch.setattr(
        ledger,
        "_zero_financing_rows",
        lambda *_a, **_k: (projection.PROVIDER, [row("2026-01-05", "STATEMENT")], []),
    )
    monkeypatch.setattr(
        ledger,
        "_zero_financing_replay_rows",
        lambda *_a, **_k: (
            projection.PROVIDER,
            [row("2026-01-06", "DERIVED_X"), row("2099-01-01", "DERIVED_LATE")],
            [],
        ),
    )
    spec = {**pins.spec(), "replays": [{}]}
    _a, _b, staged, _spec_path, _c = admit._stage_world(
        tmp_path, monkeypatch, {"zero_financing_observations": spec}
    )
    generation = tmp_path / "staged" / "generations" / str(staged["generation_id"])
    catalog = json.loads((generation / "provider-ledger.json").read_bytes())
    zero = [
        r
        for r in catalog["financing_liability_observations"]
        if r.get("measurement_basis") == "OBSERVED_ZERO_FINANCING_DAY"
    ]
    # both sources are projected; the row past the financial cutoff is not
    assert sorted(r["as_of_date"] for r in zero) == ["2026-01-05", "2026-01-06"]
    assert date(2026, 1, 6) <= projection.AS_OF
