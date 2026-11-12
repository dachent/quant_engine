"""Benchmark and provider comparisons on the sleeve basis (#624, option B, branch A).

A strategy named in a ``strategy-basis-declaration/1`` file has its comparison recomputed
on the sleeve path (derived from the sleeve facts), beside the unchanged account-basis
comparison.  The synthetic world is the derived-sleeve world of
``test_account_manifest_sleeve_seam``; the real R2 bytes run when the environment names
them (``QE_AI624_R2_FACTS``, ``QE_AI624_R2_ATTRIBUTION_RETURNS``,
``QE_AI624_R2_ATTRIBUTION_SPEC``) and are never committed.
"""

# ruff: noqa: E402

from __future__ import annotations

import copy
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
from typing import Any

import pytest

_TESTS = Path(__file__).resolve().parents[1]
for _sibling in ("portfolio_control_panel", "core", "portfolio_attribution"):
    if str(_TESTS / _sibling) not in sys.path:
        sys.path.append(str(_TESTS / _sibling))

from jsonschema import Draft202012Validator
from quant_engine.core.contracts import canonical_json_bytes

import portfolio_attribution as attribution
import portfolio_report as report
import strategy_basis
from test_account_manifest_sleeve_seam import (
    PARENT,
    _facts_bytes,
    _liquidate_and_reallocate_facts,
)
from test_portfolio_attribution import _baseline, _inputs

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = "strategy-1"
SYNTHETIC_STRATEGY_IN_FACTS = "strategy-s2"
# sha256 of the switched-off catalog of this synthetic world, built by the builder as it
# was at the keystone head 07f02431 (before this branch)
SWITCH_OFF_SHA256 = "0111a1abe7aa1234d0d741c44c25ca00e9f2c8e26e4f775a0ee7ebfa77cb4a68"
SLEEVE_DATES = ("2025-12-31", "2026-01-01", "2026-01-02")
OPENING = "2025-12-30"

# the reference series: every sleeve date is a stored date, with older dates before it
BENCHMARK = {
    "2025-12-28": "0.004",
    "2025-12-29": "-0.002",
    "2025-12-31": "0.05",
    "2026-01-01": "0.01",
    "2026-01-02": "-0.02",
}
PROVIDER = {
    "2025-12-28": "0.001",
    "2025-12-29": "0.002",
    "2025-12-31": "0.07",
    "2026-01-01": "-0.004",
    "2026-01-02": "-0.03",
}
# an unchanged copy of the account-basis series the spec names (parent account rows)
ACCOUNT = {"2025-12-29": "0.01", "2025-12-31": "-0.02", "2026-01-01": "0.03"}


def _facts() -> bytes:
    text = _facts_bytes(_liquidate_and_reallocate_facts()).decode("ascii")
    text = text.replace(SYNTHETIC_STRATEGY_IN_FACTS, STRATEGY)
    # the baseline of the shared fixture ends 2026-01-03: move the world 30 days earlier
    for day in (
        "2026-02-01",
        "2026-01-31",
        "2026-01-30",
        "2026-01-29",
        "2026-01-28",
        "2026-01-27",
    ):
        text = text.replace(
            day, (date.fromisoformat(day) - timedelta(days=30)).isoformat()
        )
    return text.encode("ascii")


def _declaration(tmp_path: Path, strategy: str = STRATEGY) -> Path:
    path = tmp_path / "strategy-basis-declaration.json"
    path.write_text(
        json.dumps(
            {
                "schema": strategy_basis.DECLARATION_SCHEMA,
                "strategies": [{"strategy_id": strategy, "basis": "SLEEVE_EPOCH"}],
            }
        ),
        encoding="utf-8",
    )
    return path


