"""Parent-owned #355 core public-producer acceptance, no provider calls.

Usage: python -I -B verify_ortho.py CHECKOUT
Frozen fixture constructors live beside this file, not in candidate tests.
Output field names follow ORTHOGONAL-METHOD.md; numeric maps and arrays are
both accepted where the document does not prescribe JSON representation.
This is core arithmetic proof, not interval/consumer/full-product acceptance.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile

import numpy as np


root = Path(sys.argv[1]).resolve()
os.chdir(root)
sys.path[:0] = [str(root), str(root / "tools")] + [
    str(p) for p in sorted((root / "packages").glob("*/src"))
]
spec = importlib.util.spec_from_file_location(
    "portfolio_decomposition", root / "tools/portfolio_decomposition.py"
)
assert spec and spec.loader
producer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = producer
spec.loader.exec_module(producer)
fixture = runpy.run_path(
    str(Path(__file__).with_name("ortho-fixture.py")),
    init_globals={"decomposition": producer},
)
scratch = Path(tempfile.mkdtemp(prefix="ortho-", dir=root.parent))
FACTOR_IDS = ["factor-one", "factor-two"]
MODE_IDS = ["symmetric_mode:" + name for name in FACTOR_IDS]


def close(actual, expected, message, atol=2e-10):
    a = np.asarray(actual, dtype=float)
    e = np.asarray(expected, dtype=float)
    assert a.shape == e.shape, f"{message}: shape {a.shape} != {e.shape}"
    assert np.all(np.isfinite(a)), f"{message}: nonfinite output"
    assert np.allclose(a, e, rtol=2e-9, atol=atol), f"{message}: {a} != {e}"


def vector(value, ids):
    return [value[key] for key in ids] if isinstance(value, dict) else value


def matrix(value, rows, columns):
    if isinstance(value, dict):
        return [vector(value[row], columns) for row in rows]
    return value


def build(name, dynamic=False, singular=False, perturb=False):
    source = scratch / name
    if dynamic:
        target, days, _truth = fixture["_write_dynamic_sources"](source)
        model = fixture["_dynamic_model"](days)
    else:
        target, days = fixture["_write_sources"](source, identical_factors=singular)
        model = fixture["_model"](days)
    if perturb:
        # Alter held-out factor values and preserve the known raw target
        # relationship. This keeps the model admissible while discriminating
        # a covariance/scale fit that incorrectly consumes holdout factors.
        factor_path = source / "factors.csv"
        with factor_path.open(newline="", encoding="utf-8") as handle:
            factor_rows = list(csv.reader(handle))
        for row in factor_rows[1:]:
            if row[0] >= str(model["test_start"]):
                row[2] = format(float(row[2]) + 0.007, ".17g")
                row[3] = format(float(row[3]) - 0.003, ".17g")
        with factor_path.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(factor_rows)
        with target.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        for row in rows[1:]:
            if row[1] >= str(model["test_start"]):
                row[2] = format(float(row[2]) + 0.6 * 0.007 - 0.2 * -0.003, ".17g")
        with target.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(rows)
    spec_path = fixture["_spec"](source, target, days, models=[model])
    result, manifest, encoded = producer.build_decomposition(spec_path, source)
    assert json.loads(encoded) == result, "public producer bytes differ from payload"
    selected = [row for row in result["model_results"] if row["selected_for_series"]]
    assert len(selected) == 1 and selected[0]["status"] == "AVAILABLE", (
        f"{name}: fixture did not produce an available raw model"
    )
    raw = selected[0]
    assert raw["contributions"] and raw["exposures"], f"{name}: raw audit disappeared"
    with (source / "factors.csv").open(newline="", encoding="utf-8") as handle:
        factors = {row["date"]: [float(row["F1"]), float(row["F2"])]
                   for row in csv.DictReader(handle)}
    beta = {row["factor_id"]: float(row["beta"]) for row in raw["exposures"]}
    for row in raw["contributions"]:
        betas = row.get("factor_exposures", beta)
        expected = np.asarray(factors[row["date"]]) * np.asarray(vector(betas, FACTOR_IDS), float)
        close(vector(row["factor_contributions"], FACTOR_IDS), expected, "raw contribution preservation")
        close(float(row["fitted_excess_return"]), float(row["alpha_contribution"]) + expected.sum(), "raw fitted preservation")
    if not dynamic and not singular:
        close(vector(beta, FACTOR_IDS), [0.6, -0.2], "frozen synthetic static truth", atol=1e-8)
    return raw, factors


def check_projection(raw, factors, dynamic=False):
    assert "orthogonal_attribution" in raw, "MISSING_PLANNED_ORTHOGONAL_ATTRIBUTION: public selected raw model has no separate projection"
    out = raw["orthogonal_attribution"]
    assert out["status"] == "AVAILABLE", out
    required = {"method_id", "training_row_identity", "train_start", "train_end",
                "raw_factor_ids", "raw_exposures", "train_mean", "train_scale",
                "covariance", "eigenvalues", "rank", "rank_tolerance",
                "raw_to_mode_weights", "mode_ids", "mode_coefficients",
                "orthogonal_intercept", "train_fitted_risk_budget", "per_date",
                "raw_prediction_reconciliation"}
    assert required <= out.keys(), f"missing method receipt fields: {required - out.keys()}"
    assert out["method_id"] and out["training_row_identity"]
    assert out["raw_factor_ids"] == FACTOR_IDS and out["mode_ids"] == MODE_IDS
    raw_rows = raw["contributions"]
    train = [row for row in raw_rows if row["partition"] == "TRAIN"]
    assert out["train_start"] == train[0]["date"] and out["train_end"] == train[-1]["date"]
    x = np.asarray([factors[row["date"]] for row in train])
    mean, scale = x.mean(axis=0), x.std(axis=0, ddof=1)
    z = (x - mean) / scale
    covariance = z.T @ z / (len(x) - 1)
    eigenvalues, q = np.linalg.eigh(covariance)
    whitening = (q * (1 / np.sqrt(eigenvalues))) @ q.T
    square_root = (q * np.sqrt(eigenvalues)) @ q.T
    tolerance = np.finfo(float).eps * max(x.shape) * eigenvalues[-1]
    close(vector(out["train_mean"], FACTOR_IDS), mean, "TRAIN means")
    close(vector(out["train_scale"], FACTOR_IDS), scale, "TRAIN scales")
    close(matrix(out["covariance"], FACTOR_IDS, FACTOR_IDS), covariance, "TRAIN covariance")
    close(out["eigenvalues"], eigenvalues, "TRAIN eigenvalues")
    close(out["rank_tolerance"], tolerance, "scale-aware tolerance", atol=1e-25)
    assert out["rank"] == 2
    close(matrix(out["raw_to_mode_weights"], FACTOR_IDS, MODE_IDS), whitening, "symmetric whitening weights")
    emitted = {row["date"]: row for row in out["per_date"]}
    assert len(emitted) == len(out["per_date"]) == len(raw_rows)
    beta = {row["factor_id"]: float(row["beta"]) for row in raw["exposures"]}
    if not dynamic:
        raw_beta = np.asarray(vector(beta, FACTOR_IDS))
        close(vector(out["raw_exposures"], FACTOR_IDS), raw_beta, "receipt raw exposure preservation")
        close(vector(out["mode_coefficients"], MODE_IDS), square_root @ (scale * raw_beta), "static transformed coefficients")
        close(out["orthogonal_intercept"], float(raw_rows[0]["alpha_contribution"]) + mean @ raw_beta, "static transformed intercept")
    expected_components, intercepts = [], []
    for row in raw_rows:
        betas = np.asarray(vector(row.get("factor_exposures", beta), FACTOR_IDS), float)
        modes = ((np.asarray(factors[row["date"]]) - mean) / scale) @ whitening
        theta = square_root @ (scale * betas)
        intercept = float(row["alpha_contribution"]) + mean @ betas
        terms = modes * theta
        actual = emitted[row["date"]]
        assert actual["partition"] == row["partition"]
        close(vector(actual["mode_contributions"], MODE_IDS), terms, "per-date orthogonal components")
        close(actual["fitted_excess_return"], intercept + terms.sum(), "orthogonal prediction")
        close(actual["fitted_excess_return"], row["fitted_excess_return"], "raw/orthogonal prediction reconciliation")
        if row["partition"] == "TRAIN":
            expected_components.append(terms)
            intercepts.append(intercept)
    components = np.asarray(expected_components)
    varying = bool(np.ptp(intercepts) > 1e-12)
    if dynamic:
        assert varying, "dynamic positive control has no varying transformed intercept"
    budget = out["train_fitted_risk_budget"]
    assert "EQUAL_FACTOR_ANCHOR_FITTED_COMPONENT_SAMPLE_VARIANCE" in json.dumps(budget), (
        "missing required equal-anchor unannualized fitted-risk interpretation"
    )
    ids = budget["component_ids"]
    assert set(MODE_IDS) <= set(ids) and len(ids) == 2 + int(varying)
    assert budget["intercept_time_varying"] is varying
    ordered = []
    for component in ids:
        ordered.append(components[:, MODE_IDS.index(component)] if component in MODE_IDS else np.asarray(intercepts))
    g = np.column_stack(ordered)
    gamma = np.cov(g, rowvar=False, ddof=1)
    euler = gamma @ np.ones(len(ids))
    close(matrix(budget["component_covariance"], ids, ids), gamma, "full fitted-component covariance")
    close(vector(budget["euler_allocations"], ids), euler, "Euler allocations")
    close(budget["fitted_variance"], np.var(g.sum(axis=1), ddof=1), "fitted variance")
    close(sum(map(float, vector(budget["euler_allocations"], ids))), budget["fitted_variance"], "Euler budget reconciliation")
    mode_gamma = np.cov(components, rowvar=False, ddof=1)
    cross = bool(np.max(np.abs(mode_gamma - np.diag(np.diag(mode_gamma)))) > 1e-12)
    assert budget["cross_mode_covariance_present"] is cross
    if dynamic:
        assert cross, "dynamic positive control lacks cross-mode covariance"
    else:
        theta = square_root @ (scale * np.asarray(vector(beta, FACTOR_IDS)))
        close(budget["fitted_variance"], theta @ theta, "static squared-mode coefficient budget")
    return out


# Run all three raw positive controls before demanding the new field. A red
# baseline must therefore mean the missing planned surface, not broken setup.
static, factors = build("static")
dynamic, dynamic_factors = build("dynamic", dynamic=True)
singular, singular_factors = build("singular", singular=True)
assert singular["diagnostics"]["training_design"]["matrix_rank"] == 1
print("RAW_POSITIVE_CONTROLS_PASS: static, dynamic, singular selected available", flush=True)
static_out = check_projection(static, factors)
check_projection(dynamic, dynamic_factors, dynamic=True)
rank = singular.get("orthogonal_attribution")
assert rank and rank["status"] == "UNAVAILABLE_ORTHOGONAL_RANK_DEFICIENT"
assert rank["raw_factor_ids"] == FACTOR_IDS and rank["rank"] < 2
assert rank["eigenvalues"] and float(rank["rank_tolerance"]) > 0
assert singular["contributions"] and singular["exposures"], "rank failure destroyed raw audit"
changed, changed_factors = build("holdout-changed", perturb=True)
changed_out = check_projection(changed, changed_factors)
for key in ("train_mean", "train_scale", "covariance", "eigenvalues", "rank",
            "rank_tolerance", "raw_to_mode_weights", "mode_coefficients"):
    assert changed_out[key] == static_out[key], f"holdout altered TRAIN projection: {key}"
print(json.dumps({"status": "PASS", "scope": "C2_CORE_PUBLIC_PRODUCER_ONLY",
                  "checks": ["static_prediction_and_variance", "dynamic_prediction_and_covariance_euler",
                             "raw_preservation", "rank_fail_closed", "frozen_train_transform"],
                  "scratch": str(scratch)}, sort_keys=True))
