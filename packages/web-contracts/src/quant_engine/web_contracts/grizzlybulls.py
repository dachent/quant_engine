"""Reviewed GrizzlyBulls web contract, signal parsers, and sanitization audit.

Raw response bytes are the evidence. Everything here is a derived layer: it
validates shapes, parses signal rows, and proves what a sanitizer would change.
It does not call ``extract._sanitize`` for output because that function's
name-based and token-shaped masking can hide legitimate data; the audit below
reports every value it would have masked instead.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import io
import json
import re
from typing import Any, Final
from urllib.parse import urlsplit

from quant_engine.web_contracts.contracts import (
    EndpointContract,
    ProviderContract,
    SemanticEffect,
    WebContractError,
)
from quant_engine.web_contracts.extract import REDACTION, _sanitize


ORIGIN: Final = "https://grizzlybulls.com"
#: Routes the page code calls that carry identity, credentials, or billing. They
#: are never captured, never read, and never routed by the contract.
DENIED_PATHS: Final = frozenset(
    {"/api/user", "/api/login", "/api/logout", "/api/create-portal-link"}
)
SLUG_PATTERN: Final = r"[a-z0-9][a-z0-9-]{0,63}"
#: Account identity fields removed from the derived layer. Deliberately explicit:
#: no regex and no substring rule, so a data field is never removed by accident.
SENSITIVE_FIELDS: Final = frozenset({"email", "customer_id", "stripe_customer_id"})

#: Gated routes found in the model page's public JS chunk (2026-10-30). The page
#: sends a Firebase ID token in an Authorization header; the contract never sees it.
#: modelTradeHistory serves the "Download CSV" menu: "Blended history (backtest +
#: live)" is mode=blended, "Live history only" is mode=live.
HISTORY_MODES: Final = frozenset({"blended", "live"})

SIGNAL_VOCABULARY: Final = frozenset({"Buy", "Sell", "Rebuy", "Hypothetical Sell"})
LONG_ACTIONS: Final = frozenset({"Buy", "Rebuy"})


def _history_query(value: Mapping[str, Any]) -> None:
    if (
        set(value) != {"mode", "slug"}
        or re.fullmatch(SLUG_PATTERN, str(value["slug"])) is None
        or value["mode"] not in HISTORY_MODES
    ):
        raise WebContractError("history query differs from the reviewed shape")


def _historical_query(value: Mapping[str, Any]) -> None:
    if set(value) != {"slug"} or re.fullmatch(SLUG_PATTERN, str(value["slug"])) is None:
        raise WebContractError("historical query differs from the reviewed shape")


def _endpoint(
    capability: str,
    path: str,
    *,
    path_pattern: str | None = None,
    query_keys: frozenset[str] = frozenset(),
    validate_query: Any = None,
    authentication_header: str | None = None,
) -> EndpointContract:
    return EndpointContract(
        capability=capability,
        method="GET",
        origin=ORIGIN,
        path=path,
        path_pattern=path_pattern,
        query_keys=query_keys,
        validate_query=validate_query,
        semantic_effect=SemanticEffect.READ_ONLY,
        authentication_header=authentication_header,
    )


def build_contract() -> ProviderContract:
    """Return the deny-by-default contract; identity routes are never routed."""

    return ProviderContract(
        provider="grizzlybulls",
        endpoints=(
            _endpoint("models.catalog", "/api/models"),
            _endpoint(
                "models.index",
                "/_next/data/{build_id}/models.json",
                path_pattern=r"/_next/data/[^/]+/models\.json",
            ),
            _endpoint(
                "models.page",
                "/_next/data/{build_id}/models/{slug}.json",
                path_pattern=rf"/_next/data/[^/]+/models/{SLUG_PATTERN}\.json",
                query_keys=frozenset({"slug"}),
                validate_query=_historical_query,
            ),
            _endpoint(
                "models.history",
                "/api/modelTradeHistory",
                query_keys=frozenset({"mode", "slug"}),
                validate_query=_history_query,
                authentication_header="authorization",
            ),
            _endpoint(
                "models.historical",
                "/api/modelsHistorical",
                query_keys=frozenset({"slug"}),
                validate_query=_historical_query,
                authentication_header="authorization",
            ),
        ),
        sensitive_fields=SENSITIVE_FIELDS,
    )


GRIZZLYBULLS_CONTRACT: Final = build_contract()


@dataclass(frozen=True)
class SignalEvent:
    """One vendor signal row. ``moment`` is naive America/New_York wall time."""

    moment: datetime
    action: str
    price: Decimal | None

    @property
    def long(self) -> bool:
        return self.action in LONG_ACTIONS


@dataclass(frozen=True)
class SignalSeries:
    kind: str
    events: tuple[SignalEvent, ...]

    @property
    def first(self) -> datetime:
        return self.events[0].moment

    @property
    def last(self) -> datetime:
        return self.events[-1].moment


_US_DATE = re.compile(
    r"(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4}),? (?P<h>\d{1,2}):(?P<mi>\d{2}) (?P<ap>AM|PM)"
)
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}) ET")


def _moment(text: str) -> datetime:
    value = text.strip()
    try:
        matched = _US_DATE.fullmatch(value)
        if matched is not None:
            hour = int(matched["h"]) % 12 + (12 if matched["ap"] == "PM" else 0)
            return datetime(
                int(matched["y"]),
                int(matched["m"]),
                int(matched["d"]),
                hour,
                int(matched["mi"]),
            )
        iso = _ISO_DATE.fullmatch(value)
        if iso is not None:
            year, month, day, hour, minute = (int(part) for part in iso.groups())
            return datetime(year, month, day, hour, minute)
    except ValueError as exc:
        raise WebContractError("signal timestamp is not a real date") from exc
    raise WebContractError("signal timestamp has an unreviewed format")


def _price(text: str) -> Decimal | None:
    if text.strip() == "":
        return None
    try:
        value = Decimal(text.strip())
    except InvalidOperation as exc:
        raise WebContractError("signal price is not numeric") from exc
    if not value.is_finite() or value <= 0:
        raise WebContractError("signal price must be positive")
    return value


def _series(kind: str, rows: Sequence[tuple[str, str, str]]) -> SignalSeries:
    if not rows:
        raise WebContractError("signal source has no rows")
    events: list[SignalEvent] = []
    for stamp, action, price in rows:
        if action not in SIGNAL_VOCABULARY:
            raise WebContractError("signal action is outside the reviewed vocabulary")
        events.append(SignalEvent(_moment(stamp), action, _price(price)))
    for before, after in zip(events, events[1:]):
        if after.moment < before.moment:
            raise WebContractError("signal dates are not monotonic")
        if after.long == before.long:
            raise WebContractError("signal actions do not alternate long and flat")
    if "Hypothetical Sell" in {item.action for item in events[:-1]}:
        raise WebContractError("hypothetical signal is only valid as the final row")
    return SignalSeries(kind, tuple(events))


def _csv_rows(raw: bytes, header: Sequence[str]) -> list[list[str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WebContractError("signal CSV is not UTF-8") from exc
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows or rows[0] != list(header):
        raise WebContractError("signal CSV header differs from the reviewed shape")
    body = rows[1:]
    if any(len(row) != len(header) for row in body):
        raise WebContractError("signal CSV row width differs from the header")
    return body


def parse_live_trades_csv(raw: bytes) -> SignalSeries:
    """``Date (New York time),Trade,ES Price`` owner export."""

    rows = _csv_rows(raw, ("Date (New York time)", "Trade", "ES Price"))
    return _series("live-trades", [(r[0], r[1], r[2]) for r in rows])


_HISTORY_HEADER: Final = (
    "Date (ET)",
    "Action",
    "SPX",
    "Prev Price",
    "Profit / Loss",
    "PnL %",
    "Model",
    "Buy & Hold",
)


def parse_trade_history_csv(raw: bytes) -> SignalSeries:
    """``Date (ET),Action,SPX,...`` live and blended history exports."""

    rows = _csv_rows(raw, _HISTORY_HEADER)
    return _series("trade-history", [(r[0], r[1], r[2]) for r in rows])


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WebContractError(f"{name} must be an object")
    return value


def _summary(value: object) -> None:
    if (
        not isinstance(value, list)
        or not value
        or value[0]
        != [
            "Summary",
            "Value",
            "Notes",
        ]
    ):
        raise WebContractError("summaryData header differs from the reviewed shape")
    for row in value:
        # The published table carries one empty spacer row.
        if (
            not isinstance(row, list)
            or len(row) not in {0, 2, 3}
            or not all(isinstance(cell, str) for cell in row)
        ):
            raise WebContractError("summaryData row differs from the reviewed shape")


def parse_models_index(body: object) -> tuple[str, ...]:
    """``/models`` SSG page: plans plus one summary table per model slug."""

    props = _object(_object(body, "page").get("pageProps"), "pageProps")
    plans = props.get("plans")
    if not isinstance(plans, list) or not plans:
        raise WebContractError("plans must be a nonempty array")
    for plan in plans:
        record = _object(plan, "plan")
        if not isinstance(record.get("key"), str) or not isinstance(
            record.get("prices"), list
        ):
            raise WebContractError("plan differs from the reviewed shape")
    summaries = _object(props.get("summaryData"), "summaryData")
    if not summaries:
        raise WebContractError("summaryData must name at least one model")
    for slug, table in summaries.items():
        if re.fullmatch(SLUG_PATTERN, slug) is None:
            raise WebContractError("summaryData slug is invalid")
        _summary(table)
    return tuple(sorted(summaries))


def parse_model_page(body: object) -> SignalSeries:
    """Next.js SSG page ``pageProps.sheetData`` chart table."""

    props = _object(_object(body, "page").get("pageProps"), "pageProps")
    _summary(props.get("summaryData"))
    identity = props.get("slug", props.get("key"))
    if not isinstance(identity, str) or not isinstance(props.get("doc"), str):
        raise WebContractError("page slug and doc must be text")
    return _sheet_series(props.get("sheetData"), "model-page")


def parse_historical(body: object) -> SignalSeries:
    """``/api/modelsHistorical``: JSON object carrying ``sheetData``."""

    return _sheet_series(_object(body, "historical").get("sheetData"), "historical")


def _sheet_series(sheet: object, kind: str) -> SignalSeries:
    if not isinstance(sheet, list) or not sheet:
        raise WebContractError("sheetData must be a nonempty array")
    header = sheet[0]
    if not isinstance(header, list) or header[1:] != list(_HISTORY_HEADER[1:]):
        raise WebContractError("sheetData header differs from the reviewed shape")
    rows: list[tuple[str, str, str]] = []
    for row in sheet[1:]:
        # The public page ships text cells; the signed-in page ships numbers.
        if (
            not isinstance(row, list)
            or len(row) != len(_HISTORY_HEADER)
            or not all(
                isinstance(cell, (str, int, float)) and not isinstance(cell, bool)
                for cell in row
            )
            or not isinstance(row[0], str)
            or not isinstance(row[1], str)
        ):
            raise WebContractError("sheetData row differs from the reviewed shape")
        rows.append((row[0], row[1], str(row[2])))
    return _series(kind, rows)


@dataclass(frozen=True)
class ModelEntry:
    slug: str
    privilege: str
    signal: str | None


def parse_models_catalog(body: object) -> tuple[ModelEntry, ...]:
    """``/api/models``: array of models; ``signal`` appears only when entitled."""

    if not isinstance(body, list) or not body:
        raise WebContractError("models catalog must be a nonempty array")
    entries: list[ModelEntry] = []
    for item in body:
        record = _object(item, "model")
        slug, privilege, signal = (
            record.get("slug"),
            record.get("privilege"),
            record.get("signal"),
        )
        if not isinstance(slug, str) or re.fullmatch(SLUG_PATTERN, slug) is None:
            raise WebContractError("model slug is invalid")
        if not isinstance(privilege, str) or not privilege:
            raise WebContractError("model privilege is invalid")
        if signal is not None and not isinstance(signal, str):
            raise WebContractError("model signal must be text when present")
        entries.append(ModelEntry(slug, privilege, signal))
    if len({entry.slug for entry in entries}) != len(entries):
        raise WebContractError("model slugs are not unique")
    return tuple(entries)


def parse_json_bytes(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebContractError("response body is not valid UTF-8 JSON") from exc


def derive_sanitized(
    value: object, sensitive_fields: frozenset[str] = SENSITIVE_FIELDS
) -> object:
    """Remove only the explicitly named identity fields; never mask by pattern."""

    if isinstance(value, Mapping):
        return {
            key: derive_sanitized(item, sensitive_fields)
            for key, item in value.items()
            if str(key).casefold() not in sensitive_fields
        }
    if isinstance(value, list):
        return [derive_sanitized(item, sensitive_fields) for item in value]
    return value


def _walk(value: object, path: str = "") -> Iterator[tuple[str, object]]:
    yield path, value
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk(item, f"{path}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


def sanitization_audit(
    raw: object, sensitive_fields: frozenset[str] = SENSITIVE_FIELDS
) -> dict[str, object]:
    """Raw-vs-sanitized diff plus the paths ``extract._sanitize`` would mask.

    ``data_fields_masked`` must be empty: every difference between raw and the
    derived layer has to be an explicitly named sensitive field.
    """

    derived = derive_sanitized(raw, sensitive_fields)
    raw_paths = dict(_walk(raw))
    derived_paths = dict(_walk(derived))
    removed = sorted(path for path in raw_paths if path not in derived_paths)
    explicit_roots = [
        path
        for path in removed
        if path.rsplit("/", 1)[-1].casefold() in sensitive_fields
    ]
    masked = sorted(
        path
        for path, value in derived_paths.items()
        if not isinstance(value, (Mapping, list)) and raw_paths.get(path) != value
    )
    try:
        stock = dict(_walk(_sanitize(raw, sensitive_fields)))
    except WebContractError as exc:
        stock = {}
        stock_error = str(exc)
    else:
        stock_error = ""
    regex_hits = sorted(
        path
        for path, value in stock.items()
        if value == REDACTION and raw_paths.get(path) != REDACTION
    )
    return {
        "data_fields_masked": masked,
        "explicitly_removed": explicit_roots,
        "stock_sanitizer_would_mask": regex_hits,
        "stock_sanitizer_error": stock_error,
        "unexplained_removals": [
            path
            for path in removed
            if not any(path.startswith(root) for root in explicit_roots)
        ],
    }


def classify(
    method: str, url: str, contract: ProviderContract
) -> tuple[str, str | None]:
    """Return (verdict, capability): offorigin, denied, routed, or unrouted."""

    parsed = urlsplit(url)
    if f"{parsed.scheme}://{parsed.netloc}" != ORIGIN:
        return "offorigin", None
    if parsed.path.rstrip("/") in DENIED_PATHS:
        return "denied", None
    try:
        endpoint = contract.route(method, url)
        if endpoint.validate_query is not None:
            endpoint.validate_query(endpoint.query(url))
    except WebContractError:
        return "unrouted", None
    return "routed", endpoint.capability


def summarize_capability(capability: str, raw: bytes) -> dict[str, object]:
    """Parse one raw body for its capability and return a value-free summary."""

    series: SignalSeries
    if capability == "models.catalog":
        entries = parse_models_catalog(parse_json_bytes(raw))
        return {
            "model_count": len(entries),
            "signal_present": sorted(e.slug for e in entries if e.signal is not None),
        }
    if capability == "models.index":
        return {"slugs": list(parse_models_index(parse_json_bytes(raw)))}
    if capability == "models.page":
        series = parse_model_page(parse_json_bytes(raw))
    elif capability == "models.historical":
        series = parse_historical(parse_json_bytes(raw))
    elif capability == "models.history":
        series = parse_trade_history_csv(raw)
    else:
        raise WebContractError("capability has no reviewed parser")
    actions: dict[str, int] = {}
    for event in series.events:
        actions[event.action] = actions.get(event.action, 0) + 1
    return {
        "actions": dict(sorted(actions.items())),
        "first": series.first.isoformat(),
        "kind": series.kind,
        "last": series.last.isoformat(),
        "row_count": len(series.events),
    }
