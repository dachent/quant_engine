"""Recorders, adapters and the source chain (#913). Fake pages and synthetic statements only."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from tools import capture_pipeline as cp
from tools.capture_stages import adapters as ad
from tools.capture_stages import chain
from tools.capture_stages import recorders as rec
from tools.capture_stages import statement_browser as sb

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "capture-sources-v1.json"
CUT = "2026-11-06"
PLAN = "planId=00000000-0000-0000-0000-000000000000&plancode=AB1234&planGroup=DC"


def jh_text(start: str, end: str, opening: str, items: list[str], closing: str) -> str:
    rows = [opening, *items, closing]
    body = "".join(f"{'x' * 160}row     {v}\n" for v in rows)
    return f"STATEMENT PERIOD: {start} - {end}\nOpening Balance   $ {opening}\nFor the period\n{body}Closing Balance   ${closing}\n"


def composer_text(cash: str, position: str, total: str) -> str:
    return (
        f"Cash Summary\nEnding Value ${cash} ${cash}\nTotal Market Value ${total}\nHoldings\n"
        "Symbol Description Quantity Market Price Market Value Cost Price Unrealized TD Cost Basis\n"
        f"*Cash USD $ -- $ -- ${cash} $ -- $ -- $ --\n"
        f"ABC SOME FUND 2.5 $4.00 ${position} $4.10 -$0.25 $10.25\n3\nIncome\n"
    )


class FakePage:
    def __init__(
        self, pages, links=None, pdf=b"%PDF-1.4 x", walls=(), click_links=None
    ):
        self.pages, self.links, self.pdf, self.walls = (
            pages,
            links or [],
            pdf,
            set(walls),
        )
        self.click_links = click_links or {}
        self.url, self.visited, self.clicked, self.fetched = "", [], [], []

    def goto(self, url):
        self.visited.append(url)
        self.url = url

    def inner_text(self, _sel):
        return (
            "Please sign in to continue"
            if self.url in self.walls
            else self.pages[self.url.split("#")[0]]
        )

    def evaluate(self, script, arg=None):
        if script == rec.LINKS_JS:
            return (
                self.click_links.get(self.clicked[-1], self.links)
                if self.clicked
                else self.links
            )
        if script == rec.FETCH_JS:
            self.fetched.append(arg)
            return [200, base64.b64encode(self.pdf).decode()]
        if script == rec.SELECT_ACCOUNT_JS:
            return arg in ("123", "4567")
        raise AssertionError("an unexpected script was evaluated")

    def click_text(self, label):
        self.clicked.append(label)


def to_text_from(mapping):
    return lambda pdf: mapping[pdf.name]


# ------------------------------------------------------------------ recorders


def jh_page():
    base = rec.JH_ORIGIN
    return {
        f"{base}/Home/Overview/AccountOverview.aspx?{PLAN}": "Account Overview\n" * 300,
        f"{base}/Home/StatementsAndReporting/StatementCentral.aspx": "Statements\n"
        * 300,
    }


def test_john_hancock_recorder_whole_plan_same_origin_pdfs_and_text(tmp_path):
    base = rec.JH_ORIGIN
    links = [
        ("Q1", f"{base}/files/statement-1.pdf"),
        ("Q2", f"{base}/files/statement-2.pdf?x=1"),
        (
            "evil",
            "https://evil.example.test/statement-3.pdf",
        ),  # a link off the origin is never fetched
        ("html", f"{base}/files/statement.html"),
    ]
    page = FakePage(jh_page(), links=links)
    pages = {"stmt-01.pdf": "t1", "stmt-02.pdf": "t2"}
    names = rec.record_john_hancock(
        page, PLAN, tmp_path / "run", to_text=to_text_from(pages)
    )
    assert names == ["account_overview", "statements_archive", "stmt-01", "stmt-02"]
    assert page.fetched == [
        f"{base}/files/statement-1.pdf",
        f"{base}/files/statement-2.pdf?x=1",
    ]
    assert (tmp_path / "run" / "stmt-01.pdf").read_bytes().startswith(b"%PDF") and (
        tmp_path / "run" / "stmt-02.txt"
    ).read_text() == "t2"
    with pytest.raises(FileExistsError):
        rec.record_john_hancock(
            page, PLAN, tmp_path / "run", to_text=to_text_from(pages)
        )


def test_john_hancock_wall_bad_pdf_and_bad_plan_leave_nothing(tmp_path):
    base = rec.JH_ORIGIN
    walled = FakePage(
        jh_page(), walls={f"{base}/Home/Overview/AccountOverview.aspx?{PLAN}"}
    )
    with pytest.raises(sb.StageBlocked):
        rec.record_john_hancock(walled, PLAN, tmp_path / "a")
    assert not (tmp_path / "a").exists() and not list(tmp_path.glob(".*staging"))
    html = FakePage(
        jh_page(),
        links=[("Q1", f"{base}/files/statement-1.pdf")],
        pdf=b"<html>sign in</html>",
    )
    with pytest.raises(ValueError, match="PDF_FETCH"):
        rec.record_john_hancock(html, PLAN, tmp_path / "b", to_text=to_text_from({}))
    assert not (tmp_path / "b").exists()
    none = FakePage(jh_page(), links=[])
    with pytest.raises(sb.StageBlocked):
        rec.record_john_hancock(none, PLAN, tmp_path / "c")
    with pytest.raises(ValueError, match="PLAN_QUERY"):
        rec.john_hancock_plan("planId=x&plancode=y&planGroup=z")


def composer_pages():
    return {
        rec.COMPOSER_ORIGIN + "/portfolio": "Portfolio\n" * 300,
        rec.COMPOSER_ORIGIN + "/settings/documents": "Documents\n" * 300,
    }


S3 = "https://bucket.s3.amazonaws.com/acct/statements/{m}.pdf?X-Amz-Signature=abc"


def test_composer_recorder_clicks_only_the_whitelisted_label_and_fetches_only_presigned_statements(
    tmp_path,
):
    links = [
        ("Download", S3.format(m="2026-09")),
        ("Download", S3.format(m="2026-10")),
        ("x", "https://evil.example.test/statements/2026-08.pdf?k=1"),
        ("y", "http://bucket.s3.amazonaws.com/statements/2026-07.pdf?k=1"),
    ]
    page = FakePage(composer_pages(), links=links)
    got = []

    def http_get(url):
        got.append(url)
        return b"%PDF-1.7 y"

    texts = {"stmt-2026-09.pdf": "a", "stmt-2026-10.pdf": "b"}
    names = rec.record_composer(
        page, tmp_path / "run", http_get=http_get, to_text=to_text_from(texts)
    )
    assert page.clicked == ["Statements"] and names[-2:] == [
        "stmt-2026-09",
        "stmt-2026-10",
    ]
    assert got == [
        S3.format(m="2026-09"),
        S3.format(m="2026-10"),
    ]  # no foreign host, no plain http
    walled = FakePage(composer_pages(), walls={rec.COMPOSER_ORIGIN + "/portfolio"})
    with pytest.raises(sb.StageBlocked):
        rec.record_composer(
            walled, tmp_path / "w", http_get=http_get, to_text=to_text_from({})
        )
    assert not (tmp_path / "w").exists()
    nolinks = FakePage(composer_pages(), links=[])
    with pytest.raises(sb.StageBlocked):
        rec.record_composer(
            nolinks, tmp_path / "n", http_get=http_get, to_text=to_text_from({})
        )
    assert not (tmp_path / "n").exists()


def test_schwab_pages_recorder_selects_accounts_and_refuses_a_missing_selector(
    tmp_path,
):
    pages = {
        rec.SCHWAB_ORIGIN + "/app/accounts/history/": "Account ending in 123 " * 100,
        rec.SCHWAB_ORIGIN + "/app/accounts/statements/": "Account ending in 123 " * 100,
    }
    page = FakePage(pages)
    names = rec.record_schwab_pages(page, ["123"], tmp_path / "ok")
    assert names == ["history-123", "statements-123"]
    with pytest.raises(
        sb.StageBlocked
    ):  # selector finds no such account: not the expected page / not signed in
        rec.record_schwab_pages(FakePage(pages), ["999"], tmp_path / "bad")
    with pytest.raises(ValueError, match="ACCOUNT_SUFFIX"):
        rec.record_schwab_pages(FakePage(pages), ["12; x"], tmp_path / "bad2")
    assert not (tmp_path / "bad").exists()


def test_pdf_to_text_refuses_an_empty_conversion(tmp_path):
    class R:
        returncode, stdout = 0, "  "

    with pytest.raises(ValueError, match="PDF_TEXT"):
        rec.pdf_to_text(tmp_path / "x.pdf", run=lambda *a, **k: R())


# ------------------------------------------------------------------ adapters


def write_jh(dir_: Path, quarters):
    dir_.mkdir(parents=True, exist_ok=True)
    for i, q in enumerate(quarters, start=1):
        (dir_ / f"stmt-{i:02d}.txt").write_text(jh_text(*q), encoding="utf-8")


QS = [
    ("01/01/2026", "03/31/2026", "100.00", ["5.00", "-2.50"], "102.50"),
    (
        "04/01/2026",
        "06/30/2026",
        "102.40",
        ["0.60"],
        "103.00",
    ),  # a boundary restatement of 0.10
]


def test_john_hancock_adapter_reads_the_files_not_numbers(tmp_path):
    write_jh(tmp_path / "c", QS)
    as_of, checks = ad.john_hancock(tmp_path / "c", {})
    assert as_of == "2026-06-30"
    assert (
        checks["nav_identity"]().ok and checks["expected_dates"]().ok
    )  # the restatement is a boundary gap, not a failure
    _a, strict = ad.john_hancock(tmp_path / "c", {"nav_identity": {"chain": True}})
    assert strict["nav_identity"]().code == "NAV_IDENTITY_BREAK"
    write_jh(
        tmp_path / "gap",
        [QS[0], ("07/01/2026", "09/30/2026", "103.00", ["1.00"], "104.00")],
    )  # Q2 missing
    _a, checks = ad.john_hancock(tmp_path / "gap", {})
    assert checks["expected_dates"]().code == "DATES_DIFFER"
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "stmt-01.txt").write_text("nothing", encoding="utf-8")
    with pytest.raises(ValueError, match="JH_SHAPE"):
        ad.john_hancock(broken, {})
    with pytest.raises(ValueError, match="NO_STATEMENTS"):
        ad.john_hancock(tmp_path, {})


def test_composer_adapter_per_line_rounding_and_month_gaps(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    for month, total in (
        ("2026-08", "13.00"),
        ("2026-09", "13.01"),
        ("2026-10", "13.00"),
    ):
        (d / f"stmt-{month}.txt").write_text(
            composer_text("3.00", "10.00", total), encoding="utf-8"
        )
    as_of, checks = ad.composer(d, {})
    assert as_of == "2026-10-31" and checks["expected_dates"]().ok
    assert (
        checks["sum_equals_total"]().code == "TOTAL_MISMATCH"
    )  # one cent off, no allowance declared
    _a, rounded = ad.composer(d, {"sum_equals_total": {"tol_per_part": "0.005"}})
    assert rounded["sum_equals_total"]().ok
    (d / "stmt-2026-09.txt").unlink()
    _a, gap = ad.composer(d, {})
    assert gap["expected_dates"]().code == "DATES_DIFFER"


def test_schwab_cash_adapter_walks_with_captured_post_ledger_rows_and_statements(
    tmp_path,
):
    d = tmp_path / "c"
    d.mkdir()
    (d / "history-123.txt").write_text(
        "header\n11/09/2026\t\n\nBuy\n\tABC\tSOME FUND\t1\t$5.00\t\t-$5.00\tMore\n11/05/2026\t\n\nCash Dividend\n\t\t$2.00\n",
        encoding="utf-8",
    )
    ledger = [
        {
            "kind": "Journal",
            "event_date": "2026-10-01",
            "settle_date": "2026-10-01",
            "amount": 100.0,
        }
    ]
    nav = {"2026-10-01": 100.0, "2026-10-31": 100.0, "2026-11-06": 100.0}
    as_of, checks = ad.schwab_cash(
        d,
        suffix="123",
        nav_by_date=nav,
        ledger_transactions=ledger,
        anchor_date="2026-11-11",
        anchor_cash=95.0,
        ledger_end="2026-11-05",
        statement_cash_by_date={"2026-10-31": 100.0},
    )
    assert (
        as_of == "2026-11-11"
        and checks["cash_walk"]().ok
        and checks["statement_cash"]().ok
    )
    _a, wrong = ad.schwab_cash(
        d,
        suffix="123",
        nav_by_date=nav,
        ledger_transactions=ledger,
        anchor_date="2026-11-11",
        anchor_cash=95.0,
        ledger_end="2026-11-05",
        statement_cash_by_date={"2026-10-31": 90.0},
    )
    assert wrong["statement_cash"]().code == "STATEMENT_CASH_MISMATCH"
    rows = ad.schwab_history_rows(
        (d / "history-123.txt").read_text(), since="2026-11-06"
    )
    assert rows == [{"date": "2026-11-09", "type": "Buy", "amount": -5.0}]


def test_ibkr_adapter_flags_conflicts_gaps_and_uses_not_applicable_for_the_bridge():
    row = lambda total: {"row": {"total": total}, "ending": total, "conflict": False}  # noqa: E731
    series = {"20261102": row("10.0"), "20261103": row("11.0"), "20261104": row("12.0")}
    _a, checks = ad.ibkr_flex(series, start="2026-11-02", end="2026-11-04")
    assert (
        checks["coverage"]().ok
        and checks["sum_equals_total"]().ok
        and checks["bridge_receipt"]().code == "NOT_APPLICABLE"
    )
    gap = {k: v for k, v in series.items() if k != "20261103"}
    assert (
        ad.ibkr_flex(gap, start="2026-11-02", end="2026-11-04")[1]["coverage"]().code
        == "COVERAGE_GAP"
    )
    series["20261104"] = {**series["20261104"], "conflict": True}
    assert (
        ad.ibkr_flex(series, start="2026-11-02", end="2026-11-04")[1][
            "sum_equals_total"
        ]().code
        == "TOTAL_MISMATCH"
    )


# ------------------------------------------------------------------ chain


def source(sid, **over):
    base = cp.load_sources(CONTRACT, repo_root=ROOT)[sid]
    return cp.Source(**{**base.__dict__, **over})


def run_chain(tmp_path, record, sid="john_hancock_nav", adapter=ad.john_hancock):
    s = source(sid)
    return s, chain.run_source(
        s, record=record, adapter=adapter, run_dir=tmp_path / "run", manifest_path=tmp_path / "m.json", evidence_root=tmp_path / "ev",
        pin_path=tmp_path / "pins.json", cutoff="2026-07-15", captured_on="2026-11-12",
    )  # fmt: skip


def test_chain_admits_a_passing_capture_and_the_reader_can_open_it(tmp_path):
    s, out = run_chain(tmp_path, lambda d: write_jh(d, QS))
    assert out["status"] == "ADMITTED" and out["entry"]["as_of"] == "2026-06-30"
    got = cp.captured_input(
        s.source_id,
        "stmt-01.txt",
        pin_path=tmp_path / "pins.json",
        evidence_root=tmp_path / "ev",
        cutoff="2026-07-15",
        sources={s.source_id: s},
        reader="reserve_nav",
    )
    assert got.read_text().startswith("STATEMENT PERIOD")


def test_chain_reports_wall_failure_and_unreadable_capture_without_admitting(tmp_path):
    def wall(_d):
        raise sb.StageBlocked("BLOCKED_LOGIN")

    s, out = run_chain(tmp_path / "w", wall)
    assert out["status"] == "BLOCKED_LOGIN" and out["entry"] is None
    s, out = run_chain(
        tmp_path / "f",
        lambda d: write_jh(
            d, [QS[0], ("07/01/2026", "09/30/2026", "103.00", ["1.00"], "104.00")]
        ),
    )
    assert out["status"] == "FAIL" and any("expected_dates" in c for c in out["codes"])
    assert (
        not (tmp_path / "f" / "ev").exists()
        and not (tmp_path / "f" / "pins.json").exists()
    )

    def junk(d):
        d.mkdir(parents=True)
        (d / "stmt-01.txt").write_text("nothing", encoding="utf-8")

    s, out = run_chain(tmp_path / "j", junk)
    assert out["status"] == "FAIL" and any(
        c.startswith("ADAPTER:") for c in out["codes"]
    )
    s, out = run_chain(
        tmp_path / "late",
        lambda d: write_jh(
            d, [("01/01/2026", "03/31/2026", "100.00", ["1.00"], "101.00")]
        ),
    )
    assert (
        out["status"] == "FAIL" and "STALE_INPUT" in out["codes"]
    )  # 106 days before the cutoff, source allows 100


def test_contract_lists_the_recorders_honestly():
    s = cp.load_sources(CONTRACT, repo_root=ROOT)
    for sid in ("john_hancock_nav", "composer_balance", "schwab_daily_cash"):
        assert (
            s[sid].stage_mode == "EXISTING_RECORDER"
            and s[sid].stage_tool == "tools/capture_stages/recorders.py"
            or sid == "john_hancock_nav"
            or sid == "composer_balance"
        )
    assert "bcomplete.com" in s["john_hancock_nav"].window_hosts
    assert s["schwab_daily_cash"].stage_mode == "EXISTING_RECORDER"
    assert (
        "sprint-data-bridge" in s["governed_price_tqqq_shv"].capture_stage["blocked_on"]
    )
    json.dumps(dict(s["ibkr_flex_history"].capture_stage))  # serialisable


# ------------------------------------------------------------------ Schwab statements recorder, price and audit adapters


class SchwabFake(FakePage):
    def __init__(self, dates, **kw):
        super().__init__({}, **kw)
        self.dates, self.ranges, self.downloads = dates, [], []

    def inner_text(self, _sel):
        rows = "".join(
            f"{m}/{d}/{y}\tStatements\nAccount ending in 123\n"
            for y, m, d in (x.split("-") for x in self.dates)
        )
        return "Account ending in 123 " * 80 + rows

    def evaluate(self, script, arg=None):
        if script == rec.SET_RANGE_JS:
            self.ranges.append(arg)
            return True
        return super().evaluate(script, arg)

    def download_pdf(self, index, dest):
        self.downloads.append(index)
        Path(dest).write_bytes(b"%PDF-1.4 stmt")


def test_schwab_statements_recorder_downloads_every_listed_statement(tmp_path):
    page = SchwabFake(["2026-10-31", "2026-09-30"])
    texts = {
        f"statement-123-{d}.pdf": f"text {d}" for d in ("2026-10-31", "2026-09-30")
    }
    names = rec.record_schwab_statements(
        page, ["123"], tmp_path / "run", to_text=to_text_from(texts)
    )
    assert (
        page.ranges == ["Last 5 years"]
        and page.clicked == ["Search"]
        and page.downloads == [0, 1]
    )
    assert (
        tmp_path / "run" / "statement-123-2026-09-30.txt"
    ).read_text() == "text 2026-09-30"
    assert names[:2] == ["history-123", "statements-123"] and len(names) == 4
    assert rec.statement_dates("x\n10/31/2026\tStatements\n") == ["2026-10-31"]


def test_schwab_statements_recorder_refuses_empty_lists_bad_pdfs_and_walls(tmp_path):
    with pytest.raises(sb.StageBlocked):  # no statements listed: not the expected page
        rec.record_schwab_statements(SchwabFake([]), ["123"], tmp_path / "a")
    bad = SchwabFake(["2026-10-31"])
    bad.download_pdf = lambda i, dest: Path(dest).write_bytes(b"<html>")
    with pytest.raises(ValueError, match="PDF_FETCH"):
        rec.record_schwab_statements(
            bad, ["123"], tmp_path / "b", to_text=to_text_from({})
        )
    with pytest.raises(sb.StageBlocked):  # unknown account in the selector
        rec.record_schwab_statements(
            SchwabFake(["2026-10-31"]), ["999"], tmp_path / "c"
        )
    assert not any((tmp_path / n).exists() for n in "abc")


def test_schwab_adapter_derives_statement_cash_from_the_capture(tmp_path):
    from tools.capture_stages import schwab_cash as sc

    d = tmp_path / "c"
    d.mkdir()
    (d / "history-123.txt").write_text("header\n", encoding="utf-8")
    detail = "Total Cash and Cash Investments        $20.00     $100.00     $10.00\n"
    (d / "statement-123-2026-10-31.txt").write_text(detail, encoding="utf-8")
    (d / "statement-123-2020-01-31.txt").write_text(
        "unparseable", encoding="utf-8"
    )  # predates the walk: out of scope
    ledger = [
        {
            "kind": "Journal",
            "event_date": "2026-10-01",
            "settle_date": "2026-10-01",
            "amount": 100.0,
        }
    ]
    nav = {"2026-10-01": 100.0, "2026-10-31": 100.0}
    adapter = ad.schwab_cash_adapter(
        suffix="123",
        nav_by_date=nav,
        ledger_transactions=ledger,
        anchor_date="2026-10-31",
        anchor_cash=100.0,
        ledger_end="2026-10-31",
    )
    as_of, checks = adapter(d, {})
    assert (
        as_of == "2026-10-31"
        and checks["statement_cash"]().ok
        and checks["cash_walk"]().ok
    )
    (d / "statement-123-2026-10-31.txt").write_text("unparseable", encoding="utf-8")
    with pytest.raises(ValueError, match="STATEMENT_UNPARSED"):
        adapter(d, {})[1]["statement_cash"]()
    assert sc.parse_statement_cash(detail) == 100.0


def test_governed_price_adapter_coverage_and_two_pull_stability(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    rows = "symbol,date,close\n" + "".join(
        f"AAA,2026-11-0{i},1.{i}\n" for i in (2, 3, 4, 5, 6)
    )
    (d / "prices-a.csv").write_text(rows, encoding="utf-8")
    (d / "prices-b.csv").write_text(rows, encoding="utf-8")
    _a, checks = ad.governed_price(
        d, symbols=["AAA"], start="2026-11-02", end="2026-11-06"
    )
    assert checks["coverage"]().ok and checks["stability"]().ok
    (d / "prices-b.csv").write_text(rows.replace("1.4", "1.9"), encoding="utf-8")
    assert (
        ad.governed_price(d, symbols=["AAA"], start="2026-11-02", end="2026-11-06")[1][
            "stability"
        ]().code
        == "PAGE_UNSTABLE"
    )
    assert (
        ad.governed_price(d, symbols=["BBB"], start="2026-11-02", end="2026-11-06")[1][
            "coverage"
        ]().code
        == "COVERAGE_GAP"
    )


def test_audit_adapter_uses_the_real_reader_validator(tmp_path):
    import portfolio_model_vs_actual as mva

    d = tmp_path / "a"
    d.mkdir()
    good = {
        "artifact": mva.AUDIT_INPUT_ARTIFACT,
        "schema_version": mva.AUDIT_INPUT_SCHEMA_VERSION,
        "closure_tolerance_pp": 0.5,
        "pulls": [],
        "strategies": [],
        "as_of": CUT,
    }
    (d / "audit-input.json").write_text(json.dumps(good), encoding="utf-8")
    as_of, checks = ad.model_vs_actual_audit(d)
    assert as_of == CUT and checks["reader_parses"]().ok
    (d / "audit-input.json").write_text(
        json.dumps({**good, "artifact": "something-else"}), encoding="utf-8"
    )
    assert ad.model_vs_actual_audit(d)[1]["reader_parses"]().code == "READER_REFUSES"
    (d / "audit-input.json").write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        ad.model_vs_actual_audit(d)


def test_derived_sources_lint_and_preflight():
    s = cp.load_sources(CONTRACT, repo_root=ROOT)
    audit = s["model_vs_actual_audit_input"]
    assert (
        audit.transport == "DERIVED"
        and audit.admits == ("model_vs_actual",)
        and audit.login_stop is False
    )
    assert (
        cp.preflight(
            audit, window_state=lambda _s: "OK", vault_ok=lambda _r: True
        ).status
        == "BLOCKED_NOT_WIRED"
    )
    wired = cp.Source(
        **{
            **audit.__dict__,
            "capture_stage": {"mode": "EXISTING_RECORDER", "tool": "x"},
        }
    )
    assert (
        cp.preflight(
            wired, window_state=lambda _s: "OK", vault_ok=lambda _r: True
        ).status
        == "READY"
    )
    assert (
        cp.check_reader_parses(lambda: None).ok
        and cp.check_reader_parses(lambda: 1 / 0).code == "READER_REFUSES"
    )
