from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from quant_engine.web_contracts import (
    GRIZZLYBULLS_CONTRACT,
    REDACTION,
    ROUTE_CATALOG,
    STRATEGY_SERIES_MAP,
    AuthClass,
    BridgeObservation,
    NormalizedSeries,
    SemanticEffect,
    SeriesIdentity,
    SessionEnvelope,
    TradeObservation,
    WebContractError,
    build_coverage,
    canonical_extraction,
    combine_series,
    compare_coverage,
    compare_with_bridge,
    coverage_gaps,
    extract_grizzlybulls_har,
    governed_series_id,
    normalize_document,
    parse_bridge_rows,
    persist_capture_run,
    replay_capture_run,
    required_series_keys,
)
from quant_engine.web_contracts.grizzlybulls_series import main as series_main


NOW = datetime(2026, 9, 2, 13, tzinfo=timezone.utc)
_BUILD = "SYNTHETIC_BUILD_ID"
_GOV = "https://grizzlybulls.com"
_DETAIL_URL = f"{_GOV}/_next/data/{_BUILD}/models/vix-ta-macro-advanced.json"


def _entry(
    url: str,
    *,
    method: str = "GET",
    status: int = 200,
    body: object | None = None,
    text: str | None = None,
    headers: list[dict[str, str]] | None = None,
    mime: str = "application/json",
) -> dict[str, object]:
    return {
        "request": {
            "headers": [] if headers is None else headers,
            "method": method,
            "url": url,
        },
        "response": {
            "content": {
                "mimeType": mime,
                "text": json.dumps({} if body is None else body)
                if text is None
                else text,
            },
            "status": status,
        },
    }


def _session(*, expired: bool = False) -> SessionEnvelope:
    return SessionEnvelope(
        captured_at=NOW,
        expires_at=NOW - timedelta(seconds=1)
        if expired
        else NOW + timedelta(minutes=30),
    )


def _document(*entries: object) -> dict[str, object]:
    return {"log": {"entries": list(entries)}}


def _detail_body(rows: list[object] | None = None) -> dict[str, object]:
    return {
        "pageProps": {
            "sheetData": [
                ["Date", "Action", "SPX"],
                ["4/20/2009", "Buy", 837.25],
                ["5/1/2009", "Sell", 851.5],
            ]
            if rows is None
            else rows,
            "summaryData": [["Summary", "Value"]],
            "doc": "SYNTHETIC model description",
        }
    }


def _detail_entry() -> dict[str, object]:
    return _entry(_DETAIL_URL, body=_detail_body())


def _live_only_series() -> NormalizedSeries:
    return NormalizedSeries(
        series_id="grizzlybears-vtma",
        strategy_slug="vix-ta-macro-advanced",
        identity=SeriesIdentity.LIVE_ONLY,
        source_capability="synthetic.live",
        rows=(
            TradeObservation(observed="2026-03-01", action="BUY", spx=5100.0),
            TradeObservation(observed="2026-04-08", action="SELL", spx=5200.0),
        ),
    )


# ─── catalog classification ──────────────────────────────────────────────────


def test_catalog_classifies_every_route_and_session_routes_are_rejected() -> None:
    assert ROUTE_CATALOG
    assert {route.auth_class for route in ROUTE_CATALOG} == {
        AuthClass.PUBLIC,
        AuthClass.SESSION,
    }
    session = [
        route for route in ROUTE_CATALOG if route.auth_class is AuthClass.SESSION
    ]
    assert session and all(route.summary for route in session)
    public_capabilities = {
        endpoint.capability for endpoint in GRIZZLYBULLS_CONTRACT.endpoints
    }
    assert public_capabilities == {
        route.capability
        for route in ROUTE_CATALOG
        if route.auth_class is AuthClass.PUBLIC
    }
    assert not {route.capability for route in session} & public_capabilities
    document = _document(
        _entry(f"{_GOV}/api/user"),
        _detail_entry(),
    )
    with pytest.raises(WebContractError, match="session-bearing route"):
        extract_grizzlybulls_har(document, session=_session())


