"""Flex credentials, only through the core credential resolver.

Configuration (``[credentials.ibkr]``) lists, per field, an ordered list of
``{source, name[, field]}`` candidates.  Fields used here:

* ``flex_token`` - the Flex Web Service token;
* one ``query_*`` field per catalog query a plan uses (the catalog's
  ``query_field``), holding that saved query's id.

Secret and vault NAMES are owner-supplied configuration; none appears in code.
Only the fields a plan needs are read (least privilege), after governance has
been consumed, and values are revealed only at the point of use.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quant_engine.core import (
    CredentialError,
    CredentialPlan,
    CredentialResolver,
    SecretValue,
    plan_from_config,
    sources_from_config,
)

from .catalog import get_query
from .plan import RequestPlan

PROVIDER = "ibkr"
TOKEN_FIELD = "flex_token"


def required_fields(plan: RequestPlan) -> tuple[str, ...]:
    fields = {TOKEN_FIELD}
    fields.update(get_query(item.query).query_field for item in plan.requests)
    return tuple(sorted(fields))


def resolve_flex_credentials(
    config: Mapping[str, Any],
    plan: RequestPlan,
    *,
    sources: Mapping[str, Any] | None = None,
) -> dict[str, SecretValue]:
    """Resolve exactly the credential fields ``plan`` needs."""

    declared = plan_from_config(config, PROVIDER)
    needed = required_fields(plan)
    missing = [field for field in needed if field not in declared.fields]
    if missing:
        raise CredentialError(
            f"{PROVIDER}: the credential plan does not declare {', '.join(missing)}"
        )
    narrowed = CredentialPlan(
        PROVIDER, {name: declared.fields[name] for name in needed}
    )
    active = dict(sources) if sources is not None else sources_from_config(config)
    return CredentialResolver(active).resolve(narrowed)


__all__ = [
    "CredentialError",
    "PROVIDER",
    "SecretValue",
    "TOKEN_FIELD",
    "required_fields",
    "resolve_flex_credentials",
]
