"""Storage gates, the shared retained-cache base, run-root retirement and run states.

Every rule the attended runner enforces here is exercised twice: once where it must
refuse, and once where the same shape must be admitted, so a gate that refuses
everything fails as surely as one that refuses nothing.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable

import pytest

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    run_storage,
)
from quant_engine.core.contracts import canonical_json_bytes

import portfolio_correlation as correlation
import portfolio_decomposition as decomposition
import portfolio_pipeline_materialize as materializer

SCRATCH = "C:\\Dev\\scratch\\"
REPOSITORY_ROOT = Path(materializer.__file__).resolve().parents[1]
MIB = 2**20
# A stage child's payload: far above the few KiB of receipts the runner itself writes.
PAYLOAD = 64 * 1024
# A venv's python.exe on Windows is a launcher that runs the real interpreter as its
# child, so the PID a run records is not the launcher's. Test runs use the base
# interpreter directly (quant_engine.core is standard-library only), so the process
# a test spawns is the process that records its state.
BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)
CHILD_ENVIRONMENT = {
    **os.environ,
    "PYTHONPATH": os.pathsep.join(
        filter(
            None,
            (
                str(REPOSITORY_ROOT / "packages" / "core" / "src"),
                os.environ.get("PYTHONPATH"),
            ),
        )
    ),
}


def _same(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _clear_sync_environment(monkeypatch: pytest.MonkeyPatch, **values: Path) -> None:
    for name in run_storage.SYNC_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, str(value))


# --- 1. location gate -----------------------------------------------------------------


def test_location_gate_refuses_a_root_under_a_onedrive_prefix(tmp_path: Path) -> None:
    synced = tmp_path / "OneDrive - Example"
    root = synced / "runs" / "out"

    with pytest.raises(run_storage.RunStorageError) as raised:
        run_storage.refuse_synced_root(
            root,
            "output",
            environ={"OneDriveCommercial": str(synced)},
            attributes=lambda _path: None,
        )

    message = str(raised.value)
    assert str(root) in message
    assert "%OneDriveCommercial%" in message
    assert SCRATCH in message


def test_location_gate_admits_a_sibling_that_only_shares_the_prefix_text(
    tmp_path: Path,
) -> None:
    """Containment is by path component, not by string prefix."""

    environ = {
        name: str(tmp_path / "OneDrive") for name in run_storage.SYNC_ENVIRONMENT
    }
    for root in (tmp_path / "OneDrive-scratch" / "out", tmp_path / "scratch" / "out"):
        run_storage.refuse_synced_root(
            root, "output", environ=environ, attributes=lambda _path: None
        )


@pytest.mark.parametrize(
    ("attributes", "reparse_tag"),
    (
        (run_storage.CLOUD_FILE_ATTRIBUTES["PINNED"], 0),
        (run_storage.CLOUD_FILE_ATTRIBUTES["UNPINNED"], 0),
        (run_storage.CLOUD_FILE_ATTRIBUTES["RECALL_ON_DATA_ACCESS"], 0),
        (run_storage.CLOUD_FILE_ATTRIBUTES["OFFLINE"], 0),
        (0, 0x9000701A),
    ),
)
def test_location_gate_refuses_cloud_files_state_on_any_ancestor(
    tmp_path: Path, attributes: int, reparse_tag: int
) -> None:
    """A SharePoint library outside every %OneDrive*% prefix is still a sync root."""

    library = tmp_path / "Example" / "investing - Documents"

    def reader(path: Path) -> tuple[int, int]:
        return (attributes, reparse_tag) if _same(path, library) else (0x10, 0)

    with pytest.raises(run_storage.RunStorageError, match="cloud-synced"):
        run_storage.refuse_synced_root(
            library / "_quant_engine" / "run", "output", environ={}, attributes=reader
        )


def test_location_gate_admits_plain_directories_as_this_filesystem_reports_them(
    tmp_path: Path,
) -> None:
    """The real attribute reader on a plain scratch directory finds no cloud state."""

    (tmp_path / "scratch").mkdir()
    run_storage.refuse_synced_root(tmp_path / "scratch" / "run", "output", environ={})
    run_storage.refuse_synced_root(
        tmp_path / "run",
        "output",
        environ={},
        attributes=lambda _path: (0x10 | 0x1, 0),  # DIRECTORY | READONLY
    )


def _attended_argv(tmp_path: Path) -> list[str]:
    scratch = tmp_path / "scratch"
    return [
        "attended",
        "--repo-root",
        str(REPOSITORY_ROOT),
        "--python",
        sys.executable,
        "--workspace-root",
        str(scratch / "workspace"),
        "--output-root",
        str(scratch / "out"),
        "--retained-cache-root",
        str(scratch / "cache"),
        "--retained-cache-base",
        str(scratch / "cache-base"),
        "--requested-as-of",
        "2026-09-17",
        "--source-run",
        str(scratch / "source-run.json"),
        "--qa-hold",
        "--execute",
    ]


@pytest.mark.parametrize(
    "flag",
    (
        "--output-root",
        "--workspace-root",
        "--retained-cache-root",
        "--retained-cache-base",
    ),
)
def test_attended_refuses_a_synced_root_before_any_probe_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
) -> None:
    synced = tmp_path / "OneDrive - Example"
    _clear_sync_environment(monkeypatch, OneDrive=synced)
    probes: list[object] = []
    monkeypatch.setattr(
        materializer, "_runtime_identity", lambda *args: probes.append(args)
    )
    argv = _attended_argv(tmp_path)
    argv[argv.index(flag) + 1] = str(synced / "run-root")

    assert materializer.main(argv) == 1

    error = capsys.readouterr().err
    assert str(synced / "run-root") in error
    assert SCRATCH in error
    assert probes == []
    assert not synced.exists()
    assert not (tmp_path / "scratch").exists()


def test_attended_location_gate_admits_scratch_roots_and_reaches_the_next_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_sync_environment(monkeypatch, OneDrive=tmp_path / "OneDrive - Example")
    reached: list[bool] = []

    def probe(*_args: object) -> dict[str, object]:
        reached.append(True)
        raise materializer.MaterializationError("runtime probe reached")

    monkeypatch.setattr(materializer, "_runtime_identity", probe)

    assert materializer.main(_attended_argv(tmp_path)) == 1
    assert reached == [True]


# --- 2. budget gate -------------------------------------------------------------------


def _storage_preflight(tmp_path: Path, cache: Path) -> dict[str, object]:
    source_run = tmp_path / "source-run.json"
    source_run.write_text("{}", encoding="utf-8")
    return {
        "financial_inputs": {},
        "financial_stage_inputs": {},
        "portfolio_root": tmp_path / "workspace" / "dataPortfolio",
        "python": Path(sys.executable),
        "qa_dir": None,
        "recipe": tmp_path / "recipe.json",
        "repo_root": tmp_path / "repo",
        "request_identity": {"fixture": "attended-run-storage"},
        "required_stages": ("ledger", "baseline"),
        "retained_cache_root": cache,
        "requested_as_of": "2026-09-17",
        "source_phase": {"status": "PASS"},
        "source_run": source_run,
        "workspace_root": tmp_path / "workspace",
    }


def _argv(preflight: dict[str, object], output_root: Path, *extra: str) -> list[str]:
    return [
        "attended",
        "--repo-root",
        str(preflight["repo_root"]),
        "--python",
        str(preflight["python"]),
        "--workspace-root",
        str(preflight["workspace_root"]),
        "--output-root",
        str(output_root),
        "--retained-cache-root",
        str(preflight["retained_cache_root"]),
        "--requested-as-of",
        "2026-09-17",
        "--source-run",
        str(preflight["source_run"]),
        "--qa-hold",
        "--execute",
        *extra,
    ]


def _writing_child(
    output_root: Path,
    *,
    payload: int = PAYLOAD,
    calls: list[list[str]] | None = None,
    returncode: int = 0,
    also: Callable[[list[str]], None] | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        role = argv[argv.index("--role") + 1]
        if calls is not None:
            calls.append(argv)
        stage = output_root / "stages" / role
        stage.mkdir(parents=True, exist_ok=True)
        (stage / "payload.bin").write_bytes(b"x" * payload)
        if also is not None:
            also(argv)
        if returncode == 0:
            results = output_root / "stage-results"
            results.mkdir(exist_ok=True)
            (results / f"{role}.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(argv, returncode, "", "")

    return run


def _complete_by_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Storage tests judge completion by receipt; native verification is tested elsewhere."""

    def completed(
        root: Path, stages: tuple[str, ...], *, failed_stage: str | None = None
    ) -> list[str]:
        done: list[str] = []
        for role in stages:
            if (
                role == failed_stage
                or not (root / "stage-results" / f"{role}.json").is_file()
            ):
                break
            done.append(role)
        return done

    monkeypatch.setattr(materializer, "_attended_completed_stages", completed)


