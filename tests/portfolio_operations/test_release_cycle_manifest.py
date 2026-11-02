"""RELEASE-DRIVER-001 block B0: the cycle manifest validates, resolves and refuses by hash and path."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import release_cycle_manifest as tool  # noqa: E402

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


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _put(path: Path, data: bytes) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"sha256": _sha(data), "size_bytes": len(data)}


def _pin(root_key: str, relative: str, info: dict[str, Any]) -> dict[str, Any]:
    return {"path": {"root_key": root_key, "relative": relative}, **info}


@pytest.fixture
def cycle(tmp_path: Path) -> dict[str, Any]:
    data, scratch = tmp_path / "data", tmp_path / "scratch"
    data.mkdir()
    scratch.mkdir()
    providers: dict[str, Any] = {}
    for index, provider_id in enumerate(PROVIDERS):
        generation_id = f"gen-{provider_id}-{index}"
        body = json.dumps(
            {"generation_id": generation_id, "provider": provider_id}
        ).encode()
        info = _put(
            data
            / f"data-{provider_id}"
            / "generations"
            / generation_id
            / "generation.json",
            body,
        )
        entry: dict[str, Any] = {
            "root": {"root_key": "data", "relative": f"data-{provider_id}"},
            "generation": {"mode": "single"},
            "manifest_sha256": info["sha256"],
        }
        if provider_id in NATIVE:
            native = _put(
                data / "native" / f"{provider_id}.json",
                f"native {provider_id}".encode(),
            )
            entry["finality_evidence"] = _pin(
                "data", f"native/{provider_id}.json", native
            )
        providers[provider_id] = entry
    strategy = _put(data / "evidence" / "strategy.json", b"{}")
    cost = _put(data / "cost" / "cost.csv", b"a,b\n")
    (data / "bundles" / "factors").mkdir(parents=True)
    manifest = {
        "schema_version": "qe-release-cycle-manifest/1",
        "cycle_id": "cycle-x",
        "cutoff": "2026-01-02",
        "evaluated_on": "2026-01-05",
        "captured_at_utc": "2026-01-04T10:00:00Z",
        "predecessor": {"release_id": "rel-prev"},
        "scratch": {"root_key": "scratch", "relative": "cycle-x"},
        "providers": providers,
        "strategy_evidence": _pin("data", "evidence/strategy.json", strategy),
        "cost_inputs": {"economics": _pin("data", "cost/cost.csv", cost)},
        "public_bundles": {
            "factors": {"root_key": "data", "relative": "bundles/factors"}
        },
    }
    return {
        "manifest": manifest,
        "roots": {"data": data, "scratch": scratch},
        "tmp": tmp_path,
    }


def _resolve(cycle: dict[str, Any]) -> dict[str, Any]:
    return tool.resolve(tool.dumps(cycle["manifest"]), cycle["roots"])


def _refused(cycle: dict[str, Any], code: str) -> None:
    with pytest.raises(tool.CycleManifestError) as info:
        _resolve(cycle)
    assert info.value.code == code, str(info.value)


def test_a_synthetic_cycle_resolves_to_the_documented_shape(
    cycle: dict[str, Any],
) -> None:
    resolved = _resolve(cycle)
    assert tool._resolved_errors(resolved) == ""
    assert set(resolved["providers"]) == set(PROVIDERS)
    assert resolved["providers"]["alpaca"]["finality_source"] == "GENERATION_MANIFEST"
    alpaca = resolved["providers"]["alpaca"]
    assert alpaca["finality_evidence"]["sha256"] == alpaca["manifest_sha256"]
    assert resolved["providers"]["ibkr"]["finality_evidence"] is None
    assert resolved["providers"]["schwab"]["finality_evidence"]["path"].endswith(
        "native/schwab.json"
    )
    assert resolved["cycle_manifest_sha256"] == _sha(tool.dumps(cycle["manifest"]))
    assert tool.ids(resolved)["providers"]["portfolio123"] == "gen-portfolio123-4"


def test_cli_validate_resolve_and_print_ids_end_to_end(
    cycle: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    tmp = cycle["tmp"]
    manifest = tmp / "cycle.json"
    manifest.write_bytes(tool.dumps(cycle["manifest"]))
    roots = [f"--root={key}={path}" for key, path in cycle["roots"].items()]
    assert tool.main(["validate", "--manifest", str(manifest)]) == 0
    assert tool.main(["validate", "--manifest", str(manifest), *roots]) == 0
    out = tmp / "resolved.json"
    assert (
        tool.main(["resolve", "--manifest", str(manifest), *roots, "--out", str(out)])
        == 0
    )
    assert json.loads(out.read_bytes())["schema_version"] == tool.RESOLVED_VERSION
    assert b"\r" not in out.read_bytes()
    capsys.readouterr()
    assert tool.main(["print-ids", "--manifest", str(manifest), *roots]) == 0
    assert json.loads(capsys.readouterr().out)["cycle_id"] == "cycle-x"
    # create-only
    assert (
        tool.main(["resolve", "--manifest", str(manifest), *roots, "--out", str(out)])
        == 1
    )


def test_resolved_output_is_deterministic(cycle: dict[str, Any]) -> None:
    assert tool.dumps(_resolve(cycle)) == tool.dumps(_resolve(cycle))


def test_hash_mismatch_of_a_generation_manifest_is_refused(
    cycle: dict[str, Any],
) -> None:
    cycle["manifest"]["providers"]["alpaca"]["manifest_sha256"] = "0" * 64
    _refused(cycle, "HASH_MISMATCH")


def test_hash_mismatch_of_native_finality_evidence_is_refused(
    cycle: dict[str, Any],
) -> None:
    (cycle["roots"]["data"] / "native" / "schwab.json").write_bytes(b"changed")
    _refused(cycle, "HASH_MISMATCH")


def test_size_pin_mismatch_is_refused(cycle: dict[str, Any]) -> None:
    cycle["manifest"]["strategy_evidence"]["size_bytes"] = 999
    _refused(cycle, "SIZE_MISMATCH")


def test_cycle_manifest_pin_mismatch_is_refused(cycle: dict[str, Any]) -> None:
    with pytest.raises(tool.CycleManifestError) as info:
        tool.resolve(tool.dumps(cycle["manifest"]), cycle["roots"], "1" * 64)
    assert info.value.code == "HASH_MISMATCH"


def test_missing_native_finality_evidence_file_is_refused(
    cycle: dict[str, Any],
) -> None:
    (cycle["roots"]["data"] / "native" / "quantbase.json").unlink()
    _refused(cycle, "MISSING")


def test_native_provider_without_declared_evidence_is_refused(
    cycle: dict[str, Any],
) -> None:
    del cycle["manifest"]["providers"]["matrixport"]["finality_evidence"]
    _refused(cycle, "FINALITY_EVIDENCE_REQUIRED")


def test_finite_provider_may_not_name_evidence_of_its_own(
    cycle: dict[str, Any],
) -> None:
    cycle["manifest"]["providers"]["alpaca"]["finality_evidence"] = cycle["manifest"][
        "strategy_evidence"
    ]
    _refused(cycle, "FINALITY_EVIDENCE_NOT_ALLOWED")


def test_two_generations_under_one_root_are_ambiguous_unless_one_is_named(
    cycle: dict[str, Any],
) -> None:
    other = cycle["roots"]["data"] / "data-fidelity" / "generations" / "other"
    other.mkdir(parents=True)
    _refused(cycle, "AMBIGUOUS_GENERATION")
    gid = "gen-fidelity-1"
    cycle["manifest"]["providers"]["fidelity"]["generation"] = {
        "mode": "explicit",
        "generation_id": gid,
    }
    assert _resolve(cycle)["providers"]["fidelity"]["generation_id"] == gid


def test_explicit_generation_that_is_not_there_is_refused(
    cycle: dict[str, Any],
) -> None:
    cycle["manifest"]["providers"]["ibkr"]["generation"] = {
        "mode": "explicit",
        "generation_id": "absent",
    }
    _refused(cycle, "MISSING")


def test_generation_manifest_naming_another_generation_is_refused(
    cycle: dict[str, Any],
) -> None:
    path = (
        cycle["roots"]["data"]
        / "data-ibkr"
        / "generations"
        / "gen-ibkr-2"
        / "generation.json"
    )
    info = _put(path, json.dumps({"generation_id": "someone-else"}).encode())
    cycle["manifest"]["providers"]["ibkr"]["manifest_sha256"] = info["sha256"]
    _refused(cycle, "ID_MISMATCH")


@pytest.mark.parametrize(
    "relative",
    [
        "../outside.json",
        "a/../../outside.json",
        "/abs.json",
        "C:/abs.json",
        "a\\b.json",
        "a//b.json",
    ],
)
def test_a_path_that_would_leave_its_root_is_refused(
    cycle: dict[str, Any], relative: str
) -> None:
    cycle["manifest"]["strategy_evidence"]["path"]["relative"] = relative
    with pytest.raises(tool.CycleManifestError) as info:
        _resolve(cycle)
    assert info.value.code == "PATH_ESCAPES_ROOT"


def test_a_symlink_leaving_its_root_is_refused(cycle: dict[str, Any]) -> None:
    outside = cycle["tmp"] / "outside.json"
    outside.write_bytes(b"{}")
    link = cycle["roots"]["data"] / "evidence" / "link.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    cycle["manifest"]["strategy_evidence"] = _pin(
        "data", "evidence/link.json", {"sha256": _sha(b"{}")}
    )
    _refused(cycle, "PATH_ESCAPES_ROOT")


def test_an_unbound_root_key_is_refused(cycle: dict[str, Any]) -> None:
    cycle["manifest"]["cost_inputs"]["economics"]["path"]["root_key"] = "nowhere"
    _refused(cycle, "UNKNOWN_ROOT")


def test_cutoff_after_evaluation_date_is_refused(cycle: dict[str, Any]) -> None:
    cycle["manifest"]["cutoff"] = "2026-02-01"
    _refused(cycle, "DATE_ORDER")


def test_schema_violations_are_refused(cycle: dict[str, Any]) -> None:
    bad = copy.deepcopy(cycle["manifest"])
    del bad["providers"]["schwab"]
    bad["surprise"] = 1
    with pytest.raises(tool.CycleManifestError) as info:
        tool.validate_static(bad)
    assert info.value.code == "SCHEMA"


def test_missing_public_bundle_directory_is_refused(cycle: dict[str, Any]) -> None:
    (cycle["roots"]["data"] / "bundles" / "factors").rmdir()
    _refused(cycle, "MISSING")


def test_roots_must_be_existing_absolute_directories(tmp_path: Path) -> None:
    with pytest.raises(tool.CycleManifestError) as info:
        tool.parse_roots([f"data={tmp_path / 'nope'}"], None)
    assert info.value.code == "ROOT_MISSING"
    with pytest.raises(tool.CycleManifestError) as info:
        tool.parse_roots([f"data={tmp_path}", f"data={tmp_path / 'x'}"], None)
    assert info.value.code == "ROOTS"
