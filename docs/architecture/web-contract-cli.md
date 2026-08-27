# Generalized web-contract CLI

## Decision

Version 0.3 adds one installed command:

```text
quant-engine-web-contract extract --har-file <source.har> --output-dir <new-directory>
```

`--input` is an alias for `--har-file`. The command is a value-safe adapter over
`quant_engine.web_contracts.extract_har`; it does not parse, select, rewrite, or
materialize HAR content itself. The output directory must not already exist.
There is no implicit current directory, latest-file selection, provider, domain,
or status filter.

The command writes one canonical JSON status record. It contains only the parse
status, source digest and size, and materialized entry count. It never contains
captured URLs, paths, headers, cookies, keys, bodies, exception text, or argument
values. This safe console record is not a sanitized extraction: `source.har`,
`entry.json`, and body derivatives deliberately retain every value supplied by
the HAR.

## Exit contract

| Exit | Meaning | Evidence state |
|---:|---|---|
| 0 | `COMPLETE` | Source and all derivatives are finalized. |
| 2 | `PARTIAL`, `INVALID_UTF8`, `INVALID_JSON`, or `INVALID_HAR` | Source is preserved; inspect the value-safe manifest. |
| 1 | Source read, existing destination, I/O, or internal failure | No raw value is printed. Any already-created directory is left for reconciliation. |
| 64 | Command usage error | Arguments are not echoed. |
| 130 | Operator interruption | Any completed source/checkpoint writes are retained. |

## Recovered command inventory and dispositions

The verified generalized recovery source is `har_capture.py` (31,862 bytes,
SHA-256 `f609fb85e9eb41a05c3b4b14ac07ca519de39d53a0e875268ac57d479ab4fe8d`),
`har_to_csv.py` (23,514 bytes, SHA-256
`d4832db553155b8ee54a3fb100fdb76ad9d4fbbcf87bb8a878f2e61717a100cb`),
and `RUNBOOK_HAR_CAPTURE.md` (14,437 bytes, SHA-256
`45ccbae0e5940d96ff3f50e5f233be284afe34ff0b04b107c22fc7215d099263`).
The source remains read-only outside Git.

| Recovered surface | Disposition | Versioned replacement or rationale |
|---|---|---|
| `har_to_csv.py --har-file` | Adopted | `extract --har-file`; the same source path form now feeds the byte-complete package API. |
| Runbook-only `--input` | Adopted and corrected | `extract --input` is a tested alias. The recovered converter itself never accepted this documented flag. |
| `--output-dir` | Adopted and hardened | The destination is required and create-only. The legacy converter created or reused `responses/` and `csv/`, which could mix runs. |
| Omitted `--har-file` selecting the newest `*.har` by modification time | Superseded | An explicit source is required, eliminating nondeterministic or unintended evidence selection. |
| Per-response JSON files | Superseded | Source-ordered `entry.json` plus body files preserve the complete entry rather than a selected response value. |
| JSON-to-CSV flattening, including depth cap 20 | Deferred derivative | It is lossy and has no accepted package API. It cannot replace or control the raw extraction plane. |
| `--filter-domain` substring filtering | Deferred derivative | Filtering is incompatible with complete raw extraction and is rejected by the v0.3 command. |
| `--extract-html` regex table conversion | Deferred derivative | HTML interpretation is a separate optional view and is rejected by the v0.3 command. |
| Runbook-only `--site-name` for conversion | Not a compatibility surface | The recovered converter never accepted the flag; v0.3 uses the caller-selected destination. |
| Converter `responses/` and `csv/` naming | Superseded | The deterministic extraction layout is `source.har`, `manifest.json`, and numeric `entries/<index>/` directories. |
| `har_capture.py --target-pages`, `--cookie-urls`, `--page-load-wait`, `--settle-wait`, `--cdp-port`, `--screenshot`, `--tab-selectors`, `--config`, `--site-name`, and capture `--output-dir` | Deferred to browser capture | Issue #379 owns browser/CDP lifecycle and its configuration. No accepted capture API exists for this CLI to call. |
| Capture outputs `{site}_{timestamp}.har`, cookie/API-summary JSON, and screenshots | Deferred to browser capture | These depend on the separately governed capture runtime. |
| Aspirational `capture` and `verify` subcommands | Unavailable | Neither operation has an accepted provider-neutral package API. Unknown command names fail value-safely. |

The recovered converter returned zero on completion, argparse status 2 for parser
errors, and string-valued `SystemExit` failures for missing or invalid inputs. The
recovered capture returned zero after a completed run, status 1 for an unavailable
CDP endpoint, and otherwise allowed several runtime exceptions to escape. Version
0.3 replaces those inconsistent and sometimes value-bearing messages with the
fixed exit and JSON error contract above.

## Security and dependency boundary

The CLI has no runtime dependency beyond the web-contract package and Python's
standard library. It performs no network, browser, provider, authentication,
replay, CSV, or HTML operation. It catches operational and interruption failures
only to emit fixed codes; it never deletes or repairs partial evidence.

Real HARs and extraction directories remain local restricted artifacts and must
not enter Git, CI fixtures, issues, or logs.
