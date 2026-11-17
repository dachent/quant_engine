"""G2 judges a panel CANDIDATE (no generation.json) instead of refusing it (#884, train blocker).

int14's G2 failed with 'required generation file is absent: generation.json' because the stage
graph pointed validate_generation at stages/panel/candidates/<id>; a candidate is data/, the page
files and candidate.json, and the manifest is written only when the generation is published.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys


from test_validate_generation import CUTOFF, build

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import validate_generation  # noqa: E402


def as_candidate(gen_dir: Path) -> Path:
    """A's real candidate layout: the generation without its manifest and qa/ output."""

    candidate = gen_dir.parent / "candidate"
    shutil.copytree(gen_dir, candidate)
    (candidate / "generation.json").unlink()
    shutil.rmtree(candidate / "qa", ignore_errors=True)
    assert not (candidate / "generation.json").exists()
    return candidate


def _run(directory: Path, tmp: Path, *extra: str) -> int:
    return validate_generation.main(
        [
            str(directory),
            "--cutoff",
            CUTOFF,
            "--no-discover",
            "--out",
            str(tmp / "f.json"),
            *extra,
        ]
    )


def test_a_candidate_without_generation_json_is_an_error_without_the_flag(tmp_path):
    candidate = as_candidate(build(tmp_path / "g"))
    assert _run(candidate, tmp_path) == validate_generation.EXIT_ERROR


def test_the_candidate_flag_judges_it_and_skips_only_manifest_bound_checks(tmp_path):
    candidate = as_candidate(build(tmp_path / "g"))
    code = _run(candidate, tmp_path, "--candidate")
    assert code in (validate_generation.EXIT_OK, validate_generation.EXIT_BLOCKERS)
    ids = {
        f["check_id"]
        for f in json.loads((tmp_path / "f.json").read_bytes())["findings"]
    }
    assert "identity.generation_id" not in ids
    assert not [i for i in ids if i.startswith("identity.nested")]


def test_a_candidate_check_still_catches_a_product_defect(tmp_path):
    candidate = as_candidate(build(tmp_path / "g"))
    panel_path = candidate / "data" / "dashboard_data.json"
    panel = json.loads(panel_path.read_bytes())
    panel["fact_status"] = {**panel.get("fact_status", {}), "accounts": "WRONG_TOKEN"}
    panel["allocations"] = [{"systematic_weight": "1.7"}]
    panel_path.write_bytes(json.dumps(panel).encode())
    assert _run(candidate, tmp_path, "--candidate") != validate_generation.EXIT_ERROR


def test_the_flag_refuses_a_directory_that_already_has_a_manifest(tmp_path):
    generation = build(tmp_path / "g")
    assert _run(generation, tmp_path, "--candidate") == validate_generation.EXIT_ERROR


def test_the_g2_stage_validates_the_candidate_in_candidate_mode():
    graph = json.loads(
        (ROOT / "ops" / "mvp2-release" / "release-stages.json").read_bytes()
    )
    g2 = next(s for s in graph["stages"] if s["id"] == "g2")
    argv = [a for a in g2["validator_argv"] if isinstance(a, str)]
    assert "--candidate" in argv
    assert any("/stages/panel/candidates/" in a for a in argv)