def _observations(output_root: Path) -> dict[str, list[dict[str, object]]]:
    value = json.loads(
        (output_root / "attended-execution-observations.json").read_text("utf-8")
    )
    assert value["schema_version"] == 3
    return value


def _state(output_root: Path) -> dict[str, object]:
    state = run_storage.read_run_state(output_root)
    assert state is not None
    return state


def test_run_growth_budget_fails_the_stage_that_breaches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    preflight = _storage_preflight(tmp_path, cache)
    output_root = tmp_path / "out"
    calls: list[list[str]] = []
    _complete_by_receipt(monkeypatch)
    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(
        materializer.subprocess, "run", _writing_child(output_root, calls=calls)
    )

    argv = _argv(preflight, output_root, "--max-run-growth", "32KiB")
    assert materializer.main(argv) == 1

    assert [call[call.index("--role") + 1] for call in calls] == ["ledger"]
    attempt = _observations(output_root)["execution_attempts"][-1]
    assert attempt["status"] == "FAILED"
    assert "per-run budget" in str(attempt["reason"])
    storage = attempt["storage"]
    assert isinstance(storage, dict)
    assert [row["role"] for row in storage["stages"]] == ["ledger"]
    assert storage["stages"][0]["bytes_written"] >= PAYLOAD
    assert _state(output_root)["state"] == "FAILED"


