"""Allow-list catalog of the Alpaca endpoints the capture driver may read.

Only entries with ``live_enabled`` may ever reach a transport, and every such
entry is a documented ``GET`` on the Trading API host family.  The catalog also
lists excluded trading, order, and transfer routes so the policy is auditable
and so :func:`catalog_sha256` binds the governance evidence to the exact policy
reviewed.  Nothing outside the catalog is reachable: unknown paths are refused,
not forwarded.

The route shapes below come from the Alpaca documentation referenced in
``docs/alpaca-integration.md``; none was observed live (UNVERIFIED).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Final

from quant_engine.alpaca.capture.errors import CaptureRefused
from quant_engine.alpaca.capture.util import canonical_json, sha256_text

LIVE_BASE_URL: Final[str] = "https://api.alpaca.markets"
PAPER_BASE_URL: Final[str] = "https://paper-api.alpaca.markets"
ENVIRONMENTS: Final[Mapping[str, str]] = {
    "live": LIVE_BASE_URL,
    "paper": PAPER_BASE_URL,
}

PAGE_CURSOR: Final[str] = "page" + "_token"

# Any path segment that names a trading, order, or money-movement surface is
# refused with a specific reason before the exact-match allow-list is consulted.
FORBIDDEN_SEGMENTS: Final[frozenset[str]] = frozenset(
    {
        "orders",
        "order",
        "transfers",
        "transfer",
        "journals",
        "journal",
        "ach_relationships",
        "recipient_banks",
        "wire",
        "withdrawals",
        "deposits",
        "exercise",
        "close_all",
        "batch",
    }
)
_FORBIDDEN_PATH_CHARACTERS: Final[str] = "?#%\\ \t\r\n"


@dataclass(frozen=True, slots=True)
class Endpoint:
    name: str
    method: str
    path: str
    allowed_params: frozenset[str]
    required_params: frozenset[str]
    pagination: str
    report: str
    safety_class: str
    disposition: str
    live_enabled: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if self.method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError(f"unsupported catalog method: {self.method}")
        if not self.path.startswith("/"):
            raise ValueError(f"catalog path must be absolute: {self.path}")
        if not self.required_params <= self.allowed_params:
            raise ValueError(f"required params are not allowlisted: {self.name}")
        if self.live_enabled and (
            self.method != "GET" or self.safety_class != "read_only"
        ):
            raise ValueError(f"unsafe endpoint cannot be live-enabled: {self.name}")
        if self.live_enabled and not self.report:
            raise ValueError(f"live endpoint needs a report id: {self.name}")


def _read(
    name: str,
    path: str,
    report: str,
    params: Iterable[str] = (),
    pagination: str = "none",
    note: str = "",
) -> Endpoint:
    return Endpoint(
        name=name,
        method="GET",
        path=path,
        allowed_params=frozenset(params),
        required_params=frozenset(),
        pagination=pagination,
        report=report,
        safety_class="read_only",
        disposition="live_read",
        live_enabled=True,
        note=note,
    )


def _excluded(
    name: str, method: str, path: str, safety: str, disposition: str
) -> Endpoint:
    return Endpoint(
        name=name,
        method=method,
        path=path,
        allowed_params=frozenset(),
        required_params=frozenset(),
        pagination="none",
        report="",
        safety_class=safety,
        disposition=disposition,
    )


CATALOG: Final[tuple[Endpoint, ...]] = (
    _read("account", "/v2/account", "account"),
    _read("positions", "/v2/positions", "positions"),
    _read(
        "activities",
        "/v2/account/activities",
        "activities",
        params=(
            "activity_types",
            "after",
            "until",
            "direction",
            "page_size",
            PAGE_CURSOR,
        ),
        pagination="cursor",
        note="cursor is the id of the last row of the previous page",
    ),
    _excluded("order-submit", "POST", "/v2/orders", "trading", "excluded_trading"),
    _excluded(
        "order-cancel-all", "DELETE", "/v2/orders", "trading", "excluded_trading"
    ),
    _excluded(
        "order-cancel", "DELETE", "/v2/orders/{order_id}", "trading", "excluded_trading"
    ),
    _excluded("order-list", "GET", "/v2/orders", "read_only", "excluded_out_of_scope"),
    _excluded(
        "position-close-all", "DELETE", "/v2/positions", "trading", "excluded_trading"
    ),
    _excluded(
        "position-close",
        "DELETE",
        "/v2/positions/{symbol}",
        "trading",
        "excluded_trading",
    ),
    _excluded(
        "position-exercise",
        "POST",
        "/v2/positions/{symbol}/exercise",
        "trading",
        "excluded_trading",
    ),
    _excluded(
        "transfer-create",
        "POST",
        "/v1/accounts/{account_id}/transfers",
        "transfer",
        "excluded_transfer",
    ),
    _excluded(
        "journal-create", "POST", "/v1/journals", "transfer", "excluded_transfer"
    ),
    _excluded(
        "ach-relationship-create",
        "POST",
        "/v1/accounts/{account_id}/ach_relationships",
        "transfer",
        "excluded_transfer",
    ),
    _excluded(
        "portfolio-history",
        "GET",
        "/v2/account/portfolio/history",
        "read_only",
        "excluded_out_of_scope",
    ),
    _excluded(
        "watchlists", "GET", "/v2/watchlists", "read_only", "excluded_out_of_scope"
    ),
)


def catalog_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for endpoint in CATALOG:
        row: dict[str, object] = dict(asdict(endpoint))
        row["allowed_params"] = sorted(endpoint.allowed_params)
        row["required_params"] = sorted(endpoint.required_params)
        rows.append(row)
    return rows


def catalog_sha256() -> str:
    """Deterministic identity of the reviewed catalog, bound by governance."""

    return sha256_text(canonical_json(catalog_rows()))


def live_endpoints() -> tuple[Endpoint, ...]:
    return tuple(endpoint for endpoint in CATALOG if endpoint.live_enabled)


def endpoint_key(endpoint: Endpoint) -> str:
    return f"{endpoint.method} {endpoint.path}"


def get_live_endpoint(path: str) -> Endpoint:
    for endpoint in live_endpoints():
        if endpoint.path == path:
            return endpoint
    raise CaptureRefused("the request path is not in the read-only allow-list")


def authorize_request(
    method: str, path: str, query: Mapping[str, str] | None = None
) -> Endpoint:
    """Return the live endpoint for a request, or refuse it.

    Refusal order: non-GET method, malformed path, trading/order/transfer
    surface, path outside the allow-list, then query parameters outside the
    endpoint's declared set.
    """

    if not isinstance(method, str) or method.upper() != "GET":
        raise CaptureRefused("only the GET method is permitted")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or any(ch in path for ch in _FORBIDDEN_PATH_CHARACTERS)
        or "//" in path
        or ".." in path
        or not path.isascii()
    ):
        raise CaptureRefused("the request path is malformed")
    segments = {segment.lower() for segment in path.split("/") if segment}
    if segments & FORBIDDEN_SEGMENTS:
        raise CaptureRefused("trading, order, and transfer routes are forbidden")
    endpoint = get_live_endpoint(path)
    parameters = dict(query or {})
    if not set(parameters) <= endpoint.allowed_params:
        raise CaptureRefused("the request carries a parameter outside the catalog")
    if not endpoint.required_params <= set(parameters):
        raise CaptureRefused("the request lacks a required parameter")
    return endpoint
