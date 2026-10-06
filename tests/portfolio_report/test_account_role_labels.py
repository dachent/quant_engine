from __future__ import annotations

import portfolio_report as reporter


def _label(
    rows: list[dict[str, object]], *, product: set[str] | None = None
) -> list[dict[str, object]]:
    return reporter._label_account_roles(
        rows,
        product_account_ids={"prod"} if product is None else product,
        reserve_account_ids={"res"},
        excluded_account_ids={"gone"},
    )


def test_every_account_row_gets_a_role_and_excluded_accounts_are_dropped() -> None:
    rows = [
        {"account_id": "prod"},
        {"account_id": "res"},
        {"account_id": "gone"},
        {"account_id": "stray"},
        {"account_id": None},
    ]

    assert [(row["account_id"], row["account_role"]) for row in _label(rows)] == [
        ("prod", "PRODUCT"),
        ("res", "RESERVE"),
        ("stray", "HISTORY_ONLY_NO_CURRENT_ASSIGNMENT"),
        (None, None),
    ]


def test_a_current_product_account_is_never_dropped_as_excluded() -> None:
    labelled = reporter._label_account_roles(
        [{"account_id": "both"}],
        product_account_ids={"both"},
        reserve_account_ids=set(),
        excluded_account_ids={"both"},
    )

    assert labelled == [{"account_id": "both", "account_role": "PRODUCT"}]


def test_legacy_reports_without_a_product_perimeter_keep_baseline_rows() -> None:
    rows = [{"account_id": "stray"}]

    assert reporter._label_account_roles(
        rows,
        product_account_ids=None,
        reserve_account_ids=set(),
        excluded_account_ids=set(),
    ) == [{"account_id": "stray"}]


def test_reserve_components_come_from_the_custody_reserve_block() -> None:
    assert reporter._reserve_account_ids({"reserve": {"components": ["a", "b"]}}) == {
        "a",
        "b",
    }
    assert reporter._reserve_account_ids({"reserve": None}) == set()
    assert reporter._reserve_account_ids({}) == set()