def _world(
    tmp_path: Path, *, parent: str = PARENT, window_end: str = "2026-01-02"
) -> dict[str, Path]:
    """A baseline, an attribution spec and return source, the sleeve facts, a declaration."""

    baseline = _baseline(
        tmp_path,
        strategy_returns=[
            {
                "component_account_ids": [parent],
                "date": day,
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": "synthetic",
                "return": value,
                "strategy_id": STRATEGY,
            }
            for day, value in sorted(ACCOUNT.items())
        ],
    )
    spec, returns = _inputs(tmp_path / "inputs")
    lines = ["series_id,date,return,evidence_layer,version_ref,source_artifact_id"]
    for series_id, layer, version, values in (
        ("account", "ACCOUNT_LIVE", "v1", ACCOUNT),
        ("provider", "PROVIDER_LIVE", "v1", PROVIDER),
        ("benchmark", "OFFICIAL_VEHICLE", "benchmark:v1", BENCHMARK),
    ):
        for day, value in sorted(values.items()):
            lines.append(
                f"{series_id},{day},{value},{layer},{version},{series_id}-source"
            )
    returns.write_text("\n".join(lines) + "\n", encoding="utf-8")
    document = json.loads(spec.read_text(encoding="utf-8"))
    document["return_source"]["expected_sha256"] = hashlib.sha256(
        returns.read_bytes()
    ).hexdigest()
    for key in ("benchmark_comparisons", "comparisons"):
        for row in document[key]:
            if row["strategy_id"] == STRATEGY:
                row["observed_start"] = "2025-12-29"
                row["observed_end"] = window_end
                row["expected_legacy_cumulative_gap"] = None
    spec.write_text(json.dumps(document), encoding="utf-8")
    facts = tmp_path / "sleeve-account-facts.json"
    facts.write_bytes(_facts())
    return {
        "baseline": baseline,
        "spec": spec,
        "returns": returns,
        "facts": facts,
        "declaration": _declaration(tmp_path),
    }


def _build(world: dict[str, Path], *, restated: bool = True) -> tuple[Any, bytes, Any]:
    kwargs: dict[str, Any] = {}
    if restated:
        kwargs = {
            "strategy_basis_declaration": world["declaration"],
            "sleeve_facts": world["facts"],
        }
    return attribution.build_catalog(world["baseline"], world["spec"], **kwargs)


def _row(catalog: dict[str, Any], key: str) -> dict[str, Any]:
    return next(row for row in catalog[key] if row["strategy_id"] == STRATEGY)


def _sleeve_rows() -> tuple[dict[str, Any], ...]:
    return strategy_basis.restatement_from_sleeve_facts(_facts(), STRATEGY).rows


# --- end to end ---------------------------------------------------------------------


def test_both_lanes_are_produced_and_the_account_lane_is_unchanged(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path)
    off, off_bytes, off_artifacts = _build(world, restated=False)
    on, _, on_artifacts = _build(world)

    for key in ("benchmark_comparisons", "comparisons"):
        row = _row(on, key)
        lane = row.pop("sleeve_basis")
        # the account-basis comparison is byte for byte the switched-off one
        assert row == _row(off, key)
        assert lane["basis"] == "SLEEVE_EPOCH"
        assert lane["account_series_source"] == attribution.ACCOUNT_SOURCE_SLEEVE_FACTS
        assert lane["metrics"]["observations"] == 3
        assert lane["metrics"]["observed_start"] == SLEEVE_DATES[0]
        assert lane["metrics"]["observed_end"] == SLEEVE_DATES[-1]
        assert lane["opening_date"] == OPENING
        assert lane["closure"]["window_open_date"] == OPENING
        assert lane["sleeve_series_sha256"] == strategy_basis.series_sha256(
            _sleeve_rows()
        )
    # nothing else moved: every other row and key is the switched-off catalog's
    stripped = {k: v for k, v in on.items() if k != "strategy_basis"}
    for key in ("benchmark_comparisons", "comparisons"):
        stripped[key] = [
            {k: v for k, v in r.items() if k != "sleeve_basis"} for r in on[key]
        ]
    assert stripped == off
    assert off_bytes == canonical_json_bytes(off)
    block = on["strategy_basis"]
    assert block["basis"] == "SLEEVE_EPOCH"
    assert block["strategies"] == [
        {
            "opening_date": OPENING,
            "parent_account_id": PARENT,
            "series_observations": 3,
            "series_observed_end": SLEEVE_DATES[-1],
            "series_sha256": strategy_basis.series_sha256(_sleeve_rows()),
            "sleeve_account_id": f"{PARENT}-s2",
            "strategy_id": STRATEGY,
        }
    ]
    # the two inputs are bound into the generation
    names = {item.artifact_id for item in on_artifacts} - {
        item.artifact_id for item in off_artifacts
    }
    assert names == {
        "input/strategy-basis-declaration.json",
        "input/sleeve-account-facts.json",
    }
    assert (
        block["declaration_sha256"]
        == hashlib.sha256(world["declaration"].read_bytes()).hexdigest()
    )
    assert (
        block["sleeve_facts_sha256"]
        == hashlib.sha256(world["facts"].read_bytes()).hexdigest()
    )


