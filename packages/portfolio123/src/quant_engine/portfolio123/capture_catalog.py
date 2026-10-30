"""Catalog of Portfolio123 captures that the reviewed web contract does not cover.

STATUS: every route path and response shape below is an UNVERIFIED placeholder.
Nothing here was observed on the provider.  The routes exist so that parsers,
staging, and verification can be built and tested offline against synthetic
fixtures; an owner-supplied HAR is first inventoried (route templates only), the
real routes are then reviewed, and only after that review does a catalog entry
become a contract entry.  The catalog never enables a route by itself: it is
contract-ready (:func:`capture_contract` and :func:`proposed_contract` build
``ProviderContract`` objects) and has no effect on ``PORTFOLIO123_CONTRACT``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
from typing import Any

from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio123.capture_shapes import (
    Choice,
    Obj,
    Seq,
    Spec,
    describe,
)
from quant_engine.web_contracts import PORTFOLIO123_CONTRACT
from quant_engine.web_contracts.contracts import (
    EndpointContract,
    ProviderContract,
    SemanticEffect,
    WebContractError,
)


ORIGIN = "https://www.portfolio123.com"
ROUTE_STATUS = "UNVERIFIED_PLACEHOLDER"
SHAPE_STATUS = "UNVERIFIED_SYNTHETIC"
FORM_HAR = "har"
FORM_FILE = "file"
MEDIA_TYPES = ("csv", "json", "pdf")


class CaptureRefusal(ValueError):
    """A capture request or artifact was refused; ``code`` is a stable reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class CaptureRoute:
    """One uncovered capture: where it may come from and what it must look like."""

    capability: str
    title: str
    forms: tuple[str, ...]
    owner_export: str
    path: str | None = None
    path_pattern: str | None = None
    query_keys: frozenset[str] = frozenset()
    shape: Spec | None = None
    file_media: tuple[str, ...] = ()
    csv_columns: tuple[tuple[str, ...], ...] = ()
    paginated: bool = False
    api_operation: str | None = None

    def __post_init__(self) -> None:
        if not self.forms or not set(self.forms) <= {FORM_HAR, FORM_FILE}:
            raise CaptureRefusal("CATALOG_INVALID", "forms")
        if FORM_HAR in self.forms and (self.path is None or self.shape is None):
            raise CaptureRefusal("CATALOG_INVALID", "har route needs path and shape")
        if FORM_FILE in self.forms and not self.file_media:
            raise CaptureRefusal("CATALOG_INVALID", "file route needs media types")
        if not set(self.file_media) <= set(MEDIA_TYPES):
            raise CaptureRefusal("CATALOG_INVALID", "media type")
        if "json" in self.file_media and self.shape is None:
            raise CaptureRefusal("CATALOG_INVALID", "json file needs a shape")
        if "csv" in self.file_media and not self.csv_columns:
            raise CaptureRefusal("CATALOG_INVALID", "csv file needs columns")

    def row(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "forms": list(self.forms),
            "path": self.path,
            "path_pattern": self.path_pattern,
            "query_keys": sorted(self.query_keys),
            "shape": None if self.shape is None else describe(self.shape),
            "file_media": list(self.file_media),
            "csv_columns": [list(item) for item in self.csv_columns],
            "paginated": self.paginated,
            "api_operation": self.api_operation,
            "route_status": ROUTE_STATUS,
            "shape_status": SHAPE_STATUS,
        }


_PREDICTION = Obj({"ticker": "str", "score": "num", "rank": "int"})
_FEATURE = Obj({"name": "str", "weight": "num"})
_NAV_POINT = Obj({"date": "date", "value": "num"})
_TRANSACTION = Obj(
    {
        "trade_date": "date",
        "ticker": "str",
        "action": Choice(("BUY", "SELL", "DIVIDEND", "FEE")),
        "quantity": "num",
        "price": "num",
        "commission": "num",
    }
)
_DESIGNER_MODEL = Obj(
    {
        "model_id": "int",
        "name": "str",
        "inception": "date",
        "rebalance": "str",
        "buy_rules": Seq("str"),
        "sell_rules": Seq("str"),
        "positions": Seq(Obj({"ticker": "str", "weight": "num"})),
    }
)

