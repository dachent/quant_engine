"""Fail closed when a local push would update the remote main branch."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import re
import sys
from typing import TextIO


PROTECTED_REF = "refs/heads/main"
ZERO_OID = "0" * 40
OID_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class PushPolicyInputError(ValueError):
    """Raised when Git hook input cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class PushUpdate:
    """One update record supplied by Git's pre-push hook."""

    local_ref: str
    local_oid: str
    remote_ref: str
    remote_oid: str


def parse_updates(lines: Iterable[str]) -> tuple[PushUpdate, ...]:
    """Parse bounded pre-push records without rendering untrusted input."""

    updates: list[PushUpdate] = []
    for raw_line in lines:
        if not raw_line.strip():
            continue
        fields = raw_line.split()
        if len(fields) != 4:
            raise PushPolicyInputError("PUSH_POLICY_INPUT_INVALID")
        local_ref, local_oid, remote_ref, remote_oid = fields
        if (
            (local_ref != "(delete)" and not local_ref.startswith("refs/"))
            or not remote_ref.startswith("refs/")
            or OID_PATTERN.fullmatch(local_oid) is None
            or OID_PATTERN.fullmatch(remote_oid) is None
            or (local_ref == "(delete)" and local_oid != ZERO_OID)
        ):
            raise PushPolicyInputError("PUSH_POLICY_INPUT_INVALID")
        updates.append(
            PushUpdate(
                local_ref=local_ref,
                local_oid=local_oid,
                remote_ref=remote_ref,
                remote_oid=remote_oid,
            )
        )
    return tuple(updates)


def denied_reason(updates: Iterable[PushUpdate]) -> str | None:
    """Return a stable reason code if any record targets protected main."""

    for update in updates:
        if update.remote_ref == PROTECTED_REF:
            if update.local_oid == ZERO_OID:
                return "QE_MAIN_DELETE_DENIED"
            return "QE_MAIN_UPDATE_DENIED"
    return None


def run(input_stream: TextIO, error_stream: TextIO) -> int:
    """Evaluate hook input and emit only stable, non-sensitive diagnostics."""

    try:
        updates = parse_updates(input_stream)
    except PushPolicyInputError as exc:
        print(str(exc), file=error_stream)
        return 2
    reason = denied_reason(updates)
    if reason is not None:
        print(reason, file=error_stream)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    return run(sys.stdin, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
