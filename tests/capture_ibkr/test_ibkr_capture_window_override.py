"""The request sets the Flex window: fd/td on SendRequest, recorded, and checked on return."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

from ibkr_capture_fixtures import (
    ACCOUNT_ONE,
    FAKE_A,
    FAKE_B,
    FakeSource,
    Harness,
    activity_xml,
    credential_config,
    make_plan,
    security_xml,
    send_ok,
    weekday_range,
)
from quant_engine.ibkr.capture import (
    CaptureRequest,
    NormalizationError,
    PlanError,
    RequestPlan,
    VerificationError,
    get_query,
    load_plan,
    normalize_run,
    plan_from_document,
    resolve_flex_credentials,
    verify_run,
)
from quant_engine.ibkr.capture._util import account_label

START, END = date(2026, 10, 5), date(2026, 10, 9)
LABELS = (account_label(ACCOUNT_ONE),)
SHORT = (date(2026, 10, 6), date(2026, 10, 8))


def _plan(query: str, start: date, end: date, *, override: bool = True) -> RequestPlan:
    return RequestPlan(
        pull_date=date(2026, 11, 3),
        requests=(CaptureRequest(query, start, end, override),),
        expected_account_labels=LABELS,
    )


def _staged(root: Path, body: bytes, plan: RequestPlan) -> Path:
    root.mkdir()
    harness = Harness(root, [send_ok(), body], plan=plan)
    (summary,) = harness.run()
    assert summary.status == "STAGED"
    (run_dir,) = harness.run_dirs()
    return run_dir


def test_a_plan_records_the_override_and_keeps_the_old_hash_without_one() -> None:
    old = make_plan()
    assert all("window_override" not in row for row in old.to_document()["requests"])
    new = _plan("activity-history", START, END)
    assert new.to_document()["requests"][0]["window_override"] is True
    assert new.sha256 != old.sha256
    assert plan_from_document(new.to_document()) == new
    assert plan_from_document(old.to_document()) == old


@pytest.mark.parametrize("value", [False, "yes", 1, None])
def test_the_override_flag_is_true_or_absent(value: object) -> None:
    document = _plan("activity-history", START, END).to_document()
    document["requests"][0]["window_override"] = value
    with pytest.raises(PlanError):
        plan_from_document(document)


def test_an_unknown_request_key_is_still_refused() -> None:
    document = _plan("activity-history", START, END).to_document()
    document["requests"][0]["window_overide"] = True
    with pytest.raises(PlanError, match="invalid field set"):
        plan_from_document(document)


def test_the_catalog_maximum_still_bounds_an_override() -> None:
    plan = _plan("activity-history", date(2026, 9, 1), date(2026, 10, 9))
    with pytest.raises(PlanError, match="exceeds the catalog maximum"):
        load_plan(json.dumps(plan.to_document()).encode())


def test_the_override_goes_out_as_fd_td_on_the_send_only(tmp_path: Path) -> None:
    plan = _plan("activity-history", START, END)
    body = activity_xml((ACCOUNT_ONE,), weekday_range(START, END))
    harness = Harness(tmp_path, [send_ok("REF9"), body], plan=plan)
    (summary,) = harness.run()
    assert summary.status == "STAGED"
    send_params, get_params = harness.http.calls[0][1], harness.http.calls[1][1]
    assert send_params == {
        "q": FAKE_B,
        "t": FAKE_A,
        "v": "3",
        "fd": "20261005",
        "td": "20261009",
    }
    assert get_params == {"q": "REF9", "t": FAKE_A, "v": "3"}
    (run_dir,) = harness.run_dirs()
    manifest = json.loads((run_dir / "capture-manifest.json").read_text())
    assert manifest["send_window"] == {"fd": "20261005", "td": "20261009"}
    assert manifest["plan"]["requests"][0]["window_override"] is True


def test_without_an_override_the_send_is_unchanged(tmp_path: Path) -> None:
    body = activity_xml((ACCOUNT_ONE,), weekday_range(START, END))
    harness = Harness(tmp_path, [send_ok(), body])
    harness.run()
    assert harness.http.calls[0][1] == {"q": FAKE_B, "t": FAKE_A, "v": "3"}
    (run_dir,) = harness.run_dirs()
    manifest = json.loads((run_dir / "capture-manifest.json").read_text())
    assert "send_window" not in manifest


def test_an_override_run_normalizes_and_verifies(tmp_path: Path) -> None:
    body = activity_xml((ACCOUNT_ONE,), weekday_range(START, END))
    run_dir = _staged(tmp_path / "r", body, _plan("activity-history", START, END))
    output = tmp_path / "n.json"
    normalize_run(run_dir, output)
    assert verify_run(run_dir, output).status == "VERIFIED"


def test_the_verifier_refuses_a_manifest_that_misstates_what_was_sent(
    tmp_path: Path,
) -> None:
    body = activity_xml((ACCOUNT_ONE,), weekday_range(START, END))
    run_dir = _staged(tmp_path / "r", body, _plan("activity-history", START, END))
    output = tmp_path / "n.json"
    normalize_run(run_dir, output)
    path = run_dir / "capture-manifest.json"
    manifest = json.loads(path.read_text())
    wrong = dict(manifest, send_window={"fd": "20261006", "td": "20261009"})
    missing = {key: value for key, value in manifest.items() if key != "send_window"}
    for changed in (wrong, missing):
        path.write_text(json.dumps(changed))
        with pytest.raises(VerificationError) as error:
            verify_run(run_dir, output)
        assert error.value.reason == "send-window"
    # a request that did not set its window must not claim one either
    plain = _staged(tmp_path / "p", body, make_plan())
    plain_output = tmp_path / "plain.json"
    normalize_run(plain, plain_output)
    plain_path = plain / "capture-manifest.json"
    claimed = json.loads(plain_path.read_text())
    claimed["send_window"] = {"fd": "20261005", "td": "20261009"}
    plain_path.write_text(json.dumps(claimed))
    with pytest.raises(VerificationError) as error:
        verify_run(plain, plain_output)
    assert error.value.reason == "send-window"


def test_a_range_that_returns_a_shorter_window_is_refused_only_for_an_override(
    tmp_path: Path,
) -> None:
    # the saved query answered 10-06..10-08 although 10-05..10-09 was asked for
    body = security_xml((ACCOUNT_ONE,), *SHORT)
    asked = _staged(tmp_path / "o", body, _plan("security-reference", START, END))
    with pytest.raises(NormalizationError, match="not the requested window"):
        normalize_run(asked, tmp_path / "n.json")
    # the same answer to a request that did not set its window keeps the old behaviour
    saved = _staged(
        tmp_path / "s", body, _plan("security-reference", START, END, override=False)
    )
    output = tmp_path / "saved.json"
    normalize_run(saved, output)
    assert verify_run(saved, output).status == "VERIFIED"


def test_the_verifier_checks_the_returned_window_on_its_own(tmp_path: Path) -> None:
    body = security_xml((ACCOUNT_ONE,), *SHORT)
    run_dir = _staged(
        tmp_path / "s", body, _plan("security-reference", START, END, override=False)
    )
    output = tmp_path / "saved.json"
    normalize_run(run_dir, output)
    # the run is re-labelled as one whose request set its window
    asked = _plan("security-reference", START, END)
    path = run_dir / "capture-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["plan"] = asked.to_document()
    manifest["plan_sha256"] = asked.sha256
    manifest["send_window"] = {"fd": "20261005", "td": "20261009"}
    path.write_text(json.dumps(manifest))
    with pytest.raises(VerificationError) as error:
        verify_run(run_dir, output)
    assert error.value.reason == "window-override"


def test_all_three_roles_can_share_one_vault_secret() -> None:
    plan = RequestPlan(
        pull_date=date(2026, 11, 3),
        requests=(
            CaptureRequest("activity-history", START, END, True),
            CaptureRequest(
                "activity-current-increment", date(2026, 11, 2), date(2026, 11, 2), True
            ),
            CaptureRequest("security-reference", START, END, True),
        ),
        expected_account_labels=LABELS,
    )
    fields = [get_query(item.query).query_field for item in plan.requests]
    config = credential_config()
    for field in fields:
        config["credentials"]["ibkr"][field] = [
            {"source": "windows-credential-manager", "name": "one-query-for-all"}
        ]
    source = FakeSource({"target-flex-a": FAKE_A, "one-query-for-all": FAKE_B})
    resolved = resolve_flex_credentials(
        config, plan, sources={"windows-credential-manager": source}
    )
    assert {resolved[field].reveal() for field in fields} == {FAKE_B}
