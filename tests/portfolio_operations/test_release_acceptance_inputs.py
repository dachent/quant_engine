"""RELEASE-DRIVER-001 block B9: acceptance-inputs and sleeve-inputs are rebuilt from the cycle's bytes.

Every fixture is built here from synthetic bytes under ``tmp_path``. The outputs are read back
through the committed derivation loaders, so a shape the derivations would refuse fails here.
"""

from __future__ import annotations

import copy
import datetime as dt
import decimal
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "acceptance"))

import release_acceptance_inputs as tool  # noqa: E402
import release_cycle_manifest as cycle_tool  # noqa: E402
from derive_a import derive as derive_a  # noqa: E402
from derive_a import sleeves as sleeves_a  # noqa: E402

CUTOFF = "2031-03-14"
PROVIDERS = (
    "alpaca",
    "fidelity",
    "ibkr",
    "matrixport",
    "portfolio123",
    "quantbase",
    "schwab",
)
NATIVE = {"matrixport", "quantbase", "schwab"}
SOURCE = {
    p: "CYCLE_MANIFEST_NATIVE"
    if p in NATIVE
    else "SELECTED_SOURCE_ARTIFACT"
    if p == "ibkr"
    else "GENERATION_MANIFEST"
    for p in PROVIDERS
}
RUN = "20310315T010203.004Z"
MP_FILES = {
    "csv/nav_daily.csv": b"date,nav\n",
    "csv/wallet_deposits.csv": b"deposits\n",
    "csv/wallet_bills.csv": b"bills\n",
    "csv/wallet_currency_dispositions.csv": b"dispositions\n",
    "csv/subscription_details.csv": b"subscriptions\n",
    "csv/coverage.csv": b"endpoint,status\n",
    "reports/request_manifest.jsonl": b'{"request":1}\n',
}
QB_FILES = {
    f"source/csv/{name}": f"quantbase {name}\n".encode()
    for name in (
        "accounts.csv",
        "account_activity.csv",
        "dashboard_Volatis_Strategy_pricehistory.csv",
        "dashboard_Crisis_Factor_Investing_pricehistory.csv",
        "dashboard_Single_Index_Leveraged_In_and_Out_pricehistory.csv",
        "dashboard_Total_pricehistory.csv",
    )
}


def _derive_b() -> Any:
    """``derive_b`` sets the ambient decimal precision when imported; keep that out of this process."""

    with decimal.localcontext():
        from derive_b import derive as module

    return module


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _put(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _sha(data)


def _json(path: Path, value: Any) -> str:
    return _put(path, json.dumps(value, sort_keys=True).encode())


def _record(artifact_id: str, data: bytes, schema: str, as_of: str) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "as_of_date": as_of,
        "schema": {"name": schema, "version": "1"},
        "sha256": _sha(data),
        "size_bytes": len(data),
    }