def test_every_public_endpoint_is_session_free_and_schema_validated() -> None:
    assert GRIZZLYBULLS_CONTRACT.provider == "grizzlybulls"
    assert GRIZZLYBULLS_CONTRACT.api_fallback is False
    for endpoint in GRIZZLYBULLS_CONTRACT.endpoints:
        assert endpoint.method == "GET"
        assert endpoint.semantic_effect is SemanticEffect.READ_ONLY
        assert endpoint.authentication_header is None
        assert endpoint.validate_response is not None


def test_session_material_on_any_provider_route_fails_closed() -> None:
    cookie = [{"name": "Cookie", "value": "gb_session=SYNTHETIC"}]
    with pytest.raises(WebContractError, match="session-bearing provider request"):
        extract_grizzlybulls_har(
            _document(_entry(f"{_GOV}/api/models", headers=cookie)),
            session=_session(),
        )
    with pytest.raises(WebContractError, match="session-bearing provider request"):
        extract_grizzlybulls_har(
            _document(_entry(f"{_DETAIL_URL}?token=SYNTHETIC")),
            session=_session(),
        )
    with pytest.raises(WebContractError, match="session-bearing provider request"):
        extract_grizzlybulls_har(
            _document(
                _entry(
                    f"{_GOV}/_next/static/chunks/synthetic.js",
                    headers=cookie,
                    mime="text/javascript",
                ),
                _detail_entry(),
            ),
            session=_session(),
        )


def test_mutation_like_request_fails_closed() -> None:
    document = _document(
        _entry(f"{_GOV}/api/models", method="POST"),
        _detail_entry(),
    )
    with pytest.raises(WebContractError, match="mutation-like"):
        extract_grizzlybulls_har(document, session=_session())


# ─── fail-closed extraction ──────────────────────────────────────────────────


def test_non_success_response_fails_closed() -> None:
    document = _document(
        _entry(
            f"{_GOV}/_next/data/{_BUILD}/models.json",
            status=500,
            body={"pageProps": {"summaryData": {}}},
        ),
        _detail_entry(),
    )
    with pytest.raises(WebContractError, match="non-success"):
        extract_grizzlybulls_har(document, session=_session())


def test_missing_or_empty_body_fails_closed() -> None:
    empty = _entry(
        f"{_GOV}/_next/data/{_BUILD}/models.json",
        text="",
    )
    with pytest.raises(WebContractError, match="missing or empty"):
        extract_grizzlybulls_har(_document(empty, _detail_entry()), session=_session())


def test_unreviewed_provider_route_fails_closed() -> None:
    document = _document(
        _entry(f"{_GOV}/_next/data/{_BUILD}/secret.json", body={"pageProps": {}}),
        _detail_entry(),
    )
    with pytest.raises(WebContractError, match="allowlisted"):
        extract_grizzlybulls_har(document, session=_session())
    document = _document(
        _entry(f"{_GOV}/api/unreviewed", body=[]),
        _detail_entry(),
    )
    with pytest.raises(WebContractError, match="allowlisted"):
        extract_grizzlybulls_har(document, session=_session())


def test_static_third_party_and_navigation_shells_are_explicitly_disposed() -> None:
    document = _document(
        _entry("https://www.example-analytics.invalid/collect", body={}),
        _entry(f"{_GOV}/_next/static/chunks/synthetic.js", mime="text/javascript"),
        _entry(f"{_GOV}/favicon.ico", mime="image/x-icon", text="SYNTHETIC"),
        _entry(
            f"{_GOV}/models/vix-ta-macro-advanced",
            mime="text/html",
            text="<html>SYNTHETIC</html>",
        ),
        _detail_entry(),
    )
    records = extract_grizzlybulls_har(document, session=_session())
    assert [record.capability for record in records] == ["models.detail"]


def test_expired_session_and_empty_capture_fail_closed() -> None:
    with pytest.raises(WebContractError, match="expired"):
        extract_grizzlybulls_har(
            _document(_detail_entry()), session=_session(expired=True)
        )
    with pytest.raises(WebContractError, match="no reviewed"):
        extract_grizzlybulls_har(
            _document(_entry("https://www.example.invalid/", body={})),
            session=_session(),
        )


