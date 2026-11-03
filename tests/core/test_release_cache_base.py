from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

from quant_engine.core import retained_cache_base as core_base

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "release_cache_base_tool", ROOT / "tools" / "release_cache_base.py"
)
assert _SPEC is not None and _SPEC.loader is not None
cb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cb)

COMMIT = "a" * 40
MARKER = "retained-cache-overlay.json"


def _overlay(tmp: Path) -> Path:
    overlay = tmp / "cache21"
    for rel, text in {
        ".decomposition-index/k1.json": "1",
        ".decomposition-training/m1.bin": "22",
        "financial-replay-receipts/r1.json": "333",
        MARKER: json.dumps({"run_root": str(tmp / "run")}),
    }.items():
        path = overlay / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return overlay


def _run(tmp: Path, state: str = "PASSED", commit: str = COMMIT) -> Path:
    run = tmp / "run"
    (run / "preparation").mkdir(parents=True, exist_ok=True)
    (run / "attended-run-state.json").write_text(
        json.dumps({"state": state}), encoding="utf-8"
    )
    (run / "preparation" / "release-recipe.json").write_text(
        json.dumps({"release_commit": commit}), encoding="utf-8"
    )
    return run


def test_promote_links_without_copy_seals_and_records_provenance(
    tmp_path: Path,
) -> None:
    overlay = _overlay(tmp_path)
    record = core_base.promote_cache_base(overlay, tmp_path / "base", COMMIT)
    linked = tmp_path / "base" / ".decomposition-index" / "k1.json"
    assert os.path.samestat(
        linked.stat(), (overlay / ".decomposition-index" / "k1.json").stat()
    )
    assert not (tmp_path / "base" / MARKER).exists()
    assert not linked.stat().st_mode & stat.S_IWUSR
    assert record["manifest"]["files"] == 3 and record["source_commit"] == COMMIT
    assert core_base.verify_cache_base(tmp_path / "base")["source_commit"] == COMMIT


def test_promote_refuses_non_overlay_existing_base_and_bad_commit(
    tmp_path: Path,
) -> None:
    overlay = _overlay(tmp_path)
    core_base.promote_cache_base(overlay, tmp_path / "base", COMMIT)
    with pytest.raises(core_base.CacheBaseError, match="already exists"):
        core_base.promote_cache_base(overlay, tmp_path / "base", COMMIT)
    for bad in ("abc", "Z" * 40):
        with pytest.raises(core_base.CacheBaseError, match="40-hex"):
            core_base.promote_cache_base(overlay, tmp_path / "b2", bad)
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(core_base.CacheBaseError, match="not a retained-cache overlay"):
        core_base.promote_cache_base(plain, tmp_path / "b3", COMMIT)