def test_run_within_budget_records_bytes_written_per_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    preflight = _storage_preflight(tmp_path, cache)
    output_root = tmp_path / "out"
    _complete_by_receipt(monkeypatch)
    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", _writing_child(output_root))

    assert materializer.main(_argv(preflight, output_root)) == 0

    attempt = _observations(output_root)["execution_attempts"][-1]
    assert attempt["status"] == "PASSED"
    assert attempt["pid"] == os.getpid()
    storage = attempt["storage"]
    assert isinstance(storage, dict)
    stages = storage["stages"]
    assert [row["role"] for row in stages] == ["ledger", "baseline"]
    for row in stages:
        # The child's payload, plus the few receipt bytes the runner itself wrote.
        assert PAYLOAD <= row["bytes_written"] < PAYLOAD + 16 * 1024
    assert storage["end"]["growth_bytes"] >= sum(row["bytes_written"] for row in stages)
    assert storage["end"]["scratch_root"] == str(tmp_path.resolve())
    assert _state(output_root)["state"] == "PASSED"
    marker = run_storage.read_run_marker(output_root)
    assert marker is not None
    assert marker["budget"] == {
        "max_run_growth_bytes": 20 * 2**30,
        "max_scratch_bytes": 40 * 2**30,
    }


def test_run_growth_is_measured_from_creation_so_a_resume_cannot_reset_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    preflight = _storage_preflight(tmp_path, cache)
    output_root = tmp_path / "out"
    _complete_by_receipt(monkeypatch)
    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(
        materializer.subprocess, "run", _writing_child(output_root, returncode=9)
    )
    assert materializer.main(_argv(preflight, output_root)) == 9

    # The resume writes two payloads (~130 KiB): under 160 KiB if growth restarted at
    # the resume, over it once the failed attempt's preserved payload is counted.
    monkeypatch.setattr(materializer.subprocess, "run", _writing_child(output_root))
    resumed = _argv(preflight, output_root, "--resume", "--max-run-growth", "160KiB")
    assert materializer.main(resumed) == 1

    attempts = _observations(output_root)["execution_attempts"]
    assert [attempt["status"] for attempt in attempts] == ["FAILED", "FAILED"]
    assert "stage baseline" in str(attempts[-1]["reason"])
    assert "per-run budget" in str(attempts[-1]["reason"])
    storage = attempts[-1]["storage"]
    assert isinstance(storage, dict)
    assert storage["start"]["growth_bytes"] >= PAYLOAD


def test_scratch_budget_refuses_before_the_run_root_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "entry.bin").write_bytes(b"x" * 2048)
    preflight = _storage_preflight(tmp_path, cache)
    output_root = tmp_path / "out"
    calls: list[list[str]] = []
    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(
        materializer.subprocess, "run", _writing_child(output_root, calls=calls)
    )

    assert (
        materializer.main(_argv(preflight, output_root, "--max-scratch", "1KiB")) == 1
    )
    assert not output_root.exists()
    assert calls == []


def test_scratch_budget_measures_the_named_scratch_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "entry.bin").write_bytes(b"x" * 2048)
    preflight = _storage_preflight(tmp_path, cache)
    output_root = tmp_path / "out"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _complete_by_receipt(monkeypatch)
    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer.subprocess, "run", _writing_child(output_root))

    argv = _argv(
        preflight, output_root, "--max-scratch", "0", "--scratch-root", str(elsewhere)
    )
    assert materializer.main(argv) == 0


def test_an_unparseable_budget_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    preflight = _storage_preflight(tmp_path, cache)
    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    argv = _argv(preflight, tmp_path / "out", "--max-run-growth", "a lot")
    assert materializer.main(argv) == 1
    assert run_storage.parse_size("20GiB") == 20 * 2**30
    assert run_storage.parse_size("40GB") == 40 * 10**9
    assert run_storage.parse_size("1.5 MiB") == 3 * 2**19
    assert run_storage.parse_size("0") == 0


# --- 3. retained cache: shared base + per-run overlay ---------------------------------


def _correlation_candidate() -> tuple[GenerationManifest, bytes]:
    as_of = date(2026, 9, 17)
    result = canonical_json_bytes({"fixture": "attended-run-storage-correlation"})
    artifact = SourceArtifact(
        artifact_id="analytics/correlation.json",
        sha256=hashlib.sha256(result).hexdigest(),
        size_bytes=len(result),
        as_of_date=as_of,
        schema_name="portfolio-correlation",
        schema_version="2",
    )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("attended-run-storage", "1"),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="attended run storage fixture",
            fact_types=("correlation",),
            observed_start=as_of,
            observed_end=as_of,
        ),
        artifacts=(artifact,),
        verification=VerificationStatus.VERIFIED,
    )
    return manifest, result


def _seed_base(base: Path) -> tuple[GenerationManifest, bytes]:
    """A base holding what the real producers retain: fits and a staged candidate."""

    (base / "bulk").mkdir(parents=True)
    (base / "bulk" / "retained.bin").write_bytes(b"r" * MIB)
    decomposition._write_training_cache(base, "a" * 64, {"fit": [1, 2, 3]})
    manifest, result = _correlation_candidate()
    correlation._stage_candidate(base / "candidates", manifest, result)
    return manifest, result


