"""Rung (a): every retained identity binds the run's provider ledger."""

from __future__ import annotations

import time

import pytest

import check_retained_authority_pins as pins
import portfolio_pipeline_materialize as materializer
from preflight_ladder_fixture import (
    REFUSAL_SECONDS,
    _json,
    _matrix,
    _refuses,
    _sha,
    make_attribution,
    make_baseline,
    make_ledger,
)

# ---------------------------------------------------------------- (a) pin matrix


def test_pin_matrix_passes_when_every_retained_identity_binds_the_run_ledger(good):
    rows = _matrix(good)
    assert {row["role"]: row["status"] for row in rows} == {
        "allocation": "MATCH",
        "attribution": "MATCH",
        "baseline": "MATCH",
        "dependence": "MATCH",
    }


def test_pin_matrix_ignores_unrelated_source_rows(good):
    # The custody-authority row is not ledger identity; it must never trip the matrix.
    assert _matrix(good)


def test_allocation_pinned_to_an_older_ledger_is_refused_naming_both(good, tmp_path):
    old = make_ledger(tmp_path / "old-ledger" / ("9" * 64), "v5")
    stale = make_baseline(tmp_path / "stale-allocation" / ("c" * 64), old)
    seconds = _refuses(
        lambda: _matrix(good, allocation=stale),
        r"allocation provider ledger identity differs.*"
        + _sha(b"manifest-v5")[:12]
        + r".*"
        + _sha(b"manifest-v6")[:12],
    )
    print(f"refusal seconds (allocation): {seconds:.3f}")


def test_retained_baseline_with_an_older_ledger_is_refused(good, tmp_path):
    old = make_ledger(tmp_path / "old-ledger" / ("9" * 64), "v5")
    stale = make_baseline(tmp_path / "stale-baseline" / ("b" * 64), old)
    seconds = _refuses(
        lambda: _matrix(good, baseline=stale),
        r"baseline provider ledger identity differs",
    )
    print(f"refusal seconds (baseline): {seconds:.3f}")


def test_attribution_built_on_another_baseline_is_refused_naming_both(good, tmp_path):
    other = make_baseline(
        tmp_path / "other-baseline" / ("0" * 5 + "2f12" + "0" * 55),
        good["ledger"],
    )
    stale = make_attribution(tmp_path / "attr2" / ("d" * 64), other)
    seconds = _refuses(
        lambda: _matrix(good, attribution=stale),
        r"attribution baseline identity differs.*"
        + other.name
        + r".*"
        + good["baseline"].name,
    )
    print(f"refusal seconds (attribution): {seconds:.3f}")


def test_dependence_selection_on_another_ledger_is_refused(good, tmp_path):
    old = make_ledger(tmp_path / "old-ledger" / ("9" * 64), "v5")
    _refuses(
        lambda: _matrix(good, dependence=old),
        r"dependence provider ledger identity differs",
    )


def test_missing_ledger_rows_are_refused_not_skipped(good, tmp_path):
    empty = tmp_path / "empty" / ("c" * 64)
    empty.mkdir(parents=True)
    (empty / "portfolio-baseline.json").write_bytes(_json({"source_artifacts": []}))
    _refuses(
        lambda: _matrix(good, allocation=empty), r"carries no provider ledger identity"
    )


def test_pin_matrix_cli_refuses_and_passes(good, tmp_path, capsys):
    base = [
        "--ledger-root",
        str(good["ledger"]),
        "--retained",
        f"allocation={good['allocation']}",
        "--retained",
        f"baseline={good['baseline']}",
        "--retained",
        f"attribution={good['attribution']}",
        str(tmp_path),
    ]
    assert pins.main(base) == 0
    old = make_ledger(tmp_path / "old-ledger" / ("9" * 64), "v5")
    stale = make_baseline(tmp_path / "stale" / ("c" * 64), old)
    bad = [
        item.replace(f"allocation={good['allocation']}", f"allocation={stale}")
        for item in base
    ]
    assert pins.main(bad) == 1
    assert "allocation provider ledger identity differs" in capsys.readouterr().out


def test_attended_preflight_helper_raises_materialization_error(good, tmp_path):
    materializer._attended_pin_matrix(
        good["ledger"],
        allocation_root=good["allocation"],
        dependence_ledger_root=good["dependence"],
    )
    old = make_ledger(tmp_path / "old-ledger" / ("9" * 64), "v5")
    stale = make_baseline(tmp_path / "stale" / ("c" * 64), old)
    started = time.monotonic()
    with pytest.raises(
        materializer.MaterializationError,
        match="allocation provider ledger identity differs",
    ):
        materializer._attended_pin_matrix(
            good["ledger"],
            allocation_root=stale,
            dependence_ledger_root=good["dependence"],
        )
    assert time.monotonic() - started < REFUSAL_SECONDS
