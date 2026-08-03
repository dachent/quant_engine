# Data recovery rehearsal

`DATA-DR-001` proves recovery instead of relying on a written rollback claim.
The rehearsal reads and hashes an accepted `DATA-PIPE-001` release, then works
only in a new isolated output root. It never publishes to, edits, or deletes an
accepted source generation.

For every target and companion in the accepted release, the tool injects both
caught exceptions and simulated process termination after journal intent and
after publication. Each scenario must finish with a
`ROLLED_BACK` journal, the complete predecessor pointer graph, and the exact
predecessor companion bytes. A full before/after fingerprint proves the source
release was unchanged.

The rehearsal also terminates rollback after a restore effect, verifies the
durable `ROLLING_BACK` journal, and explicitly resumes the same idempotent
rollback to `ROLLED_BACK`. A surviving lock can be cleared only when the
operator confirms it and the lock names the exact interrupted release.

The second lane creates four synthetic domains—account ledger, Excel report,
analytics, and panel—and proves:

1. Two consecutive dated coordinated rollforwards.
2. A late correction creates new immutable generations for the same as-of date.
3. Every predecessor generation remains byte-identical.
4. A new explicit restore release returns all roots and the launcher to the
   second accepted release.

Run against a committed private pipeline release:

```powershell
uv run --frozen --no-sync python tools/data_recovery_rehearsal.py `
  --spec 'C:\absolute\private\pipeline-spec.json' `
  --release-id '<committed-release-id>' `
  --output-root 'C:\absolute\new-empty-rehearsal-root'
```

The output root must be absent or empty. The canonical evidence file is
`recovery-rehearsal.json`, governed by
`schemas/core/recovery-rehearsal.schema.json`. The CLI reports its SHA-256.
The accepted source paths are deliberately omitted from the evidence payload.
