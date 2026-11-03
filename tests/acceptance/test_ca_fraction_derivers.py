"""#624: both acceptance derivers split a corporate action's cash in lieu by own fraction.

Each deriver has its own implementation of the rule (derive_a ``corporate_fraction``,
derive_b ``cash_in_lieu_fraction``); the same synthetic cases run against both.

SYNTHETIC: two sleeves hold 18 and 13 shares (31); a 1-for-8 reverse split entitles the
account to 3.875 shares: 3 whole and 0.875 sold for cash.  Entitlements 2.25 and 1.625,
whole 2 and 1, own fractions 0.25 and 0.625 (sum 0.875).

Real bytes (env ``QE_AI624_R21_CATALOG`` = the R2.1 ``ibkr-catalog.json``,
``QE_AI624_R22_FACTS`` = the rebuilt facts of branch S): both derivers reproduce every
sleeve NAV of the rebuilt facts.
"""

from __future__ import annotations

from collections import defaultdict
import json
import os
from decimal import Decimal as D
from pathlib import Path
import sys
from typing import Any, Callable

import pytest
from quant_engine.core import release_input_root
import test_derive_b_combined as combined

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "acceptance"))
from derive_a import sleeves as sleeves_a  # noqa: E402
from derive_a.sources import InputError  # noqa: E402

DAYS = [f"2026-03-0{n}" for n in range(1, 9)]
SEC = "sec-new"
OLD = "sec-old"
ROWS = [
    {"event_date": "2026-03-02", "kind": "RS", "security_id": OLD, "quantity": "-31"},
    {"event_date": "2026-03-02", "kind": "RS", "security_id": SEC, "quantity": "3.875"},
    # the catalog repeats each action under a second account scope: counts once
    {"event_date": "2026-03-02", "kind": "RS", "security_id": OLD, "quantity": "-31"},
    {"event_date": "2026-03-02", "kind": "RS", "security_id": SEC, "quantity": "3.875"},
]


class Refused(Exception):
    pass


def _derive_a(**kw: Any) -> dict[str, Any] | None:
    held = kw.get("held", {"s1": D(18), "s2": D(13)})
    holdings = {s: defaultdict(lambda: D(0), {SEC: q}) for s, q in held.items()}
    holdings["other"] = defaultdict(lambda: D(0))
    try:
        return sleeves_a.corporate_fraction(
            kw.get("day", "2026-03-03"),
            DAYS,
            kw.get("rows", ROWS),
            SEC,
            kw.get("q", D("-0.875")),
            holdings,
            ["s1", "s2"],
            ["s1", "s2", "other"],
            kw.get("actual", D(3)),
        )
    except InputError as exc:
        raise Refused(str(exc)) from exc


def _derive_b(**kw: Any) -> dict[str, Any] | None:
    module = combined.load_module("derive_b_ca_fraction")
    held = kw.get("held", {"s1": D(18), "s2": D(13)})
    holdings = {s: {SEC: q} for s, q in held.items()}
    holdings["other"] = {}
    try:
        found = module.cash_in_lieu_fraction(
            kw.get("day", "2026-03-03"),
            DAYS,
            kw.get("rows", ROWS),
            SEC,
            kw.get("q", D("-0.875")),
            holdings,
            ["s1", "s2"],
            ["s1", "s2", "other"],
            kw.get("actual", D(3)),
        )
    except module.SleeveInputError as exc:
        raise Refused(str(exc)) from exc
    if found is None:
        return None
    # the same names as derivation A for the comparison
    return {
        "shares": found["share"],
        "unit": found["unit"],
        "fraction": found["fraction"],
        "whole": found["whole"],
        "entitled": found["entitlement"],
    }


DERIVERS: list[Callable[..., dict[str, Any] | None]] = [_derive_a, _derive_b]


@pytest.mark.parametrize("derive", DERIVERS, ids=["a", "b"])
def test_each_sleeve_is_charged_its_own_fraction(derive: Callable[..., Any]) -> None:
    found = derive()
    assert found is not None
    assert found["entitled"] == {"s1": D("2.25"), "s2": D("1.625")}
    assert found["whole"] == {"s1": D(2), "s2": D(1)}
    assert found["fraction"] == {"s1": D("0.25"), "s2": D("0.625")}
    # shares of the sale: 0.25/0.875 and 0.625/0.875 (pro rata by held would be 18/31)
    assert abs(found["shares"]["s1"] - D(2) / 7) < D("1e-25")
    assert abs(found["shares"]["s2"] - D(5) / 7) < D("1e-25")
    assert abs(found["shares"]["s1"] - D(18) / 31) > D("0.01")
    assert found["unit"] == 8  # holdings are still in the surrendered security's shares


@pytest.mark.parametrize("derive", DERIVERS, ids=["a", "b"])
def test_holdings_already_in_new_shares_are_recognised(
    derive: Callable[..., Any],
) -> None:
    found = derive(held={"s1": D("2.25"), "s2": D("1.625")})
    assert found is not None
    assert found["unit"] == 1
    assert found["fraction"] == {"s1": D("0.25"), "s2": D("0.625")}


