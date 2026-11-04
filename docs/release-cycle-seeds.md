# Per-cycle seeds

Three files of a release cycle used to be operator inputs that no committed tool wrote:
`in/implementation-reconciliation.json`, `in/implementation-return-attribution.json` and
`attribution-spec.json`. `tools/release_cycle_seeds.py` produces them from the pinned
release-inputs root (`docs/release-publication.md`) and from the cycle's own baseline.

## `stage` (before `cycle-resolve`)

```
release_cycle_seeds.py stage --release-inputs-root R --release-inputs-manifest-sha256 H \
  --cutoff D --roots-file F --cycle C
```

- Reads `ops/mvp2-release/inputs/seeds/implementation-reconciliation.json`,
  `.../implementation-return-attribution.json` and `.../attribution-spec-template.json` through
  `resolve_release_input`: the root is verified against its pin, a root of another cutoff, an unlisted file or a
  changed byte refuses.
- Copies the two implementation files byte for byte to `C/in/`.
- Writes `C/attribution-spec-template.json`: the root's spec with `return_source` resolved from the run's root
  keys. The template names the return source as `{root_key, relative, expected_sha256, source_manifest_relative,
  source_manifest_expected_sha256}`; both files are re-hashed against those pins. The template's `as_of_date`
  must equal the cutoff.
- Create-only: an existing file with other bytes refuses; the same bytes are left as they are.

## `derive-attribution-spec` (after `baseline`, before `selected-inputs`)

```
release_cycle_seeds.py derive-attribution-spec --template C/attribution-spec-template.json \
  --baseline-root C/baseline --cutoff D --out C/attribution-spec.json
```

Writes `C/attribution-spec.json`: the template with `as_of_date` set to the cutoff and every available
comparison's `observed_start`/`observed_end` derived from data. The window is the first and last date, up to the
cutoff, that the baseline's own ACCOUNT_LIVE series for the strategy shares with the comparison's reference series
in the return source (the pair `portfolio_attribution` compares). A reference coarser than daily takes its own
dates inside the account history. A strategy the baseline keeps as history only reads its retained series from the
return source by `account_series_id`, as the attribution stage does. A baseline of another cutoff, a hash that
differs, or an available row with nothing to compare refuses; no window is defaulted. Rows whose status is not
available are copied unchanged.

Receipt-bound rows (an account whose epoch HPR spans a window would cut) are decided by the attribution stage
itself: a derived window that cuts a span makes that row `NOT_READY` there. The tool prints every derived window.

Reproduction on real bytes (2026-11-03): the R2.1 template and the retained baseline `ee6220c6...` (as of
2026-09-17) give the same `observed_start`/`observed_end` as the committed R2.1 spec for all 18 rows
(9 comparisons, 9 benchmark comparisons).

## What stays outside Git

The three seed files and the return source are cutoff inputs, so they live in the release-inputs root and its
manifest lists them. The implementation files are builder outputs
(`tools/portfolio_implementation_reconciliation.py`, `tools/portfolio_implementation_return_attribution.py`) on the
cycle's refreshed data; a cycle whose cutoff moved builds them again and the preparation spec takes the new pins
with `--pin`.
