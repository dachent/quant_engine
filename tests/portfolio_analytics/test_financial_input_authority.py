"""Retained input authority may change only source selection, never calculations."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

import portfolio_invested_capital as capital
import portfolio_report as reporter
from quant_engine.core.contracts import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[2]


def _canonical(value: object) -> bytes:
    return canonical_json_bytes(value)


def test_governed_policy_rebinds_only_product_authority() -> None:
    current = json.loads(reporter._PORTABLE_HWM_POLICY.read_bytes())
    retained_contract = b'{"retained":"synthetic source authority"}'
    expected = deepcopy(current)
    expected["product_authority"]["raw_sha256"] = hashlib.sha256(
        retained_contract
    ).hexdigest()
    expected["product_authority"]["semantic_digest"] = hashlib.sha256(
        _canonical(json.loads(retained_contract))
    ).hexdigest()
    expected_bytes = _canonical(expected)
    actual = capital.governed_hwm_input_policy(
        reporter._PORTABLE_HWM_POLICY.read_bytes(),
        retained_contract,
        hashlib.sha256(expected_bytes).hexdigest(),
    )
    assert actual == expected_bytes


def test_governed_policy_preserves_retained_pretty_bytes_when_only_authority_moves() -> (
    None
):
    """A retained v4 receipt may keep its admitted non-canonical JSON layout."""

    current = json.loads(reporter._PORTABLE_HWM_POLICY.read_bytes())
    retained_contract = b'{\n  "retained": "native source authority"\n}\n'
    old_raw = current["product_authority"]["raw_sha256"]
    old_semantic = current["product_authority"]["semantic_digest"]
    new_raw = hashlib.sha256(retained_contract).hexdigest()
    new_semantic = hashlib.sha256(_canonical(json.loads(retained_contract))).hexdigest()
    retained_pretty = json.dumps(current, indent=1).encode("utf-8") + b"\n"
    expected = retained_pretty.replace(old_raw.encode(), new_raw.encode(), 1).replace(
        old_semantic.encode(), new_semantic.encode(), 1
    )

    actual = capital.governed_hwm_input_policy(
        retained_pretty,
        retained_contract,
        hashlib.sha256(expected).hexdigest(),
    )

    assert actual == expected
    assert json.loads(actual) == {
        **current,
        "product_authority": {
            **current["product_authority"],
            "raw_sha256": new_raw,
            "semantic_digest": new_semantic,
        },
    }


@pytest.mark.parametrize(
    "current_newline, expected_newline", [(b"\n", b"\r\n"), (b"\r\n", b"\n")]
)
def test_governed_policy_accepts_lf_crlf_interchange(
    current_newline: bytes, expected_newline: bytes
) -> None:
    current = json.loads(reporter._PORTABLE_HWM_POLICY.read_bytes())
    current["test_note"] = r"line1\nline2"
    current_lf = json.dumps(current, indent=1).encode("utf-8") + b"\n"
    current_lf = current_lf.replace(b"\r\n", b"\n")
    current_bytes = current_lf.replace(b"\n", current_newline)
    retained_contract = b'{"retained":"native source authority"}'
    expected = deepcopy(current)
    expected["product_authority"]["raw_sha256"] = hashlib.sha256(
        retained_contract
    ).hexdigest()
    expected["product_authority"]["semantic_digest"] = hashlib.sha256(
        _canonical(json.loads(retained_contract))
    ).hexdigest()
    expected_lf = json.dumps(expected, indent=1).encode("utf-8") + b"\n"
    expected_lf = expected_lf.replace(b"\r\n", b"\n")
    expected_bytes = expected_lf.replace(b"\n", expected_newline)

    actual = capital.governed_hwm_input_policy(
        current_bytes,
        retained_contract,
        hashlib.sha256(expected_bytes).hexdigest(),
    )

    assert actual == expected_bytes
    assert json.loads(actual) == expected
    assert b'"test_note": "line1\\\\nline2"' in actual


def test_governed_policy_rejects_unknown_expected_hash() -> None:
    with pytest.raises(capital.CapitalValidationError, match="HWM policy"):
        capital.governed_hwm_input_policy(
            reporter._PORTABLE_HWM_POLICY.read_bytes(),
            reporter._PORTABLE_HWM_PRODUCT_CONTRACT.read_bytes(),
            "0" * 64,
        )


def test_governed_policy_rejects_changed_current_calculation() -> None:
    current = json.loads(reporter._PORTABLE_HWM_POLICY.read_bytes())
    expected_bytes = _canonical(current)
    current["policy_id"] = "SYNTHETIC-CHANGED-CALCULATION"
    with pytest.raises(capital.CapitalValidationError, match="HWM policy"):
        capital.governed_hwm_input_policy(
            _canonical(current),
            reporter._PORTABLE_HWM_PRODUCT_CONTRACT.read_bytes(),
            hashlib.sha256(expected_bytes).hexdigest(),
        )


def test_governed_policy_preserves_exact_already_bound_bytes() -> None:
    content = reporter._PORTABLE_HWM_POLICY.read_bytes()
    assert (
        capital.governed_hwm_input_policy(
            content,
            reporter._PORTABLE_HWM_PRODUCT_CONTRACT.read_bytes(),
            hashlib.sha256(content).hexdigest(),
        )
        == content
    )


def test_crosswalk_accepts_current_selected_baseline_date() -> None:
    crosswalk = json.loads(
        (
            ROOT / "contracts/account-strategy-current-epoch-crosswalk-v1.json"
        ).read_bytes()
    )
    crosswalk["as_of_date"] = "2026-09-04"
    crosswalk["calculation_cutoff_date"] = "2026-09-04"
    crosswalk["selected_baseline"]["as_of_date"] = "2026-09-04"
    reporter._validate_current_epoch_crosswalk(crosswalk)


def test_crosswalk_rejects_cutoff_after_selected_baseline() -> None:
    crosswalk = json.loads(
        (
            ROOT / "contracts/account-strategy-current-epoch-crosswalk-v1.json"
        ).read_bytes()
    )
    crosswalk["calculation_cutoff_date"] = "2026-09-05"
    with pytest.raises(reporter.ReportValidationError, match="cutoff"):
        reporter._validate_current_epoch_crosswalk(crosswalk)


def test_same_contract_uses_existing_capital_authority(tmp_path: Path) -> None:
    contract_path = tmp_path / "current.json"
    content = b'{"synthetic":"unchanged selected contract"}'
    contract_path.write_bytes(content)
    inputs = tmp_path / "capital-input.json"
    inputs.write_bytes(
        _canonical({"product_contract_sha256": hashlib.sha256(content).hexdigest()})
    )
    assert capital.resolve_financial_input_authority(inputs, contract_path, {}) == (
        contract_path,
        content,
    )


def test_retained_authority_rejects_unbound_provider_spec(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    current.write_bytes(b"{}")
    ledger = tmp_path / "provider-ledger.json"
    ledger.write_bytes(b"{}")
    spec = tmp_path / "provider-spec.json"
    spec.write_bytes(b"{}")
    ledger_binding, _ = capital._file_binding(ledger)
    spec_binding, _ = capital._file_binding(spec)
    inputs = tmp_path / "capital-input.json"
    inputs.write_bytes(
        _canonical(
            {
                "authority_profile": "ACCEPTED_GOVERNED",
                "product_contract_sha256": "1" * 64,
                "source_bindings": {
                    "provider_ledger": ledger_binding,
                    "provider_ledger_spec": spec_binding,
                },
            }
        )
    )
    spec.write_bytes(b'{"synthetic":"tampered specification"}')
    with pytest.raises(capital.CapitalValidationError, match="source binding differs"):
        capital.resolve_financial_input_authority(inputs, current, {})


def test_retained_authority_rejects_provider_outside_baseline(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    current.write_bytes(b"{}")
    ledger = tmp_path / "provider-ledger.json"
    ledger.write_bytes(b"{}")
    spec = tmp_path / "provider-spec.json"
    spec.write_bytes(b"{}")
    (tmp_path / "generation.json").write_bytes(b"{}")
    ledger_binding, _ = capital._file_binding(ledger)
    spec_binding, _ = capital._file_binding(spec)
    inputs = tmp_path / "capital-input.json"
    inputs.write_bytes(
        _canonical(
            {
                "authority_profile": "ACCEPTED_GOVERNED",
                "product_contract_sha256": "1" * 64,
                "source_bindings": {
                    "provider_ledger": ledger_binding,
                    "provider_ledger_spec": spec_binding,
                },
            }
        )
    )
    with pytest.raises(capital.CapitalValidationError, match="selected baseline"):
        capital.resolve_financial_input_authority(inputs, current, {})
