"""Disclosures of a release with derived sleeve epochs: every figure from governed bytes."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import sleeve_disclosures as disclosures

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INPUTS_DIR = REPOSITORY_ROOT / "ops" / "mvp2-release" / "inputs" / "sleeves"
PARENT = "account-1b44c987969b9e47"
S2 = f"{PARENT}-s2"
S1 = f"{PARENT}-s1"
EVIDENCE_TEXT = """=== p123-ai-factor IBKR 6907 account-1b44c987969b9e47  legs=5 (external=2)
committed TWR            2.453178 %
bounds b_all          lower -0.187871 pt  upper +0.000000 pt
bounds b_ext          lower +0.000000 pt  upper +0.100000 pt
bounds b_dt           lower -9.000000 pt  upper +9.000000 pt
=== p123-eu IBKR 9054 account-d69701da9143c430  legs=14 (external=7)
committed TWR            33.027382 %
bounds b_all          lower -2.203870 pt  upper +0.979914 pt
bounds b_ext          lower -1.715383 pt  upper +0.392776 pt
"""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def test_the_governed_evidence_file_parses_to_the_bounds_it_records() -> None:
    parsed = disclosures.parse_timestamp_evidence(
        (INPUTS_DIR / "timestamp-delta-result.txt").read_text(encoding="utf-8")
    )
    block = parsed[("p123-eu", "account-d69701da9143c430")]
    assert block["committed_twr_pct"] == "33.027382"
    assert (block["b_all_lower_pt"], block["b_all_upper_pt"]) == (
        "-2.203870",
        "+0.979914",
    )
    ai = parsed[("p123-ai-factor", PARENT)]
    assert (ai["b_all_lower_pt"], ai["b_all_upper_pt"]) == ("-0.187871", "+0.000000")


def test_the_pinned_disclosure_inputs_match_their_evidence_and_the_manifest_pin() -> (
    None
):
    inputs = json.loads((INPUTS_DIR / "disclosure-inputs.json").read_bytes())
    evidence = inputs["timestamp_bounds"]["evidence"]
    raw = (INPUTS_DIR / evidence["path"]).read_bytes()
    assert (_sha(raw), len(raw)) == (evidence["sha256"], evidence["size_bytes"])
    manifest = json.loads((INPUTS_DIR / "sleeve-policy-manifest.json").read_bytes())
    pin = manifest["disclosure-inputs"]
    pinned = (INPUTS_DIR / pin["path"]).read_bytes()
    assert (_sha(pinned), len(pinned)) == (pin["sha256"], pin["size_bytes"])


def _facts(tmp_path: Path) -> list[Path]:
    quantbase = {
        "artifact_id": "quantbase-fund-facts",
        "provider_id": "quantbase",
        "parent_account_id": "account-58e75e8d952fa1b8",
        "closure": {
            "account": {
                "cash": {
                    "derived_cash": "112.18",
                    "provider_settled_cash": "75.69",
                    "residual": "36.49",
                    "declared_residual": "36.49",
                    "residual_tolerance": "0.01",
                },
                "dividends": {
                    "total": "10",
                    "attributed_to_sleeves": {"crisis": "1.5", "singleindex": "2.25"},
                    "unattributed": "0",
                },
            }
        },
        "open_items": {
            "funding_attribution": {
                "evidence_class": "NOT_IN_SOURCE_BYTES",
                "treatment": "UNALLOCATED_REMAINDER",
                "unallocated_remainder_pairs": 12,
            },
            "dividend_nav_treatment": {
                "evidence_class": "NOT_IN_SOURCE_BYTES",
                "treatment": "NOT_FOLDED_INTO_NAV",
            },
        },
        "flows": [],
    }
    ibkr = {
        "artifact_id": "sleeve-account-facts",
        "provider_id": "ibkr",
        "parent_account_id": PARENT,
        "closure_policy": {
            "max_abs_unexplained": "0.04838",
            "window_unexplained": "-0.0149",
            "tolerance_per_day": "1",
            "tolerance_window": "1",
        },
        "disclosures": {
            "manual_assignments": [{"day": "2026-02-06"}] * 3,
            "reconciliation_gaps": [{"day": "2026-02-06"}],
            "reconciliation_scalings": [],
            "offsetting_residues": [{"day": "2026-03-24"}],
            "unallocated_dividends": [
                {"amount": "1.10", "day": "2026-03-26"},
                {"amount": "0.40", "day": "2026-03-31"},
            ],
        },
        "flows": [
            {
                "account_id": S2,
                "class": "INTERNAL_IN",
                "event_date": "2026-01-30",
                "amount": "9868.44",
                "pair_id": "2026-01-30:0001",
                "trace": {"evidence_refs": ["S2 entry funded by account margin"]},
            },
            {
                "account_id": S2,
                "class": "INTERNAL_IN",
                "event_date": "2026-02-17",
                "amount": "100",
                "pair_id": "2026-02-17:0001",
                "trace": {"evidence_refs": ["funded by the sale of s1"]},
            },
        ],
    }
    paths = []
    for name, document in (("qb.json", quantbase), ("ibkr.json", ibkr)):
        path = tmp_path / name
        path.write_bytes(json.dumps(document).encode())
        paths.append(path)
    return paths


def _declared(tmp_path: Path) -> Path:
    document = {
        "funding_events": {
            "s2": {
                "2026-01-30": {
                    "kind": "IN",
                    "note": "S2 entry funded by account margin",
                },
                "2026-02-17": {"kind": "IN", "note": "funded by the sale of s1"},
            },
            "s1": {"2026-01-26": {"kind": "IN", "note": "entry, 224 margin"}},
        }
    }
    path = tmp_path / "declared.json"
    path.write_bytes(json.dumps(document).encode())
    return path


def _inputs(
    tmp_path: Path, *, evidence: str = EVIDENCE_TEXT, extra: bool = False
) -> Path:
    raw = evidence.encode()
    (tmp_path / "evidence.txt").write_bytes(raw)
    document = {
        "schema_version": 1,
        "artifact_id": "sleeve-disclosure-inputs",
        "timestamp_bounds": {
            "evidence": {
                "path": "evidence.txt",
                "sha256": _sha(raw),
                "size_bytes": len(raw),
            },
            "entries": [
                {"strategy_id": "p123-ai-factor", "account_id": PARENT},
                {"strategy_id": "p123-eu", "account_id": "account-d69701da9143c430"},
                *(
                    [{"strategy_id": "gone", "account_id": "account-0"}]
                    if extra
                    else []
                ),
            ],
        },
    }
    path = tmp_path / "inputs.json"
    path.write_bytes(json.dumps(document).encode())
    return path


def test_sources_are_read_from_the_facts_and_the_pinned_evidence(
    tmp_path: Path,
) -> None:
    sources = disclosures.build_sources(
        facts_paths=_facts(tmp_path),
        inputs_path=_inputs(tmp_path),
        predecessor_data_root=None,
        declared_inputs_path=_declared(tmp_path),
    )
    quantbase = next(f for f in sources["facts"] if f["provider_id"] == "quantbase")
    assert quantbase["account_cash"]["residual"] == "36.49"
    ibkr = next(f for f in sources["facts"] if f["provider_id"] == "ibkr")
    assert ibkr["disclosure_counts"]["manual_assignments"] == 3
    assert Decimal(ibkr["unallocated_dividends"]["total"]) == Decimal("1.50")
    assert Decimal(ibkr["internal_in_by_day"][S2]["2026-01-30"]) == Decimal("9868.44")
    assert [(e["sleeve_id"], e["day"]) for e in sources["margin_funding_events"]] == [
        ("s1", "2026-01-26"),
        ("s2", "2026-01-30"),
    ]
    assert len(sources["timestamp_bounds"]["entries"]) == 2


def test_an_entry_the_evidence_does_not_hold_is_omitted_by_name(tmp_path: Path) -> None:
    sources = disclosures.build_sources(
        facts_paths=_facts(tmp_path),
        inputs_path=_inputs(tmp_path, extra=True),
        predecessor_data_root=None,
    )
    assert (
        sources["timestamp_bounds"]["entries"][2]["omitted"] == "NOT_IN_EVIDENCE_FILE"
    )
    result = disclosures.compose(
        sources,
        rows=[],
        invested_capital=None,
        sleeve_accounts={},
        product_strategies=[],
    )
    assert {
        "entry": "timestamp bound gone on account-0",
        "reason": "NOT_IN_EVIDENCE_FILE",
    } in result["omitted"]


def test_an_evidence_file_that_differs_from_its_pin_is_refused(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    (tmp_path / "evidence.txt").write_text(EVIDENCE_TEXT + "x", encoding="utf-8")
    with pytest.raises(disclosures.DisclosureError, match="differs from its pin"):
        disclosures.build_sources(
            facts_paths=_facts(tmp_path), inputs_path=inputs, predecessor_data_root=None
        )


def _without_reconciliation(omitted: list[dict[str, str]]) -> list[dict[str, str]]:
    """These fixtures carry no ledger, so the perimeter reconciliation is omitted by name."""

    return [
        o
        for o in omitted
        if not o["entry"].startswith(("perimeter reconciliation", "capital paid into"))
    ]


def _rows() -> list[dict[str, Any]]:
    def row(
        account: str, strategy: str, end: str | None, gain: str, twr: str
    ) -> dict[str, Any]:
        return {
            "account_id": account,
            "strategy_id": strategy,
            "effective_start": "2026-01-30",
            "effective_end": end,
            "end": "2026-09-17" if end is None else end,
            "status": "OK",
            "gain": gain,
            "observation_linked_return": twr,
        }

    return [
        row(S2, "p123-ai-factor", None, "100", "0.15"),
        row(S1, "retired-one", "2026-06-23", "1380.92", "0.02"),
        row(f"{PARENT}-s3", "retired-two", "2026-07-22", "-2241.72", "-0.17"),
        row(PARENT, "p123-ai-factor", None, "0", "0.02453178"),
        row("account-d69701da9143c430", "p123-eu", None, "0", "0.33027382"),
    ]


def _compose(
    tmp_path: Path, rows: list[dict[str, Any]], predecessor: Any = None
) -> dict[str, Any]:
    sources = disclosures.build_sources(
        facts_paths=_facts(tmp_path),
        inputs_path=_inputs(tmp_path),
        predecessor_data_root=None,
        declared_inputs_path=_declared(tmp_path),
    )
    if predecessor is not None:
        sources["predecessor"] = predecessor
    return disclosures.compose(
        sources,
        rows=rows,
        invested_capital={
            "current_by_strategy": [
                {"strategy_id": "p123-ai-factor", "invested_capital_usd": "1250.5"}
            ],
            "current_aggregate_usd": "5000.25",
        },
        sleeve_accounts={S1: PARENT, S2: PARENT, f"{PARENT}-s3": PARENT},
        product_strategies=["p123-ai-factor"],
    )


def test_every_disclosure_is_derived_from_the_sources_and_the_rows(
    tmp_path: Path,
) -> None:
    result = _compose(tmp_path, _rows())
    by_item = {item["item"]: item["value"] for item in result["items"]}
    assert "36.49" in by_item["Quantbase account cash"]
    assert "NOT_IN_SOURCE_BYTES" in by_item["Quantbase account cash"]
    assert "3.75" in by_item["Quantbase account cash"]
    assert (
        "0.0484" in by_item["Sleeve closure"]
        and "3 manual assignments" in by_item["Sleeve closure"]
    )
    assert "2026-01-30 9,868.44" in by_item["Sleeve capital basis"]
    assert "2026-02-17" not in by_item["Sleeve capital basis"]
    # the sleeves of the product's parent outside the product, from their own rows
    assert "-860.80 USD in total" in by_item["Product perimeter"]
    assert "s1 +1,380.92" in by_item["Product perimeter"]
    assert f"{S2} from 2026-01-30" in by_item["HWM member series"]
    assert "not attributed" in by_item["HWM member series"]
    # a bound is shown only against the row whose committed return it was measured on
    timing = by_item["Flow timing bounds"]
    assert "p123-ai-factor on account-1b44c987969b9e47 (cumulative TWR 2.45%)" in timing
    assert "-0.19 and +0.00" in timing and "p123-eu" in timing
    assert {
        entry["entry"] for entry in _without_reconciliation(result["omitted"])
    } == set()


def test_a_bound_whose_committed_return_differs_from_the_row_is_omitted_with_the_reason(
    tmp_path: Path,
) -> None:
    rows = _rows()
    rows[3]["observation_linked_return"] = "0.0300"
    result = _compose(tmp_path, rows)
    assert _without_reconciliation(result["omitted"]) == [
        {
            "entry": f"timestamp bound p123-ai-factor on {PARENT}",
            "reason": "COMMITTED_RETURN_DIFFERS_FROM_REPORT_ROW",
        }
    ]
    timing = next(
        i["value"] for i in result["items"] if i["item"] == "Flow timing bounds"
    )
    assert "p123-ai-factor" not in timing


def test_the_predecessor_comparison_is_read_by_hash_and_pointer_or_omitted(
    tmp_path: Path,
) -> None:
    previous = {
        "invested_capital": {
            "current_by_strategy": [
                {"strategy_id": "p123-ai-factor", "invested_capital_usd": "1000.4"}
            ],
            "current_aggregate_usd": "4000.21",
        }
    }
    data_root = tmp_path / "root"
    target = data_root / "gen" / "data.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(json.dumps(previous).encode())
    entry = {
        "release_id": "r" * 64,
        "panel_generation_id": "p" * 64,
        "data_file": {
            "relative_path": "gen/data.json",
            "sha256": _sha(target.read_bytes()),
        },
        "pointers": {
            "strategy_id": "p123-ai-factor",
            "strategy_capital_list": "/invested_capital/current_by_strategy",
            "strategy_capital_field": "invested_capital_usd",
            "aggregate": "/invested_capital/current_aggregate_usd",
        },
    }
    resolved = disclosures._resolve_predecessor(entry, data_root)
    assert resolved["status"] == "RESOLVED"
    result = _compose(tmp_path, _rows(), predecessor=resolved)
    text = next(
        i["value"] for i in result["items"] if i["item"] == "Sleeve capital basis"
    )
    assert "1,000.40" in text and "4,000.21" in text and "+250.10, +25.0%" in text
    # an unreachable, mis-hashed or unresolvable predecessor is omitted, never guessed
    assert (
        disclosures._resolve_predecessor(entry, None)["reason"]
        == "PREDECESSOR_DATA_ROOT_NOT_SUPPLIED"
    )
    assert (
        disclosures._resolve_predecessor(entry, tmp_path / "elsewhere")["reason"]
        == "PREDECESSOR_GENERATION_NOT_REACHABLE"
    )
    target.write_bytes(b"{}")
    assert (
        disclosures._resolve_predecessor(entry, data_root)["reason"]
        == "PREDECESSOR_GENERATION_HASH_DIFFERS"
    )
    omitted = _compose(
        tmp_path,
        _rows(),
        predecessor=disclosures._resolve_predecessor(entry, data_root),
    )["omitted"]
    assert _without_reconciliation(omitted) == [
        {
            "entry": "predecessor capital comparison",
            "reason": "PREDECESSOR_GENERATION_HASH_DIFFERS",
        }
    ]


def test_a_bound_measured_on_a_replaced_parent_epoch_is_read_against_the_strategys_series(
    tmp_path: Path,
) -> None:
    """The residual parent's own epoch has no report row: the strategy's account-live
    return is that parent account's series, and a bound is shown only when it equals the
    committed return it was measured against."""

    sources = disclosures.build_sources(
        facts_paths=_facts(tmp_path),
        inputs_path=_inputs(tmp_path),
        predecessor_data_root=None,
        declared_inputs_path=_declared(tmp_path),
    )
    rows = [row for row in _rows() if row["account_id"] != PARENT]

    def compose(own: Decimal | None) -> dict[str, Any]:
        return disclosures.compose(
            sources,
            rows=rows,
            invested_capital=None,
            sleeve_accounts={S1: PARENT, S2: PARENT},
            product_strategies=["p123-ai-factor"],
            strategy_returns={} if own is None else {"p123-ai-factor": own},
        )

    shown = compose(Decimal("0.02453178"))
    timing = next(
        i["value"] for i in shown["items"] if i["item"] == "Flow timing bounds"
    )
    assert (
        "the strategy's account-live series" in timing and "-0.19 and +0.00" in timing
    )
    assert all("p123-ai-factor" not in o["entry"] for o in shown["omitted"])
    differs = compose(Decimal("0.03"))
    assert {
        "entry": f"timestamp bound p123-ai-factor on {PARENT}",
        "reason": "COMMITTED_RETURN_DIFFERS_FROM_REPORT_ROW",
    } in differs["omitted"]
    absent = compose(None)
    assert {
        "entry": f"timestamp bound p123-ai-factor on {PARENT}",
        "reason": "NO_REPORT_ROW_FOR_THE_MEASURED_EPOCH",
    } in absent["omitted"]


def _committed_release(
    data_root: Path, release_id: str, panel_generation_id: str, **overrides: Any
) -> None:
    """A synthetic committed release authority: pointer, manifest, journal."""

    coordination = data_root / disclosures.COORDINATION_RELATIVE
    release_dir = coordination / "releases" / release_id
    release_dir.mkdir(parents=True)
    manifest = json.dumps(
        {
            "release_id": release_id,
            "targets": [
                {"root_id": "provider-ledger", "proposed_generation_id": "l" * 64},
                {
                    "root_id": "portfolio-panel",
                    "proposed_generation_id": panel_generation_id,
                },
            ],
        }
    ).encode()
    (release_dir / "release.json").write_bytes(manifest)
    (release_dir / "journal.json").write_bytes(
        json.dumps(
            {"release_id": release_id, "state": overrides.get("journal", "COMMITTED")}
        ).encode()
    )
    (coordination / "CURRENT_RELEASE.json").write_bytes(
        json.dumps(
            {
                "release_id": release_id,
                "release_manifest_sha256": overrides.get("sha", _sha(manifest)),
                "state": overrides.get("state", "COMMITTED"),
            }
        ).encode()
    )


def _pinned_predecessor() -> dict[str, Any]:
    inputs = json.loads((INPUTS_DIR / "disclosure-inputs.json").read_bytes())
    return dict(inputs["predecessor_comparison"])


def test_the_pinned_predecessor_must_be_the_committed_current_release_panel(
    tmp_path: Path,
) -> None:
    """The predecessor of the next release is the release committed at freeze time.

    A stale pin (the release before the committed one) still hashes cleanly, so only a
    comparison with the committed CURRENT_RELEASE authority can see it.  Synthetic
    release authorities only: the identifiers are the repository's own pins.
    """

    pinned = _pinned_predecessor()
    release_id = str(pinned["release_id"])
    panel = str(pinned["panel_generation_id"])
    assert len(release_id) == len(panel) == 64
    current = tmp_path / "current"
    _committed_release(current, release_id, panel)
    disclosures.check_predecessor_is_current_release(pinned, current)
    # the whole preparation path applies the same gate when a data root is supplied
    sources = disclosures.build_sources(
        facts_paths=[],
        inputs_path=INPUTS_DIR / "disclosure-inputs.json",
        predecessor_data_root=current,
    )
    assert sources["predecessor"]["reason"] == "PREDECESSOR_GENERATION_NOT_REACHABLE"

    # the committed release moved on: the pin is now the previous release and is refused
    newer = tmp_path / "newer"
    _committed_release(newer, "a" * 64, "b" * 64)
    with pytest.raises(disclosures.DisclosureError, match="not the committed CURRENT"):
        disclosures.check_predecessor_is_current_release(pinned, newer)
    with pytest.raises(disclosures.DisclosureError, match="not the committed CURRENT"):
        disclosures.build_sources(
            facts_paths=[],
            inputs_path=INPUTS_DIR / "disclosure-inputs.json",
            predecessor_data_root=newer,
        )

    # the release id alone is not enough: the panel generation must match as well
    other_panel = tmp_path / "other-panel"
    _committed_release(other_panel, release_id, "c" * 64)
    with pytest.raises(disclosures.DisclosureError, match="not the committed CURRENT"):
        disclosures.check_predecessor_is_current_release(pinned, other_panel)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"state": "PREPARED"}, "not COMMITTED"),
        ({"journal": "PREPARED"}, "not a committed release"),
        ({"sha": "0" * 64}, "differs from its pointer"),
    ],
)
def test_an_uncommitted_or_altered_release_authority_is_refused(
    tmp_path: Path, overrides: dict[str, str], message: str
) -> None:
    pinned = _pinned_predecessor()
    _committed_release(
        tmp_path,
        str(pinned["release_id"]),
        str(pinned["panel_generation_id"]),
        **overrides,
    )
    with pytest.raises(disclosures.DisclosureError, match=message):
        disclosures.check_predecessor_is_current_release(pinned, tmp_path)


def test_a_data_root_without_a_published_release_authority_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(disclosures.DisclosureError, match="not published"):
        disclosures.check_predecessor_is_current_release(
            _pinned_predecessor(), tmp_path
        )
