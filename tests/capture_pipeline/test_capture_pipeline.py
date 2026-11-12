"""Capture pipeline (#913): contract lint, S0..S5, closure checks, stage ports. Synthetic data only."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import threading

import pytest

from tools import capture_pipeline as cp
from tools import capture_refresh as cr
from tools.capture_stages import ibkr_equity_history as ibkr
from tools.capture_stages import schwab_cash as sc
from tools.capture_stages import statement_browser as sb

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "capture-sources-v1.json"
CUT = "2026-11-06"


def write_contract(tmp_path: Path, mutate=None) -> Path:
    doc = json.loads(CONTRACT.read_text(encoding="utf-8"))
    if mutate:
        mutate(doc)
    p = tmp_path / "c.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def code_of(fn, *a, **k) -> str:
    with pytest.raises(cp.CaptureRefused) as e:
        fn(*a, **k)
    return e.value.code


def src(**over):
    base = dict(
        source_id="schwab_balances_nav", provider="SCHWAB", transport="BROWSER_SIGNED_IN", credential_ref=None,
        capture_stage={"mode": "EXISTING_RECORDER", "tool": "t"}, login_stop=True, max_age_days=7,
        closure_checks=("sum_equals_total",), admits=("reserve_nav",), owner_declared_ok=False, cdp_port=9334,
        window_hosts=("client.schwab.com",),
    )  # fmt: skip
    base.update(over)
    return cp.Source(**base)


def sources_of(s):
    return {s.source_id: s}


# ------------------------------------------------------------------ contract lint


def test_real_contract_loads_with_real_readers_and_tools():
    s = cp.load_sources(CONTRACT, repo_root=ROOT)
    assert len(s) == 10 and all(x.closure_checks for x in s.values())
    assert s["matrixport_margin"].transport == "CREDENTIAL_MANAGER"


def _src(d, i=0):
    return d["sources"][i]


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda d: _src(d).update(transport="CARRIER_PIGEON"), "CONTRACT_TRANSPORT"),
        (lambda d: _src(d).update(login_stop=False), "CONTRACT_LOGIN_STOP"),
        (
            lambda d: _src(d, 4).update(credential_ref="tok=abc"),
            "CONTRACT_CREDENTIAL_REF",
        ),
        (
            lambda d: _src(d, 4).update(credential_ref="-File"),
            "CONTRACT_CREDENTIAL_REF",
        ),
        (lambda d: _src(d, 4).update(credential_ref=None), "CONTRACT_CREDENTIAL_REF"),
        (
            lambda d: _src(d).update(credential_ref="a-secret"),
            "CONTRACT_CREDENTIAL_REF",
        ),
        (lambda d: _src(d)["closure_checks"].append("vibes"), "CONTRACT_UNKNOWN_CHECK"),
        (lambda d: _src(d).update(closure_checks=[]), "CONTRACT_CHECKS"),
        (
            lambda d: _src(d)["admits"].append("no_such_reader"),
            "CONTRACT_UNKNOWN_READER",
        ),
        (
            lambda d: _src(d, 1).update(source_id=_src(d)["source_id"]),
            "CONTRACT_DUPLICATE_SOURCE",
        ),
        (lambda d: _src(d, 1).update(source_id="../esc"), "CONTRACT_SOURCE_ID"),
        (lambda d: _src(d)["freshness"].update(max_age_days=-1), "CONTRACT_FRESHNESS"),
        (lambda d: _src(d).update(freshness=[]), "CONTRACT_FRESHNESS"),
        (lambda d: _src(d).pop("owner_declared_ok"), "CONTRACT_FIELD_MISSING"),
        (
            lambda d: _src(d).update(owner_declared_ok="false"),
            "CONTRACT_OWNER_DECLARED",
        ),
        (lambda d: _src(d).update(owner_declared_ok=True), "CONTRACT_OWNER_DECLARED"),
        (lambda d: _src(d).update(surprise=1), "CONTRACT_UNKNOWN_KEY"),
        (
            lambda d: _src(d)["capture_stage"].update(mode="WHATEVER"),
            "CONTRACT_STAGE_MODE",
        ),
        (lambda d: _src(d)["capture_stage"].update(tool=None), "CONTRACT_STAGE_TOOL"),
        (
            lambda d: _src(d)["capture_stage"].update(tool="tools/nope.py"),
            "CONTRACT_STAGE_TOOL_MISSING",
        ),
        (lambda d: _src(d).update(cdp_port=None), "CONTRACT_CDP_PORT"),
        (lambda d: _src(d).update(cdp_port="9334@evil"), "CONTRACT_CDP_PORT"),
        (lambda d: _src(d).update(window_hosts=[]), "CONTRACT_WINDOW_HOSTS"),
        (
            lambda d: _src(d).update(window_hosts=["not a host"]),
            "CONTRACT_WINDOW_HOSTS",
        ),
        (lambda d: _src(d, 8).pop("probe_url"), "CONTRACT_PROBE_URL"),
        (
            lambda d: d["readers"]["hwm"].update(symbol="no_such_symbol_xyz"),
            "CONTRACT_READER_MISSING",
        ),
    ],
)
def test_contract_lint_refuses(tmp_path, mutate, code):
    assert (
        code_of(cp.load_sources, write_contract(tmp_path, mutate), repo_root=ROOT)
        == code
    )


def test_owner_declared_needs_evidence_record(tmp_path):
    def ok(d):
        _src(d).update(
            owner_declared_ok=True,
            owner_evidence={"sha256": "a" * 64, "owner_line_date": "2026-11-06"},
        )

    assert cp.load_sources(write_contract(tmp_path, ok), repo_root=ROOT)[
        "schwab_balances_nav"
    ].owner_declared_ok


# ------------------------------------------------------------------ S0


def ok_window(_s):
    return cp.WINDOW_OK


def test_preflight_statuses():
    vault = lambda _r: True  # noqa: E731

    def pre(s, **k):
        return cp.preflight(
            s,
            window_state=k.get("w", ok_window),
            vault_ok=k.get("v", vault),
            http_ok=k.get("h", lambda _s: True),
        )

    assert pre(src(), w=lambda _s: cp.WINDOW_ABSENT).status == "BLOCKED_NO_WINDOW"
    p = pre(src(), w=lambda _s: cp.WINDOW_LOGIN_WALL)
    assert (p.status, "owner" in p.owner_step) == ("BLOCKED_LOGIN", True)
    assert pre(src()).status == "READY"
    assert (
        pre(src(capture_stage={"mode": "NEW_STAGE_REQUIRED", "tool": None})).status
        == "BLOCKED_NOT_WIRED"
    )
    v = src(
        transport="FLEX_TOKEN_VAULT",
        credential_ref="s",
        login_stop=False,
        cdp_port=None,
        window_hosts=(),
    )
    assert pre(v, v=lambda _r: False).status == "BLOCKED_CREDENTIAL"
    derive = src(
        **{**v.__dict__, "capture_stage": {"mode": "DERIVE_FROM_RETAINED", "tool": "t"}}
    )
    assert (
        pre(derive, v=lambda _r: False).status == "READY"
    )  # reading retained bytes needs no credential
    pub = src(
        transport="PUBLIC_HTTP",
        login_stop=False,
        cdp_port=None,
        window_hosts=(),
        probe_url="https://x.test/",
    )
    assert (
        pre(pub, h=lambda _s: False).status == "BLOCKED_UNREACHABLE"
        and pre(pub).status == "READY"
    )
    cm = src(
        transport="CREDENTIAL_MANAGER",
        credential_ref="BIT.com-API",
        login_stop=False,
        cdp_port=None,
        window_hosts=(),
    )
    p = pre(
        cm, v=lambda _r: False
    )  # a vault miss is irrelevant to a credential-manager source
    assert p.status == "UNVERIFIABLE_BY_DESIGN" and "BIT.com-API" in p.owner_step
    assert (
        pre(src(), w=lambda _s: 1 / 0).status == "BLOCKED_PROBE_ERROR"
    )  # a broken probe blocks this source only
    assert (
        code_of(cp.preflight, src(), window_state=lambda _s: "??", vault_ok=vault)
        == "PREFLIGHT_UNKNOWN_WINDOW_STATE"
    )


def tabs(*urls):
    return lambda _p: [{"type": "page", "url": u} for u in urls]


def test_cdp_probe_is_host_scoped_and_needs_positive_evidence():
    s = src()

    def st(*u):
        return cr.cdp_window_state(s, fetch=tabs(*u))

    assert st("https://client.schwab.com/app/accounts") == cp.WINDOW_OK
    assert st("https://client.schwab.com/login/signon/uno") == cp.WINDOW_LOGIN_WALL
    assert (
        st("https://client.schwab.com/app/x", "https://client.schwab.com/login")
        == cp.WINDOW_OK
    )
    assert (
        st("https://www.portfolio123.com/app") == cp.WINDOW_ABSENT
    )  # another site's tab is not this source's window
    assert st("chrome://newtab/") == cp.WINDOW_ABSENT
    assert (
        st("https://evil.test/?next=client.schwab.com") == cp.WINDOW_ABSENT
    )  # a query string is not a host
    assert st("https://client.schwab.com.evil.test/") == cp.WINDOW_ABSENT
    assert cr.cdp_window_state(s, fetch=lambda _p: []) == cp.WINDOW_ABSENT
    assert (
        cr.cdp_window_state(s, fetch=lambda _p: {"not": "a list"}) == cp.WINDOW_ABSENT
    )
    assert (
        cr.cdp_window_state(
            s, fetch=lambda _p: (_ for _ in ()).throw(ValueError("bad json"))
        )
        == cp.WINDOW_ABSENT
    )
    assert cr.cdp_window_state(src(cdp_port=None)) == cp.WINDOW_ABSENT


# ------------------------------------------------------------------ closure checks


def test_nav_identity_chain_vacuous_and_nan():
    good = [
        {"opening": 100, "ending": 110, "components": [4, 6]},
        {"opening": 110, "ending": 111, "components": [1]},
    ]
    assert cp.check_nav_identity(good).ok
    assert (
        cp.check_nav_identity(
            [{"opening": 100, "ending": 110, "components": [4, 5]}]
        ).code
        == "NAV_IDENTITY_BREAK"
    )
    broken_chain = [good[0], {"opening": 120, "ending": 121, "components": [1]}]
    assert cp.check_nav_identity(broken_chain).count == 1
    assert cp.check_nav_identity(
        broken_chain, chain=False
    ).ok  # documented restatement boundary
    assert cp.check_nav_identity([]).code == "NO_ROWS"
    assert (
        cp.check_nav_identity(
            [{"opening": "nan", "ending": "nan", "components": []}]
        ).code
        == "NON_FINITE"
    )


def test_coverage_window_tolerance_and_vacuous():
    days = ["2026-11-02", "2026-11-03", "2026-11-05", "2026-11-06"]  # Wed 11-04 missing
    assert cp.check_coverage(days, "2026-11-02", "2026-11-06").code == "COVERAGE_GAP"
    assert cp.check_coverage(
        days, "2026-11-02", "2026-11-06", non_trading=["2026-11-04"]
    ).ok
    assert cp.check_coverage(
        [*days, "2026-11-04"], "2026-11-02", "2026-11-08"
    ).ok  # weekend not required
    assert cp.check_coverage(days, "2026-11-02", "2026-11-06", max_missing=1).ok
    assert not cp.check_coverage(
        days[:2], "2026-11-02", "2026-11-06", max_missing=1
    ).ok  # total missing is bounded too
    assert (
        cp.check_coverage([*days, "2026-11-09"], "2026-11-02", "2026-11-06").code
        == "OUT_OF_WINDOW"
    )
    assert cp.check_coverage([], "2026-11-06", "2026-11-02").code == "BAD_WINDOW"
    assert cp.check_coverage([], "2026-11-07", "2026-11-08").code == "NO_EXPECTED_DAYS"
    assert cp.check_coverage([], "2026-11-02", "2026-11-06").code == "COVERAGE_GAP"


def test_expected_dates_exact():
    assert cp.check_expected_dates(
        ["2026-03-31", "2026-06-30"], ["2026-06-30", "2026-03-31"]
    ).ok
    assert (
        cp.check_expected_dates(["2026-03-31"], ["2026-03-31", "2026-06-30"]).code
        == "DATES_DIFFER"
    )
    assert (
        cp.check_expected_dates(["2026-03-31", "2026-04-30"], ["2026-03-31"]).code
        == "DATES_DIFFER"
    )
    assert cp.check_expected_dates([], []).code == "NO_EXPECTED_DAYS"


def test_sum_equals_total_is_cent_exact():
    assert cp.check_sum_equals_total([1.10, 2.20], 3.30).ok
    assert not cp.check_sum_equals_total([1.10, 2.20], 3.31).ok  # one cent off fails
    assert cp.check_sum_equals_total([], 0).code == "NO_ROWS"
    assert cp.check_sum_equals_total([1], "nan").code == "NON_FINITE"


def test_cash_walk_bounds_declared_debit_and_degenerate():
    assert cp.check_cash_walk(
        [{"nav": 100, "cash": 50}, {"nav": 100, "cash": 101}]
    ).ok  # unsettled proceeds
    assert (
        cp.check_cash_walk([{"nav": 100, "cash": 150}]).code
        == "CASH_WALK_OUT_OF_BOUNDS"
    )
    assert not cp.check_cash_walk([{"nav": 100, "cash": -5}]).ok
    assert cp.check_cash_walk([{"nav": 100, "cash": -5}], allowed_negative=10).ok
    assert cp.check_cash_walk([]).code == "NO_ROWS"
    assert cp.check_cash_walk([{"nav": 100, "cash": "nan"}]).code == "NON_FINITE"
    assert cp.check_cash_walk([{"nav": "nan", "cash": 1}]).code == "NON_FINITE"


def test_statement_cash_needs_every_statement_matched():
    walk = {"2026-01-31": 10.0, "2026-02-28": 20.0}
    assert cp.check_statement_cash(walk, {"2026-01-31": 10.0}).ok
    assert (
        cp.check_statement_cash(walk, {"2026-01-31": 10.5}).code
        == "STATEMENT_CASH_MISMATCH"
    )
    assert (
        cp.check_statement_cash(walk, {"2026-03-31": 1.0}).count == 1
    )  # no walk row is a failure, not a skip
    assert cp.check_statement_cash(walk, {}).code == "NO_STATEMENTS"
    assert not cp.check_statement_cash(
        {"2026-01-31": float("nan")}, {"2026-01-31": 1.0}
    ).ok


def test_bridge_needs_cash_exact_legs_and_a_bounded_move():
    kw = dict(
        last_nav_before=5000.0,
        first_nav_after=0.10,
        receipt_legs=[3000.0, 2040.0],
        receipt_total=5040.0,
    )
    assert cp.check_bridge_receipt(**kw, max_implied_move=0.02).ok  # implied +0.8%
    assert (
        cp.check_bridge_receipt(**kw, max_implied_move=0.005).code
        == "BRIDGE_NOT_CLOSED"
    )  # move outside the bound
    assert not cp.check_bridge_receipt(
        **{**kw, "receipt_total": 5040.01}, max_implied_move=0.02
    ).ok  # legs not exact
    assert not cp.check_bridge_receipt(
        **{**kw, "receipt_legs": []}, max_implied_move=0.02
    ).ok
    assert not cp.check_bridge_receipt(
        **{**kw, "receipt_legs": [5041.0, -1.0]}, max_implied_move=0.02
    ).ok
    assert not cp.check_bridge_receipt(
        **kw, max_implied_move=0.5
    ).ok  # the bound itself is capped
    assert not cp.check_bridge_receipt(
        **{**kw, "first_nav_after": -1.0}, max_implied_move=0.02
    ).ok
    assert not cp.check_bridge_receipt(
        **{**kw, "last_nav_before": float("inf")}, max_implied_move=0.02
    ).ok


def test_hwm_gap_bound_kill_and_degenerate():
    ok = {"margin_below_prior_high": 0.10, "max_observed_closure_error": 0.03}
    assert cp.check_hwm_gap_bound([ok]).ok
    hide = {
        **ok,
        "margin_below_prior_high": 0.02,
    }  # margin below the closure error: the gap could hide a peak
    assert cp.check_hwm_gap_bound([ok, hide]).code == "GAP_CAN_HIDE_PEAK"
    assert not cp.check_hwm_gap_bound(
        [{"margin_below_prior_high": -0.5, "max_observed_closure_error": -1.0}]
    ).ok
    assert not cp.check_hwm_gap_bound(
        [{**ok, "cannot_create_new_high": True, "margin_below_prior_high": 0.0}]
    ).ok
    assert cp.check_hwm_gap_bound([]).code == "NO_ROWS"


def test_stability_needs_real_equal_digests():
    d = "a" * 64
    assert cp.check_stability(d, d).ok
    assert not cp.check_stability(d, "b" * 64).ok and not cp.check_stability("", "").ok


# ------------------------------------------------------------------ S2/S3/S4/S5


def make_run(tmp_path: Path, files=None) -> tuple[Path, Path, dict]:
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    for name, data in (files or {"balances.csv": b"a,b\n1,2\n"}).items():
        (run / name).write_bytes(data)
    manifest_path = tmp_path / "manifest.json"
    manifest = cp.hash_files(
        run,
        manifest_path,
        source_id="schwab_balances_nav",
        captured_on="2026-11-12",
        tool="t",
    )
    return run, manifest_path, manifest


def good_check():
    return {"sum_equals_total": lambda: cp.check_sum_equals_total([1.0], 1.0)}


def admitted(tmp_path, *, as_of=CUT, cutoff=CUT, files=None, s=None):
    s = s or src()
    run, mpath, manifest = make_run(tmp_path, files)
    v = cp.verify(s, manifest=manifest, as_of=as_of, cutoff=cutoff, checks=good_check())
    pins, ev = tmp_path / "pins.json", tmp_path / "evidence"
    cp.admit_input(
        s, v, run_dir=run, manifest_path=mpath, evidence_root=ev, pin_path=pins
    )
    return s, pins, ev


def read(s, pins, ev, *, name="balances.csv", cutoff=CUT, reader="reserve_nav"):
    return cp.captured_input(
        s.source_id,
        name,
        pin_path=pins,
        evidence_root=ev,
        cutoff=cutoff,
        sources=sources_of(s),
        reader=reader,
    )


def test_verify_runs_every_check_and_never_masks():
    s = src(
        closure_checks=(
            "coverage",
            "sum_equals_total",
            "stability",
            "cash_walk",
            "nav_identity",
        )
    )
    manifest = {"source_id": s.source_id, "files": {}}
    checks = {
        "coverage": lambda: cp.check_coverage([], "2026-11-02", "2026-11-03"),
        "sum_equals_total": lambda: cp.check_sum_equals_total([1], 2),
        "cash_walk": lambda: 1 / 0,
        "nav_identity": lambda: "ok",
    }
    v = cp.verify(s, manifest=manifest, as_of=CUT, cutoff=CUT, checks=checks)
    assert v.status == "FAIL"
    assert set(v.codes) == {
        "coverage:COVERAGE_GAP", "sum_equals_total:TOTAL_MISMATCH", "stability:CHECK_NOT_RUN",
        "cash_walk:CHECK_ERROR", "nav_identity:CHECK_BAD_RESULT",
    }  # fmt: skip
    liar = {
        "sum_equals_total": lambda: cp.check_stability("a" * 64, "a" * 64)
    }  # another check's passing result
    assert cp.verify(
        src(), manifest=manifest, as_of=CUT, cutoff=CUT, checks=liar
    ).codes == ("sum_equals_total:CHECK_BAD_RESULT",)
    contradictory = {
        "sum_equals_total": lambda: cp.CheckResult("sum_equals_total", True, "WHATEVER")
    }
    assert (
        cp.verify(
            src(), manifest=manifest, as_of=CUT, cutoff=CUT, checks=contradictory
        ).status
        == "FAIL"
    )
    assert (
        code_of(
            cp.verify,
            s,
            manifest=manifest,
            as_of=CUT,
            cutoff=CUT,
            checks={"vibes": lambda: None},
        )
        == "UNDECLARED_CHECK"
    )
    assert (
        code_of(
            cp.verify,
            s,
            manifest={"source_id": "other"},
            as_of=CUT,
            cutoff=CUT,
            checks={},
        )
        == "VERIFY_SOURCE_MISMATCH"
    )


@pytest.mark.parametrize(
    "as_of,code",
    [
        ("2026-11-07", "POST_CUTOFF"),
        ("2026-10-01", "STALE_INPUT"),
        ("2026-10-30", "OK"),
    ],
)
def test_freshness_is_against_the_cutoff_not_the_clock(as_of, code):
    assert cp.freshness_code(src(), as_of=as_of, cutoff=CUT) == code
    assert (
        code_of(cp.freshness_code, src(), as_of="2026-11-06T10:00", cutoff=CUT)
        == "BAD_AS_OF"
    )


def test_round_trip_reader_gets_the_hashed_copy_and_bytes(tmp_path):
    s, pins, ev = admitted(tmp_path)
    p = read(s, pins, ev)
    assert p.read_bytes() == b"a,b\n1,2\n" and p.resolve().is_relative_to(ev.resolve())
    got = cp.captured_bytes(
        s.source_id,
        "balances.csv",
        pin_path=pins,
        evidence_root=ev,
        cutoff=CUT,
        sources=sources_of(s),
        reader="reserve_nav",
    )
    assert got == b"a,b\n1,2\n"
    assert (ev / s.source_id / CUT / "capture-manifest.json").is_file()
    entry = json.loads(pins.read_text())["pins"][s.source_id][CUT]
    assert (
        entry["root"] == f"{s.source_id}/{CUT}" and len(entry["manifest_sha256"]) == 64
    )  # relative root: no local path


def test_kill_edited_by_one_byte(tmp_path):
    s, pins, ev = admitted(tmp_path)
    (ev / s.source_id / CUT / "balances.csv").write_bytes(b"a,b\n1,3\n")
    assert code_of(read, s, pins, ev) == "HASH_MISMATCH"
    assert (
        cp.needs_refresh(
            s, pins=json.loads(pins.read_text())["pins"], evidence_root=ev, cutoff=CUT
        )
        == "PIN_TAMPERED"
    )


def test_kill_expired_post_cutoff_unpinned_and_wrong_reader(tmp_path):
    s, pins, ev = admitted(tmp_path)
    assert code_of(read, s, pins, ev, cutoff="2026-11-20") == "STALE_INPUT"
    assert code_of(read, s, pins, ev, cutoff="2026-11-01") == "POST_CUTOFF"
    assert code_of(read, s, pins, ev, name="other.csv") == "UNCAPTURED_INPUT"
    assert code_of(read, s, pins, ev, reader="model_vs_actual") == "READER_NOT_ADMITTED"
    kw = dict(evidence_root=ev, cutoff=CUT, sources=sources_of(s), reader="reserve_nav")
    assert (
        code_of(cp.captured_input, "nope", "balances.csv", pin_path=pins, **kw)
        == "UNCAPTURED_INPUT"
    )
    assert (
        code_of(
            cp.captured_input,
            s.source_id,
            "balances.csv",
            pin_path=tmp_path / "none.json",
            **kw,
        )
        == "UNCAPTURED_INPUT"
    )
    assert code_of(read, s, pins, ev, name="../pins.json") == "UNCAPTURED_INPUT"


def test_verdict_is_bound_to_the_manifest_asof_and_cutoff(tmp_path):
    s = src()
    run, mpath, manifest = make_run(tmp_path)
    v = cp.verify(s, manifest=manifest, as_of=CUT, cutoff=CUT, checks=good_check())
    kw = dict(evidence_root=tmp_path / "ev", pin_path=tmp_path / "p.json")
    forged = cp.Verdict(
        s.source_id, "PASS", (), (), v.manifest_sha256, CUT, CUT
    )  # hand-built, no verify() key
    assert (
        code_of(cp.admit_input, s, forged, run_dir=run, manifest_path=mpath, **kw)
        == "ADMIT_NOT_PASS"
    )
    run2 = (
        tmp_path / "run2"
    )  # verified manifest A, admit a different (unverified) manifest B for the same source
    run2.mkdir()
    (run2 / "balances.csv").write_bytes(b"other")
    mpath2 = tmp_path / "manifest2.json"
    cp.hash_files(run2, mpath2, source_id=s.source_id, captured_on="d", tool="t")
    assert (
        code_of(cp.admit_input, s, v, run_dir=run2, manifest_path=mpath2, **kw)
        == "ADMIT_VERDICT_MISMATCH"
    )
    failed = cp.verify(
        s,
        manifest=manifest,
        as_of=CUT,
        cutoff=CUT,
        checks={"sum_equals_total": lambda: cp.check_sum_equals_total([1], 2)},
    )
    assert (
        code_of(cp.admit_input, s, failed, run_dir=run, manifest_path=mpath, **kw)
        == "ADMIT_NOT_PASS"
    )
    stale = cp.verify(
        s, manifest=manifest, as_of="2026-09-01", cutoff=CUT, checks=good_check()
    )
    assert stale.status == "FAIL" and "STALE_INPUT" in stale.codes
    assert (
        code_of(cp.admit_input, s, stale, run_dir=run, manifest_path=mpath, **kw)
        == "ADMIT_NOT_PASS"
    )  # as-of is the verdict's


def test_admit_refuses_traversal_and_changed_bytes_before_copying(tmp_path):
    s = src()
    run, mpath, manifest = make_run(tmp_path)
    ev, pin = tmp_path / "ev", tmp_path / "p.json"
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"s")
    bad = json.loads(mpath.read_text())
    bad["files"]["../secret.txt"] = {"sha256": cp.sha256_file(outside), "bytes": 1}
    mp = tmp_path / "evil.json"
    mp.write_text(json.dumps(bad))
    v = cp.verify(s, manifest=bad, as_of=CUT, cutoff=CUT, checks=good_check())
    assert (
        code_of(
            cp.admit_input,
            s,
            v,
            run_dir=run,
            manifest_path=mp,
            evidence_root=ev,
            pin_path=pin,
        )
        == "UNSAFE_PATH"
    )
    for name in ("/abs.txt", "C:\\x.txt", "a\\b.txt", ""):
        assert code_of(cp._safe_rel, name) == "UNSAFE_PATH"
    (run / "balances.csv").write_bytes(b"changed")  # changed between hash and admit
    v2 = cp.verify(s, manifest=manifest, as_of=CUT, cutoff=CUT, checks=good_check())
    assert (
        code_of(
            cp.admit_input,
            s,
            v2,
            run_dir=run,
            manifest_path=mpath,
            evidence_root=ev,
            pin_path=pin,
        )
        == "ADMIT_HASH_MISMATCH"
    )
    assert not ev.exists() or not any(
        ev.rglob("*.csv")
    )  # nothing was copied before the check


def test_hash_files_refuses_symlinks_empty_and_overwrite(tmp_path):
    run = tmp_path / "r"
    run.mkdir()
    assert (
        code_of(
            cp.hash_files,
            run,
            tmp_path / "m.json",
            source_id="s",
            captured_on="d",
            tool="t",
        )
        == "EMPTY_CAPTURE"
    )
    (run / "f").write_bytes(b"x")
    cp.hash_files(run, tmp_path / "m.json", source_id="s", captured_on="d", tool="t")
    assert (
        code_of(
            cp.hash_files,
            run,
            tmp_path / "m.json",
            source_id="s",
            captured_on="d",
            tool="t",
        )
        == "CREATE_ONLY_EXISTS"
    )
    assert not list(tmp_path.glob(".m.json.*.tmp"))
    try:
        (run / "link").symlink_to(tmp_path / "m.json")
    except OSError:  # symlinks need a privilege on Windows
        return
    assert (
        code_of(
            cp.hash_files,
            run,
            tmp_path / "m2.json",
            source_id="s",
            captured_on="d",
            tool="t",
        )
        == "SYMLINK_IN_CAPTURE"
    )


def test_readmission_keeps_the_old_cycle_reproducible_and_is_create_only(tmp_path):
    s, pins, ev = admitted(tmp_path, files={"balances.csv": b"v1"})
    run2 = tmp_path / "run_b"
    run2.mkdir()
    (run2 / "balances.csv").write_bytes(b"v2")
    mp2 = tmp_path / "m_b.json"
    m2 = cp.hash_files(run2, mp2, source_id=s.source_id, captured_on="d", tool="t")
    v2 = cp.verify(
        s, manifest=m2, as_of="2026-11-13", cutoff="2026-11-13", checks=good_check()
    )
    cp.admit_input(
        s, v2, run_dir=run2, manifest_path=mp2, evidence_root=ev, pin_path=pins
    )
    assert (
        read(s, pins, ev, cutoff=CUT).read_bytes() == b"v1"
    )  # the earlier cycle still resolves to its capture
    assert read(s, pins, ev, cutoff="2026-11-13").read_bytes() == b"v2"
    assert (
        code_of(
            cp.admit_input,
            s,
            v2,
            run_dir=run2,
            manifest_path=mp2,
            evidence_root=ev,
            pin_path=pins,
        )
        == "CREATE_ONLY_EXISTS"
    )


def test_concurrent_admissions_all_survive(tmp_path):
    sources = {f"s{i}": src(source_id=f"s{i}") for i in range(8)}
    pins, ev, errors = tmp_path / "pins.json", tmp_path / "ev", []
    prepared = []
    for sid, s in sources.items():
        run = tmp_path / f"run_{sid}"
        run.mkdir()
        (run / "f.csv").write_bytes(sid.encode())
        mp = tmp_path / f"m_{sid}.json"
        m = cp.hash_files(run, mp, source_id=sid, captured_on="d", tool="t")
        prepared.append(
            (
                s,
                cp.verify(s, manifest=m, as_of=CUT, cutoff=CUT, checks=good_check()),
                run,
                mp,
            )
        )

    def go(args):
        s, v, run, mp = args
        try:
            cp.admit_input(
                s, v, run_dir=run, manifest_path=mp, evidence_root=ev, pin_path=pins
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(a,)) for a in prepared]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and set(json.loads(pins.read_text())["pins"]) == set(sources)


def test_failed_pin_write_rolls_the_copy_back_so_a_retry_works(tmp_path):
    s = src()
    run, mp, m = make_run(tmp_path)
    v = cp.verify(s, manifest=m, as_of=CUT, cutoff=CUT, checks=good_check())
    pins, ev = tmp_path / "pins.json", tmp_path / "ev"
    pins.write_text("{not json")
    with pytest.raises(ValueError):
        cp.admit_input(
            s, v, run_dir=run, manifest_path=mp, evidence_root=ev, pin_path=pins
        )
    assert not (ev / s.source_id / CUT).exists()
    pins.unlink()
    cp.admit_input(
        s, v, run_dir=run, manifest_path=mp, evidence_root=ev, pin_path=pins
    )  # corrected retry succeeds


# ------------------------------------------------------------------ refresh table and CLI


def test_refresh_table_all_logins_at_once_age_and_exit_codes(tmp_path):
    s, pins_path, ev = admitted(tmp_path)
    pins = json.loads(pins_path.read_text())["pins"]
    sources = {
        s.source_id: s,
        "p123": src(
            source_id="p123",
            provider="PORTFOLIO123",
            window_hosts=("portfolio123.com",),
        ),
        "ibkr": src(
            source_id="ibkr",
            transport="FLEX_TOKEN_VAULT",
            credential_ref="x",
            login_stop=False,
            cdp_port=None,
            window_hosts=(),
        ),
    }
    wall = lambda _s: cp.WINDOW_LOGIN_WALL  # noqa: E731
    rows = cp.refresh_table(
        sources,
        pins=pins,
        cutoff=CUT,
        window_state=wall,
        vault_ok=lambda _r: True,
        evidence_root=ev,
    )
    by = {r["source"]: r for r in rows}
    assert (by[s.source_id]["status"], by[s.source_id]["as_of"]) == ("SKIP_FRESH", CUT)
    assert (by["p123"]["status"], by["p123"]["reason"]) == (
        "BLOCKED_LOGIN",
        "NEVER_CAPTURED",
    )
    assert by["ibkr"]["status"] == "READY" and "p123" in cr.format_table(rows)
    assert cr.exit_code(rows) == 3
    assert (
        cr.exit_code([r for r in rows if r["status"] in ("READY", "SKIP_FRESH")]) == 4
    )  # ready to capture is not "fine"
    assert cr.exit_code([r for r in rows if r["status"] == "SKIP_FRESH"]) == 0
    (ev / s.source_id / CUT / "balances.csv").write_bytes(
        b"tampered"
    )  # a fresh pin over edited bytes is not fresh
    rows2 = cp.refresh_table(
        sources,
        pins=pins,
        cutoff=CUT,
        window_state=wall,
        vault_ok=lambda _r: True,
        evidence_root=ev,
    )
    assert {r["source"]: r["reason"] for r in rows2}[s.source_id] == "PIN_TAMPERED"


def test_cli_real_contract_exit_code_and_table(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cr, "cdp_window_state", lambda _s: cp.WINDOW_LOGIN_WALL)
    monkeypatch.setattr(cr, "vault_ok", lambda _r: True)
    monkeypatch.setattr(cr, "http_ok", lambda _s: True)
    argv = [
        "--cutoff",
        CUT,
        "--pins",
        str(tmp_path / "none.json"),
        "--evidence-root",
        str(tmp_path),
        "--contracts",
        str(CONTRACT),
    ]
    rc = cr.main(argv)
    out = capsys.readouterr().out
    assert rc == 3
    for token in (
        "BLOCKED_LOGIN",
        "sign in to SCHWAB",
        "BLOCKED_NOT_WIRED",
        "UNVERIFIABLE_BY_DESIGN",
        "schwab_daily_cash",
    ):
        assert token in out


# ------------------------------------------------------------------ reader switch-over


def admitted_for_reader(tmp_path):
    s = src(
        source_id="p123_model_live",
        provider="PORTFOLIO123",
        admits=("model_vs_actual",),
    )
    run = tmp_path / "run"
    run.mkdir()
    (run / "model.csv").write_bytes(b"x,y\n1,2\n")
    mp = tmp_path / "m.json"
    m = cp.hash_files(run, mp, source_id=s.source_id, captured_on="d", tool="t")
    v = cp.verify(
        s,
        manifest=m,
        as_of=CUT,
        cutoff=CUT,
        checks={"sum_equals_total": lambda: cp.check_sum_equals_total([1], 1)},
    )
    pins, ev = tmp_path / "pins.json", tmp_path / "evidence"
    cp.admit_input(s, v, run_dir=run, manifest_path=mp, evidence_root=ev, pin_path=pins)
    return s, pins, ev


READERS = cp.load_readers(CONTRACT)


def spec_for(s, pins, ev, **over):
    block = {
        "pins": cp.file_pin(pins), "evidence_root": str(ev),
        "bindings": {"model_vs_actual_input": {"source_id": s.source_id, "name": "model.csv", "reader": "model_vs_actual"}},
    }  # fmt: skip
    block.update(over)
    return {"schema_version": 1, "captured_inputs": block}


def test_resolve_switches_the_reader_and_refuses_everything_else(tmp_path):
    s, pins, ev = admitted_for_reader(tmp_path)
    sources = sources_of(s)

    def run(spec, cutoff=CUT):
        return cp.resolve_captured_inputs(
            spec, cutoff=cutoff, sources=sources, readers=READERS
        )

    out = run(spec_for(s, pins, ev))
    pin = out["model_vs_actual_input"]
    assert set(pin) == {"path", "sha256", "size_bytes"} and "captured_inputs" not in out
    assert (
        cp.sha256_file(Path(pin["path"])) == pin["sha256"]
    )  # the shape the real reader's _pin() verifies
    hand = {
        **spec_for(s, pins, ev),
        "model_vs_actual_input": {"path": "x", "sha256": "0", "size_bytes": 1},
    }
    assert (
        code_of(run, hand) == "UNCAPTURED_INPUT"
    )  # a hand-built pin cannot stand in for a captured one
    assert code_of(run, spec_for(s, pins, ev), cutoff="2026-12-31") == "STALE_INPUT"
    wrong = spec_for(s, pins, ev)
    wrong["captured_inputs"]["bindings"]["model_vs_actual_input"]["reader"] = "hwm"
    assert code_of(run, wrong) == "CAPTURED_INPUTS_SHAPE"
    assert run({"a": 1}) == {"a": 1}  # a legacy spec without the block is unchanged
    assert code_of(run, {"captured_inputs": {"pins": 1}}) == "CAPTURED_INPUTS_SHAPE"
    forged = spec_for(s, pins, ev)
    forged["captured_inputs"]["pins"] = {
        **forged["captured_inputs"]["pins"],
        "sha256": "0" * 64,
    }  # the pin file is pinned
    assert code_of(run, forged) == "HASH_MISMATCH"


def test_real_attended_preparation_refuses_a_source_the_real_contract_does_not_declare(
    tmp_path,
):
    import portfolio_attended_preparation as prep

    s, pins, ev = admitted_for_reader(tmp_path)
    s2 = src(
        source_id="not_in_contract"
    )  # admit under an id the real contract does not know
    assert s2.source_id not in cp.load_sources(CONTRACT, repo_root=ROOT)
    spec = {
        "schema_version": 1,
        "financial_data_cutoff": "2026-12-31",
        **spec_for(s, pins, ev),
    }
    spec["captured_inputs"]["bindings"]["model_vs_actual_input"]["source_id"] = (
        "not_in_contract"
    )
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(
        prep.AttendedPreparationError, match="captured inputs: UNCAPTURED_INPUT"
    ):
        prep._validated_inputs(path, "2026-12-31", "2027-01-02")


# ------------------------------------------------------------------ stage ports


def test_schwab_walk_basis_calendar_days_and_refusals():
    tx = [
        {"kind": "Journal", "event_date": "2026-01-02", "amount": 1000.0},
        {
            "kind": "Buy",
            "event_date": "2026-01-05",
            "settle_date": "2026-01-06",
            "amount": -400.0,
        },
        {"kind": "Cash Dividend", "event_date": "2026-01-07", "amount": 5.0},
        {"kind": "Journal", "event_date": "2026-01-07", "amount": None},
    ]
    days = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    w = sc.walk_cash(days, tx, anchor_date="2026-01-08", anchor_cash=605.0)
    assert (w["2026-01-05"], w["2026-01-06"], w["2026-01-07"], w["2026-01-02"]) == (
        1000.0,
        600.0,
        605.0,
        1000.0,
    )
    assert (
        w["2026-01-03"] == 1000.0
    )  # calendar days: a weekend month-end statement has a row
    late = [{"kind": "Journal", "event_date": "2026-01-09", "amount": 1.0}]
    with pytest.raises(ValueError, match="FLOW_AFTER_ANCHOR"):
        sc.walk_cash(days, late, anchor_date="2026-01-08", anchor_cash=0)
    with pytest.raises(ValueError, match="ANCHOR_BEFORE_NAV"):
        sc.walk_cash(days, [], anchor_date="2026-01-07", anchor_cash=0)
    with pytest.raises(ValueError, match="MISSING_SETTLE_DATE"):
        sc.walk_cash(
            days,
            [{"kind": "Sell", "event_date": "2026-01-05", "amount": 1.0}],
            anchor_date="2026-01-08",
            anchor_cash=0,
        )
    assert sc.walk_cash([], [], anchor_date="2026-01-08", anchor_cash=0) == {}


def test_walk_plus_statement_check_catches_a_missing_post_ledger_trade():
    tx = [{"kind": "Journal", "event_date": "2026-01-02", "amount": 1000.0}]
    w = sc.walk_cash(
        ["2026-01-02", "2026-01-30"], tx, anchor_date="2026-01-30", anchor_cash=900.0
    )  # a 100 buy never captured
    assert w["2026-01-02"] == 900.0
    assert (
        cp.check_statement_cash(w, {"2026-01-02": 1000.0}).code
        == "STATEMENT_CASH_MISMATCH"
    )


def test_statement_cash_parser():
    detail = "Total Cash and Cash Investments        $20.00     $30.00     $10.00\n"
    box = "Cash and Cash Investments                 900.00 13%\n"  # asset-allocation box (counts T-bill ETFs): never used
    assert sc.parse_statement_cash(box + detail) == 30.0
    assert (
        sc.parse_statement_cash("Settled Debit Balance            (12.34)\n" + detail)
        == -12.34
    )
    assert (
        sc.parse_statement_cash(
            "Total Cash and Cash Investments    $5.00    ($7.50)   ($12.50)\n"
        )
        == -7.5
    )
    assert (
        sc.parse_statement_cash(
            "Bank Sweep      CHARLES SCHWAB       $1.00      $2.00\n" + box
        )
        == 2.0
    )
    assert (
        sc.parse_statement_cash(detail + detail) is None
    )  # two accounts' totals: ambiguous, refused
    assert sc.parse_statement_cash(box) is None


XML = """<FlexQueryResponse><FlexStatements>
<FlexStatement accountId="U1110001"><ChangeInNAV accountId="U1110001" toDate="20251103" endingValue="100.5"/>
<EquitySummaryInBase><EquitySummaryByReportDateInBase accountId="U1110001" reportDate="20251103" cash="5" total="100.5"/>
<EquitySummaryByReportDateInBase accountId="U1110001" reportDate="20251104" cash="6" total="91"/>
<EquitySummaryByReportDateInBase accountId="U1110001" reportDate="20251105" cash="6"/>
</EquitySummaryInBase></FlexStatement>
<FlexStatement accountId="U1110001"><ChangeInNAV accountId="U1110001" toDate="20251104" endingValue="90"/></FlexStatement>
<FlexStatement accountId="X1110001"><EquitySummaryInBase><EquitySummaryByReportDateInBase accountId="X1110001" reportDate="20251103" cash="1" total="1"/>
</EquitySummaryInBase></FlexStatement></FlexStatements></FlexQueryResponse>"""


def test_ibkr_extract_exact_accounts_conflicts_and_nav_check(tmp_path):
    a, b = tmp_path / "a.xml", tmp_path / "b.xml"
    a.write_text(XML, encoding="utf-8")
    b.write_text(XML.replace('cash="5"', 'cash="7"'), encoding="utf-8")
    s = ibkr.extract([a, b], ["U1110001"])["U1110001"]
    assert sorted(s) == [
        "20251103",
        "20251104",
        "20251105",
    ]  # the other account sharing the suffix is not merged
    assert (
        s["20251103"]["row"]["cash"] == "5"
        and s["20251103"]["source"] == "a.xml"
        and s["20251103"]["conflict"] is True
    )
    assert s["20251104"]["conflict"] is False
    assert ibkr.nav_vs_change_in_nav(s) == {
        "mismatch": ["20251104", "20251105"],
        "unchecked": [],
    }  # 91 vs 90; no total
    with pytest.raises(ValueError, match="ACCOUNT_IDS"):
        ibkr.extract([a], [""])
    only = ibkr.extract([a], ["U1110001"])["U1110001"]
    only["20251105"]["row"]["total"], only["20251105"]["ending"] = "1", None
    assert (
        "20251105" in ibkr.nav_vs_change_in_nav(only)["unchecked"]
    )  # a mid-month day is unchecked, not a pass


class FakePage:
    def __init__(self, pages, redirect=None):
        self.pages, self.redirect, self.url, self.visited = (
            pages,
            redirect or {},
            "",
            [],
        )

    def goto(self, url):
        self.visited.append(url)
        self.url = self.redirect.get(url, url)

    def inner_text(self, _sel):
        return self.pages[self.url]


ORIGIN = "https://app.example.test"


def test_capture_pages_create_only_wall_redirect_and_marker(tmp_path):
    long_text = "Account value\n" * 400
    page = FakePage(
        {f"{ORIGIN}/a": long_text, f"{ORIGIN}/b": "Please sign in to continue"}
    )
    plan = [{"name": "a", "url": f"{ORIGIN}/a"}, {"name": "b", "url": f"{ORIGIN}/b"}]
    with pytest.raises(sb.StageBlocked) as e:
        sb.capture_pages(page, plan, tmp_path / "run", allowed_origin=ORIGIN)
    assert e.value.code == "BLOCKED_LOGIN"
    assert (tmp_path / "run" / "a.txt").read_bytes() == long_text.encode() and not (
        tmp_path / "run" / "b.txt"
    ).exists()
    with pytest.raises(FileExistsError):
        sb.capture_pages(
            FakePage({f"{ORIGIN}/a": long_text}),
            plan[:1],
            tmp_path / "run",
            allowed_origin=ORIGIN,
        )
    off = FakePage(
        {"https://evil.example.test/x": long_text},
        redirect={f"{ORIGIN}/c": "https://evil.example.test/x"},
    )
    with pytest.raises(ValueError, match="REDIRECT_ORIGIN"):
        sb.capture_pages(
            off,
            [{"name": "c", "url": f"{ORIGIN}/c"}],
            tmp_path / "r2",
            allowed_origin=ORIGIN,
        )
    assert not (tmp_path / "r2" / "c.txt").exists()
    quiet = FakePage(
        {f"{ORIGIN}/d": "x" * 5000}
    )  # a long page that is not the expected statement
    with pytest.raises(sb.StageBlocked):
        sb.capture_pages(
            quiet,
            [{"name": "d", "url": f"{ORIGIN}/d", "expect": "Closing Balance"}],
            tmp_path / "r3",
            allowed_origin=ORIGIN,
        )


def test_capture_pages_refuses_foreign_origin_before_navigating_and_bad_names(tmp_path):
    page = FakePage({})
    with pytest.raises(ValueError, match="PLAN_ORIGIN"):
        sb.capture_pages(
            page,
            [{"name": "a", "url": "https://evil.example.test/a"}],
            tmp_path,
            allowed_origin=ORIGIN,
        )
    assert page.visited == []
    with pytest.raises(ValueError, match="PLAN_NAME"):
        sb.capture_pages(
            page,
            [{"name": "../a", "url": f"{ORIGIN}/a"}],
            tmp_path,
            allowed_origin=ORIGIN,
        )


def test_login_wall_by_url_or_short_prompt_not_a_long_page_with_a_footer_link():
    assert sb.is_login_wall("https://x.test/login?next=1", "anything")
    assert sb.is_login_wall("https://x.test/app", "Your session has timed out. Log in")
    assert not sb.is_login_wall(
        "https://x.test/app", ("balance " * 800) + "Log in help"
    )


JH = (
    "STATEMENT PERIOD: 07/01/2026 - 09/30/2026\n"
    "Opening Balance   $ 100.00\n"
    "For the period\n"
    + "".join(
        f"{'x' * 160}{label}     {v}\n"
        for label, v in (
            ("a", "100.00"),
            ("b", "$ 5.00"),
            ("c", "-2.50"),
            ("d", "102.50"),
        )
    )
    + "Closing Balance   $102.50\n"
)


def test_jh_parser_closure_and_boundary_gap():
    q = sb.parse_jh_statement(JH)
    assert (q["opening"], q["closing"], q["items"]) == (
        Decimal("100.00"),
        Decimal("102.50"),
        [Decimal("5.00"), Decimal("-2.50")],
    )
    assert cp.check_nav_identity(sb.jh_identity_rows([q]), chain=False).ok
    bad = {**q, "items": [Decimal("5.00")]}
    assert (
        cp.check_nav_identity(sb.jh_identity_rows([bad]), chain=False).code
        == "NAV_IDENTITY_BREAK"
    )
    nxt = {
        **q,
        "start": "2026-10-01",
        "opening": Decimal("102.40"),
        "closing": Decimal("103"),
        "items": [Decimal("0.60")],
    }
    assert sb.jh_boundary_gaps([q, nxt]) == [
        Decimal("-0.10")
    ]  # a restatement is reported, not absorbed
    assert not cp.check_nav_identity(
        sb.jh_identity_rows([q, nxt])
    ).ok  # the chain check alone would fail it


COMPOSER = (
    "Cash Summary\nEnding Value $3.00 $3.00\nTotal Market Value $13.00\nHoldings\n"
    "Symbol Description Quantity Market Price Market Value Cost Price Unrealized TD Cost Basis\n"
    "*Cash USD $ -- $ -- $3.00 $ -- $ -- $ --\n"
    "ABC SOME FUND 2.5 $4.00 $10.00 $4.10 -$0.25 $10.25\n3\nIncome\n"
)


def test_composer_parser_closes_and_cutoff_rule():
    c = sb.parse_composer_statement(COMPOSER)
    assert c["total_market_value"] == Decimal("13.00") and c["cash_ending"] == Decimal(
        "3.00"
    )
    assert c["holdings_values"] == [Decimal("3.00"), Decimal("10.00")]
    assert cp.check_sum_equals_total(c["holdings_values"], c["total_market_value"]).ok
    assert not cp.check_sum_equals_total(
        c["holdings_values"][:1], c["total_market_value"]
    ).ok
    pts = [
        {"date": "2026-09-30", "v": 1},
        {"date": "2026-10-31", "v": 2},
        {"date": "2026-11-11", "v": 3},
    ]
    assert (
        sb.latest_on_or_before(pts, CUT)["v"] == 2
    )  # the later live balance is post-cutoff evidence
    assert sb.latest_on_or_before(pts, "2026-09-01") is None
    with pytest.raises(ValueError, match="COMPOSER_TOTAL"):
        sb.parse_composer_statement("nothing")
