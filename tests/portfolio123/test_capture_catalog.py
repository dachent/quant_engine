from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

import pytest

from p123_capture_fixtures import (
    CREDITS,
    DESIGNER,
    DESIGNER_COMMENT,
    DESIGNER_PERSON,
    MODEL,
    PREDICTOR,
    SCREEN,
    SIMULATION,
    transactions_page,
)
from quant_engine.portfolio123.capture_shapes import (
    canonical_bytes as canonical_json_bytes,
)
from quant_engine.portfolio123 import capture_catalog as catalog
from quant_engine.portfolio123.capture_shapes import (
    REDACTION,
    Choice,
    Obj,
    Seq,
    ShapeError,
    redact_keys,
    validate_shape,
)
from quant_engine.web_contracts import PORTFOLIO123_CONTRACT, WebContractError

# Digest of the reviewed contract's endpoint identities, taken at the base of this
# change: the catalog must leave the reviewed contract byte-for-byte unchanged.
_REVIEWED = "1abfce2c39a905e1f0c8ea7a9b2386bd62ddc7df503dbd6d22f5635f22fb9d1c"


def _digest(endpoints: Any) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            [
                [
                    e.capability,
                    e.method,
                    e.origin,
                    e.path,
                    e.path_pattern,
                    sorted(e.query_keys),
                ]
                for e in endpoints
            ]
        )
    ).hexdigest()


def test_reviewed_contract_is_not_altered_by_the_catalog() -> None:
    assert _digest(PORTFOLIO123_CONTRACT.endpoints) == _REVIEWED
    proposed = catalog.proposed_contract()
    assert _digest(proposed.endpoints[:13]) == _REVIEWED
    assert (
        PORTFOLIO123_CONTRACT.api_fallback is False and proposed.api_fallback is False
    )


def test_proposed_contract_is_unique_and_read_only() -> None:
    proposed = catalog.proposed_contract()
    identities = {(e.method, e.origin, e.path) for e in proposed.endpoints}
    assert len(identities) == len(proposed.endpoints)
    capture = catalog.capture_contract()
    assert {e.capability for e in capture.endpoints} == {
        r.capability for r in catalog.har_routes()
    }
    assert all(
        e.method == "GET" and e.origin == catalog.ORIGIN for e in capture.endpoints
    )
    # No new route may be shadowed by (or shadow) a reviewed route.
    for route in catalog.har_routes():
        assert all(old.path != route.path for old in PORTFOLIO123_CONTRACT.endpoints)


# The explicit review table: how the ORIGINAL placeholder compared with the preserved
# real evidence, and what the catalog row is now.  Changing a row means changing this
# table (and the runbook table) in the same reviewed change.
_REVIEW_TABLE = {
    "aifactor.predictor": ("UNVERIFIED", "UNVERIFIED"),
    "aifactor.model": ("UNVERIFIED", "UNVERIFIED"),
    "screen.definition": ("UNVERIFIED", "UNVERIFIED"),
    "simulation.result": ("UNVERIFIED", "UNVERIFIED"),
    "transactions.account": ("DIFFERS", "UNVERIFIED"),
    "transactions.download": ("DIFFERS", "CONFIRMED"),
    "designer-model.json": ("DIFFERS", "CONFIRMED"),
    "billing.credit-table": ("UNVERIFIED", "UNVERIFIED"),
    "billing.invoice": ("UNVERIFIED", "UNVERIFIED"),
}


def test_review_table_is_explicit_and_rows_are_marked_by_verdict() -> None:
    assert catalog.findings() == _REVIEW_TABLE
    rows = catalog.catalog_rows()
    assert len(rows) == len(catalog.CATALOG) == len(_REVIEW_TABLE)
    for row in rows:
        confirmed = row["verdict"] == "CONFIRMED"
        assert row["route_status"] == (
            "CONFIRMED_PRESERVED_BYTES" if confirmed else "UNVERIFIED_PLACEHOLDER"
        )
        assert row["shape_status"] == (
            "CONFIRMED_PRESERVED_BYTES" if confirmed else "UNVERIFIED_SYNTHETIC"
        )
        # An evidence note is mandatory and never carries an identifier-sized number.
        assert row["evidence"] and not re.search(r"\d{5,}", row["evidence"])
    assert len(catalog.catalog_sha256()) == 64


def test_evidence_status_needs_every_capability_in_scope_confirmed() -> None:
    assert catalog.evidence_status(["designer-model.json"]) == "REVIEWED_SHAPES"
    assert (
        catalog.evidence_status(["designer-model.json", "transactions.download"])
        == "REVIEWED_SHAPES"
    )
    assert (
        catalog.evidence_status(["designer-model.json", "aifactor.predictor"])
        == "UNVERIFIED_SYNTHETIC_SHAPES"
    )
    assert catalog.evidence_status([]) == "UNVERIFIED_SYNTHETIC_SHAPES"
    with pytest.raises(catalog.CaptureRefusal):
        catalog.evidence_status(["absent"])