def make_world(
    tmp_path: Path,
    *,
    as_of: dict[str, str] | None = None,
    finalized: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A cycle at ``CUTOFF``: seven provider generations, run outputs, ledger, repo and carried files."""

    as_of = as_of or {}
    finalized = finalized or {}
    qe, scratch, repo = tmp_path / "qe", tmp_path / "scratch", tmp_path / "repo"
    for directory in (qe, scratch, repo):
        directory.mkdir()
    providers: dict[str, Any] = {}
    for provider in PROVIDERS:
        root = qe / f"data-{provider}"
        gid = _sha(f"generation {provider}".encode())
        directory = root / "generations" / gid
        day = as_of.get(provider, CUTOFF)
        artifacts: list[dict[str, Any]] = []
        if provider == "matrixport":
            run_dir = root / "output" / RUN
            for artifact_id, data in MP_FILES.items():
                _put(run_dir / artifact_id, data)
                name = (
                    "matrixport-acceptance-jsonl"
                    if artifact_id.endswith("l")
                    else "matrixport-canonical-csv"
                )
                artifacts.append(_record(artifact_id, data, name, day))
            catalog = json.dumps({"source_run_id": RUN}).encode()
            _put(directory / "catalog.json", catalog)
            artifacts.append(
                _record(
                    "normalized/generation-catalog.json",
                    catalog,
                    "matrixport-generation-catalog",
                    day,
                )
            )
        if provider == "quantbase":
            for artifact_id, data in QB_FILES.items():
                _put(
                    root
                    / "source-runs"
                    / "run-one"
                    / "artifacts"
                    / "csv"
                    / artifact_id.rsplit("/", 1)[1],
                    data,
                )
                artifacts.append(_record(artifact_id, data, "quantbase-capture", day))
        if provider == "ibkr":
            catalog = b'{"catalog_schema_version":7}'
            _put(directory / "ibkr-catalog.json", catalog)
            artifacts.append(
                _record("normalized/ibkr-catalog.json", catalog, "ibkr-catalog", day)
            )
        manifest = {
            "artifacts": artifacts,
            "as_of_date": day,
            "generation_id": gid,
            "manifest_schema_version": 1,
        }
        manifest_sha = _json(directory / "generation.json", manifest)
        entry: dict[str, Any] = {
            "root": {"root_key": "data", "relative": f"data-{provider}"},
            "generation": {"mode": "single"},
            "manifest_sha256": manifest_sha,
            "finality_source": SOURCE[provider],
            "qualification": "STAGED_COMPLETE",
        }
        if SOURCE[provider] != "GENERATION_MANIFEST":
            evidence = f"native {provider}".encode()
            evidence_sha = _put(qe / "native" / f"{provider}.json", evidence)
            entry["finalized_through"] = finalized.get(provider, CUTOFF)
            entry["finality_evidence"] = {
                "path": {"root_key": "data", "relative": f"native/{provider}.json"},
                "sha256": evidence_sha,
                "size_bytes": len(evidence),
            }
        else:
            entry["qualification"] = "PUBLISHED_COMPLETE"
        providers[provider] = entry
    strategy = b"{}"
    _put(qe / "strategy.json", strategy)
    manifest_doc = {
        "schema_version": "qe-release-cycle-manifest/1",
        "cycle_id": "cycle-b9",
        "cutoff": CUTOFF,
        "evaluated_on": CUTOFF,
        "captured_at_utc": "2031-03-15T09:00:00Z",
        "predecessor": {"release_id": "release-before"},
        "scratch": {"root_key": "scratch", "relative": "cycle"},
        "providers": providers,
        "strategy_evidence": {
            "path": {"root_key": "data", "relative": "strategy.json"},
            "sha256": _sha(strategy),
            "size_bytes": len(strategy),
        },
    }
    (scratch / "cycle").mkdir()
    resolved = cycle_tool.resolve(
        cycle_tool.dumps(manifest_doc), {"data": qe, "scratch": scratch}
    )
    resolved_path = tmp_path / "resolved.json"
    resolved_path.write_bytes(cycle_tool.dumps(resolved))

    ledger_dir = scratch / "ledger" / "generations" / _sha(b"ledger")
    _json(
        ledger_dir / "provider-ledger.json",
        {
            "as_of_date": CUTOFF,
            "catalog_schema_version": 2,
            "provider_generations": [
                {
                    "provider_id": pid,
                    "generation_id": row["generation_id"],
                    "manifest_sha256": row["manifest_sha256"],
                }
                for pid, row in resolved["providers"].items()
            ],
        },
    )
    _json(
        scratch / "run" / "stages" / "panel" / "account-strategy-manifest.json",
        {
            "artifact": "account-strategy-manifest",
            "calculation_cutoff_date": CUTOFF,
            "schema_version": 10,
        },
    )
    _json(
        scratch / "run" / "stages" / "report" / "current-epoch-crosswalk.json",
        {
            "artifact_id": "account-strategy-current-epoch-crosswalk",
            "as_of_date": CUTOFF,
            "calculation_cutoff_date": CUTOFF,
            "schema_version": 1,
        },
    )
    _put(
        repo / "contracts" / "portfolio-financial-mvp-source-v1.json",
        b'{"contract":true}',
    )
    sleeves = repo / "ops" / "mvp2-release" / "inputs"
    _json(
        sleeves / "sleeves" / "sleeve-declared-inputs.json",
        {"window": {"first_day": "2031-01-01", "last_day": CUTOFF}},
    )
    _json(
        sleeves / "sleeves" / "quantbase" / "declared-inputs.json",
        {"data_cutoff": CUTOFF},
    )
    _json(sleeves / "sleeves" / "nested-strategy-tag-map.json", {"tags": []})
    _json(sleeves / "current-epoch-crosswalk.json", {"calculation_cutoff_date": CUTOFF})

    carried: list[dict[str, Any]] = []
    for family in tool.CARRIED_FAMILIES:
        data = f"kept {family}".encode()
        sha = _put(qe / "carry" / f"{family}.json", data)
        item: dict[str, Any] = {
            "bytes": len(data),
            "family": family,
            "note": f"note {family}",
            "path": f"carry/{family}.json",
            "sha256": sha,
        }
        if family.startswith("fx_us"):
            item["ticker"] = "TICKER"
        if family.endswith("_response"):
            item["schema"] = "github-issue-comment/REST"
        carried.append(item)
    predecessor = {
        "cutoff": "2000-01-01",
        "inputs": carried,
        "schema": tool.ACCEPTANCE_VERSION,
    }
    predecessor_path = tmp_path / "predecessor.json"
    predecessor_path.write_bytes(tool.dumps(predecessor))
    return {
        "tmp": tmp_path,
        "qe": qe,
        "scratch": scratch,
        "repo": repo,
        "resolved": resolved,
        "resolved_path": resolved_path,
        "ledger": f"scratch:ledger/generations/{ledger_dir.name}",
        "predecessor": predecessor,
        "predecessor_path": predecessor_path,
        "out": tmp_path / "out" / "result.json",
    }


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    return make_world(tmp_path)


def acceptance_args(
    world: dict[str, Any], *extra: str, stage: bool = True
) -> list[str]:
    args = [
        "acceptance-inputs",
        "--resolved-cycle",
        str(world["resolved_path"]),
        "--qe-root",
        str(world["qe"]),
        "--repo-root",
        str(world["repo"]),
        "--root",
        f"scratch={world['scratch']}",
        "--predecessor-manifest",
        str(world["predecessor_path"]),
        "--ledger-generation",
        world["ledger"],
        "--file",
        "declared_cutoff_date=scratch:run/stages/panel/account-strategy-manifest.json",
        "--file",
        "account_strategy_map_and_active_universe=scratch:run/stages/report/current-epoch-crosswalk.json",
        "--out",
        str(world["out"]),
    ]
    if stage:
        args += ["--stage-dir", "stage"]
    return [*args, *extra]


def sleeve_args(world: dict[str, Any], *extra: str, stage: bool = True) -> list[str]:
    args = [
        "sleeve-inputs",
        "--resolved-cycle",
        str(world["resolved_path"]),
        "--qe-root",
        str(world["qe"]),
        "--repo-root",
        str(world["repo"]),
        "--root",
        f"scratch={world['scratch']}",
        "--out",
        str(world["out"]),
    ]
    if stage:
        args += ["--stage-dir", "stage"]
    return [*args, *extra]


def _dir_link(link: Path, target: Path) -> None:
    """A symlink, or on Windows a junction; skip where neither can be made."""

    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        if done.returncode == 0:
            return
    pytest.skip("directory links are not available here")


def refused(argv: list[str], code: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert tool.main(argv) == 1
    err = capsys.readouterr().err
    assert f"release-acceptance-inputs: FAIL: {code}:" in err, err


def snapshot(world: dict[str, Any]) -> set[str]:
    return {
        p.relative_to(world["tmp"]).as_posix()
        for p in world["tmp"].rglob("*")
        if p.is_file()
    }


def result(world: dict[str, Any]) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(world["out"].read_bytes())
    return loaded


def by_family(document: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for entry in document["inputs"]:
        found.setdefault(entry["family"], []).append(entry)
    return found


# ----------------------------------------------------------------------------- round trips


def test_acceptance_inputs_round_trip_through_the_committed_loader(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert tool.main(acceptance_args(world)) == 0
    summary = json.loads(capsys.readouterr().out)
    data = world["out"].read_bytes()
    assert summary["sha256"] == _sha(data)
    document = result(world)
    assert document["schema"] == "qe-cp-acceptance-inputs/3"
    assert document["cutoff"] == CUTOFF
    loaded = derive_a.load_inputs_manifest(data, "synthetic")
    assert loaded.cutoff == dt.date.fromisoformat(CUTOFF)
    assert loaded.schema == "qe-cp-acceptance-inputs/3"
    assert loaded.selection is not None  # the native source families are all present
    # every pin was recomputed from the bytes it names, at the path the loader places it
    for role, path in loaded.layout.items():
        content = (world["qe"] / path).read_bytes()
        assert loaded.pins[path] == _sha(content), role
        assert loaded.sizes[path] == len(content), role
    assert loaded.epoch_metadata is not None
    assert not Path(loaded.epoch_metadata[0]).is_absolute()


def test_every_entry_reads_its_pinned_bytes_and_the_cycle_files_are_staged(
    world: dict[str, Any],
) -> None:
    assert tool.main(acceptance_args(world)) == 0
    document = result(world)
    for entry in document["inputs"]:
        path = world["qe"] / entry["path"]
        assert path.is_file(), entry["path"]
        assert _sha(path.read_bytes()) == entry["sha256"]
        assert path.stat().st_size == entry["bytes"]
    families = by_family(document)
    # provider captures stay where they are; scratch files are staged under the stage dir
    assert families["portfolio_and_strategy_capital_at_cutoff"][0]["path"].startswith(
        "stage/"
    )
    assert families["declared_cutoff_date"][0]["path"].startswith("stage/")
    assert families["financial_source_contract"][0]["path"].startswith("stage/")
    nav = [
        e
        for e in families["portfolio_and_strategy_capital_at_cutoff"]
        if e["path"].endswith("nav_daily.csv")
    ]
    assert nav[0]["path"] == f"data-matrixport/output/{RUN}/csv/nav_daily.csv"
    assert families["matrixport_generation_catalog"][0]["path"].endswith(
        "/catalog.json"
    )
    # carried families keep the predecessor's non-location fields
    assert families["fx_usdt_usd_daily"][0]["ticker"] == "TICKER"
    assert (
        families["provider_capital_owner_receipt_b_response"][0]["schema"]
        == "github-issue-comment/REST"
    )
    evidence = document["generation_selection_evidence"]
    assert evidence["financial_cutoff"] == CUTOFF
    assert evidence["captured_at_utc"] == world["resolved"]["captured_at_utc"]
    block = evidence["matrixport"]
    assert block["source_run_id"] == RUN
    assert (
        block["generation_manifest_sha256"]
        == world["resolved"]["providers"]["matrixport"]["manifest_sha256"]
    )
    assert (
        block["catalog_sha256"]
        == families["matrixport_generation_catalog"][0]["sha256"]
    )
    assert block["finality_evidence_sha256_verified_V"] is True
    assert block["finality_evidence_path"] == "native/matrixport.json"
    assert document["provenance"]["resolved_cycle_sha256"] == _sha(
        world["resolved_path"].read_bytes()
    )


def test_sleeve_inputs_round_trip_through_both_committed_loaders(
    world: dict[str, Any],
) -> None:
    assert tool.main(sleeve_args(world)) == 0
    document = result(world)
    assert document["schema"] == "qe-cp-sleeve-inputs/1"
    assert document["cutoff"] == CUTOFF
    assert set(document["inputs"]) == set(sleeves_a.ROLES)
    cutoff = dt.date.fromisoformat(CUTOFF)
    records_a, data_a = sleeves_a.load_sleeve_inputs(
        world["out"], world["qe"], world["repo"], cutoff
    )
    loaded_b = _derive_b().load_sleeve_inputs(
        world["out"], world["qe"], world["repo"], cutoff
    )
    assert loaded_b is not None
    for role, entry in document["inputs"].items():
        assert records_a[role]["sha256"] == entry["sha256"]
        assert _sha(data_a[role]) == entry["sha256"]
    # governed repo files are pinned in place; provider captures at their data location
    assert document["inputs"]["tag_map"]["root"] == "repo"
    assert document["inputs"]["ibkr_catalog"]["root"] == "qe"
    assert document["inputs"]["ibkr_catalog"]["path"].endswith("/ibkr-catalog.json")
    assert not (world["qe"] / "stage").exists()  # nothing needed staging


def test_a_file_outside_both_roots_is_staged_and_pinned_at_the_copy(
    world: dict[str, Any],
) -> None:
    loose = world["scratch"] / "loose-tag-map.json"
    _put(loose, b'{"tags":["x"]}')
    assert (
        tool.main(sleeve_args(world, "--file", "tag_map=scratch:loose-tag-map.json"))
        == 0
    )
    entry = result(world)["inputs"]["tag_map"]
    assert entry == {
        "path": "stage/sleeves/loose-tag-map.json",
        "root": "qe",
        "sha256": _sha(b'{"tags":["x"]}'),
    }
    assert (world["qe"] / entry["path"]).read_bytes() == b'{"tags":["x"]}'


def test_a_second_run_reuses_identical_staged_copies_and_refuses_a_changed_one(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert tool.main(acceptance_args(world)) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["staged"] and not first["reused"]
    world["out"].unlink()
    assert tool.main(acceptance_args(world)) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["reused"] == first["staged"] and not second["staged"]
    world["out"].unlink()
    (world["qe"] / first["staged"][0]).write_bytes(b"changed")
    refused(acceptance_args(world), "HASH_MISMATCH", capsys)
    assert not world["out"].exists()


def test_the_pins_follow_the_bytes_not_a_remembered_hash(world: dict[str, Any]) -> None:
    # a kept file replaced by the caller is hashed again; the predecessor's pin plays no part
    _put(world["qe"] / "carry" / "new-rates.json", b"new rates")
    argv = acceptance_args(world, "--file", "fx_daily_rates=qe:carry/new-rates.json")
    assert tool.main(argv) == 0
    rates = by_family(result(world))["fx_daily_rates"][0]
    assert rates["sha256"] == _sha(b"new rates")
    assert rates["path"] == "carry/new-rates.json"
    assert rates["note"] == "note fx_daily_rates"


def test_the_module_holds_no_date_hash_or_workstation_path() -> None:
    source = (REPO_ROOT / "tools" / "release_acceptance_inputs.py").read_text(
        encoding="utf-8"
    )
    assert re.search(r"\b(19|20)\d\d-\d\d-\d\d\b", source) is None
    assert re.search(r"\b[0-9a-f]{40,64}\b", source) is None
    assert re.search(r"[A-Za-z]:\\\\|/Users/|\\\\Users", source) is None


# ----------------------------------------------------------------------------- refusals


@pytest.mark.parametrize("command", ["acceptance", "sleeve"])
def test_a_missing_capture_is_a_refusal_and_nothing_is_written(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str], command: str
) -> None:
    (
        world["qe"]
        / "data-quantbase"
        / "source-runs"
        / "run-one"
        / "artifacts"
        / "csv"
        / "accounts.csv"
    ).unlink()
    before = snapshot(world)
    argv = acceptance_args(world) if command == "acceptance" else sleeve_args(world)
    refused(argv, "MISSING", capsys)
    assert snapshot(world) == before


@pytest.mark.parametrize("command", ["acceptance", "sleeve"])
def test_a_changed_capture_does_not_match_its_manifest_record(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str], command: str
) -> None:
    csv = (
        world["qe"]
        / "data-quantbase"
        / "source-runs"
        / "run-one"
        / "artifacts"
        / "csv"
        / "accounts.csv"
    )
    csv.write_bytes(b"x" * len(csv.read_bytes()))  # same size, other bytes
    argv = acceptance_args(world) if command == "acceptance" else sleeve_args(world)
    refused(argv, "MISSING", capsys)
    assert not world["out"].exists()


@pytest.mark.parametrize("command", ["acceptance", "sleeve"])
def test_a_generation_manifest_that_no_longer_matches_the_cycle_is_refused(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str], command: str
) -> None:
    row = world["resolved"]["providers"]["quantbase"]
    manifest = Path(row["manifest_path"])
    manifest.write_bytes(manifest.read_bytes() + b" ")
    argv = acceptance_args(world) if command == "acceptance" else sleeve_args(world)
    refused(argv, "HASH_MISMATCH", capsys)


@pytest.mark.parametrize("command", ["acceptance", "sleeve"])
def test_two_candidate_files_are_a_refusal_even_with_equal_bytes(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str], command: str
) -> None:
    root = world["qe"] / "data-quantbase" / "source-runs"
    source = root / "run-one" / "artifacts" / "csv" / "accounts.csv"
    _put(root / "run-two" / "artifacts" / "csv" / "accounts.csv", source.read_bytes())
    argv = acceptance_args(world) if command == "acceptance" else sleeve_args(world)
    refused(argv, "AMBIGUOUS_FILE", capsys)
    # naming the directory narrows the search
    narrowed = (
        acceptance_args(
            world, "--artifact-dir", "quantbase=qe:data-quantbase/source-runs/run-one"
        )
        if command == "acceptance"
        else sleeve_args(
            world, "--artifact-dir", "quantbase=qe:data-quantbase/source-runs/run-one"
        )
    )
    assert tool.main(narrowed) == 0


@pytest.mark.parametrize(
    "as_of,finalized",
    [
        ({"matrixport": "2031-03-13"}, None),
        (None, {"quantbase": "2031-03-13"}),
        ({"ibkr": "2031-03-01"}, None),
    ],
)
def test_a_generation_that_is_stale_for_the_cutoff_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    as_of: dict[str, str] | None,
    finalized: dict[str, str] | None,
) -> None:
    world = make_world(tmp_path, as_of=as_of, finalized=finalized)
    command = "sleeve" if as_of == {"ibkr": "2031-03-01"} else "acceptance"
    argv = acceptance_args(world) if command == "acceptance" else sleeve_args(world)
    refused(argv, "STALE_GENERATION", capsys)


def test_a_ledger_that_admitted_another_generation_is_refused(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    path = world["scratch"] / world["ledger"].partition(":")[2] / "provider-ledger.json"
    ledger = json.loads(path.read_bytes())
    ledger["provider_generations"][0]["manifest_sha256"] = "0" * 64
    path.write_bytes(json.dumps(ledger).encode())
    before = snapshot(world)
    refused(acceptance_args(world), "LEDGER_PROVIDERS", capsys)
    assert snapshot(world) == before  # no staged copy, no output
    ledger = json.loads(path.read_bytes())
    ledger["provider_generations"][0]["manifest_sha256"] = world["resolved"][
        "providers"
    ][ledger["provider_generations"][0]["provider_id"]]["manifest_sha256"]
    ledger["as_of_date"] = "2031-03-13"
    path.write_bytes(json.dumps(ledger).encode())
    refused(acceptance_args(world), "STALE_GENERATION", capsys)


@pytest.mark.parametrize(
    "target,key,expected",
    [
        (
            "scratch/run/stages/panel/account-strategy-manifest.json",
            "calculation_cutoff_date",
            "CUTOFF_MISMATCH",
        ),
        (
            "scratch/run/stages/report/current-epoch-crosswalk.json",
            "as_of_date",
            "CUTOFF_MISMATCH",
        ),
    ],
)
def test_a_run_output_for_another_cutoff_is_refused(
    world: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    target: str,
    key: str,
    expected: str,
) -> None:
    path = world["tmp"] / target
    document = json.loads(path.read_bytes())
    document[key] = "2031-03-13"
    path.write_bytes(json.dumps(document).encode())
    refused(acceptance_args(world), expected, capsys)


@pytest.mark.parametrize(
    "role,relative,key",
    [
        (
            "quantbase_declared_inputs",
            "ops/mvp2-release/inputs/sleeves/quantbase/declared-inputs.json",
            "data_cutoff",
        ),
        (
            "crosswalk",
            "ops/mvp2-release/inputs/current-epoch-crosswalk.json",
            "calculation_cutoff_date",
        ),
    ],
)
def test_governed_sleeve_inputs_for_another_cutoff_are_refused(
    world: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    role: str,
    relative: str,
    key: str,
) -> None:
    path = world["repo"] / relative
    document = json.loads(path.read_bytes())
    document[key] = "2031-03-13"
    path.write_bytes(json.dumps(document).encode())
    refused(sleeve_args(world), "CUTOFF_MISMATCH", capsys)


def test_the_ibkr_window_must_end_at_the_cutoff(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    path = world["repo"] / "ops/mvp2-release/inputs/sleeves/sleeve-declared-inputs.json"
    path.write_bytes(json.dumps({"window": {"last_day": "2031-03-13"}}).encode())
    refused(sleeve_args(world), "CUTOFF_MISMATCH", capsys)


@pytest.mark.parametrize(
    "ref",
    [
        "scratch:../outside.json",
        "scratch:/abs.json",
        "scratch:a/../b.json",
        "scratch:a//b.json",
        "scratch:a\\b.json",
    ],
)
def test_a_path_that_leaves_its_root_is_refused(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str], ref: str
) -> None:
    argv = acceptance_args(world, "--file", f"fx_daily_rates={ref}")
    refused(argv, "PATH_ESCAPES_ROOT", capsys)


def test_a_symlinked_path_is_refused_and_a_symlinked_capture_is_not_a_candidate(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    outside = world["tmp"] / "outside"
    _put(outside / "rates.json", b"outside")
    link = world["scratch"] / "linked"
    _dir_link(link, outside)
    refused(
        acceptance_args(world, "--file", "fx_daily_rates=scratch:linked/rates.json"),
        "PATH_ESCAPES_ROOT",
        capsys,
    )


def test_a_symlinked_capture_is_not_a_candidate(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    csv = world["qe"] / "data-matrixport" / "output" / RUN / "csv" / "nav_daily.csv"
    kept = csv.read_bytes()
    csv.unlink()
    elsewhere = world["tmp"] / "elsewhere.csv"
    elsewhere.write_bytes(kept)
    try:
        csv.symlink_to(elsewhere)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are not available here")
    refused(acceptance_args(world), "MISSING", capsys)


def test_a_linked_directory_in_the_search_tree_is_not_followed(
    world: dict[str, Any],
) -> None:
    root = world["qe"] / "data-quantbase"
    copy_dir = world["tmp"] / "second-copy" / "csv"
    source = root / "source-runs" / "run-one" / "artifacts" / "csv" / "accounts.csv"
    _put(copy_dir / "accounts.csv", source.read_bytes())
    _dir_link(root / "source-runs" / "linked-run", world["tmp"] / "second-copy")
    # the link would make a second candidate if it were followed
    assert tool.main(sleeve_args(world)) == 0


def test_a_symlinked_stage_directory_is_refused(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    elsewhere = world["tmp"] / "elsewhere"
    elsewhere.mkdir()
    _dir_link(world["qe"] / "stage", elsewhere)
    refused(acceptance_args(world), "PATH_ESCAPES_ROOT", capsys)


def test_a_file_under_no_root_needs_a_stage_directory(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    refused(acceptance_args(world, stage=False), "NOT_UNDER_ROOT", capsys)
    assert not world["out"].exists()


def test_an_existing_output_is_never_overwritten(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    world["out"].parent.mkdir()
    world["out"].write_bytes(b"mine")
    before = snapshot(world)
    refused(acceptance_args(world), "OUTPUT_EXISTS", capsys)
    assert world["out"].read_bytes() == b"mine"
    assert snapshot(world) == before


def test_a_native_file_outside_its_source_run_is_refused(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    root = world["qe"] / "data-matrixport"
    (root / "output" / RUN / "csv" / "coverage.csv").rename(root / "coverage.csv")
    refused(acceptance_args(world), "RUN_PATH", capsys)


def test_a_kept_file_that_changed_is_a_drift_not_a_silent_carry(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    (world["qe"] / "carry" / "fx_daily_rates.json").write_bytes(b"drifted")
    refused(acceptance_args(world), "CARRY_DRIFT", capsys)


def test_a_predecessor_path_off_the_qe_root_needs_a_pointer(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    predecessor = copy.deepcopy(world["predecessor"])
    old = "Z" + ":" + chr(92) + "retired-root" + chr(92) + "fx.json"
    pointers = []
    for entry in predecessor["inputs"]:
        if entry["family"] == "fx_daily_rates":
            pointers.append({"path": entry["path"], "sha256": entry["sha256"]})
            entry["path"] = old
    world["predecessor_path"].write_bytes(tool.dumps(predecessor))
    refused(acceptance_args(world), "CARRY_UNRESOLVED", capsys)
    baseline = world["tmp"] / "baseline.json"
    baseline.write_bytes(
        json.dumps({"metrics": [{"authority_pointer": pointers}]}).encode()
    )
    assert tool.main(acceptance_args(world, "--baseline-pointers", str(baseline))) == 0
    rates = by_family(result(world))["fx_daily_rates"][0]
    assert rates["path"] == "carry/fx_daily_rates.json"
    assert old not in world["out"].read_text(encoding="utf-8")


def test_two_locations_for_one_digest_are_never_guessed(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    predecessor = copy.deepcopy(world["predecessor"])
    sha = ""
    for entry in predecessor["inputs"]:
        if entry["family"] == "fx_daily_rates":
            sha = entry["sha256"]
            entry["path"] = "Z" + ":" + chr(92) + "gone.json"
    world["predecessor_path"].write_bytes(tool.dumps(predecessor))
    baseline = world["tmp"] / "baseline.json"
    pointers = [
        {"path": "carry/a.json", "sha256": sha},
        {"path": "carry/b.json", "sha256": sha},
    ]
    baseline.write_bytes(
        json.dumps({"metrics": [{"authority_pointer": pointers}]}).encode()
    )
    refused(
        acceptance_args(world, "--baseline-pointers", str(baseline)),
        "CARRY_UNRESOLVED",
        capsys,
    )


def test_the_predecessor_must_be_the_pinned_v3_manifest_with_every_kept_family(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    refused(
        acceptance_args(world, "--predecessor-manifest-sha256", "0" * 64),
        "HASH_MISMATCH",
        capsys,
    )
    predecessor = copy.deepcopy(world["predecessor"])
    predecessor["inputs"] = [
        e for e in predecessor["inputs"] if e["family"] != "fx_daily_rates"
    ]
    world["predecessor_path"].write_bytes(tool.dumps(predecessor))
    refused(acceptance_args(world), "PREDECESSOR", capsys)
    predecessor["schema"] = "qe-cp-acceptance-inputs/2"
    world["predecessor_path"].write_bytes(tool.dumps(predecessor))
    refused(acceptance_args(world), "PREDECESSOR", capsys)


def test_the_resolved_cycle_pin_and_shape_are_checked(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    refused(
        acceptance_args(world, "--resolved-cycle-sha256", "0" * 64),
        "HASH_MISMATCH",
        capsys,
    )
    document = json.loads(world["resolved_path"].read_bytes())
    document["schema_version"] = "qe-release-resolved-cycle/9"
    world["resolved_path"].write_bytes(json.dumps(document).encode())
    refused(acceptance_args(world), "RESOLVED_CYCLE", capsys)


def test_roots_must_agree_and_roles_must_be_known(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    other = world["tmp"] / "other"
    other.mkdir()
    refused(acceptance_args(world, "--root", f"qe={other}"), "ROOTS", capsys)
    refused(
        acceptance_args(world, "--file", "nothing=qe:x.json"), "UNKNOWN_ROLE", capsys
    )
    refused(
        acceptance_args(world, "--file", "fx_daily_rates=nowhere:x.json"),
        "UNKNOWN_ROOT",
        capsys,
    )
    refused(
        acceptance_args(
            world,
            "--file",
            "fx_daily_rates=qe:carry/a",
            "--file",
            "fx_daily_rates=qe:carry/b",
        ),
        "USAGE",
        capsys,
    )


def test_missing_required_run_files_and_bad_usage_exit_nonzero(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [a for a in acceptance_args(world)]
    index = argv.index("--file")
    del argv[index : index + 2]  # no declared cutoff manifest named
    refused(argv, "USAGE", capsys)
    with pytest.raises(SystemExit) as info:
        tool.main(["acceptance-inputs"])
    assert info.value.code == 2


def test_the_resolved_cycle_cutoff_is_the_only_cutoff(world: dict[str, Any]) -> None:
    assert tool.main(sleeve_args(world)) == 0
    assert result(world)["cutoff"] == world["resolved"]["cutoff"] == CUTOFF


def test_a_native_generation_finalized_after_the_cutoff_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # derive_a takes a later finality date and derive_b requires the cutoff, so no manifest can bind one
    world = make_world(tmp_path, finalized={"matrixport": "2031-03-15"})
    refused(acceptance_args(world), "FINALITY_NOT_AT_CUTOFF", capsys)
    assert not world["out"].exists()


def test_a_missing_predecessor_path_falls_back_to_the_baseline_pointer(
    world: dict[str, Any],
) -> None:
    predecessor = copy.deepcopy(world["predecessor"])
    pointers = []
    for entry in predecessor["inputs"]:
        if entry["family"] == "fx_daily_rates":
            pointers.append({"path": entry["path"], "sha256": entry["sha256"]})
            entry["path"] = (
                "carry/moved-away.json"  # a plain relative path that is gone
            )
    world["predecessor_path"].write_bytes(tool.dumps(predecessor))
    baseline = world["tmp"] / "baseline.json"
    baseline.write_bytes(
        json.dumps({"metrics": [{"authority_pointer": pointers}]}).encode()
    )
    argv = acceptance_args(world, "--baseline-pointers", str(baseline))
    assert tool.main(argv) == 0
    assert by_family(result(world))["fx_daily_rates"][0]["path"] == (
        "carry/fx_daily_rates.json"
    )


def test_an_interrupted_write_leaves_nothing_and_the_run_can_be_repeated(
    world: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_fd: int) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(tool.os, "fsync", fail)
        refused(acceptance_args(world), "WRITE_FAILED", capsys)
    assert not world["out"].exists()
    assert not (world["qe"] / "stage").exists() or not any(
        p.is_file() for p in (world["qe"] / "stage").rglob("*")
    )
    assert not list(world["tmp"].rglob(".*.tmp"))
    assert (
        tool.main(acceptance_args(world)) == 0
    )  # no OUTPUT_EXISTS from a partial file


def test_a_failure_while_writing_the_output_leaves_no_partial_file(
    world: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # stage nothing: every file is under a root, so only the output itself is written
    argv = sleeve_args(world, stage=False)
    real = tool.os.fsync

    def fail(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(tool.os, "fsync", fail)
    refused(argv, "WRITE_FAILED", capsys)
    monkeypatch.setattr(tool.os, "fsync", real)
    assert not world["out"].exists()
    assert not list(world["tmp"].rglob(".*.tmp"))
    assert tool.main(argv) == 0


# ----------------------------------------------------------------------------- cutoff-aware defaults

OTHER_DAY = "2031-02-01"
DEFAULT_RELATIVE = {
    "crosswalk": "ops/mvp2-release/inputs/current-epoch-crosswalk.json",
    "quantbase_declared_inputs": "ops/mvp2-release/inputs/sleeves/quantbase/declared-inputs.json",
    "ibkr_declared_inputs": "ops/mvp2-release/inputs/sleeves/sleeve-declared-inputs.json",
}


def _stale_default(world: dict[str, Any], role: str) -> None:
    """Date one governed default for another cutoff, as the previous cycle's root would be."""

    path = world["repo"] / DEFAULT_RELATIVE[role]
    if role == "ibkr_declared_inputs":
        document: dict[str, Any] = {"window": {"last_day": OTHER_DAY}}
    elif role == "quantbase_declared_inputs":
        document = {"data_cutoff": OTHER_DAY}
    else:
        document = {"calculation_cutoff_date": OTHER_DAY}
    path.write_bytes(json.dumps(document).encode())


def _stderr(world: dict[str, Any], argv: list[str], capsys: Any) -> str:
    before = snapshot(world)
    assert tool.main(argv) == 1
    err: str = capsys.readouterr().err
    assert snapshot(world) == before, "a refusal writes nothing"
    return err


def test_a_stale_crosswalk_default_names_the_exact_file_flag(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    _stale_default(world, "crosswalk")
    err = _stderr(world, sleeve_args(world), capsys)
    assert "FAIL: CUTOFF_MISMATCH:" in err
    assert f"crosswalk: calculation_cutoff_date is '{OTHER_DAY}'" in err
    assert f"the cycle cutoff is {CUTOFF}" in err
    assert f"(default {DEFAULT_RELATIVE['crosswalk']})" in err
    assert "--file crosswalk=KEY:REL" in err
    assert "inputs are refused" not in err  # one problem is reported as itself
    # the flag it names is accepted and clears the refusal
    _put(
        world["scratch"] / "fresh" / "current-epoch-crosswalk.json",
        json.dumps({"calculation_cutoff_date": CUTOFF}).encode(),
    )
    argv = sleeve_args(
        world, "--file", "crosswalk=scratch:fresh/current-epoch-crosswalk.json"
    )
    assert tool.main(argv) == 0


@pytest.mark.parametrize("role", ["ibkr_declared_inputs", "quantbase_declared_inputs"])
def test_a_stale_declared_inputs_default_names_the_exact_file_flag(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str], role: str
) -> None:
    _stale_default(world, role)
    err = _stderr(world, sleeve_args(world), capsys)
    assert "FAIL: CUTOFF_MISMATCH:" in err
    assert f"{role}:" in err and OTHER_DAY in err
    assert f"(default {DEFAULT_RELATIVE[role]})" in err
    assert f"--file {role}=KEY:REL" in err
    assert "--file crosswalk=" not in err  # a consistent default is not listed


def test_every_stale_default_is_listed_together(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    for role in DEFAULT_RELATIVE:
        _stale_default(world, role)
    err = _stderr(world, sleeve_args(world), capsys)
    assert "FAIL: CUTOFF_MISMATCH: 3 inputs are refused" in err
    for role in DEFAULT_RELATIVE:
        assert f"--file {role}=KEY:REL" in err
    assert "tag_map" not in err  # it carries no date and is not stale
    # replacing two of the three leaves exactly the third in the refusal
    _json(
        world["scratch"] / "a" / "crosswalk.json",
        {"calculation_cutoff_date": CUTOFF},
    )
    _json(world["scratch"] / "a" / "qb.json", {"data_cutoff": CUTOFF})
    err = _stderr(
        world,
        sleeve_args(
            world,
            "--file",
            "crosswalk=scratch:a/crosswalk.json",
            "--file",
            "quantbase_declared_inputs=scratch:a/qb.json",
        ),
        capsys,
    )
    assert "ibkr_declared_inputs" in err and "inputs are refused" not in err
    assert "--file crosswalk=" not in err


def test_a_stale_file_given_with_file_is_refused_not_trusted(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    _json(world["scratch"] / "old" / "cw.json", {"calculation_cutoff_date": OTHER_DAY})
    err = _stderr(
        world, sleeve_args(world, "--file", "crosswalk=scratch:old/cw.json"), capsys
    )
    assert "(given with --file)" in err and "--file crosswalk=KEY:REL" in err


def test_a_release_inputs_root_for_another_cutoff_lists_every_default(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    from quant_engine.core import build_release_inputs_manifest

    root = world["tmp"] / "old-root"
    paths = sorted(tool.SLEEVE_FILE_DEFAULTS.values())
    for relative in paths:
        _put(root / relative, (world["repo"] / relative).read_bytes())
    _, pin = build_release_inputs_manifest(root, paths, cutoff=OTHER_DAY)
    argv = sleeve_args(
        world,
        "--release-inputs-root",
        str(root),
        "--release-inputs-manifest-sha256",
        pin,
    )
    err = _stderr(world, argv, capsys)
    assert "FAIL: RELEASE_INPUTS: 4 inputs are refused" in err
    assert f"release inputs are for {OTHER_DAY}, this run is for {CUTOFF}" in err
    for role in tool.SLEEVE_FILE_DEFAULTS:
        assert f"--file {role}=KEY:REL" in err


def test_an_ambiguous_family_prints_the_candidates_and_the_flag(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    root = world["qe"] / "data-quantbase" / "source-runs"
    for name in QB_FILES:
        leaf = name.rpartition("/")[2]
        _put(
            root / "run-two" / "artifacts" / "csv" / leaf,
            (root / "run-one" / "artifacts" / "csv" / leaf).read_bytes(),
        )
    for argv in (acceptance_args(world), sleeve_args(world)):
        err = _stderr(world, argv, capsys)
        assert "FAIL: AMBIGUOUS_FILE:" in err
        assert "accounts.csv" in err and "run-one" in err and "run-two" in err
        assert "--artifact-dir quantbase=qe:data-quantbase/source-runs/run-one" in err
        assert "--artifact-dir quantbase=qe:data-quantbase/source-runs/run-two" in err
        # the captures of one provider share one fix, which is printed once
        assert err.count("narrow the search with one of") == 1
    assert "quantbase_dashboard_total" in err
    narrowed = "quantbase=qe:data-quantbase/source-runs/run-one"
    assert tool.main(sleeve_args(world, "--artifact-dir", narrowed)) == 0


def test_an_ambiguous_family_and_a_stale_default_are_refused_in_one_error(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    root = world["qe"] / "data-quantbase" / "source-runs"
    source = root / "run-one" / "artifacts" / "csv" / "accounts.csv"
    _put(root / "run-two" / "artifacts" / "csv" / "accounts.csv", source.read_bytes())
    _stale_default(world, "crosswalk")
    err = _stderr(world, sleeve_args(world), capsys)
    assert "2 inputs are refused" in err
    assert "[CUTOFF_MISMATCH]" in err and "[AMBIGUOUS_FILE]" in err
    assert "--file crosswalk=KEY:REL" in err
    assert "--artifact-dir quantbase=qe:data-quantbase/source-runs/run-one" in err


def test_stale_run_outputs_are_listed_together_for_the_acceptance_inputs(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    for target, keys in (
        (
            "scratch/run/stages/panel/account-strategy-manifest.json",
            ("calculation_cutoff_date",),
        ),
        (
            "scratch/run/stages/report/current-epoch-crosswalk.json",
            ("calculation_cutoff_date", "as_of_date"),
        ),
    ):
        path = world["tmp"] / target
        document = json.loads(path.read_bytes())
        for key in keys:
            document[key] = OTHER_DAY
        path.write_bytes(json.dumps(document).encode())
    err = _stderr(world, acceptance_args(world), capsys)
    assert "FAIL: CUTOFF_MISMATCH: 2 inputs are refused" in err
    assert "--file declared_cutoff_date=KEY:REL" in err
    assert "--file account_strategy_map_and_active_universe=KEY:REL" in err
    assert "calculation_cutoff_date is" in err and "as_of_date is" in err


def test_consistent_defaults_pin_the_governed_bytes_unchanged(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert tool.main(sleeve_args(world)) == 0
    assert capsys.readouterr().err == ""
    inputs = result(world)["inputs"]
    for role, relative in tool.SLEEVE_FILE_DEFAULTS.items():
        assert inputs[role] == {
            "path": relative,
            "root": "repo",
            "sha256": _sha((world["repo"] / relative).read_bytes()),
        }


def test_only_directories_holding_the_whole_family_are_advertised(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    root = world["qe"] / "data-quantbase" / "source-runs"
    source = root / "run-one" / "artifacts" / "csv" / "accounts.csv"
    _put(root / "run-two" / "artifacts" / "csv" / "accounts.csv", source.read_bytes())
    for argv in (acceptance_args(world), sleeve_args(world)):
        err = _stderr(world, argv, capsys)
        assert "FAIL: AMBIGUOUS_FILE:" in err
        assert "--artifact-dir quantbase=qe:data-quantbase/source-runs/run-one" in err
        assert "run-two" not in err.partition("narrow the search")[2]
    # when no candidate holds the family, no flag is suggested
    (
        root / "run-one" / "artifacts" / "csv" / "dashboard_Total_pricehistory.csv"
    ).unlink()
    err = _stderr(world, sleeve_args(world), capsys)
    assert "no candidate directory holds the whole quantbase family" in err
    assert "narrow the search" not in err


def test_a_stale_provider_does_not_hide_a_later_ambiguous_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = make_world(tmp_path, as_of={"ibkr": "2031-03-01"})
    root = world["qe"] / "data-quantbase" / "source-runs"
    source = root / "run-one" / "artifacts" / "csv" / "accounts.csv"
    _put(root / "run-two" / "artifacts" / "csv" / "accounts.csv", source.read_bytes())
    err = _stderr(world, sleeve_args(world), capsys)
    assert "2 inputs are refused" in err
    assert "[STALE_GENERATION] ibkr" in err
    assert "[AMBIGUOUS_FILE] quantbase_accounts" in err
    assert "--artifact-dir quantbase=qe:data-quantbase/source-runs/run-one" in err
