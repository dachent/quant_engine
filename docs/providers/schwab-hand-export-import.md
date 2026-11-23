# Schwab hand-export import (fallback only)

The governed path for Schwab data is a live `tools/schwab_capture_driver.py` run followed by
`tools/schwab_capture_ingest.py`. When the driver cannot be run in time (after the 09:30 ET first
price update, or the owner signed in too late), the owner's own exports can be imported with
`tools/schwab_hand_export_import.py`. The result is labelled `HAND_EXPORT` and is **not
release-eligible**: a release needs the live driver run, so the finality replay input is a real
provider response and not one rebuilt by hand. A hand-export generation is replaced by the live
run's generation (remove it from the world first: one generation only).

## Inputs

Per account, three files in one exports directory (the suffix is the account's last three
characters):

| file | pattern |
|---|---|
| Transaction History CSV, all time | `*XXX<suffix>_Transactions_*.csv` |
| Account Value History CSV | `*XXXX<suffix>_Balances_*.CSV` |
| live positions page text, with a `CAPTURED_LOCAL: <iso>` stamp | `schwab-positions-<suffix>.txt` |

and the retained ingest directory of the last governed generation (`transactions/<suffix>.json`,
`nav/<suffix>.csv`).

## Run

```
python tools/schwab_hand_export_import.py --exports-dir <exports> --retained-dir <retained> \
    --out-dir <new empty dir> --accounts 123 456 --cutoff 2026-11-19 --capture-day 2026-11-20
```

Output is create-only. Exit code 2 prints `REFUSED: <reason>` and writes nothing.

## What it checks (each is a refusal)

1. Every retained transaction row is still in the CSV (multiset on date, action, symbol,
   description, quantity, price, fees, amount).
2. No new row is older than the newest retained row; rows after the capture day are dropped.
3. The balances export has a value on the cutoff day.
4. Positions at the page's prior close plus the sweep value equal the Account Value History value at
   the cutoff to the cent.

NAV days where the export disagrees with the retained value are reported under `nav_conflicts` and
kept as retained; `--apply-nav-conflicts` applies them and needs the owner's or control's decision
(provider restatements are disclosed, not silently applied).

## Output

`transactions/<suffix>.json` (retained rows untouched; new rows flagged `csvDerivedRow`, they carry
no effective or settle dates), `nav/<suffix>.csv`, and `HAND_EXPORT.json` (label, every input file's
sha256, per-account counts and closure differences).

## Not covered (follow-ups)

- The generation's terminal-history manifest and current snapshot are not written (they need the
  generation's own identifiers); the importer produces the merged rows, series, closure and label
  that those steps consume.
- A ledger refusal for a release that carries a hand-rebuilt replay input edits `provider_ledger.py`
  (a pinned file) and is a separate change in a pin window.
