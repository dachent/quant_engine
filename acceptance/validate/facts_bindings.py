"""Fail-closed binding of a sleeve-facts file to the ledger run that consumes it (#624).

A ``sleeve-account-facts`` artifact binds the provider generation, the account rows and
the tag map it was built from.  A consumer must not take a facts file whose bindings are
not the ones its own run holds: an oracle file built from a scratch candidate generation
(R2, id ``f65c8ddb``) must be refused against the ledger of another lineage.  This module
recomputes every binding from the consumer's own sources and refuses on any difference:

* ``parent_ledger_generation_id`` against the selected provider ledger's
  ``provider_generations`` row of the facts' provider (exactly one row);
* the IBKR catalog read from that generation (its directory name, ``generation.json``
  artifact digest and provider are checked) must list the facts' parent account;
* ``account_rows_sha256`` against the SHA-256 this module computes over the same catalog
  rows (transactions, positions, change-in-NAV of the parent inside the facts' window);
* ``tag_map_sha256`` against the canonical-JSON SHA-256 of the tag map the consumer holds.

A missing comparison side is a refusal, never "not comparable".

Imports stdlib and ``sleeve_basis`` (for ``Refusal``) only; the row selection and the tag
map digest are written here, not imported from the facts builder.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any

from .sleeve_basis import Refusal

BINDING_MISSING_SIDE = "BINDING_MISSING_SIDE"
BINDING_LEDGER_GENERATION = "BINDING_LEDGER_GENERATION"
BINDING_CATALOG = "BINDING_CATALOG"
BINDING_PARENT_ACCOUNT = "BINDING_PARENT_ACCOUNT"
BINDING_ACCOUNT_ROWS = "BINDING_ACCOUNT_ROWS"
BINDING_TAG_MAP = "BINDING_TAG_MAP"
BINDING_CORPORATE_ACTIONS = "BINDING_CORPORATE_ACTIONS"
BINDING_OLD_SCHEMA = "BINDING_OLD_SCHEMA"
BINDING_RECEIPT = "BINDING_RECEIPT"
MIN_CATALOG_SCHEMA = 7
CATALOG_NAME = "ibkr-catalog.json"
CATALOG_ARTIFACT = "normalized/ibkr-catalog.json"


@dataclass(frozen=True)
class FactsSources:
    """What the consumer's own run holds to bind a facts file against."""

    ledger: Mapping[str, Any]
    catalog_raw: bytes | None
    tag_map_raw: bytes | None


def tag_map_sha256(raw: bytes) -> str:
    """Canonical-JSON SHA-256 of a parsed tag map (sorted keys, compact, ASCII)."""

    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refusal(BINDING_TAG_MAP, "the tag map is not JSON") from exc
    return hashlib.sha256(
        json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    ).hexdigest()


def ledger_provider_generation(ledger: Mapping[str, Any], provider_id: str) -> str:
    """The generation id the ledger records for a provider (exactly one row)."""

    rows = ledger.get("provider_generations")
    if not isinstance(rows, list):
        raise Refusal(
            BINDING_MISSING_SIDE, "the provider ledger has no provider_generations"
        )
    found = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("provider_id", "")).casefold() == provider_id.casefold()
    ]
    if len(found) != 1:
        raise Refusal(
            BINDING_LEDGER_GENERATION,
            f"the provider ledger records {len(found)} generations for {provider_id}, "
            "not exactly one",
        )
    identifier = found[0].get("generation_id")
    if not isinstance(identifier, str) or not identifier:
        raise Refusal(BINDING_LEDGER_GENERATION, "the ledger's generation has no id")
    return identifier