def test_two_runs_share_the_base_and_their_writes_land_only_in_their_overlay(
    tmp_path: Path,
) -> None:
    base = tmp_path / "cache-base"
    manifest, result = _seed_base(base)
    before = run_storage.cache_inventory(base)
    run_a, run_b = tmp_path / "run-a", tmp_path / "run-b"
    overlay_a, overlay_b = tmp_path / "overlay-a", tmp_path / "overlay-b"
    run_storage.create_cache_overlay(base, overlay_a, run_root=run_a)
    run_storage.create_cache_overlay(base, overlay_b, run_root=run_b)

    # Every base file is one file on disk, named three times: no byte was copied.
    base_files = [path for path in base.rglob("*") if path.is_file()]
    assert base_files
    for path in base_files:
        relative = path.relative_to(base)
        identities = {
            (os.stat(root / relative).st_dev, os.stat(root / relative).st_ino)
            for root in (base, overlay_a, overlay_b)
        }
        assert len(identities) == 1
        assert os.stat(path).st_nlink == 3
    assert run_storage.measure([overlay_a, overlay_b], exclude=before["inodes"])[
        "bytes"
    ] == len((overlay_a / run_storage.OVERLAY_MARKER).read_bytes()) + len(
        (overlay_b / run_storage.OVERLAY_MARKER).read_bytes()
    )

    # The real producers reuse base entries through the overlay without writing...
    assert decomposition._read_training_cache(overlay_a, "a" * 64) is not None
    decomposition._write_training_cache(overlay_a, "a" * 64, {"fit": [1, 2, 3]})
    staged = correlation._stage_candidate(overlay_a / "candidates", manifest, result)
    assert staged == overlay_a / "candidates" / manifest.generation_id
    # ...and a new entry lands in this run's overlay alone.
    decomposition._write_training_cache(overlay_a, "b" * 64, {"fit": [4]})
    new_entry = overlay_a / ".decomposition-training" / f"{'b' * 64}.json"
    assert new_entry.is_file()
    assert not (base / ".decomposition-training" / f"{'b' * 64}.json").exists()
    assert not (overlay_b / ".decomposition-training" / f"{'b' * 64}.json").exists()

    assert run_storage.cache_inventory(base)["sha256"] == before["sha256"]
    run_storage.verify_cache_overlay(base, overlay_a, run_root=run_a)
    run_storage.verify_cache_overlay(base, overlay_b, run_root=run_b)
    grown = run_storage.measure([overlay_a], exclude=before["inodes"])["bytes"]
    assert 0 < grown < MIB


def test_a_write_through_an_overlay_link_is_refused_and_the_base_is_unchanged(
    tmp_path: Path,
) -> None:
    base = tmp_path / "cache-base"
    _seed_base(base)
    before = run_storage.cache_inventory(base)
    overlay = tmp_path / "overlay"
    run_storage.create_cache_overlay(base, overlay, run_root=tmp_path / "run")

    with pytest.raises(PermissionError):
        with open(overlay / "bulk" / "retained.bin", "r+b") as handle:
            handle.write(b"tampered")

    assert run_storage.cache_inventory(base)["sha256"] == before["sha256"]
    assert (base / "bulk" / "retained.bin").read_bytes() == b"r" * MIB


@pytest.mark.parametrize("change", ("rewrite", "add"))
def test_a_base_changed_after_the_overlay_was_created_is_refused(
    tmp_path: Path, change: str
) -> None:
    base = tmp_path / "cache-base"
    _seed_base(base)
    overlay = tmp_path / "overlay"
    run_root = tmp_path / "run"
    run_storage.create_cache_overlay(base, overlay, run_root=run_root)
    if change == "rewrite":
        target = base / "bulk" / "retained.bin"
        os.chmod(target, 0o644)
        target.write_bytes(b"s" * (MIB + 1))
    else:
        (base / "bulk" / "late.bin").write_bytes(b"late")

    with pytest.raises(run_storage.RunStorageError, match="changed since"):
        run_storage.verify_cache_overlay(base, overlay, run_root=run_root)


def test_an_overlay_is_bound_to_the_run_root_that_created_it(tmp_path: Path) -> None:
    base = tmp_path / "cache-base"
    _seed_base(base)
    overlay = tmp_path / "overlay"
    run_storage.create_cache_overlay(base, overlay, run_root=tmp_path / "run-a")

    with pytest.raises(run_storage.RunStorageError, match="different run root"):
        run_storage.verify_cache_overlay(base, overlay, run_root=tmp_path / "run-b")


def test_an_occupied_unmarked_overlay_root_is_refused(tmp_path: Path) -> None:
    base = tmp_path / "cache-base"
    _seed_base(base)
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "copied.bin").write_bytes(b"a previous cp -r")

    with pytest.raises(run_storage.RunStorageError, match="already holds files"):
        run_storage.create_cache_overlay(base, overlay, run_root=tmp_path / "run")


def test_an_overlay_that_cannot_be_linked_is_refused_rather_than_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "cache-base"
    _seed_base(base)
    overlay = tmp_path / "overlay"

    def no_links(_source: object, _target: object) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", no_links)
    with pytest.raises(run_storage.RunStorageError, match="never copies"):
        run_storage.create_cache_overlay(base, overlay, run_root=tmp_path / "run")

    assert not overlay.exists()
    assert not list(tmp_path.glob(".overlay*"))


