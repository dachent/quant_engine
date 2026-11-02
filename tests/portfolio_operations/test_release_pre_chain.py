"""RELEASE-DRIVER-001 B5: the pre-chain stage replaces the retained hand script ``pre_chain.py``.

Everything is synthetic and built here. The heavy producers (layered history, dependence,
attribution) are stubbed by small deterministic modules in a throwaway repository whose output
is a hash of every argument it receives, so two runs write the same bytes only if they made the
same calls with the same arguments. The retained R2 script and the new tool run on that same
cycle and every byte either writes is compared.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import pre_chain_r2_reference as reference  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "release_pre_chain.py"
COMMIT = "1fa910efde7255244e7dee3d5c36ae880eadb562"
CYCLE_SHA = "c" * 64
BASELINE = "b" * 64
HISTORY = "a1" * 32

_STUB_PREP = """
from pathlib import Path
import hashlib, json

ROOT = Path(__file__).resolve().parents[1]


def _pin(value):
    return Path(str(value))


def _selected_history_authorities(files):
    names = ("provider_admission_receipt_path", "financial_input_contract_path",
             "provider_ledger_manifest_path", "provider_ledger_catalog_path",
             "portfolio123_version_evidence_path")
    return {name: _pin(files.get(name)) for name in names}


def selected_history_authorities(files):
    return _selected_history_authorities(files)


def _rebuild_layered_history(selection, output_root, release_commit, contract_path, authorities):
    digest = hashlib.sha256(json.dumps(
        [selection, str(output_root), release_commit, str(contract_path),
         {k: str(v) for k, v in sorted(authorities.items())}], sort_keys=True).encode()).hexdigest()
    target = Path(output_root) / digest
    target.mkdir(parents=True)
    (target / "history.json").write_text(digest, encoding="utf-8")
    return target


def rebuild_layered_history(*args):
    return _rebuild_layered_history(*args)
"""

_STUB_DEPENDENCE = """
from pathlib import Path
import hashlib, json


def write_source(output_root, **kwargs):
    digest = hashlib.sha256(json.dumps(
        [str(output_root), {k: str(v) for k, v in sorted(kwargs.items())}]).encode()).hexdigest()
    target = Path(output_root) / digest
    target.mkdir(parents=True)
    (target / "dependence.json").write_text(digest, encoding="utf-8")
    return {"destination": str(target)}


def verify_packaged_source(root, **kwargs):
    if not (Path(root) / "dependence.json").is_file():
        raise SystemExit("unverified")
"""

_STUB_ATTRIBUTION = """
from pathlib import Path
import hashlib, json, sys

