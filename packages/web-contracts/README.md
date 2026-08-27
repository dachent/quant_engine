# Web Contract Harvester

`quant-engine-web-contract-harvester` is a provider-neutral, offline HAR
evidence extractor. It accepts an already-captured HAR and writes a create-only
local evidence directory. It does not browse, authenticate, replay, call a
provider, or infer provider semantics.

## Raw-secret warning

Extraction deliberately preserves the complete captured evidence plane.
`source.har`, `entry.json`, and body derivatives can contain passwords, bearer
tokens, cookies, session material, private identifiers, account data, and other
secrets. Use a restricted local output directory. Never commit real extraction
directories, copy them into CI fixtures, attach them to issues, or treat the
manifest as a redacted substitute for the raw artifacts.

## Artifact layout

```text
<output_dir>/
  source.har
  manifest.json
  entries/
    000000/
      entry.json
      request.body.har-text
      request.body.base64
      request.body.decoded
      response.body.har-text
      response.body.base64
      response.body.decoded
```

Only source-justified body files exist. `manifest.json` contains paths, sizes,
SHA-256 digests, representation/decode dispositions, and value-safe
diagnostics; it does not contain captured values. See
[the extraction architecture](../../docs/architecture/web-contract-extraction.md)
for the exact preservation and recovery contract.

## CLI

Version 0.3 installs a thin offline wrapper over the same API:

```powershell
quant-engine-web-contract extract `
  --har-file C:\restricted\capture.har `
  --output-dir C:\restricted\capture-extraction
```

`--input` is an alias for `--har-file`. Both paths are required; the output must
not already exist. Exit status is 0 for `COMPLETE`, 2 when the source is preserved
but parsing/materialization is noncomplete, 1 for an operational error, 64 for
usage, and 130 for interruption. Console JSON contains only value-safe status,
digest, size, and count fields. It never prints captured values or argument paths.

Capture, verification, latest-HAR discovery, domain filtering, CSV flattening,
and HTML table extraction are not hidden inside this command. Their recovered
dispositions and migration details are recorded in
[the CLI architecture](../../docs/architecture/web-contract-cli.md).

## 0.2 and 0.3 migration

The public API is now `HarExtractionReceipt`, `HarParseStatus`, and
`extract_har` from `quant_engine.web_contracts.evidence`. The redacting legacy
symbols `ExtractedResponse`, `SessionEnvelope`, `REDACTION`,
`canonical_extraction`, and `extract_quantbase_har` are removed. The former
public `SOURCE_ANCHORS` is removed; use generalized
`RECOVERED_SOURCE_ANCHORS` instead. The
`quant_engine.web_contracts.extract` import path is deleted. Provider contracts
remain declarations only; provider-specific capture, CLI, and replay work is
separately governed.

Version 0.3 adds `quant-engine-web-contract extract`. It keeps the complete v0.2
artifact contract unchanged and adds no browser or provider dependency.

Migration tracking: [#375](https://github.com/dachent/quant_engine/issues/375),
[#376](https://github.com/dachent/quant_engine/issues/376),
[#377](https://github.com/dachent/quant_engine/issues/377),
[#378](https://github.com/dachent/quant_engine/issues/378), and
[#379](https://github.com/dachent/quant_engine/issues/379).