@pytest.mark.parametrize("derive", DERIVERS, ids=["a", "b"])
@pytest.mark.parametrize(
    "case",
    [
        {"q": D("-0.5")},  # not the fractional part: an ordinary untagged sale
        {"q": D("0.875")},  # a buy
        {"day": "2026-03-08"},  # more than five ledger days after the action
        {"day": "2026-03-01"},  # before the action
        {"held": {"s1": D(31), "s2": D(0)}},  # one holder: assigned, not split
        {"rows": [r for r in ROWS if r["security_id"] != SEC]},  # no action delivers it
    ],
    ids=["other-size", "buy", "late", "early", "one-holder", "no-action"],
)
def test_anything_else_stays_pro_rata(
    derive: Callable[..., Any], case: dict[str, Any]
) -> None:
    assert derive(**case) is None


@pytest.mark.parametrize("derive", DERIVERS, ids=["a", "b"])
@pytest.mark.parametrize(
    ("case", "text"),
    [
        ({"rows": [r for r in ROWS if r["security_id"] != OLD]}, "surrendered"),
        ({"held": {"s1": D(18), "s2": D(12)}}, "neither"),
        ({"actual": D(4)}, "account position"),
    ],
    ids=[
        "no-surrendered-security",
        "holdings-match-neither-side",
        "whole-shares-differ",
    ],
)
def test_a_broken_closure_stops_the_derivation(
    derive: Callable[..., Any], case: dict[str, Any], text: str
) -> None:
    with pytest.raises(Refused, match=text):
        derive(**case)


def test_sleeve_fractions_that_do_not_add_to_the_sale_are_refused() -> None:
    # 13 + 12 = 25 shares become 3.125; the sleeves' own fractions are .625 + .5 = 1.125
    # while the broker sold the aggregate fraction 0.125: the closure fails
    rows = [
        {**r, "quantity": "-25" if r["quantity"] == "-31" else "3.125"} for r in ROWS
    ]
    for derive in DERIVERS:
        with pytest.raises(Refused, match="do not add"):
            derive(held={"s1": D(13), "s2": D(12)}, rows=rows, q=D("-0.125"))


# ------------------------------------------------------------------------ real bytes

CATALOG = os.environ.get("QE_AI624_R21_CATALOG")
FACTS = os.environ.get("QE_AI624_R22_FACTS")
real = pytest.mark.skipif(
    not (CATALOG and FACTS and Path(CATALOG).exists() and Path(FACTS).exists()),
    reason="QE_AI624_R21_CATALOG and QE_AI624_R22_FACTS are not set",
)
REPO = Path(__file__).resolve().parents[2]
DECLARED = REPO / "ops/mvp2-release/inputs/sleeves/sleeve-declared-inputs.json"
TAG_MAP = (
    release_input_root(REPO)
    / "ops/mvp2-release/inputs/sleeves/nested-strategy-tag-map.json"
)


def _compare(rows: dict[str, dict[str, Any]], facts: dict[str, Any]) -> tuple[int, D]:
    worst, cells = D(0), 0
    for sleeve in facts["sleeves"]:
        mine = rows[sleeve["sleeve_id"]]
        for row in sleeve["days"]:
            worst = max(
                worst,
                abs(D(str(mine[row["day"]])) - D(row["nav_end_after_alloc"])),
            )
            cells += 1
    return cells, worst


@real
def test_derivation_a_reproduces_every_sleeve_nav_of_the_rebuilt_facts() -> None:
    assert CATALOG and FACTS
    facts = json.loads(Path(FACTS).read_bytes())
    catalog = json.loads(Path(CATALOG).read_bytes())
    window = facts["window"]
    source = sleeves_a.slice_ibkr(
        catalog, facts["parent_account_id"], window["first_day"], window["last_day"]
    )
    tag = [
        p
        for p in sleeves_a._json(TAG_MAP.read_bytes(), "tag map")["parents"]
        if p["provider_id"] == "ibkr"
    ][0]
    out = sleeves_a.ibkr_facts(
        source, sleeves_a._json(DECLARED.read_bytes(), "dec"), tag
    )
    rows = {
        name: {r["day"]: r["nav_end_after_alloc"] for r in series}
        for name, series in out["sleeves"].items()
    }
    cells, worst = _compare(rows, facts)
    assert cells == sum(len(s["days"]) for s in facts["sleeves"])
    assert worst < D("1e-12"), worst
    block = out["disclosures"]["corporate_action_fractions"]
    assert len(block) == 1
    assert sorted(block[0]["sleeves"]) == ["s1", "s2"]
    assert not any(
        m["how"] == "split" for m in out["disclosures"]["manual_assignments"]
    )


@real
def test_derivation_b_reproduces_every_sleeve_nav_of_the_rebuilt_facts() -> None:
    assert CATALOG and FACTS
    module = combined.load_module("derive_b_ca_real")
    facts = json.loads(Path(FACTS).read_bytes())
    catalog = json.loads(Path(CATALOG).read_bytes())
    window = facts["window"]
    tag = [
        p
        for p in json.loads(TAG_MAP.read_bytes())["parents"]
        if p["provider_id"] == "ibkr"
    ][0]
    cut = module._cut_ibkr_slice(
        catalog, facts["parent_account_id"], window["first_day"], window["last_day"]
    )
    out = module._ibkr_facts(cut, json.loads(DECLARED.read_bytes()), tag)
    rows = {
        name: {r["day"]: r["nav_end_after_alloc"] for r in series}
        for name, series in out["sleeves"].items()
    }
    cells, worst = _compare(rows, facts)
    assert cells == sum(len(s["days"]) for s in facts["sleeves"])
    assert worst < D("1e-12"), worst
    block = out["disclosures"]["corporate_action_fractions"]
    assert len(block) == 1 and sorted(block[0]["sleeves"]) == ["s1", "s2"]
