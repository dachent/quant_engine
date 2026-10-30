"""Model vs actual (mockup C, #595): the view reads the producer's real model_vs_actual block."""

from __future__ import annotations

import copy
from pathlib import Path
import re
from typing import Any

from synthetic_r21_blocks import model_vs_actual_block
from test_panel_render_audit_543 import base, call, run


def _block() -> dict[str, Any]:
    return copy.deepcopy(model_vs_actual_block())


def _plain(markup: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup)).strip()


def _root(tmp_path: Path, payload: dict[str, Any]) -> str:
    dom = run(tmp_path, payload, calls=[{"fn": "renderModelVsActual", "args": []}])[
        "dom"
    ]
    return str(dom["#mva-root"]["innerHTML"])


def _detail(tmp_path: Path, block: dict[str, Any], strategy: int, scope: str) -> str:
    strategies = block["strategies"]
    chosen = strategies[strategy]
    window = next(
        (w for w in chosen["windows"] if w["window_id"] == chosen["primary_window_id"]),
        None,
    )
    return str(
        call(
            tmp_path,
            base(model_vs_actual=block),
            "mvaDetail",
            block,
            chosen,
            window,
            scope,
        )
    )


def test_totals_table_lists_every_strategy_with_tags_and_a_dollar_total(
    tmp_path: Path,
) -> None:
    block = _block()
    root = _root(tmp_path, base(model_vs_actual=block))
    text = _plain(root)

    for strategy in block["strategies"]:
        assert strategy["display_name"] in text
    assert "Portfolio, dollar-weighted" in text
    gap = abs(round(block["portfolio_bridge"]["account_gap_usd"]))
    assert f"{gap:,}" in text
    assert "Audit derived" in text
    assert block["sign_convention"] in text
    assert "±1.5 pp (1σ)" in text
    assert 'id="mva-total-table"' in root


def test_a_tagged_row_is_never_printed_as_evidenced(tmp_path: Path) -> None:
    block = _block()
    detail = _detail(tmp_path, block, 1, "strategy")

    assert "Audit derived" in detail
    # every bridge component row carries its own tag badge
    assert detail.count("mva-tag-audit-derived") + detail.count(
        "mva-tag-estimated"
    ) >= len(block["strategies"][1]["windows"][0]["bridge"])


def test_strategy_detail_prints_the_residual_closure_and_memo_lines(
    tmp_path: Path,
) -> None:
    block = _block()
    text = _plain(_detail(tmp_path, block, 1, "strategy"))

    assert "Components sum to" in text and "residual" in text
    assert "Memo lines (outside the closure)" in text
    assert "Governed terms" in text and "Financing interest" in text


def test_estimated_dollars_and_uncertainty_are_labelled(tmp_path: Path) -> None:
    detail = _detail(tmp_path, _block(), 0, "strategy")

    assert "estimated dollars" in detail
    assert "±1.5 pp (1σ)" in detail


def test_audit_derived_rows_name_the_pull_that_replaces_them(tmp_path: Path) -> None:
    block = _block()
    detail = _plain(_detail(tmp_path, block, 1, "strategy"))

    titles = {pull["pull_id"]: pull["title"] for pull in block["pulls"]}
    bridge = block["strategies"][1]["windows"][0]["bridge"]
    pull_id = next(c["replacing_pull"] for c in bridge if c["replacing_pull"])
    assert f"replaced by: {titles[pull_id]}" in detail
    assert 'title="source: ' in _detail(tmp_path, block, 1, "strategy")


def test_unavailable_terms_print_reason_and_pull(tmp_path: Path) -> None:
    block = _block()
    text = _plain(_detail(tmp_path, block, 1, "strategy"))

    assert "Gap to cutoff" in text
    assert "No model leg after 2026 09 11" in text
    assert "Not available" in text


def test_portfolio_bridge_states_the_windows_and_closure(tmp_path: Path) -> None:
    block = _block()
    text = _plain(_detail(tmp_path, block, 0, "portfolio"))

    assert "Each strategy primary window not a common window" in text
    assert "Windows:" in text
    assert "closes within" in text or "does not close" in text


def test_epoch_table_lists_epochs_with_main_drivers(tmp_path: Path) -> None:
    block = _block()
    detail = _detail(tmp_path, block, 1, "epoch")

    assert 'id="mva-epoch-table"' in detail
    assert detail.count("<tr>") == len(block["strategies"][1]["epochs"]) + 1


def test_a_strategy_without_epochs_says_so(tmp_path: Path) -> None:
    text = _plain(_detail(tmp_path, _block(), 2, "epoch"))

    assert "No per-epoch rows were published for this strategy." in text


def test_missing_and_unavailable_block_are_explicit(tmp_path: Path) -> None:
    missing = _plain(_root(tmp_path, base()))
    assert "not in this snapshot" in missing and "has not published" in missing

    block = {"schema_version": 1, "status": "UNAVAILABLE", "reason": "NO_AUDIT_INPUT"}
    text = _plain(_root(tmp_path, base(model_vs_actual=block)))
    assert "No audit input" in text


def _windowless(block: dict[str, Any]) -> int:
    return next(
        index
        for index, strategy in enumerate(block["strategies"])
        if not strategy["windows"]
    )


def test_a_strategy_without_an_aligned_window_still_renders_its_epochs(
    tmp_path: Path,
) -> None:
    block = _block()
    index = _windowless(block)
    assert block["strategies"][index]["primary_window_id"] is None

    root = _plain(_root(tmp_path, base(model_vs_actual=block)))
    assert "No aligned window within the cutoff" in root
    assert "Window ends after cutoff" in root

    detail = _plain(_detail(tmp_path, block, index, "strategy"))
    assert "No aligned window within the cutoff." in detail

    epochs = _detail(tmp_path, block, index, "epoch")
    assert 'id="mva-epoch-table"' in epochs
