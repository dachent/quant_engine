"""Hash-bound verification receipts: reuse only while every recorded input is unchanged."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from quant_engine.core import verification_receipt as vr


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(vr.MODE_ENV, "persist")
    vr.clear_memory_receipts()
    yield
    vr.clear_memory_receipts()


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "v.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "uv.lock").write_text("lock\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.json").write_text('{"v": 1}', encoding="utf-8")
    (data / "b.json").write_text('{"v": 2}', encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    return {"repo": repo, "data": data, "state": state}


class Verifier:
    """A deterministic function of the files it reads; counts its live runs."""

    def __init__(self, world: dict[str, Path]) -> None:
        self.world = world
        self.live = 0
        self.reused: list[str] = []

    def compute(self) -> dict[str, object]:
        self.live += 1
        total = 0
        for path in sorted(self.world["data"].iterdir()):
            total += json.loads(path.read_text(encoding="utf-8"))["v"]
        probe = (self.world["data"] / "optional.json").exists()
        return {"status": "PASS", "total": total, "optional": probe}

    def run(self, *, identity: str = "spec1") -> dict[str, object]:
        return vr.run_with_receipt(
            "toy",
            {"spec": identity},
            repo_root=self.world["repo"],
            state_root=self.world["state"],
            compute=self.compute,
            on_reuse=self.reused.append,
        )


def test_second_call_reuses_the_receipt_and_returns_an_equal_fresh_object(world):
    v = Verifier(world)
    first = v.run()
    second = v.run()
    assert v.live == 1 and len(v.reused) == 1
    assert first == second == {"status": "PASS", "total": 3, "optional": False}
    second["total"] = 999  # a caller mutating its copy cannot poison the receipt
    assert v.run()["total"] == 3


def test_a_changed_read_file_is_never_served_from_a_stale_receipt(world):
    v = Verifier(world)
    v.run()
    (world["data"] / "a.json").write_text('{"v": 10}', encoding="utf-8")  # tamper
    assert v.run()["total"] == 12
    assert v.live == 2 and v.reused == []


def test_a_new_file_in_a_listed_directory_or_a_newly_present_probe_voids_it(world):
    v = Verifier(world)
    v.run()
    (world["data"] / "optional.json").write_text('{"v": 5}', encoding="utf-8")
    assert v.run() == {"status": "PASS", "total": 8, "optional": True}
    assert v.live == 2


def test_receipt_survives_a_new_process_through_the_state_root(world):
    v = Verifier(world)
    v.run()
    vr.clear_memory_receipts()  # a different process has no memory
    assert v.run()["total"] == 3
    assert v.live == 1 and len(v.reused) == 1
    assert list((vr.receipt_directory(world["state"])).glob("toy-*.json"))


def test_a_tampered_stored_receipt_is_ignored(world):
    v = Verifier(world)
    v.run()
    (path,) = (vr.receipt_directory(world["state"])).glob("toy-*.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["body"]["result"] = json.dumps({"status": "PASS", "total": 0})
    path.write_text(json.dumps(document), encoding="utf-8")
    vr.clear_memory_receipts()
    assert v.run()["total"] == 3
    assert v.live == 2


def test_different_spec_identity_or_tool_identity_is_a_different_key(world):
    v = Verifier(world)
    v.run(identity="spec1")
    v.run(identity="spec2")
    assert v.live == 2
    (world["repo"] / "tools" / "v.py").write_text("x = 2\n", encoding="utf-8")
    vr.clear_memory_receipts()  # drops the cached tool identity too
    v.run(identity="spec1")
    assert v.live == 3


def test_a_child_process_makes_the_recording_incomplete_so_nothing_is_stored(world):
    live = {"n": 0}

    def compute() -> dict[str, object]:
        live["n"] += 1
        subprocess.run([sys.executable, "-c", "pass"], check=True)
        return {"status": "PASS"}

    for _ in range(2):
        vr.run_with_receipt(
            "child",
            {},
            repo_root=world["repo"],
            state_root=world["state"],
            compute=compute,
        )
    assert live["n"] == 2


def test_a_non_json_result_is_returned_but_never_stored(world):
    live = {"n": 0}

    def compute() -> object:
        live["n"] += 1
        return {"pair": (1, 2)}  # tuple does not round-trip

    for _ in range(2):
        out = vr.run_with_receipt(
            "tuple",
            {},
            repo_root=world["repo"],
            state_root=world["state"],
            compute=compute,
        )
        assert out == {"pair": (1, 2)}
    assert live["n"] == 2


def test_a_file_that_changes_while_the_verifier_reads_it_voids_the_receipt(world):
    target = world["data"] / "a.json"
    live = {"n": 0}

    def compute() -> dict[str, object]:
        live["n"] += 1
        value = json.loads(target.read_text(encoding="utf-8"))["v"]
        target.write_text('{"v": 77}', encoding="utf-8")  # raced after the read
        return {"seen": value}

    vr.run_with_receipt(
        "race", {}, repo_root=world["repo"], state_root=world["state"], compute=compute
    )
    assert not list((vr.receipt_directory(world["state"])).glob("race-*.json"))


def test_failures_are_never_cached(world):
    live = {"n": 0}

    def compute() -> dict[str, object]:
        live["n"] += 1
        raise ValueError("verifier refused")

    for _ in range(2):
        with pytest.raises(ValueError):
            vr.run_with_receipt(
                "bad",
                {},
                repo_root=world["repo"],
                state_root=world["state"],
                compute=compute,
            )
    assert live["n"] == 2


def test_off_mode_always_runs_live_and_refresh_detects_disagreement(
    world, monkeypatch: pytest.MonkeyPatch
):
    v = Verifier(world)
    v.run()
    monkeypatch.setenv(vr.MODE_ENV, "off")
    v.run()
    assert v.live == 2
    monkeypatch.setenv(vr.MODE_ENV, "refresh")
    v.run()  # runs live, agrees with the stored receipt
    assert v.live == 3
    # a verifier that now answers differently for identical inputs is caught
    (path,) = (vr.receipt_directory(world["state"])).glob("toy-*.json")
    vr.clear_memory_receipts()
    document = json.loads(path.read_text(encoding="utf-8"))
    document["body"]["result"] = json.dumps({"status": "PASS", "total": 0})
    document["receipt_sha256"] = vr._sha(document["body"])  # a self-consistent forgery
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(vr.VerificationReceiptMismatch):
        v.run()


# -- review findings: every one of these must void or refuse the receipt ----------------


def _run(world, name, compute, **extra):
    return vr.run_with_receipt(
        name,
        {},
        repo_root=world["repo"],
        state_root=world["state"],
        compute=compute,
        **extra,
    )


def test_an_optional_file_that_did_not_exist_and_appears_voids_the_receipt(world):
    optional = world["data"] / "override.json"
    live = {"n": 0}

    def compute() -> dict[str, object]:
        live["n"] += 1
        try:
            return {"cfg": json.loads(optional.read_text(encoding="utf-8"))}
        except FileNotFoundError:
            return {"cfg": {}}

    assert _run(world, "opt", compute) == {"cfg": {}}
    assert _run(world, "opt", compute) == {"cfg": {}} and live["n"] == 1
    optional.write_text('{"limit": 0}', encoding="utf-8")
    assert _run(world, "opt", compute) == {"cfg": {"limit": 0}}
    assert live["n"] == 2


def test_path_exists_alone_is_recorded_as_a_probe(world):
    flag = world["data"] / "halt.flag"
    live = {"n": 0}

    def compute() -> dict[str, object]:
        live["n"] += 1
        return {"halted": flag.exists()}  # no listing, no read

    assert _run(world, "flag", compute) == {"halted": False}
    assert _run(world, "flag", compute) == {"halted": False} and live["n"] == 1
    flag.write_text("x", encoding="utf-8")
    assert _run(world, "flag", compute) == {"halted": True}


def test_a_directory_entry_type_swap_changes_the_listing(world):
    entry = world["data"] / "entry"
    entry.write_text("x", encoding="utf-8")
    live = {"n": 0}

    def compute() -> dict[str, object]:
        live["n"] += 1
        return {
            "is_dir": [
                e.is_dir()
                for e in __import__("os").scandir(world["data"])
                if e.name == "entry"
            ]
        }

    _run(world, "types", compute)
    entry.unlink()
    entry.mkdir()
    assert _run(world, "types", compute) == {"is_dir": [True]}
    assert live["n"] == 2


def test_a_written_file_that_is_corrupted_later_voids_the_receipt(world):
    out = world["state"] / "summary.json"
    live = {"n": 0}

    def compute() -> dict[str, object]:
        live["n"] += 1
        out.write_text('{"ok": true}', encoding="utf-8")
        return {"status": "PASS"}

    _run(world, "writer", compute)
    assert _run(world, "writer", compute) == {"status": "PASS"} and live["n"] == 1
    out.write_text("", encoding="utf-8")  # same path, different content
    _run(world, "writer", compute)
    assert live["n"] == 2 and out.read_text(encoding="utf-8") == '{"ok": true}'


def test_a_non_python_file_under_tools_is_part_of_the_read_set(world):
    contract = world["repo"] / "tools" / "contract.json"
    contract.write_text('{"v": 1}', encoding="utf-8")
    live = {"n": 0}

    def compute() -> dict[str, object]:
        live["n"] += 1
        return json.loads(contract.read_text(encoding="utf-8"))

    _run(world, "contract", compute)
    contract.write_text('{"v": 2}', encoding="utf-8")
    assert _run(world, "contract", compute) == {"v": 2} and live["n"] == 2


def test_a_python_source_edit_between_two_calls_in_one_process_changes_the_key(world):
    v = Verifier(world)
    v.run()
    (world["repo"] / "tools" / "v.py").write_text("x = 99\n", encoding="utf-8")
    v.run()  # no clear_memory_receipts(): the identity is recomputed every call
    assert v.live == 2


def test_a_symlink_retarget_with_identical_content_is_detected(world, tmp_path):
    first, second = world["data"] / "one.json", world["data"] / "two.json"
    first.write_text('{"v": 1}', encoding="utf-8")
    second.write_text('{"v": 1}', encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(first)
    except OSError:
        pytest.skip("symlinks unavailable")
    live = {"n": 0}

    def compute() -> dict[str, object]:
        live["n"] += 1
        return {
            "link": link.is_symlink(),
            "v": json.loads(link.read_text("utf-8"))["v"],
        }

    _run(world, "link", compute)
    link.unlink()
    link.symlink_to(second)
    _run(world, "link", compute)
    assert live["n"] == 2


def test_a_descriptor_opened_from_a_path_inside_the_run_is_recorded_by_path(world):
    live = {"n": 0}

    def compute() -> dict[str, object]:
        import os

        live["n"] += 1
        fd = os.open(world["data"] / "a.json", os.O_RDONLY)
        with open(fd, "rb") as handle:
            return {"size": len(handle.read())}

    _run(world, "fd", compute)
    _run(world, "fd", compute)
    assert live["n"] == 1
    (world["data"] / "a.json").write_text('{"v": 1000}', encoding="utf-8")
    _run(world, "fd", compute)
    assert live["n"] == 2


def test_refresh_does_not_cry_wolf_when_an_input_legitimately_changed(
    world, monkeypatch: pytest.MonkeyPatch
):
    v = Verifier(world)
    v.run()
    (world["data"] / "a.json").write_text('{"v": 10}', encoding="utf-8")
    monkeypatch.setenv(vr.MODE_ENV, "refresh")
    assert v.run()["total"] == 12  # stale receipt: no comparison, just re-store


def test_a_deleted_receipt_file_is_not_served_from_memory(world):
    v = Verifier(world)
    v.run()
    for path in (vr.receipt_directory(world["state"])).glob("toy-*.json"):
        path.unlink()
    v.run()
    assert v.live == 2


def test_the_source_tree_never_uses_dir_fd_relative_io():
    """The recorder cannot resolve ``dir_fd``-relative paths (documented limit)."""

    root = Path(__file__).resolve().parents[2]
    offenders = [
        str(path.relative_to(root))
        for top in ("tools", "packages")
        for path in (root / top).rglob("*.py")
        if "dir_fd" in path.read_text(encoding="utf-8", errors="ignore")
        and path.name
        not in {
            "verification_receipt.py",
            "publication.py",
        }  # publication.py: commit-time renames, never inside a wrapped verifier
    ]
    assert offenders == []


def test_dir_fd_relative_io_makes_the_recording_incomplete(world):
    import os

    if os.open not in os.supports_dir_fd:
        pytest.skip("dir_fd unsupported on this platform")
    live = {"n": 0}

    def compute() -> dict[str, object]:
        live["n"] += 1
        directory = os.open(world["data"], os.O_RDONLY)
        try:
            return {"size": os.stat("a.json", dir_fd=directory).st_size}
        finally:
            os.close(directory)

    _run(world, "dirfd", compute)
    _run(world, "dirfd", compute)
    assert live["n"] == 2


def test_installing_the_recorder_keeps_os_capability_checks_true(world):
    import os

    before = {
        name: (os.open in getattr(os, name), os.stat in getattr(os, name))
        for name in ("supports_dir_fd", "supports_fd", "supports_follow_symlinks")
    }
    _run(world, "cap", lambda: {"ok": True})  # installs the recorder
    after = {
        name: (os.open in getattr(os, name), os.stat in getattr(os, name))
        for name in ("supports_dir_fd", "supports_fd", "supports_follow_symlinks")
    }
    assert before == after


def test_only_a_pinned_git_show_child_is_allowed():
    sha = "a" * 40
    assert vr.allowed_child_command(["git", "show", f"{sha}:contracts/x.json"])
    assert vr.allowed_child_command(f"git show {sha}:contracts/x.json")
    assert not vr.allowed_child_command(["git", "show", "HEAD:contracts/x.json"])
    assert not vr.allowed_child_command(["git", "status"])
    assert not vr.allowed_child_command(["gh", "api", "x"])
    assert not vr.allowed_child_command(["git", "show", f"{sha}:a", "--textconv"])


def test_default_mode_keeps_receipts_in_this_process_only(
    world, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv(vr.MODE_ENV, raising=False)
    v = Verifier(world)
    v.run()
    v.run()
    assert v.live == 1 and len(v.reused) == 1  # in-process verify-once
    assert not vr.receipt_directory(world["state"]).exists()  # nothing persisted
    vr.clear_memory_receipts()  # a new process has nothing
    v.run()
    assert v.live == 2


def test_a_declared_tree_is_part_of_the_read_set(world):
    tree = world["data"] / "generation"
    (tree / "sub").mkdir(parents=True)
    (tree / "a.bin").write_bytes(b"1")
    (tree / "sub" / "b.bin").write_bytes(b"2")
    live = {"n": 0}

    def compute() -> dict[str, object]:
        import sys

        live["n"] += 1
        sys.audit("quant_engine.read_tree", str(tree))  # handle-based capture
        return {"status": "PASS"}

    _run(world, "tree", compute)
    _run(world, "tree", compute)
    assert live["n"] == 1
    (tree / "sub" / "b.bin").write_bytes(
        b"3"
    )  # a file only the tree declaration covers
    _run(world, "tree", compute)
    assert live["n"] == 2
    (tree / "c.bin").write_bytes(b"x")  # a new file in the declared tree
    _run(world, "tree", compute)
    assert live["n"] == 3


def test_a_pinned_git_child_does_not_void_the_recording_but_another_one_does(world):
    recorder = vr._Recorder()
    sha = "b" * 40
    recorder.on_event("subprocess.Popen", (None, f"git show {sha}:x.json", ".", None))
    recorder.on_event("_winapi.CreateProcess", (None, "\x02", "."))
    assert recorder.incomplete == []
    recorder.on_event("_winapi.CreateProcess", (None, "\x02", "."))
    assert recorder.incomplete == ["_winapi.CreateProcess"]


def test_allowed_read_only_children_are_exactly_these():
    sha = "c" * 40
    assert vr.allowed_child_command(f"git -C C:\repo show {sha}:docs/x.json")
    assert vr.allowed_child_command(
        "git -C C:\repo rev-parse --show-toplevel HEAD^{commit}"
    )
    assert vr.allowed_child_command('C:\Windows\system32\cmd.exe /c "ver"')
    assert not vr.allowed_child_command("git -C C:\repo checkout main")
    assert not vr.allowed_child_command("git -C C:\repo show HEAD:docs/x.json")
    assert not vr.allowed_child_command("cmd.exe /c del x")
