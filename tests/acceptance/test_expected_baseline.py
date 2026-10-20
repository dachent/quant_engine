"""Schema and cross-derivation checks for acceptance/expected-baseline.json.

Standard library only (plan 4.5A oracle rule: acceptance/oracle code never
imports repo production code and never reads generation outputs). This test
reads only the committed baseline file itself.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "acceptance" / "expected-baseline.json"
HISTORICAL_V2_PATH = (
    REPO_ROOT / "acceptance" / "baselines" / "expected-baseline-v2.json"
)
HISTORICAL_V2_SHA256 = (
    "b87ab5bfd638d0ceb7d4415c05b5c43a7df7c48517519ddee0d815f4b9c6ce91"
)
HISTORICAL_09F6_PATH = (
    REPO_ROOT / "acceptance" / "baselines" / "expected-baseline-09f6-054ff579.json"
)
HISTORICAL_09F6_SHA256 = (
    "054ff579f8a4e940055a9987869ed4353e8e86a613b98623a4001b40be91a2ec"
)
HISTORICAL_FC9A_SHA256 = (
    "1584761a58de8a69b51c2b3fabbdfc6c3ba2b62d0a0171aaf6de9f54982bde4b"
)
HISTORICAL_FC9A_PATH = (
    REPO_ROOT / "acceptance" / "baselines" / "expected-baseline-fc9a-1584761a.json"
)
HISTORICAL_V3_PATH = (
    REPO_ROOT / "acceptance" / "baselines" / "expected-baseline-v3-49df41d2.json"
)
HISTORICAL_V3_SHA256 = (
    "cd828cc30cc7bce30ef92e77f72283d69ab93613b0a0d6fe8bbf5524c1d19181"
)
CURRENT_SUCCESSOR_HEAD = "6b9719c8dcce51000e0852624856e0a39062ef51"
CURRENT_AGREE_COUNT = 100
CURRENT_NOT_DERIVABLE_COUNT = 18
CURRENT_RAW_OUTPUT_SHA256 = {
    "a": "58e63c32236ef726130d4665c159588c178612bc0c3e65d6c754fedf04e240c5",
    "b": "a9fd8b0c5a380b3d8150b1073b93035e2282172854e1393032e865689a45b9ef",
}
CURRENT_ORACLE_MANIFEST_SHA256 = {
    # R1: each deriver ran its own copy of the same inputs (separate contract path).
    "a": "f4e11cdc6e534e12c772b424ddc3edd2d143419a8c069b533a9156c4f5ba64c0",
    "b": "f7bc9a3743ca947c33951a2c342e13fbaf9f4c742dbc3c8fc9b6d3c092c37ce4",
}
CURRENT_CONTRACT_POINTER = (
    "contracts/portfolio-financial-mvp-source-v1.json",
    "89c78abb39472da2fefed362f899bc7e119b35b5ea3a62d42654975f9615de16",
)
CURRENT_AUDIT_AUTHORITY_POINTERS: frozenset[tuple[str, str]] = frozenset()

TOLERANCE_UNITS = {"USD", "ratio"}
EXACT_UNITS = {"count", "date", "list", "status"}


def _load(path: Path = BASELINE_PATH) -> dict[str, Any]:
    raw = path.read_bytes()
    assert b"\r\n" not in raw, f"{path.name} must use LF line endings"
    value: dict[str, Any] = json.loads(raw.decode("utf-8"))
    return value


def _rows(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [*doc["metrics"], *doc["not_derivable"]]


def test_file_is_utf8_lf_json() -> None:
    assert BASELINE_PATH.is_file(), f"missing {BASELINE_PATH}"
    _load()  # decodes cleanly as UTF-8 JSON with no CRLF


def test_historical_v2_baseline_is_byte_exact() -> None:
    assert HISTORICAL_V2_PATH.is_file(), f"missing {HISTORICAL_V2_PATH}"
    raw = HISTORICAL_V2_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == HISTORICAL_V2_SHA256
    _load(HISTORICAL_V2_PATH)


def test_historical_09f6_baseline_is_byte_exact() -> None:
    assert HISTORICAL_09F6_PATH.is_file(), f"missing {HISTORICAL_09F6_PATH}"
    raw = HISTORICAL_09F6_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == HISTORICAL_09F6_SHA256
    _load(HISTORICAL_09F6_PATH)


def test_historical_v3_baseline_is_byte_exact() -> None:
    assert HISTORICAL_V3_PATH.is_file(), f"missing {HISTORICAL_V3_PATH}"
    raw = HISTORICAL_V3_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == HISTORICAL_V3_SHA256
    _load(HISTORICAL_V3_PATH)


def test_historical_fc9a_baseline_is_byte_exact() -> None:
    assert HISTORICAL_FC9A_PATH.is_file(), f"missing {HISTORICAL_FC9A_PATH}"
    raw = HISTORICAL_FC9A_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == HISTORICAL_FC9A_SHA256
    _load(HISTORICAL_FC9A_PATH)


def test_header_schema() -> None:
    doc = _load()
    assert doc["schema"] == "qe-cp-expected-baseline/1"
    assert doc["cutoff"] == "2026-09-17"
    assert isinstance(doc["conventions"], str) and doc["conventions"]
    assert set(doc["derivations"]) == {"a", "b"}
    for side in ("a", "b"):
        assert isinstance(doc["derivations"][side]["code_commit"], str)
        assert len(doc["derivations"][side]["code_commit"]) == 40
    assert isinstance(doc["generated_utc"], str) and doc["generated_utc"]


def test_metrics_and_not_derivable_present() -> None:
    doc = _load()
    assert isinstance(doc["metrics"], list) and doc["metrics"]
    assert isinstance(doc["not_derivable"], list) and doc["not_derivable"]


def test_every_row_has_metric_id_and_entity() -> None:
    doc = _load()
    for row in doc["metrics"]:
        assert isinstance(row["metric_id"], str) and row["metric_id"]
        assert isinstance(row["entity"], str) and row["entity"]
        assert "expected" in row


def test_no_null_authority_pointer_sha256() -> None:
    """Fix round 1 (Kimi K3 review of PR #485): every authority_pointer must
    carry a real sha256 -- a missing one (e.g. a contract file whose digest
    lives in derive_a.json's separate top-level "contracts" list rather than
    "inputs") must fail this test, not silently pass through as null."""
    doc = _load()
    offenders = [
        (row["metric_id"], row["entity"], pointer["path"])
        for row in doc["metrics"]
        for pointer in row.get("authority_pointer", [])
        if not pointer.get("sha256")
    ]
    assert not offenders, offenders


# Fix round 2 (Codex scoped re-review of cp/acc-baseline-v2): the gate is the
# stricter allowlist form -- a pointer passes only when it is data-root-relative
# under one of the declared prefixes the file actually uses. Any absolute or
# rooted path fails, including driveless Windows rooted forms such as
# "\\Dev\\scratch\\..." for which Path(...).is_absolute() is False and a ":\\"
# substring test is False too.
ALLOWED_AUTHORITY_POINTER_ROOTS = (
    "contracts/",
    "dataPortfolio/",
    "dataQuantbase/",
    "dataMatrixport/",
    "evidence/",
    "schemas/",
)
ALLOWED_AUDIT_AUTHORITY_POINTERS = frozenset(
    path for path, _sha256 in CURRENT_AUDIT_AUTHORITY_POINTERS
)


def _is_data_root_relative(path: str) -> bool:
    if PureWindowsPath(path).anchor or PurePosixPath(path).anchor:
        return False
    if chr(92) in path:
        return False
    parts = PurePosixPath(path).parts
    if not parts or any(part in {".", ".."} for part in parts):
        return False
    if path != str(PurePosixPath(path)):
        return False
    return (
        path.startswith(ALLOWED_AUTHORITY_POINTER_ROOTS)
        or path in ALLOWED_AUDIT_AUTHORITY_POINTERS
    )


def test_no_machine_local_absolute_authority_pointer_paths() -> None:
    """Fix round 1 (Kimi K3 + Codex review of cp/acc-baseline-v2): authority
    pointers must be data-root-relative, never machine-local absolute paths
    (e.g. C:\\Dev\\scratch\\... scratch lineage captures)."""
    doc = _load()
    offenders = [
        (row["metric_id"], row["entity"], pointer["path"])
        for row in doc["metrics"]
        for pointer in row.get("authority_pointer", [])
        if not _is_data_root_relative(pointer["path"])
    ]
    assert not offenders, offenders


def test_authority_pointer_gate_regression_rooted_and_relative() -> None:
    """Fix round 2 paired regression: a driveless rooted Windows pointer must
    FAIL the gate (it slipped past the round-1 is_absolute()/":\\" check), the
    "C:\\" form must still fail, and legit data-root-relative pointers must
    pass every declared prefix."""
    assert not _is_data_root_relative(
        r"\Dev\scratch\quant_engine\lineage\v2\x\accounts.csv"
    )
    assert not _is_data_root_relative(
        r"C:\Dev\scratch\quant_engine\lineage\v2\x\accounts.csv"
    )
    assert not _is_data_root_relative("/Dev/scratch/quant_engine/accounts.csv")
    for good in (
        "contracts/portfolio-financial-mvp-source-v1.json",
        "dataPortfolio/provider-ledger/generations/<sha>/provider-ledger.json",
        "dataQuantbase/source-runs/run/artifacts/csv/accounts.csv",
        "dataMatrixport/source-runs/run/csv/nav_daily.csv",
        "evidence/capture-run/evidence.json",
        "schemas/core/account.json",
    ):
        assert _is_data_root_relative(good), good
    # prefix must match at a component boundary, not as a loose substring
    assert not _is_data_root_relative("contracts-evil/payload.json")
    for path in ALLOWED_AUDIT_AUTHORITY_POINTERS:
        assert _is_data_root_relative(path), path
    for bad in (
        "contracts/../evidence/payload.json",
        "contracts/./portfolio-financial-mvp-source-v1.json",
        "contracts//portfolio-financial-mvp-source-v1.json",
        r"contracts\portfolio-financial-mvp-source-v1.json",
        "audit/coding/2026-09-26_quant_engine_corrective_program/state/build/codex-cr2-owner-post-20261002/unadmitted.json",
    ):
        assert not _is_data_root_relative(bad), bad


def test_tolerance_classes_match_spec() -> None:
    """USD +/-0.01, ratio +/-1e-8, everything else exact (no tolerance)."""
    doc = _load()
    for row in doc["metrics"]:
        unit = row["unit"]
        tolerance = row.get("tolerance")
        if unit == "USD":
            assert Decimal(tolerance) == Decimal("0.01"), row
        elif unit == "ratio":
            assert Decimal(tolerance) == Decimal("0.00000001"), row
        else:
            assert unit in EXACT_UNITS, f"unexpected unit {unit!r} in {row}"
            assert tolerance is None, row


def test_both_derivations_present_on_every_critical_row() -> None:
    doc = _load()
    for row in doc["metrics"]:
        if not row.get("critical"):
            continue
        assert "derivation_a" in row and "code_commit" in row["derivation_a"]
        assert "derivation_b" in row and "code_commit" in row["derivation_b"]
        assert row["derivation_a"]["value"] is not None
        assert row["derivation_b"]["value"] is not None
    for row in doc["not_derivable"]:
        if not row.get("critical"):
            continue
        assert row.get("reason_a") is not None
        assert row.get("reason_b") is not None


def _as_decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None


def test_expected_equals_derivation_a_and_agrees_with_derivation_b() -> None:
    """expected is always derivation_a's value (never averaged), and every AGREE
    row's derivation_b value must independently fall within the row's tolerance
    of derivation_a's value (dual-derivation confirmation, plan 4.5D)."""
    doc = _load()
    for row in doc["metrics"]:
        assert row["expected"] == row["derivation_a"]["value"], row
        unit = row["unit"]
        a_value = row["derivation_a"]["value"]
        b_value = row["derivation_b"]["value"]
        if unit in TOLERANCE_UNITS:
            a_dec, b_dec = _as_decimal(a_value), _as_decimal(b_value)
            assert a_dec is not None and b_dec is not None, row
            tolerance = Decimal(row["tolerance"])
            assert abs(a_dec - b_dec) <= tolerance, row
        elif unit == "list":
            a_list = a_value if isinstance(a_value, list) else [a_value]
            b_list = b_value if isinstance(b_value, list) else [b_value]
            assert sorted(map(str, a_list)) == sorted(map(str, b_list)), row
        else:
            assert str(a_value) == str(b_value), row


def test_not_derivable_rows_have_no_expected_value() -> None:
    doc = _load()
    for row in doc["not_derivable"]:
        assert isinstance(row["metric_id"], str) and row["metric_id"]
        assert isinstance(row["entity"], str) and row["entity"]
        assert "expected" not in row
        assert "reason_a" in row and "reason_b" in row


def test_current_successor_identity_and_authority_coverage() -> None:
    doc = _load()
    rows = _rows(doc)
    assert doc["head"] == CURRENT_SUCCESSOR_HEAD
    assert doc["inventory_sha256"] == (
        "2593811724ce4c2bf52d1039d7edabdee72b590b53d326bfe2a4167bd313c74d"
    )
    assert doc["pointer_map_sha256"] == (
        "fca86572166ee4b422d104587c3397b48757250f5a629e44d584b2a3e1d8de8a"
    )
    assert doc["admission_decision_sha256"] == (
        "7c43d6740c8d077d2d7d13330439699115cc1f84555f967574cd4f0526547278"
    )
    assert len(doc["metrics"]) == CURRENT_AGREE_COUNT
    assert len(doc["not_derivable"]) == CURRENT_NOT_DERIVABLE_COUNT
    assert len(rows) == 118
    for side in ("a", "b"):
        assert doc["derivations"][side]["code_commit"] == CURRENT_SUCCESSOR_HEAD
        assert (
            doc["derivations"][side]["raw_output_sha256"]
            == CURRENT_RAW_OUTPUT_SHA256[side]
        )
        assert (
            doc["derivations"][side]["manifest_sha256"]
            == CURRENT_ORACLE_MANIFEST_SHA256[side]
        )
        assert {row[f"derivation_{side}"]["code_commit"] for row in doc["metrics"]} == {
            CURRENT_SUCCESSOR_HEAD
        }
    pointers = [pointer for row in rows for pointer in row.get("authority_pointer", [])]
    for row in rows:
        contract_pointers = [
            (pointer["path"], pointer["sha256"])
            for pointer in row.get("authority_pointer", [])
            if pointer.get("path") == CURRENT_CONTRACT_POINTER[0]
        ]
        assert contract_pointers == [CURRENT_CONTRACT_POINTER], row
    audit_pointers = {
        (pointer["path"], pointer["sha256"])
        for pointer in pointers
        if str(pointer.get("path", "")).startswith("audit/")
    }
    assert audit_pointers == CURRENT_AUDIT_AUTHORITY_POINTERS


# --- v2 row set (successor of PR #485's v1; inputs acceptance/v2/*) ---

# Main commit (merge of PR #492) at which acceptance/derive_a and
# acceptance/derive_b, run from that checkout against the v2 inputs manifest,
# reproduce every row below; re-verified at main 549eb89 (post-#502), see
# derivations.*.reproduced_at_main. The pre-merge lane commits the baseline was
# first stamped with (fb10931 / dac6e41) are no longer named.
V2_COMMITS = {
    "a": "b7f9ec537506055a078d0d96058ea30f22d30f5a",
    "b": "b7f9ec537506055a078d0d96058ea30f22d30f5a",
}
V2_AGREE_COUNT = 91
V2_NOT_DERIVABLE_COUNT = 14


def _find(
    doc: dict[str, Any], metric_id: str, entity: str, window: Any = None
) -> dict[str, Any]:
    matches = [
        row
        for row in doc["metrics"]
        if row["metric_id"] == metric_id
        and row["entity"] == entity
        and row.get("window") == window
    ]
    assert len(matches) == 1, (metric_id, entity, window, len(matches))
    return matches[0]


def test_v2_row_set_and_derivation_identities() -> None:
    doc = _load(HISTORICAL_V2_PATH)
    assert len(doc["metrics"]) == V2_AGREE_COUNT
    assert len(doc["not_derivable"]) == V2_NOT_DERIVABLE_COUNT
    assert doc["derivations"]["a"]["code_commit"] == V2_COMMITS["a"]
    assert doc["derivations"]["b"]["code_commit"] == V2_COMMITS["b"]
    for row in doc["metrics"]:
        assert row["derivation_a"]["code_commit"] == V2_COMMITS["a"]
        assert row["derivation_b"]["code_commit"] == V2_COMMITS["b"]


def test_v2_quantbase_rederived_on_finalized_capture() -> None:
    """Volatis (Quantbase) re-derived on the finalized 52b3758a capture:
    59,092.28 USD as of 2026-09-17, TWR 0.6765085987, MWR 0.1956573642."""
    doc = _load(HISTORICAL_V2_PATH)
    for metric_id, entity in (
        ("capital.account.cutoff", "account-58e75e8d952fa1b8"),
        ("capital.strategy.cutoff", "volatis-strategy"),
    ):
        row = _find(doc, metric_id, entity)
        assert row["as_of"] == "2026-09-17", row
        assert Decimal(row["expected"]) == Decimal("59092.28"), row
    assert Decimal(
        _find(doc, "twr.strategy", "volatis-strategy", "ITD")["expected"]
    ) == Decimal("0.676508598689")
    assert Decimal(
        _find(doc, "mwr.strategy", "volatis-strategy", "ITD")["expected"]
    ) == Decimal("0.195657364162")
    assert Decimal(
        _find(doc, "capital.portfolio.systematic.cutoff", "portfolio")["expected"]
    ) == Decimal("425056.98")


def test_v2_matrixport_rows_unchanged_with_raw_observation_date() -> None:
    """Matrixport rows keep their USD-converted values with true as_of
    2026-09-03; date.source_last_observation is the last raw observation
    date (2026-09-17) of the finalized f6e87f8c capture."""
    doc = _load(HISTORICAL_V2_PATH)
    row = _find(doc, "capital.strategy.cutoff", "robuxio-crypto")
    assert row["as_of"] == "2026-09-03", row
    assert Decimal(row["expected"]) == Decimal("52055.38"), row
    assert Decimal(
        _find(doc, "twr.strategy", "robuxio-crypto", "ITD")["expected"]
    ) == Decimal("0.043017076433")
    assert Decimal(
        _find(doc, "mwr.strategy", "robuxio-crypto", "ITD")["expected"]
    ) == Decimal("0.058366399911")
    observed = _find(doc, "date.source_last_observation", "account-d2e5821e608e25f1")
    assert observed["expected"] == "2026-09-17", observed
