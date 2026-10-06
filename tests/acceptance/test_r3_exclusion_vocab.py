"""R3 (5): the owner-exclusion predicate matches the crosswalk contract's vocabulary.

contracts/account-strategy-current-epoch-crosswalk-v1.json defines exactly three
dispositions.  ``COMPOSER_OWNER_EXCLUDED_IMMATERIAL`` is an ``owner_reason`` label the
producer maps from ``EXCLUDED_IMMATERIAL`` (tools/portfolio_report.py), not a disposition,
so the pinned pair is the whole owner-exclusion vocabulary.
"""

from __future__ import annotations

import json
from pathlib import Path

from test_r2_exclusions import (
    _add_crosswalk_row,
    _context,
    _not_derivable,
    _row,
)
from test_validate_generation import Spec, build

from acceptance.validate import checks

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _contract_dispositions() -> set[str]:
    contract = json.loads(
        (
            REPOSITORY_ROOT
            / "contracts"
            / "account-strategy-current-epoch-crosswalk-v1.json"
        ).read_bytes()
    )
    found = set(contract["disposition_counts"])
    found |= {row["product_epoch_disposition"] for row in contract["rows"]}
    return found


def test_the_owner_exclusion_vocabulary_is_the_contracts() -> None:
    defined = _contract_dispositions()
    assert defined == {
        "EXCLUDED_DEPRECATED_HISTORY_ONLY",
        "EXCLUDED_IMMATERIAL",
        "INCLUDED_PRODUCT_EPOCH",
    }
    assert {d for d in defined if d.startswith("EXCLUDED_")} == (
        checks.OWNER_EXCLUSION_DISPOSITIONS
    )
    assert "COMPOSER_OWNER_EXCLUDED_IMMATERIAL" not in defined


def test_every_contract_disposition_is_classified_as_the_contract_says(
    tmp_path: Path,
) -> None:
    for number, disposition in enumerate(sorted(_contract_dispositions())):
        account = f"K{number}"
        hook = _add_crosswalk_row(
            account_id=account, product_epoch_disposition=disposition
        )
        gen_dir = build(tmp_path / f"d{number}", Spec(post_write=[hook]))
        context = _context(gen_dir, [_row("twr.account", account)])
        excluded = [r["entity"] for r in checks.expected_exclusions(context)]
        if disposition.startswith("EXCLUDED_"):
            assert excluded == [account], disposition
            assert _not_derivable(checks.run_checks(context)) == [], disposition
        else:
            assert excluded == [], disposition
            assert _not_derivable(checks.run_checks(context)) == [
                (f"twr.account:{account}", checks.NOT_READY)
            ], disposition
