"""interaction_qa.mjs must follow the panel it checks.

Release B (int14) failed two interaction checks that were stale, not regressions:

* PR 966/975 made Max the common window (rebased at the group anchor) with an explicit
  Own start basis; the QA still demanded the pre-966 Max (log axis, "own first week").
* The factor state-separation check clicked the *last* strategy chip and needed the
  Source state / Model state panels, which render only for a diagnostic or unavailable
  chip.  Whether the last chip is one depends on sealed-holdout results (Volatis moved
  from -0.51 to +0.08 predicted R2 between A5 and int14), not on the panel.
"""

from __future__ import annotations

from pathlib import Path

QA = Path(__file__).resolve().parents[2] / "apps" / "portfolio-control-panel" / "interaction_qa.mjs"


def _source() -> str:
    return QA.read_bytes().decode("utf-8")


def _block(text: str, start: str, length: int) -> str:
    index = text.index(start)
    return text[index : index + length]


def test_max_check_expects_the_common_window_then_the_own_start_basis() -> None:
    text = _source()
    block = _block(text, 'await page.locator(\'[data-nav="performance"]\').click();', 3200)
    # Default Max is the common window and the log axis is not forced on it.
    assert "Common window" in block
    # Own start is reached through the explicit basis control and is the log, own-first-week view.
    assert 'data-perf-basis="own"' in block
    assert "own first week" in block


def test_max_check_no_longer_demands_a_forced_log_axis_by_default() -> None:
    text = _source()
    check = _block(text, "Return chart reads the union grid", 900)
    assert "Common window" in check
    # The pre-966 expectation: the default Max view is a log axis with own-first-week note.
    assert 'returnsMax.axisLogPressed === "true" && /own first week/.test(returnsMax.note)' not in check


def test_factor_state_check_selects_a_chip_that_has_state_panels() -> None:
    text = _source()
    assert '[data-factor-strategy]").last().click()' not in text, (
        "the state-separation check must not click an arbitrary last chip"
    )
    block = _block(text, "Factor source and model states remain separate", 900)
    assert "is-diagnostic" in text and "is-unavailable" in text
    # With no diagnostic or unavailable chip the check is not applicable, and says so in its evidence.
    assert "state_chips" in block
