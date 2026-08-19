"""Fail-closed policy checks for quant_engine GitHub Actions workflows."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml


EXPECTED_WORKFLOWS = frozenset(
    {"data-boundary.yml", "issue-guard.yml", "quality.yml", "security.yml"}
)
READ_ONLY_PERMISSIONS = {"contents": "read"}
ISSUE_GUARD_PERMISSIONS = {
    "validate": {"contents": "read"},
    "reconcile": {"contents": "read", "issues": "write"},
}
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
ACTION_REFERENCE = re.compile(r"^(?P<target>[^@]+)@(?P<sha>[0-9a-f]{40})$")
LOCKED_AUDIT_COMMAND = (
    "uv audit --locked --python-version 3.11 --preview-features audit-command"
)
ISSUE_GUARD_RECONCILE_COMMAND = (
    'python tools/issue_guard.py reconcile --repository "${{ github.repository }}" '
    "--format json --max-pages 20 --timeout-seconds 20 "
    "--failure-consumer-issue 333"
)


@dataclass(frozen=True, slots=True, order=True)
class Finding:
    """One sanitized workflow-policy failure."""

    path: str
    code: str
    detail: str


class WorkflowPolicyError(RuntimeError):
    """A deterministic configuration-load failure."""


def _object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowPolicyError(f"{field} must be an object")
    return {str(key): item for key, item in value.items()}


def _load_json_object(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowPolicyError(f"CI_LOCK_UNREADABLE:{type(exc).__name__}") from exc
    return _object(value, "ci-lock")


def load_ci_lock(path: Path) -> Mapping[str, Any]:
    """Load and structurally validate the committed CI dependency lock."""

    document = _load_json_object(path)
    if set(document) != {"schema_version", "verified_on", "actions", "tools"}:
        raise WorkflowPolicyError("CI_LOCK_FIELDS_INVALID")
    if document["schema_version"] != 1:
        raise WorkflowPolicyError("CI_LOCK_SCHEMA_INVALID")
    if (
        re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", str(document["verified_on"]))
        is None
    ):
        raise WorkflowPolicyError("CI_LOCK_VERIFICATION_DATE_INVALID")

    actions = _object(document["actions"], "ci-lock.actions")
    if not actions:
        raise WorkflowPolicyError("CI_LOCK_ACTIONS_EMPTY")
    for name, raw_record in actions.items():
        if re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", name) is None:
            raise WorkflowPolicyError("CI_LOCK_ACTION_NAME_INVALID")
        record = _object(raw_record, f"ci-lock.actions.{name}")
        if set(record) != {"release", "sha", "release_url", "license"}:
            raise WorkflowPolicyError("CI_LOCK_ACTION_FIELDS_INVALID")
        if FULL_SHA.fullmatch(str(record["sha"])) is None:
            raise WorkflowPolicyError("CI_LOCK_ACTION_SHA_INVALID")
        if not str(record["release_url"]).startswith(
            f"https://github.com/{name}/releases/tag/"
        ):
            raise WorkflowPolicyError("CI_LOCK_ACTION_URL_INVALID")
        if not str(record["release"]).startswith("v") or not str(record["license"]):
            raise WorkflowPolicyError("CI_LOCK_ACTION_METADATA_INVALID")

    tools = _object(document["tools"], "ci-lock.tools")
    if set(tools) != {"gitleaks", "uv"}:
        raise WorkflowPolicyError("CI_LOCK_TOOLS_INVALID")
    uv_record = _object(tools["uv"], "ci-lock.tools.uv")
    if set(uv_record) != {"version", "release_url", "license"}:
        raise WorkflowPolicyError("CI_LOCK_UV_FIELDS_INVALID")
    gitleaks = _object(tools["gitleaks"], "ci-lock.tools.gitleaks")
    if set(gitleaks) != {
        "version",
        "archive",
        "sha256",
        "release_url",
        "license",
    }:
        raise WorkflowPolicyError("CI_LOCK_GITLEAKS_FIELDS_INVALID")
    if re.fullmatch(r"[0-9a-f]{64}", str(gitleaks["sha256"])) is None:
        raise WorkflowPolicyError("CI_LOCK_GITLEAKS_SHA_INVALID")
    return document


def _load_workflow(path: Path) -> Mapping[str, Any]:
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise WorkflowPolicyError(
            f"WORKFLOW_UNREADABLE:{path.name}:{type(exc).__name__}"
        ) from exc
    return _object(document, path.name)


def _permissions(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): str(item).casefold() for key, item in value.items()}


def _workflow_steps(
    document: Mapping[str, Any],
) -> Iterable[tuple[str, int, Mapping[str, Any]]]:
    jobs = document.get("jobs")
    if not isinstance(jobs, Mapping):
        return
    for raw_job_name, raw_job in jobs.items():
        job_name = str(raw_job_name)
        if not isinstance(raw_job, Mapping):
            continue
        steps = raw_job.get("steps")
        if not isinstance(steps, list):
            continue
        for index, step in enumerate(steps):
            if isinstance(step, Mapping):
                yield job_name, index, {str(key): value for key, value in step.items()}


def _action_findings(
    workflow_name: str,
    document: Mapping[str, Any],
    ci_lock: Mapping[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    actions = _object(ci_lock["actions"], "ci-lock.actions")
    tools = _object(ci_lock["tools"], "ci-lock.tools")
    uv_record = _object(tools["uv"], "ci-lock.tools.uv")
    for job_name, index, step in _workflow_steps(document):
        uses_value = step.get("uses")
        if uses_value is None:
            continue
        uses = str(uses_value)
        locator = f"{workflow_name}:jobs.{job_name}.steps[{index}]"
        if uses.startswith("./"):
            findings.append(
                Finding(
                    locator,
                    "LOCAL_ACTION_NOT_APPROVED",
                    "local actions are not approved",
                )
            )
            continue
        match = ACTION_REFERENCE.fullmatch(uses)
        if match is None:
            findings.append(
                Finding(
                    locator, "ACTION_REF_NOT_IMMUTABLE", "action must use a full SHA"
                )
            )
            continue
        target_parts = match.group("target").split("/")
        if len(target_parts) < 2:
            findings.append(
                Finding(locator, "ACTION_NAME_INVALID", "action repository is invalid")
            )
            continue
        action_name = "/".join(target_parts[:2])
        raw_lock_record = actions.get(action_name)
        if not isinstance(raw_lock_record, Mapping):
            findings.append(
                Finding(
                    locator, "ACTION_NOT_ALLOWLISTED", "action is absent from ci-lock"
                )
            )
            continue
        if match.group("sha") != str(raw_lock_record.get("sha", "")):
            findings.append(
                Finding(
                    locator, "ACTION_SHA_MISMATCH", "action SHA differs from ci-lock"
                )
            )
        if action_name == "actions/checkout":
            with_value = step.get("with")
            with_mapping = with_value if isinstance(with_value, Mapping) else {}
            if str(with_mapping.get("persist-credentials", "")).casefold() != "false":
                findings.append(
                    Finding(
                        locator,
                        "CHECKOUT_CREDENTIAL_PERSISTENCE",
                        "checkout must disable persisted credentials",
                    )
                )
        if action_name == "astral-sh/setup-uv":
            with_value = step.get("with")
            with_mapping = with_value if isinstance(with_value, Mapping) else {}
            if str(with_mapping.get("version", "")) != str(uv_record["version"]):
                findings.append(
                    Finding(
                        locator,
                        "UV_VERSION_LOCK_MISMATCH",
                        "setup-uv version differs from ci-lock",
                    )
                )
            if str(with_mapping.get("enable-cache", "")).casefold() != "false":
                findings.append(
                    Finding(
                        locator,
                        "UV_CACHE_NOT_DISABLED",
                        "setup-uv cache must be disabled",
                    )
                )
            jobs = document.get("jobs")
            raw_job = jobs.get(job_name) if isinstance(jobs, Mapping) else None
            job = raw_job if isinstance(raw_job, Mapping) else {}
            raw_env = job.get("env")
            job_env = raw_env if isinstance(raw_env, Mapping) else {}
            configured_python = str(job_env.get("UV_PYTHON", ""))
            setup_python_versions: set[str] = set()
            for candidate_job, _, candidate in _workflow_steps(document):
                if candidate_job != job_name:
                    continue
                candidate_match = ACTION_REFERENCE.fullmatch(
                    str(candidate.get("uses", ""))
                )
                if candidate_match is None:
                    continue
                candidate_action = "/".join(
                    candidate_match.group("target").split("/")[:2]
                )
                if candidate_action != "actions/setup-python":
                    continue
                candidate_with = candidate.get("with")
                if isinstance(candidate_with, Mapping):
                    setup_python_versions.add(
                        str(candidate_with.get("python-version", ""))
                    )
            if (
                str(job_env.get("UV_PYTHON_DOWNLOADS", "")).casefold() != "never"
                or not configured_python
                or setup_python_versions != {configured_python}
            ):
                findings.append(
                    Finding(
                        locator,
                        "UV_PYTHON_SELECTION_INVALID",
                        "uv must use the installed Python without downloads",
                    )
                )
    return findings


def validate_workflow_document(
    workflow_name: str,
    document: Mapping[str, Any],
    text: str,
    ci_lock: Mapping[str, Any],
) -> list[Finding]:
    """Validate one parsed workflow without performing external reads."""

    findings: list[Finding] = []
    on_value = document.get("on")
    triggers = on_value if isinstance(on_value, Mapping) else {}
    if "pull_request_target" in {str(key) for key in triggers}:
        findings.append(
            Finding(
                workflow_name,
                "PULL_REQUEST_TARGET_FORBIDDEN",
                "unsafe trigger is forbidden",
            )
        )
    if workflow_name == "issue-guard.yml" and "workflow_dispatch" in {
        str(key) for key in triggers
    }:
        findings.append(
            Finding(
                workflow_name,
                "ISSUE_GUARD_DISPATCH_FORBIDDEN",
                "manual dispatch cannot receive issue-write authority",
            )
        )
    if workflow_name == "issue-guard.yml":
        if {str(key) for key in triggers} != {
            "issues",
            "pull_request",
            "push",
            "schedule",
        }:
            findings.append(
                Finding(
                    workflow_name,
                    "ISSUE_GUARD_TRIGGERS_INVALID",
                    "issue guard triggers must use only default-branch authorities",
                )
            )
        pull_request = triggers.get("pull_request")
        if (
            "pull_request" not in triggers
            or (isinstance(pull_request, Mapping) and pull_request)
            or (
                not isinstance(pull_request, Mapping) and pull_request not in (None, "")
            )
        ):
            findings.append(
                Finding(
                    workflow_name,
                    "ISSUE_GUARD_PR_FILTER_FORBIDDEN",
                    "issue guard must report on every pull request",
                )
            )
        push = triggers.get("push")
        if not isinstance(push, Mapping) or push.get("branches") != ["main"]:
            findings.append(
                Finding(
                    workflow_name,
                    "ISSUE_GUARD_PUSH_INVALID",
                    "issue guard push authority is limited to main",
                )
            )
    lowered_text = text.casefold()
    if re.search(r"secrets\s*:\s*inherit", lowered_text) or re.search(
        r"\$\{\{\s*secrets\b", lowered_text
    ):
        findings.append(
            Finding(
                workflow_name,
                "WORKFLOW_SECRET_REFERENCE",
                "repository secrets are forbidden",
            )
        )
    if "continue-on-error" in lowered_text:
        findings.append(
            Finding(
                workflow_name,
                "CONTINUE_ON_ERROR_FORBIDDEN",
                "failures must remain blocking",
            )
        )
    if re.search(r"curl[^\r\n]*\|", lowered_text):
        findings.append(
            Finding(
                workflow_name,
                "DYNAMIC_INSTALLER_FORBIDDEN",
                "piped installers are forbidden",
            )
        )
    if "github.token" in lowered_text and workflow_name != "issue-guard.yml":
        findings.append(
            Finding(
                workflow_name,
                "GITHUB_TOKEN_REFERENCE_FORBIDDEN",
                "token is limited to issue guard",
            )
        )

    top_permissions = _permissions(document.get("permissions"))
    expected_top = {} if workflow_name == "issue-guard.yml" else READ_ONLY_PERMISSIONS
    if top_permissions != expected_top:
        findings.append(
            Finding(
                workflow_name,
                "WORKFLOW_PERMISSIONS_INVALID",
                "top permissions are not minimal",
            )
        )

    jobs = document.get("jobs")
    if not isinstance(jobs, Mapping) or not jobs:
        findings.append(
            Finding(workflow_name, "WORKFLOW_JOBS_INVALID", "jobs must be non-empty")
        )
        return findings
    if workflow_name == "issue-guard.yml" and {str(key) for key in jobs} != set(
        ISSUE_GUARD_PERMISSIONS
    ):
        findings.append(
            Finding(
                workflow_name,
                "ISSUE_GUARD_JOBS_INVALID",
                "issue guard requires exactly validate and reconcile jobs",
            )
        )
    for raw_job_name, raw_job in jobs.items():
        job_name = str(raw_job_name)
        locator = f"{workflow_name}:jobs.{job_name}"
        if not isinstance(raw_job, Mapping):
            findings.append(
                Finding(locator, "WORKFLOW_JOB_INVALID", "job must be an object")
            )
            continue
        raw_timeout = raw_job.get("timeout-minutes")
        try:
            timeout = int(str(raw_timeout))
        except (TypeError, ValueError):
            timeout = 0
        if timeout < 1 or timeout > 15:
            findings.append(
                Finding(
                    locator, "WORKFLOW_TIMEOUT_INVALID", "timeout must be 1-15 minutes"
                )
            )
        job_permissions = _permissions(raw_job.get("permissions"))
        if workflow_name == "issue-guard.yml":
            expected_job = ISSUE_GUARD_PERMISSIONS.get(job_name)
            if expected_job is None or job_permissions != expected_job:
                findings.append(
                    Finding(
                        locator,
                        "JOB_PERMISSIONS_INVALID",
                        "issue-guard permissions are invalid",
                    )
                )
            if job_name == "reconcile" and (
                str(raw_job.get("if", "")) != "github.event_name != 'pull_request'"
                or str(raw_job.get("needs", "")) != "validate"
                or raw_job.get("env") not in (None, {})
            ):
                findings.append(
                    Finding(
                        locator,
                        "ISSUE_GUARD_RECONCILE_GATE_INVALID",
                        "reconcile must follow validation and exclude pull requests",
                    )
                )
        elif job_permissions is not None and job_permissions != READ_ONLY_PERMISSIONS:
            findings.append(
                Finding(
                    locator,
                    "JOB_PERMISSIONS_INVALID",
                    "job permissions exceed read-only",
                )
            )

    issue_token_steps = 0
    for job_name, index, step in _workflow_steps(document):
        run_value = step.get("run")
        run_text = str(run_value) if run_value is not None else ""
        locator = f"{workflow_name}:jobs.{job_name}.steps[{index}]"
        if workflow_name == "quality.yml":
            commands = [line for line in run_text.splitlines() if line.strip()]
            if len(commands) > 1:
                findings.append(
                    Finding(
                        locator,
                        "CROSS_PLATFORM_MULTI_COMMAND_STEP",
                        "quality commands must fail independently on Windows",
                    )
                )
        if workflow_name == "issue-guard.yml":
            raw_env = step.get("env")
            step_env = raw_env if isinstance(raw_env, Mapping) else {}
            if job_name == "reconcile":
                uses_value = step.get("uses")
                if uses_value is not None:
                    match = ACTION_REFERENCE.fullmatch(str(uses_value))
                    action_name = (
                        "/".join(match.group("target").split("/")[:2])
                        if match is not None
                        else ""
                    )
                    if action_name not in {"actions/checkout", "actions/setup-python"}:
                        findings.append(
                            Finding(
                                locator,
                                "ISSUE_WRITE_JOB_STEP_INVALID",
                                "write job actions are limited to checkout and Python",
                            )
                        )
                elif run_text and "GITHUB_TOKEN" not in {str(key) for key in step_env}:
                    findings.append(
                        Finding(
                            locator,
                            "ISSUE_WRITE_JOB_STEP_INVALID",
                            "write job permits only the direct reconciler command",
                        )
                    )
            if "GITHUB_TOKEN" in {str(key) for key in step_env}:
                issue_token_steps += 1
                token_value = str(step_env.get("GITHUB_TOKEN", ""))
                if (
                    job_name != "reconcile"
                    or set(step_env) != {"GITHUB_TOKEN"}
                    or re.fullmatch(r"\$\{\{\s*github\.token\s*\}\}", token_value)
                    is None
                    or run_text != ISSUE_GUARD_RECONCILE_COMMAND
                ):
                    findings.append(
                        Finding(
                            locator,
                            "ISSUE_TOKEN_ENV_INVALID",
                            "explicit token environment is limited to the reconciler "
                            "and named failure consumer",
                        )
                    )
    if workflow_name == "issue-guard.yml" and (
        issue_token_steps != 1
        or len(re.findall(r"\$\{\{\s*github\.token\s*\}\}", text)) != 1
    ):
        findings.append(
            Finding(
                workflow_name,
                "ISSUE_TOKEN_ENV_INVALID",
                "issue guard requires one explicit reconciler token reference",
            )
        )
    if workflow_name == "security.yml":
        security_jobs = document.get("jobs")
        raw_audit_job = (
            security_jobs.get("dependency-audit")
            if isinstance(security_jobs, Mapping)
            else None
        )
        audit_job = raw_audit_job if isinstance(raw_audit_job, Mapping) else {}
        raw_audit_env = audit_job.get("env")
        audit_env = raw_audit_env if isinstance(raw_audit_env, Mapping) else {}
        if str(audit_env.get("UV_PYTHON", "")) != "3.11":
            findings.append(
                Finding(
                    workflow_name,
                    "DEPENDENCY_AUDIT_PYTHON_INVALID",
                    "dependency audit must provision the supported Python floor",
                )
            )
        audit_command_count = sum(
            line.strip() == LOCKED_AUDIT_COMMAND
            for job_name, _, step in _workflow_steps(document)
            if job_name == "dependency-audit"
            for line in str(step.get("run", "")).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        if audit_command_count != 1:
            findings.append(
                Finding(
                    workflow_name,
                    "DEPENDENCY_AUDIT_COMMAND_INVALID",
                    "security must audit the locked graph at the Python floor",
                )
            )

    findings.extend(_action_findings(workflow_name, document, ci_lock))
    return sorted(set(findings))


def validate_repository(root: Path) -> list[Finding]:
    """Validate the complete committed workflow and CI dependency surface."""

    findings: list[Finding] = []
    workflow_root = root / ".github" / "workflows"
    actual_files = {
        path.name
        for pattern in ("*.yml", "*.yaml")
        for path in workflow_root.glob(pattern)
        if path.is_file()
    }
    for missing in sorted(EXPECTED_WORKFLOWS.difference(actual_files)):
        findings.append(
            Finding(missing, "WORKFLOW_MISSING", "required workflow is absent")
        )
    for extra in sorted(actual_files.difference(EXPECTED_WORKFLOWS)):
        findings.append(
            Finding(extra, "WORKFLOW_UNAPPROVED", "workflow is not approved")
        )
    try:
        ci_lock = load_ci_lock(root / ".github" / "ci-lock.json")
    except WorkflowPolicyError as exc:
        return [Finding(".github/ci-lock.json", "CI_LOCK_INVALID", str(exc))]

    used_actions: set[str] = set()
    for workflow_name in sorted(EXPECTED_WORKFLOWS.intersection(actual_files)):
        path = workflow_root / workflow_name
        try:
            text = path.read_text(encoding="utf-8")
            document = _load_workflow(path)
        except (OSError, UnicodeError, WorkflowPolicyError) as exc:
            findings.append(Finding(workflow_name, "WORKFLOW_INVALID", str(exc)))
            continue
        findings.extend(
            validate_workflow_document(workflow_name, document, text, ci_lock)
        )
        for _, _, step in _workflow_steps(document):
            uses = str(step.get("uses", ""))
            match = ACTION_REFERENCE.fullmatch(uses)
            if match is not None:
                used_actions.add("/".join(match.group("target").split("/")[:2]))

    locked_actions = set(_object(ci_lock["actions"], "ci-lock.actions"))
    for unused in sorted(locked_actions.difference(used_actions)):
        findings.append(
            Finding(unused, "CI_LOCK_ACTION_UNUSED", "locked action is unused")
        )

    security_path = workflow_root / "security.yml"
    if security_path.is_file():
        security_text = security_path.read_text(encoding="utf-8")
        tools = _object(ci_lock["tools"], "ci-lock.tools")
        gitleaks = _object(tools["gitleaks"], "ci-lock.tools.gitleaks")
        required_gitleaks_values = (
            str(gitleaks["version"]),
            str(gitleaks["archive"]),
            str(gitleaks["sha256"]),
            "gitleaks git --redact --no-banner --exit-code 1",
        )
        if any(value not in security_text for value in required_gitleaks_values):
            findings.append(
                Finding(
                    "security.yml",
                    "GITLEAKS_LOCK_MISMATCH",
                    "security workflow differs from the tool lock",
                )
            )

    return sorted(set(findings))


def _print_findings(findings: Sequence[Finding], output_format: str) -> None:
    if output_format == "json":
        print(
            json.dumps(
                [
                    {"code": item.code, "path": item.path, "detail": item.detail}
                    for item in findings
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return
    for item in findings:
        print(f"{item.code}\t{item.path}\t{item.detail}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--format", choices=("json", "text"), default="text")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        findings = validate_repository(root)
    except (OSError, WorkflowPolicyError) as exc:
        print(f"WORKFLOW_POLICY_FATAL:{type(exc).__name__}", file=sys.stderr)
        return 2
    _print_findings(findings, args.format)
    if findings:
        return 1
    if args.format == "text":
        print("workflow-policy: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
