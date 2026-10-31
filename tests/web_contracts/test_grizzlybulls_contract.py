from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path

import pytest

from quant_engine.web_contracts import WebContractError
from quant_engine.web_contracts.grizzlybulls import (
    DENIED_PATHS,
    GRIZZLYBULLS_CONTRACT,
    ORIGIN,
    classify,
    parse_historical,
    parse_live_trades_csv,
    parse_model_page,
    parse_models_catalog,
    parse_models_index,
    parse_trade_history_csv,
    sanitization_audit,
    summarize_capability,
)

HISTORY_HEADER = "Date (ET),Action,SPX,Prev Price,Profit / Loss,PnL %,Model,Buy & Hold"
Rows = list[tuple[str, str, str]]


def history_csv(rows: Rows, *, bom: bool = True) -> bytes:
    lines = [HISTORY_HEADER] + [f"{d},{a},{p},,,,1,1" for d, a, p in rows]
    return (("﻿" if bom else "") + "\r\n".join(lines) + "\r\n").encode()


ROWS: Rows = [
    ("2030-01-02 11:00 ET", "Sell", "100.5"),
    ("2030-01-03 07:00 ET", "Rebuy", "101"),
    ("2030-01-09 14:00 ET", "Sell", "102.25"),
    ("2030-01-10 07:00 ET", "Rebuy", "103"),
]


def page(
    rows: list[list[str]], extra: dict[str, object] | None = None
) -> dict[str, object]:
    props: dict[str, object] = {
        "key": "synthetic",
        "doc": "synthetic doc",
        "summaryData": [
            ["Summary", "Value", "Notes"],
            [],
            ["Row:", "1"],
            ["Row:", "1", "n"],
        ],
        "sheetData": [["Date (EST)", *HISTORY_HEADER.split(",")[1:]], *rows],
    }
    props.update(extra or {})
    return {"pageProps": props, "__N_SSG": True}


def sheet_rows() -> list[list[str]]:
    return [
        ["1/2/2030 11:00 AM", "Sell", "100.5", "", "", "", "1", "1"],
        ["1/3/2030 7:00 AM", "Rebuy", "101", "", "", "", "1", "1"],
        ["1/9/2030 3:00 PM", "Hypothetical Sell", "102", "", "", "", "1", "1"],
    ]


def test_trade_history_parses_bom_crlf_and_summarizes() -> None:
    series = parse_trade_history_csv(history_csv(ROWS))
    assert [e.action for e in series.events] == ["Sell", "Rebuy", "Sell", "Rebuy"]
    assert summarize_capability("models.history", history_csv(ROWS)) == {
        "actions": {"Rebuy": 2, "Sell": 2},
        "first": "2030-01-02T11:00:00",
        "kind": "trade-history",
        "last": "2030-01-10T07:00:00",
        "row_count": 4,
    }


def test_live_trades_accepts_both_us_timestamp_styles() -> None:
    raw = (
        "Date (New York time),Trade,ES Price\r\n"
        "9/2/2030 7:00 PM,Sell,10\r\n"
        '"9/3/2030, 12:00 AM",Rebuy,11\r\n'
    ).encode()
    series = parse_live_trades_csv(raw)
    assert series.events[0].moment.hour == 19
    assert series.events[1].moment.hour == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r[:1] + [("2030-01-03 07:00 ET", "Hold", "1")],
        lambda r: [r[1], r[0]],
        lambda r: [r[0], r[0]],
        lambda r: [("2030-02-30 07:00 ET", "Sell", "1")],
        lambda r: [("2030-01-02 07:00 EST", "Sell", "1")],
        lambda r: [("2030-01-02 07:00 ET", "Sell", "-3")],
        lambda r: [("2030-01-02 07:00 ET", "Sell", "abc")],
        lambda r: [],
        lambda r: [("2030-01-02 07:00 ET", "Hypothetical Sell", "1")] + r[1:2],
    ],
)
def test_trade_history_fails_closed_on_drift(
    mutate: Callable[[Rows], Rows],
) -> None:
    with pytest.raises(WebContractError):
        parse_trade_history_csv(history_csv(mutate(ROWS)))


