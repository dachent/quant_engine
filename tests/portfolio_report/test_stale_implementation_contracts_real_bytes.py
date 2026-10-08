"""F1 (GOOD real) and F9 on the retained smoke-8 bytes (ruling rulings-qc1i-stale-contracts).

The two sealed implementation contracts (sha256 34f05da3 and 94921c97) are read
against the real smoke-8 baseline catalog (governed provider ledger 8946efa9) through
the report's own wiring, ``_implementation_contracts``.  Nothing here is synthetic:
the contracts, the catalog, the account-live series and the admission receipt are
the retained run-root bytes.

Real provider and broker bytes are never committed (AGENTS.md, SEC-001), so this
module reads them in place, read-only, from the run root named by
``QE_N66_REAL_RUN_ROOT`` (default ``C:\\Dev\\q\\smoke8``) and skips, with a reason,
when that root or a pinned file is absent.  The synthetic shape tests live in
``test_stale_implementation_contracts.py`` and always run.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT))

from acceptance.validate import checks  # noqa: E402

RUN_ROOT = Path(os.environ.get("QE_N66_REAL_RUN_ROOT", r"C:\Dev\q\smoke8"))
REC_SHA = "34f05da3285ee49111b09ae82824ecb6c827fdd657586d9436f3ec7453ef3ff2"
ATTR_SHA = "94921c972a1bd97afa844b9ce7a2b25a843242b7bfa4dd692df3d1dc522a583c"
GOVERNED_LEDGER_PREFIX = "8946efa9"
CONTRACT_LEDGER_PREFIX = "23768b65"
PORTFOLIO_AS_OF = "2026-09-17"
GRIZZLY, AI, TT = "grizzlybears-vtma", "p123-ai-factor", "tt-portfolio"
STALE_STATUS = "NOT_READY_STALE_CONTRACT_LEDGER_VINTAGE"


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, TOOLS_ROOT / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reporter = _load("portfolio_report_real_bytes_test", "portfolio_report.py")


def _pinned_contract(spec: dict[str, Any], key: str, sha256: str) -> Path:
    pin = spec[key]
    path = Path(pin["path"])
    if not path.is_file():
        pytest.skip(f"retained contract {path} is not on this machine")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha256 == pin["sha256"]
    return path


@pytest.fixture(scope="module")
def real() -> dict[str, Any]:
    spec_path = RUN_ROOT / "in" / "spec.json"
    baseline_root = RUN_ROOT / "out" / "stages" / "baseline"
    ledger_root = RUN_ROOT / "in" / "ledger"
    if not (spec_path.is_file() and baseline_root.is_dir() and ledger_root.is_dir()):
        pytest.skip(f"retained smoke-8 run root {RUN_ROOT} is not on this machine")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    generations = sorted(
        path.name for path in (baseline_root / "generations").iterdir()
    )
    assert len(generations) == 1, generations
    catalog, _reference, _path = reporter._baseline(baseline_root, generations[0])
    catalog = reporter._calculation_catalog_through(catalog, PORTFOLIO_AS_OF)
    return {
        "catalog": catalog,
        "baseline_root": baseline_root,
        "ledger_root": ledger_root,
        "rec": _pinned_contract(spec, "implementation_reconciliation", REC_SHA),
        "attr": _pinned_contract(spec, "implementation_return_attribution", ATTR_SHA),
    }


@pytest.fixture(scope="module")
def rows(real: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    result: tuple[Any, Any, Any, Any] = reporter._implementation_contracts(
        real["catalog"],
        real["baseline_root"],
        real["ledger_root"],
        real["rec"],
        real["attr"],
    )
    return result


def _sealed(path: Path) -> dict[str, dict[str, Any]]:
    return {
        row["strategy_id"]: row
        for row in json.loads(path.read_text(encoding="utf-8"))["strategies"]
    }


def test_the_governed_ledger_is_not_the_one_the_contracts_pin(
    real: dict[str, Any],
) -> None:
    governed = reporter._baseline_provider_ledger_identity(real["catalog"])
    assert governed["provider-ledger-catalog"].startswith(GOVERNED_LEDGER_PREFIX)
    for key in ("rec", "attr"):
        sealed = json.loads(real[key].read_text(encoding="utf-8"))
        pins = [
            artifact["sha256"]
            for artifact in sealed["source_artifacts"]
            if artifact.get("artifact_id") == "normalized/provider-ledger.json"
            or artifact.get("source_id") == "provider-ledger"
        ]
        assert [pin[:8] for pin in pins] == [CONTRACT_LEDGER_PREFIX]


def test_f1_real_contracts_publish_three_stale_rows_and_keep_the_rest_byte_identical(
    real: dict[str, Any], rows: tuple[Any, Any, Any, Any]
) -> None:
    rec_rows, rec_ref, attr_rows, attr_ref = rows
    sealed_rec, sealed_attr = _sealed(real["rec"]), _sealed(real["attr"])

    assert rec_ref["stale_strategy_ids"] == [GRIZZLY, TT]
    assert attr_ref["stale_strategy_ids"] == [GRIZZLY]
    assert rec_ref["sha256"] == REC_SHA and attr_ref["sha256"] == ATTR_SHA

    for label, published in (("rec", rec_rows[GRIZZLY]), ("attr", attr_rows[GRIZZLY])):
        assert published["status"] == STALE_STATUS, label
        assert published["governed_account_return"] == pytest.approx(
            0.0027196, abs=1e-12
        )
        assert published["superseded_contract_account_return"] == pytest.approx(
            0.0003508, abs=1e-10
        )
        assert published["governed_observations"] == 194
        assert (published["window_start"], published["window_end"]) == (
            "2026-01-05",
            "2026-07-17",
        )
        assert published["contract_provider_ledger_sha256"].startswith(
            CONTRACT_LEDGER_PREFIX
        )
        assert published["governed_provider_ledger_sha256"].startswith(
            GOVERNED_LEDGER_PREFIX
        )
    tt = rec_rows[TT]
    assert tt["status"] == STALE_STATUS and tt["contract_kind"] == "RECONCILIATION"
    assert tt["governed_account_return"] == pytest.approx(0.0035765117, abs=1e-10)
    assert tt["superseded_contract_account_return"] == pytest.approx(
        0.0039681303, abs=1e-10
    )
    assert tt["governed_observations"] == 248
    assert (tt["window_start"], tt["window_end"]) == ("2025-11-25", "2026-07-30")

    assert rec_rows[AI] == sealed_rec[AI]
    assert attr_rows[AI] == sealed_attr[AI]
    assert attr_rows[TT] == sealed_attr[TT]

    for stale in (rec_rows[GRIZZLY], attr_rows[GRIZZLY], tt):
        for forbidden in ("bridge", "return_reconciliation", "account_leg"):
            assert forbidden not in stale


def test_f1_real_strategy_status_follows_which_contract_is_stale(
    rows: tuple[Any, Any, Any, Any],
) -> None:
    rec_rows, _rec_ref, attr_rows, _attr_ref = rows
    status = {
        sid: reporter._implementation_status(
            "ACCEPTED_ACCOUNT_LIVE", rec_rows[sid], attr_rows[sid]
        )
        for sid in (GRIZZLY, AI, TT)
    }
    assert status[GRIZZLY] == STALE_STATUS
    assert status[AI] == "VERIFIED_CAUSAL_RETURN_ATTRIBUTION"
    assert status[TT] == "PARTIAL_CAUSAL_ATTRIBUTION_RECONCILIATION_NOT_READY"


def test_f2_real_same_ledger_with_a_different_figure_is_still_fatal(
    real: dict[str, Any], tmp_path: Path
) -> None:
    sealed = json.loads(real["rec"].read_text(encoding="utf-8"))
    governed = reporter._baseline_provider_ledger_identity(real["catalog"])[
        "provider-ledger-catalog"
    ]
    for artifact in sealed["source_artifacts"]:
        if artifact.get("artifact_id") == "normalized/provider-ledger.json":
            artifact["sha256"] = governed
    pinned = tmp_path / "rec-pinned-to-governed-ledger.json"
    pinned.write_bytes(json.dumps(sealed).encode("utf-8"))

    with pytest.raises(
        reporter.ReportValidationError, match="differs from the governed"
    ):
        reporter._implementation_contracts(
            real["catalog"], real["baseline_root"], real["ledger_root"], pinned, None
        )


def _report_with(
    real: dict[str, Any], rec_rows: dict[str, Any], attr_rows: dict[str, Any]
) -> checks.Context:
    class _Generation:
        panel: dict[str, Any] = {}

    generation = _Generation()
    generation.report = {  # type: ignore[attr-defined]
        "strategy_returns": real["catalog"]["strategy_returns"],
        "strategy_summary": [
            {
                "strategy_id": sid,
                "implementation_reconciliation": rec_rows[sid],
                "implementation_return_attribution": attr_rows[sid],
            }
            for sid in (GRIZZLY, AI, TT)
        ],
    }
    return checks.Context(
        gen=generation,  # type: ignore[arg-type]
        cutoff=date.fromisoformat(PORTFOLIO_AS_OF),
        cutoff_source="real",
    )


def test_f9_real_validator_reports_exactly_three_not_ready_findings_and_no_blocker(
    real: dict[str, Any], rows: tuple[Any, Any, Any, Any]
) -> None:
    rec_rows, _rec_ref, attr_rows, _attr_ref = rows

    findings = list(
        checks.check_implementation_account_legs(
            _report_with(real, rec_rows, attr_rows)
        )
    )

    assert Counter(finding.klass for finding in findings) == {checks.NOT_READY: 3}
    assert sorted((finding.entity, finding.json_path) for finding in findings) == [
        (GRIZZLY, "$.strategy_summary[0].implementation_reconciliation"),
        (GRIZZLY, "$.strategy_summary[0].implementation_return_attribution"),
        (TT, "$.strategy_summary[2].implementation_reconciliation"),
    ]
    assert {finding.defect_ids for finding in findings} == {("N66-R",)}
    assert not [f for f in findings if f.klass in checks.BLOCKERS]


def test_f6_real_publishing_the_sealed_rows_unchanged_is_three_blockers(
    real: dict[str, Any],
) -> None:
    sealed_rec, sealed_attr = _sealed(real["rec"]), _sealed(real["attr"])

    findings = list(
        checks.check_implementation_account_legs(
            _report_with(real, sealed_rec, sealed_attr)
        )
    )

    stale = {(GRIZZLY, "rec"), (GRIZZLY, "attr"), (TT, "rec")}
    assert {
        (f.entity, "rec" if f.json_path.endswith("reconciliation") else "attr")
        for f in findings
        if f.klass == checks.BLOCKER_INVARIANT
    } == stale
    assert len(findings) == 3
