"""Admission of the transactions HTML list (``/transact_list.jsp``).

All HTML is generated here from invented values in the REAL structure seen in eight
preserved pages: a pager row (position text, total count, page links, page-size
selector, download link), a ``rowTwo`` header row, and data rows of 11 cells.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from p123_capture_fixtures import (
    DESIGNER,
    EXPIRES,
    NOW,
    ORIGIN,
    _HEADER,
    entry,
    write_har,
)
from quant_engine.portfolio123 import capture_catalog as catalog
from quant_engine.portfolio123.capture_catalog import CaptureRefusal
from quant_engine.portfolio123.capture_html import (
    HEADER,
    HtmlRefusal,
    parse_transactions_page,
)
from quant_engine.portfolio123.capture_intake import (
    StageRequest,
    inventory_har,
    stage_run,
    verify_run,
)
from quant_engine.portfolio123.capture_shapes import ShapeError, validate_shape

STRATEGY = "9006"
OTHER = "9007"
SCOPE = ("transactions.account",)
TEXT_MARK = "Synthetic note"


def _row(seq: int, strategy: str, kind: str = "BUY") -> str:
    empty = kind in {"DIV", "SPLIT"}
    shares = "" if empty else "-1,234.50"
    return (
        '<tr class="rowAlt1 hl" align="left">'
        f'<td style="text-align:left;width:30px">{seq}</td>'
        f'<td style="text-align:left;width:60px"><a href="holdings_history.jsp?'
        f'portid={strategy}&date=5/16/2022">05/16/22</a></td>'
        f'<td style="text-align:left;width:50px"><span class="txt-b txt-buy">{kind}</span></td>'
        '<td style="text-align:left"><div class="ticker-box">'
        '<span class="ticker-box-ticker ticker-box-trigger">AAAA^B'
        '<small class="text-muted">:USA</small></span>'
        "<ticker-chart>3M</ticker-chart><ticker-chart>1Y</ticker-chart>"
        '<span class="ticker-box-all ticker-box-all-hidden">'
        '<span class="txt-bigger">x</span></span></div></td>'
        f'<td style="text-align:right">{shares}</td>'
        f'<td style="text-align:right">{"" if empty else "12.50"}</td>'
        '<td style="text-align:right"><span class="txt-red">1,000,000.25</span></td>'
        f'<td style="text-align:right">{"" if empty else "1.00"}</td>'
        f'<td style="text-align:right">{"" if empty else "55.50"}</td>'
        f'<td style="text-align:right">{TEXT_MARK}</td>'
        '<td style="text-align:right"></td></tr>'
    )


def page_html(
    offset: int = 0,
    total: int = 120,
    *,
    strategy: str = STRATEGY,
    rows: int | None = None,
    first_seq: int | None = None,
    kinds: tuple[str, ...] = ("BUY", "SELL", "DIV", "SPLIT"),
) -> str:
    last = min(offset + 50, total)
    count = last - offset if rows is None else rows
    start = offset * 10 + 1 if first_seq is None else first_seq
    body = "".join(
        _row(start + i, strategy, kinds[i % len(kinds)]) for i in range(count)
    )
    last_offset = ((total - 1) // 50) * 50
    base = f"/transact_list.jsp?portid={strategy}"
    head = "".join(f"<th>{label}</th>" for label in HEADER)
    return (
        '<html><head><script>var s = "<table>";</script></head><body>'
        "<table class=\"TableBase TableData\" align='center'><thead>"
        "<tr><th colspan='12' class='clearfix tbl-header'>"
        '<div class="form-group form-inline pagination-box">'
        f'<span class="curPos">{offset + 1} - {last} of '
        f'<span id="resulttotalcnt" rel="1">{total}</span></span>'
        '<ul class="pagination">'
        f'<li><a href="{base}" title="First Page">&laquo;</a></li>'
        f'<li><a href="{base}&offset={max(offset - 50, 0)}" title="Previous Page">&lsaquo;</a></li>'
        f'<li><a href="{base}&offset={offset + 50}" title="Next Page">&rsaquo;</a></li>'
        f'<li><a href="{base}&offset={last_offset}" title="Last Page">&raquo;</a></li>'
        "</ul>"
        '<select class="form-control" id="rowspp" name="rowspp">'
        '<option value="20">20 pp</option><option value="50" selected>50 pp</option>'
        '<option value="100">100 pp</option><option value="500">500 pp</option>'
        '<option value="0">All</option></select>'
        f'<a href="/p123/DownloadPortTrans?portid={strategy}" class="btn" '
        'title="Download"><span class="glyphicon glyphicon-save"></span></a>'
        "</div></th></tr>"
        f'<tr class="rowTwo">{head}</tr></thead><tbody>{body}</tbody></table>'
        "</body></html>"
    )


def parse(html: str, *, strategy: str = STRATEGY, offset: int = 0) -> Any:
    return parse_transactions_page(html.encode(), strategy=strategy, offset=offset)


def _code(html: str, **kwargs: Any) -> str:
    with pytest.raises(HtmlRefusal) as caught:
        parse(html, **kwargs)
    return caught.value.code


def html_entry(
    offset: int | None,
    html: str,
    *,
    strategy: str = STRATEGY,
    status: int = 200,
    method: str = "GET",
    signed_in: bool = True,
    mime: str = "text/html; charset=utf-8",
    base64_body: bool = False,
    query: str | None = None,
) -> dict[str, Any]:
    if query is None:
        query = f"portid={strategy}" + ("" if offset is None else f"&offset={offset}")
    item = entry(
        f"{ORIGIN}/transact_list.jsp?{query}", None, status=status, method=method
    )
    content = item["response"]["content"]
    content["mimeType"] = mime
    if base64_body:
        content["encoding"] = "base64"
        content["text"] = base64.b64encode(html.encode()).decode()
    else:
        content["text"] = html
    if not signed_in:
        item["request"]["headers"] = []
    return item


def pages(
    total: int = 120, strategy: str = STRATEGY, skip: tuple[int, ...] = ()
) -> list[dict[str, Any]]:
    return [
        html_entry(
            None if o == 0 else o,
            page_html(o, total, strategy=strategy),
            strategy=strategy,
        )
        for o in range(0, total, 50)
        if o not in skip
    ]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "staging"
    path.mkdir()
    return path


def request(
    root: Path,
    tmp_path: Path,
    entries: list[dict[str, Any]],
    scope: tuple[str, ...] = SCOPE,
    run_id: str = "html-run",
    **kwargs: Any,
) -> StageRequest:
    source = write_har(tmp_path / f"{run_id}.har", entries)
    return StageRequest(root, run_id, "har", source, scope, NOW, EXPIRES, **kwargs)


def refusal(req: StageRequest) -> str:
    with pytest.raises(CaptureRefusal) as caught:
        stage_run(req)
    return caught.value.code


# --- parser: what it extracts ----------------------------------------------


def test_page_parses_to_normalized_rows_without_identifiers() -> None:
    page = parse(page_html(0, 120))
    assert (page.first, page.last, page.total, len(page.rows)) == (1, 50, 120, 50)
    buy, sell, dividend, split = page.rows[:4]
    assert buy == {
        "date": "2022-05-16",
        "type": "BUY",
        "ticker": "AAAA^B",
        "market": "USA",
        "shares": -1234.5,
        "price": 12.5,
        "amount": 1000000.25,
        "tot_fees": 1.0,
        "rank": 55.5,
        "note": TEXT_MARK,
    }
    assert sell["type"] == "SELL"
    for empty in (dividend, split):
        assert [empty[k] for k in ("shares", "price", "tot_fees", "rank")] == [None] * 4
    assert len(page.sequences) == 50
    flat = json.dumps(page.rows)
    assert STRATEGY not in flat and page.sequences[0] not in flat.split('"')
    assert "holdings_history" not in flat and "transact_list" not in flat


def test_two_digit_years_pivot_and_last_page_is_short_but_valid() -> None:
    html = (
        page_html(100, 120)
        .replace("05/16/22", "05/16/99")
        .replace("date=5/16/2022", "date=5/16/1999")
    )
    page = parse(html, offset=100)
    assert (page.first, page.last, len(page.rows)) == (101, 120, 20)
    assert page.rows[0]["date"] == "1999-05-16"


def test_normalized_page_matches_the_catalog_shape() -> None:
    page = parse(page_html(0, 120))
    shape = catalog.route_for("transactions.account").shape
    assert shape is not None
    body = {
        "strategy_sha256": "0" * 64,
        "offset": 0,
        "first": page.first,
        "last": page.last,
        "total": page.total,
        "rows": list(page.rows),
    }
    validate_shape(body, shape)
    for bad in (
        {**body, "rows": []},
        {**body, "rows": [{**body["rows"][0], "type": "OTHER"}]},
        {**body, "extra": 1},
    ):
        with pytest.raises(ShapeError):
            validate_shape(bad, shape)


# --- parser: every refusal -------------------------------------------------


def _mutated(old: str, new: str, count: int = 1, **kwargs: Any) -> str:
    html = page_html(**kwargs)
    assert old in html, old
    return html.replace(old, new, count)


@pytest.mark.parametrize(
    ("html", "code"),
    [
        ("<html><body>nothing</body></html>", "TABLE_NOT_FOUND"),
        (
            page_html().replace("</body>", page_html().split("<body>")[1]),
            "TABLE_AMBIGUOUS",
        ),
        (_mutated("</td></tr>", "</tr>"), "TABLE_MALFORMED"),
        (
            _mutated("</tr></thead>", "</tr></thead><tfoot></tfoot>", 1),
            "TABLE_STRUCTURE",
        ),
        (_mutated("<thead>", "<thead><tr></tr>"), "TABLE_STRUCTURE"),
        (_mutated("<th>Tot Fees</th>", "<th>Fees</th>"), "HEADER_UNREVIEWED"),
        (_mutated("<th>TRADE</th>", ""), "HEADER_UNREVIEWED"),
        (_mutated('<tr class="rowTwo">', "<tr>"), "HEADER_UNREVIEWED"),
        (
            _mutated('<span id="resulttotalcnt" rel="1">120</span>', ""),
            "PAGER_UNREVIEWED",
        ),
        (_mutated("1 - 50 of", "1 - 50 off"), "PAGER_UNREVIEWED"),
        (_mutated("colspan='12'", "colspan='9'"), "PAGER_UNREVIEWED"),
        (_mutated('title="Next Page"', 'title="Jump"'), "PAGER_UNREVIEWED"),
        (
            _mutated('offset=100" title="Last Page', 'offset=50" title="Last Page'),
            "PAGER_INCONSISTENT",
        ),
        (
            _mutated('<option value="50" selected>', '<option value="50">'),
            None,
        ),
        (
            _mutated(
                '<option value="50" selected>50 pp</option><option value="100">',
                '<option value="50">50 pp</option><option value="100" selected>',
            ),
            "ROWS_PER_PAGE_UNREVIEWED",
        ),
        (
            _mutated(f"portid={STRATEGY}&date", f"portid={OTHER}&date"),
            "LINK_STRATEGY_MISMATCH",
        ),
        (
            _mutated(
                f'href="/p123/DownloadPortTrans?portid={STRATEGY}"',
                f'href="/p123/DownloadPortTrans?portid={OTHER}"',
            ),
            "LINK_STRATEGY_MISMATCH",
        ),
        (_mutated("date=5/16/2022", "date=5/17/2022"), "DATE_LINK_MISMATCH"),
        (
            _mutated("05/16/22", "13/45/22", 1).replace(
                "date=5/16/2022", "date=13/45/2022"
            ),
            "DATE_UNREVIEWED",
        ),
        (_mutated("holdings_history.jsp", "other.jsp"), "LINK_UNREVIEWED"),
        (_mutated(">BUY<", ">SHORT<"), "TYPE_UNREVIEWED"),
        (_mutated('<span class="txt-b txt-buy">BUY</span>', "BUY"), "CELL_STRUCTURE"),
        (_mutated("12.50", "1e5"), "NUMBER_UNREVIEWED"),
        (_mutated("12.50", "12,50"), "NUMBER_UNREVIEWED"),
        (_mutated("12.50", "(12.50)"), "NUMBER_UNREVIEWED"),
        (_mutated("12.50", "<b>12.50</b>"), "CELL_STRUCTURE"),
        (_mutated(":USA", "USA"), "TICKER_UNREVIEWED"),
        (_mutated("AAAA^B", "AA AA"), "TICKER_UNREVIEWED"),
        (
            _mutated('<div class="ticker-box">', '<div class="other-box">'),
            "CELL_STRUCTURE",
        ),
        (
            _mutated('<td style="text-align:right"></td></tr>', "<td>BUY</td></tr>"),
            "TRADE_CELL_UNREVIEWED",
        ),
        (
            _mutated('<td style="text-align:right"></td></tr>', "</tr>"),
            "ROW_CELL_COUNT",
        ),
        (
            _mutated(
                '<td style="text-align:right"></td></tr>', "<td></td><td></td></tr>"
            ),
            "ROW_CELL_COUNT",
        ),
        (
            _mutated('<td style="text-align:left;width:30px">1</td>', "<td>x1</td>"),
            "ROW_SEQUENCE_UNREVIEWED",
        ),
        (_mutated(TEXT_MARK, "x" * 201), "NOTE_TOO_LONG"),
        (_mutated(TEXT_MARK, "<i>note</i>"), "CELL_STRUCTURE"),
    ],
    ids=lambda v: "html" if isinstance(v, str) and len(v) > 80 else str(v),
)
def test_unexpected_structure_refuses(html: str, code: str | None) -> None:
    if code is None:
        # no selected option at all is allowed: row counts still bind the size
        assert parse(html).total == 120
        return
    assert _code(html) == code


def test_declared_range_rules() -> None:
    assert _code(page_html(0, 120), offset=50) == "OFFSET_MISMATCH"
    assert _code(page_html(0, 120, rows=49)) == "SHORT_PAGE_NOT_LAST"
    assert _code(page_html(0, 120, rows=51)) == "ROWS_AFTER_DECLARED_END"
    assert _code(page_html(100, 120, rows=19), offset=100) == "SHORT_PAGE_NOT_LAST"
    assert _code(page_html(100, 120, rows=21), offset=100) == "ROWS_AFTER_DECLARED_END"
    assert _code(page_html(0, 120, rows=0)) == "EMPTY_PAGE"
    short = page_html(0, 30).replace("1 - 30 of", "1 - 20 of")
    assert _code(short) in {"PAGER_INCONSISTENT", "ROWS_AFTER_DECLARED_END"}
    assert _code(page_html(0, 0, rows=0)) in {"PAGER_INCONSISTENT", "EMPTY_PAGE"}


def test_duplicate_rows_and_bad_input_refuse() -> None:
    row = _row(7, STRATEGY)
    html = page_html(0, 2, rows=0).replace("<tbody>", "<tbody>" + row + row)
    assert _code(html) == "DUPLICATE_ROWS"
    with pytest.raises(HtmlRefusal, match="HTML_NOT_UTF8"):
        parse_transactions_page(b"\xff\xfe", strategy=STRATEGY, offset=0)
    for strategy, offset in (("abc", 0), ("", 0), (STRATEGY, -1)):
        with pytest.raises(HtmlRefusal, match="REQUEST_UNREVIEWED"):
            parse_transactions_page(b"x", strategy=strategy, offset=offset)
    with pytest.raises(HtmlRefusal, match="HTML_TOO_LARGE"):
        parse_transactions_page(
            b"x" * (16 * 1024 * 1024 + 1), strategy=STRATEGY, offset=0
        )


def test_refusal_messages_carry_a_code_and_never_page_content() -> None:
    with pytest.raises(HtmlRefusal) as caught:
        parse(_mutated(">BUY<", ">" + TEXT_MARK + "<"))
    assert TEXT_MARK not in str(caught.value) and STRATEGY not in str(caught.value)


# --- catalog and request matching ------------------------------------------


def test_html_route_is_outside_the_json_contract() -> None:
    route = catalog.route_for("transactions.account")
    assert route.html is not None and route.verdict == "CONFIRMED"
    assert route not in catalog.har_routes()
    assert route in catalog.html_routes()
    with pytest.raises(Exception):
        catalog.capture_contract().route(
            "GET", f"{ORIGIN}/transact_list.jsp?portid={STRATEGY}&offset=50"
        )
    assert catalog.evidence_status(["transactions.account"]) == "REVIEWED_SHAPES"
    assert (
        catalog.evidence_status(["transactions.account", "aifactor.model"])
        == "UNVERIFIED_SYNTHETIC_SHAPES"
    )


@pytest.mark.parametrize(
    ("method", "url", "expected"),
    [
        ("GET", f"{ORIGIN}/transact_list.jsp?portid=9006", (STRATEGY, 0)),
        ("GET", f"{ORIGIN}/transact_list.jsp?portid=9006&offset=0", (STRATEGY, 0)),
        ("get", f"{ORIGIN}/transact_list.jsp?offset=150&portid=9006", (STRATEGY, 150)),
        ("POST", f"{ORIGIN}/transact_list.jsp?portid=9006", None),
        ("GET", f"{ORIGIN}/transact_list.jsp", None),
        ("GET", f"{ORIGIN}/transact_list.jsp?offset=50", None),
        ("GET", f"{ORIGIN}/transact_list.jsp?portid=9006&pp=100", None),
        ("GET", f"{ORIGIN}/transact_list.jsp?portid=9006&portid=9007", None),
        ("GET", f"{ORIGIN}/transact_list.jsp?portid=0", None),
        ("GET", f"{ORIGIN}/transact_list.jsp?portid=x", None),
        ("GET", f"{ORIGIN}/transact_list.jsp?portid=9006&offset=-1", None),
        ("GET", f"{ORIGIN}/transact_list.jsp?portid=9006&offset=1234567890", None),
        ("GET", f"{ORIGIN}/transact_list.jsp?portid=9006#frag", None),
        ("GET", f"{ORIGIN}/transact_list2.jsp?portid=9006", None),
        ("GET", "https://evil.example/transact_list.jsp?portid=9006", None),
        ("GET", f"{ORIGIN}:8443/transact_list.jsp?portid=9006", None),
    ],
)
def test_request_matching(method: str, url: str, expected: Any) -> None:
    matched = catalog.match_html_route(method, url)
    if expected is None:
        assert matched is None
    else:
        assert matched is not None and matched[1:] == expected


# --- staging: HAR run, completeness, forgery -------------------------------


def test_complete_run_stages_verifies_and_is_reviewed(
    root: Path, tmp_path: Path
) -> None:
    summary = stage_run(request(root, tmp_path, pages()))
    info = summary.capabilities["transactions.account"]
    assert info == {
        "records": 3,
        "pages_seen": 3,
        "page_count": 3,
        "strategies": 1,
        "rows": 120,
        "coverage": "COMPLETE",
    }
    assert summary.complete
    verified = verify_run(root, "html-run")
    assert verified.manifest_sha256 == summary.manifest_sha256
    run = root / "p123-capture" / "html-run"
    manifest = json.loads((run / "manifest.json").read_text("utf-8"))
    assert manifest["evidence_status"] == "REVIEWED_SHAPES"
    assert manifest["scope"] == list(SCOPE) and manifest["restricted"] is True
    normalized = (run / "normalized" / "records.json").read_bytes().decode()
    records = json.loads(normalized)["records"]
    assert [r["response"]["offset"] for r in records] == sorted(
        r["response"]["offset"] for r in records
    )
    assert sum(len(r["response"]["rows"]) for r in records) == 120
    for forbidden in (
        STRATEGY,
        "holdings_history",
        "transact_list",
        "portid",
        "Cookie",
    ):
        assert forbidden not in normalized
    assert len({r["response"]["strategy_sha256"] for r in records}) == 1


def test_first_page_may_omit_offset_and_pages_may_arrive_in_any_order(
    root: Path, tmp_path: Path
) -> None:
    entries = list(reversed(pages()))
    assert "offset" not in entries[-1]["request"]["url"]
    assert stage_run(request(root, tmp_path, entries)).complete


def test_identical_duplicate_pages_dedupe_and_conflicting_ones_refuse(
    root: Path, tmp_path: Path
) -> None:
    doubled = pages() + [html_entry(0, page_html(0, 120))]
    summary = stage_run(request(root, tmp_path, doubled))
    assert summary.capabilities["transactions.account"]["records"] == 3
    changed = page_html(0, 120).replace("12.50", "12.75", 1)
    conflict = pages() + [html_entry(0, changed)]
    req = request(root, tmp_path, conflict, run_id="run-two")
    assert refusal(req) == "CONFLICTING_DUPLICATE"


def test_gaps_are_partial_and_never_reviewed_complete(
    root: Path, tmp_path: Path
) -> None:
    gap = pages(skip=(50,))
    assert refusal(request(root, tmp_path, gap)) == "COVERAGE_PARTIAL"
    partial = stage_run(
        request(root, tmp_path, gap, run_id="run-two", allow_partial=True)
    )
    assert not partial.complete
    assert partial.capabilities["transactions.account"]["coverage"] == "PARTIAL"
    with pytest.raises(CaptureRefusal) as caught:
        verify_run(root, "run-two")
    assert caught.value.code == "COVERAGE_PARTIAL"
    assert not verify_run(root, "run-two", require_complete=False).complete
    # a run missing the last page, and one missing the first, are partial too
    for skip, run_id in (((100,), "run-three"), ((0,), "run-four")):
        assert (
            refusal(request(root, tmp_path, pages(skip=skip), run_id=run_id))
            == "COVERAGE_PARTIAL"
        )


def test_inconsistent_totals_and_overlapping_rows_refuse(
    root: Path, tmp_path: Path
) -> None:
    mixed = [
        html_entry(None, page_html(0, 120)),
        html_entry(50, page_html(50, 130)),
    ]
    assert refusal(request(root, tmp_path, mixed)) == "PAGINATION_INCONSISTENT"
    overlap = [
        html_entry(None, page_html(0, 120)),
        html_entry(50, page_html(50, 120, first_seq=1)),
        html_entry(100, page_html(100, 120)),
    ]
    req = request(root, tmp_path, overlap, run_id="run-two")
    assert refusal(req) == "DUPLICATE_ROWS"


def test_offset_beyond_the_declared_end_or_off_step_refuses(
    root: Path, tmp_path: Path
) -> None:
    beyond = pages() + [html_entry(150, page_html(100, 120))]
    assert refusal(request(root, tmp_path, beyond)) == "OFFSET_MISMATCH"
    offstep = [html_entry(25, page_html(0, 120))]
    req = request(root, tmp_path, offstep, run_id="run-two")
    assert refusal(req) == "OFFSET_MISMATCH"


def test_two_strategies_are_checked_independently(root: Path, tmp_path: Path) -> None:
    both = pages(120) + pages(70, OTHER)
    summary = stage_run(request(root, tmp_path, both))
    info = summary.capabilities["transactions.account"]
    assert (info["strategies"], info["rows"], info["page_count"]) == (2, 190, 5)
    broken = pages(120) + pages(70, OTHER, skip=(50,))
    req = request(root, tmp_path, broken, run_id="run-two")
    assert refusal(req) == "COVERAGE_PARTIAL"


def test_entry_gates_status_session_mime_route(root: Path, tmp_path: Path) -> None:
    def code(entries: list[dict[str, Any]], run_id: str) -> str:
        return refusal(request(root, tmp_path, entries, run_id=run_id))

    ok = page_html(0, 30)
    assert code([html_entry(None, ok, status=401)], "r-status") == "HAR_ENTRY_REJECTED"
    assert (
        code([html_entry(None, ok, signed_in=False)], "r-signed_in")
        == "HAR_ENTRY_REJECTED"
    )
    assert (
        code([html_entry(None, ok, mime="application/json")], "r-mime")
        == "HAR_ENTRY_REJECTED"
    )
    login = "<html><body>Please sign in</body></html>"
    assert code([html_entry(None, login)], "r-login") == "TABLE_NOT_FOUND"
    two = html_entry(None, ok)
    two["request"]["headers"] = two["request"]["headers"] * 2
    assert code([two], "r-dup") == "HAR_ENTRY_REJECTED"
    short = html_entry(None, ok)
    short["request"]["headers"] = [{"name": _HEADER, "value": "short"}]
    assert code([short], "r-short") == "HAR_ENTRY_REJECTED"
    stale = request(root, tmp_path, pages(30), run_id="r-expired")
    expired = StageRequest(**{**stale.__dict__, "session_expires_at": NOW})
    assert refusal(expired) == "HAR_ENTRY_REJECTED"
    # unreviewed requests are only inventoried, never parsed
    for entries in (
        [html_entry(None, ok, method="POST")],
        [html_entry(None, ok, query=f"portid={STRATEGY}&pp=100")],
        [html_entry(None, ok, query="offset=0")],
    ):
        assert code(entries, f"r-un{len(entries)}{id(entries)}") == "NO_REVIEWED_ROUTES"


def test_base64_bodies_are_accepted(root: Path, tmp_path: Path) -> None:
    entries = [html_entry(None, page_html(0, 30), base64_body=True)]
    assert stage_run(request(root, tmp_path, entries)).complete
    bad = html_entry(None, "x")
    bad["response"]["content"].update({"encoding": "base64", "text": "***"})
    assert (
        refusal(request(root, tmp_path, [bad], run_id="r-b64")) == "HAR_ENTRY_REJECTED"
    )


def test_html_and_json_capabilities_stage_together(root: Path, tmp_path: Path) -> None:
    designer = entry(f"{ORIGIN}/spr/strategy/model/9005?dm=true", DESIGNER)
    scope = ("designer-model.json", "transactions.account")
    summary = stage_run(request(root, tmp_path, [designer, *pages(30)], scope))
    assert summary.records == 1 + 1
    manifest = json.loads(
        (root / "p123-capture" / "html-run" / "manifest.json").read_text("utf-8")
    )
    assert manifest["evidence_status"] == "REVIEWED_SHAPES"
    assert (
        refusal(request(root, tmp_path, pages(30), scope, run_id="r-two"))
        == "SCOPE_MISMATCH"
    )


def test_inventory_names_the_html_route_instead_of_redacting_it(
    root: Path, tmp_path: Path
) -> None:
    source = write_har(
        tmp_path / "inv.har", pages(30) + [entry(f"{ORIGIN}/x/y", None, raw_text="n")]
    )
    inventory_har(root, "inv", source)
    text = (root / "p123-capture-inventory" / "inv.json").read_text("utf-8")
    assert "/transact_list.jsp" in text
    assert STRATEGY not in text


def test_verify_detects_a_tampered_normalized_page(root: Path, tmp_path: Path) -> None:
    stage_run(request(root, tmp_path, pages(30)))
    run = root / "p123-capture" / "html-run"
    target = run / "normalized" / "records.json"
    target.write_bytes(target.read_bytes().replace(b'"BUY"', b'"SELL"', 1))
    with pytest.raises(CaptureRefusal) as caught:
        verify_run(root, "html-run")
    assert caught.value.code == "FILE_HASH_MISMATCH"


def test_forged_manifest_with_rebound_hashes_fails_rederivation(
    root: Path, tmp_path: Path
) -> None:
    from quant_engine.portfolio123.capture_shapes import canonical_bytes
    import hashlib

    stage_run(request(root, tmp_path, pages(30)))
    run = root / "p123-capture" / "html-run"
    target = run / "normalized" / "records.json"
    forged = target.read_bytes().replace(b'"BUY"', b'"SELL"', 1)
    target.write_bytes(forged)
    manifest = json.loads((run / "manifest.json").read_text("utf-8"))
    for item in manifest["files"]:
        if item["path"] == "normalized/records.json":
            item["sha256"] = hashlib.sha256(forged).hexdigest()
            item["bytes"] = len(forged)
    (run / "manifest.json").write_bytes(canonical_bytes(manifest))
    with pytest.raises(CaptureRefusal) as caught:
        verify_run(root, "html-run")
    assert caught.value.code == "REDERIVATION_MISMATCH"
