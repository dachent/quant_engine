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

## 0.2 migration

The public API is now `HarExtractionReceipt`, `HarParseStatus`, and
`extract_har` from `quant_engine.web_contracts.evidence`. The redacting legacy
symbols `ExtractedResponse`, `SessionEnvelope`, `REDACTION`,
`canonical_extraction`, and `extract_quantbase_har` are removed. Provider
contracts remain declarations only; provider-specific capture, CLI, and replay
work is separately governed.

Migration tracking: [#375](https://github.com/dachent/quant_engine/issues/375),
[#376](https://github.com/dachent/quant_engine/issues/376),
[#377](https://github.com/dachent/quant_engine/issues/377),
[#378](https://github.com/dachent/quant_engine/issues/378), and
[#379](https://github.com/dachent/quant_engine/issues/379).
