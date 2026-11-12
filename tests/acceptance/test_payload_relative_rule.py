"""#893: payload limits computed from the previous committed release, under a code ceiling."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from acceptance.validate import checks, payload_rule

BUDGET: dict[str, Any] = {
    "blocks": {"grow": 1_000_000, "flat": 5_000},
    "unlisted_block_max_bytes": 100,
    "other_blocks_max_bytes": 150,
    "physical": {"total_bytes": 2_000_000},
    "committed_bytes": {
        "blocks": {"grow": 990_000, "flat": 4_950},
        "total_physical": 1_980_000,
    },
}


def over(blocks: dict[str, int], derived: dict[str, Any]) -> list[str]:
    return [f[0] for f in checks.payload_budget_findings(blocks, {}, derived)]


def derive(prev: dict[str, int], days: int | None, **kw: Any) -> dict[str, Any]:
    return payload_rule.derive_budget(
        BUDGET, prev, {"total_bytes": 1_980_000}, days, **kw
    )


def test_growth_inside_the_allowance_passes_where_the_file_limit_would_refuse() -> None:
    # previous 1_000_000, 7 added days: allowance 1 + 7 x 0.005 + 0.01 = 1.045
    size = 1_044_000
    assert over({"grow": size}, BUDGET) == [
        "grow"
    ]  # the old rule refuses (kill: old code)
    assert over({"grow": size}, derive({"grow": 1_000_000}, 7)) == []


def test_growth_past_the_allowance_is_refused() -> None:
    # kill test: the relative rule can refuse
    assert over({"grow": 1_046_000}, derive({"grow": 1_000_000}, 7)) == ["grow"]


def test_the_allowance_scales_with_the_added_days() -> None:
    assert over({"grow": 1_080_000}, derive({"grow": 1_000_000}, 7)) == ["grow"]
    assert over({"grow": 1_080_000}, derive({"grow": 1_000_000}, 21)) == []


def test_the_code_ceiling_binds_even_when_the_relative_rule_would_allow() -> None:
    ceiling = payload_rule.ceiling(990_000)  # 1.5 x committed
    far = {"grow": ceiling + 1}
    assert over(far, derive({"grow": ceiling}, 365)) == ["grow"]
    assert over({"grow": ceiling}, derive({"grow": ceiling}, 365)) == []


def test_the_reviewed_file_limit_still_admits_a_deliberate_raise() -> None:
    # a limit raised in the change that grows the block is never lowered by a small previous
    assert over({"flat": 5_000}, derive({"flat": 10}, 7)) == []
    assert over({"flat": 5_001}, derive({"flat": 10}, 7)) == ["flat"]


def test_no_previous_release_keeps_the_file_limits() -> None:
    assert payload_rule.derive_budget(BUDGET, None, None, None) == {
        "blocks": BUDGET["blocks"],
        "physical": BUDGET["physical"],
        "unlisted_block_max_bytes": 100,
        "other_blocks_max_bytes": 150,
    }


def test_a_previous_release_that_cannot_be_found_leaves_only_the_ceiling() -> None:
    derived = payload_rule.derive_budget(
        BUDGET, None, None, None, previous_unknown=True
    )
    assert derived["blocks"]["grow"] == payload_rule.ceiling(990_000)
    assert over({"grow": payload_rule.ceiling(990_000) + 1}, derived) == ["grow"]


def test_added_days_never_negative_and_unparsable_is_none() -> None:
    assert payload_rule.added_days("2026-09-17", "2026-11-06") == 50
    assert payload_rule.added_days("2026-11-06", "2026-09-17") == 0
    assert payload_rule.added_days(None, "2026-09-17") is None
    assert payload_rule.added_days("garbage", "2026-09-17") is None


def test_physical_limits_follow_the_previous_release_too() -> None:
    derived = derive({"grow": 1_000_000}, 7)  # previous total 1_980_000 -> 2_070_000
    assert checks.payload_budget_findings({}, {"total_bytes": 2_070_000}, derived) == []
    over_total = checks.payload_budget_findings({}, {"total_bytes": 2_071_000}, derived)
    assert [f[0] for f in over_total] == ["total_bytes"]


def _write_generation(root: Path, gen_id: str, as_of: str, grow_bytes: int) -> Path:
    gen = root / gen_id
    (gen / "data").mkdir(parents=True)
    (gen / "data" / "dashboard_data.json").write_bytes(
        json.dumps({"grow": "x" * grow_bytes}, sort_keys=True).encode()
    )
    (gen / "candidate.json").write_bytes(
        json.dumps({"portfolio_as_of": as_of}).encode()
    )
    (gen / "index.html").write_bytes(b"<html></html>")
    return gen


def _ctx(root: Path, pointer: dict[str, Any] | None, as_of: str, gen_id: str) -> Any:
    return SimpleNamespace(
        _cache={},
        gen=SimpleNamespace(
            gen_dir=root / gen_id,
            generation_id=gen_id,
            candidate={"portfolio_as_of": as_of},
            external=SimpleNamespace(current_pointer=pointer),
        ),
    )


def test_the_validator_reads_the_previous_generation_the_pointer_names(
    tmp_path: Path,
) -> None:
    _write_generation(tmp_path, "prev", "2026-09-17", 1000)
    _write_generation(tmp_path, "cand", "2026-09-24", 1020)
    ctx = _ctx(tmp_path, {"generation_id": "prev"}, "2026-09-24", "cand")
    blocks, physical, days, unknown, _schema = checks._previous_payload(ctx, {})
    assert days == 7 and unknown is False
    assert blocks is not None and blocks["grow"] == len(json.dumps("x" * 1000))
    assert physical is not None and physical["total_bytes"] > 0


def test_a_candidate_that_is_already_current_has_no_discoverable_predecessor(
    tmp_path: Path,
) -> None:
    _write_generation(tmp_path, "cand", "2026-09-24", 1020)
    ctx = _ctx(tmp_path, {"generation_id": "cand"}, "2026-09-24", "cand")
    assert checks._previous_payload(ctx, {}) == (None, None, None, True, None)


def test_no_current_pointer_means_the_file_limits() -> None:
    ctx = _ctx(Path("."), None, "2026-09-24", "cand")
    assert checks._previous_payload(ctx, {}) == (None, None, None, False, None)


def test_an_unreadable_previous_generation_falls_back_to_the_file_limits(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path, {"generation_id": "gone"}, "2026-09-24", "cand")
    assert checks._previous_payload(ctx, {}) == (None, None, None, False, None)


TABLE = (payload_rule.ContentAllowance("grow", 3, 0.10, "ticket 894: new rows"),)


def test_a_declared_content_change_widens_only_its_block_and_only_at_its_schema_bump() -> (
    None
):
    allowances = payload_rule.active_allowances(2, 3, TABLE)
    assert allowances == {"grow": 0.10}
    derived = derive({"grow": 1_000_000, "flat": 5_000}, 0, allowances=allowances)
    # zero added days (release A at the same cutoff): +8 % passes with the allowance
    assert over({"grow": 1_080_000}, derived) == []
    # kill test: the same growth refuses without a declared change
    assert over({"grow": 1_080_000}, derive({"grow": 1_000_000}, 0)) == ["grow"]
    # past the declared allowance it still refuses
    assert over({"grow": 1_120_000}, derived) == ["grow"]


def test_no_allowance_without_a_schema_change_or_for_other_versions() -> None:
    assert payload_rule.active_allowances(3, 3, TABLE) == {}
    assert payload_rule.active_allowances(2, 4, TABLE) == {}
    assert payload_rule.active_allowances(2, None, TABLE) == {}


def test_the_ceiling_binds_a_declared_content_change_too() -> None:
    derived = derive({"grow": 1_400_000}, 0, allowances={"grow": 5.0})
    assert derived["blocks"]["grow"] == payload_rule.ceiling(990_000)


def test_the_50mb_warning_follows_the_budget_not_a_literal() -> None:
    # #895 A6: dashboard_data.json of 292 MB is inside its committed budget: no warning
    ctx = SimpleNamespace(
        repo_root=None,
        _cache={
            "payload_effective": {
                "blocks": {},
                "unlisted_block_max_bytes": 1,
                "other_blocks_max_bytes": 1,
                "physical": {"dashboard_data_json_bytes": 301_000_000},
            }
        },
        gen=SimpleNamespace(
            scan=SimpleNamespace(
                files={
                    "data/dashboard_data.json": (292_000_000, "a"),
                    "other.bin": (60_000_000, "b"),
                },
                report_embedded_only=False,
            ),
            app_js="",
        ),
        panel={},
    )
    findings = [
        f for f in checks.check_lint_payload_size(ctx) if f.klass == checks.WARNING
    ]
    sizes = [f.observed for f in findings if f.entity == "generation"]
    assert sizes and "other.bin" in sizes[0]["files_over_50MB"]
    assert "data/dashboard_data.json" not in sizes[0]["files_over_50MB"]
