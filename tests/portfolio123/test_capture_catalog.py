from __future__ import annotations

import hashlib
from typing import Any

import pytest

from p123_capture_fixtures import (
    CREDITS,
    DESIGNER,
    MODEL,
    PREDICTOR,
    SCREEN,
    SIMULATION,
    transactions_page,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio123 import capture_catalog as catalog
from quant_engine.portfolio123.capture_shapes import (
    Choice,
    Obj,
    Seq,
    ShapeError,
    validate_shape,
)
from quant_engine.web_contracts import PORTFOLIO123_CONTRACT

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


def test_every_row_is_marked_unverified_and_pinned() -> None:
    rows = catalog.catalog_rows()
    assert len(rows) == len(catalog.CATALOG)
    assert all(r["route_status"] == "UNVERIFIED_PLACEHOLDER" for r in rows)
    assert all(r["shape_status"] == "UNVERIFIED_SYNTHETIC" for r in rows)
    assert len(catalog.catalog_sha256()) == 64


def test_synthetic_fixtures_validate_against_their_shapes() -> None:
    bodies = {
        "aifactor.predictor": PREDICTOR,
        "aifactor.model": MODEL,
        "screen.definition": SCREEN,
        "simulation.result": SIMULATION,
        "designer-model.json": DESIGNER,
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