def read_generation_catalog(directory: Path, generation_id: str) -> bytes:
    """The IBKR catalog bytes of one generation directory, proven against its manifest."""

    if directory.name != generation_id:
        raise Refusal(
            BINDING_CATALOG,
            f"directory {directory.name} is not the ledger's generation {generation_id}",
        )
    try:
        manifest = json.loads((directory / "generation.json").read_bytes())
        raw = (directory / CATALOG_NAME).read_bytes()
    except (OSError, ValueError) as exc:
        raise Refusal(
            BINDING_CATALOG, f"cannot read generation {generation_id}"
        ) from exc
    if manifest.get("generation_id") != generation_id:
        raise Refusal(BINDING_CATALOG, "generation.json names another generation")
    listed = [
        item
        for item in manifest.get("artifacts", ())
        if isinstance(item, Mapping) and item.get("artifact_id") == CATALOG_ARTIFACT
    ]
    if (
        len(listed) != 1
        or listed[0].get("sha256") != hashlib.sha256(raw).hexdigest()
        or listed[0].get("size_bytes") != len(raw)
    ):
        raise Refusal(
            BINDING_CATALOG, "the catalog differs from its generation manifest"
        )
    return raw


def _day(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise Refusal(BINDING_ACCOUNT_ROWS, f"{where} is not a date")
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError as exc:
        raise Refusal(BINDING_ACCOUNT_ROWS, f"{where} is not a date") from exc


def account_rows_sha256(
    catalog: Mapping[str, Any], parent: str, first_day: str, last_day: str
) -> str:
    """SHA-256 over the parent's catalog rows inside the window, canonical JSON.

    Selection: transactions (dated by ``nav_recognition_date`` or ``event_date`` for cash
    activity, else ``event_date``), positions (``as_of_date``) and change-in-NAV
    observations (``accounting_date``) of the parent, ``first_day <= day <= last_day``.
    """

    selected: dict[str, list[Any]] = {}
    for key, day_key in (
        ("transactions", None),
        ("positions", "as_of_date"),
        ("change_in_nav_observations", "accounting_date"),
    ):
        rows = catalog.get(key)
        if not isinstance(rows, list):
            raise Refusal(BINDING_ACCOUNT_ROWS, f"the catalog has no {key} list")
        chosen: list[Any] = []
        for row in rows:
            if not isinstance(row, Mapping) or row.get("account_id") != parent:
                continue
            if day_key is not None:
                day = row.get(day_key)
            elif str(row.get("kind", "")).startswith("CASH_ACTIVITY:"):
                day = row.get("nav_recognition_date") or row.get("event_date")
            else:
                day = row.get("event_date")
            if first_day <= _day(day, f"{key} day") <= last_day:
                chosen.append(row)
        selected[key] = chosen
    canonical = json.dumps(
        selected, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def corporate_actions_sha256(
    catalog: Mapping[str, Any], parent: str, first_day: str, last_day: str
) -> str:
    """SHA-256 over the parent's corporate-action rows dated inside the window.

    Every row of the account (a catalog repeats an action under two provider-account
    scopes; both stay), in catalog order, ``first_day <= event_date <= last_day``,
    serialised as canonical JSON (sorted keys, compact, ASCII).
    """

    rows = catalog.get("corporate_actions")
    if not isinstance(rows, list):
        raise Refusal(BINDING_CORPORATE_ACTIONS, "the catalog has no corporate_actions")
    chosen = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and row.get("account_id") == parent
        and first_day <= _day(row.get("event_date"), "corporate action day") <= last_day
    ]
    return hashlib.sha256(
        json.dumps(
            chosen, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()


def check_bindings(
    facts: Mapping[str, Any],
    sources: FactsSources | None,
    *,
    regression_reference: bool = False,
) -> None:
    """Refuse a facts file whose bindings are not those of the consumer's sources.

    A facts file without ``corporate_actions_sha256`` predates the corporate-action rule
    (its fraction was split pro rata): it is refused as an old-schema file unless the
    caller names it a regression reference.
    """

    if sources is None:
        raise Refusal(
            BINDING_MISSING_SIDE,
            "no provider ledger, IBKR catalog and tag map to bind the sleeve facts "
            "against",
        )
    bindings = facts.get("bindings")
    provider = facts.get("provider_id")
    parent = facts.get("parent_account_id")
    window = facts.get("window")
    if not (
        isinstance(bindings, Mapping)
        and isinstance(provider, str)
        and isinstance(parent, str)
        and isinstance(window, Mapping)
    ):
        raise Refusal(BINDING_MISSING_SIDE, "the facts carry no bindings or provider")
    expected = ledger_provider_generation(sources.ledger, provider)
    bound = bindings.get("parent_ledger_generation_id")
    if bound != expected:
        raise Refusal(
            BINDING_LEDGER_GENERATION,
            f"the facts bind provider generation {bound}; the ledger records "
            f"{expected} for {provider}",
        )
    if sources.catalog_raw is None:
        raise Refusal(
            BINDING_MISSING_SIDE, "no IBKR catalog for the ledger's generation"
        )
    try:
        catalog = json.loads(sources.catalog_raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refusal(BINDING_CATALOG, "the catalog is not JSON") from exc
    version = catalog.get("catalog_schema_version")
    if (
        catalog.get("provider_id") != provider
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < MIN_CATALOG_SCHEMA
    ):
        raise Refusal(
            BINDING_CATALOG,
            f"the catalog is not a schema-{MIN_CATALOG_SCHEMA}-or-later {provider} catalog",
        )
    listed = {
        row.get("account_id")
        for row in catalog.get("accounts", ())
        if isinstance(row, Mapping)
    }
    if parent not in listed:
        raise Refusal(
            BINDING_PARENT_ACCOUNT,
            f"the facts' parent account {parent} is not in the generation's catalog",
        )
    first, last = window.get("first_day"), window.get("last_day")
    if not (isinstance(first, str) and isinstance(last, str)):
        raise Refusal(BINDING_MISSING_SIDE, "the facts carry no window")
    computed = account_rows_sha256(catalog, parent, first, last)
    if bindings.get("account_rows_sha256") != computed:
        raise Refusal(
            BINDING_ACCOUNT_ROWS,
            f"the facts bind account rows {bindings.get('account_rows_sha256')}; the "
            f"catalog's rows hash to {computed}",
        )
    if "corporate_actions_sha256" not in bindings:
        if not regression_reference:
            raise Refusal(
                BINDING_OLD_SCHEMA,
                "the facts carry no corporate_actions_sha256: an old-schema facts file "
                "that splits a corporate-action fraction pro rata (a regression "
                "reference only)",
            )
    else:
        actions = corporate_actions_sha256(catalog, parent, first, last)
        if bindings.get("corporate_actions_sha256") != actions:
            raise Refusal(
                BINDING_CORPORATE_ACTIONS,
                f"the facts bind corporate actions "
                f"{bindings.get('corporate_actions_sha256')}; the catalog's rows hash "
                f"to {actions}",
            )
    if sources.tag_map_raw is None:
        raise Refusal(
            BINDING_MISSING_SIDE, "no tag map to bind the sleeve facts against"
        )
    tag_map = tag_map_sha256(sources.tag_map_raw)
    if bindings.get("tag_map_sha256") != tag_map:
        raise Refusal(
            BINDING_TAG_MAP,
            f"the facts bind tag map {bindings.get('tag_map_sha256')}; the tag map "
            f"held hashes to {tag_map}",
        )


def load_sources(
    ledger: Mapping[str, Any],
    facts: Mapping[str, Any],
    ibkr_root: Path | None,
    tag_map_path: Path | None,
) -> FactsSources:
    """The consumer's sources: the catalog of the ledger's own generation and the map."""

    provider = facts.get("provider_id")
    catalog_raw: bytes | None = None
    if ibkr_root is not None and isinstance(provider, str):
        generation = ledger_provider_generation(ledger, provider)
        catalog_raw = read_generation_catalog(
            ibkr_root / "generations" / generation, generation
        )
    tag_raw: bytes | None = None
    if tag_map_path is not None:
        try:
            tag_raw = tag_map_path.read_bytes()
        except OSError as exc:
            raise Refusal(BINDING_TAG_MAP, f"cannot read {tag_map_path}") from exc
    return FactsSources(ledger=ledger, catalog_raw=catalog_raw, tag_map_raw=tag_raw)


RECEIPT_NAME = "admit-or-declare-receipt.json"
RECEIPT_ARTIFACT = "normalized/admit-or-declare-receipt.json"
LEDGER_NAME = "provider-ledger.json"
LEDGER_ARTIFACT = "normalized/provider-ledger.json"
RECEIPT_TYPE = "PROVIDER_ADMIT_OR_DECLARE_V1"


def _canonical_digest(value: object) -> str:
    """SHA-256 of the producer's canonical JSON (provider_admission._canonical_json_value_bytes:
    sorted keys, compact separators, ASCII, one trailing newline); a test pins the two equal."""

    return hashlib.sha256(
        (
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            + "\n"
        ).encode("utf-8")
    ).hexdigest()


def _json_object(raw: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise Refusal(BINDING_RECEIPT, f"{what} is not JSON") from exc
    if not isinstance(value, dict):
        raise Refusal(BINDING_RECEIPT, f"{what} is not an object")
    return value


def bind_admission_receipt(
    receipt_path: Path, ledger_path: Path | None
) -> dict[str, Any]:
    """The receipt of the same verified provider-ledger generation as ``--provider-ledger``.

    A receipt grants a source-clock exemption, so it is bound like the facts file: it must
    be the type the pipeline writes, sit in a generation directory whose ``generation.json``
    is VERIFIED, names that directory as its id and records this receipt's SHA-256 as the
    ``normalized/admit-or-declare-receipt.json`` artifact; and the ``--provider-ledger``
    file must be that same generation's catalog, by the same record.  A stale, hand-edited
    or other-run receipt, or one given without its ledger, is a refusal.
    """

    try:
        raw = receipt_path.read_bytes()
        directory = receipt_path.resolve().parent
        manifest_raw = (directory / "generation.json").read_bytes()
    except OSError as exc:
        raise Refusal(
            BINDING_RECEIPT, f"cannot read the receipt or its generation.json: {exc}"
        ) from exc
    receipt = _json_object(raw, "the receipt")
    if (
        receipt.get("receipt_type") != RECEIPT_TYPE
        or receipt.get("schema_version") != 1
        or not isinstance(receipt.get("dispositions"), list)
    ):
        raise Refusal(
            BINDING_RECEIPT, "the file is not a provider admit-or-declare receipt"
        )
    for rows_key, digest_key in (
        ("dispositions", "disposition_sha256"),
        ("inventory", "inventory_sha256"),
    ):
        if _canonical_digest(receipt.get(rows_key)) != receipt.get(digest_key):
            raise Refusal(
                BINDING_RECEIPT,
                f"the receipt's {rows_key} do not hash to its {digest_key}",
            )
    manifest = _json_object(manifest_raw, "generation.json")
    if (
        manifest.get("generation_id") != directory.name
        or manifest.get("verification") != "VERIFIED"
    ):
        raise Refusal(
            BINDING_RECEIPT, "the receipt's generation is not its verified directory"
        )
    recorded = {
        item.get("artifact_id"): item.get("sha256")
        for item in manifest.get("artifacts", [])
        if isinstance(item, Mapping)
    }
    if recorded.get(RECEIPT_ARTIFACT) != hashlib.sha256(raw).hexdigest():
        raise Refusal(
            BINDING_RECEIPT,
            "the receipt's SHA-256 is not the one its generation records",
        )
    if ledger_path is None:
        raise Refusal(
            BINDING_RECEIPT,
            "a receipt is only usable with the --provider-ledger of its generation",
        )
    try:
        ledger_raw = ledger_path.read_bytes()
    except OSError as exc:
        raise Refusal(BINDING_RECEIPT, f"cannot read {ledger_path}") from exc
    if (
        ledger_path.resolve().parent != directory
        or recorded.get(LEDGER_ARTIFACT) != hashlib.sha256(ledger_raw).hexdigest()
    ):
        raise Refusal(
            BINDING_RECEIPT,
            "--provider-ledger is not the catalog of the receipt's generation",
        )
    return receipt
