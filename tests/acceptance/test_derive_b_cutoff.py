"""The declared cutoff and the ledger generation come from the caller, never a constant."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from decimal import getcontext, setcontext
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[2] / "acceptance" / "derive_b" / "derive.py"
R3_CUTOFF = "2026-11-06"
LEGACY_CUTOFF = "2026-09-17"


def load_derivation_module(name: str):
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    ambient = getcontext().copy()
    try:
        spec.loader.exec_module(module)
    finally:
        setcontext(ambient)
    return module


def _ledger(as_of: str, value: str) -> dict[str, object]:
    return {
        "as_of_date": as_of,
        "nav_observations": [
            {
                "account_id": "account-a",
                "as_of_date": as_of,
                "nav": {"status": "AVAILABLE", "currency": "USD", "value": value},
                "mapping_status": "MAPPED",
            }
        ],
        "flows": [],
        "capital_anchors": [],
    }


def _crosswalk(as_of: str) -> dict[str, object]:
    return {
        "as_of_date": as_of,
        "calculation_cutoff_date": as_of,
        "rows": [
            {
                "account_id": "account-a",
                "strategy_id": "strategy-a",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2026-01-01",
                "effective_end": None,
            }
        ],
    }


def write_inputs(
    tmp_path: Path,
    *,
    cutoff: str,
    ledger_as_of: str | None = None,
    crosswalk_as_of: str | None = None,
    manifest_cutoff: str | None = None,
    ledger_dir: str = "dataPortfolio/provider-ledger/generations/" + "ab" * 32,
    nav_value: str = "123",
    declared_cutoff: str | None = "same",
) -> Path:
    """declared_cutoff: "same" = the owner artifact names `cutoff`; None = absent."""
    # A schema-2 manifest whose ledger sits at a path no constant names.
    files = {
        f"{ledger_dir}/provider-ledger.json": json.dumps(
            _ledger(ledger_as_of or cutoff, nav_value)
        ),
        "x/account-strategy-current-epoch-crosswalk.json": json.dumps(
            _crosswalk(crosswalk_as_of or cutoff)
        ),
        "x/dashboard_Volatis_Strategy_pricehistory.csv": "time,value\n",
        "x/account_activity.csv": "activity_type,id,fund,type,amount,time\n",
        "x/nav_daily.csv": "day_timestamp_utc,nav\n",
        "x/wallet_deposits.csv": "state,currency,amount,created_at_utc\n",
        "x/wallet_bills.csv": "currency,direction,amount,timestamp_utc\n",
        "x/yfinance-history.json": json.dumps({"rows": []}),
    }
    if declared_cutoff is not None:
        files["x/declared-cutoff-manifest.json"] = json.dumps(
            {
                "artifact": "account-strategy-manifest",
                "calculation_cutoff_date": (
                    cutoff if declared_cutoff == "same" else declared_cutoff
                ),
                "rows": [
                    {
                        "account_id": "account-a",
                        "strategy_id": "strategy-a",
                        "effective_start": "2026-01-01",
                        "effective_end": None,
                    }
                ],
            }
        )
    entries = []
    for relative, contents in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        entries.append(
            {
                "family": (
                    "declared_cutoff_date"
                    if relative.endswith("declared-cutoff-manifest.json")
                    else "test"
                ),
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
        )
    manifest_path = tmp_path / "acceptance-inputs.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "qe-cp-acceptance-inputs/2",
                "cutoff": manifest_cutoff or cutoff,
                "inputs": entries,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def run_cli(
    derivation, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *extra: str
) -> int | None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "derive.py",
            "--qe-root",
            str(tmp_path),
            "--out",
            str(tmp_path / "out.json"),
            *extra,
        ],
    )
    try:
        derivation.main()
    except SystemExit as exit_info:
        code = exit_info.code
        assert code is None or isinstance(code, int)
        return code
    return None


def capital_row(output: dict[str, object]) -> dict[str, object]:
    return next(
        row
        for row in output["metrics"]  # type: ignore[attr-defined]
        if row["metric_id"] == "capital.account.cutoff" and row["entity"] == "account-a"
    )


def test_cli_without_cutoff_is_refused_not_defaulted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    derivation = load_derivation_module("derive_b_cutoff_1")
    manifest = write_inputs(tmp_path, cutoff=LEGACY_CUTOFF)

    code = run_cli(derivation, monkeypatch, tmp_path, "--inputs", str(manifest))

    assert code == 2
    assert "--cutoff" in capsys.readouterr().err
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize("bad", ["2026-9-17", "20260917", "not-a-date", ""])
def test_cli_malformed_cutoff_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, bad: str
):
    derivation = load_derivation_module("derive_b_cutoff_2")
    manifest = write_inputs(tmp_path, cutoff=LEGACY_CUTOFF)

    code = run_cli(
        derivation, monkeypatch, tmp_path, "--cutoff", bad, "--inputs", str(manifest)
    )

    assert code == 2
    assert "input error" in capsys.readouterr().err
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize(
    ("declared", "manifest_cutoff"),
    [(R3_CUTOFF, LEGACY_CUTOFF), (LEGACY_CUTOFF, R3_CUTOFF)],
)
def test_cli_cutoff_must_equal_the_manifest_cutoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    declared: str,
    manifest_cutoff: str,
):
    derivation = load_derivation_module("derive_b_cutoff_3")
    manifest = write_inputs(
        tmp_path, cutoff=manifest_cutoff, manifest_cutoff=manifest_cutoff
    )

    code = run_cli(
        derivation,
        monkeypatch,
        tmp_path,
        "--cutoff",
        declared,
        "--inputs",
        str(manifest),
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "differs from the declared" in err
    assert declared in err
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize("artifact", [None, "2026-09-18", "2026-11-05"])
def test_cli_owner_declared_cutoff_artifact_gates_the_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, artifact: str | None
):
    """Absent artifact, or one naming another date, refuses even if flag == manifest."""
    derivation = load_derivation_module("derive_b_cutoff_3b")
    manifest = write_inputs(tmp_path, cutoff=R3_CUTOFF, declared_cutoff=artifact)

    code = run_cli(
        derivation,
        monkeypatch,
        tmp_path,
        "--cutoff",
        R3_CUTOFF,
        "--inputs",
        str(manifest),
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "owner-declared cutoff artifact" in err
    assert not (tmp_path / "out.json").exists()


def test_cli_unreadable_owner_declared_cutoff_artifact_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    derivation = load_derivation_module("derive_b_cutoff_3c")
    manifest = write_inputs(tmp_path, cutoff=R3_CUTOFF)
    (tmp_path / "x" / "declared-cutoff-manifest.json").write_text("{", encoding="utf-8")
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    for entry in entries["inputs"]:
        entry.pop("sha256", None)
        entry.pop("bytes", None)
    manifest.write_text(json.dumps(entries), encoding="utf-8")

    code = run_cli(
        derivation,
        monkeypatch,
        tmp_path,
        "--cutoff",
        R3_CUTOFF,
        "--inputs",
        str(manifest),
    )

    assert code == 1
    assert "is unreadable" in capsys.readouterr().err


def test_cli_without_a_manifest_refuses_a_non_legacy_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """The built-in v1 selection names only the 2026-09-17 generations."""
    derivation = load_derivation_module("derive_b_cutoff_4")

    code = run_cli(derivation, monkeypatch, tmp_path, "--cutoff", R3_CUTOFF)

    assert code == 1
    assert "requires an inputs manifest" in capsys.readouterr().err


def test_non_legacy_cutoff_end_to_end_is_not_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A manifest, ledger and crosswalk all at 2026-11-06 derive normally."""
    derivation = load_derivation_module("derive_b_cutoff_5")
    manifest = write_inputs(tmp_path, cutoff=R3_CUTOFF)

    code = run_cli(
        derivation,
        monkeypatch,
        tmp_path,
        "--cutoff",
        R3_CUTOFF,
        "--inputs",
        str(manifest),
        "--code-commit",
        "control-constant",
    )

    assert code is None
    output = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert not any(
        row["metric_id"] == "acceptance.input.cutoff" for row in output["not_derivable"]
    )
    assert output["metrics"]
    capital = capital_row(output)
    assert capital["value"] == "123.00"
    assert capital["as_of"] == R3_CUTOFF
    assert output["code_commit"] == "control-constant"
    assert all(
        row["as_of"] == R3_CUTOFF
        for row in output["metrics"]
        if row["metric_id"] == "capital.account.cutoff"
    )