def _float_statistics(account: list[float], reference: list[float]) -> dict[str, float]:
    """An independent recomputation of the comparison statistics."""

    n = len(account)
    active = [a - r for a, r in zip(account, reference)]
    mean_a, mean_r = sum(account) / n, sum(reference) / n
    var_r = sum((r - mean_r) ** 2 for r in reference) / n
    cov = sum((a - mean_a) * (r - mean_r) for a, r in zip(account, reference)) / n
    tracking = statistics.stdev(active) * math.sqrt(252)
    return {
        "beta": cov / var_r,
        "tracking_error_annualized": tracking,
        "active_return_annualized": sum(active) / n * 252,
        "information_ratio": sum(active) / n * 252 / tracking,
    }


def test_sleeve_lane_statistics_equal_an_independent_recomputation(
    tmp_path: Path,
) -> None:
    catalog, _, _ = _build(_world(tmp_path))
    sleeve = {row["date"]: float(row["return"]) for row in _sleeve_rows()}
    for key, reference in (
        ("benchmark_comparisons", BENCHMARK),
        ("comparisons", PROVIDER),
    ):
        lane = _row(catalog, key)["sleeve_basis"]
        metrics = lane["metrics"]
        expected = _float_statistics(
            [sleeve[d] for d in SLEEVE_DATES],
            [float(reference[d]) for d in SLEEVE_DATES],
        )
        for name, value in expected.items():
            assert float(metrics[name]) == pytest.approx(value, rel=1e-12)
        wealth = Decimal(1)
        for day in SLEEVE_DATES:
            wealth *= 1 + Decimal(reference[day])
        assert Decimal(metrics["reference_cumulative_return"]) == wealth - 1
        # the sleeve path's own compounded return
        compounded = Decimal(1)
        for row in _sleeve_rows():
            compounded *= 1 + Decimal(row["return"])
        assert Decimal(metrics["account_cumulative_return"]) == pytest.approx(
            compounded - 1, rel=Decimal("1e-20")
        )
        # closure: the subset hash is the hash of the stored reference values used
        subset = [
            {"date": d, "return": format(Decimal(reference[d]).normalize(), "f")}
            for d in SLEEVE_DATES
        ]
        assert (
            lane["closure"]["reference_subset_sha256"]
            == hashlib.sha256(canonical_json_bytes(subset)).hexdigest()
        )
        assert lane["closure"]["common_observations"] == 3
        assert lane["closure"]["sleeve_dates_without_reference"] == []


def test_the_window_is_the_exact_common_date_subset_inside_the_sleeve(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path)
    # a market holiday: the sleeve has an observation the reference does not
    reference = dict(BENCHMARK)
    del reference["2026-01-01"]
    lines = ["series_id,date,return,evidence_layer,version_ref,source_artifact_id"]
    for series_id, layer, version, values in (
        ("account", "ACCOUNT_LIVE", "v1", ACCOUNT),
        ("provider", "PROVIDER_LIVE", "v1", PROVIDER),
        ("benchmark", "OFFICIAL_VEHICLE", "benchmark:v1", reference),
    ):
        for day, value in sorted(values.items()):
            lines.append(
                f"{series_id},{day},{value},{layer},{version},{series_id}-source"
            )
    world["returns"].write_text("\n".join(lines) + "\n", encoding="utf-8")
    document = json.loads(world["spec"].read_text(encoding="utf-8"))
    document["return_source"]["expected_sha256"] = hashlib.sha256(
        world["returns"].read_bytes()
    ).hexdigest()
    world["spec"].write_text(json.dumps(document), encoding="utf-8")
    catalog, _, _ = _build(world)
    lane = _row(catalog, "benchmark_comparisons")["sleeve_basis"]
    assert lane["metrics"]["observations"] == 2
    assert lane["closure"]["sleeve_dates_without_reference"] == ["2026-01-01"]
    assert lane["closure"]["sleeve_observations_in_window"] == 3
    # the provider reference is complete: three pairs
    assert _row(catalog, "comparisons")["sleeve_basis"]["metrics"]["observations"] == 3


# --- switch off ---------------------------------------------------------------------