def test_confirmed_designer_route_is_the_recorded_real_route() -> None:
    route = catalog.route_for("designer-model.json")
    assert route.path == "/spr/strategy/model/{model_id}"
    assert route.query_keys == frozenset({"dm"}) and route.query_values == (
        ("dm", "true"),
    )
    endpoint = catalog.endpoint_contract(route)
    base = f"{catalog.ORIGIN}/spr/strategy/model/9005"
    assert endpoint.matches("GET", f"{base}?dm=true")
    assert not endpoint.matches("POST", f"{base}?dm=true")
    assert not endpoint.matches("GET", base)
    # The sibling performance route is a different path and stays unreviewed.
    assert not endpoint.matches("GET", f"{base}/perf?dm=true")
    assert endpoint.validate_query is not None
    endpoint.validate_query({"dm": "true"})
    for bad in ({"dm": "false"}, {"dm": "1"}, {"dm": "true", "x": "1"}, {}):
        with pytest.raises(WebContractError):
            endpoint.validate_query(bad)


def test_redactions_are_declared_and_reach_the_contract() -> None:
    assert catalog.route_for("designer-model.json").redact == frozenset(
        {"designerinfo", "comments"}
    )
    fields = catalog.capture_contract().sensitive_fields
    assert PORTFOLIO123_CONTRACT.sensitive_fields <= fields
    assert {"designerinfo", "comments"} <= fields
    # The reviewed contract itself is untouched.
    assert "comments" not in PORTFOLIO123_CONTRACT.sensitive_fields


def test_designer_shape_is_the_real_field_set() -> None:
    spec = catalog.route_for("designer-model.json").shape
    assert isinstance(spec, Obj)
    # Top-level field names of the preserved response (names only).
    assert set(spec.required) == {
        "backtestDisabled", "benchmark", "benchmarkUid", "comments", "context",
        "currency", "desc", "designerModel", "editable", "endDt", "hasRebalTran",
        "hedgeEnabled", "holdingsAvg", "inceptionDt", "lastRebal",
        "lastRebalanceToday", "launchDt", "liquidationValue", "maxdd90",
        "mkttypeUid", "name", "pctHedgeAvg", "portUid", "rebalFreq", "refLiquidity",
        "riskScore", "stats3y", "statsInception", "statsLaunch", "topData",
        "tradesTtm", "tradingAll", "tradingCommitment", "tradingLose", "tradingWin",
        "trailingReturns", "volatility90",
    }  # fmt: skip
    assert not spec.optional


def test_confirmed_transactions_header_is_the_real_header() -> None:
    route = catalog.route_for("transactions.download")
    assert route.csv_columns == (
        (
            "Date",
            "Symbol",
            "Type",
            "Shares",
            "Price",
            "Amount",
            "TotFees",
            "Rank",
            "Notes",
        ),
    )


def test_synthetic_fixtures_validate_against_their_shapes() -> None:
    bodies = {
        "aifactor.predictor": PREDICTOR,
        "aifactor.model": MODEL,
        "screen.definition": SCREEN,
        "simulation.result": SIMULATION,
        "designer-model.json": _redacted_designer(),
        "billing.credit-table": CREDITS,
        "transactions.account": transactions_page(1, 1, 2, 2),
    }
    for capability, body in bodies.items():
        shape = catalog.route_for(capability).shape
        assert shape is not None
        validate_shape(body, shape)


def test_catalog_rejects_inconsistent_rows() -> None:
    with pytest.raises(catalog.CaptureRefusal):
        catalog.CaptureRoute("x.y", "t", ("har",), "o")
    with pytest.raises(catalog.CaptureRefusal):
        catalog.CaptureRoute("x.y", "t", ("file",), "o", file_media=("csv",))
    with pytest.raises(catalog.CaptureRefusal):
        catalog.route_for("absent")


def test_shape_engine_is_strict() -> None:
    spec = Obj(
        {"a": "int", "b": Seq("num", min_items=1), "c": Choice(("X",))}, {"d": "str?"}
    )
    validate_shape({"a": 1, "b": [1, 2.5], "c": "X", "d": None}, spec)
    for bad in (
        {"a": 1, "b": [1], "c": "Y"},
        {"a": True, "b": [1], "c": "X"},
        {"a": 1, "b": [float("nan")], "c": "X"},
        {"a": 1, "b": [], "c": "X"},
        {"a": 1, "b": [1], "c": "X", "z": 1},
        {"a": 1, "b": [1]},
        [1],
    ):
        with pytest.raises(ShapeError):
            validate_shape(bad, spec)
    with pytest.raises(ShapeError):
        validate_shape({"a": None}, Obj({"a": "int"}))
    deep: object = 1
    nested: Any = "int"
    for _ in range(20):
        deep = [deep]
        nested = Seq(nested)
    with pytest.raises(ShapeError):
        validate_shape(deep, nested)


