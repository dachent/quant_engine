# Quantbase security quarantine

The Quantbase profile in `tools/provider_security_quarantine.py` is the bounded
local control for `QB-SEC-001`. It selects only authenticated HARs, browser
state exports, derived API summaries, account API evidence, and account
screenshots under one explicit legacy root. It leaves the normalized CSV
baseline and runbook untouched for the later `QB-GEN0-001` integration.

Cloud-backed selected files must first be pinned and hydrated. The operator
then creates a content-addressed private plan:

```powershell
uv run --frozen --no-sync python tools/provider_security_quarantine.py plan-quantbase `
  --legacy-root <legacy-quantbase-root> `
  --quarantine-root <data-quantbase-quarantine-root>
```

The shared `apply`, `verify`, `scan`, and `restore` commands use the plan path
and have the same create-only manifest, per-attempt journal, rollback,
Windows-long-path, and value-free scanning guarantees documented in the
[Portfolio123 quarantine runbook](p123-security-quarantine.md).

Quarantine cannot revoke a web session. `QB-SEC-001` may close only when the
provider session is invalidated, or when every captured token has a verifiable
expiry claim earlier than the recorded evaluation time. The proof must not
disclose, replay, or persist token values.
