"""B6: the HWM calendar authority is authored from a first attempt's own evidence."""

from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from tools import release_hwm_calendar as cal

TOOLS = Path(cal.__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import portfolio_hwm_selected_inputs as adapter  # noqa: E402

CUTOFF = "2026-03-10"
START = date(2026, 3, 1)


def _strategies() -> list[str]:
    contract = json.loads((cal.REPO_ROOT / cal.DEFAULT_CONTRACT).read_bytes())
    return cal.required_strategies(contract)


def _accepted_policy_id() -> str:
    policy = json.loads((cal.REPO_ROOT / cal.DEFAULT_HWM_POLICY).read_bytes())

    def find(node: Any) -> list[str] | None:
        if isinstance(node, dict):
            if "accepted_governed_calendar_policy_ids" in node:
                return list(node["accepted_governed_calendar_policy_ids"])
            for value in node.values():
                found = find(value)
                if found:
                    return found
        return None

    ids = find(policy)
    assert ids
    return ids[0]


def _days(count: int, skip: tuple[int, ...] = ()) -> list[str]:
    return [
        (START + timedelta(days=index)).isoformat()
        for index in range(count)
        if index not in skip
    ]


def _write(path: Path, document: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(document).encode("utf-8"))
    return path


class World:
    """A synthetic first attempt: the first required strategy has a gap, the rest none."""

    def __init__(self, root: Path, *, late: bool = False, drop: str | None = None):
        self.root = root
        strategies = _strategies()
        self.gapped = strategies[0]
        rows = []
        for strategy_id in strategies:
            if strategy_id == drop:
                continue
            skip = (3, 4) if strategy_id == self.gapped else ()
            days = _days(10 if not late else 12, skip)
            rows.append(
                {
                    "epoch": {"strategy_id": strategy_id},
                    "nav_observations": [{"as_of_date": day} for day in days],
                }
            )
        self.run = root / "run"
        _write(
            self.run
            / "stages/epoch/generations/epoch-g/authority/calculation-inputs.json",
            {"rows": rows},
        )
        for stage in ("ledger", "baseline"):
            _write(
                self.run / f"stages/{stage}/generations/{stage}-g/generation.json", {}
            )
        _write(self.run / "preparation/sleeve-facts/one-facts.json", {})
        self.census = _write(root / "census.json", {})
        self.admission = _write(root / "admission.json", {})
        self.spec = _write(
            root / "spec.json", {"epoch_membership_census": {"path": str(self.census)}}
        )
        self.seed = _write(
            root / "seed.json",
            {
                "calendar_policy_id": _accepted_policy_id(),
                "owner_decision_evidence": "synthetic owner decision text",
            },
        )
        self.resolved = _write(
            root / "resolved.json",
            {
                "schema_version": cal.RESOLVED_VERSION,
                "cycle_id": "synthetic",
                "cutoff": CUTOFF,
                "cycle_manifest_sha256": "a" * 64,
                "predecessor": {"release_id": "synthetic-prev"},
                "epoch_admission_policy": {"path": str(self.admission)},
            },
        )
        self.out = root / "out" / "authority.json"

    def argv(self, *extra: str) -> list[str]:
        return [
            "author",
            "--resolved-cycle",
            str(self.resolved),
            "--resolved-cycle-sha256",
            hashlib.sha256(self.resolved.read_bytes()).hexdigest(),
            "--run",
            str(self.run),
            "--spec",
            str(self.spec),
            "--seed-authority",
            str(self.seed),
            "--out",
            str(self.out),
            *extra,
        ]


@pytest.fixture
def fake_adapter(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stand in for the selected-source adapter: it derives one return per gap."""

    state: dict[str, Any] = {"omit": False, "calls": 0}

    def build_selected(**kwargs: Any) -> dict[str, Any]:
        state["calls"] += 1
        rows = (
            []
            if state["omit"]
            else [
                {
                    "strategy_id": strategy,
                    "interval_start": (START + timedelta(days=2)).isoformat(),
                    "interval_end": (START + timedelta(days=5)).isoformat(),
                    "net_return": "0.0123",
                }
                for strategy in _strategies()[:1]
            ]
        )
        rows.append(  # a one-day row is never bound
            {
                "strategy_id": "other",
                "interval_start": START.isoformat(),
                "interval_end": (START + timedelta(days=1)).isoformat(),
                "net_return": "9",
            }
        )
        return adapter.build_governed_hwm_inputs(return_observations=rows)

    monkeypatch.setattr(adapter, "build_selected_hwm_inputs", build_selected)
    return state


def test_authority_declares_the_gap_and_binds_the_adapters_value(
    tmp_path: Path, fake_adapter: dict[str, Any]
) -> None:
    world = World(tmp_path)
    assert cal.main(world.argv()) == 0
    authority = json.loads(world.out.read_bytes())
    kinds = {
        row["strategy_id"]: row["calendar_kind"]
        for row in authority["source_calendars"]
    }
    assert kinds[world.gapped] == "DECLARED_SESSION"
    assert {kind for name, kind in kinds.items() if name != world.gapped} == {
        "ALWAYS_ON"
    }
    (interval,) = authority["accepted_source_intervals"]
    assert interval["strategy_id"] == world.gapped
    assert interval["prior_observation_date"] == (START + timedelta(days=2)).isoformat()
    assert interval["next_observation_date"] == (START + timedelta(days=5)).isoformat()
    assert interval["binding_return_value"] == "0.0123"
    assert authority["calendar_policy_id"] == _accepted_policy_id()
    assert world.out.read_bytes() == cal.render(authority)
    receipt = json.loads(
        world.out.with_name("authority.json.receipt.json").read_bytes()
    )
    assert receipt["cycle_manifest_sha256"] == "a" * 64
    assert receipt["bound_interval_count"] == 1
    assert receipt["epoch_generation_id"] == "epoch-g"


def test_check_passes_on_the_authored_file_and_fails_on_any_change(
    tmp_path: Path, fake_adapter: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    assert cal.main(world.argv()) == 0
    assert cal.main(world.argv("--check")) == 0
    world.out.write_bytes(world.out.read_bytes() + b" ")
    assert cal.main(world.argv("--check")) == 1
    assert "DIFFERS" in capsys.readouterr().err


def test_output_is_create_only(
    tmp_path: Path, fake_adapter: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    assert cal.main(world.argv()) == 0
    world.out.write_bytes(b"different")
    assert cal.main(world.argv()) == 1
    assert "OUTPUT_EXISTS" in capsys.readouterr().err


def test_an_interrupted_pair_is_completed_and_a_missing_receipt_fails_check(
    tmp_path: Path, fake_adapter: dict[str, Any]
) -> None:
    world = World(tmp_path)
    assert cal.main(world.argv()) == 0
    receipt = world.out.with_name("authority.json.receipt.json")
    receipt.unlink()
    assert cal.main(world.argv("--check")) == 1
    assert cal.main(world.argv()) == 0
    assert cal.main(world.argv("--check")) == 0


def test_an_edited_resolved_cycle_is_refused(
    tmp_path: Path, fake_adapter: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    argv = world.argv()
    document = json.loads(world.resolved.read_bytes())
    document["cutoff"] = "2027-01-01"
    world.resolved.write_bytes(json.dumps(document).encode("utf-8"))
    assert cal.main(argv) == 1
    assert "HASH_MISMATCH" in capsys.readouterr().err


def test_an_interval_the_adapter_did_not_derive_is_refused(
    tmp_path: Path, fake_adapter: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    fake_adapter["omit"] = True
    world = World(tmp_path)
    assert cal.main(world.argv()) == 1
    assert "UNBOUND_INTERVAL" in capsys.readouterr().err
    assert not world.out.exists()


def test_observations_after_the_cutoff_are_refused(
    tmp_path: Path, fake_adapter: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path, late=True)
    assert cal.main(world.argv()) == 1
    assert "AFTER_CUTOFF" in capsys.readouterr().err
    assert fake_adapter["calls"] == 0


def test_a_required_strategy_without_observations_is_refused(
    tmp_path: Path, fake_adapter: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path, drop=_strategies()[-1])
    assert cal.main(world.argv()) == 1
    assert "STRATEGY_PERIMETER" in capsys.readouterr().err


def test_two_staged_generations_need_a_name(
    tmp_path: Path, fake_adapter: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    _write(world.run / "stages/ledger/generations/ledger-h/generation.json", {})
    assert cal.main(world.argv()) == 1
    assert "AMBIGUOUS_GENERATION" in capsys.readouterr().err
    assert cal.main(world.argv("--ledger-generation", "ledger-g")) == 0


@pytest.mark.parametrize("change", ["schema", "seed_text", "no_sleeve_facts"])
def test_malformed_inputs_are_refused(
    tmp_path: Path,
    fake_adapter: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    change: str,
) -> None:
    world = World(tmp_path)
    if change == "schema":
        _write(world.resolved, {"schema_version": "other/1"})
        expected = "RESOLVED_CYCLE"
    elif change == "seed_text":
        _write(world.seed, {"calendar_policy_id": "x", "owner_decision_evidence": " "})
        expected = "SEED"
    else:
        (world.run / "preparation/sleeve-facts/one-facts.json").unlink()
        expected = "MISSING"
    assert cal.main(world.argv()) == 1
    assert expected in capsys.readouterr().err


def test_the_module_names_no_date_id_or_workstation_path() -> None:
    text = Path(cal.__file__).read_text(encoding="utf-8")
    assert "C:\\" not in text and "C:/" not in text
    assert not any(part.isdigit() and len(part) == 4 for part in text.split('"'))


def _stale_seed(path: Path) -> dict[str, Any]:
    """A governed authority a predecessor head issued: valid for a policy hash that is not this head's."""

    from quant_engine.portfolio_ledger import derive_governed_calendar_authority

    authority = dict(
        derive_governed_calendar_authority(
            strategy_calendars=[
                {
                    "strategy_id": strategy,
                    "calendar_kind": "ALWAYS_ON",
                    "observed_dates": _days(5),
                    "closing_capital_usd_by_date": {},
                }
                for strategy in _strategies()
            ],
            calendar_policy_id=_accepted_policy_id(),
            owner_decision_evidence="synthetic owner decision text",
            hwm_policy_sha256="f" * 64,
        )
    )
    _write(path, authority)
    return authority


def _reissue(seed: Path, out: Path, *extra: str) -> int:
    return cal.main(
        ["reissue", "--seed-authority", str(seed), "--out", str(out), *extra]
    )


def test_reissue_rebinds_the_seed_to_this_heads_policy_and_carries_the_rest(
    tmp_path: Path,
) -> None:
    from quant_engine.portfolio_ledger import derive_governed_calendar_authority

    seed = tmp_path / "seed.json"
    old = _stale_seed(seed)
    out = tmp_path / "out" / "seed-authority.json"
    assert _reissue(seed, out) == 0
    _effective, policy_sha = cal.effective_policy_sha256(cal.REPO_ROOT)
    new = json.loads(out.read_bytes())
    assert new["hwm_policy_sha256"] == policy_sha != old["hwm_policy_sha256"]
    # byte-identical to what the derivation itself writes for this head's policy
    expected = derive_governed_calendar_authority(
        strategy_calendars=[
            {
                "strategy_id": strategy,
                "calendar_kind": "ALWAYS_ON",
                "observed_dates": _days(5),
                "closing_capital_usd_by_date": {},
            }
            for strategy in _strategies()
        ],
        calendar_policy_id=_accepted_policy_id(),
        owner_decision_evidence="synthetic owner decision text",
        hwm_policy_sha256=policy_sha,
    )
    assert out.read_bytes() == cal.render(expected)
    assert _reissue(seed, out) == 0  # the same bytes complete
    assert _reissue(seed, out, "--check") == 0


def test_reissue_refuses_a_different_existing_file_and_a_seed_without_a_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed = tmp_path / "seed.json"
    _stale_seed(seed)
    out = tmp_path / "out.json"
    out.write_bytes(b"{}")
    assert _reissue(seed, out) == 1
    assert "OUTPUT_EXISTS" in capsys.readouterr().err
    assert _reissue(seed, out, "--check") == 1
    broken = _write(tmp_path / "broken.json", {"calendar_policy_id": "x"})
    assert _reissue(broken, tmp_path / "other.json") == 1
    assert "SEED" in capsys.readouterr().err
