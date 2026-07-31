# Continuous integration controls

CI-001 establishes a frozen Python workspace and four independent workflows.
It does not implement provider, ledger, broker, or other CORE contracts.

## Workspace contract

The repository root is a virtual uv project supporting Python 3.11 through
3.13. `packages/ci-controls` is the only initial buildable member and is marked
`Private :: Do Not Upload`. It contains repository-local policy code, not
investment product behavior. Future packages join the shared workspace and
lock only through their governing issues.

`uv.lock` is authoritative for development and CI dependencies. Every quality
job runs `uv lock --check` before a frozen sync; later commands use `--no-sync`
so a check cannot silently change the environment. Builds use `--no-sources`
to prove that each member's published metadata is complete.

## Workflow boundaries

| Workflow | Purpose | Token authority |
|---|---|---|
| `quality.yml` | Six-cell Windows/Ubuntu and Python 3.11–3.13 matrix: frozen install, Ruff, mypy, bounded offline pytest, and all-package build | `contents: read` |
| `security.yml` | Local Git-history secret scan, locked dependency audit, and workflow policy validation | `contents: read` |
| `data-boundary.yml` | Standard-library scan of the authoritative Git index and staged blobs | `contents: read` |
| `issue-guard.yml` | Validate the issue graph; outside pull requests, reconcile native dependencies and reopen illegal closures | Pull requests: `contents: read`; reconcile only: `contents: read`, `issues: write` |

All external actions are allowlisted in `.github/ci-lock.json` and referenced by
full commit SHA. Checkout never persists credentials. There is no
`pull_request_target`, inherited secret set, OIDC authority, provider/broker
credential, cache publication, or uploaded diagnostic artifact.

The security workflow's direct binary download is limited to Gitleaks 8.30.1's
Linux x64 archive from its official GitHub release, and SHA-256 is verified
before execution. Gitleaks
scans locally with redacted findings and performs no credential verification,
comment, summary, or report upload. The uv dependency audit queries OSV for the
frozen lock graph at the supported Python 3.11 floor; it sends package
identities and versions, not repository content or credentials. An unavailable
vulnerability service fails the job.

## Issue guard

The issue body's `Blocked by` section is authoritative; checkbox state is not.
The guard reads every live issue, validates stable keys, missing references and
cycles, computes direct and transitive illegal closures, and compares the body
graph with GitHub's native dependency mirror before any write.

Reconciliation is conservative and idempotent: it reopens illegal closures,
adds missing relationships using blocker database IDs, and removes stale
relationships only after every add succeeds. It then re-reads the complete
graph and requires the exact postcondition. A failed run may leave an issue
safely open or temporarily over-blocked, but may not silently under-block work.
It never closes issues, edits issue bodies or checkboxes, or mutates another
repository.

Pull-request code receives no Issues token and runs only local tests with fake
clients, and the check reports on every pull request so the operator can verify
it without path-filter gaps. Event, scheduled, and main-push
reconciliation use the default-branch implementation. Reconciliation installs
no dependencies. Only the reconciliation job has `issues: write`; its pinned
checkout and Python-setup actions remain inside that job's token trust boundary,
checkout does not persist credentials, and only the direct standard-library
process maps `GITHUB_TOKEN` into its environment. The hourly schedule catches
native relationship changes because those changes do not have a dedicated
Issues workflow event.

## Local proof

Use a repository-local uv cache on this synced Windows worktree:

```powershell
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$env:UV_CACHE_DIR = "$PWD\.codex-bootstrap\uv-cache"
$env:UV_LINK_MODE = "copy"
$env:UV_PYTHON = "3.11"
$env:UV_PYTHON_DOWNLOADS = "never"
uv lock --check
uv sync --frozen --all-groups --all-packages --python 3.11
uv run --frozen --no-sync ruff format --check .
uv run --frozen --no-sync ruff check .
uv run --frozen --no-sync mypy packages/ci-controls/src packages/core/src packages/ibkr/src packages/portfolio-ledger/src tools/check_repository_boundary.py tools/check_push_policy.py tools/issue_guard.py tools/ibkr_generation.py tools/portfolio_provenance.py tools/portfolio_baseline.py tools/portfolio_report.py tools/portfolio_control_panel.py
uv run --frozen --no-sync pytest -q --disable-socket tests/security tests/ci tests/core tests/ibkr tests/portfolio_ledger tests/portfolio_provenance tests/portfolio_baseline tests/portfolio_report tests/portfolio_control_panel tests/governance
uv build --all-packages --no-sources --python 3.11
uv audit --locked --python-version 3.11 --preview-features audit-command
python tools/check_repository_boundary.py --root . --policy .policy/data-boundary.toml
```

Generated environments, caches, and `dist/` artifacts are ignored and are not
acceptance artifacts. CI uses synthetic inputs only and carries no live-network
product tests.

## Failure and recovery

A failing job blocks acceptance under the project operating canon;
`continue-on-error` is forbidden. On the current private personal GitHub Free
plan, GitHub does not enforce that block. Pull-request results are pre-merge
evidence only when the operator verifies all four workflows against the exact
unchanged head SHA. A workflow triggered by a push to `main` runs after the
mutation and is detective evidence, not prevention.

Keep a failed candidate on its feature branch, preserve the last accepted
`main`, and retry only after the diagnosed input, code, lock, dependency, or
environment changes. A merged CI defect is recovered through a governed revert
or corrective pull request. Do not rewrite history, weaken a check, expose a
matched secret, or use a provider credential to make CI pass.

`GOV-002` records the accepted Free-plan residual risk. Actions are restricted
to selected full-SHA-pinned dependencies, default workflow permission is read,
and workflows cannot approve pull requests. CODEOWNERS, the PR template, issue
reopening, and the local pre-push hook are procedural or defense-in-depth—not
protected `main` or GitHub-required checks.

## Primary references

- [GitHub secure use of Actions](https://docs.github.com/en/actions/reference/security/secure-use)
- [GitHub workflow permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions)
- [GitHub issue dependency REST API](https://docs.github.com/en/rest/issues/issue-dependencies?apiVersion=2026-03-10)
- [uv workspaces](https://docs.astral.sh/uv/concepts/projects/workspaces/)
- [uv GitHub Actions integration](https://docs.astral.sh/uv/guides/integration/github/)