# ─── schema validation ───────────────────────────────────────────────────────


def test_schema_validation_accepts_the_reviewed_shapes() -> None:
    document = _document(
        _entry(
            f"{_GOV}/api/models",
            body=[
                {
                    "shortName": "SYNTHETIC VTMA",
                    "signal": "Buy",
                    "slug": "vix-ta-macro-advanced",
                }
            ],
        ),
        _entry(
            f"{_GOV}/api/indicators",
            body=[{"shortName": "SYNTHETIC", "slug": "synthetic-carp", "value": 100}],
        ),
        _entry(
            f"{_GOV}/_next/data/{_BUILD}/index.json",
            body={
                "pageProps": {
                    "summaryData": {
                        "vix-ta-macro-advanced": {
                            "CAGR": "1.0%",
                            "uuid": "SYNTHETIC-UUID-VALUE",
                        }
                    }
                }
            },
        ),
        _detail_entry(),
    )
    first = canonical_extraction(extract_grizzlybulls_har(document, session=_session()))
    second = canonical_extraction(
        extract_grizzlybulls_har(document, session=_session())
    )
    assert first == second
    decoded = json.loads(first)
    assert {record["capability"] for record in decoded["records"]} == {
        "indicators.signals",
        "models.detail",
        "models.home",
        "models.signals",
    }
    assert b"SYNTHETIC-UUID-VALUE" not in first
    assert REDACTION.encode() in first


@pytest.mark.parametrize(
    "url,body",
    [
        (
            _DETAIL_URL,
            _detail_body(rows=[["Date", "Action", "SPX"], ["2009-04-20", "Buy", 1]]),
        ),
        (
            _DETAIL_URL,
            _detail_body(rows=[["Day", "Action"], ["4/20/2009", "Buy"]]),
        ),
        (
            _DETAIL_URL,
            _detail_body(rows=[["Date", "Action", "SPX"], ["4/20/2009", "Buy"]]),
        ),
        (f"{_GOV}/api/models", [{"shortName": "SYNTHETIC", "signal": "Buy"}]),
        (f"{_GOV}/api/models", {"shortName": "SYNTHETIC"}),
        (
            f"{_GOV}/api/indicators",
            [{"shortName": "SYNTHETIC", "slug": "synthetic", "value": "high"}],
        ),
        (f"{_GOV}/_next/data/{_BUILD}/index.json", {"unexpected": {}}),
    ],
)
def test_schema_drift_fails_closed(url: str, body: object) -> None:
    document = _document(_entry(url, body=body), _detail_entry())
    with pytest.raises(WebContractError, match="schema drifted"):
        extract_grizzlybulls_har(document, session=_session())


# ─── authoritative strategy mapping ──────────────────────────────────────────


def test_strategy_mapping_is_authoritative_not_heuristic() -> None:
    assert STRATEGY_SERIES_MAP == {"vix-ta-macro-advanced": "grizzlybears-vtma"}
    assert governed_series_id("vix-ta-macro-advanced") == "grizzlybears-vtma"
    with pytest.raises(WebContractError, match="no authoritative series mapping"):
        governed_series_id("vix-ta-macro-mp-extre")
    series = normalize_document(_document(_detail_entry()), session=_session())
    assert [item.series_id for item in series] == ["grizzlybears-vtma"]
    unmapped = _entry(
        f"{_GOV}/_next/data/{_BUILD}/models/ta-mr-basic.json",
        body=_detail_body(),
    )
    with pytest.raises(WebContractError, match="no authoritative series mapping"):
        normalize_document(_document(unmapped), session=_session())


# ─── deterministic replay ────────────────────────────────────────────────────


def test_persist_and_replay_derive_identical_hashes(tmp_path: Path) -> None:
    document = _document(
        _detail_entry(),
        _entry(
            f"{_GOV}/api/models",
            body=[
                {
                    "shortName": "SYNTHETIC VTMA",
                    "signal": "Sell",
                    "slug": "vix-ta-macro-advanced",
                }
            ],
        ),
    )
    run_dir = tmp_path / "run"
    manifest = persist_capture_run(document, session=_session(), run_dir=run_dir)
    first = replay_capture_run(run_dir)
    second = replay_capture_run(run_dir)
    assert first.to_dict() == second.to_dict()
    for field in (
        "capture_sha256",
        "coverage_sha256",
        "normalized_sha256",
        "raw_sha256",
    ):
        assert first.replayed[field] == manifest[field]
    assert series_main(["replay", str(run_dir)]) == 0


