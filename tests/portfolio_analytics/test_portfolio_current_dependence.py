from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from quant_engine.core.contracts import canonical_json_bytes


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
    monkeypatch.setattr(
        current,
        "_verified_history",
        lambda *_args, **_kwargs: (
            projected,
            identities,
            current.date(2026, 1, 2),
            {"generation_id": "a" * 64},
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
    for name in ("dependence-source.json", "returns.csv", "segments.csv", "spec.json"):
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
