"""A value the finality evidence declares derived is labelled so, per account row and position row.

Synthetic evidence, accounts and positions only; no real identifier or figure appears here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import portfolio_report as reporter

DERIVED = "DERIVED_FROM_OFFICIAL_CLOSES"
CAPTURED = "CAPTURED_AFTER_NEW_YORK_CLOSE_OF_AS_OF_DATE"
H_CLOSES = "c" * 64
H_RECORD = "d" * 64


def _evidence(tmp_path: Path, accounts: list[str], dates: list[str]) -> dict[str, Any]:
    document = {
        "derived_patch": {
            "derivation_basis": DERIVED,
            "patched_dates": dates,
            "official_closes_manifest": {
                "path": "m.json",
                "sha256": H_CLOSES,
                "size_bytes": 1,
            },
            "derivation_record": {
                "path": "r.json",
                "sha256": H_RECORD,
                "size_bytes": 1,
            },
            "patches": [{"account_id": a} for a in accounts],
        }
    }
    raw = json.dumps(document).encode("utf-8")
    path = tmp_path / "finality.json"
    path.write_bytes(raw)
    return {
        "source_provenance": {
            "providers": {
                "prov": {
                    "finality_evidence": {
                        "path": str(path),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "size_bytes": len(raw),
                    },
                    "finality_interpretation": {"provider_finality_guarantee": False},
                },
                "native-only": {
                    "finality_evidence": _plain(tmp_path),
                    "finality_interpretation": {"provider_finality_guarantee": False},
                },
            }
        }
    }


def _plain(tmp_path: Path) -> dict[str, Any]:
    raw = json.dumps({"checks": []}).encode("utf-8")
    path = tmp_path / "plain.json"
    path.write_bytes(raw)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def _catalog() -> dict[str, Any]:
    def position(
        account: str, security: str, day: str = "2026-09-17"
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "account_id": account,
            "as_of_date": day,
            "mapping_status": "MAPPED",
            "market_value": {"currency": "USD", "status": "AVAILABLE", "value": "10"},
            "quantity": "1",
            "security_id": security,
            "strategy_id": "strategy-1",
        }
        if day == "2026-09-17":
            row.update(
                as_of_basis=CAPTURED,
                capture_date="2026-09-18",
                observed_at="2026-09-18T03:59:40+00:00",
            )
        return row

    return {
        "positions": [
            position("acct-derived", "sec-1"),
            position("acct-derived", "sec-2"),
            position("acct-derived", "sec-old", "2026-09-10"),
            position("acct-native", "sec-3"),
        ]
    }


def test_the_derived_set_comes_from_the_pinned_evidence_file(tmp_path: Path) -> None:
    index = reporter._derived_value_index(
        _evidence(tmp_path, ["acct-derived"], ["2026-09-17"])
    )
    assert len(index) == 1  # the provider without a derived patch contributes nothing
    basis = reporter._derived_value_basis(index, "acct-derived", "2026-09-17")
    assert basis is not None
    assert basis["basis"] == DERIVED
    assert basis["official_closes_manifest_sha256"] == H_CLOSES
    assert basis["derivation_record_sha256"] == H_RECORD
    assert len(basis["finality_evidence_sha256"]) == 64
    assert basis["provider_finality_guarantee"] is False
    assert reporter._derived_value_basis(index, "acct-native", "2026-09-17") is None
    assert reporter._derived_value_basis(index, "acct-derived", "2026-09-10") is None
    assert reporter._derived_value_index(None) == []


def test_a_tampered_evidence_file_is_refused(tmp_path: Path) -> None:
    phase = _evidence(tmp_path, ["acct-derived"], ["2026-09-17"])
    (tmp_path / "finality.json").write_bytes(b'{"derived_patch": {}}')
    with pytest.raises(reporter.ReportValidationError):
        reporter._derived_value_index(phase)


def test_every_derived_position_carries_the_basis_and_none_says_captured(
    tmp_path: Path,
) -> None:
    index = reporter._derived_value_index(
        _evidence(tmp_path, ["acct-derived"], ["2026-09-17"])
    )
    rows = reporter._positions(_catalog(), None, index)
    derived = [
        r
        for r in rows
        if r["account_id"] == "acct-derived" and r["as_of_date"] == "2026-09-17"
    ]
    assert len(derived) == 2
    for row in derived:
        assert row["value_basis"]["basis"] == DERIVED
        assert row["as_of_basis"] == DERIVED
        assert (
            row["capture_date"] == "2026-09-18"
        )  # the capture it was held under is kept
        assert row["observed_at"] == "2026-09-18T03:59:40+00:00"
    # observed rows keep the captured basis and carry no value_basis
    native = next(r for r in rows if r["account_id"] == "acct-native")
    assert native["as_of_basis"] == CAPTURED and "value_basis" not in native
    old = next(r for r in rows if r["security_id"] == "sec-old")
    assert "value_basis" not in old and "as_of_basis" not in old


def test_no_row_may_pair_a_derived_value_with_a_captured_basis(tmp_path: Path) -> None:
    """The invariant over a whole built payload: derived <=> value_basis, never CAPTURED_*."""

    index = reporter._derived_value_index(
        _evidence(tmp_path, ["acct-derived"], ["2026-09-17"])
    )
    for row in reporter._positions(_catalog(), None, index):
        derived = "value_basis" in row
        assert derived == (row.get("as_of_basis") == DERIVED)
        if derived:
            assert not str(row["as_of_basis"]).startswith("CAPTURED")
    # without the evidence the old labelling is unchanged (and not claimed derived)
    for row in reporter._positions(_catalog()):
        assert "value_basis" not in row


def test_account_nav_rows_name_the_basis_only_where_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = reporter._derived_value_index(
        _evidence(tmp_path, ["acct-derived"], ["2026-09-17"])
    )
    catalog = {
        "accounts": [
            {
                "account_id": "acct-derived",
                "role": "INVESTMENT",
                "provider": "PROV",
                "base_currency": "USD",
            },
            {
                "account_id": "acct-native",
                "role": "INVESTMENT",
                "provider": "PROV",
                "base_currency": "USD",
            },
        ],
        "nav_observations": [
            {
                "account_id": a,
                "as_of_date": "2026-09-17",
                "nav": {"currency": "USD", "status": "AVAILABLE", "value": "5"},
            }
            for a in ("acct-derived", "acct-native")
        ],
    }
    monkeypatch.setattr(
        reporter,
        "_current_capital",
        lambda _c: {
            "reserve": {
                "status": "NOT_READY_AT_CUTOFF",
                "as_of_date": None,
                "current_value": {
                    "currency": "USD",
                    "status": "UNAVAILABLE",
                    "value": None,
                },
            }
        },
    )
    rows = {
        r["account_id"]: r for r in reporter._accounts(catalog, derived_values=index)
    }
    assert rows["acct-derived"]["value_basis"]["basis"] == DERIVED
    assert rows["acct-derived"]["status"] == "AVAILABLE_LATEST_OBSERVATION"
    assert "value_basis" not in rows["acct-native"]


def _row_errors(rows: list[dict[str, Any]]) -> list[str]:
    from jsonschema import Draft202012Validator

    schema_path = (
        Path(__file__).resolve().parents[2]
        / "schemas/portfolio/control-panel-data.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(
        {"$ref": "#/$defs/rows_with_value_basis", "$defs": schema["$defs"]}
    )
    return [e.message for e in validator.iter_errors(rows)]


def test_the_panel_schema_refuses_a_derived_row_without_its_basis_or_with_a_captured_one() -> (
    None
):
    basis = {
        "basis": DERIVED,
        "derivation_record_sha256": H_RECORD,
        "finality_evidence_sha256": "e" * 64,
        "official_closes_manifest_sha256": H_CLOSES,
        "provider_finality_guarantee": False,
    }
    assert _row_errors([{"as_of_basis": DERIVED, "value_basis": basis}]) == []
    assert _row_errors([{"as_of_basis": CAPTURED}]) == []
    assert _row_errors([{"as_of_basis": DERIVED}])  # derived label without the evidence
    assert _row_errors(
        [{"as_of_basis": CAPTURED, "value_basis": basis}]
    )  # captured label on a derived value
    assert _row_errors(
        [
            {
                "as_of_basis": DERIVED,
                "value_basis": {**basis, "provider_finality_guarantee": True},
            }
        ]
    )
