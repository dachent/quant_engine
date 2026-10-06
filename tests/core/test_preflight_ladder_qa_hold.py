"""Rung (c): the QA hold is explicit in the plan and in execute."""

from __future__ import annotations

from pathlib import Path
import time

import pytest

import portfolio_pipeline_materialize as materializer
from preflight_ladder_fixture import (
    REFUSAL_SECONDS,
    ROOT,
)

pytestmark = pytest.mark.real_attended_path_budget

# ---------------------------------------------------------------- (c) QA hold


def test_plan_names_the_qa_stage_and_required_flag():
    plan = materializer._attended_qa_plan(materializer.ATTENDED_STAGES, None)
    assert plan["stage"] == "panel"
    assert plan["required_flag"] == "--qa-dir"
    assert plan["supplied"] is False
    assert "--qa-dir" in plan["instructions"] and "--resume" in plan["instructions"]
    supplied = materializer._attended_qa_plan(
        materializer.ATTENDED_STAGES, Path("C:\\Dev\\scratch\\qa")
    )
    assert supplied["supplied"] is True


def test_view_carries_the_qa_plan():
    view = materializer._attended_view(
        Path("C:\\Dev\\scratch\\nonexistent-out"),
        materializer.ATTENDED_STAGES,
        {},
        [],
        qa_dir=None,
    )
    assert view["qa"]["required_flag"] == "--qa-dir"


def test_execute_refuses_a_panel_plan_without_qa_dir_or_declared_hold():
    started = time.monotonic()
    with pytest.raises(materializer.MaterializationError, match=r"--qa-dir.*--qa-hold"):
        materializer._attended_require_qa_plan(
            materializer.ATTENDED_STAGES, qa_dir=None, qa_hold=False
        )
    assert time.monotonic() - started < REFUSAL_SECONDS


def test_execute_admits_qa_dir_or_declared_hold():
    materializer._attended_require_qa_plan(
        materializer.ATTENDED_STAGES, qa_dir=Path("C:\\qa"), qa_hold=False
    )
    materializer._attended_require_qa_plan(
        materializer.ATTENDED_STAGES, qa_dir=None, qa_hold=True
    )
    # A plan with no panel stage needs no hold.
    materializer._attended_require_qa_plan(
        ("ledger", "baseline"), qa_dir=None, qa_hold=False
    )


def test_qa_hold_flag_is_parsed():
    args = materializer._parser().parse_args(
        [
            "attended",
            "--repo-root", "r", "--python", "p", "--workspace-root", "w",
            "--output-root", "o", "--retained-cache-root", "c",
            "--requested-as-of", "2026-09-17", "--source-run", "s", "--qa-hold",
        ]
    )  # fmt: skip
    assert args.qa_hold is True


@pytest.mark.parametrize(
    "document", ["docs/attended-run-storage.md", "docs/attended-generation-runbook.md"]
)
def test_docs_state_the_qa_prerequisite(document):
    text = (ROOT / document).read_text(encoding="utf-8")
    assert "--qa-dir" in text and "--qa-hold" in text


def test_storage_doc_invocation_declares_the_hold_or_qa_dir():
    text = (ROOT / "docs" / "attended-run-storage.md").read_text(encoding="utf-8")
    invocation = next(
        line
        for line in text.splitlines()
        if line.startswith("python tools/portfolio_pipeline_materialize.py attended")
        and "--execute" in line
    )
    assert "--qa-dir" in invocation or "--qa-hold" in invocation