def test_replay_fails_closed_on_altered_evidence(tmp_path: Path) -> None:
    document = _document(_detail_entry())
    run_dir = tmp_path / "run"
    persist_capture_run(document, session=_session(), run_dir=run_dir)
    (run_dir / "raw.har").write_bytes(b" " + (run_dir / "raw.har").read_bytes())
    with pytest.raises(WebContractError, match="altered"):
        replay_capture_run(run_dir)
    other_dir = tmp_path / "other"
    persist_capture_run(document, session=_session(), run_dir=other_dir)
    (other_dir / "normalized.json").write_bytes(b"{}")
    with pytest.raises(WebContractError, match="altered"):
        replay_capture_run(other_dir)


def test_persist_never_overwrites_evidence(tmp_path: Path) -> None:
    document = _document(_detail_entry())
    run_dir = tmp_path / "run"
    persist_capture_run(document, session=_session(), run_dir=run_dir)
    with pytest.raises((FileExistsError, WebContractError)):
        persist_capture_run(document, session=_session(), run_dir=run_dir)


# ─── all-history vs live-only identity ───────────────────────────────────────


def test_histories_are_separately_identified_and_never_concatenated() -> None:
    (all_history,) = normalize_document(_document(_detail_entry()), session=_session())
    live_only = _live_only_series()
    assert all_history.identity is SeriesIdentity.ALL_HISTORY
    assert live_only.identity is SeriesIdentity.LIVE_ONLY
    assert all_history.series_key == "grizzlybears-vtma/all-history"
    assert live_only.series_key == "grizzlybears-vtma/live-only"
    with pytest.raises(WebContractError, match="contract violation"):
        combine_series([all_history, live_only])
    fragment = NormalizedSeries(
        series_id=all_history.series_id,
        strategy_slug=all_history.strategy_slug,
        identity=all_history.identity,
        source_capability=all_history.source_capability,
        rows=(TradeObservation(observed="2009-06-01", action="BUY", spx=900.0),),
    )
    combined = combine_series([all_history, fragment])
    assert combined.series_key == all_history.series_key
    assert combined.observed_end == "2009-06-01"
    coverage = build_coverage([all_history, live_only])
    assert [item.series_key for item in coverage] == [
        "grizzlybears-vtma/all-history",
        "grizzlybears-vtma/live-only",
    ]


def test_coverage_records_freshness_and_reports_gaps() -> None:
    (all_history,) = normalize_document(_document(_detail_entry()), session=_session())
    coverage = build_coverage([all_history])
    (record,) = coverage
    assert record.observed_start == "2009-04-20"
    assert record.observed_end == "2009-05-01"
    assert record.row_count == 2
    assert record.series_key == "grizzlybears-vtma/all-history"
    assert set(required_series_keys()) == {
        "grizzlybears-vtma/all-history",
        "grizzlybears-vtma/live-only",
    }
    assert coverage_gaps(coverage) == ("grizzlybears-vtma/live-only",)
    assert coverage_gaps(build_coverage([all_history, _live_only_series()])) == ()
    with pytest.raises(WebContractError, match="duplicate series identity"):
        build_coverage([all_history, all_history])


# ─── drift handling ──────────────────────────────────────────────────────────


