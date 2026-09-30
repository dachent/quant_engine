"""Schema and cross-derivation checks for acceptance/expected-baseline.json.

Standard library only (plan 4.5A oracle rule: acceptance/oracle code never
imports repo production code and never reads generation outputs). This test
reads only the committed baseline file itself.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "acceptance" / "expected-baseline.json"

TOLERANCE_UNITS = {"USD", "ratio"}
EXACT_UNITS = {"count", "date", "list", "status"}


def _load() -> dict[str, Any]:
    raw = BASELINE_PATH.read_bytes()
    assert b"\r\n" not in raw, "expected-baseline.json must use LF line endings"
    value: dict[str, Any] = json.loads(raw.decode("utf-8"))
    return value


def test_file_is_utf8_lf_json() -> None:
    assert BASELINE_PATH.is_file(), f"missing {BASELINE_PATH}"
    _load()  # decodes cleanly as UTF-8 JSON with no CRLF


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


def _is_data_root_relative(path: str) -> bool:
    # PureWindowsPath(...).anchor is non-empty for both "C:\..." and the
    # driveless rooted "\Dev..." form; PurePosixPath covers "/...".
    if PureWindowsPath(path).anchor or PurePosixPath(path).anchor:
        return False
    return path.startswith(ALLOWED_AUTHORITY_POINTER_ROOTS)


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
    doc = _load()
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
    doc = _load()
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
    doc = _load()
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
