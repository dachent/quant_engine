"""Deterministic normalized outputs derived from staged raw response bytes.

Shared by the runner and the independent verifier, so a verifier re-derives the
exact bytes the runner wrote and compares them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from quant_engine.alpaca.capture.normalize import (
    normalize_account,
    normalize_activities,
    normalize_positions,
    parse_json_bytes,
    summarize_activities,
)
from quant_engine.alpaca.capture.util import canonical_json

ACCOUNT_FILE = "normalized/account.json"
POSITIONS_FILE = "normalized/positions.json"
ACTIVITIES_FILE = "normalized/activities.jsonl"
SUMMARY_FILE = "normalized/summary.json"


def _line(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def derive_outputs(
    *,
    binding_sha256: str,
    account_body: bytes,
    positions_body: bytes | None,
    activity_bodies: Sequence[bytes],
) -> dict[str, bytes]:
    """Return ``relative path -> bytes`` for every normalized output."""

    outputs: dict[str, bytes] = {
        ACCOUNT_FILE: _line(
            normalize_account(parse_json_bytes(account_body), binding_sha256)
        )
    }
    if positions_body is not None:
        outputs[POSITIONS_FILE] = _line(
            normalize_positions(parse_json_bytes(positions_body))
        )
    records = normalize_activities([parse_json_bytes(body) for body in activity_bodies])
    outputs[ACTIVITIES_FILE] = b"".join(_line(record) for record in records)
    outputs[SUMMARY_FILE] = _line(summarize_activities(records))
    return outputs
