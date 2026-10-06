"""C6 binds the visible week to the admitted weekly source, not the portfolio cutoff."""

from datetime import date
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from acceptance.validate import checks


SOURCE_ARTIFACT_ID = "input/portfolio-weekly-returns.json"


def _context(tmp_path: Path, weekly: dict[str, object], cutoff: date, source=None):
    artifacts = []
    files = {}
    if source is not None:
        raw = json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        artifacts.append(
            {
                "artifact_id": SOURCE_ARTIFACT_ID,
                "sha256": digest,
                "size_bytes": len(raw),
                "as_of_date": source.get("as_of_date"),
                "schema_name": "portfolio-weekly-returns",
                "schema_version": "2",
            }
        )
    generation = SimpleNamespace(
        gen_dir=tmp_path,
        manifest={"artifacts": artifacts},
        scan=SimpleNamespace(files=files),
        panel={"weekly_returns": weekly},
    )
    return checks.Context(
        gen=generation,
        cutoff=cutoff,
        cutoff_source="synthetic financial cutoff",
    )


def test_weekly_current_week_uses_manifest_bound_source_cutoff_when_financial_cutoff_differs(
    tmp_path: Path,
) -> None:
    source_as_of = "2026-09-20"
    weekly = {
        "status": "AVAILABLE",
        "as_of_date": source_as_of,
        "generation_id": "a" * 64,
        "source_sha256": hashlib.sha256(
            json.dumps(
                {"as_of_date": source_as_of}, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        "current_week": {
            "week_start": "2026-09-14",
            "week_end": "2026-09-20",
            "is_partial": False,
            "default_visible": True,
            "data_through_date": source_as_of,
        },
    }
    context = _context(
        tmp_path,
        weekly,
        date(2026, 9, 21),
        {"as_of_date": source_as_of},
    )

    assert list(checks.check_weekly_current_week(context)) == []


def test_weekly_source_hash_must_match_manifest_snapshot_hash(tmp_path: Path) -> None:
    source_as_of = "2026-09-20"
    weekly = {
        "status": "AVAILABLE",
        "as_of_date": source_as_of,
        "generation_id": "a" * 64,
        "source_sha256": "b" * 64,
        "current_week": {
            "week_start": "2026-09-14",
            "week_end": "2026-09-20",
            "is_partial": False,
            "default_visible": True,
            "data_through_date": source_as_of,
        },
    }
    context = _context(
        tmp_path,
        weekly,
        date(2026, 9, 21),
        {"as_of_date": source_as_of},
    )

    findings = list(checks.check_weekly_current_week(context))

    assert any(finding.klass == checks.BLOCKER_AUTHORITY for finding in findings)


def test_weekly_available_requires_manifest_bound_source_artifact(tmp_path: Path) -> None:
    weekly = {
        "status": "AVAILABLE",
        "as_of_date": "2026-09-20",
        "generation_id": "a" * 64,
        "source_sha256": "b" * 64,
        "current_week": {
            "week_start": "2026-09-14",
            "week_end": "2026-09-20",
            "is_partial": False,
            "default_visible": True,
            "data_through_date": "2026-09-20",
        },
    }
    context = _context(tmp_path, weekly, date(2026, 9, 20))

    findings = list(checks.check_weekly_current_week(context))

    assert any(finding.klass == checks.BLOCKER_AUTHORITY for finding in findings)


def test_unavailable_weekly_source_cannot_hide_a_bound_source_artifact(tmp_path: Path) -> None:
    weekly = {
        "status": "UNAVAILABLE_NOT_CONFIGURED",
        "as_of_date": "2026-09-20",
        "generation_id": None,
        "source_sha256": None,
        "current_week": {
            "week_start": None,
            "week_end": None,
            "is_partial": False,
            "default_visible": True,
            "data_through_date": None,
        },
    }
    context = _context(
        tmp_path,
        weekly,
        date(2026, 9, 20),
        {"as_of_date": "2026-09-20"},
    )

    findings = list(checks.check_weekly_current_week(context))

    assert any(finding.klass == checks.BLOCKER_AUTHORITY for finding in findings)


def test_truly_unconfigured_weekly_source_remains_unavailable_without_c6_finding(
    tmp_path: Path,
) -> None:
    weekly = {
        "status": "UNAVAILABLE_NOT_CONFIGURED",
        "as_of_date": "2026-09-20",
        "generation_id": None,
        "source_sha256": None,
        "current_week": {
            "week_start": None,
            "week_end": None,
            "is_partial": False,
            "default_visible": True,
            "data_through_date": None,
        },
    }
    context = _context(tmp_path, weekly, date(2026, 9, 20))

    assert list(checks.check_weekly_current_week(context)) == []
