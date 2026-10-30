from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from ibkr_capture_fixtures import NOW, make_plan, write_evidence
from quant_engine.core import CredentialError
from quant_engine.ibkr.capture import (
    CaptureRequest,
    GovernanceError,
    PlanError,
    RequestPlan,
    ResponseKind,
    authorize_live_read,
    catalog_rows,
    catalog_sha256,
    classify_response,
    get_query,
    load_plan,
    plan_from_document,
    required_fields,
    resolve_flex_credentials,
)
from ibkr_capture_fixtures import (
    FAKE_A,
    credential_config,
    fake_source_for,
    send_ok,
    service_error,
)


def test_catalog_is_fixed_and_names_no_identifier() -> None:
    assert [row["name"] for row in catalog_rows()] == [
        "activity-history",
        "activity-current-increment",
        "security-reference",
    ]
    assert len(catalog_sha256()) == 64
    assert catalog_sha256() == catalog_sha256()
    flat = str(catalog_rows()).lower()
    assert "tokenvalue" not in flat
    with pytest.raises(KeyError):
        get_query("unknown")


def test_plan_hash_is_canonical_and_round_trips() -> None:
    plan = make_plan(labels=("0123456789abcdef",), non_trading=(date(2026, 10, 7),))
    assert plan.sha256 == plan_from_document(plan.to_document()).sha256
    assert load_plan(plan.canonical_bytes()).sha256 == plan.sha256
    other = make_plan(end=date(2026, 10, 8))
    assert other.sha256 != plan.sha256


@pytest.mark.parametrize(
    "request_args",
    [
        {"start": date(2026, 10, 9), "end": date(2026, 10, 5)},  # reversed
        {"start": date(2026, 9, 1), "end": date(2026, 10, 9)},  # too long
        {"start": date(2026, 10, 5), "end": date(2026, 11, 3)},  # not final
        {"query": "activity-current-increment"},  # needs exactly one day
        {"query": "no-such-query"},
    ],
)
def test_plan_refuses_bad_requests(request_args: dict[str, Any]) -> None:
    with pytest.raises(PlanError):
        plan_from_document(make_plan(**request_args).to_document())


def test_plan_refuses_duplicates_bad_labels_and_unknown_fields() -> None:
    request = CaptureRequest("activity-history", date(2026, 10, 5), date(2026, 10, 9))
    twice = RequestPlan(date(2026, 11, 3), (request, request))
    with pytest.raises(PlanError):
        plan_from_document(twice.to_document())
    with pytest.raises(PlanError):
        plan_from_document(make_plan(labels=("SYNTHACCT1",)).to_document())
    document = make_plan().to_document()
    document["extra"] = 1
    with pytest.raises(PlanError):
        plan_from_document(document)
    with pytest.raises(PlanError):
        plan_from_document({**make_plan().to_document(), "pull_date": "20261103"})
    with pytest.raises(PlanError):
        load_plan(b"not json")


def test_single_day_increment_is_accepted() -> None:
    plan = make_plan(
        query="activity-current-increment",
        start=date(2026, 10, 9),
        end=date(2026, 10, 9),
    )
    assert plan_from_document(plan.to_document()).requests[0].window_from == date(
        2026, 10, 9
    )


def test_governance_consumes_single_use_evidence(tmp_path: Path) -> None:
    plan = make_plan()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    evidence = write_evidence(tmp_path, plan)
    governance = authorize_live_read(
        evidence, plan=plan, receipt_root=receipts, now=lambda: NOW
    )
    assert governance.max_send_requests == 1
    with pytest.raises(GovernanceError, match="already consumed"):
        authorize_live_read(evidence, plan=plan, receipt_root=receipts, now=lambda: NOW)
    governance.reserve_send(0)
    with pytest.raises(GovernanceError):
        governance.reserve_send(0)
    for _ in range(5):
        governance.reserve_get(0)
    with pytest.raises(GovernanceError):
        governance.reserve_get(0)


def test_get_requires_a_reserved_send(tmp_path: Path) -> None:
    plan = make_plan()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    governance = authorize_live_read(
        write_evidence(tmp_path, plan),
        plan=plan,
        receipt_root=receipts,
        now=lambda: NOW,
    )
    with pytest.raises(GovernanceError):
        governance.reserve_get(0)