def test_ledger_path_is_the_one_the_manifest_names(tmp_path: Path):
    """Two different ledger files: the manifest's path is the one read."""
    derivation = load_derivation_module("derive_b_cutoff_6")
    other_dir = "elsewhere/generations/" + "cd" * 32
    manifest_path = write_inputs(
        tmp_path, cutoff=R3_CUTOFF, ledger_dir=other_dir, nav_value="777"
    )
    derivation.set_cutoff(derivation.date.fromisoformat(R3_CUTOFF))

    manifest = derivation.load_inputs_manifest(manifest_path)
    resolved = derivation.manifest_input_paths(manifest, tmp_path)
    output = derivation.derive(tmp_path, manifest)

    assert resolved["ledger"] == Path(other_dir) / "provider-ledger.json"
    assert resolved["ledger"] != derivation.LEDGER_PATH
    assert capital_row(output)["value"] == "777.00"
    assert {item["path"] for item in output["inputs"]} >= {
        f"{other_dir}/provider-ledger.json"
    }


@pytest.mark.parametrize(
    ("ledger_as_of", "crosswalk_as_of", "label"),
    [
        (LEGACY_CUTOFF, None, "ledger"),
        (None, LEGACY_CUTOFF, "crosswalk"),
    ],
)
def test_ledger_or_crosswalk_dated_differently_is_not_derivable(
    tmp_path: Path,
    ledger_as_of: str | None,
    crosswalk_as_of: str | None,
    label: str,
):
    derivation = load_derivation_module("derive_b_cutoff_7")
    manifest_path = write_inputs(
        tmp_path,
        cutoff=R3_CUTOFF,
        ledger_as_of=ledger_as_of,
        crosswalk_as_of=crosswalk_as_of,
    )
    derivation.set_cutoff(derivation.date.fromisoformat(R3_CUTOFF))

    manifest = derivation.load_inputs_manifest(manifest_path)
    output = derivation.derive(tmp_path, manifest)

    assert output["metrics"] == []
    [row] = output["not_derivable"]
    assert row["metric_id"] == "acceptance.input.cutoff"
    assert label in row["reason"]
    assert f"fixed {R3_CUTOFF} cutoff" in output["convention_notes"][0]


