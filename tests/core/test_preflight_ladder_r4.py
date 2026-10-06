"""Round 4 of the preflight ladder: the final-phase oracle join and receipt-read order."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

import portfolio_attended_preparation as preparation
import portfolio_pipeline_materialize as materializer
from preflight_ladder_fixture import _json

PLANNED = "b" * 64
FINAL = "d" * 64
GEN = "a" * 64


def _phase(sha: str) -> dict[str, object]:
    return {
        "provider_ledger_generation_id": GEN,
        "provider_ledger_catalog": {"path": "p", "sha256": sha},
        "status": "PASS",
    }


def _oracle(tmp_path: Path, sha: str) -> Path:
    path = tmp_path / "oracle.json"
    path.write_bytes(
        _json(
            {
                "schema": "qe-cp-acceptance-inputs/2",
                "cutoff": "2026-09-17",
                "inputs": [{"path": "x/v6-copy/provider-ledger.json", "sha256": sha}],
            }
        )
    )
    return path


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    (tmp_path / "workspace" / "dataPortfolio").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(exist_ok=True)
    return [
        "attended",
        "--repo-root", str(Path(materializer.__file__).resolve().parents[1]),
        "--python", sys.executable,
        "--workspace-root", str(tmp_path / "workspace"),
        "--output-root", str(tmp_path / "output"),
        "--retained-cache-root", str(tmp_path / "cache"),
        "--requested-as-of", "2026-09-17",
        "--evaluated-on", "2026-09-19",
        "--preparation-spec", str(tmp_path / "native-inputs.json"),
        *extra,
    ]  # fmt: skip


class _Storage:
    def admit(self, **_kwargs):
        pass

    def mark_created(self, *_a, **_k):
        pass

    def fail_if_open(self, _reason):
        pass


def _stubs(monkeypatch, tmp_path, *, planned: str, final: str):
    executed: list[str] = []
    monkeypatch.setattr(materializer, "_attended_basic_preflight", lambda _a: {})
    monkeypatch.setattr(materializer, "_attended_storage", lambda _a: _Storage())
    monkeypatch.setattr(materializer, "_git", lambda *_a: "a" * 40)
    monkeypatch.setattr(
        preparation,
        "plan_attended_inputs",
        lambda **_k: {"status": "PLANNED", "source_phase": _phase(planned)},
    )
    monkeypatch.setattr(
        preparation,
        "prepare_attended_inputs",
        lambda **_k: {
            "source_run": tmp_path / "prepared-source-run.json",
            "financial_stage_inputs": {
                "--invested-capital-result": Path("capital-result.json"),
                "--invested-capital-input-generation": Path("capital-generation.json"),
            },
        },
    )
    monkeypatch.setattr(
        materializer,
        "_attended_preflight",
        lambda _a: {
            "source_phase": _phase(final),
            "required_stages": materializer.ATTENDED_STAGES,
            "request_identity": {},
            "qa_dir": None,
        },
    )

    def execute(*_a, **_k):
        executed.append("execute")
        return 0, {"planned_stages": []}

    monkeypatch.setattr(materializer, "_attended_execute", execute)
    return executed


def test_final_admitted_phase_with_another_ledger_refuses_before_the_run_finishes(
    tmp_path, monkeypatch, capsys
):
    executed = _stubs(monkeypatch, tmp_path, planned=PLANNED, final=FINAL)
    code = materializer.main(
        _argv(tmp_path, "--oracle-manifest", str(_oracle(tmp_path, PLANNED)), "--qa-hold", "--execute")
    )  # fmt: skip
    assert code == 1
    assert "oracle ledger identity differs" in capsys.readouterr().err
    assert executed == []


def test_matching_planned_and_final_phases_record_the_final_join(
    tmp_path, monkeypatch, capsys
):
    executed = _stubs(monkeypatch, tmp_path, planned=FINAL, final=FINAL)
    code = materializer.main(
        _argv(tmp_path, "--oracle-manifest", str(_oracle(tmp_path, FINAL)), "--qa-hold", "--execute")
    )  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and executed == ["execute"]
    assert out["oracle_ledger_join"] == {
        "status": "MATCH",
        "generation_id": None,
        "generation_id_checked": False,
        "sha256": FINAL,
    }


def test_the_pre_admission_join_still_refuses_a_mismatched_planned_phase(
    tmp_path, monkeypatch, capsys
):
    executed = _stubs(monkeypatch, tmp_path, planned=PLANNED, final=FINAL)
    code = materializer.main(
        _argv(tmp_path, "--oracle-manifest", str(_oracle(tmp_path, FINAL)), "--qa-hold", "--execute")
    )  # fmt: skip
    assert code == 1 and executed == []
    assert "oracle ledger identity differs" in capsys.readouterr().err


# --------------------------------------------- resume: location before receipts


def test_resume_in_a_synced_root_refuses_before_any_receipt_is_read(
    tmp_path, monkeypatch, capsys
):
    synced = tmp_path / "OneDrive - Example"
    monkeypatch.setenv("OneDriveCommercial", str(synced))
    output = synced / "runs" / "out"
    receipt = output / "stage-results" / "ledger.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_bytes(b"{}")
    opened: list[str] = []

    real_read_bytes = Path.read_bytes
    real_read_text = Path.read_text
    real_open = Path.open

    def spy(name, real):
        def wrapper(self, *args, **kwargs):
            if "stage-results" in str(self):
                opened.append(f"{name}:{self}")
            return real(self, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(Path, "read_bytes", spy("read_bytes", real_read_bytes))
    monkeypatch.setattr(Path, "read_text", spy("read_text", real_read_text))
    monkeypatch.setattr(Path, "open", spy("open", real_open))
    probed: list[str] = []
    real_completed = materializer._attended_completed_stages

    def completed(*args, **kwargs):
        probed.append("completed")
        return real_completed(*args, **kwargs)

    monkeypatch.setattr(materializer, "_attended_completed_stages", completed)
    (tmp_path / "workspace" / "dataPortfolio").mkdir(parents=True)
    (tmp_path / "cache").mkdir()
    code = materializer.main(
        [
            "attended",
            "--repo-root", str(Path(materializer.__file__).resolve().parents[1]),
            "--python", sys.executable,
            "--workspace-root", str(tmp_path / "workspace"),
            "--output-root", str(output),
            "--retained-cache-root", str(tmp_path / "cache"),
            "--requested-as-of", "2026-09-17",
            "--source-run", str(tmp_path / "source-run.json"),
            "--qa-hold", "--execute", "--resume",
        ]
    )  # fmt: skip
    assert code == 1
    assert "cloud-synced" in capsys.readouterr().err
    assert opened == [] and probed == []


def test_resume_in_a_scratch_root_still_reads_the_receipts(tmp_path, monkeypatch):
    probed: list[str] = []

    def completed(*_a, **_k):
        probed.append("completed")
        return []

    monkeypatch.setattr(materializer, "_attended_completed_stages", completed)
    monkeypatch.setattr(
        materializer,
        "_runtime_identity",
        lambda python, repo: {"repo": str(repo)},
    )
    monkeypatch.setattr(
        materializer, "_attended_preflight", lambda _a: {"source_phase": {}}
    )

    class Stop(Exception):
        pass

    class Storage:
        def admit(self, **_kwargs):
            raise Stop

        def fail_if_open(self, _reason):
            pass

    def storage(_a):
        return Storage()

    monkeypatch.setattr(materializer, "_attended_storage", storage)
    (tmp_path / "workspace" / "dataPortfolio").mkdir(parents=True)
    (tmp_path / "cache").mkdir()
    with pytest.raises(Stop):
        materializer.main(
        [
            "attended",
            "--repo-root", str(Path(materializer.__file__).resolve().parents[1]),
            "--python", sys.executable,
            "--workspace-root", str(tmp_path / "workspace"),
            "--output-root", str(tmp_path / "output"),
            "--retained-cache-root", str(tmp_path / "cache"),
            "--requested-as-of", "2026-09-17",
            "--source-run", str(tmp_path / "source-run.json"),
            "--qa-hold", "--execute", "--resume",
        ]
    )  # fmt: skip
    assert probed == ["completed"]


_ = pytest
