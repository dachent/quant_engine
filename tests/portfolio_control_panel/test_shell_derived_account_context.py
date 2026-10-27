"""A derived sleeve account is labelled and selected with its parent in the panel shell (D14, #567).

A sleeve epoch sits on ``<parent>-<sleeve>``; the account dimension lists provider accounts only.
The shell names a derived account by the label its account-manifest row carries and shows it
under its parent's account context.  An account the manifest does not name, or one that hangs on
no listed account or on two, stays "Unrecognized account" and is never shown under a parent.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).with_name("shell_harness.mjs")
APP = ROOT / "apps" / "portfolio-control-panel" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node required")

PARENT = "account-aaaa"
OTHER = "account-bbbb"
NESTED = (
    f"{PARENT}-s1"  # listed itself, so a name under it hangs on two listed accounts
)


def _performance(account_id: str) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "strategy_id": "strategy-x",
        "strategy_name": "Strategy X",
        "effective_start": "2026-01-30",
        "effective_end": None,
    }


def _run(tmp_path: Path, selected: str) -> dict[str, Any]:
    payload = {
        "meta": {"performance_lineage": {"status": "PASS"}},
        "strategies": [],
        "account_summary": [
            {"account_id": account_id, "provider": "synthetic", "account_label": label}
            for account_id, label in (
                (PARENT, "Synthetic 1111"),
                (OTHER, "Synthetic 2222"),
                (NESTED, "Synthetic 3333"),
            )
        ],
        "account_manifest": {
            "rows": [
                {"account_id": f"{PARENT}-s2", "account_label": "Synthetic 1111 / s2"},
                # hangs on two listed accounts (PARENT and NESTED): not a derived account
                {"account_id": f"{NESTED}-x", "account_label": "Synthetic 3333 / x"},
                # hangs on no listed account
                {
                    "account_id": "account-cccc-s1",
                    "account_label": "Synthetic 4444 / s1",
                },
            ]
        },
        "performance": [
            _performance(f"{PARENT}-s2"),  # named by the manifest, hangs on PARENT
            _performance(f"{PARENT}-s9"),  # not named by the manifest
            _performance(f"{NESTED}-x"),  # hangs on two listed accounts
            _performance("account-cccc-s1"),  # hangs on no listed account
            _performance(OTHER),
        ],
    }
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    calls = tmp_path / "calls.json"
    calls.write_text(
        json.dumps(
            [
                {"eval": f"selectedContextAccount = {json.dumps(selected)}"},
                {"fn": "renderLifecycleTable"},
                *(
                    {"eval": f"accountLabel({json.dumps(account)})"}
                    for account in (
                        f"{PARENT}-s2",
                        f"{PARENT}-s9",
                        f"{NESTED}-x",
                        "account-cccc-s1",
                    )
                ),
            ]
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(shutil.which("node")), str(HARNESS), str(APP), str(path), "", str(calls)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result: dict[str, Any] = json.loads(completed.stdout)
    assert result["errors"] == []
    return result


def _shown_accounts(result: dict[str, Any]) -> set[str]:
    table = result["dom"]["#lifecycle-table"]["innerHTML"]
    return {part.split('"', 1)[0] for part in table.split('data-account-id="')[1:]}


def test_a_derived_account_is_labelled_by_its_manifest_row(tmp_path: Path) -> None:
    result = _run(tmp_path, "ALL")
    labels = [item.get("value") for item in result["returns"][2:]]
    assert labels == [
        "Synthetic 1111 / s2",
        "Unrecognized account",
        "Unrecognized account",
        "Unrecognized account",
    ]


def test_selecting_the_parent_shows_exactly_its_derived_accounts(
    tmp_path: Path,
) -> None:
    shown = _shown_accounts(_run(tmp_path, PARENT))
    assert shown == {f"{PARENT}-s2"}


def test_other_accounts_never_show_under_a_parent_they_do_not_hang_on(
    tmp_path: Path,
) -> None:
    assert _shown_accounts(_run(tmp_path, OTHER)) == {OTHER}
    # the nested listed account is its own context; its unnamed child is not shown there
    assert _shown_accounts(_run(tmp_path, NESTED)) == set()
