# ADR 0001: GitHub Free main-branch governance

- **Status:** Accepted
- **Date:** 2026-07-28
- **Governing issue:** [GOV-002](https://github.com/dachent/quant_engine/issues/6)
- **Decision owner:** Repository owner

## Context

`quant_engine` is private and owned by a personal GitHub account. The original
Foundation plan required protected `main` and GitHub-required checks. The live
branch-protection API returned HTTP 403 with the instruction to upgrade to
GitHub Pro or make the repository public. The user instead directed: “Stay on
the free plan. Continue.”

For a private personal GitHub Free repository, GitHub does not server-enforce
protected branches or branch rulesets. Consequently it cannot prevent an
authorized writer from pushing directly, force-updating or deleting `main`, or
merging while CI is red. CODEOWNERS is ownership metadata here, not mandatory
review enforcement.

## Decision

Keep the repository private on GitHub Free and use the following controls:

| Control | Classification | Evidence and limitation |
|---|---|---|
| Verified direct-writer minimization | Preventive access minimization | `dachent` is the only collaborator; invitations and deploy keys are empty. Personal GitHub App installation authority remains unverified. |
| Non-main branch plus pull request | Procedural | Required by the operating canon; GitHub does not enforce it. |
| Four green workflows for exact PR head SHA | Procedural/detective | The operator verifies Quality, Security, Data boundary, and Issue guard before merge; GitHub does not mark them required. |
| Selected actions and full SHA pinning | Preventive within Actions | Limits workflow dependency execution; it does not protect Git refs. |
| Read-only default workflow token; PR approval disabled | Preventive within Actions | Limits default token authority; issue reconciliation retains its explicit narrow write job. |
| Merge-commit as sole UI merge method | Preventive UI consistency | Preserves imported history; direct pushes and APIs remain possible. |
| Repository pre-push hook | Workstation defense-in-depth | Rejects all local updates/deletions of `main`; bypassable and absent on other clients. |
| Push-to-main workflows and issue guard | Detective/recovery | Run after mutation and cannot prevent it. |

The project must describe these controls accurately. “Required check,”
“protected main,” or equivalent language is reserved for actual GitHub server
enforcement.

## Residual risk

The sole verified direct writer can still bypass the process through GitHub, an
API, another client, `--no-verify`, a changed hook, or a direct/forced push. A
workflow can be changed by a writer, and a red post-push workflow does not undo
the change. The current token cannot enumerate personal GitHub App
installations, and the signed-in browser route is unavailable under the user's
browser policy; repository-access app authority is therefore an explicit
unverified residual rather than an assumed empty inventory. This risk is
accepted only while no write-capable app is known or observed and live trading
remains disabled.

## Operating and incident rules

Before every merge, record the exact PR head SHA and prove all four expected
workflows succeeded for that SHA. Stop if evidence is absent or ambiguous.

A detected direct, forced, deletion, or repointing update to `main` freezes new
work. Record a governance incident in the governing GitHub Issue, reconcile the
remote history to the last accepted OID, and recover through a corrective or
revert pull request. Never rewrite history as routine recovery.

Reopen `GOV-002` before:

- adding any collaborator or write-capable app, or discovering such an app;
- changing account plan or repository visibility;
- enabling live trading; or
- accepting a release whose assurance requires independently enforced branch
  controls.

## Rejected alternatives

- **Purchase GitHub Pro:** rejected by the user's Free-plan decision; requires a
  new explicit instruction.
- **Make the repository public:** rejected because visibility is not an
  acceptable substitute for protecting private investment-system code.
- **Claim CI, CODEOWNERS, templates, issue automation, or hooks are protection:**
  rejected as false assurance.
- **Squash-only merges:** rejected because future provenance-preserving imports
  must retain their commit histories.

## Recovery

An unmerged defective candidate stays on its feature branch. A merged defect is
recovered through a governed revert or corrective pull request. Repository
settings are restored from their captured pre-change values if a mutation
breaks accepted workflows. The worktree hook can be disabled by unsetting
`core.hooksPath` under a governing issue; that does not alter remote history.

## References

- [GitHub protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches)
- [GitHub rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)
- [GitHub Actions repository settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)
- [GitHub status checks](https://docs.github.com/en/pull-requests/reference/status-checks)
