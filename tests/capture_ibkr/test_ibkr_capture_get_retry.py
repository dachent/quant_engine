"""A connection-class GetStatement failure is retried with the same reference code."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

from ibkr_capture_fixtures import (
    ACCOUNT_ONE,
    NOW,
    Harness,
    activity_xml,
    make_plan,
    send_ok,
    service_error,
    weekday_range,
    write_evidence,
)
from quant_engine.ibkr.capture import (
    CaptureRequest,
    GovernanceError,
    RequestPlan,
    TransportError,
    authorize_live_read,
    normalize_run,
    verify_run,
)
from quant_engine.ibkr.capture._util import account_label

DAYS = weekday_range(date(2026, 10, 5), date(2026, 10, 9))
REPORT = activity_xml((ACCOUNT_ONE,), DAYS)
DNS = "the service request failed (URLError/gaierror) at ndcdyn.interactivebrokers.com"


def _dns() -> TransportError:
    return TransportError(DNS, retryable=True)


def _manifest(run_dir: Path) -> dict[str, object]:
    return json.loads((run_dir / "capture-manifest.json").read_text(encoding="utf-8"))


def test_a_connection_failure_on_get_is_retried_with_the_same_reference(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, [send_ok("REF42"), _dns(), _dns(), REPORT])
    (summary,) = harness.run()
    assert (summary.status, summary.attempts) == ("STAGED", 3)
    urls = [url for url, _ in harness.http.calls]
    assert sum(1 for url in urls if url.endswith("/SendRequest")) == 1
    assert [params["q"] for url, params in harness.http.calls[1:]] == ["REF42"] * 3
    assert harness.waits == [20.0, 5.0, 15.0]
    (run_dir,) = harness.run_dirs()
    assert (run_dir / "reference-code.txt").read_text() == "REF42\n"
    attempts = _manifest(run_dir)["attempts"]
    kinds = [(row["step"], row["kind"]) for row in attempts]  # type: ignore[index]
    assert kinds == [
        ("send", "SEND_ACCEPTED"),
        ("get", "TRANSPORT_FAILURE"),
        ("get", "TRANSPORT_FAILURE"),
        ("get", "REPORT"),
    ]
    failure = attempts[1]  # type: ignore[index]
    assert failure["detail"] == DNS and "ndcdyn.interactivebrokers.com" in DNS
    receipt = json.loads(next(harness.receipts.iterdir()).read_text())
    assert receipt["send_requests_used"] == [0]
    assert receipt["get_attempts_used"] == {"0": 3}
    # the attempt log with retried failures still verifies
    output = tmp_path / "n.json"
    normalize_run(run_dir, output)
    assert verify_run(run_dir, output).status == "VERIFIED"


def test_the_backoff_grows_and_polling_keeps_its_own_waits(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [
            send_ok(),
            service_error("1019", "Warn"),
            _dns(),
            _dns(),
            _dns(),
            _dns(),
            REPORT,
        ],
        max_get=6,
    )
    (summary,) = harness.run()
    assert summary.status == "STAGED"
    # first wait 20 s, one 1019 poll (next 40 s), then 5, 15, 30, 60 s after failures
    assert harness.waits == [20.0, 40.0, 5.0, 15.0, 30.0, 60.0]


def test_the_retry_budget_is_the_evidences_get_attempts(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [send_ok(), _dns(), _dns(), _dns(), _dns()], max_get=3)
    (summary,) = harness.run()
    assert (summary.status, summary.refusal_kind) == ("REFUSED", "TRANSPORT_FAILURE")
    assert len(harness.http.calls) == 4  # one SendRequest, three GetStatement
    (run_dir,) = harness.run_dirs()
    refusal = _manifest(run_dir)["refusal"]
    assert refusal["detail"].startswith("get attempts exhausted: ")  # type: ignore[index]
    assert DNS in refusal["detail"]  # type: ignore[index]
    receipt = json.loads(next(harness.receipts.iterdir()).read_text())
    assert receipt["get_attempts_used"] == {"0": 3}


def test_a_failure_that_is_not_the_connection_class_stops_at_once(
    tmp_path: Path,
) -> None:
    harness = Harness(
        tmp_path, [send_ok(), TransportError("the service answered HTTP 503"), REPORT]
    )
    (summary,) = harness.run()
    assert (summary.status, summary.refusal_kind) == ("REFUSED", "TRANSPORT_FAILURE")
    assert len(harness.http.calls) == 2


def test_a_send_failure_is_never_retried_even_if_it_is_the_connection_class(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, [_dns(), send_ok(), REPORT])
    (summary,) = harness.run()
    assert summary.refusal_kind == "TRANSPORT_FAILURE"
    assert len(harness.http.calls) == 1


@pytest.mark.parametrize("code", ["1012", "1015", "1001", "1018", "1004"])
def test_service_error_codes_on_get_keep_their_refusal(
    tmp_path: Path, code: str
) -> None:
    harness = Harness(tmp_path, [send_ok(), service_error(code), REPORT])
    (summary,) = harness.run()
    assert summary.status == "REFUSED" and summary.refusal_kind != "TRANSPORT_FAILURE"
    assert len(harness.http.calls) == 2


def _override_plan() -> RequestPlan:
    return RequestPlan(
        date(2026, 11, 3),
        (
            CaptureRequest(
                "activity-history", date(2026, 10, 5), date(2026, 10, 5), True
            ),
        ),
        (account_label(ACCOUNT_ONE),),
    )


@pytest.mark.parametrize(("budget", "accepted"), [(5, False), (6, True), (9, True)])
def test_an_override_plan_needs_a_get_budget_that_covers_retries(
    tmp_path: Path, budget: int, accepted: bool
) -> None:
    plan = _override_plan()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    evidence = write_evidence(tmp_path, plan, max_get=budget)
    if accepted:
        authorize_live_read(evidence, plan=plan, receipt_root=receipts, now=lambda: NOW)
    else:
        with pytest.raises(GovernanceError, match="at least 6"):
            authorize_live_read(
                evidence, plan=plan, receipt_root=receipts, now=lambda: NOW
            )


def test_a_plan_without_an_override_keeps_any_budget(tmp_path: Path) -> None:
    plan = make_plan()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    evidence = write_evidence(tmp_path, plan, max_get=1)
    authorize_live_read(evidence, plan=plan, receipt_root=receipts, now=lambda: NOW)


# ---------------------------------------------- readiness deadline and the staged prefix


def _days_plan(count: int, first: date = date(2026, 10, 5)) -> RequestPlan:
    days = weekday_range(first, date(2026, 12, 31))[:count]
    return RequestPlan(
        date(2026, 11, 3),
        tuple(CaptureRequest("activity-history", day, day, True) for day in days),
        (account_label(ACCOUNT_ONE),),
    ), days


def test_no_request_starts_after_the_readiness_deadline(tmp_path: Path) -> None:
    from datetime import datetime, timedelta

    plan, days = _days_plan(3)
    responses: list[bytes] = []
    for day in days:
        responses += [send_ok(), activity_xml((ACCOUNT_ONE,), [day])]
    harness = Harness(tmp_path, responses, plan=plan)
    ticks = iter([NOW, NOW, NOW + timedelta(hours=2)] + [NOW + timedelta(hours=2)] * 40)

    def moving_now() -> datetime:
        return next(ticks)

    summaries = harness._capture(
        harness.plan,
        governance=harness.governance,
        credentials=harness.credentials,
        http_get=harness.http,
        wait=harness.waits.append,
        stage=harness._stage,
        user_agent="quant-engine-synthetic-test",
        now=moving_now,
        clock=lambda: 1000.0 + len(harness.http.calls) * 5.0,
        deadline=NOW + timedelta(hours=1),
    )
    assert [s.status for s in summaries] == ["STAGED", "NOT_ATTEMPTED"]
    assert (
        summaries[1].refusal_kind == "DEADLINE_REACHED"
        and summaries[1].request_index == 1
    )
    # one SendRequest only; the unattempted days used no evidence
    assert sum(1 for url, _ in harness.http.calls if url.endswith("/SendRequest")) == 1
    receipt = json.loads(next(harness.receipts.iterdir()).read_text())
    assert receipt["send_requests_used"] == [0]
    assert len(harness.run_dirs()) == 1


def test_a_refusal_leaves_a_verifiable_prefix_and_a_remaining_days_plan_with_new_evidence(
    tmp_path: Path,
) -> None:
    plan, days = _days_plan(4)
    responses: list[bytes] = [
        send_ok(),
        activity_xml((ACCOUNT_ONE,), [days[0]]),
        send_ok(),
        activity_xml((ACCOUNT_ONE,), [days[1]]),
        send_ok(),
        service_error("1018"),  # refused on the third day
    ]
    harness = Harness(tmp_path, responses, plan=plan)
    summaries = harness.run()
    assert [s.status for s in summaries] == ["STAGED", "STAGED", "REFUSED"]
    dirs = harness.run_dirs()
    assert len(dirs) == 3
    # the staged prefix normalizes and verifies run by run
    for index, run_dir in enumerate(dirs[:2]):
        output = tmp_path / f"n{index}.json"
        normalize_run(run_dir, output)
        assert verify_run(run_dir, output).status == "VERIFIED"
    # the remaining days are a new plan with its own hash and its own single-use evidence
    remaining = RequestPlan(
        plan.pull_date,
        plan.requests[2:],
        plan.expected_account_labels,
    )
    assert remaining.sha256 != plan.sha256 and len(remaining.requests) == 2
    receipts = tmp_path / "receipts2"
    receipts.mkdir()
    evidence = write_evidence(tmp_path, remaining, name="evidence2.json", max_get=6)
    governance = authorize_live_read(
        evidence, plan=remaining, receipt_root=receipts, now=lambda: NOW
    )
    assert governance.max_send_requests == 2
    # the original plan's evidence does not cover it
    with pytest.raises(GovernanceError):
        authorize_live_read(
            write_evidence(tmp_path, plan, name="evidence3.json", max_get=6),
            plan=remaining,
            receipt_root=receipts,
            now=lambda: NOW,
        )
