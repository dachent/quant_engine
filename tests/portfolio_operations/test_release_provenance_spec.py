"""``release_provenance_spec.py``: the financial MVP contract hashes come from the repository, not the seed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import release_provenance_spec as tool  # noqa: E402

FILES = {
    "contract": "contracts/financial-contract.json",
    "registry": "contracts/financial-registry.json",
    "schema": "schemas/financial-schema.json",
}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    for name, relative in FILES.items():
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_bytes(f"{name} v1\n".encode())
    return repo


def _seed(tmp_path: Path) -> Path:
    section = {}
    for name, relative in FILES.items():
        section[f"{name}_path"] = str(Path("/old-clone") / relative)
        section[f"{name}_sha256"] = "0" * 64
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps({"acquisition_date": "2026-09-17", "financial_mvp": section}),
        encoding="utf-8",
    )
    return seed


def _run(tmp_path: Path, repo: Path, *extra: str) -> int:
    return tool.main(
        [
            "refresh",
            "--seed-spec",
            str(_seed(tmp_path)),
            "--out",
            str(tmp_path / "out.json"),
            "--repo-root",
            str(repo),
            *extra,
        ]
    )


def test_hashes_and_paths_come_from_the_repository_and_the_rest_is_carried(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    assert _run(tmp_path, repo) == 0
    spec = json.loads((tmp_path / "out.json").read_bytes())
    assert spec["acquisition_date"] == "2026-09-17"
    for name, relative in FILES.items():
        path = (repo / relative).resolve()
        assert spec["financial_mvp"][f"{name}_path"] == str(path)
        assert (
            spec["financial_mvp"][f"{name}_sha256"]
            == hashlib.sha256(path.read_bytes()).hexdigest()
        )


def test_a_changed_contract_changes_the_derived_hash_and_check_refuses_the_old_file(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    assert _run(tmp_path, repo) == 0
    (repo / FILES["contract"]).write_bytes(b"contract v2\n")
    assert _run(tmp_path, repo, "--check") == 1
    # the create-only output is not silently replaced either
    assert _run(tmp_path, repo) == 1


def test_a_rerun_that_derives_the_same_bytes_completes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _run(tmp_path, repo) == 0
    assert _run(tmp_path, repo) == 0
    assert _run(tmp_path, repo, "--check") == 0


def test_a_missing_repository_file_or_section_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    (repo / FILES["schema"]).unlink()
    assert _run(tmp_path, repo) == 1
    assert "MISSING" in capsys.readouterr().err
    with pytest.raises(tool.ProvenanceSpecError, match="SEED"):
        tool.refresh_spec({"financial_mvp": []}, repo)
    # a spec without the optional section is carried unchanged
    assert tool.refresh_spec({"acquisition_date": "2026-09-17"}, repo) == {
        "acquisition_date": "2026-09-17"
    }


def test_an_interrupted_write_leaves_no_partial_output_and_a_rerun_completes(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    # what an interrupted run leaves: the temporary file only, never a truncated output
    (tmp_path / "out.json.partial").write_bytes(b'{"acq')
    assert not (tmp_path / "out.json").exists()
    assert _run(tmp_path, repo) == 0
    assert json.loads((tmp_path / "out.json").read_bytes())["financial_mvp"]
    assert not (tmp_path / "out.json.partial").exists()
