from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "release_cache_base_tool", ROOT / "tools" / "release_cache_base.py"
)
assert _SPEC is not None and _SPEC.loader is not None
cb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cb)

COMMIT = "a" * 40


def _overlay(tmp: Path) -> Path:
    overlay = tmp / "cache21"
    for rel, text in {
        ".decomposition-index/k1.json": "1",
        ".decomposition-training/m1.bin": "22",
        "financial-replay-receipts/r1.json": "333",
        cb.OVERLAY_MARKER: "{}",
    }.items():
        path = overlay / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return overlay


def test_promote_links_without_copy_seals_and_records_provenance(
    tmp_path: Path,
) -> None:
    overlay = _overlay(tmp_path)
    record = cb.promote(overlay, tmp_path / "base", COMMIT)
    linked = tmp_path / "base" / ".decomposition-index" / "k1.json"
    assert os.path.samestat(
        linked.stat(), (overlay / ".decomposition-index" / "k1.json").stat()
    )
    assert not (tmp_path / "base" / cb.OVERLAY_MARKER).exists()
    assert not linked.stat().st_mode & stat.S_IWUSR
    assert record["manifest"]["files"] == 3 and record["source_commit"] == COMMIT
    assert cb.verify(tmp_path / "base")["source_commit"] == COMMIT


def test_promote_refuses_non_overlay_existing_base_and_short_commit(
    tmp_path: Path,
) -> None:
    overlay = _overlay(tmp_path)
    cb.promote(overlay, tmp_path / "base", COMMIT)
    with pytest.raises(cb.CacheBaseError, match="already exists"):
        cb.promote(overlay, tmp_path / "base", COMMIT)
    with pytest.raises(cb.CacheBaseError, match="40-hex"):
        cb.promote(overlay, tmp_path / "b2", "abc")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(cb.CacheBaseError, match="not a retained-cache overlay"):
        cb.promote(plain, tmp_path / "b3", COMMIT)


def test_verify_detects_a_changed_byte(tmp_path: Path) -> None:
    cb.promote(_overlay(tmp_path), tmp_path / "base", COMMIT)
    victim = tmp_path / "base" / "financial-replay-receipts" / "r1.json"
    os.chmod(victim, stat.S_IWUSR | stat.S_IRUSR)
    victim.write_text("444", encoding="utf-8")
    with pytest.raises(cb.CacheBaseError, match="differ"):
        cb.verify(tmp_path / "base")


def test_verify_refuses_a_base_without_provenance(tmp_path: Path) -> None:
    (tmp_path / "base").mkdir()
    with pytest.raises(cb.CacheBaseError, match="provenance"):
        cb.verify(tmp_path / "base")


def _next_overlay(tmp: Path, new_index_entries: int) -> Path:
    nxt = tmp / "cache22"
    base = tmp / "base"
    for src in base.rglob("*"):
        if src.is_file():
            dst = nxt / src.relative_to(base)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.link(src, dst)
    for i in range(new_index_entries):
        (nxt / ".decomposition-index" / f"new{i}.json").write_text(
            "n", encoding="utf-8"
        )
    (nxt / cb.OVERLAY_MARKER).write_text("{}", encoding="utf-8")
    return nxt


def test_reuse_report_counts_inherited_and_new(tmp_path: Path) -> None:
    cb.promote(_overlay(tmp_path), tmp_path / "base", COMMIT)
    result = cb.reuse_report(tmp_path / "base", _next_overlay(tmp_path, 2))
    assert result["kinds"][".decomposition-index"] == {"inherited": 1, "new": 2}
    assert result["kinds"]["financial-replay-receipts"] == {"inherited": 1, "new": 0}


@pytest.mark.parametrize(
    ("stages", "new", "code"),
    [
        (["analytics"], 0, cb.EXIT_SUSPICIOUS_REUSE),
        (["analytics"], 1, 0),
        ([], 0, 0),
        (["report"], 0, 0),
    ],
)
def test_producer_change_with_nothing_recomputed_is_suspicious(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stages: list[str],
    new: int,
    code: int,
) -> None:
    cb.promote(_overlay(tmp_path), tmp_path / "base", COMMIT)
    monkeypatch.setattr(cb, "_affected_stages", lambda repo, a, b: stages)
    result, rc = cb.report(
        tmp_path / "base", _next_overlay(tmp_path, new), tmp_path, "b" * 40
    )
    assert rc == code
    assert bool(result["suspicious_reuse"]) == (code != 0)
