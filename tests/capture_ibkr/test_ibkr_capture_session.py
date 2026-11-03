from __future__ import annotations

import json
from pathlib import Path

import pytest

from ibkr_capture_fixtures import (
    FAKE_A,
    FAKE_B,
    Harness,
    activity_xml,
    make_plan,
    send_ok,
    service_error,
    weekday_range,
)
from quant_engine.ibkr.capture import (
    CaptureError,
    GovernanceError,
    LiveReadGovernance,
    RawStage,
    StagingError,
    TransportError,
)
from datetime import date

DAYS = weekday_range(date(2026, 10, 5), date(2026, 10, 9))
REPORT = activity_xml(("SYNTHACCT1",), DAYS)


def _manifest(run_dir: Path) -> dict[str, object]:
    return json.loads((run_dir / "capture-manifest.json").read_text(encoding="utf-8"))


def test_happy_path_polls_only_1019_and_stages_exact_bytes(tmp_path: Path) -> None:
    send = send_ok("REF777")
    harness = Harness(
        tmp_path,
        [send, service_error("1019", "Warn"), service_error("1019", "Warn"), REPORT],
    )
    summaries = harness.run()
    assert [(s.status, s.attempts) for s in summaries] == [("STAGED", 3)]
    assert harness.waits == [20.0, 40.0, 80.0]
    urls = [call[0] for call in harness.http.calls]
    assert urls[0].endswith("/SendRequest") and len(urls) == 4
    assert sum(1 for url in urls if url.endswith("/SendRequest")) == 1
    (run_dir,) = harness.run_dirs()
    assert (run_dir / "send-response.xml").read_bytes() == send
    assert (run_dir / "statement.xml").read_bytes() == REPORT
    assert (run_dir / "reference-code.txt").read_text() == "REF777\n"
    manifest = _manifest(run_dir)
    assert manifest["status"] == "STAGED"
    names = {row["name"] for row in manifest["files"]}  # type: ignore[attr-defined]
    assert names == {"send-response.xml", "reference-code.txt", "statement.xml"}
    # The query id is retained only as a hash and the token nowhere.
    blob = b"".join(p.read_bytes() for p in run_dir.iterdir())
    assert FAKE_A.encode() not in blob and FAKE_B.encode() not in blob
    assert "query_id_sha256" in manifest
    receipt = json.loads(next(harness.receipts.iterdir()).read_text())
    assert receipt["send_requests_used"] == [0]
    assert receipt["get_attempts_used"] == {"0": 3}
    assert receipt["retry_count"] == 0


def test_credentials_reach_the_transport_only_as_parameters(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [send_ok(), REPORT])
    harness.run()
    send_params = harness.http.calls[0][1]
    assert send_params == {"q": FAKE_B, "t": FAKE_A, "v": "3"}
    assert harness.http.calls[1][1] == {"q": "REF123", "t": FAKE_A, "v": "3"}


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("1004", "REFUSED_INCOMPLETE"),
        ("1005", "REFUSED_SETTLEMENT_NOT_READY"),
        ("1006", "REFUSED_PNL_NOT_READY"),
        ("1007", "REFUSED_PNL_NOT_READY"),
        ("1008", "REFUSED_PNL_NOT_READY"),
        ("1001", "REFUSED_RETRY_LATER"),
        ("1018", "REFUSED_RETRY_LATER"),
        ("1012", "REFUSED_REQUEST"),
        ("1999", "REFUSED_UNKNOWN"),
    ],
)
def test_incomplete_and_not_ready_responses_are_refused_without_a_statement(
    tmp_path: Path, code: str, kind: str
) -> None:
    harness = Harness(tmp_path, [send_ok(), service_error(code, "Warn")])
    summaries = harness.run()
    assert [(s.status, s.refusal_kind, s.refusal_code) for s in summaries] == [
        ("REFUSED", kind, code)
    ]
    (run_dir,) = harness.run_dirs()
    assert not (run_dir / "statement.xml").exists()
    assert (run_dir / "refusal-response.xml").is_file()
    manifest = _manifest(run_dir)
    assert manifest["status"] == "REFUSED"
    assert len(harness.http.calls) == 2  # no polling and no second SendRequest