def test_attended_builds_the_overlay_from_the_base_before_any_child_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "cache-base"
    _seed_base(base)
    before = run_storage.cache_inventory(base)
    overlay = tmp_path / "overlay"
    preflight = _storage_preflight(tmp_path, overlay)
    output_root = tmp_path / "out"
    seen: list[Path] = []

    def producer(argv: list[str]) -> None:
        cache = Path(argv[argv.index("--retained-cache-root") + 1])
        marker = run_storage.read_overlay_marker(cache)
        assert marker is not None and marker["run_root"] == str(output_root)
        seen.append(cache)
        role = argv[argv.index("--role") + 1]
        decomposition._write_training_cache(cache, role[0] * 64, {"fit": [role]})

    _complete_by_receipt(monkeypatch)
    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(
        materializer.subprocess, "run", _writing_child(output_root, also=producer)
    )
    argv = _argv(preflight, output_root, "--retained-cache-base", str(base))

    assert materializer.main(argv) == 0

    assert seen == [overlay, overlay]
    assert run_storage.cache_inventory(base)["sha256"] == before["sha256"]
    storage = _observations(output_root)["execution_attempts"][-1]["storage"]
    assert isinstance(storage, dict)
    assert storage["cache"]["base_inventory_sha256"] == before["sha256"]
    # The 1 MiB the overlay names through links is not growth; the new fits are.
    assert storage["end"]["growth_bytes"] < MIB
    marker = run_storage.read_run_marker(output_root)
    assert marker is not None
    assert marker["retained_cache_base"] == str(base.resolve())
    assert marker["retained_cache_root"] == str(overlay.resolve())


def test_attended_resume_refuses_a_base_that_changed_under_its_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "cache-base"
    _seed_base(base)
    overlay = tmp_path / "overlay"
    preflight = _storage_preflight(tmp_path, overlay)
    output_root = tmp_path / "out"
    calls: list[list[str]] = []
    _complete_by_receipt(monkeypatch)
    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(
        materializer.subprocess,
        "run",
        _writing_child(output_root, returncode=5, calls=calls),
    )
    argv = _argv(preflight, output_root, "--retained-cache-base", str(base))
    assert materializer.main(argv) == 5
    (base / "bulk" / "late.bin").write_bytes(b"late")

    assert materializer.main([*argv, "--resume"]) == 1
    assert len(calls) == 1
    attempt = _observations(output_root)["execution_attempts"][-1]
    assert attempt["status"] == "FAILED"
    assert "changed since" in str(attempt["reason"])


def test_request_identity_binds_a_base_only_when_one_is_used(tmp_path: Path) -> None:
    source_run = tmp_path / "source-run.json"
    source_run.write_text("{}", encoding="utf-8")
    common: dict[str, object] = {
        "repo_root": tmp_path,
        "python": Path(sys.executable),
        "workspace_root": tmp_path / "workspace",
        "output_root": tmp_path / "out",
        "retained_cache_root": tmp_path / "cache",
        "requested_as_of": "2026-09-17",
        "evaluated_on": "2026-09-19",
        "source_run": source_run,
        "financial_stage_inputs": {},
        "source_phase": {"status": "PASS"},
        "runtime": {"identity": "fixture"},
    }
    plain = materializer._attended_request_identity(**common)
    shared = materializer._attended_request_identity(
        **common, retained_cache_base=tmp_path / "cache-base"
    )

    assert "retained_cache_base" not in plain
    assert shared == {**plain, "retained_cache_base": str(tmp_path / "cache-base")}


# --- 4. run-root lifecycle ------------------------------------------------------------


def _marked_run(
    runs_root: Path,
    name: str,
    created_at: str,
    state: str | None,
    *,
    pid: int = 2**22 + 7,
    started: str | None = None,
    overlay: Path | None = None,
    base: Path | None = None,
) -> Path:
    root = runs_root / name
    (root / "stages").mkdir(parents=True)
    (root / "stages" / "payload.bin").write_bytes(b"p" * 1000)
    marker = {
        "budget": {"max_run_growth_bytes": 1, "max_scratch_bytes": 1},
        "created_at": created_at,
        "created_by_pid": pid,
        "kind": run_storage.RUN_MARKER_KIND,
        "retained_cache_base": None if base is None else str(base),
        "retained_cache_root": str(overlay or runs_root / "shared-cache"),
        "run_root": str(root),
        "schema_version": 1,
        "scratch_root": str(runs_root),
        "storage_baseline": {"bytes": 0, "files": 0},
    }
    (root / run_storage.RUN_MARKER).write_bytes(canonical_json_bytes(marker))
    if state is not None:
        (root / run_storage.RUN_STATE).write_bytes(
            canonical_json_bytes(
                {
                    "attempt": 1,
                    "phase": "execution",
                    "pid": pid,
                    "process_started": started,
                    "reason": None,
                    "schema_version": 1,
                    "state": state,
                    "updated_at": created_at,
                }
            )
        )
    return root


def _runs_fixture(tmp_path: Path) -> dict[str, Path]:
    runs = (tmp_path / "runs").resolve()
    runs.mkdir()
    base = tmp_path / "cache-base"
    _seed_base(base)
    (runs / "shared-cache").mkdir()
    (runs / "shared-cache" / "entry.bin").write_bytes(b"shared")
    nested_overlay = runs / "nested-cache"
    nested = runs / "nested" / "out"
    rows = {
        "old": _marked_run(runs, "old", "2026-09-01T00:00:00+00:00", "PASSED"),
        "failed": _marked_run(runs, "failed", "2026-09-02T00:00:00+00:00", "FAILED"),
        "nested": _marked_run(
            runs / "nested",
            "out",
            "2026-09-02T12:00:00+00:00",
            "FAILED",
            overlay=nested_overlay,
            base=base.resolve(),
        ),
        "newest": _marked_run(runs, "newest", "2026-09-03T00:00:00+00:00", "PASSED"),
    }
    assert rows["nested"] == nested
    run_storage.create_cache_overlay(base, nested_overlay, run_root=nested)
    (runs / "manual").mkdir()
    (runs / "manual" / "operator-notes.txt").write_text("not the runner's")
    moved = _marked_run(runs, "moved", "2026-08-01T00:00:00+00:00", "PASSED")
    marker = json.loads((moved / run_storage.RUN_MARKER).read_text("utf-8"))
    marker["run_root"] = str(runs / "elsewhere")
    (moved / run_storage.RUN_MARKER).write_bytes(canonical_json_bytes(marker))
    return {
        **rows,
        "runs": runs,
        "base": base,
        "nested_overlay": nested_overlay,
        "manual": runs / "manual",
        "moved": moved,
        "shared": runs / "shared-cache",
    }


