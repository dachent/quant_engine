# Portfolio123 security quarantine

`tools/p123_security_quarantine.py` is the bounded local control for
`P123-SEC-001`. It identifies authenticated HAR, cookie, session,
configuration-risk, and legacy credential-named artifacts only in the two
explicit Portfolio123 source roots. It does not parse provider payloads,
discover other directories, call Portfolio123, rotate credentials, or publish
data.

The plan is content-addressed and private. It records absolute source paths,
relative paths, byte counts, categories, and SHA-256 hashes under the governed
`dataPortfolio123` quarantine root; it must never enter GitHub. Application
preflights every source and destination before moving anything, writes an
fsynced per-file journal, and rolls back completed moves if the process raises
an error. Verification requires every source to be absent and every quarantined
artifact to retain its planned size and hash. Restore moves exact artifacts
back only when source and destination do not conflict.

Run from the repository with exact roots:

```powershell
uv run --frozen --no-sync python tools/p123_security_quarantine.py plan `
  --har-root <p123-har-workspace> `
  --legacy-root <legacy-p123-data-root> `
  --quarantine-root <data-portfolio123-quarantine-root>

uv run --frozen --no-sync python tools/p123_security_quarantine.py apply `
  --plan <private-plan.json>

uv run --frozen --no-sync python tools/p123_security_quarantine.py verify `
  --plan <private-plan.json>

uv run --frozen --no-sync python tools/p123_security_quarantine.py scan `
  --plan <private-plan.json>
```

The optional scan streams each quarantined file once, re-verifies its hash,
and records only the names of credential, authorization, cookie, or session
indicator classes. It never records matched values or excerpts.

Recovery is explicit:

```powershell
uv run --frozen --no-sync python tools/p123_security_quarantine.py restore `
  --plan <private-plan.json>
```

Quarantine does not prove provider-side credential rotation. `P123-SEC-001`
remains open until the affected provider credentials or sessions are changed
or invalidated and old material is proven unusable without exposing values.
