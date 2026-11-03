"""Governed, create-only Flex capture session (SendRequest then GetStatement).

Order of operations for every planned request:

1. a governance reservation for the one SendRequest, then the call;
2. the exact response bytes are staged before they are interpreted;
3. GetStatement is polled only for error 1019, each attempt reserved first,
   with the documented 20/40/80/120 second backoff and the evidence's cap;
4. any other non-report response is a refusal: nothing becomes a statement,
   no second SendRequest is made, and the remaining requests are not attempted.

This module is never exercised against the network in the repository: callers
inject the transport, the clock and the credentials.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import time

from .credentials import TOKEN_FIELD, SecretValue
from quant_engine.ibkr.flex import GET_STATEMENT_URL, SEND_REQUEST_URL, HttpGet

from ._util import sha256_hex
from .catalog import SERVICE_VERSION, get_query
from .governance import LiveReadGovernance
from .plan import RequestPlan
from .responses import STEP_GET, STEP_SEND, ResponseKind, classify_response
from .staging import RawStage, RunDirectory
from .transport import Pacer, TransportError

MANIFEST_SCHEMA = "ibkr-capture-manifest-v1"
_POLL_INITIAL_SECONDS = 20.0
_POLL_MAX_SECONDS = 120.0
KIND_POLL_EXHAUSTED = "POLL_EXHAUSTED"
KIND_TRANSPORT_FAILURE = "TRANSPORT_FAILURE"


class CaptureError(RuntimeError):
    """An internal safety check failed (for example a credential echo)."""


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    request_index: int
    query: str
    status: str
    attempts: int
    refusal_kind: str | None = None
    refusal_code: str | None = None


def _guard_echo(body: bytes, secrets_in_use: tuple[str, ...]) -> None:
    for value in secrets_in_use:
        if value and value.encode() in body:
            raise CaptureError("a response echoed a credential; nothing was staged")


def _finish(
    run: RunDirectory,
    base: dict[str, object],
    *,
    status: str,
    attempts: list[dict[str, object]],
    refusal: dict[str, object] | None,
) -> None:
    manifest = dict(base)
    manifest["status"] = status
    manifest["attempts"] = attempts
    if refusal is not None:
        manifest["refusal"] = refusal
    run.finalize(manifest)


def capture_plan(
    plan: RequestPlan,
    *,
    governance: LiveReadGovernance,
    credentials: Mapping[str, SecretValue],
    http_get: HttpGet,
    wait: Callable[[float], None],
    stage: RawStage,
    user_agent: str,
    now: Callable[[], datetime] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[RunSummary]:
    """Run the plan under governance; stop at the first refusal."""

    if governance.plan_sha256 != plan.sha256:
        raise CaptureError("governance was consumed for a different plan")
    if not user_agent.strip():
        raise CaptureError("a User-Agent is required by the service")
    service_param = credentials[TOKEN_FIELD].reveal()
    current = now or (lambda: datetime.now(timezone.utc))
    pacer = Pacer(wait=wait, clock=clock)
    headers = {"User-Agent": user_agent}
    summaries: list[RunSummary] = []
    for index, request in enumerate(plan.requests):
        query = get_query(request.query)
        query_id = credentials[query.query_field].reveal()
        in_use = (service_param, query_id)
        stamp = current().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"flex-{stamp}-{plan.sha256[:8]}-{index:02d}"
        run = stage.open_run(run_id)
        base: dict[str, object] = {
            "schema": MANIFEST_SCHEMA,
            "run_id": run_id,
            "plan": plan.to_document(),
            "plan_sha256": plan.sha256,
            "request_index": index,
            "query": query.name,
            "query_id_sha256": sha256_hex(query_id.encode()),
            "service_version": SERVICE_VERSION,
            "user_agent": user_agent,
            "governance": {
                "evidence_sha256": governance.evidence_sha256,
                "review_reference": governance.review["reference"],
            },
            "created_utc": stamp,
        }
        attempts: list[dict[str, object]] = []

        def refuse(
            kind: str,
            code: str | None,
            body: bytes | None,
            count: int,
        ) -> RunSummary:
            if body is not None:
                run.write_new("refusal-response.xml", body)
            _finish(
                run,
                base,
                status="REFUSED",
                attempts=attempts,
                refusal={"kind": kind, "error_code": code},
            )
            return RunSummary(run_id, index, query.name, "REFUSED", count, kind, code)

        governance.reserve_send(index)
        pacer.before_call()
        try:
            send_body = http_get(
                SEND_REQUEST_URL,
                {"q": query_id, "t": service_param, "v": SERVICE_VERSION},
                headers,
            )
        except TransportError:
            summaries.append(refuse(KIND_TRANSPORT_FAILURE, None, None, 0))
            break
        _guard_echo(send_body, in_use)
        run.write_new("send-response.xml", send_body)
        sent = classify_response(send_body, step=STEP_SEND)
        attempts.append(
            {"step": STEP_SEND, "kind": sent.kind.value, "code": sent.error_code}
        )
        if sent.kind is not ResponseKind.SEND_ACCEPTED or sent.reference_code is None:
            summaries.append(refuse(sent.kind.value, sent.error_code, None, 0))
            break
        reference = sent.reference_code
        run.write_new("reference-code.txt", (reference + "\n").encode())
        get_url = sent.statement_url or GET_STATEMENT_URL
        base["get_url"] = get_url
        base["send_url"] = SEND_REQUEST_URL
        outcome: RunSummary | None = None
        for attempt in range(1, governance.max_get_attempts + 1):
            wait(min(_POLL_INITIAL_SECONDS * (2 ** (attempt - 1)), _POLL_MAX_SECONDS))
            governance.reserve_get(index)
            pacer.before_call()
            try:
                body = http_get(
                    get_url,
                    {"q": reference, "t": service_param, "v": SERVICE_VERSION},
                    headers,
                )
            except TransportError:
                outcome = refuse(KIND_TRANSPORT_FAILURE, None, None, attempt)
                break
            _guard_echo(body, in_use)
            response = classify_response(body, step=STEP_GET)
            attempts.append(
                {
                    "step": STEP_GET,
                    "attempt": attempt,
                    "kind": response.kind.value,
                    "code": response.error_code,
                    "body_sha256": sha256_hex(body),
                }
            )
            if response.kind is ResponseKind.POLL_AGAIN:
                continue
            if response.kind is ResponseKind.REPORT:
                run.write_new("statement.xml", body)
                _finish(run, base, status="STAGED", attempts=attempts, refusal=None)
                outcome = RunSummary(run_id, index, query.name, "STAGED", attempt)
                break
            outcome = refuse(response.kind.value, response.error_code, body, attempt)
            break
        else:
            outcome = refuse(
                KIND_POLL_EXHAUSTED, "1019", None, governance.max_get_attempts
            )
        summaries.append(outcome)
        if outcome.status != "STAGED":
            break
    return summaries