def test_retire_is_a_dry_run_by_default(tmp_path: Path) -> None:
    fixture = _runs_fixture(tmp_path)

    report = run_storage.retire_run_roots(fixture["runs"], keep=1)

    assert report["applied"] is False
    assert [row["root"] for row in report["kept"]] == [str(fixture["newest"])]
    assert report["kept"][0]["reasons"] == [
        "current candidate",
        "one of the 1 most recent",
    ]
    assert sorted(row["root"] for row in report["retired"]) == sorted(
        str(fixture[name]) for name in ("old", "failed", "nested")
    )
    nested = next(
        row for row in report["retired"] if row["root"] == str(fixture["nested"])
    )
    assert nested["overlay"] == str(fixture["nested_overlay"])
    assert [row["root"] for row in report["skipped"]] == [str(fixture["moved"])]
    for name in ("old", "failed", "nested", "nested_overlay", "manual", "moved"):
        assert fixture[name].exists()


def test_retire_apply_removes_only_marked_superseded_roots_and_their_overlays(
    tmp_path: Path,
) -> None:
    fixture = _runs_fixture(tmp_path)
    base_before = run_storage.cache_inventory(fixture["base"])

    report = materializer.main(
        ["retire", "--runs-root", str(fixture["runs"]), "--keep", "1", "--apply"]
    )

    assert report == 0
    for name in ("old", "failed", "nested", "nested_overlay"):
        assert not fixture[name].exists(), name
    for name in ("newest", "manual", "moved", "shared", "base"):
        assert fixture[name].exists(), name
    assert (
        run_storage.cache_inventory(fixture["base"])["sha256"] == base_before["sha256"]
    )
    # The base stays sealed after its overlay links were unlinked.
    assert not os.access(fixture["base"] / "bulk" / "retained.bin", os.W_OK)


def test_retire_keeps_a_live_run_and_a_base_a_kept_run_depends_on(
    tmp_path: Path,
) -> None:
    runs = (tmp_path / "runs").resolve()
    runs.mkdir()
    base = tmp_path / "cache-base"
    _seed_base(base)
    child = subprocess.Popen([BASE_PYTHON, "-c", "import time; time.sleep(60)"])
    try:
        live = _marked_run(
            runs,
            "live",
            "2026-09-01T00:00:00+00:00",
            "RUNNING",
            pid=child.pid,
            started=run_storage.process_started(child.pid),
        )
        feeder_overlay = runs / "feeder-cache"
        feeder = _marked_run(
            runs,
            "feeder",
            "2026-09-02T00:00:00+00:00",
            "PASSED",
            overlay=feeder_overlay,
            base=base.resolve(),
        )
        run_storage.create_cache_overlay(base, feeder_overlay, run_root=feeder)
        newest = _marked_run(
            runs,
            "newest",
            "2026-09-03T00:00:00+00:00",
            "PASSED",
            base=feeder_overlay,
        )
        stale = _marked_run(runs, "stale", "2026-08-01T00:00:00+00:00", "FAILED")

        report = run_storage.retire_run_roots(runs, keep=1, apply=True)
    finally:
        child.kill()
        child.wait()

    kept = {row["root"]: row["reasons"] for row in report["kept"]}
    assert kept[str(live)] == ["its runner process is still alive"]
    assert kept[str(feeder)] == ["a kept run uses it as its retained-cache base"]
    assert str(newest) in kept
    assert [row["root"] for row in report["retired"]] == [str(stale)]
    assert live.exists() and feeder.exists() and feeder_overlay.exists()
    assert not stale.exists()


def test_retire_keeps_an_explicit_current_candidate(tmp_path: Path) -> None:
    fixture = _runs_fixture(tmp_path)

    report = run_storage.retire_run_roots(
        fixture["runs"], keep=0, current=fixture["failed"]
    )

    assert [row["root"] for row in report["kept"]] == [str(fixture["failed"])]
    with pytest.raises(run_storage.RunStorageError, match="negative"):
        run_storage.retire_run_roots(fixture["runs"], keep=-1)


# --- 5. run states and the PID-keyed waiter -------------------------------------------