CATALOG: tuple[CaptureRoute, ...] = (
    CaptureRoute(
        "aifactor.predictor",
        "AI Factor predictor",
        (FORM_HAR,),
        "Open the predictor page, let predictions load, export the HAR.",
        path="/app/ai-factor/predictor/{predictor_id}",
        path_pattern=r"/app/ai-factor/predictor/[1-9][0-9]*",
        shape=Obj(
            {
                "predictor_id": "int",
                "name": "str",
                "model_id": "int",
                "as_of": "date",
                "universe": "str",
                "predictions": Seq(_PREDICTION, min_items=1),
            }
        ),
        # Candidate API route for a declared HAR gap: UNVERIFIED, not allow-listed.
        api_operation="ai_factor_predict",
    ),
    CaptureRoute(
        "aifactor.model",
        "AI Factor model",
        (FORM_HAR,),
        "Open the model page and its validation tab, export the HAR.",
        path="/app/ai-factor/model/{model_id}",
        path_pattern=r"/app/ai-factor/model/[1-9][0-9]*",
        shape=Obj(
            {
                "model_id": "int",
                "name": "str",
                "target": "str",
                "horizon_weeks": "int",
                "trained_through": "date",
                "features": Seq(_FEATURE, min_items=1),
                "validation": Obj({"metric": "str", "value": "num"}),
            }
        ),
    ),
    CaptureRoute(
        "screen.definition",
        "Screen definition",
        (FORM_HAR,),
        "Open each screen and its rules tab, export the HAR.",
        path="/app/screen/{screen_id}",
        path_pattern=r"/app/screen/[1-9][0-9]*",
        shape=Obj(
            {
                "screen_id": "int",
                "name": "str",
                "universe": "str",
                "rules": Seq("str", min_items=1),
            },
            optional={"ranking_system_id": "int?"},
        ),
    ),
    CaptureRoute(
        "simulation.result",
        "Simulation result",
        (FORM_HAR,),
        "Open the simulation results page, wait for the curve, export the HAR.",
        path="/app/simulation/{simulation_id}",
        path_pattern=r"/app/simulation/[1-9][0-9]*",
        shape=Obj(
            {
                "simulation_id": "int",
                "name": "str",
                "start": "date",
                "end": "date",
                "nav": Seq(_NAV_POINT, min_items=2),
                "statistics": Obj({"total_return": "num", "max_drawdown": "num"}),
            }
        ),
    ),
    CaptureRoute(
        "transactions.account",
        "Account or live-book transactions",
        (FORM_HAR,),
        "Open the transactions tab and page to the end, export the HAR.",
        path="/app/account/transactions/{account_id}",
        path_pattern=r"/app/account/transactions/[1-9][0-9]*",
        query_keys=frozenset({"page"}),
        shape=Obj(
            {
                "page": "int",
                "page_count": "int",
                "total": "int",
                "transactions": Seq(_TRANSACTION),
            }
        ),
        paginated=True,
    ),
    CaptureRoute(
        "transactions.download",
        "Transactions download",
        (FORM_FILE,),
        "Use the transactions download control and save the file unmodified.",
        file_media=("csv",),
        csv_columns=(("Date", "Ticker", "Action", "Shares", "Price", "Commission"),),
    ),
    CaptureRoute(
        "designer-model.json",
        "Designer model definition",
        (FORM_HAR, FORM_FILE),
        "Export the designer model JSON, or open the model page and export the HAR.",
        path="/app/investment/model/{model_id}",
        path_pattern=r"/app/investment/model/[1-9][0-9]*",
        shape=_DESIGNER_MODEL,
        file_media=("json",),
    ),
    CaptureRoute(
        "billing.credit-table",
        "Signed-in API credit table",
        (FORM_HAR,),
        "Open the billing credits page signed in, export the HAR.",
        path="/app/billing/credits",
        shape=Obj(
            {
                "plan": "str",
                "quota_remaining": "int",
                "rows": Seq(Obj({"operation": "str", "credits": "num"}), min_items=1),
            }
        ),
    ),
    CaptureRoute(
        "billing.invoice",
        "Billing statement or invoice",
        (FORM_FILE,),
        "Download each statement or invoice from the billing page, unmodified.",
        file_media=("csv", "pdf"),
        csv_columns=(("Invoice Date", "Description", "Amount", "Currency"),),
    ),
)

_BY_CAPABILITY: Mapping[str, CaptureRoute] = {item.capability: item for item in CATALOG}
if len(_BY_CAPABILITY) != len(CATALOG):
    raise CaptureRefusal("CATALOG_INVALID", "duplicate capability")


def route_for(capability: str) -> CaptureRoute:
    try:
        return _BY_CAPABILITY[capability]
    except KeyError:
        raise CaptureRefusal("CAPABILITY_UNKNOWN") from None


def catalog_rows() -> list[dict[str, Any]]:
    return [item.row() for item in sorted(CATALOG, key=lambda item: item.capability)]


def catalog_sha256() -> str:
    return hashlib.sha256(canonical_json_bytes(catalog_rows())).hexdigest()


def _query_validator(keys: frozenset[str]) -> Callable[[Mapping[str, Any]], None]:
    def validate(value: Mapping[str, Any]) -> None:
        digits = all(
            str(value.get(key, "")).isdigit() and int(value[key]) > 0 for key in keys
        )
        if set(value) != keys or not digits:
            raise WebContractError("Portfolio123 query differs from the reviewed shape")

    return validate


def endpoint_contract(route: CaptureRoute) -> EndpointContract:
    """The contract entry this catalog row would become after route review."""

    if route.path is None:
        raise CaptureRefusal("CAPABILITY_HAS_NO_ROUTE", route.capability)
    return EndpointContract(
        capability=route.capability,
        method="GET",
        origin=ORIGIN,
        path=route.path,
        path_pattern=route.path_pattern,
        query_keys=route.query_keys,
        validate_query=_query_validator(route.query_keys) if route.query_keys else None,
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header="cookie",
    )


def har_routes() -> tuple[CaptureRoute, ...]:
    return tuple(item for item in CATALOG if item.path is not None)


def capture_contract() -> ProviderContract:
    """A deny-by-default contract holding only the uncovered, unverified routes."""

    return ProviderContract(
        provider="portfolio123",
        endpoints=tuple(endpoint_contract(item) for item in har_routes()),
        sensitive_fields=PORTFOLIO123_CONTRACT.sensitive_fields,
        api_fallback=False,
    )


def proposed_contract() -> ProviderContract:
    """Existing reviewed routes plus the proposed ones (nothing is enabled)."""

    return ProviderContract(
        provider="portfolio123",
        endpoints=PORTFOLIO123_CONTRACT.endpoints + capture_contract().endpoints,
        sensitive_fields=PORTFOLIO123_CONTRACT.sensitive_fields,
        api_fallback=False,
    )