def test_legacy_cutoff_behaviour_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    derivation = load_derivation_module("derive_b_cutoff_8")
    manifest = write_inputs(tmp_path, cutoff=LEGACY_CUTOFF)

    code = run_cli(
        derivation,
        monkeypatch,
        tmp_path,
        "--cutoff",
        LEGACY_CUTOFF,
        "--inputs",
        str(manifest),
    )

    assert code is None
    output = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert capital_row(output)["as_of"] == LEGACY_CUTOFF
    # Default code_commit: the checkout HEAD (or the honest no-git sentinel).
    assert re.fullmatch(
        r"[0-9a-f]{40}|unavailable:no-git-metadata", output["code_commit"]
    )


def test_metric_as_of_default_follows_the_declared_cutoff():
    derivation = load_derivation_module("derive_b_cutoff_9")
    derivation.set_cutoff(derivation.date.fromisoformat(R3_CUTOFF))

    row = derivation.metric("m", "e", "1", "USD", [])

    assert row["as_of"] == R3_CUTOFF


def test_no_cutoff_or_generation_literals_remain_outside_the_v1_selection():
    """Guard: the only 2026-09-17 literal in code is the named legacy constant."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    code_literals = [
        line.strip()
        for line in source.splitlines()
        if "2026, 9, 17" in line or "fd6096d8" in line
    ]
    assert code_literals == ["LEGACY_V1_CUTOFF = date(2026, 9, 17)"]
