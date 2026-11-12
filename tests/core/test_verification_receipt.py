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
    monkeypatch.delenv(vr.MODE_ENV, raising=False)
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
    assert list((world["state"] / vr.RECEIPT_DIRECTORY).glob("toy-*.json"))


def test_a_tampered_stored_receipt_is_ignored(world):
    v = Verifier(world)
    v.run()
    (path,) = (world["state"] / vr.RECEIPT_DIRECTORY).glob("toy-*.json")
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
    assert not list((world["state"] / vr.RECEIPT_DIRECTORY).glob("race-*.json"))


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
    (path,) = (world["state"] / vr.RECEIPT_DIRECTORY).glob("toy-*.json")
    vr.clear_memory_receipts()
    document = json.loads(path.read_text(encoding="utf-8"))
    document["body"]["result"] = json.dumps({"status": "PASS", "total": 0})
    document["receipt_sha256"] = vr._sha(document["body"])  # a self-consistent forgery
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(vr.VerificationReceiptMismatch):
        v.run()
