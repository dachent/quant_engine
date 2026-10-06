"""Round 3 of the preflight ladder: the residual fail-opens and false refusals."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile

import pytest

import check_retained_authority_pins as pins
import portfolio_attended_preparation as preparation
import portfolio_pipeline_materialize as materializer
from preflight_ladder_fixture import (
    _json,
    _sha,
    make_attribution,
    make_baseline,
    make_ledger,
)

RUN_SHA = "b" * 64
GEN = "a" * 64


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


def _phase():
    return {
        "provider_ledger_generation_id": GEN,
        "provider_ledger_catalog": {"path": "p", "sha256": RUN_SHA},
        "status": "PASS",
    }


def _manifest(path: Path, entries: list[dict[str, str]]) -> Path:
    path.write_bytes(
        _json(
            {
                "schema": "qe-cp-acceptance-inputs/2",
                "cutoff": "2026-09-17",
                "inputs": entries,
            }
        )
    )
    return path


class _Reached(Exception):
    """The refusal under test did not fire and execution moved on."""


# ------------------------------------------------ 1: acceptance needs an oracle


def test_execute_declaring_acceptance_without_an_oracle_is_refused(
    tmp_path, monkeypatch, capsys
):
    def storage(_args):
        raise _Reached

    monkeypatch.setattr(materializer, "_attended_storage", storage)
    code = materializer.main(
        _cli(tmp_path, "--qa-hold", "--declares-acceptance", "--execute")
    )
    assert code == 1
    assert "--oracle-manifest" in capsys.readouterr().err


def test_execute_declaring_acceptance_with_an_oracle_passes_the_gate(
    tmp_path, monkeypatch
):
    def storage(_args):
        raise _Reached

    monkeypatch.setattr(materializer, "_attended_storage", storage)
    oracle = _manifest(tmp_path / "o.json", [])
    with pytest.raises(_Reached):
        materializer.main(
            _cli(
                tmp_path,
                "--qa-hold",
                "--declares-acceptance",
                "--oracle-manifest",
                str(oracle),
                "--execute",
            )
        )


def test_plan_only_declaring_acceptance_still_reports_not_checked(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(materializer, "_attended_basic_preflight", lambda _a: {})
    monkeypatch.setattr(
        preparation,
        "plan_attended_inputs",
        lambda **_k: {"status": "PLANNED", "source_phase": _phase()},
    )
    code = materializer.main(
        _cli(tmp_path, "--declares-acceptance", preparation_spec=True)
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["oracle_ledger_join"]["status"] == "NOT_CHECKED"


def test_runbook_says_execute_requires_the_oracle_when_declaring_acceptance():
    text = (
        Path(materializer.__file__).resolve().parents[1]
        / "docs"
        / "attended-generation-runbook.md"
    ).read_text(encoding="utf-8")
    assert "--execute" in text and "refuses `--declares-acceptance`" in text


# ----------------------------------------------- 2: duplicate oracle ledger paths


def test_conflicting_duplicate_ledger_path_is_refused(tmp_path):
    manifest = _manifest(
        tmp_path / "m.json",
        [
            {"path": "x/v6/provider-ledger.json", "sha256": RUN_SHA},
            {"path": "x/v6/provider-ledger.json", "sha256": "c" * 64},
        ],
    )
    with pytest.raises(
        materializer.MaterializationError, match="conflicting duplicate"
    ):
        materializer._attended_oracle_ledger_join(
            _phase(), manifest, declares_acceptance=True
        )


def test_conflicting_duplicate_with_another_slash_spelling_is_refused(tmp_path):
    manifest = _manifest(
        tmp_path / "m.json",
        [
            {"path": "x/v6/provider-ledger.json", "sha256": RUN_SHA},
            {"path": "x\\v6\\provider-ledger.json", "sha256": "c" * 64},
        ],
    )
    with pytest.raises(
        materializer.MaterializationError, match="conflicting duplicate"
    ):
        materializer._attended_oracle_ledger_join(
            _phase(), manifest, declares_acceptance=True
        )


def test_identical_duplicate_ledger_entries_are_accepted(tmp_path):
    entry = {"path": "x/v6/provider-ledger.json", "sha256": RUN_SHA}
    manifest = _manifest(tmp_path / "m.json", [entry, dict(entry)])
    result = materializer._attended_oracle_ledger_join(
        _phase(), manifest, declares_acceptance=True
    )
    assert result["status"] == "MATCH"


# ------------------------------------- 3: attribution-baseline join by content


def _retained(tmp_path):
    ledger = make_ledger(tmp_path / "ledger" / ("9" * 64), "v6")
    baseline = make_baseline(tmp_path / "baseline" / ("b" * 64), ledger)
    return ledger, baseline


def test_preserved_baseline_copy_with_another_folder_name_is_accepted(tmp_path):
    ledger, baseline = _retained(tmp_path)
    attribution = make_attribution(tmp_path / "attr" / ("d" * 64), baseline)
    preserved = tmp_path / "preserved" / "baseline-copy"
    shutil.copytree(baseline, preserved)
    rows = pins.check_ledger_pin_matrix(
        ledger, retained={"baseline": preserved, "attribution": attribution}
    )
    assert {row["role"]: row["status"] for row in rows}["attribution"] == "MATCH"


def test_preserved_baseline_with_different_content_is_refused(tmp_path):
    ledger, baseline = _retained(tmp_path)
    attribution = make_attribution(tmp_path / "attr" / ("d" * 64), baseline)
    preserved = tmp_path / "preserved" / "baseline-copy"
    shutil.copytree(baseline, preserved)
    (preserved / "portfolio-baseline.json").write_bytes(
        (preserved / "portfolio-baseline.json").read_bytes() + b" "
    )
    with pytest.raises(pins.LedgerPinMismatch, match="attribution baseline identity"):
        pins.check_ledger_pin_matrix(
            ledger, retained={"baseline": preserved, "attribution": attribution}
        )


def test_generation_folder_that_differs_is_refused_even_with_equal_content(tmp_path):
    ledger, baseline = _retained(tmp_path)
    attribution = make_attribution(tmp_path / "attr" / ("d" * 64), baseline)
    other = tmp_path / "other" / ("e" * 64)
    shutil.copytree(baseline, other)
    with pytest.raises(pins.LedgerPinMismatch, match="attribution baseline identity"):
        pins.check_ledger_pin_matrix(
            ledger, retained={"baseline": other, "attribution": attribution}
        )


# ---------------------------------------- 4: cheap joins before storage.admit


class _Storage:
    def __init__(self, calls):
        self.calls = calls

    def admit(self, **_kwargs):
        self.calls.append("admit")

    def mark_created(self, *_a, **_k):
        self.calls.append("mark_created")

    def fail_if_open(self, _reason):
        self.calls.append("fail")


def _prep_stubs(monkeypatch, calls, *, refuse: bool):
    monkeypatch.setattr(materializer, "_attended_basic_preflight", lambda _a: {})
    monkeypatch.setattr(materializer, "_attended_storage", lambda _a: _Storage(calls))

    def plan(**_kwargs):
        calls.append("plan")
        if refuse:
            raise preparation.AttendedPreparationError(
                "allocation provider ledger identity differs"
            )
        return {"status": "PLANNED", "source_phase": _phase()}

    def prepare(**_kwargs):
        calls.append("prepare")
        raise _Reached

    monkeypatch.setattr(preparation, "plan_attended_inputs", plan)
    monkeypatch.setattr(preparation, "prepare_attended_inputs", prepare)


def test_execute_with_preparation_spec_validates_before_admitting_storage(
    tmp_path, monkeypatch, capsys
):
    calls: list[str] = []
    _prep_stubs(monkeypatch, calls, refuse=True)
    code = materializer.main(
        _cli(tmp_path, "--qa-hold", "--execute", preparation_spec=True)
    )
    assert code == 1
    assert "allocation provider ledger identity differs" in capsys.readouterr().err
    assert "admit" not in calls and "prepare" not in calls
    assert not (tmp_path / "output").exists()


def test_execute_with_preparation_spec_admits_only_after_the_validation_passes(
    tmp_path, monkeypatch
):
    calls: list[str] = []
    _prep_stubs(monkeypatch, calls, refuse=False)
    with pytest.raises(_Reached):
        materializer.main(
            _cli(tmp_path, "--qa-hold", "--execute", preparation_spec=True)
        )
    assert calls == ["plan", "admit", "prepare"]


def test_execute_with_source_run_refuses_before_admitting_storage(
    tmp_path, monkeypatch, capsys
):
    calls: list[str] = []
    monkeypatch.setattr(materializer, "_attended_storage", lambda _a: _Storage(calls))

    def refuse(_args):
        raise materializer.MaterializationError(
            "retained allocation provider ledger identity differs"
        )

    monkeypatch.setattr(materializer, "_attended_preflight", refuse)
    code = materializer.main(_cli(tmp_path, "--qa-hold", "--execute"))
    assert code == 1
    assert "admit" not in calls and not (tmp_path / "output").exists()


# --------------------------------------------- 6: measured provenance basenames


def _spec_with_provenance(tmp_path: Path, names: list[str]) -> Path:
    template = tmp_path / "base-spec.json"
    template.write_bytes(
        _json({"provenance_sources": [{"path": f"provenance/{n}"} for n in names]})
    )
    spec = tmp_path / "preparation-spec.json"
    spec.write_bytes(
        _json(
            {
                "plans": {
                    "LIVE_PLUS_BACKTEST": {
                        "base_spec_template": {
                            "path": str(template),
                            "sha256": _sha(template.read_bytes()),
                            "size_bytes": template.stat().st_size,
                        }
                    }
                }
            }
        )
    )
    return spec


def test_a_longer_measured_provenance_basename_is_refused_by_name(tmp_path):
    long_name = "p" * 135 + ".json"
    spec = _spec_with_provenance(tmp_path, ["short.json", long_name])
    tails = materializer._attended_plan_provenance_tails(spec)
    with pytest.raises(materializer.MaterializationError, match=long_name):
        materializer._attended_path_budget(
            Path("C:/Dev/q/out"), Path("C:/Dev/q/cache"), extra_tails=tails
        )


def test_short_measured_provenance_names_pass(tmp_path):
    spec = _spec_with_provenance(tmp_path, ["short.json", "base-proxies.json"])
    materializer._attended_path_budget(
        Path("C:/Dev/q/out"),
        Path("C:/Dev/q/cache"),
        extra_tails=materializer._attended_plan_provenance_tails(spec),
    )


def test_unreadable_preparation_spec_yields_no_measured_tails(tmp_path):
    assert materializer._attended_plan_provenance_tails(tmp_path / "missing.json") == []
    assert materializer._attended_plan_provenance_tails(None) == []


def test_basic_preflight_measures_the_supplied_preparation_spec(tmp_path, monkeypatch):
    scratch = Path("C:/Dev/scratch/quant_engine")
    if not scratch.is_dir():
        pytest.skip("no short scratch root on this machine")
    monkeypatch.setattr(
        materializer, "_runtime_identity", lambda python, repo: {"repo": str(repo)}
    )
    spec = _spec_with_provenance(tmp_path, ["q" * 140 + ".json"])
    base = Path(tempfile.mkdtemp(prefix="r3", dir=scratch))
    try:
        argv = _cli(base, "--preparation-spec", str(spec))
        argv = [a for a in argv if a != "--source-run"]
        # drop the --source-run value and the default --preparation-spec pair
        args = materializer._parser().parse_args(
            [
                "attended",
                "--repo-root", str(materializer.REPOSITORY_ROOT),
                "--python", sys.executable,
                "--workspace-root", str(base / "workspace"),
                "--output-root", str(base / "o"),
                "--retained-cache-root", str(base / "cache"),
                "--requested-as-of", "2026-09-17",
                "--evaluated-on", "2026-09-19",
                "--preparation-spec", str(spec),
            ]
        )  # fmt: skip
        with pytest.raises(materializer.MaterializationError, match="q" * 140):
            materializer._attended_basic_preflight(args)
    finally:
        shutil.rmtree(base, ignore_errors=True)
