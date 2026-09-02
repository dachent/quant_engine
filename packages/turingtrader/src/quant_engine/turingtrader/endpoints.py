"""Registry of the governed TuringTrader public-token JSON endpoints."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote


HOST = "firebasestorage.googleapis.com"
BUCKET = "turingtrader-com.appspot.com"
SCHEMA_ID_PREFIX = (
    "https://github.com/dachent/quant_engine/schemas/providers/turingtrader-"
)

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One governed endpoint object and its capture contract."""

    object_name: str
    token_key: str
    carries_dates: bool

    @property
    def schema_name(self) -> str:
        return self.object_name.removesuffix(".json")

    @property
    def schema_filename(self) -> str:
        return f"turingtrader-{self.schema_name}.schema.json"

    @property
    def schema_id(self) -> str:
        return f"{SCHEMA_ID_PREFIX}{self.schema_name}.schema.json"


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("portfolio-comp.json", "public", carries_dates=False),
    Endpoint("performance-chart.json", "public", carries_dates=True),
    Endpoint("performance-table.json", "public", carries_dates=False),
    Endpoint("holdings-history.json", "allochist", carries_dates=True),
    Endpoint("portfolio-holdings.json", "holdings", carries_dates=False),
)


def get_endpoint(object_name: str) -> Endpoint:
    for endpoint in ENDPOINTS:
        if endpoint.object_name == object_name:
            return endpoint
    raise KeyError(f"unknown endpoint object: {object_name}")


def validate_slug(slug: str) -> str:
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(f"invalid strategy slug: {slug!r}")
    return slug


def _object_url(slug: str, endpoint: Endpoint) -> str:
    object_path = quote(f"{slug}/{endpoint.object_name}", safe="")
    return f"https://{HOST}/v0/b/{BUCKET}/o/{object_path}?alt=media"


def build_url(slug: str, endpoint: Endpoint, token: str) -> str:
    """Build the capture URL; the only place a token value enters a URL."""
    return f"{_object_url(validate_slug(slug), endpoint)}&token={token}"


def sanitized_url(slug: str, endpoint: Endpoint) -> str:
    """The persisted URL form; the ``token`` query parameter is stripped."""
    return _object_url(validate_slug(slug), endpoint)
