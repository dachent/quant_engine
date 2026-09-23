# Portfolio QC profiles

Run one finite profile from the repository root:

```text
python tools/portfolio_qc.py --profile <profile> [profile arguments]
```

The command prints one JSON result. Exit `0` means every invoked check passed.
Exit `2` names a missing input or workstation prerequisite. Exit `1` means a
check rejected the repository, candidate, measured state, or committed release.
No profile supplies owner publication authorization.

## `ci`

```text
python tools/portfolio_qc.py --profile ci
```

CI requires and runs the complete production-code MVP-2 vertical and full-product
proof, financial outcome and contract goldens, invested-capital tests,
attribution, operations, and the repository boundary check. A missing required
target returns exit `2` by name. Test fixtures may use explicit synthetic authority to exercise the
complete machinery; synthetic authority cannot qualify a production candidate.

## `pre-pr`

```text
python tools/portfolio_qc.py --profile pre-pr \
  --expected-head <40-hex-head> --pr <number> \
  --changed-path <path> [--changed-path <path> ...] \
  --test-target <pytest-target> [--test-target <pytest-target> ...]
```

The profile requires a clean checkout and the exact checked-out and remote PR
head, runs the caller's scoped tests,
Ruff lint and format checks for changed Python files, `git diff --check`, the
financial-MVP contract check, and the existing merge-readiness probe. Every
subprocess has a bounded timeout; the general command bound follows the existing
Quality workflow's 20-minute limit.

## `workstation`

```text
python tools/portfolio_qc.py --profile workstation \
  --spec <complete-pipeline-spec.json> \
  --candidate-dir <complete-private-candidate> \
  --evidence-dir <explicit-workspace-evidence-directory>
```

The profile calls `validate_financial_outcomes(spec, allow_synthetic=False)`,
inventories the retained candidate, and structurally rewrites every xlsx-win job
path into a disposable copy created inside the explicit evidence directory. Any
job path outside the candidate is rejected. Native Excel runs against the copy,
the workbook contract is validated, and the retained candidate inventory must
remain byte-identical. The profile retains a redacted content-addressed receipt;
it records hashes and statuses, not workbook values or machine paths.

## `release-preflight`

```text
python tools/portfolio_qc.py --profile release-preflight \
  --spec <final-production-spec.json> \
  --evidence-dir <explicit-workspace-evidence-directory>
```

Preflight derives the exact proposed roots, generation IDs, companions, and
launcher destination from the final `PipelineSpec`. It runs the production
financial-outcome validator and the existing pipeline planner and verifies every
governed gate and companion source hash. Report and panel generation construction
use their existing workbook and browser QA. Before and after validation it hashes
the real coordination authority, every target `CURRENT.json`, and every
companion destination. Any change fails the profile. A PASS creates its own
redacted, content-addressed receipt in the evidence directory; a caller-supplied
PASS receipt is never accepted as preflight evidence.

The explicit evidence directory may be a dated audit sibling of the private
stage; it need not be inside the pipeline state directory.

## `post-release-verify`

```text
python tools/portfolio_qc.py --profile post-release-verify \
  --spec <committed-production-spec.json> \
  --preflight-receipt <accepted-content-addressed-preflight.json> \
  --evidence-dir <explicit-workspace-evidence-directory>
```

Post-release verification recomputes the preflight receipt identity and exact
spec plan, reruns the non-synthetic financial outcome validator, and calls the
core `read_current_release` authority reader. That reader requires the actual
coordinated journal to be `COMMITTED` and validates the root pointers and
companions. The profile additionally compares the committed code SHA, root set,
generation IDs, companion identities and hashes, and governed gates with the
spec. Production pointers and companions must remain unchanged throughout.

Complete the requested fresh-session launcher/browser and native Excel checks
through the existing panel QA and workstation commands. The wrapper adds no
browser-evidence arguments, second admission layer, or replacement QA workflow.
Its receipt proves the checks it invokes; it does not by itself establish the
complete product's browser or native Excel acceptance.
