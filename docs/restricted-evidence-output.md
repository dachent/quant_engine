# Restricted local evidence output

`GOV-EVIDENCE-001` prohibits broad text search, `ConvertTo-Json`, or whole-file
serialization against restricted local catalogs. Read-only filesystem access is
not sufficient protection: a single-line manifest can stream every private
field into stdout when a search matches one harmless key.

Use `tools/restricted_evidence_summary.py` for local JSON evidence. Every query
must declare a public output label, an exact JSON Pointer, and one of four
aggregate-only operations:

- `count` returns the length of an object or array;
- `hash` returns only canonical byte count and SHA-256;
- `present` returns only that the selected path exists;
- `type` returns only the JSON value type.

```powershell
uv run --frozen --no-sync python tools/restricted_evidence_summary.py `
  --input <absolute-private-json> `
  --query artifact_count:/artifacts:count `
  --query evidence_digest:/evidence:hash
```

The tool never emits the input path, selected value, object keys below the
selector, or parser details. Missing/unknown fields, unsupported operations,
duplicate labels, non-collection counts, invalid JSON, links, and read failures
produce stable reason codes only. Start with file hash, shape, and counts; add a
selector only when deleting it would leave the governing issue unproven.

The rejected method is unrestricted search or serialization over a private
catalog, even for a read-only task. If a command emits a restricted value, stop,
do not quote or persist it, and govern the incident without repeating the value.
