"""The weekly cutoff must remain source-bound and internally consistent."""

from datetime import date
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from acceptance.validate import checks


def test_weekly_current_week_requires_source_through_date_to_equal_cutoff(tmp_path: Path) -> None:
    source = {"as_of_date": "2026-09-17"}
    raw = json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    artifact_id = "input/portfolio-weekly-returns.json"
    digest = hashlib.sha256(raw).hexdigest()
    current_week = {
        "week_start": "2026-09-14",
        "week_end": "2026-09-20",
        "is_partial": True,
        "default_visible": True,
        "data_through_date": "2026-09-16",
    }
    context = checks.Context(
        gen=SimpleNamespace(
            gen_dir=tmp_path,
            manifest={
                "artifacts": [
                    {
                        "artifact_id": artifact_id,
                        "as_of_date": "2026-09-17",
                        "size_bytes": len(raw),
                        "sha256": digest,
                        "schema": {"name": "portfolio-weekly-returns", "version": "2"},
                    }
                ]
            },
            scan=SimpleNamespace(files={}),
            panel={
                "weekly_returns": {
                    "status": "AVAILABLE",
                    "as_of_date": "2026-09-17",
                    "generation_id": "a" * 64,
                    "source_sha256": digest,
                    "current_week": current_week,
                }
            },
        ),
        cutoff=date(2026, 9, 17),
        cutoff_source="synthetic verified cutoff",
    )

    findings = list(checks.check_weekly_current_week(context))

    assert any(
        isinstance(finding.expected, dict)
        and finding.expected.get("data_through_date") == "2026-09-17"
        for finding in findings
    )
