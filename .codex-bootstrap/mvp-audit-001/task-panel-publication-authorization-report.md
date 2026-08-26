# MVP-AUDIT-001 panel-publication authorization report

Status: COMMITTED

Commit: `5f4a0b5 Enforce coordinated panel publication authorization`

## Root cause and remediation

The release builder had two incomplete lifecycle/compatibility behaviors:

- It wrote semantic validation and created the authorization receipt without first
  validating the supplied browser-QA evidence.
- Its directory-inventory preimage omitted the canonical JSON trailing newline,
  while the panel independently used canonical inventory bytes. A builder receipt
  was therefore rejected at the panel authority seam even when all content was
  unchanged.

The builder now invokes the panel QA validator after semantic validation and
before receipt creation. Both the builder and panel derive the identical
deterministic inventory preimage (sorted relative-path, SHA-256, and byte-size
rows serialized with canonical JSON bytes including the trailing newline).

The receipt remains an honest-operator guard against accidental out-of-band
pointer advancement; it is not a signer or security boundary against a local
principal able to alter both the receipt and output trees.

## Receipt contract covered

- Exact root keys, schema/version/type, operation/state/coordinator vocabulary,
  non-empty release ID and release commit, and independently observed output,
  candidate, and QA identities fail closed before `publish_generation`.
- Candidate and QA tree mutations after receipt creation fail before pointer
  movement.
- Nested file/directory links are rejected where symlink creation is available;
  this Windows environment skipped those two hermetic cases because it lacks the
  symlink privilege (`WinError 1314`). A direct reparse-point classifier unit
  test remains green and proves reparse metadata is treated as a link.
- Browser QA and semantic validation are frozen before the receipt; the builder
  does no QA-tree write after receipt creation.
- The release-ID SHA-256 preimage is asserted exactly, and receipt creation is
  same-byte idempotent but rejects a different candidate-derived byte sequence.
- A real builder-wrapper-to-panel publication path verifies canonical raw receipt
  bytes and binds wrapper receipt SHA-256/release ID to the panel result.

## TDD and verification evidence

- RED: `python -m pytest tests/core/test_portfolio_release_builder.py::test_panel_publication_validates_browser_qa_before_authorizing_receipt -q`
  failed as expected: actual tool calls were `verify, publish`, missing `qa`.
- GREEN for that lifecycle test: `1 passed in 2.87s` after the minimal QA call.
- Builder selection: `5 passed in 13.46s`.
- Final rollback/post-receipt mutation selection (captured by integration owner):
  `3 passed in 117.83s`.
- Final authorization mutation/link matrix (captured by integration owner):
  `22 passed, 2 skipped in 524.18s`.
- Real wrapper-to-panel positive seam: `1 passed in 64.91s` after cross-layer
  canonical-inventory alignment.
- Ruff: `All checks passed!`
- Mypy: `Success: no issues found in 2 source files`
- `git diff --check` exited 0.

All panel tests requiring the `rpds` dependency needed the approved elevated
Windows execution path: non-elevated collection failed with `DLL load failed:
Access is denied`. No test or validation was weakened to work around that
environmental constraint.

## Scoped files

- `tools/portfolio_release_builder.py`
- `tools/portfolio_control_panel.py`
- `tests/core/test_portfolio_release_builder.py`
- `tests/portfolio_control_panel/test_portfolio_control_panel.py`

## Self-review and next action

The change is limited to the authorization matrix, canonical cross-layer
inventory compatibility, and required lifecycle proof. No provider, browser,
Excel, GitHub, pointer, or publication target was touched outside temporary test
directories. Next action: hand committed SHA `5f4a0b5` to the integration owner
for final branch-wide verification.
