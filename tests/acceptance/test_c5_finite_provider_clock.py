"""C5 (#895 F2): a finite-boundary provider with no active account is not a source-clock row.

The exemption is recomputed from the authoritative crosswalk (a provider the crosswalk shows
with no account active at the cutoff); the producer's label alone grants nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_validate_generation import (
    REPORT_GEN,
    Spec,
    _append,
    _crosswalk,
    build,
    run,
)

FINITE_ROW = {
    "source_id": "alpaca",
    "provider_data_date": "2025-11-24",
    "applies_to_latest_common_eod": False,
    "finality": {"basis": "FINITE_SELECTED_MANIFEST_BOUNDARY"},
}


def _crosswalk_with_providers(extra: list[dict[str, Any]]):
    def write(gen_dir: Path) -> None:
        root = gen_dir.parents[2]
        path = (
            root
            / "reporting"
            / "generations"
            / REPORT_GEN
            / "input"
            / "account-strategy-current-epoch-crosswalk.json"
        )
        document = _crosswalk()
        for item in document["rows"]:
            item["provider"] = "IBKR"
        document["rows"] += extra
        path.write_bytes(json.dumps(document, sort_keys=True).encode())

    return write


def _alpaca_account(*, active: bool) -> dict[str, Any]:
    return {
        "account_id": "AL1",
        "strategy_id": "gamma",
        "provider": "ALPACA",
        "effective_start": "2025-01-01",
        "effective_end": None if active else "2025-11-24",
        "product_epoch_disposition": (
            "INCLUDED_PRODUCT_EPOCH" if active else "EXCLUDED_DEPRECATED_HISTORY_ONLY"
        ),
    }


def _source_clock_rows(tmp_path: Path, row: dict[str, Any], *, active: bool):
    spec = Spec(
        report=[_append(("source_clock", "source_data_as_of"), row)],
        post_write=[_crosswalk_with_providers([_alpaca_account(active=active)])],
    )
    return [
        f
        for f in run(build(tmp_path, spec))
        if f.check_id == "dates.source_clock" and f.entity == "alpaca"
    ]


def test_finite_provider_with_no_active_account_is_silent(tmp_path):
    assert _source_clock_rows(tmp_path, FINITE_ROW, active=False) == []


def test_finite_provider_with_an_active_account_is_still_not_ready(tmp_path):
    found = _source_clock_rows(tmp_path, FINITE_ROW, active=True)
    assert [f.klass for f in found] == ["NOT_READY"]


def test_a_label_without_the_finite_basis_grants_nothing(tmp_path):
    row = {**FINITE_ROW, "finality": {"basis": "PROVIDER_FINAL"}}
    found = _source_clock_rows(tmp_path, row, active=False)
    assert [f.klass for f in found] == ["NOT_READY"]


def test_a_provider_the_crosswalk_never_names_is_not_exempt(tmp_path):
    # No account is ever recorded for this provider: nothing proves it feeds no active
    # strategy, so the finding stays.
    row = {**FINITE_ROW, "source_id": "fidelity"}
    spec = Spec(
        report=[_append(("source_clock", "source_data_as_of"), row)],
        post_write=[_crosswalk_with_providers([])],
    )
    found = [
        f
        for f in run(build(tmp_path, spec))
        if f.check_id == "dates.source_clock" and f.entity == "fidelity"
    ]
    assert [f.klass for f in found] == ["NOT_READY"]
