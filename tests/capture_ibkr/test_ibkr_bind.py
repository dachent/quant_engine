"""build-plan and bind-spec: the plan and the generation source spec are produced, not typed."""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from ibkr_capture_fixtures import (
    ACCOUNT_ONE,
    Harness,
    activity_xml,
    send_ok,
    weekday_range,
)
from quant_engine.ibkr.capture import CaptureRequest, RequestPlan, load_plan
from quant_engine.ibkr.capture._util import account_label
from quant_engine.ibkr.capture.bind import BindError, bind_spec, build_plan
from quant_engine.ibkr.capture.cli import main
from quant_engine.ibkr.capture.staging import StagingError

LABEL = account_label(ACCOUNT_ONE)


def _emitted(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_build_plan_writes_the_35_request_r3_plan_and_its_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "plan.json"
    argv = [
        "build-plan",
        "--first-day",
        "2026-09-21",
        "--last-day",
        "2026-11-06",
        "--pull-date",
        "2026-11-07",
        "--label",
        LABEL,
        "--out",
        str(out),
    ]
    assert main(argv) == 0
    printed = _emitted(capsys)
    plan = load_plan(out.read_bytes())
    assert printed == {"plan_sha256": plan.sha256, "requests": 35}
    assert all(item.window_override for item in plan.requests)
    assert (plan.requests[0].window_from, plan.requests[-1].window_to) == (
        date(2026, 9, 21),
        date(2026, 11, 6),
    )
    assert all(item.window_from == item.window_to for item in plan.requests)
    # create-only: a second write is refused and the file is untouched
    before = out.read_bytes()
    assert main(argv) == 2
    assert out.read_bytes() == before


def test_build_plan_leaves_out_weekends_and_listed_holidays_and_checks_the_rest() -> (
    None
):
    plan = build_plan(
        first_day=date(2026, 10, 9),
        last_day=date(2026, 10, 14),
        pull_date=date(2026, 11, 3),
        labels=(LABEL,),
        non_trading_days=(date(2026, 10, 12),),
    )
    assert [item.window_from.isoformat() for item in plan.requests] == [
        "2026-10-09",
        "2026-10-13",
        "2026-10-14",
    ]
    assert plan.known_non_trading_days == (date(2026, 10, 12),)
    with pytest.raises(BindError, match="before the pull date"):
        build_plan(
            first_day=date(2026, 10, 9),
            last_day=date(2026, 11, 3),
            pull_date=date(2026, 11, 3),
            labels=(LABEL,),
        )
    with pytest.raises(BindError, match="no trading day"):
        build_plan(
            first_day=date(2026, 10, 10),
            last_day=date(2026, 10, 11),
            pull_date=date(2026, 11, 3),
            labels=(LABEL,),
        )
    with pytest.raises(BindError, match="expected account scope"):
        build_plan(
            first_day=date(2026, 10, 9),
            last_day=date(2026, 10, 9),
            pull_date=date(2026, 11, 3),
            labels=(),
        )


def _captured(tmp_path: Path, days: list[date], count: int | None = None) -> Harness:
    plan = RequestPlan(
        date(2026, 11, 3),
        tuple(CaptureRequest("activity-history", day, day, True) for day in days),
        (LABEL,),
    )
    responses: list[bytes] = []
    for day in days:
        responses += [send_ok(), activity_xml((ACCOUNT_ONE,), [day])]
    harness = Harness(tmp_path, responses, plan=plan)
    harness.run()
    return harness


def _base_spec(tmp_path: Path) -> Path:
    spec = {
        "schema_version": 3,
        "as_of_date": "2026-10-01",
        "baseline": {
            "catalog_sha256": "0" * 64,
            "generation_id": "1" * 64,
            "root": str(tmp_path),
        },
        "declared_zero_activity_accounts": [],
        "minimum_crosswalk_overlap": 2,
        "roots": {"data": str(tmp_path)},
        "sources": [
            {
                "artifact_id": "old/statement.xml",
                "as_of_date": "2026-10-01",
                "expected_sha256": "2" * 64,
                "expected_size_bytes": 1,
                "relative_path": "old/statement.xml",
                "role": "REFRESH_FLEX_ACTIVITY",
                "root_id": "data",
                "schema_name": "activity-flex",
                "schema_version": "3",
            }
        ],
    }
    path = tmp_path / "base-spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def test_bind_spec_adds_verified_runs_with_their_real_hashes(tmp_path: Path) -> None:
    days = weekday_range(date(2026, 10, 5), date(2026, 10, 7))
    harness = _captured(tmp_path, days)
    runs = harness.run_dirs()
    base = _base_spec(tmp_path)
    out = tmp_path / "r3-spec.json"
    atom = "missing weekday statement for account-aaaa: 2026-06-30 to 2026-10-07"
    result = bind_spec(
        base_spec=base,
        runs=runs,
        root_id="data",
        as_of=date(2026, 10, 7),
        out=out,
        exclusions={atom: "owner ruling, issue 286"},
    )
    assert result["added_sources"] == 3 and result["preflight"] == "NOT_RUN"
    spec = json.loads(out.read_text())
    assert result["spec_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert spec["as_of_date"] == "2026-10-07"
    assert spec["declared_general_exclusions"] == [
        {"gap": atom, "provenance": "owner ruling, issue 286"}
    ]
    old, *new = spec["sources"]
    assert old["artifact_id"] == "old/statement.xml"
    assert [item["as_of_date"] for item in new] == [day.isoformat() for day in days]
    for item, run_dir in zip(new, runs, strict=True):
        payload = (run_dir / "statement.xml").read_bytes()
        assert item["expected_sha256"] == hashlib.sha256(payload).hexdigest()
        assert item["expected_size_bytes"] == len(payload)
        assert (
            item["relative_path"]
            == item["artifact_id"]
            == f"stage/{run_dir.name}/statement.xml"
        )
        assert (item["role"], item["schema_name"], item["root_id"]) == (
            "REFRESH_FLEX_ACTIVITY",
            "activity-flex",
            "data",
        )
    # the base spec is not touched and the output is create-only
    assert (
        json.loads(base.read_text())["sources"][0]["artifact_id"] == "old/statement.xml"
    )
    with pytest.raises(StagingError):
        bind_spec(
            base_spec=base, runs=runs, root_id="data", as_of=date(2026, 10, 7), out=out
        )


def test_bind_spec_refuses_what_it_cannot_bind(tmp_path: Path) -> None:
    days = weekday_range(date(2026, 10, 5), date(2026, 10, 6))
    harness = _captured(tmp_path, days)
    runs = harness.run_dirs()
    base = _base_spec(tmp_path)

    def attempt(name: str, **kw: Any) -> None:
        arguments: dict[str, Any] = dict(
            base_spec=base,
            runs=runs,
            root_id="data",
            as_of=date(2026, 10, 6),
            out=tmp_path / name,
        )
        arguments.update(kw)
        bind_spec(**arguments)

    with pytest.raises(BindError, match="older than a source"):
        attempt("a.json", as_of=date(2026, 9, 1))
    with pytest.raises(BindError, match="root id is not in the base spec"):
        attempt("b.json", root_id="elsewhere")
    with pytest.raises(BindError, match="already a source"):
        attempt("c.json", runs=[runs[0], runs[0]])
    with pytest.raises(BindError, match="no run to bind"):
        attempt("d.json", runs=[])
    with pytest.raises(BindError, match="lies outside the root"):
        other = tmp_path / "other-root"
        other.mkdir()
        attempt("e.json", base_spec=_with_root(base, other))
    with pytest.raises(BindError, match="declared exclusion is repeated"):
        spec = json.loads(base.read_text())
        spec["declared_general_exclusions"] = [{"gap": "x", "provenance": "p"}]
        repeated = tmp_path / "base2.json"
        repeated.write_text(json.dumps(spec))
        attempt("f.json", base_spec=repeated, exclusions={"x": "q"})
    # a run that is not STAGED is refused
    manifest = runs[0] / "capture-manifest.json"
    changed = json.loads(manifest.read_text())
    changed["status"] = "REFUSED"
    manifest.write_text(json.dumps(changed))
    with pytest.raises(BindError, match="not a STAGED run"):
        attempt("g.json")


def _with_root(base: Path, root: Path) -> Path:
    spec = json.loads(base.read_text())
    spec["roots"]["data"] = str(root)
    path = base.with_name("base-other-root.json")
    path.write_text(json.dumps(spec))
    return path


def test_bind_spec_refuses_a_run_that_does_not_verify(tmp_path: Path) -> None:
    harness = _captured(tmp_path, weekday_range(date(2026, 10, 5), date(2026, 10, 5)))
    (run_dir,) = harness.run_dirs()
    statement = run_dir / "statement.xml"
    statement.write_bytes(statement.read_bytes() + b"\n")  # the staged bytes changed
    with pytest.raises(Exception, match="manifest hash|file-hash|differs"):
        bind_spec(
            base_spec=_base_spec(tmp_path),
            runs=[run_dir],
            root_id="data",
            as_of=date(2026, 10, 5),
            out=tmp_path / "x.json",
        )


def test_the_preflight_runs_the_generation_builder_and_writes_nothing_on_a_refusal(
    tmp_path: Path,
) -> None:
    harness = _captured(tmp_path, weekday_range(date(2026, 10, 5), date(2026, 10, 5)))
    out = tmp_path / "never.json"
    with pytest.raises(BindError, match="generation builder refused the spec"):
        bind_spec(
            base_spec=_base_spec(tmp_path),
            runs=harness.run_dirs(),
            root_id="data",
            as_of=date(2026, 10, 5),
            out=out,
            preflight=True,
        )
    assert not out.exists()


def test_make_evidence_writes_what_the_authorization_accepts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import datetime, timezone

    from quant_engine.ibkr.capture import GovernanceError, authorize_live_read

    plan_path = tmp_path / "plan.json"
    assert (
        main(
            [
                "build-plan",
                "--first-day",
                "2026-10-05",
                "--last-day",
                "2026-10-07",
                "--pull-date",
                "2026-11-03",
                "--label",
                LABEL,
                "--out",
                str(plan_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    out = tmp_path / "evidence.json"
    argv = [
        "make-evidence",
        "--plan",
        str(plan_path),
        "--review-identity",
        "synthetic-reviewer",
        "--review-reference",
        "https://github.com/dachent/quant_engine/issues/1#issuecomment-1",
        "--issued-at",
        "2026-11-03T00:00:00Z",
        "--expires-at",
        "2026-11-04T00:00:00Z",
        "--out",
        str(out),
    ]
    assert main(argv) == 0
    printed = _emitted(capsys)
    assert printed["max_send_requests"] == 3 and printed["max_get_attempts"] == 6
    plan = load_plan(plan_path.read_bytes())
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    now = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    governance = authorize_live_read(
        out, plan=plan, receipt_root=receipts, now=lambda: now
    )
    assert governance.max_send_requests == 3 and governance.max_get_attempts == 6
    # create-only, and a budget the override authorization would refuse is refused up front
    assert main(argv) == 2
    low = [*argv[:-2], "--max-get-attempts", "5", "--out", str(tmp_path / "low.json")]
    assert main(low) == 2
    assert not (tmp_path / "low.json").exists()
    with pytest.raises(GovernanceError):
        authorize_live_read(
            out, plan=plan, receipt_root=receipts, now=lambda: now
        )  # single use
