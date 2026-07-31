"""Build, verify, QA, and publish the private read-only control panel."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Final, Mapping, Sequence

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationConflictError,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    publish_generation,
    read_current,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


PANEL_SCHEMA_VERSION: Final = 1
BUILDER_VERSION: Final = "1.0.0"
SOURCE_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "portfolio-control-panel-source-v1.json"
)
DEFAULT_TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[1] / "apps" / "portfolio-control-panel"
)
PANEL_FILES: Final = (
    "index.html",
    "styles.css",
    "app.js",
    "data/dashboard_data.json",
    "data/dashboard_data.js",
)
VIEWS: Final = ("command", "capital", "risk", "signals", "candidates", "data")
CAPTURES: Final = (
    "desktop-command",
    "desktop-risk",
    "desktop-candidates",
    "desktop-data",
    "mobile-command",
    "direct-file",
)


class ControlPanelValidationError(ValueError):
    """An input or generated control-panel artifact violates its contract."""


def _object(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ControlPanelValidationError(f"{field_name} must be an object")
    return value


def _objects(value: object, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ControlPanelValidationError(f"{field_name} must be an array")
    return [_object(item, field_name) for item in value]


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControlPanelValidationError(f"{field_name} must be non-empty text")
    return value.strip()


def _load_json(path: Path, field_name: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ControlPanelValidationError(f"{field_name} is missing or linked")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(f"{field_name} is not readable JSON") from exc
    return _object(decoded, field_name)


def _hash(path: Path) -> str:
    return hash_file(path)[0]


def _current_report(root: Path) -> tuple[dict[str, Any], dict[str, str], Path]:
    try:
        pointer = read_current(root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ControlPanelValidationError("report pointer is not accepted") from exc
    path = root / "generations" / pointer.generation_id / "portfolio-report-data.json"
    report = _load_json(path, "portfolio report data")
    if report.get("report_schema_version") != 1:
        raise ControlPanelValidationError("portfolio report schema is unsupported")
    return report, {"generation_id": pointer.generation_id, "sha256": _hash(path)}, path


def _source_inputs(legacy_panel: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    contract = _load_json(SOURCE_CONTRACT, "control-panel source contract")
    if contract.get("contract_version") != 1:
        raise ControlPanelValidationError(
            "control-panel source contract is unsupported"
        )
    source_files = _object(contract.get("source_files"), "source_files")
    observed: dict[str, str] = {}
    for relative, expected in source_files.items():
        path = legacy_panel / relative
        actual = _hash(path)
        if actual != _text(expected, f"source_files.{relative}"):
            raise ControlPanelValidationError(f"legacy source hash differs: {relative}")
        observed[relative] = actual
    appendix = _object(contract.get("frozen_appendix"), "frozen_appendix")
    data_relative = _text(appendix.get("data_file"), "frozen_appendix.data_file")
    data_path = legacy_panel / data_relative
    if _hash(data_path) != appendix.get("data_sha256"):
        raise ControlPanelValidationError("frozen appendix data hash differs")
    legacy = _load_json(data_path, "frozen candidate appendix")
    providers = _objects(legacy.get("providers"), "legacy.providers")
    if len(providers) != appendix.get("candidate_count"):
        raise ControlPanelValidationError("frozen candidate count differs")
    return legacy, {"contract": contract, "observed_hashes": observed}


def _summary(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = _objects(report.get("portfolio_summary"), "portfolio_summary")
    return {_text(row.get("metric"), "summary.metric"): row for row in rows}


def _summary_value(summary: Mapping[str, Mapping[str, Any]], metric: str) -> str:
    row = summary.get(metric)
    if row is None:
        raise ControlPanelValidationError(f"portfolio summary lacks {metric}")
    return _text(row.get("value"), f"portfolio_summary.{metric}")


def _fact_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _objects(report.get("coverage_data_quality"), "coverage_data_quality")


def _panel_data(
    report: Mapping[str, Any],
    report_identity: Mapping[str, str],
    legacy: Mapping[str, Any],
    source: Mapping[str, Any],
    template_root: Path,
) -> dict[str, Any]:
    summary = _summary(report)
    strategies = _objects(report.get("strategy_summary"), "strategy_summary")
    accounts = _objects(report.get("account_summary"), "account_summary")
    allocations = _objects(report.get("allocation_history"), "allocation_history")
    performance = _objects(report.get("twr_mwr"), "twr_mwr")
    coverage = _fact_rows(report)
    reconciliations = _objects(
        report.get("reconciliation_checks"), "reconciliation_checks"
    )
    if len(strategies) != 9 or len(allocations) != 10:
        raise ControlPanelValidationError(
            "current strategy/allocation boundary differs"
        )
    if len(accounts) != 19:
        raise ControlPanelValidationError("current account boundary differs")

    strategy_names = {
        _text(row.get("strategy_id"), "strategy.strategy_id"): _text(
            row.get("strategy_name"), "strategy.strategy_name"
        )
        for row in strategies
    }
    normalized_allocations = []
    for row in allocations:
        strategy_id = row.get("strategy_id")
        normalized_allocations.append(
            {
                **row,
                "strategy_name": (
                    "Zero-risk reserve"
                    if strategy_id is None
                    else strategy_names.get(str(strategy_id), "UNMAPPED")
                ),
            }
        )
    normalized_performance = [
        {
            **row,
            "strategy_name": (
                None
                if row.get("strategy_id") is None
                else strategy_names.get(str(row.get("strategy_id")), "UNMAPPED")
            ),
        }
        for row in performance
    ]

    fact_by_item = {
        str(row.get("item")): row
        for row in coverage
        if row.get("category") == "FACT_COVERAGE"
    }
    gates = []
    for item, label in (
        ("assignments", "Account/strategy mapping"),
        ("nav", "Daily NAV"),
        ("flows", "Cash flows"),
        ("positions", "Positions"),
        ("fx", "FX observations"),
    ):
        fact = fact_by_item.get(item)
        if fact is None:
            raise ControlPanelValidationError(f"coverage lacks {item}")
        gates.append(
            {
                "item": item,
                "label": label,
                "note": fact.get("known_gap")
                or "Accepted within the declared boundary",
                "status": fact.get("status"),
            }
        )
    blocking_items = [
        row
        for row in coverage
        if row.get("category") == "RECONCILIATION_EXCEPTION"
        and row.get("status") != "PASS"
    ]
    providers = _objects(legacy.get("providers"), "legacy.providers")
    allowed_candidate_fields = (
        "candidate",
        "ticker",
        "category",
        "tier",
        "portfolio_role",
        "philosophy_module",
        "execution",
        "cadence",
        "principal_gap",
    )
    frozen_providers = [
        {field: row.get(field) for field in allowed_candidate_fields}
        for row in providers
    ]
    tier_counts = Counter(str(row.get("tier")) for row in frozen_providers)
    provider_counts = Counter(
        _text(row.get("provider"), "account.provider") for row in accounts
    )
    twr_available = sum(
        str(row.get("twr_status", "")).startswith("AVAILABLE") for row in performance
    )
    mwr_available = sum(
        str(row.get("mwr_status", "")).startswith("AVAILABLE") for row in performance
    )
    reconciliation_passed = sum(row.get("status") == "PASS" for row in reconciliations)
    source_contract = _object(source.get("contract"), "source.contract")
    appendix = _object(source_contract.get("frozen_appendix"), "frozen_appendix")
    baseline = _object(report.get("baseline"), "baseline")
    template_hashes = {
        name: _hash(template_root / name) for name in ("index.html", "app.js")
    }
    provenance = _objects(report.get("provenance"), "provenance")
    provenance = [
        {
            "artifact": "portfolio-report-generation",
            "as_of_date": report.get("as_of_date"),
            "schema": "portfolio-report-data/1",
            "sha256": report_identity["sha256"],
        },
        *provenance,
        {
            "artifact": "frozen-candidate-appendix",
            "as_of_date": appendix.get("as_of_date"),
            "schema": "strategy-allocation-audit/frozen",
            "sha256": appendix.get("data_sha256"),
        },
    ]
    limitations = sorted(
        {
            "Read-only local artifact; no provider, broker, order, or deployment connection exists.",
            "The candidate view is a frozen audit appendix and is not strategy, selection, or allocation authority.",
            "Full-systematic HWM is unavailable until complete reconciled daily NAV exists.",
            "Positions and FX remain unavailable; the panel does not infer either.",
            "MWR remains unavailable when complete external-flow coverage is absent.",
            "Provider, account-live, predecessor, shadow, and backtest evidence are not concatenated.",
            *(str(row.get("known_gap")) for row in coverage if row.get("known_gap")),
        }
    )
    return {
        "account_summary": accounts,
        "advisory": {
            "correlation_one_stress": _summary_value(summary, "Correlation-one stress"),
            "hwm_limit": "0.15",
            "hwm_status": _summary_value(summary, "Full-systematic HWM"),
            "nav_coverage": _summary_value(summary, "NAV coverage"),
            "readiness": _summary_value(summary, "Advisory readiness"),
            "reason": "Full-systematic HWM, positions, FX, and complete attribution remain unavailable or partial.",
        },
        "allocations": normalized_allocations,
        "blocking_items": blocking_items,
        "candidate_appendix": {
            "as_of_date": appendix.get("as_of_date"),
            "providers": frozen_providers,
            "source_sha256": appendix.get("data_sha256"),
            "status": appendix.get("status"),
            "tier_counts": dict(sorted(tier_counts.items())),
        },
        "capital": {
            "reserve": _summary_value(summary, "Reserve"),
            "systematic": _summary_value(summary, "Systematic capital"),
            "total_liquid": _summary_value(summary, "Total liquid"),
        },
        "counts": {
            "accounts": len(accounts),
            "mwr_available": mwr_available,
            "reconciliation_checks": len(reconciliations),
            "reconciliation_passed": reconciliation_passed,
            "strategies": len(strategies),
            "twr_available": twr_available,
            "twr_rows": len(performance),
        },
        "coverage": coverage,
        "fact_status": {
            item: row.get("status") for item, row in sorted(fact_by_item.items())
        },
        "gates": gates,
        "limitations": limitations,
        "meta": {
            "baseline_generation_id": _text(
                baseline.get("generation_id"), "baseline.generation_id"
            ),
            "panel_mode": "LOCAL_DIRECT_FILE_READ_ONLY",
            "portfolio_as_of": _text(report.get("as_of_date"), "report.as_of_date"),
            "report_generation_id": report_identity["generation_id"],
            "report_sha256": report_identity["sha256"],
            "source_shell_hashes": source.get("observed_hashes"),
            "template_hashes": template_hashes,
        },
        "panel_schema_version": PANEL_SCHEMA_VERSION,
        "performance": normalized_performance,
        "provenance": provenance,
        "provider_counts": dict(sorted(provider_counts.items())),
        "reconciliation_checks": reconciliations,
        "strategies": strategies,
    }


def _rendered_files(
    report_root: Path, legacy_panel: Path, template_root: Path
) -> tuple[dict[str, bytes], dict[str, Any], Path, Path]:
    report, report_identity, report_path = _current_report(report_root)
    legacy, source = _source_inputs(legacy_panel)
    data = _panel_data(report, report_identity, legacy, source, template_root)
    data_bytes = canonical_json_bytes(data)
    files = {
        "index.html": (template_root / "index.html").read_bytes(),
        "styles.css": (legacy_panel / "styles.css").read_bytes(),
        "app.js": (template_root / "app.js").read_bytes(),
        "data/dashboard_data.json": data_bytes,
        "data/dashboard_data.js": b"window.PORTFOLIO_DATA = " + data_bytes + b";\n",
    }
    legacy_data = legacy_panel / "data" / "dashboard_data.json"
    return files, data, report_path, legacy_data


def _panel_id(files: Mapping[str, bytes]) -> str:
    identity = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in sorted(files.items())
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def _write_exact(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise GenerationConflictError(f"candidate file differs: {path}")
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def build_candidate(
    report_root: Path,
    legacy_panel: Path,
    output_root: Path,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
) -> dict[str, Any]:
    files, data, _, _ = _rendered_files(report_root, legacy_panel, template_root)
    panel_id = _panel_id(files)
    candidate = output_root / "candidates" / panel_id
    for relative, content in files.items():
        _write_exact(candidate / relative, content)
    artifact_hashes = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in sorted(files.items())
    }
    manifest = {
        "artifact_sha256": artifact_hashes,
        "panel_id": panel_id,
        "panel_schema_version": PANEL_SCHEMA_VERSION,
        "portfolio_as_of": data["meta"]["portfolio_as_of"],
        "report_generation_id": data["meta"]["report_generation_id"],
        "state": "STAGED",
    }
    _write_exact(candidate / "candidate.json", canonical_json_bytes(manifest))
    return {
        "candidate_dir": str(candidate),
        "candidate_rows": len(data["candidate_appendix"]["providers"]),
        "panel_id": panel_id,
        "strategy_count": data["counts"]["strategies"],
        "twr_available_count": data["counts"]["twr_available"],
    }


def _check(name: str, condition: bool, evidence: object) -> dict[str, Any]:
    return {
        "check": name,
        "evidence": evidence,
        "status": "PASS" if condition else "FAIL",
    }


def verify_candidate(
    report_root: Path,
    legacy_panel: Path,
    candidate_dir: Path,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
) -> dict[str, Any]:
    expected, data, _, _ = _rendered_files(report_root, legacy_panel, template_root)
    panel_id = _panel_id(expected)
    checks: list[dict[str, Any]] = []
    checks.append(
        _check("Candidate identity", candidate_dir.name == panel_id, panel_id)
    )
    exact = True
    for relative, content in expected.items():
        path = candidate_dir / relative
        matches = (
            path.is_file() and not path.is_symlink() and path.read_bytes() == content
        )
        exact = exact and matches
    checks.append(_check("Candidate files are deterministic", exact, list(PANEL_FILES)))
    html = expected["index.html"].decode("utf-8")
    javascript = expected["app.js"].decode("utf-8")
    checks.append(
        _check(
            "All six operator views are present",
            all(f'data-view="{view}"' in html for view in VIEWS),
            list(VIEWS),
        )
    )
    checks.append(
        _check(
            "No external frontend dependency",
            re.search(r'<(?:script|link)[^>]+(?:src|href)="https?://', html, re.I)
            is None,
            "relative local assets only",
        )
    )
    execution_pattern = re.compile(
        r"\b(fetch\s*\(|XMLHttpRequest|WebSocket|placeOrder|submitOrder|sendBeacon|brokerApi)\b",
        re.I,
    )
    checks.append(
        _check(
            "No network or trade execution code",
            execution_pattern.search(javascript) is None,
            "static embedded renderer",
        )
    )
    checks.append(
        _check(
            "Accepted capital replay",
            data["capital"]
            == {
                "reserve": "100043.62",
                "systematic": "410272.39",
                "total_liquid": "510316.01",
            },
            data["capital"],
        )
    )
    checks.append(
        _check(
            "Current strategy boundary",
            data["counts"]["strategies"] == 9
            and len(data["allocations"]) == 10
            and data["counts"]["accounts"] == 19,
            data["counts"],
        )
    )
    checks.append(
        _check(
            "Coverage-aware risk replay",
            data["advisory"]["hwm_status"]
            == "NOT_AUTOMATABLE_MISSING_COMPLETE_DAILY_NAV"
            and data["advisory"]["readiness"] == "DO_NOT_EXPAND_SYSTEMATIC_RISK",
            data["advisory"],
        )
    )
    checks.append(
        _check(
            "Frozen candidates remain isolated",
            data["candidate_appendix"]["status"] == "FROZEN_AUDIT_APPENDIX"
            and len(data["candidate_appendix"]["providers"]) == 33
            and not any("candidate" in row for row in data["strategies"]),
            {
                "as_of": data["candidate_appendix"]["as_of_date"],
                "rows": len(data["candidate_appendix"]["providers"]),
            },
        )
    )
    checks.append(
        _check(
            "Unavailable facts remain explicit",
            data["fact_status"].get("positions") == "FAILED"
            and data["fact_status"].get("fx") == "FAILED",
            {
                "fx": data["fact_status"].get("fx"),
                "positions": data["fact_status"].get("positions"),
            },
        )
    )
    result = {
        "all_passed": all(row["status"] == "PASS" for row in checks),
        "checks": checks,
        "checks_passed": sum(row["status"] == "PASS" for row in checks),
        "checks_total": len(checks),
        "panel_id": panel_id,
        "report_generation_id": data["meta"]["report_generation_id"],
    }
    if not result["all_passed"]:
        raise ControlPanelValidationError("candidate verification failed")
    return result


def _json_array(path: Path, field_name: str) -> list[object]:
    if path.is_symlink() or not path.is_file():
        raise ControlPanelValidationError(f"{field_name} is missing or linked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPanelValidationError(f"{field_name} is invalid JSON") from exc
    if not isinstance(value, list):
        raise ControlPanelValidationError(f"{field_name} must be an array")
    return value


def validate_qa(candidate_dir: Path, qa_dir: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    screenshot_sizes: dict[str, int] = {}
    console_counts: dict[str, int] = {}
    request_counts: dict[str, int] = {}
    target_matches: dict[str, bool] = {}
    for name in CAPTURES:
        capture = qa_dir / name
        screenshot = capture / "screenshot.png"
        screenshot_sizes[name] = (
            screenshot.stat().st_size if screenshot.is_file() else 0
        )
        console_counts[name] = len(
            _json_array(capture / "console-events.json", f"{name} console evidence")
        )
        request_counts[name] = len(
            _json_array(capture / "request-failures.json", f"{name} request evidence")
        )
        manifest = _load_json(capture / "manifest.json", f"{name} manifest")
        target_matches[name] = candidate_dir.name in str(manifest.get("target", ""))
    checks.extend(
        [
            _check(
                "Required browser screenshots exist",
                all(value > 0 for value in screenshot_sizes.values()),
                screenshot_sizes,
            ),
            _check(
                "Browser captures have no console errors",
                all(value == 0 for value in console_counts.values()),
                console_counts,
            ),
            _check(
                "Browser captures have no request failures",
                all(value == 0 for value in request_counts.values()),
                request_counts,
            ),
            _check(
                "Browser evidence targets this candidate",
                all(target_matches.values()),
                target_matches,
            ),
        ]
    )
    lint_counts: dict[str, int] = {}
    for name in ("desktop-command", "mobile-command"):
        lint = _load_json(qa_dir / name / "visual-lint.json", f"{name} visual lint")
        count = lint.get("finding_count")
        if not isinstance(count, int):
            raise ControlPanelValidationError(f"{name} visual lint count is invalid")
        lint_counts[name] = count
    checks.append(
        _check(
            "Desktop and mobile visual lint are clean",
            all(value == 0 for value in lint_counts.values()),
            lint_counts,
        )
    )
    interaction = _load_json(qa_dir / "interaction-results.json", "interaction QA")
    interaction_checks = _objects(interaction.get("checks"), "interaction.checks")
    checks.append(
        _check(
            "Interaction suite passes",
            interaction.get("all_passed") is True
            and len(interaction_checks) >= 6
            and all(row.get("status") == "PASS" for row in interaction_checks),
            {
                "checks": len(interaction_checks),
                "candidate": interaction.get("panel_id"),
            },
        )
    )
    checks.append(
        _check(
            "Direct-file runtime passes",
            console_counts["direct-file"] == 0
            and request_counts["direct-file"] == 0
            and screenshot_sizes["direct-file"] > 0,
            "file:// evidence",
        )
    )
    pdf = qa_dir / "desktop-command" / "page.pdf"
    checks.append(
        _check(
            "Command view PDF export exists",
            pdf.is_file() and pdf.stat().st_size > 0,
            pdf.stat().st_size if pdf.is_file() else 0,
        )
    )
    result = {
        "all_passed": all(row["status"] == "PASS" for row in checks),
        "checks": checks,
        "checks_passed": sum(row["status"] == "PASS" for row in checks),
        "checks_total": len(checks),
        "panel_id": candidate_dir.name,
    }
    if not result["all_passed"]:
        raise ControlPanelValidationError("browser QA failed")
    _write_exact(qa_dir / "qa-validation.json", canonical_json_bytes(result))
    return result


def _copy_exact(source: Path, destination: Path) -> None:
    content = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != content:
            raise GenerationConflictError(f"generation artifact differs: {destination}")
        return
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with (
        source.open("rb") as input_handle,
        os.fdopen(descriptor, "wb") as output_handle,
    ):
        shutil.copyfileobj(input_handle, output_handle)
        output_handle.flush()
        os.fsync(output_handle.fileno())


def publish_panel(
    report_root: Path,
    legacy_panel: Path,
    candidate_dir: Path,
    qa_dir: Path,
    output_root: Path,
    template_root: Path = DEFAULT_TEMPLATE_ROOT,
) -> dict[str, Any]:
    verification = verify_candidate(
        report_root, legacy_panel, candidate_dir, template_root
    )
    qa = validate_qa(candidate_dir, qa_dir)
    _, data, report_path, legacy_data = _rendered_files(
        report_root, legacy_panel, template_root
    )
    as_of = date.fromisoformat(_text(data["meta"]["portfolio_as_of"], "as_of"))
    artifacts: list[SourceArtifact] = [
        SourceArtifact.from_path(
            report_path,
            artifact_id="input/portfolio-report-data.json",
            as_of_date=as_of,
            schema_name="portfolio-report-data",
            schema_version="1",
        ),
        SourceArtifact.from_path(
            legacy_data,
            artifact_id="input/frozen-candidate-appendix.json",
            as_of_date=date.fromisoformat(data["candidate_appendix"]["as_of_date"]),
            schema_name="strategy-allocation-audit-candidates",
            schema_version="frozen-2026-07-17",
        ),
    ]
    for relative in (*PANEL_FILES, "candidate.json"):
        artifacts.append(
            SourceArtifact.from_path(
                candidate_dir / relative,
                artifact_id=f"panel/{relative}",
                as_of_date=as_of,
                schema_name="portfolio-control-panel-artifact",
                schema_version="1",
            )
        )
    qa_files = sorted(path for path in qa_dir.rglob("*") if path.is_file())
    for path in qa_files:
        relative = path.relative_to(qa_dir).as_posix()
        artifacts.append(
            SourceArtifact.from_path(
                path,
                artifact_id=f"evidence/{relative}",
                as_of_date=as_of,
                schema_name="portfolio-control-panel-qa",
                schema_version="1",
            )
        )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("portfolio-control-panel-builder", BUILDER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="accepted portfolio report rendered read-only with explicit gaps and a separate frozen candidate appendix",
            fact_types=("portfolio control panel", "browser QA", "frozen appendix"),
            observed_start=as_of,
            observed_end=as_of,
        ),
        artifacts=tuple(artifacts),
        verification=VerificationStatus.VERIFIED,
    )
    manifest_path = stage_generation(output_root, manifest)
    generation_dir = manifest_path.parent
    for relative in (*PANEL_FILES, "candidate.json"):
        _copy_exact(candidate_dir / relative, generation_dir / relative)
    for path in qa_files:
        _copy_exact(path, generation_dir / "qa" / path.relative_to(qa_dir))
    pointer = publish_generation(output_root, manifest.generation_id)
    return {
        "browser_checks": qa["checks_total"],
        "generation_id": manifest.generation_id,
        "panel_id": verification["panel_id"],
        "publication_state": pointer.state.value,
        "report_generation_id": verification["report_generation_id"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "verify", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--report-root", type=Path, required=True)
        command.add_argument("--legacy-panel", type=Path, required=True)
        command.add_argument(
            "--template-root", type=Path, default=DEFAULT_TEMPLATE_ROOT
        )
        command.add_argument("--output-root", type=Path, required=name != "verify")
        if name != "build":
            command.add_argument("--candidate-dir", type=Path, required=True)
        if name == "publish":
            command.add_argument("--qa-dir", type=Path, required=True)
    qa = commands.add_parser("qa")
    qa.add_argument("--candidate-dir", type=Path, required=True)
    qa.add_argument("--qa-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_candidate(
                args.report_root,
                args.legacy_panel,
                args.output_root,
                args.template_root,
            )
        elif args.command == "verify":
            result = verify_candidate(
                args.report_root,
                args.legacy_panel,
                args.candidate_dir,
                args.template_root,
            )
        elif args.command == "qa":
            result = validate_qa(args.candidate_dir, args.qa_dir)
        else:
            result = publish_panel(
                args.report_root,
                args.legacy_panel,
                args.candidate_dir,
                args.qa_dir,
                args.output_root,
                args.template_root,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"portfolio-control-panel: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
