# Schwab and Matrixport security quarantine

The provider quarantine tool has bounded profiles for the legacy Schwab and
Matrixport recovery surfaces identified by `SEC-002`. The profiles select only
credential/session-shaped artifacts, hash every source before moving it, write
an append-only journal, and fail closed on drift, links, or destination
collisions.

Schwab planning accepts the exact cutover-recovery and cutover-staging roots and
selects cookie-named JSON files. Matrixport planning accepts the exact
cutover-recovery and legacy-snapshot roots and selects `credentials.py` helpers
plus files below `.codex-bootstrap/venv`. Both write their immutable plan below
the provider-local quarantine root.

```powershell
uv run --frozen --no-sync python tools/provider_security_quarantine.py plan-schwab `
  --recovery-root <absolute-recovery-root> `
  --staging-root <absolute-staging-root> `
  --quarantine-root <absolute-quarantine-root>

uv run --frozen --no-sync python tools/provider_security_quarantine.py plan-matrixport `
  --recovery-root <absolute-recovery-root> `
  --snapshot-root <absolute-snapshot-root> `
  --quarantine-root <absolute-quarantine-root>
```

Run `apply`, then `verify`, against the returned `plan.json`. Verification
persists `verify.json` containing only plan identity, artifact count, byte count,
and state. `scan` records indicator names and affected relative paths without
credential values. `restore` reverses hash-matched moves to their original
relative paths. None of these operations changes a provider `CURRENT.json` or
published generation.