def test_send_failure_is_refused_and_remaining_requests_are_not_attempted(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, [service_error("1015")])
    (summary,) = harness.run()
    assert (summary.status, summary.refusal_code) == ("REFUSED", "1015")
    assert len(harness.http.calls) == 1


def test_poll_exhaustion_refuses_after_the_evidence_cap(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [send_ok(), service_error("1019", "Warn"), service_error("1019", "Warn")],
        max_get=2,
    )
    (summary,) = harness.run()
    assert (summary.status, summary.refusal_kind) == ("REFUSED", "POLL_EXHAUSTED")
    assert harness.waits == [20.0, 40.0]
    assert len(harness.http.calls) == 3


def test_transport_failure_is_terminal_and_never_retried(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [TransportError("the service request failed")])
    (summary,) = harness.run()
    assert summary.refusal_kind == "TRANSPORT_FAILURE"
    assert len(harness.http.calls) == 1


@pytest.mark.parametrize(
    "body",
    [b"<html><body>Error</body></html>", b"", b"<FlexQueryResponse", b"<x/>"],
)
def test_malformed_send_responses_are_refused(tmp_path: Path, body: bytes) -> None:
    harness = Harness(tmp_path, [body])
    (summary,) = harness.run()
    assert summary.status == "REFUSED"


def test_a_credential_echo_in_a_response_stages_nothing(tmp_path: Path) -> None:
    echoing = f"<FlexStatementResponse><Status>Fail</Status><ErrorMessage>{FAKE_A}</ErrorMessage></FlexStatementResponse>".encode()
    harness = Harness(tmp_path, [echoing])
    with pytest.raises(CaptureError):
        harness.run()
    (run_dir,) = harness.run_dirs()
    assert list(run_dir.iterdir()) == []


def test_a_statement_url_off_the_approved_hosts_is_refused(tmp_path: Path) -> None:
    harness = Harness(
        tmp_path,
        [send_ok(url="https://evil.example.test/x/FlexStatementService.GetStatement")],
    )
    (summary,) = harness.run()
    assert summary.status == "REFUSED"
    assert len(harness.http.calls) == 1


def test_an_approved_statement_url_is_used_for_polling(tmp_path: Path) -> None:
    url = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"
    harness = Harness(tmp_path, [send_ok(url=url), REPORT])
    harness.run()
    assert harness.http.calls[1][0] == url


def test_staging_is_create_only(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    stage = RawStage(root)
    run = stage.open_run("flex-20261103T150000Z-abcdef12-00")
    run.write_new("statement.xml", b"<a/>")
    with pytest.raises(StagingError):
        run.write_new("statement.xml", b"<b/>")
    with pytest.raises(StagingError):
        stage.open_run("flex-20261103T150000Z-abcdef12-00")
    with pytest.raises(StagingError):
        run.write_new("capture-manifest.json", b"{}")
    with pytest.raises(StagingError):
        run.write_new("../escape.xml", b"x")
    assert (
        root / "flex-20261103T150000Z-abcdef12-00" / "statement.xml"
    ).read_bytes() == b"<a/>"
    with pytest.raises(StagingError):
        RawStage(Path("relative-root"))
    with pytest.raises(StagingError):
        RawStage(tmp_path / "missing")


def test_a_second_capture_into_the_same_run_id_is_refused(tmp_path: Path) -> None:
    first = Harness(tmp_path, [send_ok(), REPORT])
    first.run()
    first.governance = LiveReadGovernance(  # fresh budget, same plan
        receipt_path=first.receipts / "second.json",
        evidence_sha256=first.governance.evidence_sha256,
        plan_sha256=first.governance.plan_sha256,
        review=first.governance.review,
        max_send_requests=1,
        max_get_attempts=5,
    )
    first.http.responses = [send_ok(), REPORT]
    with pytest.raises(StagingError):
        first.run()


def test_governance_budget_is_enforced_by_the_session(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [send_ok(), REPORT])
    harness.governance.reserve_send(0)
    with pytest.raises(GovernanceError):
        harness.run()
    assert harness.http.calls == []


def test_a_plan_the_governance_did_not_approve_is_refused(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [send_ok(), REPORT])
    harness.plan = make_plan(end=date(2026, 10, 8))
    with pytest.raises(CaptureError):
        harness.run()
    assert harness.http.calls == []
