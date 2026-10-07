from __future__ import annotations

import portfolio_report as reporter


CONTROLS = {
    "current": {"product_requirement": "REQUIRED"},
    "retired": {"product_requirement": "EXCLUDED"},
}


def _catalog() -> dict[str, object]:
    def rows(kind: str) -> list[dict[str, str]]:
        return [
            {"strategy_id": "current", "kind": kind},
            {"strategy_id": "retired", "kind": kind},
        ]

    return {
        "benchmark_assignments": rows("assignment"),
        "benchmark_comparisons": rows("benchmark"),
        "comparisons": rows("comparison"),
        "costs": rows("cost"),
    }


def test_every_attribution_list_applies_the_current_product_filter() -> None:
    """benchmark_comparisons was the only unfiltered sibling (9 rows against 7)."""

    section = reporter._strategy_attribution_section(
        _catalog(), {"generation_id": "g"}, CONTROLS
    )

    for key in (
        "benchmark_assignments",
        "benchmark_comparisons",
        "comparisons",
        "costs",
    ):
        assert [row["strategy_id"] for row in section[key]] == ["current"], key
    assert section["generation_id"] == "g"


def test_benchmark_comparisons_stay_optional_for_older_catalogs() -> None:
    catalog = _catalog()
    del catalog["benchmark_comparisons"]

    section = reporter._strategy_attribution_section(
        catalog, {"generation_id": "g"}, CONTROLS
    )

    assert section["benchmark_comparisons"] == []


def test_unknown_strategy_in_benchmark_comparisons_fails_closed() -> None:
    catalog = _catalog()
    catalog["benchmark_comparisons"] = [{"strategy_id": "stranger"}]

    try:
        reporter._strategy_attribution_section(
            catalog, {"generation_id": "g"}, CONTROLS
        )
    except reporter.ReportValidationError as error:
        assert "benchmark_comparisons" in str(error)
    else:
        raise AssertionError("an unknown product strategy must be rejected")
