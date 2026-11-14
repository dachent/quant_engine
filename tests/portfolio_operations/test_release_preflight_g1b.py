"""#884 G1: the attribution-contract row (R3 refusals 10, 11) and the factor-freshness row (refusal 14).

Each kill test writes the real input the consumer reads, breaks one fact and asserts that only the target row
is FAIL; the green case proves the row can pass on the consumer's own accepted shape.
"""

from __future__ import annotations

from datetime import date
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import portfolio_report as reporter  # noqa: E402
import release_driver as driver  # noqa: E402
import release_preflight as pf  # noqa: E402
from test_release_driver import world  # noqa: E402,F401
from test_release_preflight import gworld  # noqa: E402,F401

G1B = ("g1:attribution-contract", "g1:factor-freshness")


def _load(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    # dataclasses resolve their annotations through sys.modules while the module executes;
    # the entry must not outlive the load (it would replace what other tests import)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


helpers = _load(
    "decomposition_fixture_helpers",
    "tests/portfolio_analytics/test_portfolio_decomposition.py",
)


def _bridge(effect: float | None = -2.0) -> dict[str, Any]:
    return {
        "account_return": 0.08,
        "categories": [
            {
                "category": "DELAYED_TRADES",
                "effect_pp": effect,
                "status": "EXACT_DATES_PROXY",
            }
        ],
        "gap_pp": -2.0,
        "model_return": 0.10,
        "status": "ORDERED_COUNTERFACTUAL_WITH_EXPLICIT_RESIDUAL",
    }


def _unavailable_bridge(null_effect: float | None) -> dict[str, Any]:
    categories = []
    for name in sorted(reporter._ATTRIBUTION_CATEGORIES):
        if name in reporter._ATTRIBUTION_NULL_CATEGORIES:
            value = null_effect
        elif name == reporter._ATTRIBUTION_PRIMARY_CATEGORY:
            value = None
        else:
            value = 0.5
        stated, unstated = reporter._ATTRIBUTION_CATEGORY_STATUSES[name]
        categories.append(
            {
                "category": name,
                "effect_pp": value,
                "status": stated if value is not None else unstated,
            }
        )
    return {
        "account_return": 0.08,
        "categories": categories,
        "gap_pp": -2.0,
        "model_return": 0.10,
        "status": reporter._ATTRIBUTION_UNAVAILABLE_STATUS,
    }


def _attribution(path: Path, p123_bridge: dict[str, Any] | None = None) -> None:
    verifications = {
        "grizzlybears-vtma": "VERIFIED_WITH_DECLARED_PROXY",
        "p123-ai-factor": "PARTIAL_EXACT_ACTUAL_PNL_PARTIAL_COUNTERFACTUAL_GAP",
        "tt-portfolio": "VERIFIED_WITH_DECLARED_PUBLIC_PRICE_PROXY",
    }
    strategies = []
    for strategy_id, verification in verifications.items():
        bridge = _bridge()
        if strategy_id == "p123-ai-factor" and p123_bridge is not None:
            bridge = p123_bridge
        strategies.append(
            {
                "bridge": bridge,
                "observed_end": "2026-08-02",
                "observed_start": "2026-08-01",
                "strategy_id": strategy_id,
                "verification": verification,
            }
        )
    path.write_text(
        json.dumps(
            {
                "as_of_date": "2026-08-09",
                "attribution_id": "b" * 64,
                "builder": "portfolio-implementation-return-attribution",
                "schema_version": 1,
                "strategies": strategies,
                "verification": "VERIFIED_WITH_DECLARED_PARTIAL_CAUSAL_BOUNDARIES",
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def g1(gworld: dict[str, Any], tmp_path: Path) -> dict[str, Any]:  # noqa: F811
    attribution = tmp_path / "implementation-return-attribution.json"
    _attribution(attribution)
    source = tmp_path / "decomposition"
    target, days = helpers._write_sources(source)
    spec = helpers._spec(source, target, days, maximum_staleness_days=7)
    config = json.loads(gworld["config"].read_text(encoding="utf-8"))
    config.update(
        {
            "impl_return_attribution": str(attribution),
            "base_decomposition_spec": str(spec),
            "base_decomposition_source_root": str(source),
        }
    )
    gworld["config"].write_text(json.dumps(config), encoding="utf-8")
    gworld.update({"attribution": attribution, "source": source, "dspec": spec})
    return gworld


def _statuses(state: dict[str, Any]) -> dict[str, str]:
    run = driver.load_run(state["config"], state["graph"])
    result: dict[str, str] = {}
    for row_id in G1B:
        try:
            pf.recheck(run, [row_id])
            result[row_id] = "PASS"
        except driver.Refusal as exc:
            result[row_id] = str(exc)
    return result


def test_the_g1b_rows_pass_on_inputs_the_consumers_accept(g1: dict[str, Any]) -> None:
    assert _statuses(g1) == {row: "PASS" for row in G1B}


def test_refusal_10_a_non_finite_effect_fails_only_the_attribution_row(
    g1: dict[str, Any],
) -> None:
    _attribution(g1["attribution"], p123_bridge=_bridge(effect=None))
    found = _statuses(g1)
    assert "must be a finite number" in found["g1:attribution-contract"]
    assert found["g1:factor-freshness"] == "PASS"


def test_refusal_11_a_declared_unavailable_effect_with_a_value_fails_only_that_row(
    g1: dict[str, Any],
) -> None:
    _attribution(g1["attribution"], p123_bridge=_unavailable_bridge(null_effect=1.0))
    found = _statuses(g1)
    assert "unavailable effect carries a value" in found["g1:attribution-contract"]
    assert found["g1:factor-freshness"] == "PASS"


def test_a_missing_attribution_file_is_fail_not_pass(g1: dict[str, Any]) -> None:
    g1["attribution"].unlink()
    assert "not written yet" in _statuses(g1)["g1:attribution-contract"]


def test_refusal_14_stale_factors_fail_only_the_freshness_row(
    g1: dict[str, Any], tmp_path: Path
) -> None:
    root = tmp_path / "stale"
    target, days = helpers._write_sources(root, target_only_dates=(date(2026, 2, 2),))
    spec = helpers._spec(
        root, target, [*days, date(2026, 2, 2)], maximum_staleness_days=1
    )
    config = json.loads(g1["config"].read_text(encoding="utf-8"))
    config.update(
        {
            "base_decomposition_spec": str(spec),
            "base_decomposition_source_root": str(root),
        }
    )
    g1["config"].write_text(json.dumps(config), encoding="utf-8")
    found = _statuses(g1)
    assert "UNAVAILABLE_STALE_FACTORS" in found["g1:factor-freshness"]
    assert found["g1:attribution-contract"] == "PASS"


def test_an_unconfigured_freshness_input_is_fail(g1: dict[str, Any]) -> None:
    config = json.loads(g1["config"].read_text(encoding="utf-8"))
    del config["base_decomposition_spec"]
    g1["config"].write_text(json.dumps(config), encoding="utf-8")
    assert "lacks 'base_decomposition_spec'" in _statuses(g1)["g1:factor-freshness"]
