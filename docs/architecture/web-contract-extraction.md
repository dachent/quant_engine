# Byte-complete web-contract extraction

## Decision

The generalized web-contract extractor is an offline evidence-preservation tool. It accepts an existing HAR artifact, writes the exact source bytes before attempting to parse them, and then emits source-ordered convenience files. It does not require a provider contract, a session envelope, an allowlist, a successful HTTP status, JSON content, or a browser.

The source HAR is authoritative. Parsed entries and materialized bodies are derivatives: useful for inspection, but never substitutes for bytes the HAR contained. This distinction matters because ordinary HAR `content.text` is a Unicode representation and does not prove the original HTTP wire encoding. When the HAR explicitly labels content as base64, the extractor retains both the encoded text and a decoded byte derivative when decoding succeeds.

## Public API

```python
from quant_engine.web_contracts import HarExtractionReceipt, HarParseStatus, extract_har

receipt = extract_har(source, output_dir)
```

`source` is a filesystem path, `bytes`, `bytearray`, or `memoryview`. `output_dir` must not already exist. The function returns an immutable `HarExtractionReceipt` containing only the output paths, source SHA-256 and size, final parse status, and number of materialized entries. Captured URLs, keys, headers, cookies, bodies, and exception values do not appear in the receipt.

This version intentionally removes the earlier redacted API contract: `ExtractedResponse`, `SessionEnvelope`, `REDACTION`, and `canonical_extraction` are no longer public. The package-local `extract_quantbase_har` wrapper is also removed. Provider contract declarations remain available and unchanged for separately governed provider work.

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

Only files justified by the source fields are emitted. A present empty body produces a zero-byte text file. Numeric entry directories prevent captured values from becoming paths.

`source.har` is byte-for-byte identical to the supplied artifact. It is created and flushed before decoding or parsing begins. `entry.json` contains the entire parsed entry without field selection, sorting, redaction, truncation, or status filtering. It uses ASCII JSON escapes so every parsed value, including a lone Unicode surrogate, is representable in valid UTF-8. Header and cookie arrays therefore retain their source order and duplicate records. The source file remains the authority for whitespace, encoding, duplicate JSON object keys, malformed JSON, and all other byte-level details that parsing cannot represent.

For any request `postData.text` or response `content.text`, the extractor writes `<side>.body.har-text` as the exact Python string encoded as UTF-8. A body text containing a Unicode surrogate is reversibly encoded with UTF-8 `surrogatepass`, recorded as representation `HAR_TEXT_SURROGATEPASS`, and produces a value-free indexed, side-specific diagnostic and `PARTIAL` receipt. When `encoding` is `base64`, the same string is also written as `<side>.body.base64`; successful strict base64 decoding creates `<side>.body.decoded`. Invalid base64 and malformed present body values are per-body dispositions, not extraction failures. The extractor never follows captured external-file locators.

## Manifest

`manifest.json` is deterministic canonical JSON with schema identifier `quant-engine.web-contract-extraction.v1`. It contains:

- source filename, SHA-256, and byte size;
- final parse status and completeness state;
- source entry count and materialized entry count;
- source-indexed relative paths, sizes, SHA-256 digests, representations, and decode dispositions for emitted derivative files;
- value-free structural diagnostic codes.

An initial manifest with `SOURCE_PRESERVED` state is atomically published immediately after the source write. The final manifest replaces it atomically. If execution stops after preservation but before finalization, the directory still states that only source preservation completed.

Final parse statuses are:

- `COMPLETE`: UTF-8 JSON with a HAR-shaped `log.entries` array, all entries materialized;
- `INVALID_UTF8`: source is not strict UTF-8;
- `INVALID_JSON`: source is UTF-8 but not valid JSON;
- `INVALID_HAR`: JSON lacks the required object/array shape;
- `PARTIAL`: the entries array was available and every value was preserved, but one or more entries was not a HAR object, a present body structure was malformed, a body representation was invalid, or a body text required surrogate-pass encoding.

Diagnostics use codes, source indexes, and the fixed body side (`request` or `response`) when applicable. They never copy captured values into logs, exceptions, receipts, or the manifest.

## Failure and recovery

The output is create-only. Existing paths fail before a source write, so an earlier extraction cannot be overwritten. Once `source.har` is complete, later parse, structure, or body-decoding failures do not remove it. Files already written are not deleted on a later failure.

The extractor performs no network or browser operation. There are no fixed body, entry, or file-size caps and no silent truncation. Operational I/O failures surface to the caller while leaving the create-only directory available for reconciliation.

## Security and data boundary

Complete HAR evidence commonly contains credentials, cookies, session material, private identifiers, and financial data. The caller chooses a suitably restricted local output directory. Real extraction directories are never repository artifacts. Repository and CI tests use synthetic values only.

Safe diagnostics do not mean the extraction is sanitized: `source.har`, `entry.json`, and body files deliberately contain the complete values supplied by the HAR. A future redacted or provider-specific view must be a separate, explicitly labelled derivative and cannot change this raw plane.

## Excluded surfaces

This design does not implement CLI commands, browser/CDP capture, navigation, screenshots, provider semantics, authenticated traffic, API fallback, replay, crawling/frontier coverage, DOM exploration, verification/promotion, code generation, or provider contract corrections. Those are independently governed surfaces.
