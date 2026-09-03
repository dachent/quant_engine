"""Counts/hashes-only run manifest with a pre-finalization credential scan.

The manifest lists artifact paths, SHA-256 hashes, byte sizes, row counts,
and the route used. It never contains session tokens, cookies, position
values, or page content. A bearer/cookie scan runs over every would-be
manifest field before finalization; any hit fails the run.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from grizzly_capture import CredentialScanError
from grizzly_capture.persistence import ArtifactRecord

MANIFEST_FILENAME = "manifest.json"
MANIFEST_SCHEMA = "grizzly-capture-manifest/v1"
STATUS_TERMINAL = "TERMINAL"
STATUS_NON_TERMINAL = "NON_TERMINAL"

_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization-style scheme prefixes.
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    # JSON Web Tokens (header segment always starts with 'eyJ').
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"),
    # Keyword-prefixed secrets (key: value / key=value shapes). Bare hashes
    # such as artifact SHA-256 values do not match: a keyword must precede.
    re.compile(
        r"\b(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|"
        r"x-auth-token|session-token|access-token|refresh-token|api[-_]?key|"
        r"client[-_]?secret|password|passwd)\b\s*[:=]\s*[^\s,}\"']{4,}",
        re.IGNORECASE,
    ),
    # Cookie assignment shapes.
    re.compile(
        r"\b(?:sessionid|session_id|csrftoken|sid)=[A-Za-z0-9._-]{6,}",
        re.IGNORECASE,
    ),
)


def scan_manifest_fields(payload: Mapping[str, Any]) -> list[str]:
    """Return the JSON paths of credential-shaped strings (values not kept)."""
    hits: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, str):
            for pattern in _CREDENTIAL_PATTERNS:
                if pattern.search(value):
                    hits.append(path)
                    return
        elif isinstance(value, Mapping):
            for key, item in value.items():
                walk(str(key), f"{path}.<key>")
                walk(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(payload, "$")
    return hits


@dataclass
class CaptureManifest:
    """Counts/hashes-only description of one capture run."""

    model_slug: str
    base_url: str
    cdp_port: int
    route_requested: str
    status: str = STATUS_NON_TERMINAL
    route_used: str | None = None
    started_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    attempts: list[dict[str, str]] = field(default_factory=list)
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        artifacts = [asdict(record) for record in self.artifacts]
        trades_rows = sum(
            record.rows or 0
            for record in self.artifacts
            if record.source in ("download", "state")
        )
        positions_rows = sum(
            record.rows or 0
            for record in self.artifacts
            if record.source == "dom-table"
        )
        return {
            "schema": MANIFEST_SCHEMA,
            "status": self.status,
            "started_utc": self.started_utc,
            "model_slug": self.model_slug,
            "base_url": self.base_url,
            "cdp_port": self.cdp_port,
            "route_requested": self.route_requested,
            "route_used": self.route_used,
            "attempts": self.attempts,
            "artifacts": artifacts,
            "counts": {
                "artifacts": len(artifacts),
                "trades_rows": trades_rows,
                "positions_rows": positions_rows,
            },
            "notes": self.notes,
            "credential_scan": "passed",
        }

    def finalize(self, run_dir: Path) -> Path:
        """Credential-scan every field, then write the manifest atomically."""
        payload = self.to_dict()
        hits = scan_manifest_fields(payload)
        if hits:
            paths = ", ".join(sorted(set(hits)))
            raise CredentialScanError(
                f"credential-shaped content in manifest fields: {paths}",
            )
        target = run_dir / MANIFEST_FILENAME
        target.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return target