def test_a_created_run_root_is_queued_under_the_creating_pid(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    storage = run_storage.AttendedStorage(
        output_root=root,
        cache_root=tmp_path / "cache",
        base=None,
        scratch_root=tmp_path,
        budget=run_storage.StorageBudget.parse("20GiB", "40GiB"),
    )

    storage.mark_created(root)
    queued = _state(root)
    assert (queued["state"], queued["pid"]) == ("QUEUED", os.getpid())
    assert queued["process_started"] == run_storage.process_started(os.getpid())

    attempt = materializer._attended_start_preparation_attempt(root)
    assert _state(root)["state"] == "RUNNING"
    materializer._attended_finish_preparation_attempt(root, attempt, "PASSED")
    # A passed preparation hands over to execution: the run is still RUNNING.
    assert (_state(root)["state"], _state(root)["phase"]) == ("RUNNING", "preparation")
    attempt = materializer._attended_start_execution_attempt(root)
    materializer._attended_finish_execution_attempt(root, attempt, "PASSED")
    assert _state(root)["state"] == "PASSED"
    with pytest.raises(materializer.MaterializationError, match="status differs"):
        materializer._attended_finish_execution_attempt(root, attempt, "COMPLETED")


def test_legacy_attempt_receipts_map_onto_explicit_run_states(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    receipt = root / "attended-execution-observations.json"
    receipt.write_bytes(
        canonical_json_bytes(
            {
                "execution_attempts": [
                    {
                        "attempt": 1,
                        "ended_at": "2026-09-19T12:00:01+00:00",
                        "started_at": "2026-09-19T12:00:00+00:00",
                        "status": "FAILED",
                    },
                    {
                        "attempt": 2,
                        "started_at": "2026-09-19T12:01:00+00:00",
                        "status": "INCOMPLETE",
                    },
                ],
                "preparation_attempts": [
                    {
                        "attempt": 1,
                        "ended_at": "2026-09-19T11:00:01+00:00",
                        "started_at": "2026-09-19T11:00:00+00:00",
                        "status": "COMPLETED",
                    }
                ],
                "schema_version": 2,
            }
        )
    )

    read = materializer._attended_execution_observations(root)
    assert [a["status"] for a in read["execution_attempts"]] == ["FAILED", "CANCELLED"]
    assert [a["status"] for a in read["preparation_attempts"]] == ["PASSED"]
    assert {a["pid"] for a in read["execution_attempts"]} == {None}

    materializer._attended_start_execution_attempt(root)
    rewritten = _observations(root)["execution_attempts"]
    assert [a["status"] for a in rewritten] == ["FAILED", "CANCELLED", "RUNNING"]
    assert rewritten[-1]["pid"] == os.getpid()


@pytest.mark.parametrize(
    "attempt",
    (
        {"attempt": 1, "started_at": "2026-09-19T12:00:00+00:00", "status": "RUNNING"},
        {
            "attempt": 1,
            "ended_at": "2026-09-19T12:00:01+00:00",
            "pid": 7,
            "started_at": "2026-09-19T12:00:00+00:00",
            "status": "COMPLETED",
        },
        {
            "attempt": 1,
            "pid": 7,
            "started_at": "2026-09-19T12:00:00+00:00",
            "status": "PASSED",
        },
    ),
)
def test_current_attempt_receipts_require_a_pid_and_an_explicit_state(
    tmp_path: Path, attempt: dict[str, object]
) -> None:
    root = tmp_path / "out"
    root.mkdir()
    (root / "attended-execution-observations.json").write_bytes(
        canonical_json_bytes(
            {
                "execution_attempts": [attempt],
                "preparation_attempts": [],
                "schema_version": 3,
            }
        )
    )
    with pytest.raises(materializer.MaterializationError, match="observations differ"):
        materializer._attended_execution_observations(root)


def test_an_interrupted_execution_leaves_the_run_cancelled_and_the_attempt_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    preflight = _storage_preflight(tmp_path, cache)
    output_root = tmp_path / "out"

    def interrupted(_argv: list[str], **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(materializer.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        materializer._attended_execute(preflight, output_root, resume=False)

    assert _state(output_root)["state"] == "CANCELLED"
    assert _observations(output_root)["execution_attempts"][-1]["status"] == "RUNNING"


def test_a_resume_that_fails_validation_leaves_its_claimed_state_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    preflight = _storage_preflight(tmp_path, cache)
    output_root = tmp_path / "out"
    output_root.mkdir()
    (output_root / "attended-run.json").write_bytes(
        canonical_json_bytes(
            {"request_identity": {"fixture": "another run"}, "schema_version": 1}
        )
    )
    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)

    assert materializer.main(_argv(preflight, output_root, "--resume")) == 1
    state = _state(output_root)
    assert (state["state"], state["pid"]) == ("FAILED", os.getpid())
    assert "resume identity differs" in str(state["reason"])


def _recorded_state(root: Path, pid: int, started: str | None, state: str) -> None:
    (root / run_storage.RUN_STATE).write_bytes(
        canonical_json_bytes(
            {
                "attempt": 1,
                "phase": "execution",
                "pid": pid,
                "process_started": started,
                "reason": None,
                "schema_version": 1,
                "state": state,
                "updated_at": "2026-09-19T12:00:00+00:00",
            }
        )
    )


def test_a_resume_cannot_claim_a_root_whose_runner_is_still_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    preflight = _storage_preflight(tmp_path, cache)
    output_root = tmp_path / "out"
    output_root.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(
        materializer.subprocess, "run", _writing_child(output_root, calls=calls)
    )
    child = subprocess.Popen([BASE_PYTHON, "-c", "import time; time.sleep(60)"])
    try:
        started = run_storage.process_started(child.pid)
        _recorded_state(output_root, child.pid, started, "RUNNING")
        code = materializer.main(_argv(preflight, output_root, "--resume"))
    finally:
        child.kill()
        child.wait()

    assert code == 1
    assert calls == []
    state = _state(output_root)
    assert (state["pid"], state["state"]) == (child.pid, "RUNNING")


def test_a_resume_may_claim_a_root_whose_runner_has_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    preflight = _storage_preflight(tmp_path, cache)
    output_root = tmp_path / "out"
    output_root.mkdir()
    child = subprocess.Popen([BASE_PYTHON, "-c", "pass"])
    child.wait()
    _recorded_state(output_root, child.pid, "1", "RUNNING")
    claimed: list[str] = []

    def start_or_resume(*_args: object, **_kwargs: object) -> None:
        claimed.append(str(_state(output_root)["state"]))
        raise materializer.MaterializationError("stop after the claim")

    monkeypatch.setattr(materializer, "_attended_preflight", lambda _args: preflight)
    monkeypatch.setattr(materializer, "_attended_start_or_resume", start_or_resume)

    assert materializer.main(_argv(preflight, output_root, "--resume")) == 1
    assert claimed == ["QUEUED"]
    state = _state(output_root)
    assert (state["pid"], state["state"]) == (os.getpid(), "FAILED")


def test_the_controller_graph_maps_the_storage_module_to_every_stage() -> None:
    from portfolio_controller_graph import affected_stages, build_controller_graph

    graph = build_controller_graph(REPOSITORY_ROOT)
    assert affected_stages(
        graph, ["packages/core/src/quant_engine/core/run_storage.py"]
    ) == ["ledger", "baseline", "attribution", "analytics", "report", "panel"]


def test_wait_refuses_a_state_that_names_its_own_process(tmp_path: Path) -> None:
    run_storage.write_run_state(tmp_path, "RUNNING")

    with pytest.raises(run_storage.RunStorageError, match="own process"):
        run_storage.wait_for_run(tmp_path, poll=0.01)
    assert materializer.main(["wait", str(tmp_path), "--poll", "0.01"]) == 1


_CHILD = """
import sys, time
from pathlib import Path
from quant_engine.core import run_storage
root = Path(sys.argv[1])
run_storage.write_run_state(root, "RUNNING")
time.sleep(0.5)
if sys.argv[2] != "none":
    run_storage.write_run_state(root, sys.argv[2])
time.sleep(float(sys.argv[3]))
"""


def _spawn_run(root: Path, outcome: str, linger: float) -> subprocess.Popen[bytes]:
    child = subprocess.Popen(
        [BASE_PYTHON, "-c", _CHILD, str(root), outcome, str(linger)],
        env=CHILD_ENVIRONMENT,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            state = run_storage.read_run_state(root)
        except run_storage.RunStorageError:
            state = None
        if state is not None and state["pid"] == child.pid:
            return child
        time.sleep(0.02)
    child.kill()
    raise AssertionError("the child run never recorded its state")


def test_wait_returns_only_after_the_recorded_process_exits(tmp_path: Path) -> None:
    child = _spawn_run(tmp_path, "PASSED", 1.0)
    try:
        result = run_storage.wait_for_run(tmp_path, poll=0.02, timeout=60)
        # PASSED was recorded a full second before the process ended; a waiter
        # that trusted the file alone would have returned while it still ran.
        assert child.poll() is not None
    finally:
        child.kill()
        child.wait()
    assert result["state"] == "PASSED"
    assert result["pid"] == child.pid
    assert result["timed_out"] is False


def test_wait_reports_cancelled_when_the_process_ended_without_an_outcome(
    tmp_path: Path,
) -> None:
    child = _spawn_run(tmp_path, "none", 0.0)
    try:
        result = run_storage.wait_for_run(tmp_path, poll=0.02, timeout=60)
    finally:
        child.wait()
    assert (result["state"], result["recorded_state"]) == ("CANCELLED", "RUNNING")
    assert materializer.main(["wait", str(tmp_path), "--poll", "0.01"]) == 2


def test_wait_does_not_mistake_a_reused_pid_for_the_run(tmp_path: Path) -> None:
    child = subprocess.Popen([BASE_PYTHON, "-c", "import time; time.sleep(60)"])
    try:
        (tmp_path / run_storage.RUN_STATE).write_bytes(
            canonical_json_bytes(
                {
                    "attempt": 1,
                    "phase": "execution",
                    "pid": child.pid,
                    "process_started": "1",
                    "reason": None,
                    "schema_version": 1,
                    "state": "RUNNING",
                    "updated_at": "2026-09-19T12:00:00+00:00",
                }
            )
        )
        started = time.monotonic()
        result = run_storage.wait_for_run(tmp_path, poll=0.02, timeout=30)
        assert time.monotonic() - started < 10
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()
    assert result["state"] == "CANCELLED"


def test_wait_times_out_while_the_run_is_still_alive(tmp_path: Path) -> None:
    child = _spawn_run(tmp_path, "none", 60.0)
    try:
        result = run_storage.wait_for_run(tmp_path, poll=0.02, timeout=0.2)
        code = materializer.main(
            ["wait", str(tmp_path), "--timeout", "0.2", "--poll", "0.02"]
        )
    finally:
        child.kill()
        child.wait()
    assert result["timed_out"] is True and result["state"] == "RUNNING"
    assert code == materializer.WAIT_TIMEOUT_EXIT_CODE