def _redacted_designer() -> dict[str, Any]:
    keys = catalog.route_for("designer-model.json").redact
    value = redact_keys(copy.deepcopy(DESIGNER), keys)
    assert isinstance(value, dict)
    return value


def test_designer_fixture_validates_only_after_redaction() -> None:
    shape = catalog.route_for("designer-model.json").shape
    assert shape is not None
    redacted = _redacted_designer()
    validate_shape(redacted, shape)
    assert redacted["comments"] == REDACTION
    assert redacted["designerModel"]["designerInfo"] == REDACTION
    text = repr(redacted)
    assert DESIGNER_PERSON not in text and DESIGNER_COMMENT not in text
    # The raw (unredacted) body is not the reviewed shape: personal data cannot slip in.
    with pytest.raises(ShapeError):
        validate_shape(DESIGNER, shape)
    # Redaction is case-insensitive and does not mutate its input.
    assert DESIGNER["comments"] != REDACTION


def _mutations() -> list[Any]:
    def drop(value: dict[str, Any]) -> None:
        del value["tradesTtm"]

    def extra(value: dict[str, Any]) -> None:
        value["unreviewedField"] = 1

    def iso_date(value: dict[str, Any]) -> None:
        value["inceptionDt"] = "2028-01-03"

    def us_date_wrong(value: dict[str, Any]) -> None:
        value["endDt"] = "13/45/2029"

    def context_value(value: dict[str, Any]) -> None:
        value["context"] = "x"

    def null_int(value: dict[str, Any]) -> None:
        value["riskScore"] = None

    def bool_as_int(value: dict[str, Any]) -> None:
        value["hasRebalTran"] = True

    def nested_extra(value: dict[str, Any]) -> None:
        value["stats3y"]["model"]["unreviewed"] = 1

    def short_trailing(value: dict[str, Any]) -> None:
        value["trailingReturns"][0]["values"] = [0.1]

    return [
        drop, extra, iso_date, us_date_wrong, context_value, null_int,
        bool_as_int, nested_extra, short_trailing,
    ]  # fmt: skip


@pytest.mark.parametrize("mutate", _mutations())
def test_designer_shape_refuses_every_drift(mutate: Any) -> None:
    shape = catalog.route_for("designer-model.json").shape
    assert shape is not None
    body = _redacted_designer()
    mutate(body)
    with pytest.raises(ShapeError):
        validate_shape(body, shape)


def test_shape_engine_us_date_and_null_scalars() -> None:
    validate_shape("02/28/2029", "us_date")
    validate_shape(None, "null")
    validate_shape(None, "num?")
    for bad, spec in (
        ("2029-02-28", "us_date"),
        ("02/30/2029", "us_date"),
        (1, "us_date"),
        (0, "null"),
        ("", "null"),
    ):
        with pytest.raises(ShapeError):
            validate_shape(bad, spec)
    with pytest.raises(ShapeError):
        validate_shape(None, "null?")


def test_unverified_rows_are_still_marked_and_never_enable_a_real_route() -> None:
    contract = catalog.capture_contract()
    for capability, (_, verdict) in _REVIEW_TABLE.items():
        if verdict != "UNVERIFIED":
            continue
        route = catalog.route_for(capability)
        assert route.verdict == "UNVERIFIED"
        assert "placeholder" in route.evidence.lower()
    # No observed real route is matched by an unverified placeholder.
    for real in (
        "/transact_list.jsp?portid=1&offset=0",
        "/port_simul6.jsp",
        "/sv/opener/AIFACTOR/-2",
        "/sv/account-settings/billing",
        "/app/screen/summary/0/revert?PortSimId=1",
        "/app/investment/perf-data/1?init=true",
    ):
        for method in ("GET", "POST"):
            with pytest.raises(WebContractError):
                contract.route(method, catalog.ORIGIN + real)


def test_verify_confirmed_shape_is_the_public_revalidation() -> None:
    from quant_engine import portfolio123

    redacted = _redacted_designer()
    portfolio123.verify_confirmed_shape("designer-model.json", redacted)
    drifted = {**redacted, "unreviewed": 1}
    for capability, body, code in (
        ("designer-model.json", drifted, "SHAPE_REFUSED"),
        ("designer-model.json", DESIGNER, "SHAPE_REFUSED"),
        ("aifactor.predictor", PREDICTOR, "SHAPE_NOT_REVIEWED"),
        ("absent", {}, "CAPABILITY_UNKNOWN"),
    ):
        with pytest.raises(portfolio123.CaptureRefusal) as caught:
            portfolio123.verify_confirmed_shape(capability, body)
        assert caught.value.code == code
