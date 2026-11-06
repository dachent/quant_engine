"""Executable hand-offs of a capture: the request plan and the generation source spec.

``build_plan`` writes the plan of one-day override requests (one per trading day) from a first
day, a last day, the non-trading days and the expected account labels, so the plan file and its
hash are produced, not typed.  ``bind_spec`` adds the verified runs of a capture to a base
generation spec (the R2.1 spec is the model) as ``REFRESH_FLEX_ACTIVITY`` sources with their
actual sha256 and size, sets the generation date, optionally declares general exclusions, and
refuses the result unless the generation builder itself accepts it (``--preflight``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any

from ._util import canonical_json_bytes, parse_iso_date
from .catalog import KIND_ACTIVITY, catalog_sha256, get_query
from .governance import MIN_GET_ATTEMPTS_OVERRIDE, TERMS_SHA256, TERMS_VERSION
from .normalize import normalize_run
from .plan import CaptureRequest, PlanError, RequestPlan, plan_from_document
from .staging import write_new_file
from .verify import verify_run

# the refresh-capable generation spec schemas (2 added the Flex refresh and TWS roles)
SPEC_SCHEMA_VERSIONS = (2, 3)
_REFRESH_ROLE = "REFRESH_FLEX_ACTIVITY"
_ACTIVITY_SCHEMA = ("activity-flex", "3")


class BindError(ValueError):
    """The plan inputs or the capture runs cannot be bound into a spec."""


def build_plan(
    *,
    first_day: date,
    last_day: date,
    pull_date: date,
    labels: Sequence[str],
    non_trading_days: Sequence[date] = (),
    query: str = "activity-history",
) -> RequestPlan:
    """One override request per trading day of ``first_day``..``last_day`` (a role used once per day)."""

    if first_day > last_day:
        raise BindError("the first day is after the last day")
    if get_query(query).kind != KIND_ACTIVITY:
        raise BindError("a day plan is for an activity query")
    skipped = set(non_trading_days)
    days = []
    cursor = first_day
    while cursor <= last_day:
        if cursor.weekday() < 5 and cursor not in skipped:
            days.append(cursor)
        cursor += timedelta(days=1)
    if not days:
        raise BindError("the window holds no trading day")
    plan = RequestPlan(
        pull_date=pull_date,
        requests=tuple(CaptureRequest(query, day, day, True) for day in days),
        expected_account_labels=tuple(labels),
        known_non_trading_days=tuple(sorted(skipped)),
    )
    # the same checks a plan read from a file goes through (cap, labels, finality)
    try:
        return plan_from_document(plan.to_document())
    except PlanError as exc:
        raise BindError(str(exc)) from None


def write_plan(plan: RequestPlan, out: Path) -> None:
    write_new_file(out, canonical_json_bytes(plan.to_document()))


def build_evidence(
    *,
    plan: RequestPlan,
    review_identity: str,
    review_reference: str,
    issued_at: datetime,
    expires_at: datetime,
    max_get_attempts: int,
) -> dict[str, Any]:
    """The governance evidence document for one plan (the comment it cites is made first).

    It names the plan hash, the catalog and the accepted terms from the code, the send budget equal
    to the plan size, and a GetStatement budget that an override plan's authorization accepts.
    """

    if issued_at.tzinfo is None or expires_at.tzinfo is None:
        raise BindError("the evidence times need a UTC offset")
    if expires_at <= issued_at:
        raise BindError("the evidence expires before it is issued")
    if not 1 <= max_get_attempts <= 9:
        raise BindError("max_get_attempts is outside 1..9")
    if (
        any(request.window_override for request in plan.requests)
        and max_get_attempts < MIN_GET_ATTEMPTS_OVERRIDE
    ):
        raise BindError(
            f"a plan that overrides its windows needs max_get_attempts of at least "
            f"{MIN_GET_ATTEMPTS_OVERRIDE}"
        )

    def stamp(moment: datetime) -> str:
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "schema_version": 1,
        "operation": "flex-capture",
        "plan": {"sha256": plan.sha256},
        "review": {"identity": review_identity, "reference": review_reference},
        "authority": {
            "status": "CURRENT",
            "issued_at": stamp(issued_at),
            "expires_at": stamp(expires_at),
        },
        "terms": {"version": TERMS_VERSION, "sha256": TERMS_SHA256},
        "catalog": {"sha256": catalog_sha256()},
        "metering": {
            "max_send_requests": len(plan.requests),
            "max_get_attempts": max_get_attempts,
            "retry_count": 0,
        },
    }


def _run_request(run_dir: Path) -> tuple[CaptureRequest, str]:
    manifest_path = run_dir / "capture-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise BindError(f"{run_dir.name} has no readable capture manifest") from None
    if not isinstance(manifest, dict) or manifest.get("status") != "STAGED":
        raise BindError(f"{run_dir.name} is not a STAGED run")
    try:
        plan = plan_from_document(manifest["plan"])
        request = plan.requests[int(manifest["request_index"])]
    except (PlanError, KeyError, IndexError, TypeError, ValueError):
        raise BindError(f"{run_dir.name} names no valid plan request") from None
    if (
        get_query(request.query).kind != KIND_ACTIVITY
        or not request.window_override
        or request.window_from != request.window_to
    ):
        raise BindError(f"{run_dir.name} is not a one-day override activity run")
    return request, str(manifest["run_id"])


def bind_spec(
    *,
    base_spec: Path,
    runs: Sequence[Path],
    root_id: str,
    as_of: date,
    out: Path,
    exclusions: Mapping[str, str] | None = None,
    verify: bool = True,
    preflight: bool = False,
) -> dict[str, Any]:
    """Write ``out``: the base spec plus the runs as refresh sources, each with its real hash."""

    try:
        spec = json.loads(base_spec.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise BindError("the base spec is not readable JSON") from None
    if (
        not isinstance(spec, dict)
        or spec.get("schema_version") not in SPEC_SCHEMA_VERSIONS
    ):
        raise BindError("the base spec must be schema version 2 or 3")
    roots = spec.get("roots")
    if not isinstance(roots, dict) or root_id not in roots:
        raise BindError("the root id is not in the base spec")
    root = Path(str(roots[root_id])).resolve()
    sources = list(spec["sources"])
    known = {item["artifact_id"] for item in sources}
    added: list[dict[str, Any]] = []
    days: set[date] = set()
    for run_dir in sorted(runs, key=lambda path: path.name):
        request, _ = _run_request(run_dir)
        if verify:
            with tempfile.TemporaryDirectory(prefix="bind-verify-") as scratch:
                normalized = Path(scratch) / "normalized.json"
                normalize_run(run_dir, normalized)
                verify_run(run_dir, normalized)
        statement = (run_dir / "statement.xml").resolve()
        try:
            relative = statement.relative_to(root)
        except ValueError:
            raise BindError(f"{run_dir.name} lies outside the root {root_id}") from None
        artifact_id = relative.as_posix()
        if artifact_id in known:
            raise BindError(f"{artifact_id} is already a source")
        if request.window_to in days:
            raise BindError(f"two runs cover {request.window_to.isoformat()}")
        days.add(request.window_to)
        payload = statement.read_bytes()
        known.add(artifact_id)
        added.append(
            {
                "artifact_id": artifact_id,
                "as_of_date": request.window_to.isoformat(),
                "expected_sha256": hashlib.sha256(payload).hexdigest(),
                "expected_size_bytes": len(payload),
                "relative_path": artifact_id,
                "role": _REFRESH_ROLE,
                "root_id": root_id,
                "schema_name": _ACTIVITY_SCHEMA[0],
                "schema_version": _ACTIVITY_SCHEMA[1],
            }
        )
    if not added:
        raise BindError("no run to bind")
    added.sort(key=lambda item: item["as_of_date"])
    spec["sources"] = sources + added
    newest = max(
        [parse_iso_date(item["as_of_date"], "source as_of") for item in spec["sources"]]
    )
    if as_of < newest:
        raise BindError("the generation date is older than a source")
    spec["as_of_date"] = as_of.isoformat()
    if exclusions:
        declared = list(spec.get("declared_general_exclusions") or [])
        seen = {item["gap"] for item in declared}
        for gap, provenance in sorted(exclusions.items()):
            if gap in seen:
                raise BindError("a declared exclusion is repeated")
            declared.append({"gap": gap, "provenance": provenance})
        spec["declared_general_exclusions"] = declared
    body = (json.dumps(spec, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if preflight:
        # the generation builder's own checks (hashes, role counts, crosswalk, the end rule),
        # run on a scratch copy; nothing is written to `out` unless they pass
        from quant_engine.ibkr.generation import IbkrGenerationError, build_generation

        with tempfile.TemporaryDirectory(prefix="bind-preflight-") as scratch:
            candidate = Path(scratch) / "spec.json"
            candidate.write_bytes(body)
            try:
                build_generation(candidate)
            except IbkrGenerationError as exc:
                raise BindError(
                    f"the generation builder refused the spec: {exc}"
                ) from None
    write_new_file(out, body)
    return {
        "spec_sha256": hashlib.sha256(body).hexdigest(),
        "added_sources": len(added),
        "first_day": added[0]["as_of_date"],
        "last_day": added[-1]["as_of_date"],
        "preflight": "PASSED" if preflight else "NOT_RUN",
    }
