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
    assert states[D].reason == "DECLARED_NO_MARGIN_AND_NO_DEBIT_EVIDENCE"
    assert states[D].conflicts == ()
    # an unused facility is not "no margin"
    unused = af.classify(_load(), {}, [C])[C]
    assert unused.reason == "MARGIN_AVAILABLE_UNUSED_NO_DEBIT_EVIDENCE"


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
        _set("scope", []),
        _set("evidence_ref", 5),
        lambda d: d.__setitem__("as_of_date", "not-a-date"),
        lambda d: d.__setitem__("as_of_date", {}),
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


def _with_roles(roles: list[dict[str, object]]) -> bytes:
    import json as _json

    acct = "account-" + "a" * 16
    return _json.dumps(
        {
            "schema_version": 1,
            "artifact_id": "account-financing-declaration",
            "as_of_date": "2026-01-02",
            "accounts": [
                {
                    "account_id": acct,
                    "scope": "RESERVE",
                    "facility": "NO_MARGIN_FACILITY",
                    "verification": "VERIFIED",
                    "evidence_ref": "e",
                }
            ],
            "sleeve_roles": roles,
        }
    ).encode()


def _role(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "sleeve_alias": "r-01",
        "role": "RESERVE",
        "ledger_account_id": None,
        "nav_authority": "STATEMENT",
        "admit_windows": [
            {"start": "2026-01-01", "end": "2026-01-02", "basis": "STATEMENT_POINTS"}
        ],
        "evidence_ref": "e",
    }
    row.update(over)
    return row


def test_sleeve_roles_load_with_and_without_a_ledger_account() -> None:
    raw = _with_roles(
        [_role(), _role(sleeve_alias="r-02", ledger_account_id="account-" + "a" * 16)]
    )
    roles = af.load_sleeve_roles(raw)
    assert roles["r-01"].ledger_account_id is None
    assert roles["r-02"].admit_windows[0][2] == "STATEMENT_POINTS"


def test_a_sleeve_role_naming_an_undeclared_ledger_account_or_no_window_is_refused() -> (
    None
):
    import pytest

    for bad in (
        _role(ledger_account_id="account-" + "b" * 16),
        _role(admit_windows=[]),
        _role(role="LIVE"),
    ):
        with pytest.raises(af.FinancingDeclarationError):
            af.load_sleeve_roles(_with_roles([bad]))


def test_reserve_nav_windows_come_only_from_account_live_reserves_with_closed_windows() -> (
    None
):
    acct = "account-" + "a" * 16
    raw = _with_roles(
        [
            _role(
                sleeve_alias="live",
                ledger_account_id=acct,
                nav_authority="ACCOUNT_LIVE",
                admit_windows=[
                    {
                        "start": "2026-01-01",
                        "end": "2026-01-02",
                        "basis": "EXACT_FLOW_CLOSURE",
                    },
                    {"start": "2026-01-03", "end": None, "basis": "EXACT_FLOW_CLOSURE"},
                ],
            ),
            _role(sleeve_alias="statement"),
            _role(sleeve_alias="excluded", role="EXCLUDED_IMMATERIAL"),
        ]
    )
    windows = af.reserve_nav_windows(af.load_sleeve_roles(raw))
    assert list(windows) == [acct]
    assert [(w[0].isoformat(), w[1].isoformat()) for w in windows[acct]] == [
        ("2026-01-01", "2026-01-02")
    ]


def _roles(
    windows_a: list[dict[str, str]], windows_b: list[dict[str, str]] | None = None
) -> bytes:
    top = json.loads(_declaration(**{A: "MARGIN_USED"}))
    top["sleeve_roles"] = [
        {
            "sleeve_alias": alias,
            "role": "RESERVE",
            "ledger_account_id": A,
            "nav_authority": "ACCOUNT_LIVE",
            "admit_windows": windows,
            "evidence_ref": "owner",
        }
        for alias, windows in (("r1", windows_a), ("r2", windows_b))
        if windows is not None
    ]
    return json.dumps(top).encode()


def _win(start: str, end: str) -> dict[str, str]:
    return {"start": start, "end": end, "basis": "EXACT_IDENTITY_CLOSURE"}


def test_overlapping_declared_reserve_windows_refuse() -> None:
    ok = af.reserve_nav_windows(
        af.load_sleeve_roles(
            _roles([_win("2026-01-01", "2026-01-31"), _win("2026-02-01", "2026-02-28")])
        )
    )
    assert len(ok[A]) == 2
    for blob in (
        _roles([_win("2026-01-01", "2026-01-31"), _win("2026-01-31", "2026-02-28")]),
        _roles([_win("2026-01-01", "2026-01-31")], [_win("2026-01-15", "2026-02-15")]),
        _roles([_win("2026-02-01", "2026-01-01")]),
    ):
        with pytest.raises(af.FinancingDeclarationError):
            af.reserve_nav_windows(af.load_sleeve_roles(blob))


def test_a_verified_no_margin_declaration_is_the_not_levered_evidence() -> None:
    declared = _load()  # B is NO_MARGIN_FACILITY, verified
    quiet = af.classify(declared, {B: af.DebitEvidence(observed_base_days=0)}, [B])[B]
    assert quiet.state == "NOT_LEVERED" and not quiet.conflicts
    available = af.classify(declared, {C: af.DebitEvidence(observed_base_days=0)}, [C])[
        C
    ]
    assert "NOT_LEVERED_WITHOUT_OBSERVED_CASH_DAYS" in available.conflicts
