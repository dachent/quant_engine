"""Effective-perimeter refusals and unchanged baseline bytes."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
import test_derive_a as original
from derive_a import combined
from derive_a.sources import Assignment, Crosswalk, INCLUDED, InputError

BASE = Path(__file__).resolve().parents[3]
real_inputs = pytest.mark.skipif(
    not (BASE / "inputs/sleeve-inputs.json").exists(),
    reason="real R2 inputs are only present in the seat tree",
)
START = dt.date(2026, 1, 1)


def row(account: str, strategy: str = "product") -> Assignment:
    return Assignment(account, strategy, "TEST", "", START, None, INCLUDED)


def identity(account: str, strategy: str = "product") -> dict[str, Any]:
    return dict(
        account_id=account,
        strategy_id=strategy,
        effective_start=START.isoformat(),
        effective_end=None,
    )


def test_valid_replacement() -> None:
    crosswalk = Crosswalk(START, START, (row("parent"), row("live", "other")))
    epochs = [identity("derived")]
    assert combined.perimeter(
        crosswalk, epochs, {"derived": "parent"}, [*epochs, identity("live", "other")]
    ) == {"parent"}


@pytest.mark.parametrize("mutation", ["extra", "missing", "kept", "nonparent"])
def test_perimeter_refusals(mutation: str) -> None:
    crosswalk = Crosswalk(START, START, (row("parent"), row("live", "other")))
    epochs = [identity("derived")]
    declared = [*epochs, identity("live", "other")]
    if mutation == "extra":
        declared.append(identity("extra"))
    elif mutation == "missing":
        declared.pop(0)
    elif mutation == "kept":
        declared.append(identity("parent"))
    else:
        declared.pop(1)
    with pytest.raises(InputError):
        combined.perimeter(crosswalk, epochs, {"derived": "parent"}, declared)


def test_partial_parent_refused() -> None:
    crosswalk = Crosswalk(START, START, (row("parent"), row("parent", "other")))
    with pytest.raises(InputError, match="partly"):
        combined.perimeter(
            crosswalk,
            [identity("derived")],
            {"derived": "parent"},
            [identity("derived"), identity("parent", "other")],
        )


def test_duplicate_derived_start_refused() -> None:
    crosswalk = Crosswalk(START, START, (row("parent"),))
    duplicate = identity("derived") | {"effective_end": "2026-02-01"}
    with pytest.raises(InputError, match="duplicate derived"):
        combined.perimeter(
            crosswalk,
            [identity("derived"), duplicate],
            {"derived": "parent"},
            [identity("derived")],
        )


@real_inputs
@pytest.mark.parametrize("duplicate", [False, True])
def test_declared_duplicate_identity_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    duplicate: bool,
) -> None:
    import hashlib
    from derive_a import cli

    manifest = json.loads(
        (BASE / "inputs/acceptance-inputs-manifest.json").read_bytes()
    )
    entry = next(e for e in manifest["inputs"] if e["family"] == "declared_cutoff_date")
    declared = json.loads(Path(entry["path"]).read_bytes())
    ordinary = next(
        r for r in declared["rows"] if r["account_id"] == "account-58e75e8d952fa1b8"
    )
    key = combined.identity(ordinary)
    if duplicate:
        declared["rows"].append(dict(ordinary))
    payload = json.dumps(declared).encode()
    selected = tmp_path / "declared.json"
    selected.write_bytes(payload)
    entry.update(
        path=str(selected),
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    selection = tmp_path / "inputs.json"
    selection.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "result.json"
    status = cli.main(
        [
            "--qe-root",
            str(BASE / "tree/qe-root"),
            "--out",
            str(output),
            "--inputs",
            str(selection),
            "--sleeve-inputs",
            str(BASE / "inputs/sleeve-inputs.json"),
        ]
    )
    if duplicate:
        assert status == 2
        error = capsys.readouterr().err
        assert "input error:" in error
        assert "duplicate declared epoch" in error
        assert repr(key) in error
        assert not output.exists()
    else:
        assert status == 0
        assert output.exists()


@real_inputs
def test_without_sleeves_exact_original_bytes() -> None:
    actual = original.derive.dumps(original._run(BASE / "out/compat-fixture"))
    assert actual == (BASE / "out/before-no-sleeves.json").read_bytes()


def test_no_derived_requires_exact_baseline() -> None:
    crosswalk = Crosswalk(START, START, (row("live"),))
    assert combined.perimeter(crosswalk, [], {}, [identity("live")]) == set()
    with pytest.raises(InputError, match="non-parent"):
        combined.perimeter(crosswalk, [], {}, [])


@pytest.fixture(scope="module")
def real_result() -> dict[str, Any]:
    derive = original.derive
    manifest_path = BASE / "inputs/acceptance-inputs-manifest.json"
    return derive.derive(
        BASE / "tree/qe-root",
        BASE / "tree",
        cutoff=dt.date(2026, 9, 17),
        model="test",
        code_commit="test",
        manifest=derive.load_inputs_manifest(
            manifest_path.read_bytes(), str(manifest_path)
        ),
        sleeve_inputs=BASE / "inputs/sleeve-inputs.json",
    )


@real_inputs
def test_real_metrics_and_only_allowed_change(real_result: dict[str, Any]) -> None:
    before = json.loads((BASE / "out/before.json").read_bytes())

    def key(r: dict[str, Any]) -> tuple[str, str, str]:
        return (r["metric_id"], r["entity"], r["window"])

    old = {key(r): r for r in before["metrics"]}
    new = {key(r): r for r in real_result["metrics"]}
    assert old.keys() == new.keys()
    changed = {k for k in old if old[k]["value"] != new[k]["value"]}
    assert changed == {("mwr.strategy", "p123-ai-factor", "ITD")}
    assert (
        new[("mwr.strategy", "p123-ai-factor", "ITD")]["value"]
        == new[("mwr.account", "account-1b44c987969b9e47-s2", "ITD")]["value"]
    )
    for k in old:
        if k[0] in {
            "twr.strategy",
            "capital.strategy.cutoff",
            "invested_capital.strategy.cutoff",
            "weight.strategy.cutoff",
            "universe.active_strategy_count",
            "universe.active_strategy_ids",
        }:
            assert old[k] == new[k]
    parent = "account-1b44c987969b9e47"
    account_metrics = {
        "twr.account",
        "mwr.account",
        "flows.account.net",
        "flows.account.count",
    }
    for section in ("metrics", "not_derivable"):
        assert not any(
            r["entity"] == parent and r["metric_id"] in account_metrics
            for r in real_result[section]
        )
    assert ("capital.account.cutoff", parent, None) in new
    assert ("date.source_last_observation", parent, None) in new
    for epoch in real_result["sleeve_derivation"]["epochs"]:
        assert ("flows.account.count", epoch["account_id"], "ITD") in new
    assert before["not_derivable"] == real_result["not_derivable"]


@real_inputs
@pytest.mark.parametrize("mutation", ["extra", "missing", "kept", "nonparent"])
def test_real_cli_refuses_perimeter(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
) -> None:
    import hashlib
    from derive_a import cli

    manifest = json.loads(
        (BASE / "inputs/acceptance-inputs-manifest.json").read_bytes()
    )
    entry = next(e for e in manifest["inputs"] if e["family"] == "declared_cutoff_date")
    declared = json.loads(Path(entry["path"]).read_bytes())
    rows = declared["rows"]
    if mutation == "extra":
        rows.append(identity("unexpected-account"))
    elif mutation == "missing":
        rows[:] = [r for r in rows if r["account_id"] != "account-1b44c987969b9e47-s2"]
    elif mutation == "kept":
        crosswalk = next(
            e
            for e in manifest["inputs"]
            if e["family"] == "account_strategy_map_and_active_universe"
        )
        rows.extend(
            r
            for r in json.loads(Path(crosswalk["path"]).read_bytes())["rows"]
            if r["account_id"] == "account-1b44c987969b9e47"
        )
    else:
        nonparent = next(
            r["account_id"]
            for r in rows
            if "-s" not in r["account_id"]
            and r["account_id"]
            not in {
                "account-58e75e8d952fa1b8",
                "account-58e75e8d952fa1b8-crisis",
                "account-58e75e8d952fa1b8-singleindex",
            }
        )
        rows[:] = [r for r in rows if r["account_id"] != nonparent]
    payload = json.dumps(declared).encode()
    selected = tmp_path / "declared.json"
    selected.write_bytes(payload)
    entry.update(
        path=str(selected),
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    selection = tmp_path / "inputs.json"
    selection.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "result.json"
    assert (
        cli.main(
            [
                "--qe-root",
                str(BASE / "tree/qe-root"),
                "--out",
                str(output),
                "--inputs",
                str(selection),
                "--sleeve-inputs",
                str(BASE / "inputs/sleeve-inputs.json"),
            ]
        )
        == 2
    )
    assert "input error:" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("with_baseline", [False, True])
def test_hand_checked_pooled_mwr(with_baseline: bool) -> None:
    from decimal import Decimal as D, localcontext
    from derive_a import derive, numerics, sleeves
    from derive_a.sources import AccountFlows

    end = dt.date(2027, 1, 1)
    epoch = sleeves.epoch(
        "derived", "product", {"2026-01-01": D(100), "2027-01-01": D(110)}, None, []
    )
    output = derive.Output()
    members: dict[str, numerics.Member] = {}
    with localcontext() as context:
        context.prec = numerics.PRECISION
        sleeves.emit_epoch(epoch, output, end, (), members)
        account_return = next(
            r["value"] for r in output.metrics if r["metric_id"] == "mwr.account"
        )
        baseline = row("live")
        sources = {
            "live": derive.AccountSource(
                "live", "TEST", {START: D(100), end: D(130)}, AccountFlows(), (), ()
            )
        }
        membership = derive.Membership(
            funded=[baseline] if with_baseline else [], assignment_only=[], unknown=[]
        )
        combined.apply(
            output,
            Crosswalk(end, end, (row("parent"), baseline)),
            membership,
            sources,
            end,
            {"sleeve_derivation": {"epochs": [epoch], "inputs": {}}},
            members,
            {"parent"},
        )
    result = next(
        r["value"] for r in output.metrics if r["metric_id"] == "mwr.strategy"
    )
    assert D(result) == D("0.2" if with_baseline else "0.1")
    if not with_baseline:
        assert result == account_return
