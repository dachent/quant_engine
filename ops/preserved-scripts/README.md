# Preserved scripts

206 scripts that existed only in `C:\Dev\scratch\quant-engine`, `C:\Dev\scratch\quant_engine`
and the work-folder root, recovered before those trees were deleted on 2026-09-15.

**This is a preservation commit, not a design.** Paths mirror where each file was found, so
provenance is legible. Nothing here has been reviewed, tested, or made importable.

## Why this exists

Two classes of load-bearing code were untracked:

**Capture drivers — the tools that acquire provider data.** `packages/web-contracts/` holds the
parsers, but nothing that drives a browser session was in the repository:

| script | provider |
|---|---|
| `p123_har_capture.py` | Portfolio123 — CDP session capture |
| `p123_har_to_csv.py`, `p123_har_to_csv_extended.py` | Portfolio123 — HAR to series |
| `extract_legacy_equity_curve.py`, `extract_managed_equity_curve.py` | Portfolio123 — curve extraction |
| `schwab_playwright_capture.py` | Schwab |
| `capture_robuxio.py`, `capture_robuxio_benchmarks.py` | Robuxio |

Deleting scratch would have destroyed the ability to refresh any of these sources.

**Producers of governed inputs.** `ops/mvp2-release/inputs/` preserves the epoch crosswalk, the
account-interval receipt authority, the HWM governed result and calendar authority, and the
invested-capital result — but the scripts that *build* them (`build_capital_input.py`,
`build_hwm_calendar.py`, `build_hwm_result.py`, `sync_contract_pins.py`,
`decomposition_input_index.py`, and the correction-registry builders) lived only in
`pl12/epoch/`. Outputs were preserved, producers were not. The same defect, twice.

The rest are one-shot patch and test-authoring scripts from the MVP-2 build. Their effect is in
git history; they are kept because deciding which of 194 scripts mattered was the kind of
recall-based judgement that produced this mess in the first place. 2.8 MB is cheaper than being
wrong.

## What should happen to this directory

It should shrink to nothing. `p123_har_capture.py` is the subject of
[#422](https://github.com/dachent/quant_engine/issues/422) — it records the response body of
every request with no filter, so 65.1% of a capture is JavaScript and 2.0% is the JSON the
pipeline reads. Fixing that means the capture layer moves into `packages/web-contracts/` with
`full` and `targets` modes, and these drivers become thin declarations.

[#414](https://github.com/dachent/quant_engine/issues/414) covers the orchestration the
`pl12/epoch` builders belong in. [#421](https://github.com/dachent/quant_engine/issues/421) is
the RCCA for why any of this was outside the repository at all, and
[#423](https://github.com/dachent/quant_engine/issues/423) is the cleanup runbook whose
durability-verification phase is what caught it this time — one step before the delete, rather
than after.