def test_without_a_declaration_the_catalog_is_unchanged(tmp_path: Path) -> None:
    world = _world(tmp_path)
    catalog, content, artifacts = _build(world, restated=False)
    assert "strategy_basis" not in catalog
    assert all("sleeve_basis" not in row for row in catalog["comparisons"])
    assert all("sleeve_basis" not in row for row in catalog["benchmark_comparisons"])
    assert not any("basis" in item.artifact_id for item in artifacts)
    # the builder before this branch (the keystone's bytes) produced exactly these bytes
    assert hashlib.sha256(content).hexdigest() == SWITCH_OFF_SHA256


def test_declaration_and_facts_are_supplied_together(tmp_path: Path) -> None:
    world = _world(tmp_path)
    with pytest.raises(attribution.AttributionValidationError, match="together"):
        attribution.build_catalog(
            world["baseline"],
            world["spec"],
            strategy_basis_declaration=world["declaration"],
        )
    with pytest.raises(attribution.AttributionValidationError, match="together"):
        attribution.build_catalog(
            world["baseline"], world["spec"], sleeve_facts=world["facts"]
        )


# --- closure and fail closed --------------------------------------------------------


def test_a_declared_strategy_must_be_a_baseline_strategy(tmp_path: Path) -> None:
    world = _world(tmp_path)
    _declaration(tmp_path, "no-such-strategy")
    with pytest.raises(attribution.AttributionValidationError, match="not a baseline"):
        _build(world)


def test_the_facts_parent_must_be_an_account_of_the_baseline_series(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path, parent="another-account")
    with pytest.raises(attribution.AttributionValidationError, match="parent account"):
        _build(world)


def test_a_receipt_bound_strategy_is_refused(tmp_path: Path) -> None:
    world = _world(tmp_path)
    baseline = attribution._baseline(world["baseline"])[0]  # noqa: SLF001
    with pytest.raises(attribution.AttributionValidationError, match="receipt-bound"):
        attribution._sleeve_series(  # noqa: SLF001
            world["declaration"], world["facts"], baseline, {STRATEGY: {}}
        )


