"""Round 2 of the preflight ladder: false refusals and gaps found in review.

Every case is built from the reviewer's shape. Path lengths below are measured from the
real attended runs under C:\\Dev\\q (2026-10-04), copied here as literals so the gate is
checked against what the producers wrote, not against its own tables.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

import portfolio_attended_preparation as preparation
import portfolio_pipeline_materialize as materializer
from preflight_ladder_fixture import (
    _json,
)

pytestmark = pytest.mark.real_attended_path_budget

LIMIT = 260
GEN = "0" * 64

# ---- measured from the real runs: relative to the output / cache root ----
REAL_OUTPUT_PATHS = (
    # stages\epoch static-snapshot receipt (longest of the r2 run, 183 characters)
    "stages\\epoch\\generations\\" + GEN + "\\receipt\\static-snapshot\\"
    "account-5a0baeaef10bedb2\\grizzlybears-vtma\\2026-01-02-2026-09-17.json",
    # deepest decomposition lens (182)
    "stages\\analytics\\decomposition-lenses\\LIVE_PLUS_BACKTEST\\DYNAMIC_FLS\\"
    "multi-asset-style\\generations\\" + GEN + "\\decomposition.json",
    # preparation plan provenance (168)
    "preparation\\plans\\live_plus_backtest\\" + GEN + "\\inputs\\base\\provenance\\"
    "05-base-proxies-adjusted.json.manifest.json",
)
REAL_CACHE_PATHS = (
    ".decomposition-training-evidence\\" + GEN + ".json",  # 102
    ".decomposition-training\\" + GEN + ".json",
    ".decomposition-index\\" + GEN + ".json",
    ".correlation-index\\" + GEN + ".json",
    "candidates\\" + GEN + "\\decomposition.json",
)


def _root(length: int, prefix: str = "C:\\Dev\\scratch\\") -> Path:
    return Path(prefix + "x" * (length - len(prefix)))


# ---------------------------------------- 4/5: the real attended CLI entry point


def _cli(tmp_path: Path, *extra: str, preparation_spec: bool = False) -> list[str]:
    (tmp_path / "workspace" / "dataPortfolio").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cache").mkdir(exist_ok=True)
    argv = [
        "attended",
        "--repo-root", str(Path(materializer.__file__).resolve().parents[1]),
        "--python", sys.executable,
        "--workspace-root", str(tmp_path / "workspace"),
        "--output-root", str(tmp_path / "output"),
        "--retained-cache-root", str(tmp_path / "cache"),
        "--requested-as-of", "2026-09-17",
        "--evaluated-on", "2026-09-19",
    ]  # fmt: skip
    if preparation_spec:
        argv += ["--preparation-spec", str(tmp_path / "native-inputs.json")]
    else:
        argv += ["--source-run", str(tmp_path / "source-run.json")]
    return [*argv, *extra]


RUN_SHA = "b" * 64


def _phase():
    return {
        "provider_ledger_generation_id": "a" * 64,
        "provider_ledger_catalog": {"path": "p", "sha256": RUN_SHA},
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


def _stub_preparation(monkeypatch, calls):
    monkeypatch.setattr(materializer, "_attended_basic_preflight", lambda _a: {})

    def plan(**_kwargs):
        calls.append("plan")
        return {"status": "PLANNED", "source_phase": _phase(), "data_clock": {}}

    def prepare(**_kwargs):
        calls.append("prepare")
        raise AssertionError("preparation must not start after a refused join")

    monkeypatch.setattr(preparation, "plan_attended_inputs", plan)
    monkeypatch.setattr(preparation, "prepare_attended_inputs", prepare)


def test_cli_plan_with_preparation_spec_joins_the_oracle_and_prints_the_qa_block(
    tmp_path, monkeypatch, capsys
):
    calls: list[str] = []
    _stub_preparation(monkeypatch, calls)
    code = materializer.main(
        _cli(tmp_path, "--oracle-manifest", str(_oracle(tmp_path, RUN_SHA)),
             preparation_spec=True)
    )  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["oracle_ledger_join"]["status"] == "MATCH"
    assert out["qa"]["required_flag"] == "--qa-dir"


def test_cli_plan_with_preparation_spec_refuses_a_mismatched_oracle(
    tmp_path, monkeypatch, capsys
):
    _stub_preparation(monkeypatch, [])
    code = materializer.main(
        _cli(tmp_path, "--oracle-manifest", str(_oracle(tmp_path, "c" * 64)),
             preparation_spec=True)
    )  # fmt: skip
    assert code == 1
    assert "oracle ledger identity differs" in capsys.readouterr().err


def test_cli_execute_refuses_a_mismatched_oracle_before_any_preparation(
    tmp_path, monkeypatch, capsys
):
    calls: list[str] = []
    _stub_preparation(monkeypatch, calls)

    class Storage:
        def admit(self, **_kwargs):
            calls.append("admit")

        def fail_if_open(self, _reason):
            calls.append("fail")

    monkeypatch.setattr(materializer, "_attended_storage", lambda _a: Storage())
    code = materializer.main(
        _cli(tmp_path, "--oracle-manifest", str(_oracle(tmp_path, "c" * 64)),
             "--qa-hold", "--execute", preparation_spec=True)
    )  # fmt: skip
    assert code == 1
    assert "oracle ledger identity differs" in capsys.readouterr().err
    assert "admit" not in calls and "prepare" not in calls


class _Gate(Exception):
    """The QA gate admitted the run and execution moved on to storage."""


def _stub_storage(monkeypatch):
    # A resume reads receipts for the QA gate only after the environment admission
    # (`_attended_preflight`); the gate precedes storage.admit.
    class Storage:
        def admit(self, **_kwargs):
            raise _Gate

        def fail_if_open(self, _reason):
            pass

    monkeypatch.setattr(materializer, "_attended_storage", lambda _args: Storage())
    monkeypatch.setattr(
        materializer, "_attended_preflight", lambda _args: {"source_phase": {}}
    )


def test_cli_resume_with_panel_already_complete_needs_no_qa_declaration(
    tmp_path, monkeypatch
):
    _stub_storage(monkeypatch)
    monkeypatch.setattr(
        materializer,
        "_attended_completed_stages",
        lambda _root, stages, **_kw: list(stages),
    )
    with pytest.raises(_Gate):
        materializer.main(_cli(tmp_path, "--execute", "--resume"))


def test_cli_resume_with_panel_remaining_still_demands_the_declaration(
    tmp_path, monkeypatch, capsys
):
    _stub_storage(monkeypatch)
    monkeypatch.setattr(
        materializer,
        "_attended_completed_stages",
        lambda _root, stages, **_kw: ["ledger", "baseline"],
    )
    assert materializer.main(_cli(tmp_path, "--execute", "--resume")) == 1
    assert "--qa-hold" in capsys.readouterr().err