def test_interrupted_promotion_is_retried_by_running_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = _overlay(tmp_path)
    real_replace = os.replace

    def boom(src: str, dst: str) -> None:
        raise OSError("simulated stop before publish")

    monkeypatch.setattr(core_base.os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        core_base.promote_cache_base(overlay, tmp_path / "base", COMMIT)
    monkeypatch.setattr(core_base.os, "replace", real_replace)
    assert not (tmp_path / "base").exists()
    assert not list(tmp_path.glob(".base.building-*"))
    core_base.promote_cache_base(overlay, tmp_path / "base", COMMIT)
    assert core_base.verify_cache_base(tmp_path / "base")["source_commit"] == COMMIT


def test_verify_detects_a_changed_byte_and_missing_provenance(tmp_path: Path) -> None:
    core_base.promote_cache_base(_overlay(tmp_path), tmp_path / "base", COMMIT)
    victim = tmp_path / "base" / "financial-replay-receipts" / "r1.json"
    os.chmod(victim, stat.S_IWUSR | stat.S_IRUSR)
    victim.write_text("444", encoding="utf-8")
    with pytest.raises(core_base.CacheBaseError, match="differ"):
        core_base.verify_cache_base(tmp_path / "base")
    with pytest.raises(core_base.CacheBaseError, match="differ"):
        core_base.verify_if_promoted(tmp_path / "base")
    (tmp_path / "plain").mkdir()
    assert core_base.verify_if_promoted(tmp_path / "plain") is None
    with pytest.raises(core_base.CacheBaseError, match="provenance"):
        core_base.verify_cache_base(tmp_path / "plain")


def test_source_commit_comes_from_the_passed_run_not_from_the_operator(
    tmp_path: Path,
) -> None:
    overlay = _overlay(tmp_path)
    assert cb.source_commit_of(_run(tmp_path), overlay) == COMMIT
    with pytest.raises(core_base.CacheBaseError, match="not PASSED"):
        cb.source_commit_of(_run(tmp_path, state="FAILED"), overlay)
    other = tmp_path / "other-run"
    other.mkdir()
    (other / "attended-run-state.json").write_text(
        json.dumps({"state": "PASSED"}), encoding="utf-8"
    )
    with pytest.raises(core_base.CacheBaseError, match="different run root"):
        cb.source_commit_of(other, overlay)
    with pytest.raises(core_base.CacheBaseError, match="40-hex"):
        cb.source_commit_of(_run(tmp_path, commit="short"), overlay)


def _next_overlay(tmp: Path, new: dict[str, int]) -> Path:
    nxt = tmp / "cache22"
    base = tmp / "base"
    for src in base.rglob("*"):
        if src.is_file():
            dst = nxt / src.relative_to(base)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.link(src, dst)
    for kind, count in new.items():
        for i in range(count):
            path = nxt / kind / f"new{i}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("n", encoding="utf-8")
    (nxt / MARKER).write_text("{}", encoding="utf-8")
    return nxt


def test_reuse_report_counts_inherited_and_new(tmp_path: Path) -> None:
    core_base.promote_cache_base(_overlay(tmp_path), tmp_path / "base", COMMIT)
    result = cb.reuse_report(
        tmp_path / "base", _next_overlay(tmp_path, {".decomposition-index": 2})
    )
    assert result["kinds"][".decomposition-index"] == {"inherited": 1, "new": 2}
    assert result["kinds"]["financial-replay-receipts"] == {"inherited": 1, "new": 0}


@pytest.mark.parametrize(
    ("stages", "new", "code"),
    [
        (["analytics"], {}, cb.EXIT_SUSPICIOUS_REUSE),
        (["analytics"], {".decomposition-index": 1}, 0),
        # a changed financial producer recomputes replay receipts: that is a miss, not suspicious
        (["analytics"], {"financial-replay-receipts": 1}, 0),
        ([], {}, 0),
        (["report"], {}, 0),
    ],
)
def test_producer_change_with_nothing_recomputed_is_suspicious(
    tmp_path: Path, stages: list[str], new: dict[str, int], code: int
) -> None:
    core_base.promote_cache_base(_overlay(tmp_path), tmp_path / "base", COMMIT)
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"affected_stages": stages}), encoding="utf-8")
    result, rc = cb.report(
        tmp_path / "base", _next_overlay(tmp_path, new), graph, "b" * 40
    )
    assert rc == code
    assert bool(result["suspicious_reuse"]) == (code != 0)


RUN_MARKER = "attended-run-root.json"


