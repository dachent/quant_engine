# quant-engine-turingtrader

Governed, fail-closed capture of the TuringTrader public-token JSON endpoints
(hosted on Firebase Storage). The package persists exact response bytes before
any parsing, validates every payload against the versioned JSON Schemas in
`schemas/providers/`, enforces a freshness floor on dated endpoints, and
supports a deterministic offline replay that re-derives every output hash.

This package never ships tokens. Access tokens come only from a
deployment-local JSON config file that is never committed:

```json
{"version": 1, "tokens": {"public": "...", "allochist": "...", "holdings": "..."}}
```

The file must contain exactly the keys `public`, `allochist`, and `holdings`
with non-empty string values. Missing files, malformed JSON, missing or extra
keys, and empty values are rejected closed.

Usage (capture; the run directory is created once and must not exist):

```
python -m quant_engine.turingtrader.capture \
  --config cfg.json --run-dir RUN_DIR --slug STRATEGY_SLUG [--slug OTHER ...] \
  [--freshness-grace-days 14] [--run-date YYYY-MM-DD]
```

Deterministic replay (no network):

```
python -m quant_engine.turingtrader.capture replay --run-dir RUN_DIR
```

Run layout:

- `raw/{slug}/{object}.bin` — exact response bytes, create-only, written
  before any parsing.
- `envelopes/{slug}/{object}.json` — sanitized request/response metadata. The
  token key NAME is recorded; token values and the `token` query parameter
  never appear.
- `normalized/{slug}/{object}.json` — canonical JSON normalized rows; a pure
  function of the raw bytes.
- `coverage.json` — per-slug, per-endpoint observed end dates and freshness
  verdicts (`fresh`, `stale`, or `not_applicable_declared`).
- `manifest.json` — sha256 of every persisted file.
