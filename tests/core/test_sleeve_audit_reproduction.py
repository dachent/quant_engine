"""The sleeve facts reproduce the audit's closure identities on a synthetic account (#567).

``sleeve_audit_fixture.json`` is synthetic: invented ids, tags, quantities and
prices, shaped like the first real IBKR run (five sleeves; a rebalance inside the
account; a deposit-funded step; a reallocation between sleeves; a reverse split
that re-issues the security id; a liquidation that is not tagged; an untagged
fractional sale in the same second as a tagged sale).  It holds no real account,
security or dollar figure.  The audit identities checked here are the ones the
real run was judged by: sleeve values sum to the account's stock, commissions
tie to ChangeInNAV, the dollar closure stays inside tolerance, and the
sleeve-weighted time-weighted return equals the account's when no account item
belongs to no sleeve.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
import pytest

import build_sleeve_account_facts as builder
import verify_sleeve_ledger as verifier

FIXTURE = Path(__file__).with_name("sleeve_audit_fixture.json")
ACCOUNT = "account-synthetic"
GENERATION = "synthetic-generation-001"
DAYS = [
    "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06",
    "2026-03-09", "2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13",
]  # fmt: skip
SPLIT_DAY = "2026-03-10"
C_OLD, C_NEW = "security-synthetic-c", "security-synthetic-c2"


def _tag_map() -> dict[str, Any]:
    def sleeve(name: str, tags: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "sleeve_id": name,
            "strategy_id": f"strategy-{name}",
            "label": name,
            "effective_start": "2026-03-01",
            "effective_end": None,
            "tags": tags,
        }

    def starts(pattern: str) -> list[dict[str, str]]:
        return [{"match": "startswith", "pattern": pattern}]

    return {
        "schema_version": 1,
        "artifact_id": "nested-strategy-tag-map",
        "as_of_date": "2026-03-01",
        "parents": [
            {
                "parent_account_id": ACCOUNT,
                "provider_id": "provider-a",
                "tag_field": "orderReference",
                "tag_normalization": "STRIP_LEADING_DIGITS_AND_DASH",
                "sleeves": [
                    sleeve("s1", starts("BookAlpha")),
                    sleeve("s2", starts("BookBeta")),
                    sleeve("s3", starts("BookGamma")),
                    sleeve("s4", starts("BookDelta")),
                    sleeve(
                        "s5",
                        [
                            {"match": "contains", "pattern": "EpsilonOne"},
                            {"match": "contains", "pattern": "EpsilonTwo"},
                        ],
                    ),
                ],
                "unmatched": {
                    "mode": "ASSIGN_BY_HOLDING_SYMBOL",
                    "fallback_sleeve_id": "s4",
                },
            }
        ],
    }


def _event(
    kind: str, ref: str, *, evidence: str = "DERIVED", cash: bool = False
) -> dict[str, Any]:
    return {
        "kind": kind,
        "evidence": evidence,
        "evidence_ref": ref,
        "live": True,
        "note": None,
        "counterparty": "UNALLOCATED_ACCOUNT_CASH" if cash else None,
    }


def _declared() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_id": "sleeve-declared-inputs",
        "parent_account_id": ACCOUNT,
        "base_currency": "USD",
        "residual_id": "other",
        "window": {"first_day": DAYS[0], "last_day": DAYS[-1]},
        "closure_tolerance": {"per_day": "0.01", "window": "0.01"},
        "change_in_nav_components": {
            "external_flow": ["depositsWithdrawals", "internalCashTransfers"],
            "interest": ["interest", "changeInInterestAccruals"],
            "fees": ["otherFees", "brokerFees", "otherIncome", "other"],
            "dividend_accrual": ["changeInDividendAccruals"],
        },
        "symbol_aliases": {
            "aliases": {
                C_OLD: {
                    "to": C_NEW,
                    "from_day": SPLIT_DAY,
                    "evidence_ref": "synthetic reverse split",
                }
            }
        },
        "unheld_fallback": {},
        "funding_events": {
            "s1": {
                DAYS[0]: _event("IN", "trades:s1-entry", cash=True),
                DAYS[1]: _event("OUT", "trades:s1-realloc"),
                DAYS[2]: _event("OUT", "trades:s1-rebalance-out", cash=True),
                DAYS[3]: _event("IN", "trades:s1-rebalance-in", cash=True),
            },
            "s2": {
                DAYS[1]: _event("IN", "trades:s2-entry"),
                DAYS[4]: _event(
                    "IN", "deposit+trades", evidence="EVIDENCED", cash=True
                ),
                DAYS[8]: _event("IN", "trades:s2-add"),
            },
            "s3": {
                DAYS[4]: _event("IN", "trades:s3-entry", cash=True),
                DAYS[6]: _event("EXIT", "trades:s3-exit"),
            },
            "s4": {
                DAYS[5]: _event("IN", "trades:s4-entry", cash=True),
                DAYS[8]: _event("EXIT", "untagged:s4-exit"),
            },
            "s5": {
                DAYS[6]: _event("IN", "trades:s5-entry"),
                DAYS[7]: _event("IN", "trades:s5-add", cash=True),
                DAYS[8]: _event("EXIT", "untagged:s5-exit"),
            },
        },
    }


def _bytes(document: object) -> bytes:
    return json.dumps(document, sort_keys=True).encode()


def _rows(document: dict[str, Any] | None = None) -> builder.AccountRows:
    data = document or json.loads(FIXTURE.read_text(encoding="utf-8"))
    return builder.AccountRows(
        tuple(data["transactions"]),
        tuple(data["positions"]),
        tuple(data["change_in_nav_observations"]),
    )


def _build(rows: builder.AccountRows) -> bytes:
    return builder.build_sleeve_account_facts(
        tag_map_bytes=_bytes(_tag_map()),
        declared_inputs_bytes=_bytes(_declared()),
        rows=rows,
        rows_sha256="0" * 64,
        parent_ledger_generation_id=GENERATION,
    )


def _chain(values: list[Decimal]) -> Decimal:
    product = Decimal(1)
    for value in values:
        product *= 1 + value
    return product - 1


def test_the_audit_identities_hold_on_the_synthetic_account() -> None:
    rows = _rows()
    artifact = json.loads(_build(rows))
    Draft202012Validator(json.loads(builder.SCHEMA_PATH.read_text("utf-8"))).validate(
        artifact
    )
    sleeves = {
        item["sleeve_id"]: {r["day"]: r for r in item["days"]}
        for item in artifact["sleeves"]
    }

    # 1. sleeve values sum to the account's stock on every day
    stock = {day: Decimal(0) for day in DAYS}
    for position in rows.positions:
        stock[position["as_of_date"]] += Decimal(position["quantity"]) * Decimal(
            position["mark_price"]
        )
    for day in DAYS:
        total = sum(
            (Decimal(days[day]["value"]) for days in sleeves.values() if day in days),
            Decimal(0),
        )
        assert abs(total - stock[day]) <= Decimal("0.0001"), day

    # 2. sleeve commissions tie to ChangeInNAV commissions
    account_commissions = Decimal(0)
    for change in rows.change_in_nav:
        components = {
            c["source_component_key"]: c["value"] for c in change["components"]
        }
        account_commissions += Decimal(components["commissions"])
    sleeve_commissions = sum(
        (Decimal(r["commission"]) for days in sleeves.values() for r in days.values()),
        Decimal(0),
    )
    assert sleeve_commissions == account_commissions

    # 3. dollar closure inside tolerance every day and over the window
    assert all(
        abs(Decimal(c["unexplained"])) <= Decimal("0.01") for c in artifact["closure"]
    )
    assert abs(Decimal(artifact["closure_policy"]["window_unexplained"])) <= Decimal(
        "0.01"
    )

    # 4. the sleeve-weighted TWR equals the account TWR (no stubs, no residual)
    account_r: list[Decimal] = []
    weighted: list[Decimal] = []
    for change in rows.change_in_nav:
        components = {
            c["source_component_key"]: Decimal(c["value"]) for c in change["components"]
        }
        flow = components["depositsWithdrawals"] + components["internalCashTransfers"]
        start, end = Decimal(change["opening_nav"]), Decimal(change["ending_nav"])
        denominator = start + flow
        account_r.append((end - start - flow) / denominator)
        day = change["accounting_date"]
        weighted.append(
            sum(
                (
                    (Decimal(days[day]["own_pnl"]) + Decimal(days[day]["alloc_total"]))
                    / denominator
                    for days in sleeves.values()
                    if day in days and Decimal(days[day]["exposure"]) > 1
                ),
                Decimal(0),
            )
        )
    assert abs(_chain(account_r) - _chain(weighted)) < Decimal("1e-9")


def test_a_rebalance_inside_the_account_needs_no_unresolved_flag() -> None:
    artifact = json.loads(_build(_rows()))
    s1 = next(item for item in artifact["sleeves"] if item["sleeve_id"] == "s1")
    by_day = {row["day"]: row for row in s1["days"]}
    assert by_day[DAYS[2]]["event"] == "OUT" and by_day[DAYS[3]]["event"] == "IN"
    assert {row["event_evidence"] for row in s1["days"]} <= {"", "DERIVED"}
    assert by_day[DAYS[2]]["counterparty"] == "UNALLOCATED_ACCOUNT_CASH"


def test_a_reverse_split_that_reissues_the_security_id_is_not_a_loss() -> None:
    artifact = json.loads(_build(_rows()))
    s2 = next(item for item in artifact["sleeves"] if item["sleeve_id"] == "s2")
    day = next(row for row in s2["days"] if row["day"] == SPLIT_DAY)
    # 200 old shares at 5.3 become 20 new shares at 52: the day's price P&L is the
    # market move (-20), not the disappearance of the old security
    assert Decimal(day["price_pnl"]) == Decimal("-20")
    assert Decimal(day["value"]) == Decimal("1040")


def test_the_same_second_tie_gives_the_same_facts_in_any_row_order() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    forward = _build(_rows(document))
    reverse = _build(
        builder.AccountRows(
            tuple(reversed(document["transactions"])),
            tuple(reversed(document["positions"])),
            tuple(reversed(document["change_in_nav_observations"])),
        )
    )
    assert forward == reverse
    artifact = json.loads(forward)
    s3 = next(item for item in artifact["sleeves"] if item["sleeve_id"] == "s3")
    # the tagged sale goes first, so the untagged fraction belongs to the sleeve
    # that still holds the security and s3 leaves nothing behind
    assert s3["days"][-1]["event"] == "EXIT" and s3["days"][-1]["value"] == "0"
    assert not artifact["disclosures"]["offsetting_residues"]
    assert [
        item["sleeves"]
        for item in artifact["disclosures"]["manual_assignments"]
        if item["day"] == "2026-03-10" and item["how"] == "assigned"
    ] == ["s4"]


def test_a_catalog_is_cut_to_the_declared_account_and_window(tmp_path: Path) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    other = {**document["positions"][0], "account_id": "account-other"}
    early = {**document["positions"][0], "as_of_date": "2026-02-27"}
    catalog = {
        "provider_id": "provider-a",
        "catalog_schema_version": 7,
        "accounts": [{"account_id": ACCOUNT}, {"account_id": "account-other"}],
        "transactions": document["transactions"],
        "positions": [*document["positions"], other, early],
        "change_in_nav_observations": document["change_in_nav_observations"],
    }
    raw = _bytes(catalog)
    rows, sha = builder.rows_from_catalog(raw, ACCOUNT, "provider-a", DAYS[0], DAYS[-1])
    assert len(rows.positions) == len(document["positions"])
    assert len(rows.transactions) == len(document["transactions"])
    assert len(sha) == 64
    with pytest.raises(builder.SleeveFactsError, match="provider"):
        builder.rows_from_catalog(raw, ACCOUNT, "provider-b", DAYS[0], DAYS[-1])
    with pytest.raises(builder.SleeveFactsError, match="parent account"):
        builder.rows_from_catalog(
            raw, "account-missing", "provider-a", DAYS[0], DAYS[-1]
        )
    (tmp_path / "catalog.json").write_bytes(raw)
    (tmp_path / "tag.json").write_bytes(_bytes(_tag_map()))
    (tmp_path / "declared.json").write_bytes(_bytes(_declared()))
    args = [
        "--tag-map", str(tmp_path / "tag.json"),
        "--declared-inputs", str(tmp_path / "declared.json"),
        "--catalog", str(tmp_path / "catalog.json"),
        "--parent-ledger-generation-id", GENERATION,
        "--output", str(tmp_path / "out.json"),
    ]  # fmt: skip
    assert builder.main(args) == 0
    expected = builder.build_sleeve_account_facts(
        tag_map_bytes=_bytes(_tag_map()),
        declared_inputs_bytes=_bytes(_declared()),
        rows=rows,
        rows_sha256=sha,
        parent_ledger_generation_id=GENERATION,
    )
    assert (tmp_path / "out.json").read_bytes() == expected
    assert hashlib.sha256(expected).hexdigest()


def test_a_row_that_names_no_account_is_refused() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del document["positions"][0]["account_id"]
    with pytest.raises(builder.SleeveFactsError, match="names none"):
        _build(_rows(document))
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del document["change_in_nav_observations"][0]["account_id"]
    with pytest.raises(builder.SleeveFactsError, match="names none"):
        _build(_rows(document))


# --- the clean-room verifier against the builder -------------------------------


def _catalog_bytes(document: dict[str, Any], *, reverse: bool = False) -> bytes:
    def order(items: list[Any]) -> list[Any]:
        return list(reversed(items)) if reverse else list(items)

    return _bytes(
        {
            "provider_id": "provider-a",
            "catalog_schema_version": 7,
            "accounts": [{"account_id": ACCOUNT}],
            "transactions": order(document["transactions"]),
            "positions": order(document["positions"]),
            "change_in_nav_observations": order(document["change_in_nav_observations"]),
        }
    )


def _built_from(catalog: bytes) -> bytes:
    rows, sha = builder.rows_from_catalog(
        catalog, ACCOUNT, "provider-a", DAYS[0], DAYS[-1]
    )
    return builder.build_sleeve_account_facts(
        tag_map_bytes=_bytes(_tag_map()),
        declared_inputs_bytes=_bytes(_declared()),
        rows=rows,
        rows_sha256=sha,
        parent_ledger_generation_id=GENERATION,
    )


def _verify(artifact: bytes, catalog: bytes) -> verifier.FactsReport:
    rows, sha = verifier.select_catalog_rows(catalog, ACCOUNT, DAYS[0], DAYS[-1])
    return verifier.verify_sleeve_account_facts(
        artifact, _bytes(_tag_map()), _bytes(_declared()), rows, sha
    )


def _canonical(document: Any) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def test_builder_and_verifier_agree_on_the_synthetic_audit() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    catalog = _catalog_bytes(document)
    artifact = _built_from(catalog)
    report = _verify(artifact, catalog)
    assert report.mismatches == ()
    assert report.compared > 1000
    # nothing is "close enough" by accident: most figures are the same text
    assert report.byte_identical > report.within_tolerance
    parsed = json.loads(artifact)
    assert {leg["trace"]["kind"] for leg in parsed["flows"]} >= {"REALLOCATION"}
    # the verifier reads the rows in another order and still agrees on every
    # derived fact (the same-second tie is decided by the stated rule, not by row
    # order); only the rows' own digest, which is over their order, differs
    reordered = _verify(artifact, _catalog_bytes(document, reverse=True))
    assert [(item.table, item.key) for item in reordered.mismatches] == [
        ("bindings", "account_rows_sha256")
    ]


def _tampered(mutate: Any) -> tuple[bytes, bytes]:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    catalog = _catalog_bytes(document)
    parsed = json.loads(_built_from(catalog))
    mutate(parsed)
    return _canonical(parsed), catalog


def _flip_nav(parsed: dict[str, Any]) -> None:
    row = parsed["sleeves"][0]["days"][3]
    row["nav_end"] = str(Decimal(row["nav_end"]) + Decimal("0.01"))


def _flip_flow(parsed: dict[str, Any]) -> None:
    leg = next(item for item in parsed["flows"] if item["class"] == "INTERNAL_IN")
    leg["amount"] = str(Decimal(leg["amount"]) + Decimal("1"))


def _flip_pairing(parsed: dict[str, Any]) -> None:
    leg = next(
        item for item in parsed["flows"] if item["trace"]["pairing"] == "SLEEVE_PAIR"
    )
    leg["trace"] = {**leg["trace"], "pairing": "UNALLOCATED_REMAINDER"}


def _flip_own_pnl(parsed: dict[str, Any]) -> None:
    row = parsed["sleeves"][1]["days"][2]
    row["own_pnl"] = str(Decimal(row["own_pnl"]) - Decimal("0.5"))


def _flip_alloc(parsed: dict[str, Any]) -> None:
    row = parsed["allocated_cost"]["sleeve_allocations"][0]
    row["alloc_total"] = str(Decimal(row["alloc_total"]) + Decimal("0.01"))


def _flip_closure(parsed: dict[str, Any]) -> None:
    parsed["closure"][2]["unexplained"] = "0.5"


def _flip_tie_break(parsed: dict[str, Any]) -> None:
    parsed["bindings"]["builder_code"]["same_second_tie_break"]["rule"] = "ROW_ORDER"


def _swap_leg_accounts(parsed: dict[str, Any]) -> None:
    first, second = parsed["flows"][0], parsed["flows"][1]
    first["account_id"], second["account_id"] = (
        second["account_id"],
        first["account_id"],
    )


@pytest.mark.parametrize(
    ("mutate", "table"),
    [
        (_flip_nav, "sleeves"),
        (_flip_flow, "flows"),
        (_flip_pairing, "flows"),
        (_flip_own_pnl, "sleeves"),
        (_flip_alloc, "allocated_cost"),
        (_flip_closure, "closure"),
        (_flip_tie_break, "bindings"),
        (_swap_leg_accounts, "flows"),
    ],
)
def test_the_verifier_fails_when_a_fact_is_tampered(mutate: Any, table: str) -> None:
    artifact, catalog = _tampered(mutate)
    report = _verify(artifact, catalog)
    assert not report.verified
    assert table in {item.table for item in report.mismatches}


def test_the_verifier_fails_when_the_inputs_are_not_the_ones_bound() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    catalog = _catalog_bytes(document)
    artifact = _built_from(catalog)
    document["positions"][0]["mark_price"] = str(
        Decimal(document["positions"][0]["mark_price"]) + 1
    )
    report = _verify(artifact, _catalog_bytes(document))
    assert not report.verified
    assert "account_rows_sha256" in {item.key for item in report.mismatches}


def test_the_verifier_refuses_bytes_that_are_not_canonical() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    catalog = _catalog_bytes(document)
    spaced = json.dumps(json.loads(_built_from(catalog)), indent=1).encode()
    report = _verify(spaced, catalog)
    assert [item.table for item in report.mismatches] == ["artifact"]


def test_the_artifact_states_the_tie_break_and_binds_it_into_the_code_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    catalog = _catalog_bytes(document)
    code = json.loads(_built_from(catalog))["bindings"]["builder_code"]
    rule = code["same_second_tie_break"]
    assert rule["rule"].startswith("TAG_RESOLVED_FIRST>")
    assert rule["sha256"] == hashlib.sha256(rule["rule"].encode("ascii")).hexdigest()
    assert (
        code["sha256"]
        == hashlib.sha256(
            json.dumps(
                {
                    "files": code["files"],
                    "same_second_tie_break_sha256": rule["sha256"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    # another rule gives another digest, and the verifier no longer agrees
    monkeypatch.setattr(builder.ledger_module, "SAME_SECOND_TIE_BREAK", "ROW_ORDER")
    monkeypatch.setattr(
        builder.ledger_module,
        "SAME_SECOND_TIE_BREAK_SHA256",
        hashlib.sha256(b"ROW_ORDER").hexdigest(),
    )
    other = json.loads(_built_from(catalog))["bindings"]["builder_code"]
    assert other["sha256"] != code["sha256"]
    assert not _verify(_built_from(catalog), catalog).verified


def test_the_verifier_refuses_a_skipped_weekday_and_a_foreign_tag_field() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    catalog = _catalog_bytes(document)
    artifact = _built_from(catalog)
    document["change_in_nav_observations"] = [
        row
        for row in document["change_in_nav_observations"]
        if row["accounting_date"] != DAYS[2]
    ]
    with pytest.raises(verifier.SleeveVerificationError, match="skips from"):
        _verify(artifact, _catalog_bytes(document))
    rows, sha = verifier.select_catalog_rows(catalog, ACCOUNT, DAYS[0], DAYS[-1])
    other = _tag_map()
    other["parents"][0]["tag_field"] = "clientReference"
    with pytest.raises(verifier.SleeveVerificationError, match="clientReference"):
        verifier.verify_sleeve_account_facts(
            artifact, _bytes(other), _bytes(_declared()), rows, sha
        )


@pytest.mark.parametrize("version", [7, 8, 9])
def test_the_builder_and_the_independent_verifier_admit_later_catalog_schemas(
    version: int,
) -> None:
    """IBKR catalog 8 adds symbol/listing_exchange to positions; the facts read none of it."""

    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    positions = [
        {**row, "symbol": "SYNTH", "listing_exchange": "SYNTHEX"}
        for row in document["positions"]
    ]
    catalog = _bytes(
        {
            "provider_id": "provider-a",
            "catalog_schema_version": version,
            "accounts": [{"account_id": ACCOUNT}],
            "transactions": document["transactions"],
            "positions": positions,
            "change_in_nav_observations": document["change_in_nav_observations"],
        }
    )
    built_rows, built_sha = builder.rows_from_catalog(
        catalog, ACCOUNT, "provider-a", DAYS[0], DAYS[-1]
    )
    chosen, verified_sha = verifier.select_catalog_rows(
        catalog, ACCOUNT, DAYS[0], DAYS[-1]
    )

    assert len(built_rows.positions) == len(document["positions"])
    assert len(chosen["positions"]) == len(document["positions"])
    assert built_sha == verified_sha


@pytest.mark.parametrize("version", [6, "8", True, None])
def test_an_earlier_or_malformed_catalog_schema_is_refused_by_both(
    version: object,
) -> None:
    catalog = _bytes(
        {
            "provider_id": "provider-a",
            "catalog_schema_version": version,
            "accounts": [{"account_id": ACCOUNT}],
            "transactions": [],
            "positions": [],
            "change_in_nav_observations": [],
        }
    )
    with pytest.raises(builder.SleeveFactsError, match="schema-7-or-later"):
        builder.rows_from_catalog(catalog, ACCOUNT, "provider-a", DAYS[0], DAYS[-1])
    with pytest.raises(verifier.SleeveVerificationError, match="schema-7-or-later"):
        verifier.select_catalog_rows(catalog, ACCOUNT, DAYS[0], DAYS[-1])
