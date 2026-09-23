# The production replay seam

The release proof does not trust the artifacts an attended run produced. It **replays** them:
`validate_financial_outcomes` loads a plan bundle's own declared bytes, calls the producer again,
and requires the result to be byte-identical
(`tools/portfolio_financial_outcomes.py`, `_bound_decomposition_plan`).

That gives the replay one obligation which is easy to state and was violated four times over:

> The replay must call the producer with the arguments the producer was called with.
> Anything the attended path supplies and the replay omits is a latent release blocker,
> and it will not be found until every blocker ahead of it is cleared.

## Why omitting an argument is not "verifying less"

It would be comfortable to assume a replay that passes fewer arguments simply checks fewer
things. It does not. Several producer arguments change **what the producer is required to
prove**, so omitting one asks for a *different* artifact rather than a weaker one.

The clearest case is the layered-history reader authorities.
`tools/portfolio_target_series.py:6088-6089` sets

```python
current_admission_requested = provider_admission_receipt_path is not None
version_evidence_requested = portfolio123_version_evidence_path is not None
```

and `:6106-6119` then adds `current_source_admission` and `portfolio123_version_equivalence` to
the **exact key set** the generation must carry. A generation built with those authorities can
therefore only ever be verified by a caller that also supplies them. Withholding them does not
relax the check; it demands a generation of a shape the run never produced.

Supplying them is strictly more verification, not less:

- `:2350-2357` requires the three provider-ledger files to be exact-named siblings of one
  generation directory, so the caller cannot point them anywhere it likes;
- `:2392-2396` re-derives the admission authority and requires `receipt_sha256`,
  `generation_id` and `manifest_sha256` to agree;
- `:2339-2343` makes the set all-or-nothing — a financial input contract without an admission
  receipt is refused outright.

The same shape holds for the retained public-factor capture.
`tools/portfolio_current_decomposition_plan.py:2066-2068` emits
`inputs/governance/retained-public-factor-capture.json` **only when a capture is supplied**, so a
replay that omits it cannot satisfy `outputs == files` for any bundle that declares that file —
independently of the RELEASE_QC freshness gate at `:1185-1195` that it also trips.

That last point is worth keeping: the byte-equality assertion is self-policing. Passing a capture
a bundle does not declare, or omitting one it does, both break equality. The same is true of the
authorities. There is no way to satisfy the replay by guessing.

## The four defects, and why they surfaced one at a time

All four sat in or around a single call, and each was hidden by the one ahead of it.

| # | Defect | Symptom |
|---|---|---|
| 1 | the replay never passed `retained_public_factor_capture` | `release QC requires public factors captured for the planning run` |
| 2 | the replay never passed the five reader authorities, and the specification never named four of them | `layered history verification failed: layered history generation keys differ` |
| 3 | the inventory named only the primary bundle's base sources, never the hybrid's | `financial producer/source artifact binding is absent or ambiguous` (LIVE_PLUS_BACKTEST) |
| 4 | the base source root was resolved per file, requiring each digest to be globally unique | the same error, now for **both** bundles, once the hybrid rows existed |

Defect 4 is the interesting one, because fixing 3 caused it. The primary and hybrid bundles
share eight of their nine base sources byte-for-byte; only `source/interval-authority.json`
differs. So per-file resolution worked purely because one root was named — a passing check that
rested on a coincidence in the data, not on a binding. Naming both roots made those eight digests
resolve at two paths and broke everything.

The root is what must be unique, not each file. The loop now collects the candidate roots each
source admits and intersects them, which picks the single root that explains the whole base
specification and still refuses when none or several survive. Every file is still required to sit
under that root at its declared relative path with its declared digest and size.

## Checking the seam

The mechanical check is to diff the producer's parameters against what each caller passes.
Two traps, both of which caught me:

1. **`**kwargs` expansions are invisible to a naive AST walk.** Collecting only named keywords
   reported all five authorities as absent from the attended call, when
   `portfolio_attended_preparation.py:843` and `:1637` pass them via `**history_authorities`.
   A diff that ignores `ast.keyword` entries whose `arg` is `None` will tell you the seam is
   aligned when it is not.
2. **The producing call is not always the obvious one.** `:818` is a pre-flight inside
   `_validated_inputs` that discards its result; the call that actually writes the bundle is
   `plans.write_bundle` at `:1609`. They happen to pass the same arguments, but a seam audit
   that reads only the first is reading a call whose output nobody keeps.

The authoritative check is not the diff at all — it is the replay itself. Rebuild both bundles
from their own bytes and compare. That is what `_bound_decomposition_plan` does, and it is why
the equality assertion should never be relaxed to accommodate a caller.

## The seam exists because the arguments are a convention, not an artifact

The other replay in the same module does not have this problem, and the reason is worth copying
rather than admiring. `_replay_model` (`tools/portfolio_financial_outcomes.py`, the DECOMPOSITION
and DEPENDENCE path) does not reconstruct the producer's arguments from what some caller
happened to pass. It reads them from a **pinned artifact**: the generation manifest names
`spec/decomposition-spec.json` or `spec/correlation-spec.json`, that specification is resolved by
digest, and the producer is re-driven from it.

There is no seam to drift there, because the producer's inputs are themselves part of the
evidence. The plan-bundle replay drifts because its arguments live only in the source of two
call sites that must be kept in agreement by hand.

So the structural fix — larger than anything done here, and not attempted — is for the plan
bundle to carry its producer's arguments the way a decomposition generation does. Until then the
agreement is maintained by the byte-equality assertion and by the test that pins the call.

## What is verified, and what is not

Verified by measurement against the release-candidate run: with the capture and all five
authorities supplied, both `LIVE_ONLY` and `LIVE_PLUS_BACKTEST` rebuild with an identical
manifest and zero differing bytes across all 55 declared inputs and outputs.

Not established: whether any *other* producer in the release has the same seam gap. The check
above is cheap and has found four defects at one call site; it has not been run anywhere else.

## Related

- `docs/financial-outcome-verification.md` — which financial outcome gets which check, and why
  INVESTED_CAPITAL is plumbed differently from AS_HELD_COMPOSITE_HWM.
