"""Catalog of Portfolio123 captures that the reviewed web contract does not cover.

STATUS: each row carries a ``verdict`` and a ``finding``.  A ``CONFIRMED`` row
was compared offline with preserved real bytes (2026-09 captures: route template,
method, query keys, response field names and types, never values or identifiers).
An ``UNVERIFIED`` row is still an invented placeholder: no preserved evidence
contains its response, so it exists only so that parsers, staging, and
verification can be tested against synthetic fixtures and it can never describe
a real export.  ``finding`` records how the original placeholder compared with
the evidence: ``CONFIRMED`` (matched), ``DIFFERS`` (contradicted by the evidence;
a confirmed row was corrected, an unverified row stays fail-closed), or
``UNVERIFIED`` (the evidence is silent).  A run is marked ``REVIEWED_SHAPES``
only when every capability in its scope is ``CONFIRMED``.

An owner-supplied HAR is first inventoried (route templates only), the real
routes are then reviewed, and only after that review does a catalog entry become
a contract entry.  The catalog never enables a route by itself: it is
contract-ready (:func:`capture_contract` and :func:`proposed_contract` build
``ProviderContract`` objects) and has no effect on ``PORTFOLIO123_CONTRACT``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from quant_engine.portfolio123.capture_html import (
    ROWS_PER_PAGE,
    TRANSACTIONS_TABLE,
    TYPES,
    HtmlTable,
)
from quant_engine.portfolio123.capture_shapes import (
    Choice,
    Obj,
    Seq,
    ShapeError,
    Spec,
    canonical_bytes,
    describe,
    validate_shape,
)
from quant_engine.web_contracts import (
    PORTFOLIO123_CONTRACT,
    EndpointContract,
    ProviderContract,
    SemanticEffect,
    WebContractError,
)


ORIGIN = "https://www.portfolio123.com"
VERDICT_CONFIRMED = "CONFIRMED"
VERDICT_UNVERIFIED = "UNVERIFIED"
FINDINGS = ("CONFIRMED", "DIFFERS", "UNVERIFIED")
ROUTE_STATUS = {
    VERDICT_CONFIRMED: "CONFIRMED_PRESERVED_BYTES",
    VERDICT_UNVERIFIED: "UNVERIFIED_PLACEHOLDER",
}
SHAPE_STATUS = {
    VERDICT_CONFIRMED: "CONFIRMED_PRESERVED_BYTES",
    VERDICT_UNVERIFIED: "UNVERIFIED_SYNTHETIC",
}
EVIDENCE_REVIEWED = "REVIEWED_SHAPES"
EVIDENCE_UNVERIFIED = "UNVERIFIED_SYNTHETIC_SHAPES"
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
    verdict: str = VERDICT_UNVERIFIED
    finding: str = "UNVERIFIED"
    evidence: str = ""
    query_values: tuple[tuple[str, str], ...] = ()
    redact: frozenset[str] = frozenset()
    html: HtmlTable | None = None

    def __post_init__(self) -> None:
        if self.verdict not in ROUTE_STATUS or self.finding not in FINDINGS:
            raise CaptureRefusal("CATALOG_INVALID", "verdict")
        if self.verdict == VERDICT_CONFIRMED and self.finding == "UNVERIFIED":
            raise CaptureRefusal("CATALOG_INVALID", "confirmed row without a finding")
        if not self.evidence.strip():
            raise CaptureRefusal("CATALOG_INVALID", "evidence note")
        if not {key for key, _ in self.query_values} <= self.query_keys:
            raise CaptureRefusal("CATALOG_INVALID", "query value for an unknown key")
        if not self.forms or not set(self.forms) <= {FORM_HAR, FORM_FILE}:
            raise CaptureRefusal("CATALOG_INVALID", "forms")
        if FORM_HAR in self.forms and (self.path is None or self.shape is None):
            raise CaptureRefusal("CATALOG_INVALID", "har route needs path and shape")
        if FORM_FILE in self.forms and not self.file_media:
            raise CaptureRefusal("CATALOG_INVALID", "file route needs media types")
        if not set(self.file_media) <= set(MEDIA_TYPES):
            raise CaptureRefusal("CATALOG_INVALID", "media type")
        if self.html is not None and (
            FORM_HAR not in self.forms
            or self.shape is None
            or not self.paginated
            or self.html.page_param not in self.query_keys
            or self.html.id_param not in self.query_keys
        ):
            raise CaptureRefusal("CATALOG_INVALID", "html route")
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
            "verdict": self.verdict,
            "finding": self.finding,
            "evidence": self.evidence,
            "query_values": [list(item) for item in self.query_values],
            "redact": sorted(self.redact),
            "html": None if self.html is None else self.html.describe(),
            "route_status": ROUTE_STATUS[self.verdict],
            "shape_status": SHAPE_STATUS[self.verdict],
        }


_PREDICTION = Obj({"ticker": "str", "score": "num", "rank": "int"})
_FEATURE = Obj({"name": "str", "weight": "num"})
_NAV_POINT = Obj({"date": "date", "value": "num"})
_RETURN_STATS = Obj(
    {
        "downDev": "num",
        "maxDd": "num",
        "sharpe": "num",
        "sortino": "num",
        "stdDev": "num",
    }
)
_WINDOW_STATS = Obj(
    {
        "annRet": "num",
        "annRetBench": "num",
        "bench": _RETURN_STATS,
        "beta": "num",
        "model": _RETURN_STATS,
    }
)
_TRADE_STATS = Obj(
    {"biggest": "num", "daysAvg": "num", "retAvg": "num", "trades": "int"}
)
_DESIGNER_REDACT = frozenset({"designerinfo", "comments"})
_DESIGNER_MODEL = Obj(
    {
        "backtestDisabled": "bool",
        "benchmark": "str",
        "benchmarkUid": "int",
        "comments": "str",
        "context": "null",
        "currency": "str",
        "desc": "str",
        "designerModel": Obj(
            {
                "attachment": "str",
                "backtestAgreement": "bool",
                "cost": "num",
                "designerInfo": "str",
                "img": "str",
                "isLiveBookAsset": "bool",
                "isStrategyAsset": "bool",
                "maxSubs": "int",
                "okmsg": "null",
                "pUid": "int",
                "revisions": Seq(
                    Obj(
                        {
                            "insert_dt": "us_date",
                            "reason": "str",
                            "revision_uid": "int",
                            "update_dt": "date",
                        }
                    )
                ),
                "status": "str",
                "subs": "int",
                "subscription": Obj(
                    {
                        "cancelled": "bool",
                        "nextBillDt": "us_date",
                        "portfolioUid": "int",
                        "startDt": "date",
                        "type": "str",
                    }
                ),
            }
        ),
        "editable": "bool",
        "endDt": "us_date",
        "hasRebalTran": "int",
        "hedgeEnabled": "bool",
        "holdingsAvg": "num",
        "inceptionDt": "us_date",
        "lastRebal": "us_date",
        "lastRebalanceToday": "bool",
        "launchDt": "us_date",
        "liquidationValue": "num",
        "maxdd90": "num",
        "mkttypeUid": "int",
        "name": "str",
        "pctHedgeAvg": "num",
        "portUid": "int",
        "rebalFreq": "str",
        "refLiquidity": "num",
        "riskScore": "int",
        "stats3y": _WINDOW_STATS,
        "statsInception": _WINDOW_STATS,
        "statsLaunch": _WINDOW_STATS,
        "topData": Seq(Obj({"label": "str", "tooltip": "int?", "value": "str"})),
        "tradesTtm": "int",
        "tradingAll": Obj(
            {"biggest": "num?", "daysAvg": "num", "retAvg": "num", "trades": "int"}
        ),
        "tradingCommitment": "int",
        "tradingLose": _TRADE_STATS,
        "tradingWin": _TRADE_STATS,
        "trailingReturns": Seq(
            Obj({"name": "str", "values": Seq("num", min_items=2, max_items=2)})
        ),
        "volatility90": "num",
    }
)

# Normalized page of the strategy transactions HTML list (the parser's output).  The
# row fields are the REAL table columns (Date, Type, Ticker with its market suffix,
# Shares, Price, Amount, Tot Fees, Rank, Note); the first column (a row sequence
# number), the TRADE column, every link, and the strategy id are checked and dropped.
_TRANSACTION_ROW = Obj(
    {
        "date": "date",
        "type": Choice(TYPES),
        "ticker": "str",
        "market": "str",
        "shares": "num?",
        "price": "num?",
        "amount": "num?",
        "tot_fees": "num?",
        "rank": "num?",
        "note": "str",
    }
)
_TRANSACTIONS_PAGE = Obj(
    {
        "strategy_sha256": "str",
        "offset": "int",
        "first": "int",
        "last": "int",
        "total": "int",
        "rows": Seq(_TRANSACTION_ROW, min_items=1, max_items=ROWS_PER_PAGE),
    }
)

# REAL header of the strategy transactions CSV download (CONFIRMED: one identical
# header across ten preserved captures).
_TRANSACTIONS_HEADER = (
    "Date",
    "Symbol",
    "Type",
    "Shares",
    "Price",
    "Amount",
    "TotFees",
    "Rank",
    "Notes",
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
        evidence=(
            "No preserved capture contains an AI Factor page or response, so route and "
            "shape are an invented placeholder. Only the site navigation was observed: "
            "the AI Factor list opens at /sv/opener/AIFACTOR/{cat} (a Svelte page), and "
            "the signed-in user object carries the optional flags aiFactorEnabled and "
            "hasAiFactor. The p123api operation name has no reviewed source."
        ),
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
        evidence=(
            "No preserved capture contains an AI Factor model page or response (the "
            "2026-10-16 design intake records the predictor definition as in no "
            "capture). Route and shape are an invented placeholder."
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
        evidence=(
            "No preserved request or response for a screen. Observed only in page "
            "links: the screen list /app/opener/SCR, and a copy-to-screen action link "
            "under /app/screen/summary/{n}/revert that creates a screen and must never "
            "be captured or followed. The universe page /app/universe/summary/{n} is a "
            "sibling family. Route and shape remain an invented placeholder."
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
        evidence=(
            "No JSON simulation response preserved. Observed: POST /port_simul6.jsp "
            "(an HTML settings page, status 200, many requests) and the list link "
            "/app/opener/SIM. The NAV curve of a live book arrives as JSON from "
            "GET /app/investment/perf-data/{n}?init= (columns of a data array, not the "
            "invented nav object), which is a different capability. Route and shape "
            "remain an invented placeholder."
        ),
    ),
    CaptureRoute(
        "transactions.account",
        "Strategy transactions list (HTML)",
        (FORM_HAR,),
        "Open the transactions tab, page to the end (50 rows per page), export the HAR.",
        path="/transact_list.jsp",
        path_pattern=r"/transact_list\.jsp",
        query_keys=frozenset({"portid", "offset"}),
        shape=_TRANSACTIONS_PAGE,
        paginated=True,
        html=TRANSACTIONS_TABLE,
        finding="DIFFERS",
        evidence=(
            "Route GET /transact_list.jsp?portid=&offset= and the table structure were "
            "read from eight preserved HTML responses of one strategy (status 200, "
            "text/html, request carries a session header, offsets 1400 to 1750 step "
            "50): a pager row (position text, total count, page links, a page-size "
            "selector fixed at 50, a download link), a header row Date, Type, Ticker, "
            "Shares, Price, Amount, Tot Fees, Rank, Note, TRADE after a blank first "
            "column, and 50 data rows of 11 cells (date link, BUY/SELL/DIV/SPLIT label, "
            "ticker box with a market suffix, comma-grouped numbers that are empty on "
            "dividends and splits, free-text note, empty TRADE cell). All eight parse "
            "and agree with the pager. The first page may omit offset. NOT observed, "
            "so the verdict stays UNVERIFIED and a run is never REVIEWED_SHAPES: the "
            "last page of a strategy (a short page whose range ends at the total), a "
            "strategy with no transactions, other strategies, other transaction types, "
            "a non-empty TRADE cell. The parser accepts a short last page that agrees "
            "with its pager and refuses the rest; the verdict flips to CONFIRMED when a "
            "preserved capture shows a last page. The HTML header differs from the CSV download header "
            "(Ticker, Tot Fees, Note versus Symbol, TotFees, Notes). Admission is "
            "through the fail-closed parser in capture_html, not the JSON extractor."
        ),
    ),
    CaptureRoute(
        "transactions.download",
        "Transactions download",
        (FORM_FILE,),
        "Use the transactions download control and save the file unmodified.",
        file_media=("csv",),
        csv_columns=(_TRANSACTIONS_HEADER,),
        verdict=VERDICT_CONFIRMED,
        finding="DIFFERS",
        evidence=(
            "Header taken from ten preserved downloads of the reviewed route "
            "GET /p123/DownloadPortTrans?portid= (strategy.transactions): identical "
            "nine columns Date, Symbol, Type, Shares, Price, Amount, TotFees, Rank, "
            "Notes. The placeholder header (Ticker, Action, Commission) differed."
        ),
    ),
    CaptureRoute(
        "designer-model.json",
        "Designer model definition",
        (FORM_HAR, FORM_FILE),
        "Export the designer model JSON, or open the model page and export the HAR.",
        path="/spr/strategy/model/{model_id}",
        path_pattern=r"/spr/strategy/model/[1-9][0-9]*",
        query_keys=frozenset({"dm"}),
        query_values=(("dm", "true"),),
        shape=_DESIGNER_MODEL,
        file_media=("json",),
        verdict=VERDICT_CONFIRMED,
        finding="DIFFERS",
        redact=_DESIGNER_REDACT,
        evidence=(
            "Route, method, and the single query key dm=true from the recorded request "
            "of the 2026-09-12 capture; shape from the preserved response (two "
            "byte-identical copies). One model, one capture: every field is required, "
            "optional or nullable fields are not distinguishable, and the status was "
            "not stored (a parsed JSON body implies success). Dates are MM/DD/YYYY. "
            "The placeholder route (/app/investment/model), shape, and date format "
            "all differed. The designer profile and comments are third-party personal "
            "data and are redacted. The sibling performance route .../perf is a "
            "separate capability, not catalogued."
        ),
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
        evidence=(
            "No preserved credit-table page or response. Observed: the account menu "
            "links /sv/account-settings/billing and /sv/account-settings/dataminer-api "
            "(Svelte pages), and the signed-in user object (GET /spr/user) carries an "
            "integer apiQuota and a boolean hasApiKey whose meaning (cap or remaining) "
            "is not established. Route and shape remain an invented placeholder."
        ),
    ),
    CaptureRoute(
        "billing.invoice",
        "Billing statement or invoice",
        (FORM_FILE,),
        "Download each statement or invoice from the billing page, unmodified.",
        file_media=("csv", "pdf"),
        csv_columns=(("Invoice Date", "Description", "Amount", "Currency"),),
        evidence=(
            "No preserved statement or invoice file; the CSV columns are an invented "
            "placeholder."
        ),
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
    return hashlib.sha256(canonical_bytes(catalog_rows())).hexdigest()


def _query_validator(
    keys: frozenset[str], literals: tuple[tuple[str, str], ...] = ()
) -> Callable[[Mapping[str, Any]], None]:
    fixed = dict(literals)

    def accepted(key: str, item: object) -> bool:
        text = str(item)
        if key in fixed:
            return text == fixed[key]
        return text.isdigit() and text.isascii() and int(text) > 0

    def validate(value: Mapping[str, Any]) -> None:
        if set(value) != keys or not all(accepted(key, value[key]) for key in keys):
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
        validate_query=(
            _query_validator(route.query_keys, route.query_values)
            if route.query_keys
            else None
        ),
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header="cookie",
    )


def har_routes() -> tuple[CaptureRoute, ...]:
    """Routes the JSON extractor may admit (HTML routes are admitted by capture_html)."""

    return tuple(
        item for item in CATALOG if item.path is not None and item.html is None
    )


def html_routes() -> tuple[CaptureRoute, ...]:
    return tuple(item for item in CATALOG if item.html is not None)


def match_html_route(method: str, url: str) -> tuple[CaptureRoute, str, int] | None:
    """Match a request to a reviewed HTML route: (route, strategy id, offset).

    The query must be exactly ``portid`` or ``portid`` plus ``offset``, each once;
    the first page may omit ``offset``.  Anything else is not a reviewed request.
    """

    parsed = urlsplit(url)
    if method.upper() != "GET" or f"{parsed.scheme}://{parsed.netloc}" != ORIGIN:
        return None
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = {key.casefold(): value for key, value in pairs}
    if parsed.fragment or len(query) != len(pairs):
        return None
    for route in html_routes():
        table = route.html
        if table is None or route.path_pattern is None:
            continue
        if re.fullmatch(route.path_pattern, parsed.path) is None:
            continue
        strategy = query.get(table.id_param, "")
        offset = query.get(table.page_param, "0")
        if (
            set(query) - {table.page_param} != {table.id_param}
            or re.fullmatch(r"[1-9][0-9]{0,11}", strategy) is None
            or re.fullmatch(r"[0-9]{1,9}", offset) is None
        ):
            return None
        return route, strategy, int(offset)
    return None


def capture_contract() -> ProviderContract:
    """A deny-by-default contract holding only the uncovered, unverified routes."""

    return ProviderContract(
        provider="portfolio123",
        endpoints=tuple(endpoint_contract(item) for item in har_routes()),
        sensitive_fields=redacted_fields(),
        api_fallback=False,
    )


def redacted_fields() -> frozenset[str]:
    """Reviewed sensitive fields plus the capture-specific redactions."""

    extra = frozenset(key for item in CATALOG for key in item.redact)
    return PORTFOLIO123_CONTRACT.sensitive_fields | extra


def is_confirmed(capability: str) -> bool:
    return route_for(capability).verdict == VERDICT_CONFIRMED


def evidence_status(scope: Sequence[str]) -> str:
    """``REVIEWED_SHAPES`` only when every capability in scope is CONFIRMED."""

    if scope and all(is_confirmed(item) for item in scope):
        return EVIDENCE_REVIEWED
    return EVIDENCE_UNVERIFIED


def verify_confirmed_shape(capability: str, response: object) -> None:
    """Check a normalized response against the CONFIRMED catalog shape, now.

    Raises ``CaptureRefusal`` with ``CAPABILITY_UNKNOWN``, ``SHAPE_NOT_REVIEWED``
    (the row is not CONFIRMED), or ``SHAPE_REFUSED``.  Consumers outside the package
    (the provenance admission) use this instead of trusting a manifest's own claims.
    """

    route = route_for(capability)
    if route.verdict != VERDICT_CONFIRMED or route.shape is None:
        raise CaptureRefusal("SHAPE_NOT_REVIEWED", capability)
    try:
        validate_shape(response, route.shape)
    except ShapeError:
        raise CaptureRefusal("SHAPE_REFUSED", capability) from None


def findings() -> dict[str, tuple[str, str]]:
    """Per capability: (finding about the original placeholder, current verdict)."""

    return {item.capability: (item.finding, item.verdict) for item in CATALOG}


def proposed_contract() -> ProviderContract:
    """Existing reviewed routes plus the proposed ones (nothing is enabled)."""

    return ProviderContract(
        provider="portfolio123",
        endpoints=PORTFOLIO123_CONTRACT.endpoints + capture_contract().endpoints,
        sensitive_fields=redacted_fields(),
        api_fallback=False,
    )
