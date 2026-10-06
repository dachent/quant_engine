"""Batch V1 and V14: the weekly source identity and its anchor (instrument fixes).

V1  the weekly source manifest entry carries a NESTED ``schema {name, version}``
    (generation.json), never flat ``schema_name`` / ``schema_version``; the contract
    version is 2 from 8302d4e on.  The validator read the flat shape, so it could never
    pass; a genuinely wrong nested name or version must still block.
V14 the visible current week is anchored to the DECLARED cutoff.  A manifest
    ``as_of_date`` is an execution date and may lie after the cutoff (N54); a source
    that ends after the cutoff, or that trails the cutoff by more than the last
    completed week (its final trading day, the previous Friday), is not the source at the cutoff.  A source that is anchored at the
    cutoff, or trails it only to the previous week's last trading day, still passes.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from acceptance.validate import checks

SOURCE_ARTIFACT_ID = "input/portfolio-weekly-returns.json"
CHECK = "dates.weekly_current_week"


def _context(
    tmp_path: Path,
    *,
    cutoff: date,
    source_as_of: str,
    schema: Any = None,
    current_week: dict[str, Any] | None = None,
    panel_as_of: str | None = None,
) -> checks.Context:
    source = {"as_of_date": source_as_of}
    raw = json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    anchor = date.fromisoformat(source_as_of)
    week_start = date.fromordinal(anchor.toordinal() - anchor.weekday())
    week_end = date.fromordinal(week_start.toordinal() + 6)
    week = current_week or {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "is_partial": anchor < week_end,
        "default_visible": True,
        "data_through_date": source_as_of,
    }
    artifact: dict[str, Any] = {
        "artifact_id": SOURCE_ARTIFACT_ID,
        "as_of_date": source_as_of,
        "size_bytes": len(raw),
        "sha256": digest,
        "schema": schema
        if schema is not None
        else {"name": "portfolio-weekly-returns", "version": "2"},
    }
    return checks.Context(
        gen=SimpleNamespace(
            gen_dir=tmp_path,
            manifest={"artifacts": [artifact]},
            scan=SimpleNamespace(files={}),
            panel={
                "weekly_returns": {
                    "status": "AVAILABLE",
                    "as_of_date": panel_as_of or source_as_of,
                    "generation_id": "a" * 64,
                    "source_sha256": digest,
                    "current_week": week,
                }
            },
        ),
        cutoff=cutoff,
        cutoff_source="synthetic verified cutoff",
    )


def _weekly(findings: list[checks.Finding]) -> list[checks.Finding]:
    return [finding for finding in findings if finding.check_id == CHECK]


# ------------------------------------------------------------------------------ V1


def test_v1_nested_schema_version_2_is_accepted(tmp_path: Path) -> None:
    context = _context(tmp_path, cutoff=date(2026, 9, 17), source_as_of="2026-09-17")
    assert _weekly(list(checks.check_weekly_current_week(context))) == []


@pytest.mark.parametrize(
    "schema",
    [
        {"name": "portfolio-weekly-returns", "version": "1"},
        {"name": "portfolio-weekly-returns", "version": None},
        {"name": "some-other-schema", "version": "2"},
        "portfolio-weekly-returns/2",
    ],
    ids=["version-1", "no-version", "wrong-name", "not-a-mapping"],
)
def test_v1_a_wrong_nested_schema_still_blocks(tmp_path: Path, schema: Any) -> None:
    context = _context(
        tmp_path, cutoff=date(2026, 9, 17), source_as_of="2026-09-17", schema=schema
    )
    findings = _weekly(list(checks.check_weekly_current_week(context)))
    assert [finding.klass for finding in findings] == [checks.BLOCKER_AUTHORITY]
    assert "schema" in findings[0].message


def test_v1_the_flat_shape_is_not_a_schema(tmp_path: Path) -> None:
    context = _context(tmp_path, cutoff=date(2026, 9, 17), source_as_of="2026-09-17")
    artifact = context.gen.manifest["artifacts"][0]
    del artifact["schema"]
    artifact["schema_name"] = "portfolio-weekly-returns"
    artifact["schema_version"] = "2"
    findings = _weekly(list(checks.check_weekly_current_week(context)))
    assert [finding.klass for finding in findings] == [checks.BLOCKER_AUTHORITY]


# ----------------------------------------------------------------------------- V14


def test_v14_a_source_dated_after_the_cutoff_blocks(tmp_path: Path) -> None:
    """N54: the manifest as_of is the execution date, the cutoff is 2026-09-17."""

    context = _context(tmp_path, cutoff=date(2026, 9, 17), source_as_of="2026-10-04")
    findings = _weekly(list(checks.check_weekly_current_week(context)))
    assert [finding.klass for finding in findings] == [checks.BLOCKER_AUTHORITY]
    assert findings[0].entity == "weekly_returns.source_cutoff"
    assert findings[0].expected == {"source_as_of_not_after": "2026-09-17"}


def test_v14_a_stale_source_still_blocks(tmp_path: Path) -> None:
    """The anchor fix must not let a genuinely stale source pass."""

    context = _context(tmp_path, cutoff=date(2026, 9, 17), source_as_of="2026-09-03")
    findings = _weekly(list(checks.check_weekly_current_week(context)))
    assert [finding.klass for finding in findings] == [checks.BLOCKER_AUTHORITY]
    assert findings[0].entity == "weekly_returns.source_cutoff"
    assert findings[0].expected == {"source_as_of_at_least": "2026-09-11"}


def test_v14_source_at_the_cutoff_passes(tmp_path: Path) -> None:
    context = _context(tmp_path, cutoff=date(2026, 9, 17), source_as_of="2026-09-17")
    assert _weekly(list(checks.check_weekly_current_week(context))) == []


@pytest.mark.parametrize("source_as_of", ["2026-09-20", "2026-09-18"])
def test_v14_source_at_the_previous_week_boundary_passes(
    tmp_path: Path, source_as_of: str
) -> None:
    """Monday cutoff, source through the preceding Sunday or Friday: last completed week."""

    context = _context(tmp_path, cutoff=date(2026, 9, 21), source_as_of=source_as_of)
    assert _weekly(list(checks.check_weekly_current_week(context))) == []


def test_v14_a_source_older_than_the_previous_week_boundary_blocks(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, cutoff=date(2026, 9, 21), source_as_of="2026-09-17")
    findings = _weekly(list(checks.check_weekly_current_week(context)))
    assert [finding.klass for finding in findings] == [checks.BLOCKER_AUTHORITY]