args = sys.argv[1:]
out = Path(args[args.index("--output-root") + 1])
digest = hashlib.sha256(json.dumps(args).encode()).hexdigest()
target = out / "generations" / digest
target.mkdir(parents=True)
(target / "attribution.json").write_text(json.dumps(args), encoding="utf-8")
print("staged", digest)
"""


def _repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "portfolio_attended_preparation.py").write_text(_STUB_PREP)
    (repo / "tools" / "portfolio_current_dependence.py").write_text(_STUB_DEPENDENCE)
    (repo / "tools" / "portfolio_attribution.py").write_text(_STUB_ATTRIBUTION)
    shutil.copy(TOOL, repo / "tools" / "release_pre_chain.py")
    (repo / "reference_pre_chain.py").write_text(reference.SOURCE, encoding="utf-8")
    return repo


def _cycle(tmp: Path, *, baselines: tuple[str, ...] = (BASELINE,)) -> dict[str, Path]:
    cycle = tmp / "cycle"
    for gid in baselines:
        (cycle / "baseline" / "generations" / gid).mkdir(parents=True)
        (cycle / "baseline" / "generations" / gid / "generation.json").write_text("{}")
    (cycle / "attribution-spec.json").write_text("{}", encoding="utf-8")
    ledger = cycle / "ledger" / "generations" / ("1" * 64)
    ledger.mkdir(parents=True)
    files = {
        name: str(cycle / "in" / name)
        for name in (
            "provider_admission_receipt_path",
            "financial_input_contract_path",
            "provider_ledger_catalog_path",
            "portfolio123_version_evidence_path",
        )
    }
    files["provider_ledger_manifest_path"] = str(ledger / "generation.json")
    selected = cycle / "in" / "selected-inputs.json"
    selected.parent.mkdir(parents=True)
    selected.write_text(
        json.dumps(
            {
                "files": files,
                "builder_kwargs": {
                    "provider_ledger_manifest_path": files[
                        "provider_ledger_manifest_path"
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    resolved = cycle / "in" / "resolved-cycle.json"
    resolved.write_text(
        json.dumps(
            {
                "schema_version": "qe-release-resolved-cycle/1",
                "scratch": str(cycle),
                "evaluated_on": "2026-10-27",
                "cycle_manifest_sha256": CYCLE_SHA,
                # the B1+B2 provider rows (qualification, finalized_through) are not read here
                "providers": {
                    "schwab": {
                        "qualification": "STAGED_COMPLETE",
                        "finalized_through": "2026-10-27",
                        "finality_source": "CYCLE_MANIFEST_NATIVE",
                    },
                    "alpaca": {
                        "qualification": "PUBLISHED_COMPLETE",
                        "finalized_through": None,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return {"cycle": cycle, "selected": selected, "resolved": resolved}


def _run_tool(
    repo: Path, world: dict[str, Path], *extra: str, resolved: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(repo / "tools" / "release_pre_chain.py"),
            "--resolved-cycle",
            str(resolved or world["resolved"]),
            "--cycle",
            str(world["cycle"]),
            "--selected-inputs",
            str(world["selected"]),
            "--commit",
            COMMIT,
            *extra,
        ],
        capture_output=True,
        text=True,
        cwd=repo,
    )


def _run_reference(
    repo: Path, world: dict[str, Path]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(repo / "reference_pre_chain.py"),
            str(world["cycle"]),
            str(world["selected"]),
            BASELINE,
            "2026-10-27",
            COMMIT,
        ],
        capture_output=True,
        text=True,
        cwd=repo,
    )


def _snapshot(cycle: Path, *, skip: tuple[str, ...] = ()) -> dict[str, bytes]:
    return {
        path.relative_to(cycle).as_posix(): path.read_bytes()
        for path in sorted(cycle.rglob("*"))
        if path.is_file()
        and path.relative_to(cycle).parts[0] in ("pre", "attribution")
        and path.name not in skip
    }


def test_the_reference_script_is_the_retained_r2_script() -> None:
    assert (
        hashlib.sha256(reference.SOURCE.encode("utf-8")).hexdigest() != ""
    )  # line endings normalised; the raw sha256 is recorded for the evidence note
    assert re.fullmatch(r"[0-9a-f]{64}", reference.SHA256)
    assert "prep._rebuild_layered_history" in reference.SOURCE


def test_the_stage_writes_exactly_the_bytes_of_the_retained_hand_script(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    world = _cycle(tmp_path)
    cycle = world["cycle"]
    old = _run_reference(repo, world)
    assert old.returncode == 0, old.stderr
    expected = _snapshot(cycle)
    assert any(name.startswith("attribution/generations/") for name in expected)
    assert any(name.startswith("pre/history/") for name in expected)
    assert "pre/dependence-selected-inputs.json" in expected
    shutil.rmtree(cycle / "pre")
    shutil.rmtree(cycle / "attribution")
    new = _run_tool(repo, world)
    assert new.returncode == 0, new.stderr
    assert _snapshot(cycle) == expected
    # the only addition is the cycle identity file, outside the stage's two outputs
    identity = json.loads((cycle / "pre-chain-cycle.json").read_text(encoding="utf-8"))
    assert identity == {
        "cycle_manifest_sha256": CYCLE_SHA,
        "release_commit": COMMIT,
        "schema_version": "qe-release-pre-chain-cycle/1",
    }
    # same console lines apart from the interpreter path noise
    assert [
        ln for ln in old.stdout.splitlines() if ln.startswith(("HISTORY", "DEPEND"))
    ] == [ln for ln in new.stdout.splitlines() if ln.startswith(("HISTORY", "DEPEND"))]


def test_the_baseline_id_defaults_to_the_cycles_single_generation(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    world = _cycle(tmp_path)
    explicit = _run_tool(repo, world, "--baseline-id", BASELINE)
    assert explicit.returncode == 0, explicit.stderr
    expected = _snapshot(world["cycle"])
    shutil.rmtree(world["cycle"] / "pre")
    shutil.rmtree(world["cycle"] / "attribution")
    derived = _run_tool(repo, world)
    assert derived.returncode == 0, derived.stderr
    assert _snapshot(world["cycle"]) == expected


def test_a_killed_stage_is_restarted_over_its_partial_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    world = _cycle(tmp_path)
    assert _run_tool(repo, world).returncode == 0
    history = [p.name for p in (world["cycle"] / "pre" / "history").iterdir()]
    # killed after history, before dependence: the later outputs are gone
    shutil.rmtree(world["cycle"] / "pre" / "dependence")
    shutil.rmtree(world["cycle"] / "attribution")
    again = _run_tool(repo, world)
    assert again.returncode == 0, again.stderr
    assert "reusing history" in again.stdout
    assert [p.name for p in (world["cycle"] / "pre" / "history").iterdir()] == history
    assert (world["cycle"] / "attribution" / "generations").is_dir()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("schema", "resolved cycle schema differs"),
        ("scratch-relative", "no absolute scratch root"),
        ("scratch-other", "not the resolved cycle's scratch root"),
        ("identity-missing", "no cycle_manifest_sha256"),
        ("date", "evaluated_on is not a date"),
        ("unreadable", "resolved cycle is unreadable"),
    ],
)
def test_a_bad_resolved_cycle_is_refused_before_anything_is_written(
    tmp_path: Path, mutation: str, message: str
) -> None:
    repo = _repo(tmp_path)
    world = _cycle(tmp_path)
    value: dict[str, Any] = json.loads(world["resolved"].read_text(encoding="utf-8"))
    if mutation == "schema":
        value["schema_version"] = "qe-release-resolved-cycle/2"
    elif mutation == "scratch-relative":
        value["scratch"] = "cycle"
    elif mutation == "scratch-other":
        value["scratch"] = str(tmp_path / "another-cycle")
    elif mutation == "identity-missing":
        del value["cycle_manifest_sha256"]
    elif mutation == "date":
        value["evaluated_on"] = "2026-13-40"
    if mutation == "unreadable":
        world["resolved"].write_text("{not json", encoding="utf-8")
    else:
        world["resolved"].write_text(json.dumps(value), encoding="utf-8")
    done = _run_tool(repo, world)
    assert done.returncode == 1 and message in done.stderr
    assert not (world["cycle"] / "pre").exists()
    assert not (world["cycle"] / "pre-chain-cycle.json").exists()


def test_inputs_that_are_not_what_the_cycle_holds_are_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    world = _cycle(tmp_path, baselines=(BASELINE, "d" * 64))
    ambiguous = _run_tool(repo, world)
    assert ambiguous.returncode == 1
    assert "exactly one generation" in ambiguous.stderr
    absent = _run_tool(repo, world, "--baseline-id", "e" * 64)
    assert absent.returncode == 1 and "baseline generation is absent" in absent.stderr
    bad_commit = subprocess.run(
        [
            sys.executable,
            str(repo / "tools" / "release_pre_chain.py"),
            "--resolved-cycle",
            str(world["resolved"]),
            "--cycle",
            str(world["cycle"]),
            "--selected-inputs",
            str(world["selected"]),
            "--commit",
            "HEAD",
            "--baseline-id",
            BASELINE,
        ],
        capture_output=True,
        text=True,
        cwd=repo,
    )
    assert bad_commit.returncode == 1 and "40 lowercase hex" in bad_commit.stderr
    world["selected"].unlink()
    gone = _run_tool(repo, world, "--baseline-id", BASELINE)
    assert gone.returncode == 1 and "selected inputs are absent" in gone.stderr
    assert not (world["cycle"] / "pre").exists()


def test_a_cycle_identity_from_another_manifest_blocks_a_resume(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    world = _cycle(tmp_path)
    assert _run_tool(repo, world).returncode == 0
    other = json.loads(world["resolved"].read_text(encoding="utf-8"))
    other["cycle_manifest_sha256"] = "9" * 64
    resolved = tmp_path / "other-resolved.json"
    resolved.write_text(json.dumps(other), encoding="utf-8")
    done = _run_tool(repo, world, resolved=resolved)
    assert done.returncode == 1
    assert "another cycle manifest or commit" in done.stderr


def test_an_ambiguous_existing_history_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    world = _cycle(tmp_path)
    history = world["cycle"] / "pre" / "history"
    (history / HISTORY).mkdir(parents=True)
    (history / ("f" * 64)).mkdir()
    done = _run_tool(repo, world)
    assert done.returncode == 1 and "exactly one generation" in done.stderr


def test_the_attribution_stage_failure_is_the_stage_exit_code(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    world = _cycle(tmp_path)
    (repo / "tools" / "portfolio_attribution.py").write_text(
        "import sys\nprint('refused', file=sys.stderr)\nraise SystemExit(7)\n"
    )
    done = _run_tool(repo, world)
    assert done.returncode == 7 and "refused" in done.stdout


def test_the_tool_carries_no_real_id_or_absolute_path() -> None:
    text = TOOL.read_text(encoding="utf-8")
    assert not re.search(r"\b[0-9a-f]{40,64}\b", text)
    assert not re.search(r"[A-Za-z]:[\\/]", text)
    assert "qe-p2" not in text and "worktrees" not in text