def test_trade_history_rejects_header_and_width_drift() -> None:
    with pytest.raises(WebContractError):
        parse_trade_history_csv(history_csv(ROWS).replace(b"Action", b"Trade"))
    with pytest.raises(WebContractError):
        parse_trade_history_csv(history_csv(ROWS) + b"2030-01-11 07:00 ET,Sell\r\n")
    with pytest.raises(WebContractError):
        parse_trade_history_csv(b"\xff\xfe")


def test_signed_in_page_ships_numeric_cells_and_slug() -> None:
    rows: list[list[object]] = [
        ["1/2/2030 11:00 AM", "Sell", 100.5, 100.5, 0, 0, 1, 1],
        ["1/3/2030, 7:00 AM", "Rebuy", 101, 100.5, "", "", 1, 1],
    ]
    body = page([])  # type: ignore[arg-type]
    props = body["pageProps"]
    assert isinstance(props, dict)
    del props["key"]
    props["slug"] = "synthetic"
    props["sheetData"] = [props["sheetData"][0], *rows]
    series = parse_model_page(body)
    assert [e.action for e in series.events] == ["Sell", "Rebuy"]
    props["sheetData"][1][2] = True
    with pytest.raises(WebContractError):
        parse_model_page(body)


def test_other_site_pages_are_not_routed() -> None:
    for name in ("stocks", "premium", "blog", "auth", "models/a-model"):
        url = f"{ORIGIN}/_next/data/BUILD/{name}.json"
        assert classify("GET", url, GRIZZLYBULLS_CONTRACT)[0] == "unrouted"


def test_model_page_and_hypothetical_tail() -> None:
    series = parse_model_page(page(sheet_rows()))
    assert series.events[-1].action == "Hypothetical Sell"
    body = json.dumps(page(sheet_rows())).encode()
    assert summarize_capability("models.page", body)["row_count"] == 3


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"pageProps": {}},
        page(sheet_rows(), {"summaryData": [["Bad"]]}),
        page(sheet_rows(), {"summaryData": [["Summary", "Value", "Notes"], ["a"] * 4]}),
        page(sheet_rows(), {"doc": 1}),
        page([["1/2/2030 11:00 AM", "Sell"]]),
        page([]),
        page(sheet_rows(), {"sheetData": [["Date", "x"], *sheet_rows()]}),
    ],
)
def test_model_page_fails_closed_on_drift(body: object) -> None:
    with pytest.raises(WebContractError):
        parse_model_page(body)


def test_models_catalog_and_index() -> None:
    catalog = [
        {"slug": "a-model", "privilege": "gold"},
        {"slug": "b-model", "privilege": "registered", "signal": "Buy"},
    ]
    entries = parse_models_catalog(catalog)
    assert [e.signal for e in entries] == [None, "Buy"]
    for bad in ([], {}, [{"slug": "A", "privilege": "x"}], catalog + catalog[:1]):
        with pytest.raises(WebContractError):
            parse_models_catalog(bad)
    index = {
        "pageProps": {
            "plans": [{"key": "k", "prices": []}],
            "summaryData": {"a-model": [["Summary", "Value", "Notes"]]},
        }
    }
    assert parse_models_index(index) == ("a-model",)
    with pytest.raises(WebContractError):
        parse_models_index({"pageProps": {"plans": [], "summaryData": {}}})


def test_contract_routing_is_deny_by_default() -> None:
    contract = GRIZZLYBULLS_CONTRACT
    assert contract.api_fallback is False
    assert classify("GET", f"{ORIGIN}/api/models", contract) == (
        "routed",
        "models.catalog",
    )
    assert classify(
        "GET", f"{ORIGIN}/_next/data/BUILD/models/x-model.json?slug=x-model", contract
    ) == (
        "routed",
        "models.page",
    )
    assert classify("GET", f"{ORIGIN}/_next/data/BUILD/models.json", contract) == (
        "routed",
        "models.index",
    )
    for path in DENIED_PATHS:
        assert classify("GET", f"{ORIGIN}{path}", contract)[0] == "denied"
        assert classify("POST", f"{ORIGIN}{path}/", contract)[0] == "denied"
    assert classify("GET", f"{ORIGIN}/api/models?x=1", contract)[0] == "unrouted"
    assert classify("POST", f"{ORIGIN}/api/models", contract)[0] == "unrouted"
    other = "https://www.grizzlybulls.com/api/models"
    assert classify("GET", other, contract)[0] == "offorigin"
    assert classify("GET", "http://grizzlybulls.com/api/models", contract)[0] == (
        "offorigin"
    )


