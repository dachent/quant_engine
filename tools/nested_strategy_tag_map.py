"""Validate and resolve the owner's nested-strategy tag map (#567).

One provider account can run several strategies at once.  Each execution
carries a provider tag (an order reference, a fund title); the owner's tag map
names which sleeve that tag belongs to, effective-dated.  Every sleeve is its
own derived account with its own strategy, epoch, NAV and flows, so the
one-strategy-per-account chain the manifest requires stays intact.  This module
is policy only: it holds no provider names, amounts or account numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Final, Mapping, Sequence


class TagMapError(ValueError):
    """The nested-strategy tag map is invalid or a tag cannot be resolved."""


SCHEMA_PATH: Final = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "portfolio"
    / "nested-strategy-tag-map.schema.json"
)
_INTERNAL_ID: Final = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SLEEVE_ID: Final = re.compile(r"^[a-z][a-z0-9]{0,15}$")
_LEADING_REFERENCE: Final = re.compile(r"^[0-9]+-?")
_MATCHES: Final = ("exact", "startswith", "contains")
_NORMALIZATIONS: Final = ("NONE", "STRIP_LEADING_DIGITS_AND_DASH")


@dataclass(frozen=True)
class Tag:
    match: str
    pattern: str

    def matches(self, value: str) -> bool:
        if self.match == "exact":
            return value == self.pattern
        if self.match == "startswith":
            return value.startswith(self.pattern)
        return self.pattern in value


@dataclass(frozen=True)
class Sleeve:
    sleeve_id: str
    strategy_id: str
    label: str
    effective_start: date
    effective_end: date | None
    tags: tuple[Tag, ...]

    def active_on(self, day: date) -> bool:
        return self.effective_start <= day and (
            self.effective_end is None or day <= self.effective_end
        )


@dataclass(frozen=True)
class ParentMap:
    parent_account_id: str
    provider_id: str
    tag_field: str
    tag_normalization: str
    sleeves: tuple[Sleeve, ...]
    unmatched_mode: str
    fallback_sleeve_id: str | None

    def sleeve_account_id(self, sleeve_id: str) -> str:
        """The derived account that carries one sleeve's NAV and flows."""

        if sleeve_id not in {sleeve.sleeve_id for sleeve in self.sleeves}:
            raise TagMapError(f"unknown sleeve: {sleeve_id}")
        return f"{self.parent_account_id}-{sleeve_id}"

    def normalize(self, tag: str) -> str:
        if self.tag_normalization == "STRIP_LEADING_DIGITS_AND_DASH":
            return _LEADING_REFERENCE.sub("", tag, count=1)
        return tag

    def resolve(self, tag: str, day: date) -> Sleeve | None:
        """The one sleeve a tag belongs to on ``day``; None when no tag matches.

        Two sleeves matching the same tag on the same day is an error, never a
        guess.  ``None`` is the caller's cue to apply ``unmatched``.
        """

        normalized = self.normalize(tag)
        found = [
            sleeve
            for sleeve in self.sleeves
            if sleeve.active_on(day)
            and any(item.matches(normalized) for item in sleeve.tags)
        ]
        if len(found) > 1:
            raise TagMapError(
                f"tag matches several sleeves on {day.isoformat()}: "
                + ", ".join(sleeve.sleeve_id for sleeve in found)
            )
        return found[0] if found else None


@dataclass(frozen=True)
class TagMap:
    as_of_date: date
    parents: tuple[ParentMap, ...]
    sha256: str

    def parent(self, parent_account_id: str) -> ParentMap:
        for parent in self.parents:
            if parent.parent_account_id == parent_account_id:
                return parent
        raise TagMapError(f"unknown parent account: {parent_account_id}")


def _date(value: object, name: str) -> date:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise TagMapError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise TagMapError(f"{name} must be an ISO date") from exc