def _refused(tmp_path: Path, mutate: Any, *, now: datetime = NOW) -> None:
    plan = make_plan()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    evidence = write_evidence(tmp_path, plan, mutate=mutate)
    with pytest.raises(GovernanceError):
        authorize_live_read(evidence, plan=plan, receipt_root=receipts, now=lambda: now)
    assert list(receipts.iterdir()) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["plan"].update(sha256="0" * 64),
        lambda d: d.update(operation="auth-test"),
        lambda d: d["authority"].update(status="REVOKED"),
        lambda d: d["authority"].update(expires_at="2026-11-03T14:00:00Z"),
        lambda d: d["authority"].update(issued_at="2026-11-03T16:00:00Z"),
        lambda d: d["authority"].update(issued_at="2026-11-03T00:00:00"),
        lambda d: d["terms"].update(version="other"),
        lambda d: d["catalog"].update(sha256="1" * 64),
        lambda d: d["metering"].update(retry_count=1),
        lambda d: d["metering"].update(max_send_requests=2),
        lambda d: d["metering"].update(max_get_attempts=10),
        lambda d: d["metering"].update(max_get_attempts=True),
        lambda d: d["review"].update(reference="https://example.test/x"),
        lambda d: d.update(unexpected=True),
    ],
)
def test_governance_refuses_invalid_evidence(tmp_path: Path, mutate: Any) -> None:
    _refused(tmp_path, mutate)


def test_governance_refuses_missing_or_bad_files(tmp_path: Path) -> None:
    plan = make_plan()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    with pytest.raises(GovernanceError):
        authorize_live_read(tmp_path / "none.json", plan=plan, receipt_root=receipts)
    bad = tmp_path / "bad.json"
    bad.write_text("[]")
    with pytest.raises(GovernanceError):
        authorize_live_read(bad, plan=plan, receipt_root=receipts, now=lambda: NOW)
    good = write_evidence(tmp_path, plan)
    with pytest.raises(GovernanceError):
        authorize_live_read(
            good, plan=plan, receipt_root=tmp_path / "missing", now=lambda: NOW
        )
    assert datetime.now(timezone.utc).year >= 2026  # real clock path stays importable


def test_classifier_covers_the_documented_shapes() -> None:
    assert (
        classify_response(send_ok("R1"), step="send").kind is ResponseKind.SEND_ACCEPTED
    )
    assert (
        classify_response(b"<FlexQueryResponse/>", step="get").kind
        is ResponseKind.REPORT
    )
    assert classify_response(b"<FlexQueryResponse/>", step="send").refused
    assert (
        classify_response(service_error("1019", "Warn"), step="get").kind
        is ResponseKind.POLL_AGAIN
    )
    assert classify_response(service_error("1019", "Warn"), step="send").refused
    assert classify_response(b"<!DOCTYPE x><a/>", step="get").refused
    assert classify_response(
        b"<FlexStatementResponse><Status>Success</Status></FlexStatementResponse>",
        step="get",
    ).refused
    assert classify_response(
        b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>10x</ErrorCode></FlexStatementResponse>",
        step="get",
    ).refused
    assert classify_response(
        b"<FlexStatementResponse><Status>Success</Status><ReferenceCode>a b</ReferenceCode></FlexStatementResponse>",
        step="send",
    ).refused
    with pytest.raises(ValueError):
        classify_response(b"<a/>", step="other")


def test_credentials_resolve_only_the_needed_fields_in_order() -> None:
    plan = make_plan()
    assert required_fields(plan) == ("flex_token", "query_activity_history")
    config = credential_config("query_activity_history", "query_security_reference")
    source = fake_source_for("query_activity_history", "query_security_reference")
    values = resolve_flex_credentials(
        config, plan, sources={"windows-credential-manager": source}
    )
    assert set(values) == {"flex_token", "query_activity_history"}
    assert values["flex_token"].reveal() == FAKE_A
    assert FAKE_A not in repr(values)
    assert source.reads == ["target-flex-a", "target-query-0"]


def test_credentials_refuse_an_undeclared_field_and_a_literal_value() -> None:
    plan = make_plan()
    with pytest.raises(CredentialError):
        resolve_flex_credentials(credential_config(), plan, sources={})
    literal = credential_config("query_activity_history")
    literal["credentials"]["ibkr"]["flex_token"] = [
        {"source": "windows-credential-manager", "name": "n", "value": "x"}
    ]
    with pytest.raises(CredentialError):
        resolve_flex_credentials(literal, plan, sources={})
