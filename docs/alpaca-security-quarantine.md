# Alpaca security quarantine

The Alpaca profile in `tools/provider_security_quarantine.py` is the reversible
local control for `ALP-SEC-001`. It selects Markdown reports from the exact
legacy Alpaca root because the accepted redacted scan found a generic-key
finding in one plaintext README and the reports also contain private account
evidence. CSV, JSON, and Python files remain untouched for the separate
integration issue.

```powershell
uv run --frozen --no-sync python tools/provider_security_quarantine.py plan-alpaca `
  --legacy-root <Strategies_dataAlpaca-root> `
  --quarantine-root <dataAlpaca-quarantine-root>
```

The shared `apply`, `verify`, `scan`, and `restore` commands create a
content-addressed plan, move only its exact files, verify hashes independently,
write no secret value, and provide a recovery path.

Local quarantine does not revoke an Alpaca key. `ALP-SEC-001` may close only
after either a separately approved provider-side invalidation, or evidence that
the finding is a key identifier without its required secret and that no other
persisted credential exists. No provider request is implied by this runbook.
