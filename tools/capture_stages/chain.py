"""One source end to end (#913): S1 record -> S2 hash -> S3 verify with the source's adapter -> S4 admit.

The recorder writes a NEW run directory under the scratch root (never OneDrive), a login/2FA wall surfaces as ``BLOCKED_LOGIN``
without anything admitted, and a failing verdict is returned with its codes and admits nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

try:
    import capture_pipeline as cp
    from statement_browser import StageBlocked
except ImportError:  # pragma: no cover - imported as tools.capture_stages.chain
    from tools import capture_pipeline as cp
    from tools.capture_stages.statement_browser import StageBlocked


def run_source(
    source: cp.Source,
    *,
    record: Callable[[Path], Any],
    adapter: Callable[
        [Path, Mapping[str, Any]], tuple[str, dict[str, Callable[[], cp.CheckResult]]]
    ],
    run_dir: Path,
    manifest_path: Path,
    evidence_root: Path,
    pin_path: Path,
    cutoff: str,
    captured_on: str,
) -> dict[str, Any]:
    """Returns ``{"status": "ADMITTED"|"FAIL"|"BLOCKED_LOGIN", "codes": [...], "entry": {...}|None}``. ``captured_on`` is the
    date the recorder ran (the adapter's as-of may not be after it)."""
    try:
        record(Path(run_dir))
    except StageBlocked as exc:
        return {"status": exc.code, "codes": [exc.code], "entry": None}
    manifest = cp.hash_files(
        Path(run_dir),
        Path(manifest_path),
        source_id=source.source_id,
        captured_on=captured_on,
        tool=source.stage_tool or "",
    )
    try:
        as_of, checks = adapter(Path(run_dir), source.check_params)
    except (
        ValueError,
        OSError,
    ) as exc:  # an unreadable capture fails S3, it does not crash the refresh
        verdict = cp.verify(
            source, manifest=manifest, as_of=cutoff, cutoff=cutoff, checks={}
        )
        return {
            "status": "FAIL",
            "codes": [*verdict.codes, f"ADAPTER:{exc}"],
            "entry": None,
        }
    verdict = cp.verify(
        source, manifest=manifest, as_of=as_of, cutoff=cutoff, checks=checks
    )
    if verdict.status != "PASS":
        return {"status": "FAIL", "codes": list(verdict.codes), "entry": None}
    entry = cp.admit_input(
        source,
        verdict,
        run_dir=Path(run_dir),
        manifest_path=Path(manifest_path),
        evidence_root=Path(evidence_root),
        pin_path=Path(pin_path),
    )
    return {"status": "ADMITTED", "codes": [], "entry": entry}