def test_tampered_sleeve_facts_fail_closed(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world["facts"].write_bytes(
        world["facts"].read_bytes().replace(b'"nav_end', b'"nav_xnd')
    )
    with pytest.raises(attribution.AttributionValidationError):
        _build(world)


def _sleeve_series(rows: tuple[dict[str, Any], ...]) -> Any:
    return attribution._SleeveSeries(  # noqa: SLF001
        STRATEGY,
        f"{PARENT}-s2",
        PARENT,
        date.fromisoformat(OPENING),
        {date.fromisoformat(r["date"]): Decimal(r["return"]) for r in rows},
        strategy_basis.series_sha256(rows),
    )


def _comparison_spec(reference_id: str) -> dict[str, Any]:
    return {
        "account_series_id": "account",
        "disposition": "synthetic",
        "expected_legacy_cumulative_gap": None,
        "observed_end": "2026-01-02",
        "observed_start": "2025-12-29",
        "periods_per_year": 252,
        "reference_series_id": reference_id,
        "status": attribution.AVAILABLE_STATUS,
        "strategy_id": STRATEGY,
        "version_compatibility_status": "UNRESOLVED",
    }


def _reference() -> dict[str, dict[date, Decimal]]:
    return {
        "benchmark": {date.fromisoformat(d): Decimal(v) for d, v in BENCHMARK.items()}
    }


def test_a_reference_date_without_a_sleeve_observation_is_refused() -> None:
    rows = tuple(r for r in _sleeve_rows() if r["date"] != "2026-01-01")
    reference = _reference()
    with pytest.raises(
        attribution.AttributionValidationError, match="without a sleeve"
    ):
        attribution._sleeve_comparison(  # noqa: SLF001
            _comparison_spec("benchmark"), _sleeve_series(rows), reference, {}, {}
        )


def test_closure_refuses_statistics_that_are_not_on_the_recomputed_pairing() -> None:
    sleeve = _sleeve_series(_sleeve_rows())
    reference = _reference()["benchmark"]
    good = attribution._sleeve_comparison(  # noqa: SLF001
        _comparison_spec("benchmark"), sleeve, _reference(), {}, {}
    )
    metrics = good["metrics"]
    for field, value in (
        ("observations", metrics["observations"] + 1),
        ("observed_start", "2026-01-01"),
        ("observed_end", "2026-01-01"),
    ):
        with pytest.raises(attribution.AttributionValidationError, match="closure"):
            attribution._closure(  # noqa: SLF001
                _comparison_spec("benchmark"),
                sleeve,
                reference,
                {**metrics, field: value},
            )


# --- the report merge ---------------------------------------------------------------


def _strategies() -> list[dict[str, Any]]:
    return [{"strategy_id": f"strategy-{index}"} for index in range(1, 10)]


def _restatement() -> strategy_basis.SleeveRestatement:
    return strategy_basis.restatement_from_sleeve_facts(_facts(), STRATEGY)


def _merged(
    catalog: dict[str, Any],
    restatements: dict[str, strategy_basis.SleeveRestatement] | None,
) -> dict[str, Any]:
    rows = report._merge_attribution(  # noqa: SLF001
        _strategies(), catalog, basis_restatements=restatements
    )
    return next(row for row in rows if row["strategy_id"] == STRATEGY)


def test_report_row_is_sleeve_primary_with_a_labelled_account_block(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path)
    off_catalog, _, _ = _build(world, restated=False)
    on_catalog, _, _ = _build(world)
    off = _merged(off_catalog, None)
    on = _merged(on_catalog, {STRATEGY: _restatement()})

    benchmark_lane = _row(on_catalog, "benchmark_comparisons")["sleeve_basis"][
        "metrics"
    ]
    provider_lane = _row(on_catalog, "comparisons")["sleeve_basis"]["metrics"]
    assert (
        on["benchmark_account_cumulative_return"]
        == benchmark_lane["account_cumulative_return"]
    )
    assert on["benchmark_beta"] == benchmark_lane["beta"]
    assert on["provider_cumulative_gap"] == provider_lane["cumulative_gap"]
    assert on["benchmark_comparison_observations"] == 3
    assert on["comparison_basis"]["basis"] == "SLEEVE_EPOCH"
    assert on["comparison_basis"]["sleeve_account_id"] == f"{PARENT}-s2"
    assert on["comparison_basis"]["benchmark_closure"]["common_observations"] == 3

    # non-diminish: every account-basis comparison figure of the switched-off row is on
    # the restated row, by value, in the labelled account block under the same name
    for prefix, block in (
        ("benchmark", on["benchmark_account_basis"]),
        ("provider", on["provider_account_basis"]),
    ):
        assert block["basis"] == "PARENT_ACCOUNT_LIVE_SERIES"
        assert PARENT in block["label"]
        figures = [
            name
            for name in off
            if name.startswith(f"{prefix}_")
            and name
            not in {
                f"{prefix}_annualization",
                f"{prefix}_assignment_authority",
                f"{prefix}_assignment_status",
                f"{prefix}_comparison_reason",
                f"{prefix}_comparison_window",  # carried once, by the annualization
                f"{prefix}_effective_end",
                f"{prefix}_effective_start",
                f"{prefix}_evidence_status",
                f"{prefix}_id",
                f"{prefix}_rationale",
                f"{prefix}_version_ref",
                f"{prefix}_assignment_status",
            }
        ]
        assert figures
        for name in figures:
            assert block[name] == off[name], name
    # and the primary fields differ from the account basis (the pairing differs)
    assert on["benchmark_beta"] != off["benchmark_beta"]
    # a strategy that is not restated is untouched
    others_on = report._merge_attribution(  # noqa: SLF001
        _strategies(), on_catalog, basis_restatements={STRATEGY: _restatement()}
    )
    others_off = report._merge_attribution(_strategies(), off_catalog)  # noqa: SLF001
    assert [r for r in others_on if r["strategy_id"] != STRATEGY] == [
        r for r in others_off if r["strategy_id"] != STRATEGY
    ]


def test_report_merge_without_lanes_is_unchanged(tmp_path: Path) -> None:
    world = _world(tmp_path)
    catalog, _, _ = _build(world, restated=False)
    row = _merged(catalog, None)
    assert "comparison_basis" not in row
    assert "benchmark_account_basis" not in row
    assert "provider_account_basis" not in row


def test_report_merge_ties_the_lanes_to_its_own_restatement(tmp_path: Path) -> None:
    world = _world(tmp_path)
    off_catalog, _, _ = _build(world, restated=False)
    on_catalog, _, _ = _build(world)
    restated = {STRATEGY: _restatement()}
    # lanes without the report restating, and the report restating without lanes
    with pytest.raises(report.ReportValidationError, match="differ"):
        _merged(on_catalog, None)
    with pytest.raises(report.ReportValidationError, match="differ"):
        _merged(off_catalog, restated)
    # a catalog block bound to another sleeve path
    tampered = copy.deepcopy(on_catalog)
    tampered["strategy_basis"]["strategies"][0]["series_sha256"] = "0" * 64
    with pytest.raises(report.ReportValidationError, match="differs from the report"):
        _merged(tampered, restated)
    # a lane on another path than the block names
    tampered = copy.deepcopy(on_catalog)
    _row(tampered, "comparisons")["sleeve_basis"]["sleeve_series_sha256"] = "1" * 64
    with pytest.raises(report.ReportValidationError, match="not on the sleeve path"):
        _merged(tampered, restated)
    # a lane without its block
    tampered = copy.deepcopy(on_catalog)
    del tampered["strategy_basis"]
    with pytest.raises(report.ReportValidationError, match="without its basis block"):
        _merged(tampered, restated)
    # the report restated to rows other than the catalog's
    other = strategy_basis.SleeveRestatement(
        **{**_restatement().__dict__, "rows": _restatement().rows[:-1]}
    )
    with pytest.raises(report.ReportValidationError, match="differs from the report"):
        _merged(on_catalog, {STRATEGY: other})


def _schema_validator(name: str, definition: str) -> Draft202012Validator:
    schema = json.loads(
        (ROOT / "schemas" / "portfolio" / name).read_text(encoding="utf-8")
    )
    return Draft202012Validator(
        {"$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]}
    )


@pytest.mark.parametrize(
    "schema", ["report-data.schema.json", "control-panel-data.schema.json"]
)
def test_the_new_row_blocks_validate_against_both_closed_schemas(
    tmp_path: Path, schema: str
) -> None:
    catalog, _, _ = _build(_world(tmp_path))
    row = _merged(catalog, {STRATEGY: _restatement()})
    for key, definition in (
        ("comparison_basis", "comparison_sleeve_basis"),
        ("benchmark_account_basis", "benchmark_account_basis"),
        ("provider_account_basis", "provider_account_basis"),
    ):
        _schema_validator(schema, definition).validate(row[key])
    # closed: an invented field is refused
    with pytest.raises(Exception):
        _schema_validator(schema, "benchmark_account_basis").validate(
            {**row["benchmark_account_basis"], "invented": 1}
        )
    bound = _schema_validator(
        "report-data.schema.json", "strategy_basis_attribution_block"
    )
    bound.validate(catalog["strategy_basis"])
    # and the attribution catalog schema accepts the block and still rejects strangers
    attribution_schema = json.loads(
        (ROOT / "schemas" / "portfolio" / "strategy-attribution.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(attribution_schema["properties"]["strategy_basis"]).validate(
        catalog["strategy_basis"]
    )


def test_the_sleeve_window_label_opens_at_the_sleeve_opening_observation(
    tmp_path: Path,
) -> None:
    catalog, _, _ = _build(_world(tmp_path))
    lane = _row(catalog, "benchmark_comparisons")["sleeve_basis"]
    metrics = lane["metrics"]
    label = report._lane_label_metrics(metrics, lane)  # noqa: SLF001
    assert label["observed_start"] == OPENING
    assert metrics["observed_start"] == SLEEVE_DATES[0]
    assert report._lane_label_metrics({}, lane) == {}  # noqa: SLF001


# --- the real R2 bytes (env gated) --------------------------------------------------

# QE_AI624_R2_FACTS is the oracle facts file (pro rata split): the R2 regression
# reference.  QE_AI624_R21_FACTS is the rebuilt file (R2.1 lineage, whole-share
# corporate-action rule): its sleeve series is derived in the test, nothing is typed.
_R2 = {
    name: os.environ.get(name)
    for name in (
        "QE_AI624_R2_FACTS",
        "QE_AI624_R21_FACTS",
        "QE_AI624_R2_ATTRIBUTION_RETURNS",
        "QE_AI624_R2_ATTRIBUTION_SPEC",
    )
}


def _needs(facts_name: str) -> Any:
    names = (
        facts_name,
        "QE_AI624_R2_ATTRIBUTION_RETURNS",
        "QE_AI624_R2_ATTRIBUTION_SPEC",
    )
    return pytest.mark.skipif(
        not all(_R2[name] for name in names),
        reason=f"{', '.join(names)} are not set (real R2 bytes are never committed)",
    )


@_needs("QE_AI624_R2_FACTS")
def test_real_r2_sleeve_comparisons_close_and_equal_an_independent_computation() -> (
    None
):
    """R2 regression reference (pro rata split, oracle facts)."""

    _real_comparisons("QE_AI624_R2_FACTS", pinned=True)


@_needs("QE_AI624_R21_FACTS")
def test_real_r21_sleeve_comparisons_close_and_equal_an_independent_computation() -> (
    None
):
    """R2.1 lineage: the series is derived from the rebuilt facts, not pinned."""

    _real_comparisons("QE_AI624_R21_FACTS", pinned=False)


def _real_comparisons(facts_name: str, *, pinned: bool) -> None:
    facts = Path(str(_R2[facts_name])).read_bytes()
    spec = json.loads(Path(str(_R2["QE_AI624_R2_ATTRIBUTION_SPEC"])).read_text("utf-8"))
    series, identities = attribution._rows(  # noqa: SLF001
        Path(str(_R2["QE_AI624_R2_ATTRIBUTION_RETURNS"]))
    )
    declared = "p123-ai-factor"
    restated = strategy_basis.restatement_from_sleeve_facts(facts, declared)
    sleeve = {
        date.fromisoformat(r["date"]): Decimal(r["return"]) for r in restated.rows
    }
    if pinned:
        assert len(sleeve) == 164 and restated.opening_date == "2026-01-30"
    else:
        # derived: the rebuilt series has its own sha and the same trading days
        assert len(sleeve) == len(restated.rows) > 0
    lane_source = attribution._SleeveSeries(  # noqa: SLF001
        declared,
        restated.account_id,
        restated.parent_account_id,
        date.fromisoformat(restated.opening_date),
        sleeve,
        strategy_basis.series_sha256(restated.rows),
    )
    if pinned:
        assert lane_source.series_sha256 == (
            "4243e0268646c7b542094951ce6b7f6fb5b92d39b041780512bcd196abd04a77"
        )
    else:
        assert lane_source.series_sha256 == strategy_basis.series_sha256(
            strategy_basis.restatement_from_sleeve_facts(facts, declared).rows
        )
        oracle = (
            strategy_basis.restatement_from_sleeve_facts(
                Path(str(_R2["QE_AI624_R2_FACTS"])).read_bytes(), declared
            )
            if _R2["QE_AI624_R2_FACTS"]
            else None
        )
        if oracle is not None:
            assert lane_source.series_sha256 != strategy_basis.series_sha256(
                oracle.rows
            )
    for key in ("benchmark_comparisons", "comparisons"):
        row = next(r for r in spec[key] if r["strategy_id"] == declared)
        lane = attribution._sleeve_comparison(  # noqa: SLF001
            row, lane_source, series, {}, identities
        )
        reference = series[row["reference_series_id"]]
        start = date.fromisoformat(row["observed_start"])
        end = date.fromisoformat(row["observed_end"])
        common = sorted(d for d in sleeve if d in reference and start <= d <= end)
        metrics = lane["metrics"]
        assert (
            metrics["observations"]
            == len(common)
            == lane["closure"]["common_observations"]
        )
        # every sleeve date up to the window end that the reference lacks is a market
        # holiday on which the sleeve return is below one basis point
        for missing in lane["closure"]["sleeve_dates_without_reference"]:
            assert abs(sleeve[date.fromisoformat(missing)]) < Decimal("0.0001")
        # no reference trading date inside the window lacks a sleeve observation
        # (the closure refuses otherwise; stated here from the data)
        inside = [
            d
            for d in reference
            if date.fromisoformat(restated.opening_date) < d <= common[-1]
        ]
        assert all(d in sleeve for d in inside)
        expected = _float_statistics(
            [float(sleeve[d]) for d in common], [float(reference[d]) for d in common]
        )
        for name, value in expected.items():
            assert float(metrics[name]) == pytest.approx(value, rel=1e-12)
        wealth = Decimal(1)
        for d in common:
            wealth *= 1 + reference[d]
        assert Decimal(metrics["reference_cumulative_return"]) == pytest.approx(
            wealth - 1, rel=Decimal("1e-20")
        )
        window_wealth = Decimal(1)
        for d in sorted(d for d in sleeve if start <= d <= end):
            window_wealth *= 1 + sleeve[d]
        assert Decimal(metrics["account_cumulative_return"]) == pytest.approx(
            window_wealth - 1, rel=Decimal("1e-20")
        )


# --- the windows follow the catalog's own reference windows -------------------------


@pytest.mark.parametrize("window_end", ["2026-01-01", "2026-01-02"])
def test_the_sleeve_windows_equal_the_catalogs_own_reference_windows(
    tmp_path: Path, window_end: str
) -> None:
    world = _world(tmp_path, window_end=window_end)
    catalog, _, _ = _build(world)
    spec = json.loads(world["spec"].read_text(encoding="utf-8"))
    sleeve_dates = {row["date"] for row in _sleeve_rows()}
    for key, reference in (
        ("benchmark_comparisons", BENCHMARK),
        ("comparisons", PROVIDER),
    ):
        row = next(r for r in spec[key] if r["strategy_id"] == STRATEGY)
        # the catalog's own reference window, from the spec and the stored series
        window = sorted(
            d
            for d in reference
            if row["observed_start"] <= d <= row["observed_end"] and d in sleeve_dates
        )
        lane = _row(catalog, key)["sleeve_basis"]
        assert lane["metrics"]["observed_start"] == window[0]
        assert lane["metrics"]["observed_end"] == window[-1]
        assert lane["metrics"]["observations"] == len(window)
        assert lane["closure"]["common_observations"] == len(window)


def test_the_window_limit_is_derived_from_the_window_and_the_cutoff(
    tmp_path: Path,
) -> None:
    cutoff = "2026-01-03"
    limits = {}
    closures = {}
    for window_end in ("2026-01-01", "2026-01-02"):
        sub = tmp_path / window_end
        sub.mkdir()
        catalog, _, _ = _build(_world(sub, window_end=window_end))
        row = _merged(
            catalog,
            {STRATEGY: _restatement()},
        )
        limits[window_end] = row["comparison_basis"]["benchmark_window_limit"]
        closures[window_end] = row["comparison_basis"]["benchmark_closure"]
        # the window labels the payload carries are the lane's own start and end
        merged = report._merge_attribution(  # noqa: SLF001
            _strategies(),
            catalog,
            cost_rate_at_cutoff={
                "calculation_cutoff_date": cutoff,
                "cost_schedule_as_of_date": cutoff,
            },
            basis_restatements={STRATEGY: _restatement()},
        )
        labelled = next(r for r in merged if r["strategy_id"] == STRATEGY)
        window = report._label_window(labelled["benchmark_annualization"])  # noqa: SLF001
        limit = labelled["comparison_basis"]["benchmark_window_limit"]
        assert window["start"] == limit["window_start"] == OPENING
        assert window["end"] == limit["window_end"] == window_end
        assert window["observations"] == labelled["benchmark_comparison_observations"]
        provider_window = report._label_window(  # noqa: SLF001
            labelled["provider_annualization"]
        )
        assert provider_window["end"] == window_end
    short, full = limits["2026-01-01"], limits["2026-01-02"]
    assert short["status"] == "SHORT_OF_CUTOFF" and short["days_short"] == 2
    # carried once, in the closure beside the limit
    assert "uncovered_sleeve_dates" not in short
    assert (
        closures["2026-01-01"]["sleeve_dates_after_window_count"],
        closures["2026-01-01"]["sleeve_dates_after_window_first"],
        closures["2026-01-01"]["sleeve_dates_after_window_last"],
    ) == (1, "2026-01-02", "2026-01-02")
    assert "2 days before the calculation cutoff 2026-01-03" in short["sentence"]
    assert (
        "R3" in short["sentence"]
        and closures["2026-01-01"]["reference_series_id"] in short["sentence"]
    )
    assert full["status"] == "SHORT_OF_CUTOFF" and full["days_short"] == 1
    assert closures["2026-01-02"]["sleeve_dates_after_window_count"] == 0
    # a window that reaches the cutoff says so
    reaching = report._window_limit(  # noqa: SLF001
        {
            "metrics": {"observed_end": cutoff},
            "closure": {
                "reference_series_id": "benchmark",
                "reference_stored_end": cutoff,
                "sleeve_dates_after_window_count": 0,
                "sleeve_dates_after_window_first": None,
                "sleeve_dates_after_window_last": None,
                "window_open_date": OPENING,
            },
        },
        cutoff,
        "benchmark",
    )
    assert reaching is not None
    assert reaching["status"] == "AT_CUTOFF" and reaching["days_short"] == 0
    assert report._window_limit({"metrics": None, "closure": None}, cutoff, "x") is None  # noqa: SLF001