def _cold_run(tmp: Path, *, base: str | None = None, cache: Path | None = None) -> Path:
    """A PASSED run that started with no base: a plain cache root, no overlay marker."""

    run = _run(tmp)
    cache = cache or tmp / "cold-cache"
    (run / RUN_MARKER).write_text(
        json.dumps(
            {
                "kind": "quant-engine-attended-run-root",
                "retained_cache_base": base,
                "retained_cache_root": str(cache),
                "run_root": str(run),
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    for rel, text in {
        ".decomposition-index/k1.json": "1",
        ".decomposition-training/m1.bin": "22",
        "candidates/c1/generation.json": "4444",
        "financial-replay-receipts/r1.json": "333",
        # per-run state that sits beside the caches in a cold run's root
        "pipelines/run-a/receipt.json": "state",
        "financial-verification-ledger/led.json": "ledger",
    }.items():
        path = cache / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return run


def _promote(tmp: Path, run: Path, base: Path) -> int:
    return cb.main(
        [
            "promote",
            "--overlay",
            str(tmp / "cold-cache"),
            "--base",
            str(base),
            "--run-root",
            str(run),
        ]
    )


def test_a_cold_runs_cache_seals_into_a_base_the_next_run_uses_warm(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from quant_engine.core import run_storage

    run = _cold_run(tmp_path)
    cold = tmp_path / "cold-cache"
    before = {
        rel: (cold / rel).read_bytes()
        for rel in (
            ".decomposition-index/k1.json",
            "candidates/c1/generation.json",
            "financial-replay-receipts/r1.json",
        )
    }
    base = tmp_path / "base"
    assert _promote(tmp_path, run, base) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["source_commit"] == COMMIT
    assert set(out["cold_run_kinds_sealed"]) == set(core_base.COLD_CACHE_KINDS)
    assert out["manifest"]["files"] == 4
    # the run's own state and ledger are never sealed into the base
    assert not (base / "pipelines").exists()
    assert not (base / "financial-verification-ledger").exists()
    assert core_base.verify_cache_base(base)["source_commit"] == COMMIT
    # the next run starts warm from it: hard links, same bytes, nothing recomputed
    overlay = tmp_path / "warm-cache"
    run2 = tmp_path / "run2"
    run2.mkdir()
    run_storage.create_cache_overlay(base, overlay, run_root=run2)
    for rel, content in before.items():
        assert (overlay / rel).read_bytes() == content
        assert os.path.samestat((overlay / rel).stat(), (base / rel).stat())
    report = cb.reuse_report(base, overlay)
    assert report["kinds"]
    assert all(
        row["new"] == 0 and row["inherited"] > 0 for row in report["kinds"].values()
    )
    assert core_base.verify_if_promoted(base) is not None


def test_a_broken_or_foreign_cold_root_is_refused(tmp_path: Path) -> None:
    run = _cold_run(tmp_path)
    cold = tmp_path / "cold-cache"
    # an overlay marker that exists but cannot be read is still a refusal
    (cold / MARKER).write_text("{not json", encoding="utf-8")
    with pytest.raises(core_base.CacheBaseError, match="overlay marker is unreadable"):
        cb.source_commit_of(run, cold)
    (cold / MARKER).unlink()
    # the run marker must name this root
    with pytest.raises(core_base.CacheBaseError, match="not this run's cold"):
        cb.source_commit_of(run, tmp_path / "elsewhere")
    # a run that was seeded from a base must have left an overlay marker
    seeded = _cold_run(tmp_path / "s", base="/some/base", cache=tmp_path / "s" / "c")
    with pytest.raises(core_base.CacheBaseError, match="seeded from a base"):
        cb.source_commit_of(seeded, tmp_path / "s" / "c")
    # no run marker at all
    (run / RUN_MARKER).unlink()
    with pytest.raises(core_base.CacheBaseError, match="run root marker is unreadable"):
        cb.source_commit_of(run, cold)


def test_a_cold_root_with_no_cache_entries_is_refused(tmp_path: Path) -> None:
    run = _cold_run(tmp_path)
    cold = tmp_path / "cold-cache"
    for kind in core_base.COLD_CACHE_KINDS:
        if (cold / kind).exists():
            for item in sorted((cold / kind).rglob("*"), reverse=True):
                item.unlink() if item.is_file() else item.rmdir()
            (cold / kind).rmdir()
    with pytest.raises(core_base.CacheBaseError, match="holds no cache entries"):
        core_base.promote_cache_base(
            cold, tmp_path / "b", COMMIT, cold_kinds=core_base.COLD_CACHE_KINDS
        )
    assert not (tmp_path / "b").exists()
    assert _promote(tmp_path, run, tmp_path / "b2") == 1
