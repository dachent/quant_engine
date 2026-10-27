"""The effective epoch perimeter once derived sleeves exist (#567, stage 5).

One provider account can run several strategies at once.  The account manifest
then carries, besides the epochs the owner's current-epoch crosswalk names, the
epochs of derived accounts ``<parent>-<sleeve>`` and no epoch of a residual
parent (an account whose strategy epoch the sleeves replaced).  The crosswalk
names the baseline perimeter only, so every consumer that used to compare the
manifest with the crosswalk row for row needs one shared rule for what the
manifest may differ by.  This module is that rule.

A derived epoch is identified by the hash-bound flow-coverage authority, never by
its name: its coverage row has authority kind ``DERIVED_SLEEVE_COVERAGE_V1`` and
names its parent account.  The effective perimeter is

    (crosswalk INCLUDED epochs - epochs the manifest dropped) + derived epochs

and the manifest must equal it exactly.  An epoch may be dropped only when its
account is the parent of derived epochs, and a parent that drops one baseline
epoch drops all of its baseline epochs: a residual parent never stays partly a
strategy.  With no derived epoch the perimeter is the crosswalk's and nothing
may be dropped, so a report built without sleeves is checked exactly as before.

This module reads identities only; it holds no policy, amount or account name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Mapping, Sequence

EpochKey = tuple[str, str, str, str | None]

DERIVED_SLEEVE_AUTHORITY_KIND: Final = "DERIVED_SLEEVE_COVERAGE_V1"
# The production specification names each selected sleeve-facts artifact as a node
# whose id starts with this prefix (the verifier replays need their bytes).
SLEEVE_FACTS_NODE_PREFIX: Final = "financial-sleeve-facts"
_KEY_FIELDS: Final = ("account_id", "strategy_id", "effective_start", "effective_end")


class PerimeterError(ValueError):
    """The manifest does not match the baseline perimeter plus derived epochs."""


@dataclass(frozen=True)
class EffectivePerimeter:
    """The epochs the manifest carries, split by origin."""

    included: frozenset[EpochKey]
    removed: frozenset[EpochKey]
    derived: frozenset[EpochKey]
    parents: Mapping[EpochKey, str]
    product_strategy_ids: frozenset[str]
    tag_map_digests: Mapping[EpochKey, str] = field(default_factory=dict)

    @property
    def has_derived(self) -> bool:
        return bool(self.derived)

    @property
    def product(self) -> frozenset[EpochKey]:
        """The product epochs: included epochs of the strategies the baseline
        crosswalk includes.  A derived epoch of such a strategy is the current
        epoch that replaced its residual parent's."""

        return frozenset(
            key for key in self.included if key[1] in self.product_strategy_ids
        )

    @property
    def retained(self) -> frozenset[EpochKey]:
        """Derived epochs of strategies outside the current product.

        They carry their own capital and returns in the manifest and are
        published as history-only strategies; no product aggregate counts them.
        """

        return self.included - self.product


def epoch_key(row: Mapping[str, Any], label: str = "epoch") -> EpochKey:
    """The four-part identity of an epoch row, strictly typed."""

    values = tuple(row.get(field) for field in _KEY_FIELDS)
    account, strategy, start, end = values
    if (
        not isinstance(account, str)
        or not isinstance(strategy, str)
        or not isinstance(start, str)
        or (end is not None and not isinstance(end, str))
    ):
        raise PerimeterError(f"{label} identity is invalid")
    return (account, strategy, start, end)


def derived_epochs(coverage_rows: Sequence[Mapping[str, Any]]) -> dict[EpochKey, str]:
    """Derived epoch -> parent account, from the flow-coverage authority rows."""

    result: dict[EpochKey, str] = {}
    for row in coverage_rows:
        if row.get("authority_kind") != DERIVED_SLEEVE_AUTHORITY_KIND:
            continue
        assignment = row.get("assignment")
        derivation = row.get("derivation")
        if not isinstance(assignment, Mapping) or not isinstance(derivation, Mapping):
            raise PerimeterError("a derived coverage row lacks its assignment")
        key = epoch_key(assignment, "derived coverage assignment")
        parent = derivation.get("parent_account_id")
        if not isinstance(parent, str) or not parent:
            raise PerimeterError("a derived coverage row lacks its parent account")
        if key in result:
            raise PerimeterError("a derived epoch is covered twice")
        if key[0] == parent:
            raise PerimeterError("a derived epoch lies on its own parent account")
        result[key] = parent
    return result


def derived_tag_maps(
    coverage_rows: Sequence[Mapping[str, Any]],
) -> dict[EpochKey, str]:
    """Derived epoch -> digest of the tag map its sleeve facts were built from.

    A coverage row that names none is simply absent here: a consumer that needs
    the binding (the report) refuses a derived epoch it cannot find.
    """

    result: dict[EpochKey, str] = {}
    for row in coverage_rows:
        if row.get("authority_kind") != DERIVED_SLEEVE_AUTHORITY_KIND:
            continue
        derivation = row.get("derivation")
        assignment = row.get("assignment")
        digest = (
            derivation.get("tag_map_sha256")
            if isinstance(derivation, Mapping)
            else None
        )
        if isinstance(assignment, Mapping) and isinstance(digest, str):
            result[epoch_key(assignment, "derived coverage assignment")] = digest
    return result


def effective_perimeter(
    baseline_included: Iterable[EpochKey],
    manifest_keys: Iterable[EpochKey],
    coverage_rows: Sequence[Mapping[str, Any]],
) -> EffectivePerimeter:
    """Bind the manifest's epochs to the baseline perimeter plus derived epochs."""

    return bind_perimeter(
        baseline_included,
        manifest_keys,
        derived_epochs(coverage_rows),
        derived_tag_maps(coverage_rows),
    )


def bind_perimeter(
    baseline_included: Iterable[EpochKey],
    manifest_keys: Iterable[EpochKey],
    parents: Mapping[EpochKey, str],
    tag_map_digests: Mapping[EpochKey, str] | None = None,
) -> EffectivePerimeter:
    """The same binding from an already derived epoch -> parent account map."""

    baseline = frozenset(baseline_included)
    manifest = frozenset(manifest_keys)
    derived = frozenset(parents)
    if derived & baseline:
        raise PerimeterError("a derived epoch repeats a baseline epoch")
    if not derived <= manifest:
        raise PerimeterError("a derived epoch is absent from the manifest")
    removed = baseline - manifest
    parent_accounts = frozenset(parents.values())
    for key in removed:
        if key[0] not in parent_accounts:
            raise PerimeterError(
                "a baseline epoch is missing from the manifest and its account "
                f"is not the parent of derived epochs: {key[0]}/{key[1]}"
            )
    for account in {key[0] for key in removed}:
        kept = [key for key in baseline - removed if key[0] == account]
        if kept:
            raise PerimeterError(
                f"parent {account} dropped only some of its baseline epochs"
            )
    included = (baseline - removed) | derived
    if included != manifest:
        raise PerimeterError("manifest epochs differ from the effective perimeter")
    return EffectivePerimeter(
        included=included,
        removed=removed,
        derived=derived,
        parents=dict(parents),
        product_strategy_ids=frozenset(key[1] for key in baseline),
        tag_map_digests=dict(tag_map_digests or {}),
    )