def _strict(value: object, keys: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise TagMapError(f"{name} keys are invalid")
    return value


def validate_tag_map(raw: bytes) -> TagMap:
    """Parse the tag map bytes, refuse anything ambiguous, return the model."""

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise TagMapError("tag map is not JSON") from exc
    top = _strict(
        payload, {"schema_version", "artifact_id", "as_of_date", "parents"}, "tag map"
    )
    if top["schema_version"] != 1 or top["artifact_id"] != "nested-strategy-tag-map":
        raise TagMapError("tag map identity is invalid")
    as_of = _date(top["as_of_date"], "as_of_date")
    parents_raw = top["parents"]
    if not isinstance(parents_raw, list) or not parents_raw:
        raise TagMapError("tag map has no parents")
    parents: list[ParentMap] = []
    seen_parents: set[str] = set()
    for index, parent_raw in enumerate(parents_raw):
        parent = _strict(
            parent_raw,
            {
                "parent_account_id",
                "provider_id",
                "tag_field",
                "tag_normalization",
                "sleeves",
                "unmatched",
            },
            f"parent {index}",
        )
        parent_id = parent["parent_account_id"]
        provider_id = parent["provider_id"]
        if (
            not isinstance(parent_id, str)
            or _INTERNAL_ID.fullmatch(parent_id) is None
            or not isinstance(provider_id, str)
            or _INTERNAL_ID.fullmatch(provider_id) is None
        ):
            raise TagMapError(f"parent {index} identity is invalid")
        if parent_id in seen_parents:
            raise TagMapError(f"duplicate parent account: {parent_id}")
        seen_parents.add(parent_id)
        tag_field = parent["tag_field"]
        if not isinstance(tag_field, str) or not tag_field:
            raise TagMapError(f"parent {index} tag_field is invalid")
        normalization = parent["tag_normalization"]
        if normalization not in _NORMALIZATIONS:
            raise TagMapError(f"parent {index} tag_normalization is invalid")
        sleeves_raw = parent["sleeves"]
        if not isinstance(sleeves_raw, list) or not sleeves_raw:
            raise TagMapError(f"parent {index} has no sleeves")
        sleeves: list[Sleeve] = []
        sleeve_ids: set[str] = set()
        strategy_ids: set[str] = set()
        for sleeve_index, sleeve_raw in enumerate(sleeves_raw):
            sleeve = _strict(
                sleeve_raw,
                {
                    "sleeve_id",
                    "strategy_id",
                    "label",
                    "effective_start",
                    "effective_end",
                    "tags",
                },
                f"parent {index} sleeve {sleeve_index}",
            )
            sleeve_id = sleeve["sleeve_id"]
            strategy_id = sleeve["strategy_id"]
            if (
                not isinstance(sleeve_id, str)
                or _SLEEVE_ID.fullmatch(sleeve_id) is None
                or not isinstance(strategy_id, str)
                or _INTERNAL_ID.fullmatch(strategy_id) is None
                or not isinstance(sleeve["label"], str)
                or not sleeve["label"]
            ):
                raise TagMapError(f"parent {index} sleeve {sleeve_index} is invalid")
            if len(f"{parent_id}-{sleeve_id}") > 64:
                raise TagMapError(f"sleeve account id is too long: {sleeve_id}")
            if sleeve_id in sleeve_ids or strategy_id in strategy_ids:
                raise TagMapError(
                    f"parent {index} repeats a sleeve or strategy: {sleeve_id}"
                )
            sleeve_ids.add(sleeve_id)
            strategy_ids.add(strategy_id)
            start = _date(sleeve["effective_start"], f"{sleeve_id}.effective_start")
            end_raw = sleeve["effective_end"]
            end = (
                None
                if end_raw is None
                else _date(end_raw, f"{sleeve_id}.effective_end")
            )
            if end is not None and end < start:
                raise TagMapError(f"{sleeve_id} interval is reversed")
            tags_raw = sleeve["tags"]
            if not isinstance(tags_raw, list) or not tags_raw:
                raise TagMapError(f"{sleeve_id} has no tags")
            tags: list[Tag] = []
            for tag_raw in tags_raw:
                tag = _strict(tag_raw, {"match", "pattern"}, f"{sleeve_id} tag")
                if (
                    tag["match"] not in _MATCHES
                    or not isinstance(tag["pattern"], str)
                    or not tag["pattern"]
                ):
                    raise TagMapError(f"{sleeve_id} tag is invalid")
                tags.append(Tag(str(tag["match"]), str(tag["pattern"])))
            sleeves.append(
                Sleeve(
                    sleeve_id,
                    strategy_id,
                    str(sleeve["label"]),
                    start,
                    end,
                    tuple(tags),
                )
            )
        unmatched = parent["unmatched"]
        if not isinstance(unmatched, Mapping) or unmatched.get("mode") not in {
            "BLOCK",
            "ASSIGN_BY_HOLDING_SYMBOL",
        }:
            raise TagMapError(f"parent {index} unmatched policy is invalid")
        fallback: str | None = None
        if unmatched["mode"] == "BLOCK":
            if set(unmatched) != {"mode"}:
                raise TagMapError(f"parent {index} unmatched policy is invalid")
        else:
            if set(unmatched) != {"mode", "fallback_sleeve_id"}:
                raise TagMapError(f"parent {index} unmatched policy is invalid")
            fallback = unmatched["fallback_sleeve_id"]
            if fallback not in sleeve_ids:
                raise TagMapError(f"parent {index} fallback sleeve is unknown")
        parents.append(
            ParentMap(
                parent_id,
                provider_id,
                tag_field,
                str(normalization),
                tuple(sleeves),
                str(unmatched["mode"]),
                fallback,
            )
        )
    return TagMap(
        as_of_date=as_of,
        parents=tuple(parents),
        sha256=hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        ).hexdigest(),
    )


def load_tag_map(path: Path) -> TagMap:
    return validate_tag_map(path.read_bytes())


def sleeve_accounts(tag_map: TagMap) -> Sequence[tuple[str, str, str]]:
    """(derived account id, parent account id, strategy id) for every sleeve."""

    return tuple(
        (
            parent.sleeve_account_id(sleeve.sleeve_id),
            parent.parent_account_id,
            sleeve.strategy_id,
        )
        for parent in tag_map.parents
        for sleeve in parent.sleeves
    )