def test_content_drift_is_recorded_never_silently_accepted() -> None:
    (all_history,) = normalize_document(_document(_detail_entry()), session=_session())
    baseline = build_coverage([all_history, _live_only_series()])
    assert compare_coverage(baseline, baseline).drift == ()
    compare_coverage(baseline, baseline).require_clean()
    changed = NormalizedSeries(
        series_id=all_history.series_id,
        strategy_slug=all_history.strategy_slug,
        identity=all_history.identity,
        source_capability=all_history.source_capability,
        rows=all_history.rows
        + (TradeObservation(observed="2009-06-01", action="BUY", spx=900.0),),
    )
    candidate = build_coverage([changed, _live_only_series()])
    comparison = compare_coverage(baseline, candidate)
    assert comparison.drift
    assert {entry.series_key for entry in comparison.drift} == {
        "grizzlybears-vtma/all-history"
    }
    with pytest.raises(WebContractError, match="content drift"):
        comparison.require_clean()
    partial = compare_coverage(baseline, build_coverage([all_history]))
    assert [entry.field for entry in partial.drift] == ["presence"]


# ─── offline bridge comparison seam ─────────────────────────────────────────


def test_bridge_comparison_seam_with_synthetic_rows() -> None:
    bridge_rows = parse_bridge_rows(
        [
            {
                "Action": "Buy",
                "Buy & Hold": "100.0",
                "Date (ET)": "2009-04-20 09:30 ET",
                "Model": "100.0",
                "PnL %": "",
                "SPX": "837.25",
            },
            {
                "Action": "SELL",
                "Buy & Hold": "101.0",
                "Date (ET)": "2009-05-01 16:00",
                "Model": "102.0",
                "PnL %": "1.7",
                "SPX": "851.50",
            },
            {
                "Action": "BUY",
                "Buy & Hold": "102.0",
                "Date (ET)": "2009-06-01 09:30",
                "Model": "103.0",
                "PnL %": "",
                "SPX": "900.0",
            },
        ],
        identity=SeriesIdentity.ALL_HISTORY,
    )
    (all_history,) = normalize_document(_document(_detail_entry()), session=_session())
    comparison = compare_with_bridge(
        all_history, bridge_rows, bridge_identity=SeriesIdentity.ALL_HISTORY
    )
    assert comparison.series_key == "grizzlybears-vtma/all-history"
    assert comparison.matched == 2
    assert comparison.provider_only == ()
    assert comparison.bridge_only == ("2009-06-01 BUY",)
    assert comparison.value_differences == ()


def test_bridge_comparison_records_differences_and_rejects_identity_misuse() -> None:
    (all_history,) = normalize_document(_document(_detail_entry()), session=_session())
    bridge_rows = (
        BridgeObservation(observed="2009-04-20", action="BUY", spx=838.0),
        BridgeObservation(observed="2009-05-01", action="SELL", spx=851.5),
    )
    comparison = compare_with_bridge(
        all_history, bridge_rows, bridge_identity=SeriesIdentity.ALL_HISTORY
    )
    assert comparison.matched == 1
    assert len(comparison.value_differences) == 1
    difference = comparison.value_differences[0]
    assert difference.observed == "2009-04-20"
    assert difference.provider_spx == 837.25
    assert difference.bridge_spx == 838.0
    with pytest.raises(WebContractError, match="matching series identity"):
        compare_with_bridge(
            all_history, bridge_rows, bridge_identity=SeriesIdentity.LIVE_ONLY
        )


def test_bridge_rows_fail_closed_on_drift() -> None:
    with pytest.raises(WebContractError, match="headers differ"):
        parse_bridge_rows([{"Action": "BUY"}], identity=SeriesIdentity.LIVE_ONLY)
    with pytest.raises(WebContractError, match="timestamp"):
        parse_bridge_rows(
            [
                {
                    "Action": "BUY",
                    "Buy & Hold": "1",
                    "Date (ET)": "2009-04-20",
                    "Model": "1",
                    "PnL %": "",
                    "SPX": "1",
                }
            ],
            identity=SeriesIdentity.LIVE_ONLY,
        )
    with pytest.raises(WebContractError, match="action differs"):
        parse_bridge_rows(
            [
                {
                    "Action": "HOLD",
                    "Buy & Hold": "1",
                    "Date (ET)": "2009-04-20 09:30",
                    "Model": "1",
                    "PnL %": "",
                    "SPX": "1",
                }
            ],
            identity=SeriesIdentity.LIVE_ONLY,
        )
    with pytest.raises(WebContractError, match="empty"):
        parse_bridge_rows([], identity=SeriesIdentity.LIVE_ONLY)
