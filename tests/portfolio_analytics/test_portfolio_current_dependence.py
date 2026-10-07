from __future__ import annotations

import json
import importlib.util
import csv
from decimal import Decimal
import hashlib
import io
from pathlib import Path
import sys
from types import SimpleNamespace
from collections.abc import Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "portfolio_current_dependence.py"
SPEC = importlib.util.spec_from_file_location(
    "portfolio_current_dependence", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load current dependence builder")
current = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = current
SPEC.loader.exec_module(current)

# The unmocked dependence fixture deliberately manufactures different segment
# bytes from the production history authority.  Validate that independent
# synthetic contract against its explicit copied schema, rather than asking the
# production #319 historical-authority gate to accept an invented generation.
# The production validator retains responsibility for accepted-authority lineage
# in its own contract tests; this adapter keeps the consumer fixture's expected
# values independent of the producer output it is testing.
_PRODUCTION_FINANCIAL_MODULE = current._financial_module()


def _synthetic_financial_module() -> SimpleNamespace:
    def load_contract(*, contract_path: Path) -> dict[str, Any]:
        content = contract_path.read_bytes()
        contract = json.loads(content.decode("utf-8"))
        locator = contract.get("$schema")
        if locator != "../schemas/portfolio/financial-mvp-source-contract.schema.json":
            raise ValueError("synthetic financial contract schema locator differs")
        schema = json.loads(
            (contract_path.parent / locator).resolve().read_text(encoding="utf-8")
        )
        errors = _PRODUCTION_FINANCIAL_MODULE.schema_errors(schema, contract, schema)
        if errors:
            raise ValueError("synthetic financial contract schema validation failed")
        return contract

    return SimpleNamespace(
        census_digest=_PRODUCTION_FINANCIAL_MODULE.census_digest,
        contract_digest=_PRODUCTION_FINANCIAL_MODULE.contract_digest,
        load_contract=load_contract,
        schema_errors=_PRODUCTION_FINANCIAL_MODULE.schema_errors,
    )


_SYNTHETIC_LAYERED_HISTORY_SEGMENT_FIELDS = (
    "strategy_id",
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "strategy_version",
    "segment_id",
    "source_id",
    "source_generation_id",
    "mode_admission",
    "return_basis",
    "return_type",
    "calendar",
    "frequency",
)
_SYNTHETIC_LAYERED_HISTORY_RETURN_FIELDS = (
    "series_id",
    "date",
    "return",
    "evidence_layer",
    "strategy_version",
)


def _rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for index, (strategy_id, series_id) in enumerate(
        (("alpha", "Alpha"), ("beta", "Beta"), ("gamma", "Gamma")), start=1
    ):
        for day, value in (("2026-01-01", "0.01"), ("2026-01-02", "-0.02")):
            rows.append(
                {
                    "strategy_id": strategy_id,
                    "series_id": series_id,
                    "date": day,
                    "return": value,
                    "evidence_layer": "ACCOUNT_LIVE",
                    "strategy_version": f"version-{strategy_id}",
                    "segment_id": f"segment-{strategy_id}",
                    "source_id": f"source-{strategy_id}",
                    "source_generation_id": str(index) * 64,
                    "mode_admission": "INCLUDE_SELECTED_LIVE",
                    "return_basis": "TWR_NET",
                    "return_type": "TOTAL",
                    "calendar": "CALENDAR_DAY",
                    "frequency": "DAILY",
                }
            )
    return rows


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    evaluated_on: current.date = current.date(2026, 1, 3),
    epoch_hpr_receipts: list[dict[str, Any]] | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    rows = _rows()
    identities = []
    for strategy_id in ("alpha", "beta", "gamma"):
        selected = [row for row in rows if row["strategy_id"] == strategy_id]
        first = selected[0]
        identities.append(
            {
                "calendar": first["calendar"],
                "evidence_layer": first["evidence_layer"],
                "frequency": first["frequency"],
                "mode_admission": first["mode_admission"],
                "return_basis": first["return_basis"],
                "return_type": first["return_type"],
                "segment_id": first["segment_id"],
                "source_generation_id": first["source_generation_id"],
                "source_generation_sha256": "f" * 64,
                "source_id": first["source_id"],
                "strategy_id": first["strategy_id"],
                "strategy_version": first["strategy_version"],
                **current._return_identity(selected),
                "canonicalization": "CORRELATION_FIELDS_V2_CANONICAL_CSV_V1",
            }
        )
    contract = {
        "strategies": [
            {
                "strategy_id": strategy_id,
                "product_requirement": "REQUIRED",
                "history_series_id": series_id,
                "live_implementation_identity": {
                    "version_ref": f"account-live:{strategy_id}"
                },
                "outcome_dispositions": {"DEPENDENCE": "REQUIRED"},
            }
            for strategy_id, series_id in (
                ("alpha", "Alpha"),
                ("beta", "Beta"),
                ("gamma", "Gamma"),
            )
        ]
        + [
            {
                "strategy_id": "deprecated",
                "product_requirement": "EXCLUDED",
                "history_series_id": "Deprecated",
                "live_implementation_identity": {"version_ref": None},
                "outcome_dispositions": {"DEPENDENCE": "NOT_APPLICABLE"},
            }
        ],
        "current_census": {
            "account_sleeves": [
                {
                    "current_strategy_id": strategy_id,
                    "provider_id": provider,
                    "role": "ACTIVE_STRATEGY",
                }
                for strategy_id, provider in (
                    ("alpha", "P1"),
                    ("beta", "P1"),
                    ("gamma", "P2"),
                )
            ]
        },
        "history_corrections": {"materialized_segment_identities": identities},
        "source_segments": [
            {
                "source_id": f"source-{strategy_id}",
                "freshness": {"max_age_days": 2, "publication_lag_days": 0},
            }
            for strategy_id in ("alpha", "beta", "gamma")
        ],
    }
    policy = {
        "history_authority": {
            "generation_id": "a" * 64,
            "receipt_sha256": "b" * 64,
        },
        "allocation_authority": {
            "as_of_date": "2026-01-02",
            "catalog_sha256": "c" * 64,
            "generation_id": "d" * 64,
            "manifest_sha256": "e" * 64,
        },
        "series": [
            {
                "strategy_id": strategy_id,
                "series_id": series_id,
                "allocation_weight": weight,
                "adverse_loss_fraction": loss,
            }
            for strategy_id, series_id, weight, loss in (
                ("alpha", "Alpha", "0.2", "0.3"),
                ("beta", "Beta", "0.3", "0.4"),
                ("gamma", "Gamma", "0.4", "0.5"),
            )
        ],
        "conceptual_priors": [
            {
                "left_strategy_id": left,
                "right_strategy_id": right,
                "value": value,
                "evidence_note": "accepted synthetic prior",
            }
            for left, right, value in (
                ("alpha", "beta", "0.4"),
                ("alpha", "gamma", "0.2"),
                ("beta", "gamma", "0.3"),
            )
        ],
        "calculation_parameters": {
            "correlation_one_tolerance": "0.000000000001",
            "eigenvalue_floor": "0.000001",
            "full_observed_weight_observations": 500,
            "maximum_observed_weight": "0.8",
            "minimum_downside_observations": 1,
            "minimum_observations": 2,
            "minimum_rolling_windows": 1,
            "rolling_window": 2,
            "stress_cap": "0.95",
            "stress_uplift": "0.15",
            "tail_quantile": "0.1",
        },
    }
    authority_ids = {
        "contract_raw_sha256": "1" * 64,
        "contract_semantic_digest": "2" * 64,
        "census_sha256": "3" * 64,
        "policy_sha256": "4" * 64,
    }
    monkeypatch.setattr(
        current,
        "_load_authorities",
        lambda _contract, _policy: (contract, policy, authority_ids),
    )
    history = tmp_path / "history"
    history.mkdir(parents=True)
    (history / "segments.csv").write_bytes(
        current._csv_content(current.SEGMENT_FIELDS, rows)
    )
    projected = [{field: row[field] for field in current.RETURN_FIELDS} for row in rows]
    coverage_content = canonical_json_bytes(
        {"epoch_hpr_receipts": epoch_hpr_receipts or []}
    )
    (history / "coverage.json").write_bytes(coverage_content)
    monkeypatch.setattr(
        current,
        "_verified_history",
        lambda *_args, **_kwargs: (
            projected,
            identities,
            current.date(2026, 1, 2),
            {"generation_id": "a" * 64},
            coverage_content,
        ),
    )
    monkeypatch.setattr(
        current,
        "_verified_allocations",
        lambda *_args, **_kwargs: (
            {"alpha": "0.2", "beta": "0.3", "gamma": "0.4"},
            policy["allocation_authority"],
        ),
    )
    built = current.build_source(
        contract_path=tmp_path / "contract.json",
        policy_path=tmp_path / "policy.json",
        history_generation_root=history,
        allocation_generation_root=tmp_path / "allocation",
        evaluated_on=evaluated_on,
    )
    receipt = built["receipt"]
    assert isinstance(receipt, dict)
    source = tmp_path / str(receipt["bundle_id"])
    source.mkdir()
    for name in (
        "dependence-source.json",
        "history-coverage.json",
        "returns.csv",
        "segments.csv",
        "spec.json",
    ):
        value = built[name]
        assert isinstance(value, bytes)
        (source / name).write_bytes(value)
    return source, contract, policy


def _rewrite_bundle(source: Path, receipt: dict[str, Any]) -> Path:
    identity = dict(receipt)
    identity.pop("bundle_id", None)
    bundle_id = current._sha256(canonical_json_bytes(identity))
    rewritten = {"bundle_id": bundle_id, **identity}
    destination = source.parent / bundle_id
    destination.mkdir()
    for item in source.iterdir():
        if item.name != "dependence-source.json":
            (destination / item.name).write_bytes(item.read_bytes())
    (destination / "dependence-source.json").write_bytes(
        canonical_json_bytes(rewritten)
    )
    return destination


def test_dependence_spec_is_dated_at_the_data_cutoff_and_the_receipt_keeps_the_run_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-3: the spec feeds the correlation generation, which downstream reads as data.

    The data is complete through 2026-01-02 and the run executes on 2026-01-03.  The
    correlation spec (and so the correlation and weekly artifacts built from it) is
    dated at the data; the execution date stays in the receipt as ``evaluated_on``.
    """

    source, _contract, _policy = _fixture(tmp_path, monkeypatch)

    spec = json.loads((source / "spec.json").read_text(encoding="utf-8"))
    receipt = json.loads((source / "dependence-source.json").read_text("utf-8"))

    assert spec["as_of_date"] == "2026-01-02"
    assert receipt["complete_through"] == "2026-01-02"
    assert receipt["evaluated_on"] == "2026-01-03"
    assert receipt["calculation_age_days"] == 1


def test_packaged_current_dependence_is_exact_and_self_verifying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _contract, _policy = _fixture(tmp_path, monkeypatch)
    result = current.verify_packaged_source(
        source,
        contract_path=tmp_path / "contract.json",
        policy_path=tmp_path / "policy.json",
    )
    assert result == {
        "bundle_id": source.name,
        "complete_through": "2026-01-02",
        "epoch_hpr_receipts": [],
        "pair_count": 3,
        "pair_mode_count": 6,
        "verification": "VALID",
    }
    receipt = json.loads((source / "dependence-source.json").read_text())
    assert receipt["required_strategy_ids"] == ["alpha", "beta", "gamma"]
    assert receipt["excluded_product_strategy_ids"] == ["deprecated"]
    assert receipt["source_age_status"] == "CURRENT_WITHIN_AGE_THRESHOLD"
    assert receipt["warning_after_days"] == 2
    assert receipt["age_evidence"] == {
        "calculation_age_days": 1,
        "complete_through": "2026-01-02",
        "evaluated_on": "2026-01-03",
        "limiting_inputs": [
            {
                "observed_end": "2026-01-02",
                "publication_lag_days": 0,
                "segment_id": f"segment-{strategy_id}",
                "series_id": series_id,
                "source_generation_id": str(index) * 64,
                "source_id": f"source-{strategy_id}",
                "strategy_id": strategy_id,
                "warning_after_days": 2,
            }
            for index, (strategy_id, series_id) in enumerate(
                (("alpha", "Alpha"), ("beta", "Beta"), ("gamma", "Gamma")),
                start=1,
            )
        ],
        "status": "CURRENT_WITHIN_AGE_THRESHOLD",
        "warning_after_days": 2,
    }


def test_packaged_dependence_projects_only_hash_bound_epoch_hpr_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report-facing receipt projection comes from the packaged byte,
    rather than a mutable producer-side coverage sibling."""

    epoch_hpr_receipts = [
        {
            "evidence_layer": "ACCOUNT_LIVE",
            "receipt": {"epoch_cumulative_return": "-0.0123875290080184841010757599"},
            "source_generation_id": "a" * 64,
            "source_id": "matrixport-account-generation",
            "strategy_id": "robuxio-crypto",
        }
    ]
    source, _contract, _policy = _fixture(
        tmp_path,
        monkeypatch,
        epoch_hpr_receipts=epoch_hpr_receipts,
    )
    producer_coverage = (tmp_path / "history" / "coverage.json").read_bytes()

    verified = current.verify_packaged_source(
        source,
        contract_path=tmp_path / "contract.json",
        policy_path=tmp_path / "policy.json",
    )
    assert verified["epoch_hpr_receipts"] == epoch_hpr_receipts

    # Mutating only the packaged copy leaves the producer-side receipt byte
    # untouched and must fail against the dependence receipt's coverage hash.
    (source / "history-coverage.json").write_bytes(
        canonical_json_bytes({"epoch_hpr_receipts": []})
    )
    assert (tmp_path / "history" / "coverage.json").read_bytes() == producer_coverage
    with pytest.raises(
        current.CurrentDependenceError, match="history coverage differs"
    ):
        current.verify_packaged_source(
            source,
            contract_path=tmp_path / "contract.json",
            policy_path=tmp_path / "policy.json",
        )


def test_dependence_age_warning_binds_threshold_limiters_and_rejects_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _contract, _policy = _fixture(
        tmp_path,
        monkeypatch,
        evaluated_on=current.date(2026, 1, 5),
    )
    receipt = json.loads((source / "dependence-source.json").read_text())
    assert receipt["calculation_age_days"] == 3
    assert receipt["warning_after_days"] == 2
    assert receipt["source_age_status"] == "WARN_COMPLETE_DATA_AGE"
    assert len(receipt["age_evidence"]["limiting_inputs"]) == 3

    receipt["age_evidence"]["limiting_inputs"][0]["warning_after_days"] = 99
    rewritten = _rewrite_bundle(source, receipt)
    with pytest.raises(
        current.CurrentDependenceError, match="receipt authority differs"
    ):
        current.verify_packaged_source(
            rewritten,
            contract_path=tmp_path / "contract.json",
            policy_path=tmp_path / "policy.json",
        )


def test_allocation_snapshot_after_evaluation_date_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_id = "d" * 64
    root = tmp_path / generation_id
    root.mkdir()
    manifest_content = b"{}"
    catalog_content = canonical_json_bytes({"allocations": []})
    (root / "generation.json").write_bytes(manifest_content)
    (root / "portfolio-baseline.json").write_bytes(catalog_content)
    manifest = SimpleNamespace(
        artifacts=(),
        as_of_date=current.date(2026, 1, 2),
        generation_id=generation_id,
        verification=SimpleNamespace(value="VERIFIED"),
    )
    monkeypatch.setattr(
        current.GenerationManifest,
        "from_json_bytes",
        lambda _content: manifest,
    )
    policy = {
        "allocation_authority": {
            "as_of_date": "2026-01-02",
            "catalog_sha256": current._sha256(catalog_content),
            "generation_id": generation_id,
            "manifest_sha256": current._sha256(manifest_content),
        }
    }
    with pytest.raises(
        current.CurrentDependenceError,
        match="allocation manifest exceeds the evaluation date",
    ):
        current._verified_allocations(
            root,
            policy,
            ("alpha",),
            current.date(2026, 1, 1),
        )


def test_packaged_dependence_rejects_segment_and_mode_authority_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _contract, _policy = _fixture(tmp_path, monkeypatch)
    segment_rows = current._csv_rows(
        (source / "segments.csv").read_bytes(),
        current.SEGMENT_FIELDS,
        "segments",
    )
    segment_rows[0]["return"] = "0.99"
    (source / "segments.csv").write_bytes(
        current._csv_content(current.SEGMENT_FIELDS, segment_rows)
    )
    with pytest.raises(current.CurrentDependenceError, match="segment differs"):
        current.verify_packaged_source(
            source,
            contract_path=tmp_path / "contract.json",
            policy_path=tmp_path / "policy.json",
        )

    source, _contract, _policy = _fixture(tmp_path / "second", monkeypatch)
    spec = json.loads((source / "spec.json").read_text())
    spec["evidence_mode_series"][0]["strategy_version"] = "invented-backtest"
    spec_content = canonical_json_bytes(spec)
    (source / "spec.json").write_bytes(spec_content)
    receipt = json.loads((source / "dependence-source.json").read_text())
    receipt["spec_sha256"] = current._sha256(spec_content)
    receipt["spec_size_bytes"] = len(spec_content)
    rewritten = _rewrite_bundle(source, receipt)
    with pytest.raises(current.CurrentDependenceError, match="spec authority differs"):
        current.verify_packaged_source(
            rewritten,
            contract_path=tmp_path / "contract.json",
            policy_path=tmp_path / "policy.json",
        )


def test_packaged_dependence_rejects_provider_mapping_and_perimeter_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, contract, _policy = _fixture(tmp_path, monkeypatch)
    spec = json.loads((source / "spec.json").read_text())
    spec["implementation_mappings"][0]["providers"] = ["FORGED"]
    spec_content = canonical_json_bytes(spec)
    (source / "spec.json").write_bytes(spec_content)
    receipt = json.loads((source / "dependence-source.json").read_text())
    receipt["spec_sha256"] = current._sha256(spec_content)
    receipt["spec_size_bytes"] = len(spec_content)
    rewritten = _rewrite_bundle(source, receipt)
    with pytest.raises(current.CurrentDependenceError, match="spec authority differs"):
        current.verify_packaged_source(
            rewritten,
            contract_path=tmp_path / "contract.json",
            policy_path=tmp_path / "policy.json",
        )

    source, contract, _policy = _fixture(tmp_path / "second", monkeypatch)
    contract["strategies"][0]["product_requirement"] = "EXCLUDED"
    with pytest.raises(current.CurrentDependenceError, match="perimeter differs"):
        current.verify_packaged_source(
            source,
            contract_path=tmp_path / "contract.json",
            policy_path=tmp_path / "policy.json",
        )


def test_current_policy_encodes_owner_approved_seven_strategy_perimeter() -> None:
    policy = json.loads(
        Path("contracts/portfolio-dependence-policy-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert [row["strategy_id"] for row in policy["series"]] == [
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "tt-portfolio",
        "volatis-strategy",
        "grizzlybears-vtma",
        "robuxio-crypto",
    ]
    assert len(policy["conceptual_priors"]) == 21
    assert (
        sum(current.Decimal(row["allocation_weight"]) for row in policy["series"]) < 1
    )


def _canonical_csv(fields: tuple[str, ...], rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows({field: row[field] for field in fields} for row in rows)
    return buffer.getvalue().encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _unmocked_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    return_shift: str = "0",
    coverage_strategies: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one synthetic history and allocation authority through the real builders.

    ``coverage_strategies`` are history coverage rows in the shape the layered
    history builder emits (``portfolio_target_series``: ``series_id`` plus
    ``segments``); the packaged coverage file is hash-bound to them.
    """

    authority_root = tmp_path / "authority"
    contracts = authority_root / "contracts"
    schemas = authority_root / "schemas" / "portfolio"
    contracts.mkdir(parents=True)
    schemas.mkdir(parents=True)
    for name in (
        "financial-mvp-source-contract.schema.json",
        "dependence-policy.schema.json",
        "layered-history-generation.schema.json",
    ):
        (schemas / name).write_bytes(
            (REPOSITORY_ROOT / "schemas" / "portfolio" / name).read_bytes()
        )

    contract = json.loads(
        (
            REPOSITORY_ROOT / "contracts" / "portfolio-financial-mvp-source-v1.json"
        ).read_text(encoding="utf-8")
    )
    policy = json.loads(
        (
            REPOSITORY_ROOT / "contracts" / "portfolio-dependence-policy-v1.json"
        ).read_text(encoding="utf-8")
    )
    strategy_series = {
        row["strategy_id"]: row["history_series_id"] for row in contract["strategies"]
    }
    base_identities = contract["history_corrections"]["materialized_segment_identities"]
    rows: list[dict[str, str]] = []
    identities: list[dict[str, object]] = []
    for index, base in enumerate(base_identities, start=1):
        selected = []
        for day, value in (
            ("2026-01-01", f"0.00{index}"),
            ("2026-01-02", f"-0.00{index}"),
        ):
            row = {
                "strategy_id": base["strategy_id"],
                "series_id": strategy_series[base["strategy_id"]],
                "date": day,
                "return": str(Decimal(value) + Decimal(return_shift)),
                "evidence_layer": base["evidence_layer"],
                "strategy_version": base["strategy_version"],
                "segment_id": base["segment_id"],
                "source_id": base["source_id"],
                "source_generation_id": base["source_generation_id"],
                "mode_admission": base["mode_admission"],
                "return_basis": base["return_basis"],
                "return_type": base["return_type"],
                "calendar": base["calendar"],
                "frequency": base["frequency"],
            }
            rows.append(row)
            selected.append(row)
        return_rows = [
            {field: row[field] for field in _SYNTHETIC_LAYERED_HISTORY_RETURN_FIELDS}
            for row in selected
        ]
        identities.append(
            {
                "calendar": base["calendar"],
                "canonicalization": "CORRELATION_FIELDS_V2_CANONICAL_CSV_V1",
                "evidence_layer": base["evidence_layer"],
                "frequency": base["frequency"],
                "mode_admission": base["mode_admission"],
                "observed_end": "2026-01-02",
                "observed_start": "2026-01-01",
                "return_basis": base["return_basis"],
                "return_observations": 2,
                "return_type": base["return_type"],
                "segment_id": base["segment_id"],
                "sha256": _sha256(
                    _canonical_csv(
                        _SYNTHETIC_LAYERED_HISTORY_RETURN_FIELDS, return_rows
                    )
                ),
                "source_generation_id": base["source_generation_id"],
                "source_generation_sha256": base["source_generation_sha256"],
                "source_id": base["source_id"],
                "strategy_id": base["strategy_id"],
                "strategy_version": base["strategy_version"],
            }
        )
    contract["history_corrections"]["materialized_segment_identities"] = sorted(
        identities, key=lambda row: str(row["segment_id"])
    )
    selected_by_strategy = {
        str(row["strategy_id"]): row
        for row in contract["history_corrections"]["materialized_segment_identities"]
        if row["evidence_layer"] == "ACCOUNT_LIVE"
        and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    }
    for strategy in contract["strategies"]:
        live_identity = strategy["live_implementation_identity"]
        if live_identity["status"] != "EVIDENCED":
            continue
        selected = selected_by_strategy[strategy["strategy_id"]]
        strategy["live_implementation_identity"] = {
            "status": "EVIDENCED",
            "version_ref": f"account-live-segment:{selected['sha256']}",
            "generation_id": selected["source_generation_id"],
            "receipt_sha256": selected["source_generation_sha256"],
        }

    history_files: dict[str, bytes] = {}
    history_files["segments.csv"] = _canonical_csv(
        _SYNTHETIC_LAYERED_HISTORY_SEGMENT_FIELDS, rows
    )
    live_rows = [
        {field: row[field] for field in _SYNTHETIC_LAYERED_HISTORY_RETURN_FIELDS}
        for row in rows
        if row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    ]
    live_rows.sort(key=lambda row: (row["series_id"], row["date"]))
    history_files["live-only.csv"] = _canonical_csv(
        _SYNTHETIC_LAYERED_HISTORY_RETURN_FIELDS, live_rows
    )
    history_files["live-plus-backtest.csv"] = _canonical_csv(
        _SYNTHETIC_LAYERED_HISTORY_RETURN_FIELDS, live_rows
    )
    history_files["returns.csv"] = history_files["live-plus-backtest.csv"]
    history_files["excluded.csv"] = _canonical_csv(
        _SYNTHETIC_LAYERED_HISTORY_RETURN_FIELDS, []
    )
    history_files["coverage.json"] = canonical_json_bytes(
        {"fixture": "synthetic-current-dependence"}
        if coverage_strategies is None
        else {
            "fixture": "synthetic-current-dependence",
            "strategies": [dict(row) for row in coverage_strategies],
        }
    )
    output_authority = {
        name: {"sha256": _sha256(content), "size_bytes": len(content)}
        for name, content in history_files.items()
    }
    output_row_counts = {
        "segments.csv": len(rows),
        "excluded.csv": 0,
        "returns.csv": len(live_rows),
        "live-only.csv": len(live_rows),
        "live-plus-backtest.csv": len(live_rows),
        "coverage.json": 1,
    }
    output_schemas = {
        "segments.csv": ("portfolio-layered-history-segments", "1"),
        "excluded.csv": ("portfolio-layered-history-excluded-targets", "1"),
        "returns.csv": ("portfolio-layered-strategy-returns", "2"),
        "live-only.csv": ("portfolio-layered-strategy-returns", "2"),
        "live-plus-backtest.csv": ("portfolio-layered-strategy-returns", "2"),
        "coverage.json": ("portfolio-layered-history-coverage", "1"),
    }
    receipt_outputs = {
        name: {
            "artifact_id": f"layered-history/{name}",
            "row_count": output_row_counts[name],
            "schema_name": output_schemas[name][0],
            "schema_version": output_schemas[name][1],
            **output_authority[name],
        }
        for name in history_files
    }
    history_manifest_without_id = {
        "$schema": "https://quant-engine.local/schemas/portfolio/layered-history-generation.schema.json",
        "as_of_date": "2026-01-02",
        "builder": "portfolio-target-series/layered-history-1",
        # Literal policy-pinned producer version from the accepted 31030553...
        # history receipt.  Do not derive this from the consumer constant.
        "builder_version": "1.14.0",
        "inputs": [
            {
                "artifact_id": "synthetic/current-dependence-input",
                "artifact_role": "GENERATION_ARTIFACT",
                "as_of_date": "2026-01-02",
                "generation_id": "0" * 64,
                "input_id": "synthetic-current-dependence-input",
                "sha256": "1" * 64,
                "size_bytes": 1,
            }
        ],
        "mode_status": {
            "LIVE_ONLY": {
                "required_not_ready_strategy_ids": [],
                "status": "AVAILABLE",
            },
            "LIVE_PLUS_BACKTEST": {
                "required_not_ready_strategy_ids": [],
                "status": "AVAILABLE",
            },
        },
        "outputs": receipt_outputs,
        "product": {
            "contract_sha256": _sha256(canonical_json_bytes(contract)),
            "history_policy_id": "PORT-HISTORY-TRUTH-1",
            "history_policy_version": 1,
            "product_id": "FINANCIAL_MVP",
            "product_version": "1",
        },
        "repository_sha": "2" * 40,
        "schema_version": 1,
        "source_manifests": {
            "matrixport": "{}",
            "portfolio123": "{}",
            "provider-ledger": "{}",
            "quantbase": "{}",
        },
        "source_pointers": {
            "matrixport": "{}",
            "portfolio123": "{}",
            "provider-ledger": "{}",
            "quantbase": "{}",
        },
        "tool_sha256": "3" * 64,
        "verification": "VERIFIED",
    }
    history_generation_id = _sha256(canonical_json_bytes(history_manifest_without_id))
    history_manifest = {
        **history_manifest_without_id,
        "generation_id": history_generation_id,
    }
    history_schema = json.loads(
        (schemas / "layered-history-generation.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(history_schema, format_checker=FormatChecker()).validate(
        history_manifest
    )
    history_content = canonical_json_bytes(history_manifest)
    history_root = authority_root / "history" / history_generation_id
    history_root.mkdir(parents=True)
    for name, content in history_files.items():
        (history_root / name).write_bytes(content)
    (history_root / "generation.json").write_bytes(history_content)
    policy["history_authority"] = {
        "generation_id": history_generation_id,
        "receipt_sha256": _sha256(history_content),
        "outputs": output_authority,
    }

    catalog = {
        "allocations": [
            {
                "scope": "SYSTEMATIC",
                "strategy_id": row["strategy_id"],
                "systematic_weight": row["allocation_weight"],
            }
            for row in policy["series"]
        ]
    }
    catalog_content = canonical_json_bytes(catalog)
    as_of = current.date(2026, 1, 2)
    allocation_manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("synthetic-current-dependence", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="synthetic systematic allocation perimeter",
            fact_types=("SYSTEMATIC_ALLOCATION",),
            observed_start=as_of,
            observed_end=as_of,
        ),
        artifacts=(
            SourceArtifact(
                artifact_id="normalized/portfolio-baseline.json",
                as_of_date=as_of,
                schema_name="portfolio-baseline-catalog",
                schema_version="1",
                sha256=_sha256(catalog_content),
                size_bytes=len(catalog_content),
            ),
        ),
        verification=VerificationStatus.VERIFIED,
    )
    allocation_content = allocation_manifest.to_json_bytes()
    allocation_root = authority_root / "allocations" / allocation_manifest.generation_id
    allocation_root.mkdir(parents=True)
    (allocation_root / "generation.json").write_bytes(allocation_content)
    (allocation_root / "portfolio-baseline.json").write_bytes(catalog_content)
    allocation_identity = {
        "as_of_date": "2026-01-02",
        "catalog_sha256": _sha256(catalog_content),
        "generation_id": allocation_manifest.generation_id,
        "manifest_sha256": _sha256(allocation_content),
    }
    policy["allocation_authority"] = allocation_identity

    contract_path = contracts / "portfolio-financial-mvp-source-v1.json"
    policy_path = contracts / "portfolio-dependence-policy-v1.json"
    contract_path.write_bytes(canonical_json_bytes(contract))
    policy_path.write_bytes(canonical_json_bytes(policy))
    monkeypatch.setattr(current, "_financial_module", _synthetic_financial_module)
    return {
        "allocation_identity": allocation_identity,
        "allocation_root": allocation_root,
        "contract_path": contract_path,
        "history_generation_id": history_generation_id,
        "history_root": history_root,
        "policy_path": policy_path,
    }


def test_current_dependence_builds_and_verifies_from_real_synthetic_authorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _unmocked_fixture(tmp_path, monkeypatch)

    built = current.build_source(
        contract_path=fixture["contract_path"],
        policy_path=fixture["policy_path"],
        history_generation_root=fixture["history_root"],
        allocation_generation_root=fixture["allocation_root"],
        evaluated_on=current.date(2026, 1, 3),
    )
    receipt = built["receipt"]
    assert isinstance(receipt, dict)
    source = tmp_path / str(receipt["bundle_id"])
    source.mkdir()
    for name in (
        "dependence-source.json",
        "history-coverage.json",
        "returns.csv",
        "segments.csv",
        "spec.json",
    ):
        value = built[name]
        assert isinstance(value, bytes)
        (source / name).write_bytes(value)

    assert current.verify_packaged_source(
        source,
        contract_path=fixture["contract_path"],
        policy_path=fixture["policy_path"],
    ) == {
        "bundle_id": source.name,
        "complete_through": "2026-01-02",
        "epoch_hpr_receipts": [],
        "pair_count": 21,
        "pair_mode_count": 42,
        "verification": "VALID",
    }
    assert (source / "history-coverage.json").read_bytes() == (
        fixture["history_root"] / "coverage.json"
    ).read_bytes()
    (source / "history-coverage.json").write_bytes(
        canonical_json_bytes({"fixture": "mutated-after-packaging"})
    )
    with pytest.raises(
        current.CurrentDependenceError, match="history coverage differs"
    ):
        current.verify_packaged_source(
            source,
            contract_path=fixture["contract_path"],
            policy_path=fixture["policy_path"],
        )
    assert receipt["required_strategy_ids"] == [
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "tt-portfolio",
        "volatis-strategy",
        "grizzlybears-vtma",
        "robuxio-crypto",
    ]
    # Moved when the layered history the policy names started carrying the
    # admitted backtest prehistory for grizzlybears-vtma, qsi and tt-portfolio,
    # and again when robuxio-crypto and p123-eu joined them -- the latter from
    # the designer model's own published curve before its 2024-02-18 launch.
    # Moved again when the dependence source began projecting that prehistory
    # instead of dropping it: the hybrid mode had been live-only whatever it
    # was labelled, because only INCLUDE_SELECTED_LIVE segments were selected.
    assert (
        receipt["returns_sha256"]
        == "712e27bc20a1e5cd58ac8b51946cc3b6efede0a91881e39954008a28b3c87155"
    )
    assert receipt["allocation_authority"] == {
        "as_of_date": "2026-01-02",
        "catalog_sha256": "b57948a7ee838616ec697dce5168db88a63106b7cc834c7824c44d0ff3ad07b9",
        "generation_id": "1f090daf866656fbb43c9a6f0077668e9cdd85d825481c1809a206098f990467",
        "manifest_sha256": "b7fda826a4ad224352e1690d44d4579ee0841a5b5c0b5c4e0fc1c7111c9c7a11",
    }
    assert (
        receipt["history_authority"]["generation_id"]
        == fixture["history_generation_id"]
    )


def test_current_dependence_policy_pinned_producer_seam_kills_newer_version_mutant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _unmocked_fixture(tmp_path, monkeypatch)

    with monkeypatch.context() as probe:
        probe.setattr(current, "LAYERED_HISTORY_BUILDER_VERSION", "1.15.0")
        with pytest.raises(current.CurrentDependenceError) as exc:
            _build_from_unmocked_fixture(fixture)
    assert exc.value.args == ("layered history receipt contract differs",)


def _build_from_unmocked_fixture(
    fixture: dict[str, Any],
) -> dict[str, bytes | dict[str, Any]]:
    return current.build_source(
        contract_path=fixture["contract_path"],
        policy_path=fixture["policy_path"],
        history_generation_root=fixture["history_root"],
        allocation_generation_root=fixture["allocation_root"],
        evaluated_on=current.date(2026, 1, 3),
    )


def _assert_build_rejected(fixture: dict[str, Any], message: str) -> None:
    with pytest.raises(current.CurrentDependenceError) as exc:
        _build_from_unmocked_fixture(fixture)
    assert exc.value.args == (message,)


def test_current_dependence_rejects_unbound_policy_perimeter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _unmocked_fixture(tmp_path, monkeypatch)
    policy = json.loads(fixture["policy_path"].read_text(encoding="utf-8"))
    policy["series"][0]["series_id"] = "FORGED SERIES"
    fixture["policy_path"].write_bytes(canonical_json_bytes(policy))

    _assert_build_rejected(
        fixture, "dependence series differs from product authority: p123-ai-factor"
    )


def test_current_dependence_rejects_changed_layered_history_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _unmocked_fixture(tmp_path, monkeypatch)
    segments_path = fixture["history_root"] / "segments.csv"
    segments_path.write_bytes(
        segments_path.read_bytes().replace(b"-0.001", b"-0.101", 1)
    )

    _assert_build_rejected(fixture, "layered history authority differs: segments.csv")


def test_current_dependence_rejects_changed_allocation_catalog_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _unmocked_fixture(tmp_path, monkeypatch)
    catalog_path = fixture["allocation_root"] / "portfolio-baseline.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["allocations"][0]["systematic_weight"] = "0.999"
    catalog_path.write_bytes(canonical_json_bytes(catalog))

    _assert_build_rejected(fixture, "allocation catalog identity differs")


def test_current_dependence_rejects_mixed_valid_history_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _unmocked_fixture(tmp_path / "first", monkeypatch)
    second = _unmocked_fixture(tmp_path / "second", monkeypatch, return_shift="0.1")
    (first["history_root"] / "segments.csv").write_bytes(
        (second["history_root"] / "segments.csv").read_bytes()
    )

    _assert_build_rejected(first, "layered history authority differs: segments.csv")


def test_current_dependence_unmocked_reader_negative_cases_kill_bypass_mutants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_fixture = _unmocked_fixture(tmp_path / "policy", monkeypatch)
    contract, policy, identities = current._load_authorities(
        policy_fixture["contract_path"], policy_fixture["policy_path"]
    )
    rewritten_policy = json.loads(
        policy_fixture["policy_path"].read_text(encoding="utf-8")
    )
    rewritten_policy["series"][0]["series_id"] = "FORGED SERIES"
    policy_fixture["policy_path"].write_bytes(canonical_json_bytes(rewritten_policy))
    _assert_build_rejected(
        policy_fixture,
        "dependence series differs from product authority: p123-ai-factor",
    )
    with monkeypatch.context() as probe:
        probe.setattr(
            current,
            "_load_authorities",
            lambda _contract, _policy: (contract, policy, identities),
        )
        with pytest.raises(pytest.fail.Exception):
            _assert_build_rejected(
                policy_fixture,
                "dependence series differs from product authority: p123-ai-factor",
            )

    history_fixture = _unmocked_fixture(tmp_path / "history", monkeypatch)
    history_contract, history_policy, _ = current._load_authorities(
        history_fixture["contract_path"], history_fixture["policy_path"]
    )
    history_snapshot = current._verified_history(
        history_fixture["history_root"],
        history_policy,
        history_contract,
        current.date(2026, 1, 3),
    )
    history_path = history_fixture["history_root"] / "segments.csv"
    history_path.write_bytes(history_path.read_bytes().replace(b"-0.001", b"-0.101", 1))
    _assert_build_rejected(
        history_fixture, "layered history authority differs: segments.csv"
    )
    with monkeypatch.context() as probe:
        probe.setattr(current, "_verified_history", lambda *_args: history_snapshot)
        with pytest.raises(pytest.fail.Exception):
            _assert_build_rejected(
                history_fixture, "layered history authority differs: segments.csv"
            )

    allocation_fixture = _unmocked_fixture(tmp_path / "allocation", monkeypatch)
    _, allocation_policy, _ = current._load_authorities(
        allocation_fixture["contract_path"], allocation_fixture["policy_path"]
    )
    allocation_snapshot = current._verified_allocations(
        allocation_fixture["allocation_root"],
        allocation_policy,
        tuple(row["strategy_id"] for row in allocation_policy["series"]),
        current.date(2026, 1, 3),
    )
    allocation_path = allocation_fixture["allocation_root"] / "portfolio-baseline.json"
    allocation = json.loads(allocation_path.read_text(encoding="utf-8"))
    allocation["allocations"][0]["systematic_weight"] = "0.999"
    allocation_path.write_bytes(canonical_json_bytes(allocation))
    _assert_build_rejected(allocation_fixture, "allocation catalog identity differs")
    with monkeypatch.context() as probe:
        probe.setattr(
            current, "_verified_allocations", lambda *_args: allocation_snapshot
        )
        with pytest.raises(pytest.fail.Exception):
            _assert_build_rejected(
                allocation_fixture, "allocation catalog identity differs"
            )
