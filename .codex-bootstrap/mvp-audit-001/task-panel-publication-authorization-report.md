# MVP-AUDIT-001 panel publication authorization report

Status: PARTIAL — receipt authority seam and release-builder handoff implemented;
broader legacy direct-publish regression suite remains to be converted to supply
frozen receipts before acceptance can be claimed.

Root cause: a staged panel generation could previously advance `CURRENT.json`
and the stable launcher without a release-builder-bound authorization artifact.

Contract: the builder writes a create-only canonical receipt at
`stage-results/panel-publication-authorization.json`, binding exact resolved
output, candidate, and frozen QA directory identities plus canonical
recipe/pipeline/release-commit release ID. Panel publication reads it once after
immutable staging and immediately before pointer/launcher inspection, rejects
all malformed or mismatched identities, and returns its hash and release ID.
This is an honest local-control guard, not a security boundary against a local
principal able to write the receipt and output trees.

Files: `tools/portfolio_control_panel.py`, `tools/portfolio_release_builder.py`,
`tests/portfolio_control_panel/test_portfolio_control_panel.py`,
`tests/core/test_portfolio_release_builder.py`, and
`docs/portfolio-control-panel.md`.

RED/GREEN: initial no-receipt test exposed a missing parameter binding; after
correction, focused missing-receipt/CLI tests passed (2 passed) and the focused
release-builder handoff test passed (1 passed). Ruff and mypy passed on the
changed Python files.

Uncertainty/blocker: the full legacy direct `publish_panel` successes have not
yet all been converted to provide frozen receipt fixtures; full panel and
release-builder regressions were intentionally not claimed.

Next action: update every successful direct publish test with an independently
computed frozen candidate/QA receipt, add mutation/link cases, then run bounded
panel/release-builder regression gates and final checkpoint revalidation.
