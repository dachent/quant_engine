"""The model_vs_actual block: closure, evidence honesty, and the real schema."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import portfolio_model_vs_actual as mva  # noqa: E402

SCHEMA = json.loads(
    (ROOT / "schemas" / "portfolio" / "control-panel-data.schema.json").read_text(
        encoding="utf-8"
    )
)
SYNTHETIC = (
    ROOT / "tests" / "fixtures" / "synthetic" / "model-vs-actual-audit-bridges.json"
)
# The real audit figures live in the governed data root, never in Git: an optional local run
# points this variable at that file (its sha256 is pinned in the root's manifest.json).
REAL_INPUT_ENV = "MODEL_VS_ACTUAL_AUDIT_INPUT"
SHA = "a" * 64


def _errors(instance: Any) -> list[str]:
    schema = {"$ref": "#/$defs/model_vs_actual", "$defs": SCHEMA["$defs"]}
    return [
        f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
        for e in Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).iter_errors(instance)
    ]


def _component(cid: str, pp: float, **extra: Any) -> dict[str, Any]:
    item = {
        "component_id": cid,
        "label": f"{cid} label",
        "pp": pp,
        "usd": None,
        "origin_tag": "D",
        "estimated": False,
        "uncertainty_pp_1sigma": None,
        "source_ref": {"file": "x.csv", "sha256": SHA, "columns": ["a"]},
        "replacing_pull": None,
        "sub_components": [],
    }
    item.update(extra)
    return item


def _audit() -> dict[str, Any]:
    def window(
        wid: str, kind: str, comps: list[dict[str, Any]], **extra: Any
    ) -> dict[str, Any]:
        base = {
            "window_id": wid,
            "label": wid,
            "kind": kind,
            "start": "2026-01-02",
            "end": "2026-03-02",
            "account_return_pct": 4.0,
            "model_return_pct": 10.0,
            "model_currency": "USD",
            "account_currency": "USD",
            "audit_gap_pp": -6.0,
            "view": "TEST_VIEW",
            "components": comps,
            "memo": [],
            "capital_base_usd": 10000.0,
            "capital_base_basis": "AUDIT_AVERAGE_NAV_TEST",
        }
        base.update(extra)
        return base

    comps = [
        _component("execution", -4.0, replacing_pull="EU-P1"),
        _component("commissions", -1.0),
        _component(
            "leverage",
            -0.9,
            estimated=True,
            uncertainty_pp_1sigma=4.6,
            sub_components=[
                {
                    "component_id": "cash_drag",
                    "label": "cash drag",
                    "pp": -0.9,
                    "origin_tag": "D",
                    "estimated": True,
                    "uncertainty_pp_1sigma": 4.6,
                }
            ],
        ),
    ]
    return {
        "schema_version": 1,
        "artifact": "model-vs-actual-audit-bridges",
        "audit_written": "2026-10-22",
        "closure_tolerance_pp": 0.02,
        "source_manifest": {"path": "m.json", "sha256": SHA},
        "pulls": [
            {
                "pull_id": "EU-P1",
                "title": "A",
                "source": "s",
                "entities": "e",
                "date_range": "d",
                "access": "public",
                "estimated_size": "1 MB",
                "replaces": ["execution"],
                "status": "REQUESTED_NOT_DOWNLOADED",
            }
        ],
        "strategies": [
            {
                "strategy_id": "s1",
                "display_name": "S1",
                "comparator": {
                    "description": "d",
                    "publisher": "p",
                    "provider_published": True,
                    "gross_or_net": "gross",
                    "model_currency": "USD",
                },
                "primary_window_id": "W",
                "windows": [
                    window("W", "ALIGNED", comps, audit_gap_usd=None),
                    window("E1", "EPOCH", copy.deepcopy(comps)),
                ],
                "unavailable": [
                    {
                        "term": "gap_to_cutoff",
                        "reason": "NO_MODEL_LEG",
                        "window_end": "2026-03-09",
                        "replacing_pull": "EU-P1",
                    }
                ],
                "notes": ["n"],
            },
            {
                "strategy_id": "s2",
                "display_name": "S2",
                "comparator": {
                    "description": "d",
                    "publisher": "p",
                    "provider_published": False,
                    "gross_or_net": "gross",
                    "model_currency": "USD",
                },
                "primary_window_id": "W",
                "windows": [
                    window(
                        "W",
                        "ALIGNED",
                        [
                            _component("execution", -2.5, usd=-250.0),
                            _component("other", -0.5, usd=-50.0),
                        ],
                        account_return_pct=7.0,
                        model_return_pct=10.0,
                        audit_gap_pp=-3.0,
                        audit_gap_usd=-300.0,
                    )
                ],
                "unavailable": [],
                "notes": [],
            },
        ],
    }


# ---------------------------------------------------------------- synthetic staged input

_STAGED_SHAPE = {
    # strategy id: (primary window, [(window id, kind, end date, currency of the model)], uses dollars)
    "p123-eu": (
        "W_full",
        [
            ("W_full", "ALIGNED", "2026-09-11", "EUR"),
            ("E1", "EPOCH", "2026-01-15", "EUR"),
        ],
        False,
    ),
    "p123-ai-factor": (
        "A",
        [("A", "ALIGNED", "2026-09-02", "USD"), ("E1", "EPOCH", "2026-03-01", "USD")],
        False,
    ),
    "grizzlybears-vtma": ("W2", [("W2", "ALIGNED", "2026-08-03", "USD")], False),
    "tt-portfolio": (
        "ALL",
        [("ALL", "ALIGNED", "2026-09-02", "USD"), ("G1", "EPOCH", "2026-01-02", "USD")],
        False,
    ),
    "qsi": (
        "WINDOW",
        [
            ("WINDOW", "ALIGNED", "2026-09-18", "USD"),
            ("E1", "EPOCH", "2026-02-01", "USD"),
        ],
        False,
    ),
    "volatis-strategy": ("ALL", [("ALL", "ALIGNED", "2026-09-17", "USD")], True),
    "robuxio-crypto": ("OWNER", [("OWNER", "ALIGNED", "2026-09-02", "USD")], True),
}
_STAGED_COMPONENTS = (
    "execution",
    "commissions",
    "composition",
    "timing",
    "leverage",
    "fx",
    "other",
)


def synthetic_staged_audit_input() -> dict[str, Any]:
    """A wholly invented audit input with the shape and edge cases of the real one.

    Every number is generated here from loop indices; none comes from an account or an audit.
    """

    strategies = []
    for index, (sid, (primary, windows, dollars)) in enumerate(_STAGED_SHAPE.items()):
        built = []
        for wnum, (wid, kind, end, currency) in enumerate(windows):
            base = 10000.0 + 1000.0 * (index + 1)
            comps = []
            for cnum, cid in enumerate(_STAGED_COMPONENTS[: 4 + (wnum + index) % 4]):
                pp = round(
                    ((cnum % 3) - 1 + 0.1 * (index + wnum)) * (0.4 + 0.1 * cnum), 3
                )
                estimated = (
                    sid == "p123-eu" and wid == "W_full" and cid in {"leverage", "fx"}
                )
                comps.append(
                    _component(
                        cid,
                        pp,
                        usd=round(pp * base / 100.0, 2) if dollars else None,
                        estimated=estimated,
                        uncertainty_pp_1sigma=1.0 if estimated else None,
                        replacing_pull="EU-P1" if cid == "execution" else None,
                    )
                )
            total = round(sum(c["pp"] for c in comps), 3)
            account = round(5.0 + 0.1 * (index + wnum), 3)
            built.append(
                {
                    "window_id": wid,
                    "label": wid,
                    "kind": kind,
                    "start": "2026-01-02",
                    "end": end,
                    "account_return_pct": account,
                    "model_return_pct": round(account - total, 3),
                    "model_currency": currency,
                    "account_currency": "USD",
                    "audit_gap_pp": total,
                    "view": "SYNTHETIC_VIEW",
                    "components": comps,
                    "memo": [],
                    "capital_base_usd": base,
                    "capital_base_basis": "SYNTHETIC_BASE",
                    **(
                        {"audit_gap_usd": round(total * base / 100.0, 2)}
                        if dollars
                        else {}
                    ),
                }
            )
        strategies.append(
            {
                "strategy_id": sid,
                "display_name": sid,
                "comparator": {
                    "description": "synthetic",
                    "publisher": "synthetic",
                    "provider_published": False,
                    "gross_or_net": "gross",
                    "model_currency": "USD",
                },
                "primary_window_id": primary,
                "windows": built,
                "unavailable": [],
                "notes": [],
            }
        )
    audit = _audit()
    audit["strategies"] = strategies
    return audit


def synthetic_staged_bytes() -> bytes:
    return json.dumps(synthetic_staged_audit_input()).encode("utf-8")


def _raw(audit: dict[str, Any]) -> tuple[bytes, str]:
    raw = json.dumps(audit).encode("utf-8")
    return raw, hashlib.sha256(raw).hexdigest()


def _panel() -> dict[str, Any]:
    return {
        "strategies": [
            {"strategy_id": "s1", "implementation_reconciliation": None},
            {
                "strategy_id": "s2",
                "implementation_reconciliation": {
                    "return_reconciliation": {
                        "account_cumulative_return": -0.01,
                        "account_minus_model_cumulative": -0.02,
                    }
                },
            },
        ],
        "invested_capital": {"complete_through": "2026-03-09", "daily_by_strategy": []},
        "leverage_disclosures": [
            {
                "strategy_id": "s1",
                "classification": "BROKER_DEBIT_INTEREST",
                "accounting_date": "2026-01-10",
                "source_amount": {
                    "status": "AVAILABLE",
                    "currency": "USD",
                    "value": "-2.5",
                },
            },
            {
                "strategy_id": "s1",
                "classification": "BROKER_DEBIT_INTEREST",
                "accounting_date": "2026-01-11",
                "source_amount": {
                    "status": "AVAILABLE",
                    "currency": "NOK",
                    "value": "-30",
                },
            },
            {
                "strategy_id": "s2",
                "classification": "BROKER_DEBIT_INTEREST",
                "accounting_date": "2026-01-11",
                "source_amount": {
                    "status": "AVAILABLE",
                    "currency": "USD",
                    "value": "-4",
                },
            },
        ],
    }


def _build(audit: dict[str, Any] | None = None) -> tuple[dict[str, Any], bytes, str]:
    raw, sha = _raw(audit or _audit())
    return mva.build_model_vs_actual(_panel(), raw, sha), raw, sha


def test_the_block_validates_against_the_real_schema_and_verifier() -> None:
    block, raw, sha = _build()
    assert _errors(block) == []
    mva.verify_model_vs_actual(block, _panel(), raw, sha)
    assert block["schema_version"] == 1 and block["as_of"] == "2026-03-09"


def test_the_residual_is_shown_and_closes_the_gap() -> None:
    block, _raw_bytes, _sha = _build()
    window = block["strategies"][0]["windows"][0]
    assert window["account_gap_pp"] == pytest.approx(-6.0)
    assert window["bridge_sum_pp"] == pytest.approx(-5.9)
    assert window["residual"]["pp"] == pytest.approx(-0.1)
    # |residual| above the tolerance is reported, never absorbed
    assert window["closure_ok"] is False
    other = block["strategies"][1]["windows"][0]
    assert other["residual"]["pp"] == pytest.approx(0.0)
    assert other["closure_ok"] is True
    assert other["residual"]["usd"] == pytest.approx(0.0)


def test_audit_numbers_are_never_tagged_as_governed() -> None:
    block, _raw_bytes, _sha = _build()
    for strategy in block["strategies"]:
        for window in [*strategy["windows"], *strategy["epochs"]]:
            for component in window["bridge"]:
                assert component["evidence_tag"] in {"AUDIT_DERIVED", "ESTIMATED"}
    leverage = block["strategies"][0]["windows"][0]["bridge"][-1]
    assert leverage["component_id"] == "leverage"
    assert leverage["evidence_tag"] == "ESTIMATED"
    assert leverage["uncertainty_pp_1sigma"] == 4.6
    assert leverage["sub_components"][0]["evidence_tag"] == "ESTIMATED"


def test_a_tampered_tag_is_rejected_by_the_verifier() -> None:
    block, raw, sha = _build()
    forged = copy.deepcopy(block)
    forged["strategies"][0]["windows"][0]["bridge"][0]["evidence_tag"] = "EVIDENCED"
    with pytest.raises(mva.ModelVsActualError):
        mva.verify_model_vs_actual(forged, _panel(), raw, sha)


def test_the_pin_is_checked() -> None:
    raw, _sha = _raw(_audit())
    with pytest.raises(mva.ModelVsActualError, match="pin"):
        mva.build_model_vs_actual(_panel(), raw, "b" * 64)


def test_an_unknown_pull_or_component_is_refused() -> None:
    audit = _audit()
    audit["strategies"][0]["windows"][0]["components"][0]["replacing_pull"] = "NOPE"
    raw, sha = _raw(audit)
    with pytest.raises(mva.ModelVsActualError, match="unknown pull"):
        mva.build_model_vs_actual(_panel(), raw, sha)
    audit = _audit()
    audit["strategies"][0]["windows"][0]["components"][0]["component_id"] = "magic"
    raw, sha = _raw(audit)
    with pytest.raises(mva.ModelVsActualError, match="vocabulary"):
        mva.build_model_vs_actual(_panel(), raw, sha)


def test_governed_interest_is_evidenced_and_never_sums_mixed_currencies() -> None:
    block, _raw_bytes, _sha = _build()
    mixed = block["strategies"][0]["windows"][0]["governed_terms"][0]
    assert mixed["evidence_tag"] == "EVIDENCED"
    assert mixed["by_currency"] == {"NOK": -30.0, "USD": -2.5}
    assert mixed["usd"] is None and "NOT_CONVERTED" in mixed["reason"]
    usd_only = block["strategies"][1]["windows"][0]["governed_terms"][0]
    assert usd_only["usd"] == -4.0 and usd_only["reason"] is None


def test_governed_reference_reports_the_admitted_contract_or_why_not() -> None:
    block, _raw_bytes, _sha = _build()
    assert block["strategies"][0]["governed_reference"]["status"] == "UNAVAILABLE"
    assert block["strategies"][1]["governed_reference"]["status"] == "EVIDENCED"


def test_the_portfolio_bridge_sums_components_to_the_gap_with_a_residual() -> None:
    block, _raw_bytes, _sha = _build()
    bridge = block["portfolio_bridge"]
    assert bridge["window_basis"] == "EACH_STRATEGY_PRIMARY_WINDOW_NOT_A_COMMON_WINDOW"
    total = sum(item["usd"] for item in bridge["components"])
    assert bridge["components_sum_usd"] == pytest.approx(total)
    assert bridge["account_gap_usd"] - bridge["components_sum_usd"] == pytest.approx(
        bridge["residual"]["usd"]
    )
    tags = {item["component_id"]: item["evidence_tag"] for item in bridge["components"]}
    assert tags["leverage"] == "ESTIMATED"
    assert tags["execution"] == "ESTIMATED"  # pp times a capital base for one strategy


def test_epochs_and_aligned_windows_are_separate_lists() -> None:
    block, _raw_bytes, _sha = _build()
    first = block["strategies"][0]
    assert [w["window_id"] for w in first["windows"]] == ["W"]
    assert [w["window_id"] for w in first["epochs"]] == ["E1"]


def test_the_synthetic_audit_input_builds_closes_and_validates() -> None:
    _assert_staged_shape(synthetic_staged_bytes())


@pytest.mark.skipif(
    not os.environ.get(REAL_INPUT_ENV), reason="real audit input is not in Git"
)
def test_the_governed_audit_input_builds_closes_and_validates() -> None:
    _assert_staged_shape(Path(os.environ[REAL_INPUT_ENV]).read_bytes())


def _assert_staged_shape(raw: bytes) -> None:
    sha = hashlib.sha256(raw).hexdigest()
    block = mva.build_model_vs_actual(_panel_for_staged(), raw, sha)
    assert _errors(block) == []
    mva.verify_model_vs_actual(block, _panel_for_staged(), raw, sha)
    ids = {s["strategy_id"] for s in block["strategies"]}
    assert ids == {
        "p123-eu",
        "p123-ai-factor",
        "grizzlybears-vtma",
        "tt-portfolio",
        "qsi",
        "volatis-strategy",
        "robuxio-crypto",
    }
    for strategy in block["strategies"]:
        for window in [*strategy["windows"], *strategy["epochs"]]:
            assert window["closure_ok"], (strategy["strategy_id"], window["window_id"])
    assert block["portfolio_bridge"]["closure_ok"]


def _panel_for_staged() -> dict[str, Any]:
    return {
        "strategies": [],
        "invested_capital": {"complete_through": "2026-09-17", "daily_by_strategy": []},
        "leverage_disclosures": [],
    }


def test_the_cli_pin_pair_goes_together() -> None:
    import argparse

    import portfolio_control_panel as panel

    both = argparse.Namespace(
        model_vs_actual_input=Path("x.json"), model_vs_actual_input_sha256="a" * 64
    )
    assert panel._model_vs_actual_cli_value(both) == (Path("x.json"), "a" * 64)
    neither = argparse.Namespace(
        model_vs_actual_input=None, model_vs_actual_input_sha256=None
    )
    assert panel._model_vs_actual_cli_value(neither) is None
    half = argparse.Namespace(
        model_vs_actual_input=Path("x.json"), model_vs_actual_input_sha256=None
    )
    with pytest.raises(panel.ControlPanelValidationError):
        panel._model_vs_actual_cli_value(half)


def test_an_unavailable_term_with_an_unknown_pull_is_refused_at_build() -> None:
    audit = _audit()
    audit["strategies"][0]["unavailable"][0]["replacing_pull"] = "TYPO-P9"
    raw, sha = _raw(audit)
    with pytest.raises(mva.ModelVsActualError, match="unknown pull"):
        mva.build_model_vs_actual(_panel(), raw, sha)


def test_a_window_ending_after_the_cutoff_is_withheld_not_published() -> None:
    audit = _audit()
    audit["strategies"][0]["windows"][0]["end"] = "2026-03-10"  # cutoff is 2026-03-09
    raw, sha = _raw(audit)
    block = mva.build_model_vs_actual(_panel(), raw, sha)
    first = block["strategies"][0]
    assert first["windows"] == [] and first["primary_window_id"] is None
    assert [w["window_id"] for w in first["epochs"]] == ["E1"]
    withheld = [u for u in first["unavailable"] if u["term"] == "window:W"]
    assert withheld and withheld[0]["reason"] == "WINDOW_ENDS_AFTER_CUTOFF:2026-03-10"
    excluded = {
        e["strategy_id"] for e in block["portfolio_bridge"]["strategies_excluded"]
    }
    assert "s1" in excluded
    assert _errors(block) == []
    mva.verify_model_vs_actual(block, _panel(), raw, sha)
