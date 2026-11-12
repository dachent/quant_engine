from __future__ import annotations

from typing import Any, Callable

import json

import pytest

import account_financing as af

A, B, C, D = (f"account-{c * 16}" for c in "abcd")


def _declaration(**facility: str) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "artifact_id": "account-financing-declaration",
            "as_of_date": "2026-11-06",
            "accounts": [
                {
                    "account_id": account.replace("_", "-"),
                    "scope": "ACTIVE",
                    "facility": value,
                    "verification": "VERIFIED",
                    "evidence_ref": "owner",
                }
                for account, value in facility.items()
            ],
        }
    ).encode()


def _load() -> dict[str, af.Declared]:
    return af.load_declaration(
        _declaration(
            **{
                A: "MARGIN_USED",
                B: "NO_MARGIN_FACILITY",
                C: "MARGIN_AVAILABLE_UNUSED",
                D: "NO_MARGIN_FACILITY",
            }
        )
    )


def test_the_three_states_follow_declaration_and_evidence() -> None:
    states = af.classify(
        _load(),
        {
            B: af.DebitEvidence(debit_interest_postings=6),
            C: af.DebitEvidence(currency_debit_days=3),
        },
        [A, B, C, D],
    )
    assert states[A].state == "LEVERED"
    assert states[B].state == "INCIDENTAL_DEBIT"
    assert states[C].state == "INCIDENTAL_DEBIT"
    assert states[D].state == "NOT_LEVERED"
    assert states[D].conflicts == ()


def test_evidence_overrides_a_denied_facility_upward_and_is_a_conflict() -> None:
    """Kill test: a NOT_LEVERED row for an account that shows a debit is refused."""

    states = af.classify(_load(), {D: af.DebitEvidence(net_base_debit_days=2)}, [D])
    assert states[D].state == "LEVERED"
    assert states[D].conflicts == ("DECLARED_NO_FACILITY_BUT_NET_BASE_DEBIT",)
    posted = af.classify(_load(), {B: af.DebitEvidence(debit_interest_postings=1)}, [B])
    assert posted[B].conflicts == ("DECLARED_NO_FACILITY_BUT_DEBIT_INTEREST",)


def test_an_account_missing_from_the_declaration_is_refused() -> None:
    with pytest.raises(af.FinancingDeclarationError, match="not on the financing"):
        af.classify(_load(), {}, ["account-" + "e" * 16])


def _set(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    return lambda d: d["accounts"][0].__setitem__(key, value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.__setitem__("artifact_id", "other"),
        _set("facility", "MAYBE"),
        _set("account_id", "account-short"),
        _set("extra", 1),
        lambda d: d["accounts"].append(dict(d["accounts"][0])),
    ],
)
def test_a_malformed_declaration_is_refused(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    doc = json.loads(_declaration(**{A: "MARGIN_USED"}))
    mutate(doc)
    with pytest.raises(af.FinancingDeclarationError):
        af.load_declaration(json.dumps(doc).encode())