def test_gated_history_routes_are_exact_and_vocabulary_bound() -> None:
    contract = GRIZZLYBULLS_CONTRACT
    ok = f"{ORIGIN}/api/modelTradeHistory?slug=x-model&mode="
    assert classify("GET", ok + "blended", contract) == ("routed", "models.history")
    assert classify("GET", ok + "live", contract) == ("routed", "models.history")
    assert classify("GET", f"{ORIGIN}/api/modelsHistorical?slug=x-model", contract) == (
        "routed",
        "models.historical",
    )
    for bad in (
        ok + "live&extra=1",
        f"{ORIGIN}/api/modelTradeHistory?slug=x-model",
        f"{ORIGIN}/api/modelTradeHistory?mode=live&mode=live&slug=a",
        f"{ORIGIN}/api/modelsHistorical",
    ):
        assert classify("GET", bad, contract)[0] == "unrouted"
    assert classify("POST", ok + "live", contract)[0] == "unrouted"
    endpoint = contract.route("GET", ok + "live")
    assert endpoint.authentication_header == "authorization"
    assert endpoint.validate_query is not None
    for query in (
        {"slug": "x-model", "mode": "other"},
        {"slug": "../x", "mode": "live"},
    ):
        with pytest.raises(WebContractError):
            endpoint.validate_query(query)


def test_historical_json_parses_like_the_page_table() -> None:
    body = {"sheetData": page(sheet_rows())["pageProps"]["sheetData"]}  # type: ignore[index]
    summary = summarize_capability("models.historical", json.dumps(body).encode())
    assert summary["row_count"] == 3
    with pytest.raises(WebContractError):
        parse_historical({"message": "Internal Server Error"})
    with pytest.raises(WebContractError):
        summarize_capability("models.unknown", b"{}")


def test_sanitization_audit_proves_no_data_field_is_masked() -> None:
    long_id = "A" * 30
    body = {
        "email": "someone@example.invalid",
        "rows": [{"authorization_note": "kept", "symbol": "SPXL", "ref": long_id}],
        "token_count": 3,
    }
    audit = sanitization_audit(body)
    assert audit["data_fields_masked"] == []
    assert audit["unexplained_removals"] == []
    assert audit["explicitly_removed"] == ["/email"]
    # the stock sanitizer would have hidden these legitimate fields
    assert audit["stock_sanitizer_would_mask"] == [
        "/email",
        "/rows[0]/authorization_note",
        "/rows[0]/ref",
        "/token_count",
    ]


REAL_ROOT = os.environ.get("GRIZZLYBULLS_EXPORT_ROOT")


@pytest.mark.skipif(not REAL_ROOT, reason="owner exports not available")
def test_owner_exports_validate_against_the_contract() -> None:  # pragma: no cover
    root = Path(REAL_ROOT or "")
    live = parse_trade_history_csv(
        (root / "vix-ta-macro-advanced-live-trade-history.csv").read_bytes()
    )
    blended = parse_trade_history_csv(
        (root / "vix-ta-macro-advanced-blended-trade-history.csv").read_bytes()
    )
    assert (len(live.events), len(blended.events)) == (237, 482)
    assert live.events[0].action == "Buy"
    path = root / "website_exports" / "vix-ta-macro-advanced.json"
    body = json.loads(path.read_bytes())
    assert len(parse_model_page(body).events) == 443
    assert sanitization_audit(body)["data_fields_masked"] == []


def test_invalid_query_values_are_not_routed() -> None:
    base = f"{ORIGIN}/api/modelTradeHistory"
    for url in (
        f"{base}?slug=x-model&mode=all",
        f"{base}?slug=../x&mode=live",
        f"{ORIGIN}/api/modelsHistorical?slug=A_B",
        f"{ORIGIN}/_next/data/B/models/x-model.json?slug=%2e%2e",
    ):
        assert classify("GET", url, GRIZZLYBULLS_CONTRACT)[0] == "unrouted"
