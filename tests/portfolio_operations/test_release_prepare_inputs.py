"""RELEASE-DRIVER-001 block B4: selected-inputs and attended preparation spec builders.

Every fixture is built here from synthetic bytes under ``tmp_path``; nothing is tracked, nothing is
real. The resolved cycle is written directly (its schema is checked by the tool on load), so the
tests exercise the builders and not the cycle manifest tool.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import release_driver as driver  # noqa: E402
import release_prepare_inputs as tool  # noqa: E402

CUTOFF = "2026-01-02"
EVALUATED = "2026-01-05"
COMMIT = "1fa910efde7255244e7dee3d5c36ae880eadb562"
PROVIDERS = ("alpaca", "ibkr", "matrixport")
LEDGER = "1" * 64
BASELINE = "b" * 64
HISTORY = "a1" * 32
ATTRIBUTION = "a2" * 32


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _put(path: Path, data: bytes | str | Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(data, bytes):
        data = (
            data if isinstance(data, str) else json.dumps(data, sort_keys=True)
        ).encode()
    path.write_bytes(data)
    return path


def _pin(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": path.resolve().as_posix(),
        "sha256": _sha(data),
        "size_bytes": len(data),
    }


def _posix(path: Path) -> str:
    return path.resolve().as_posix()


class World:
    """A synthetic cycle: roots, repository, provider generations, cycle outputs, predecessors."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp.resolve()
        self.data = self.tmp / "data"
        self.scratch = self.tmp / "scratch"
        self.cycle = self.scratch / "cycle"
        self.repo = self.tmp / "repo"
        self.elsewhere = self.tmp / "elsewhere"
        for directory in (self.data, self.cycle, self.repo, self.elsewhere):
            directory.mkdir(parents=True, exist_ok=True)
        self._repo_files()
        self.providers = {name: self._provider(name) for name in PROVIDERS}
        self._cycle_outputs()
        self.resolved = self._resolved()
        self.resolved_path = _put(self.tmp / "resolved.json", self.resolved)
        self.old = self.data / "old"
        self.selected_pred = self._selected_predecessor()
        self.prep_pred = self._prep_predecessor()

    # -- repository, providers, bundles -----------------------------------------------
    def _repo_files(self) -> None:
        for relative in (
            tool.CONTRACT,
            tool.PLAN,
            tool.CATALOG,
            tool.CROSSWALK,
            tool.POLICY_MANIFEST,
        ):
            _put(self.repo / relative, f"repo file {relative}")

    def _provider(self, name: str) -> dict[str, Any]:
        root = self.data / f"provider-{name}"
        gid = _sha(name.encode())
        manifest = _put(
            root / "generations" / gid / "generation.json",
            {"generation_id": gid, "who": name},
        )
        data = manifest.read_bytes()
        return {
            "root": _posix(root),
            "generation_id": gid,
            "generation_dir": _posix(manifest.parent),
            "manifest_path": _posix(manifest),
            "manifest_sha256": _sha(data),
            "manifest_size_bytes": len(data),
            "finality_source": "GENERATION_MANIFEST",
            "finality_evidence": None,
            "qualification": "PUBLISHED_COMPLETE",
            "finalized_through": None,
        }

    def bundle(self, name: str, bundle_id: str, files: tuple[str, ...]) -> Path:
        directory = self.data / "bundles" / name / bundle_id
        _put(directory / tool.MANIFEST_FILE, {"bundle_id": bundle_id})
        for filename in files:
            _put(directory / filename, f"{name} {filename}")
        return directory

    def _cycle_outputs(self) -> None:
        cycle = self.cycle
        ledger = cycle / "ledger" / "generations" / LEDGER
        _put(ledger / "generation.json", {"generation_id": LEDGER})
        _put(ledger / tool.LEDGER_RECEIPT, "receipt")
        _put(ledger / tool.LEDGER_CATALOG, "catalog")
        baseline = cycle / "baseline" / "generations" / BASELINE
        _put(baseline / "generation.json", {"generation_id": BASELINE})
        _put(baseline / tool.BASELINE_CATALOG, "baseline catalog")
        _put(
            cycle / "pre" / "history" / HISTORY / "generation.json",
            {"generation_id": HISTORY},
        )
        _put(
            cycle / "attribution" / "generations" / ATTRIBUTION / "generation.json",
            {"generation_id": ATTRIBUTION},
        )
        _put(cycle / tool.DEPENDENCE_SELECTION, {"schema_version": 1})
        _put(cycle / tool.ATTRIBUTION_SPEC, {"spec": "attribution"})
        _put(cycle / tool.BASELINE_SPEC, {"as_of_date": CUTOFF})
        _put(cycle / tool.CALENDAR_AUTHORITY, {"hwm_policy_sha256": "0" * 64})
        self.write_ledger_spec()
        self.bundle("factors", "f" * 64, tuple(tool.FACTOR_FILES.values()))
        self.bundle("sector", "5" * 64, (tool.SECTOR_FILE,))
        self.bundle("style", "7" * 64, (tool.STYLE_FILE,))
        _put(self.data / "epoch-policy.json", {"policy": "synthetic"})

    def write_ledger_spec(self, **changes: Any) -> None:
        spec: dict[str, Any] = {
            "as_of_date": CUTOFF,
            "output_root": _posix(self.cycle / "ledger"),
            "providers": {
                name: {
                    "generation_id": row["generation_id"],
                    "manifest_sha256": row["manifest_sha256"],
                }
                for name, row in self.providers.items()
            },
        }
        spec.update(changes)
        (self.cycle / tool.LEDGER_SPEC).write_bytes(json.dumps(spec).encode())

    def _resolved(self) -> dict[str, Any]:
        evidence = _put(self.data / "strategy-evidence.json", "evidence")
        policy = self.data / "epoch-policy.json"

        def file(path: Path) -> dict[str, Any]:
            data = path.read_bytes()
            return {"path": _posix(path), "sha256": _sha(data), "size_bytes": len(data)}

        return {
            "schema_version": "qe-release-resolved-cycle/1",
            "cycle_id": "cycle-synthetic",
            "cutoff": CUTOFF,
            "evaluated_on": EVALUATED,
            "captured_at_utc": "2026-01-04T10:00:00Z",
            "cycle_manifest_sha256": "c" * 64,
            "predecessor": {"release_id": "release-previous"},
            "roots": {"data": _posix(self.data), "scratch": _posix(self.scratch)},
            "scratch": _posix(self.cycle),
            "providers": self.providers,
            "strategy_evidence": file(evidence),
            "epoch_admission_policy": file(policy),
            "cost_inputs": {},
            "public_bundles": {
                name: _posix(self.data / "bundles" / name)
                for name in ("factors", "sector", "style")
            },
        }

    def rewrite_resolved(self) -> None:
        self.resolved_path.write_bytes(json.dumps(self.resolved).encode())

    # -- predecessors ------------------------------------------------------------------
    def _selected_predecessor(self) -> dict[str, Any]:
        old = self.data / "old"
        native = self.data / "native"
        files: dict[str, Path] = {
            "contract_path": _put(old / "contract.json", "old contract"),
            "financial_input_contract_path": old / "contract.json",
            "matrixport_holdings_path": _put(native / "holdings.csv", "holdings"),
            "matrixport_manifest_path": Path(
                self.providers["matrixport"]["manifest_path"]
            ),
            "portfolio123_version_evidence_path": _put(
                native / "version.json", "version"
            ),
            "provider_admission_receipt_path": _put(
                old / "ledger" / "receipt.json", "old"
            ),
            "provider_ledger_catalog_path": _put(
                old / "ledger" / "catalog.json", "old"
            ),
            "provider_ledger_manifest_path": _put(
                old / "ledger" / "generation.json", "old"
            ),
        }
        kwargs: dict[str, Any] = {name: _posix(path) for name, path in files.items()}
        kwargs.update(
            {
                "as_of_date": CUTOFF,
                "repository_sha": "0" * 40,
                "source_selection_mode": "IMMUTABLE_GENERATIONS",
                "matrixport_pointer_path": None,
                "matrixport_product_id": "synthetic-product",
            }
        )
        return {
            "builder_kwargs": kwargs,
            "files": {name: _pin(path) for name, path in files.items()},
            "financial_data_cutoff": CUTOFF,
        }

    def _prep_predecessor(self) -> dict[str, Any]:
        old = self.data / "old"
        stale = _posix(
            self.elsewhere / "old-pass"
        )  # a regenerated block is not path-checked

        def pin(name: str) -> dict[str, Any]:
            return _pin(_put(old / name, f"old {name}"))

        plan = {
            "base_source_template_root": _posix(
                _put(old / "base-source" / "x", "x").parent
            ),
            "base_spec_template": pin("base-spec.json"),
            "evidence_mode": "LIVE_ONLY",
            "plan": {"path": stale, "sha256": "0" * 64, "size_bytes": 1},
            "us_factors": {"path": stale, "sha256": "0" * 64, "size_bytes": 1},
        }
        return {
            "schema_version": 1,
            "purpose": "SYNTHETIC",
            "authority_notes": {"cutoff": "synthetic note"},
            "financial_data_cutoff": CUTOFF,
            "evaluation_date": "2026-01-01",
            "base_factor_file": pin("factors.csv"),
            "capital_account_crosswalk": pin("crosswalk.json"),
            "implementation_reconciliation": pin("reconciliation.json"),
            "implementation_return_attribution": pin("return-attribution.json"),
            "calendar_derivation_inputs": pin("calendar-inputs.json"),
            "legacy_panel_root": _posix(_put(old / "legacy" / "x", "x").parent),
            "financial_input_contract": {
                "path": stale,
                "sha256": "0" * 64,
                "size_bytes": 1,
            },
            "retained_verified_generations": {"baseline": {"root": stale}},
            "expected_staged_baseline": {
                "generation_id": "old",
                "catalog_sha256": "0" * 64,
            },
            "plans": {"LIVE_ONLY": plan, "LIVE_PLUS_BACKTEST": copy.deepcopy(plan)},
            "sleeve_facts": {
                "alpaca_generation_root": stale,
                "policy_manifest": {"path": stale, "sha256": "0" * 64, "size_bytes": 1},
                "quantbase_source_dir": _posix(
                    _put(old / "quantbase" / "x", "x").parent
                ),
                "predecessor_data_root": _posix(self.data),
            },
        }

    # -- running the builders ----------------------------------------------------------
    def roots(self) -> tool.Roots:
        return tool.declared_roots(self.resolved, self.repo)

    def selected(
        self, predecessor: dict[str, Any] | None = None, **kw: Any
    ) -> dict[str, Any]:
        return tool.build_selected(
            self.resolved,
            predecessor if predecessor is not None else self.selected_pred,
            roots=self.roots(),
            ledger_dir=self.cycle / "ledger" / "generations" / LEDGER,
            repo_root=self.repo,
            commit=kw.pop("commit", COMMIT),
            **kw,
        )

    def selected_file(self) -> Path:
        out = self.cycle / tool.SELECTED_INPUTS
        if not out.exists():
            _put(out, tool._dumps(self.selected()))
        return out

    def prep(
        self, predecessor: dict[str, Any] | None = None, **kw: Any
    ) -> dict[str, Any]:
        self.selected_file()
        return tool.build_prep_spec(
            self.resolved,
            predecessor if predecessor is not None else self.prep_pred,
            roots=self.roots(),
            repo_root=self.repo,
            ledger_dir=self.cycle / "ledger" / "generations" / LEDGER,
            omit=kw.pop("omit", ()),
            **kw,
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _refusal(code: str, fn: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(tool.PrepareError) as raised:
        fn(*args, **kwargs)
    assert raised.value.code == code, str(raised.value)
    return str(raised.value)


# -- selected-inputs -----------------------------------------------------------------------


def test_selected_rebinds_the_cycle_and_repins_every_file_from_bytes(
    world: World,
) -> None:
    out = world.selected()
    ledger = world.cycle / "ledger" / "generations" / LEDGER
    kwargs = out["builder_kwargs"]
    assert kwargs["repository_sha"] == COMMIT
    assert kwargs["contract_path"] == kwargs["financial_input_contract_path"]
    assert kwargs["contract_path"] == _posix(world.repo / tool.CONTRACT)
    assert kwargs["provider_ledger_manifest_path"] == _posix(ledger / "generation.json")
    assert kwargs["provider_admission_receipt_path"] == _posix(
        ledger / tool.LEDGER_RECEIPT
    )
    assert kwargs["provider_ledger_catalog_path"] == _posix(
        ledger / tool.LEDGER_CATALOG
    )
    assert set(out["files"]) == set(world.selected_pred["files"])
    for name, pin in out["files"].items():
        path = Path(pin["path"])
        assert pin == _pin(path), name  # recomputed from the actual bytes
        assert kwargs[name] == pin["path"]  # the pin and the builder argument agree
    # untouched keys are carried
    assert kwargs["matrixport_product_id"] == "synthetic-product"
    assert kwargs["matrixport_pointer_path"] is None
    assert out["financial_data_cutoff"] == CUTOFF


def test_selected_accepts_only_declared_roots(world: World) -> None:
    outside = _put(world.elsewhere / "holdings.csv", "holdings")
    pred = copy.deepcopy(world.selected_pred)
    pred["builder_kwargs"]["matrixport_holdings_path"] = _posix(outside)
    pred["files"]["matrixport_holdings_path"] = _pin(outside)
    _refusal("PATH_OUTSIDE_ROOTS", world.selected, pred)
    pred["builder_kwargs"]["matrixport_holdings_path"] = (
        _posix(world.data) + "/native/../../elsewhere/holdings.csv"
    )
    _refusal("PATH_ESCAPES_ROOT", world.selected, pred)
    # a path of a foreign operating system is outside whatever the roots are
    pred["builder_kwargs"]["matrixport_holdings_path"] = "Q:\\retired\\holdings.csv"
    _refusal("PATH_OUTSIDE_ROOTS", world.selected, pred)


def test_selected_refuses_a_missing_or_changed_pinned_file(world: World) -> None:
    holdings = Path(world.selected_pred["files"]["matrixport_holdings_path"]["path"])
    holdings.write_bytes(b"holdings drifted")
    message = _refusal("HASH_MISMATCH", world.selected)
    assert "matrixport_holdings_path" in message
    holdings.unlink()
    _refusal("MISSING", world.selected)


def test_selected_refuses_a_pin_that_names_another_path_than_its_argument(
    world: World,
) -> None:
    pred = copy.deepcopy(world.selected_pred)
    other = _put(world.data / "native" / "other.csv", "holdings")
    pred["files"]["matrixport_holdings_path"] = _pin(other)
    _refusal("PIN_PATH_MISMATCH", world.selected, pred)


def test_selected_refuses_a_stale_cutoff_unless_rebound_with_new_files(
    world: World,
) -> None:
    pred = copy.deepcopy(world.selected_pred)
    pred["financial_data_cutoff"] = "2025-12-26"
    pred["builder_kwargs"]["as_of_date"] = "2025-12-26"
    _refusal("STALE_PREDECESSOR", world.selected, pred)
    fresh = _put(world.data / "native" / "holdings-new.csv", "holdings of this cutoff")
    spec = f"matrixport_holdings_path=data:native/holdings-new.csv@{_sha(fresh.read_bytes())}"
    out = world.selected(pred, rebind_cutoff=True, file_overrides=[spec])
    assert (
        out["financial_data_cutoff"] == CUTOFF
        and out["builder_kwargs"]["as_of_date"] == CUTOFF
    )
    assert out["files"]["matrixport_holdings_path"] == _pin(fresh)


def test_selected_overrides_are_pinned_and_limited_to_carried_inputs(
    world: World,
) -> None:
    fresh = _put(world.data / "native" / "holdings-new.csv", "new")
    wrong = "0" * 64
    _refusal(
        "HASH_MISMATCH",
        world.selected,
        file_overrides=[
            f"matrixport_holdings_path=data:native/holdings-new.csv@{wrong}"
        ],
    )
    good = _sha(fresh.read_bytes())
    for name in ("contract_path", "repository_sha", "not_a_file"):
        _refusal(
            "OVERRIDE_NOT_ALLOWED",
            world.selected,
            file_overrides=[f"{name}=data:native/holdings-new.csv@{good}"],
        )
    _refusal(
        "UNKNOWN_ROOT",
        world.selected,
        file_overrides=[
            f"matrixport_holdings_path=other:native/holdings-new.csv@{good}"
        ],
    )
    _refusal(
        "PATH_ESCAPES_ROOT",
        world.selected,
        file_overrides=[f"matrixport_holdings_path=data:../elsewhere/x@{good}"],
    )


def test_selected_refuses_a_native_manifest_of_another_provider_generation(
    world: World,
) -> None:
    pred = copy.deepcopy(world.selected_pred)
    stale = _put(world.data / "native" / "stale-generation.json", "an older generation")
    pred["builder_kwargs"]["matrixport_manifest_path"] = _posix(stale)
    pred["files"]["matrixport_manifest_path"] = _pin(stale)
    message = _refusal("STALE_PREDECESSOR_GENERATION", world.selected, pred)
    assert "matrixport" in message


def test_selected_shape_and_commit_are_checked(world: World) -> None:
    pred = copy.deepcopy(world.selected_pred)
    pred["extra"] = 1
    _refusal("PREDECESSOR_SHAPE", world.selected, pred)
    pred = copy.deepcopy(world.selected_pred)
    del pred["builder_kwargs"]["repository_sha"]
    _refusal("MISSING_KWARG", world.selected, pred)
    _refusal("COMMIT", world.selected, commit="abc")
    _refusal("COMMIT", world.selected, commit=COMMIT.upper())


def test_two_ledger_generations_are_ambiguous_and_one_may_be_named(
    world: World,
) -> None:
    other = world.cycle / "ledger" / "generations" / ("2" * 64)
    _put(other / "generation.json", {"generation_id": "2" * 64})
    message = _refusal(
        "AMBIGUOUS_GENERATION",
        tool._ledger_generation,
        world.resolved,
        world.roots(),
        None,
    )
    assert "2 candidates" in message
    chosen = tool._ledger_generation(
        world.resolved, world.roots(), world.cycle / "ledger" / "generations" / LEDGER
    )
    assert chosen.name == LEDGER
    # a generation that is not one of the built ones, or lacks a ledger file, is refused
    _refusal(
        "MISSING",
        tool._ledger_generation,
        world.resolved,
        world.roots(),
        world.cycle / "ledger" / "generations" / ("3" * 64),
    )
    _refusal("MISSING", tool._ledger_generation, world.resolved, world.roots(), other)
    elsewhere = _put(
        world.data / "ledger" / "generations" / LEDGER / "generation.json", "{}"
    )
    _refusal(
        "PATH_OUTSIDE_ROOTS",
        tool._ledger_generation,
        world.resolved,
        world.roots(),
        elsewhere.parent,
    )


def test_a_generation_that_names_another_id_than_its_directory_is_refused(
    world: World,
) -> None:
    manifest = world.cycle / "ledger" / "generations" / LEDGER / "generation.json"
    manifest.write_bytes(json.dumps({"generation_id": "9" * 64}).encode())
    _refusal(
        "ID_MISMATCH", tool._ledger_generation, world.resolved, world.roots(), None
    )


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_a_link_inside_a_root_is_not_a_plain_path(world: World) -> None:
    real = world.data / "native"
    link = world.data / "linked"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need a privilege here")
    pred = copy.deepcopy(world.selected_pred)
    pred["builder_kwargs"]["matrixport_holdings_path"] = (
        _posix(world.data) + "/linked/holdings.csv"
    )
    _refusal("PATH_ESCAPES_ROOT", world.selected, pred)


# -- preparation spec ----------------------------------------------------------------------


def _calls() -> tuple[list[tuple[Path, str, str]], Any]:
    seen: list[tuple[Path, str, str]] = []

    def check(path: Path, cutoff: str, evaluated_on: str) -> None:
        assert path.is_file()
        seen.append((path, cutoff, evaluated_on))

    return seen, check


def test_prep_spec_derives_every_id_and_repins_every_file(world: World) -> None:
    spec = world.prep()
    cycle, ledger = world.cycle, world.cycle / "ledger" / "generations" / LEDGER
    assert (
        spec["financial_data_cutoff"] == CUTOFF and spec["evaluation_date"] == EVALUATED
    )
    assert spec["provider_ledger_generation_root"] == _posix(ledger)
    retained = spec["retained_verified_generations"]
    assert Path(retained["baseline"]["root"]).name == BASELINE
    assert Path(retained["history"]["root"]).name == HISTORY
    assert Path(retained["attribution"]["root"]).name == ATTRIBUTION
    for entry in retained.values():
        assert entry["manifest"] == _pin(Path(entry["root"]) / "generation.json")
    assert spec["expected_staged_baseline"] == {
        "catalog_sha256": _sha(b"baseline catalog"),
        "generation_id": BASELINE,
    }
    assert spec["source_selection"] == _pin(cycle / tool.SELECTED_INPUTS)
    assert spec["calendar_authority"] == _pin(cycle / tool.CALENDAR_AUTHORITY)
    assert spec["epoch_admission_policy"] == _pin(world.data / "epoch-policy.json")
    assert spec["financial_input_contract"] == _pin(world.repo / tool.CONTRACT)
    assert spec["epoch_membership_census"] == spec["financial_input_contract"]
    assert spec["provider_ledger_spec"] == _pin(cycle / tool.LEDGER_SPEC)
    assert spec["baseline_spec_template"] == _pin(cycle / tool.BASELINE_SPEC)
    assert spec["attribution_spec_template"] == _pin(cycle / tool.ATTRIBUTION_SPEC)
    assert spec["dependence_selection_template"] == _pin(
        cycle / tool.DEPENDENCE_SELECTION
    )
    assert spec["epoch_crosswalk_template"] == _pin(world.repo / tool.CROSSWALK)
    # the public bundles come from the cycle's bundle roots, one bundle each
    for plan in spec["plans"].values():
        factors = world.data / "bundles" / "factors" / ("f" * 64)
        assert plan["us_factors"] == _pin(factors / tool.FACTOR_FILES["us_factors"])
        assert plan["style_returns"] == _pin(
            world.data / "bundles" / "style" / ("7" * 64) / tool.STYLE_FILE
        )
        assert plan["sector_returns"] == _pin(
            world.data / "bundles" / "sector" / ("5" * 64) / tool.SECTOR_FILE
        )
        assert plan["plan"] == _pin(world.repo / tool.PLAN)
        assert plan["evidence_mode"] == "LIVE_ONLY"
        assert plan["base_spec_template"]["sha256"] == _sha(b"old base-spec.json")
    sleeve = spec["sleeve_facts"]
    assert sleeve["alpaca_generation_root"] == world.providers["alpaca"]["root"]
    assert sleeve["policy_manifest"] == _pin(world.repo / tool.POLICY_MANIFEST)
    # carried inputs are verified and kept; per-cycle inputs are absent
    assert spec["base_factor_file"]["sha256"] == _sha(b"old factors.csv")
    assert spec["authority_notes"] == {"cutoff": "synthetic note"}
    for key in tool.PER_CYCLE_OPTIONAL:
        assert key not in spec
    # every key is one the attended preparation reads
    assert set(spec) <= tool.REGENERATED | tool.CARRIED_REQUIRED | tool.CARRIED_OPTIONAL


def test_prep_spec_pins_each_per_cycle_input_the_run_names(world: World) -> None:
    gap = _put(world.data / "gap" / "fills.json", "fills")
    audit = _put(world.data / "gap" / "audit.json", "audit")
    perf = _put(world.data / "gap" / "perf.csv", "perf")
    pins = [
        f"vendor_gap_fills=data:gap/fills.json@{_sha(gap.read_bytes())}",
        f"model_vs_actual_input=data:gap/audit.json@{_sha(audit.read_bytes())}",
        f"implementation_model_book_perf=data:gap/perf.csv@{_sha(perf.read_bytes())}",
    ]
    spec = world.prep(pins=pins)
    assert spec["vendor_gap_fills"] == _pin(gap)
    assert spec["model_vs_actual_input"] == _pin(audit)
    assert spec["implementation_model_book_perf"] == _pin(perf)
    assert "implementation_model_book_perf" not in world.prep()
    assert "weekly_gap_fill_spec" not in spec  # absent means the key is omitted
    _refusal(
        "HASH_MISMATCH",
        world.prep,
        pins=[f"vendor_gap_fills=data:gap/fills.json@{'0' * 64}"],
    )
    _refusal("USAGE", world.prep, pins=["vendor_gap_fills=data:gap/fills.json"])
    _refusal(
        "OVERRIDE_NOT_ALLOWED",
        world.prep,
        pins=[f"financial_data_cutoff=data:gap/fills.json@{_sha(gap.read_bytes())}"],
    )


def test_a_per_cycle_input_carried_from_the_predecessor_must_be_redeclared(
    world: World,
) -> None:
    pred = copy.deepcopy(world.prep_pred)
    pred["weekly_gap_fill_spec"] = _pin(_put(world.old / "weekly.json", "last cycle"))
    message = _refusal("STALE_CARRIED_INPUT", world.prep, pred)
    assert "weekly_gap_fill_spec" in message
    assert "weekly_gap_fill_spec" not in world.prep(pred, omit=["weekly_gap_fill_spec"])
    fresh = _put(world.data / "gap" / "weekly.json", "this cycle")
    spec = world.prep(
        pred,
        pins=[f"weekly_gap_fill_spec=data:gap/weekly.json@{_sha(fresh.read_bytes())}"],
    )
    assert spec["weekly_gap_fill_spec"] == _pin(fresh)
    _refusal("OVERRIDE_NOT_ALLOWED", world.prep, pred, omit=["base_factor_file"])


def test_two_candidates_for_a_generation_id_are_refused(world: World) -> None:
    other = world.cycle / "baseline" / "generations" / ("c" * 64)
    _put(other / "generation.json", {"generation_id": "c" * 64})
    _refusal("AMBIGUOUS_GENERATION", world.prep)
    spec = world.prep(ids={"baseline": BASELINE})
    assert spec["expected_staged_baseline"]["generation_id"] == BASELINE
    _refusal("MISSING", world.prep, ids={"baseline": "d" * 64})
    # a second history generation, a second bundle and an unnamed one are ambiguous too
    _put(world.cycle / "pre" / "history" / ("e" * 64) / "generation.json", {})
    _refusal("AMBIGUOUS_GENERATION", world.prep, ids={"baseline": BASELINE})
    _refusal("MISSING", world.prep, ids={"baseline": BASELINE, "history": "f" * 64})


def test_a_second_bundle_is_ambiguous_until_named(world: World) -> None:
    world.bundle("factors", "e" * 64, tuple(tool.FACTOR_FILES.values()))
    _refusal("AMBIGUOUS_GENERATION", world.prep)
    spec = world.prep(bundle_ids={"factors": "e" * 64})
    assert "e" * 64 in spec["plans"]["LIVE_ONLY"]["us_factors"]["path"]
    _refusal(
        "MISSING",
        world.prep,
        bundle_ids={"factors": "e" * 64},
        bundles={"style": "no-such-bundle"},
    )


def test_a_bundle_naming_another_id_than_its_directory_is_refused(world: World) -> None:
    manifest = world.data / "bundles" / "style" / ("7" * 64) / tool.MANIFEST_FILE
    manifest.write_bytes(json.dumps({"bundle_id": "8" * 64}).encode())
    _refusal("ID_MISMATCH", world.prep)


def test_a_stale_predecessor_generation_is_refused(world: World) -> None:
    other = dict(world.providers["alpaca"])
    world.write_ledger_spec(
        providers={
            **{
                n: {
                    "generation_id": r["generation_id"],
                    "manifest_sha256": r["manifest_sha256"],
                }
                for n, r in world.providers.items()
            },
            "alpaca": {
                "generation_id": "old" + other["generation_id"][3:],
                "manifest_sha256": other["manifest_sha256"],
            },
        }
    )
    message = _refusal("STALE_LEDGER_SPEC", world.prep)
    assert "alpaca" in message
    world.write_ledger_spec(as_of_date="2025-12-26")
    _refusal("STALE_LEDGER_SPEC", world.prep)
    world.write_ledger_spec(output_root=_posix(world.data / "elsewhere-ledger"))
    _refusal("STALE_LEDGER_SPEC", world.prep)
    world.write_ledger_spec()
    pred = copy.deepcopy(world.prep_pred)
    pred["financial_data_cutoff"] = "2025-12-26"
    _refusal("STALE_PREDECESSOR", world.prep, pred)
    assert world.prep(pred, rebind_cutoff=True)["financial_data_cutoff"] == CUTOFF


def test_selected_inputs_and_baseline_spec_must_bind_this_cycle(world: World) -> None:
    (world.cycle / tool.BASELINE_SPEC).write_bytes(
        json.dumps({"as_of_date": "2025-12-26"}).encode()
    )
    _refusal("STALE_BASELINE_SPEC", world.prep)
    (world.cycle / tool.BASELINE_SPEC).write_bytes(
        json.dumps({"as_of_date": CUTOFF}).encode()
    )
    selected = world.selected_file()
    document = json.loads(selected.read_bytes())
    document["financial_data_cutoff"] = "2025-12-26"
    selected.write_bytes(json.dumps(document).encode())
    _refusal("STALE_SELECTED_INPUTS", world.prep)
    document["financial_data_cutoff"] = CUTOFF
    document["builder_kwargs"]["provider_ledger_manifest_path"] = _posix(
        world.old / "ledger" / "generation.json"
    )
    selected.write_bytes(json.dumps(document).encode())
    _refusal("STALE_SELECTED_INPUTS", world.prep)


def test_a_missing_input_is_refused(world: World) -> None:
    world.selected_file()
    (world.cycle / tool.CALENDAR_AUTHORITY).unlink()
    _refusal("MISSING", world.prep)
    _put(world.cycle / tool.CALENDAR_AUTHORITY, "{}")
    world.resolved.pop("epoch_admission_policy")
    _refusal("MISSING", world.prep)


def test_a_carried_input_that_changed_or_left_its_root_is_refused(world: World) -> None:
    carried = Path(world.prep_pred["base_factor_file"]["path"])
    carried.write_bytes(b"drifted")
    assert "base_factor_file" in _refusal("HASH_MISMATCH", world.prep)
    carried.unlink()
    _refusal("MISSING", world.prep)
    _put(carried, "old factors.csv")
    pred = copy.deepcopy(world.prep_pred)
    outside = _put(world.elsewhere / "factors.csv", "x")
    pred["base_factor_file"] = _pin(outside)
    _refusal("PATH_OUTSIDE_ROOTS", world.prep, pred)
    pred = copy.deepcopy(world.prep_pred)
    pred["legacy_panel_root"] = _posix(world.elsewhere)
    _refusal("PATH_OUTSIDE_ROOTS", world.prep, pred)
    pred = copy.deepcopy(world.prep_pred)
    pred["plans"]["LIVE_ONLY"]["base_spec_template"]["sha256"] = "0" * 64
    assert "plans.LIVE_ONLY.base_spec_template" in _refusal(
        "HASH_MISMATCH", world.prep, pred
    )


def test_carried_inputs_may_be_replaced_by_pinned_files_and_dirs(world: World) -> None:
    fresh = _put(world.data / "new" / "factors.csv", "new factors")
    new_dir = _put(world.data / "new" / "legacy" / "x", "x").parent
    new_qb = _put(world.data / "new" / "quantbase" / "x", "x").parent
    carried = Path(world.prep_pred["base_factor_file"]["path"])
    carried.write_bytes(
        b"drifted"
    )  # the changed predecessor file no longer matters once replaced
    spec = world.prep(
        pins=[f"base_factor_file=data:new/factors.csv@{_sha(fresh.read_bytes())}"],
        dirs=[
            "legacy_panel_root=data:new/legacy",
            "sleeve_facts.quantbase_source_dir=data:new/quantbase",
        ],
    )
    assert spec["base_factor_file"] == _pin(fresh)
    assert spec["legacy_panel_root"] == _posix(new_dir)
    assert spec["sleeve_facts"]["quantbase_source_dir"] == _posix(new_qb)
    _refusal("OVERRIDE_NOT_ALLOWED", world.prep, dirs=["nothing=data:new"])
    _refusal(
        "OVERRIDE_NOT_ALLOWED",
        world.prep,
        dirs=["sleeve_facts.policy_manifest=data:new"],
    )
    _refusal("MISSING", world.prep, dirs=["legacy_panel_root=data:new/absent"])
    _refusal(
        "USAGE", world.prep, dirs=[f"legacy_panel_root=data:new/legacy@{'0' * 64}"]
    )


def test_the_predecessor_shape_is_checked(world: World) -> None:
    for change, key in (("extra", "surprise"), ("drop", "base_factor_file")):
        pred = copy.deepcopy(world.prep_pred)
        if change == "extra":
            pred[key] = 1
        else:
            del pred[key]
        _refusal("PREDECESSOR_SHAPE", world.prep, pred)
    pred = copy.deepcopy(world.prep_pred)
    pred["plans"]["LIVE_ONLY"]["surprise"] = 1
    _refusal("PREDECESSOR_SHAPE", world.prep, pred)
    pred = copy.deepcopy(world.prep_pred)
    pred["sleeve_facts"]["surprise"] = 1
    _refusal("PREDECESSOR_SHAPE", world.prep, pred)
    pred = copy.deepcopy(world.prep_pred)
    pred["sleeve_facts"]["unknown_generation_root"] = _posix(world.data)
    _refusal("PROVIDER_UNKNOWN", world.prep, pred)
    pred = copy.deepcopy(world.prep_pred)
    del pred["sleeve_facts"]["alpaca_generation_root"]
    _refusal("PREDECESSOR_SHAPE", world.prep, pred)


def test_a_spec_naming_another_passes_generation_is_refused_by_the_driver(
    world: World, tmp_path: Path
) -> None:
    class Run:
        def expand(self, text: str) -> str:
            return {"{history_id}": HISTORY, "{baseline_id}": BASELINE}.get(text, text)

    spec_path = tmp_path / "prep-spec.json"
    stage = {
        "id": "attended",
        "bound_ids": [
            [str(spec_path), "{history_id}"],
            [str(spec_path), "{baseline_id}"],
        ],
    }
    spec_path.write_bytes(tool._dumps(world.prep()))
    driver.check_bound_ids(Run(), stage)  # the built spec binds this cycle's ids
    stale = spec_path.read_text(encoding="utf-8").replace(HISTORY, "9" * 64)
    spec_path.write_text(stale, encoding="utf-8")
    with pytest.raises(driver.Refusal, match="stale input"):
        driver.check_bound_ids(Run(), stage)


# -- writing and the command line ------------------------------------------------------------


def test_the_spec_is_written_create_only_and_checked(world: World) -> None:
    seen, check = _calls()
    out = world.tmp / "out" / "prep-spec.json"
    result = tool.write_prep_spec(world.prep(), out, check)
    assert seen == [(out.with_name(out.name + ".candidate"), CUTOFF, EVALUATED)]
    assert result["sha256"] == _sha(out.read_bytes())
    assert out.read_bytes().endswith(b"\n") and b"\r" not in out.read_bytes()
    tool.write_prep_spec(world.prep(), out, check)  # an identical re-run is accepted
    changed = world.prep()
    changed["evaluation_date"] = "2026-02-02"
    _refusal("OUTPUT_EXISTS", tool.write_prep_spec, changed, out, check)
    assert out.read_bytes() == tool._dumps(world.prep())


def test_a_spec_the_attended_check_refuses_is_renamed(world: World) -> None:
    def check(path: Path, cutoff: str, evaluated_on: str) -> None:
        raise ValueError("selected provider ledger replay differs")

    out = world.tmp / "out" / "prep-spec.json"
    message = _refusal("SELF_CHECK", tool.write_prep_spec, world.prep(), out, check)
    assert "ValueError" in message and "replay differs" in message
    assert not out.exists() and out.with_name(out.name + ".rejected").is_file()


def _cli(world: World, *args: str) -> list[str]:
    return [
        args[0],
        "--resolved-cycle",
        str(world.resolved_path),
        "--repo-root",
        str(world.repo),
        *args[1:],
    ]


def test_cli_selected_writes_the_file_and_plan_writes_nothing(
    world: World, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    predecessor = _put(tmp_path / "pred-selected.json", world.selected_pred)
    out = world.cycle / tool.SELECTED_INPUTS
    args = _cli(
        world,
        "selected",
        "--predecessor",
        str(predecessor),
        "--commit",
        COMMIT,
        "--out",
        str(out),
    )
    assert tool.main([*args, "--plan"]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert not out.exists()
    assert tool.main(args) == 0
    written = json.loads(capsys.readouterr().out)
    assert written["sha256"] == planned["sha256"] == _sha(out.read_bytes())
    assert json.loads(out.read_bytes()) == world.selected()
    assert tool.main(args) == 0  # create-only: identical bytes are accepted
    capsys.readouterr()
    out.write_bytes(b"{}")
    assert tool.main(args) == 1
    assert "FAIL: OUTPUT_EXISTS" in capsys.readouterr().err


def test_cli_refusals_exit_one_with_the_code_and_write_nothing(
    world: World, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    pred = copy.deepcopy(world.selected_pred)
    Path(pred["files"]["matrixport_holdings_path"]["path"]).write_bytes(b"changed")
    predecessor = _put(tmp_path / "pred-selected.json", pred)
    out = world.cycle / tool.SELECTED_INPUTS
    code = tool.main(
        _cli(
            world,
            "selected",
            "--predecessor",
            str(predecessor),
            "--commit",
            COMMIT,
            "--out",
            str(out),
        )
    )
    assert code == 1 and not out.exists()
    assert "release-prepare-inputs: FAIL: HASH_MISMATCH" in capsys.readouterr().err
    code = tool.main(
        _cli(
            world,
            "selected",
            "--predecessor",
            str(predecessor),
            "--commit",
            COMMIT,
            "--out",
            str(world.elsewhere / "x.json"),
        )
    )
    assert code == 1 and "PATH_OUTSIDE_ROOTS" in capsys.readouterr().err
    bad = world.tmp / "resolved-bad.json"
    bad.write_bytes(json.dumps({**world.resolved, "schema_version": "other"}).encode())
    code = tool.main(
        [
            "selected",
            "--resolved-cycle",
            str(bad),
            "--predecessor",
            str(predecessor),
            "--commit",
            COMMIT,
            "--out",
            str(out),
        ]
    )
    assert code == 1 and "RESOLVED_VERSION" in capsys.readouterr().err
    code = tool.main(
        _cli(
            world,
            "selected",
            "--predecessor",
            str(predecessor),
            "--commit",
            COMMIT,
            "--out",
            str(out),
        )
        + ["--resolved-cycle-sha256", "0" * 64]
    )
    assert (
        code == 1 and "resolved cycle differs from its pin" in capsys.readouterr().err
    )
    broken = world.tmp / "resolved-shape.json"
    broken.write_bytes(json.dumps({**world.resolved, "providers": 3}).encode())
    code = tool.main(
        [
            "selected",
            "--resolved-cycle",
            str(broken),
            "--predecessor",
            str(predecessor),
            "--commit",
            COMMIT,
            "--out",
            str(out),
        ]
    )
    assert code == 1 and "RESOLVED_SHAPE" in capsys.readouterr().err
    with pytest.raises(SystemExit) as usage:
        tool.main(["selected"])
    assert usage.value.code == 2


def test_cli_prep_spec_runs_the_attended_planning_path_and_renames_a_refused_spec(
    world: World, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The real attended preparation validates the spec; a synthetic cycle cannot satisfy it."""

    world.selected_file()
    predecessor = _put(tmp_path / "pred-prep.json", world.prep_pred)
    out = world.cycle / "out" / "prep-spec.json"
    args = _cli(
        world, "prep-spec", "--predecessor-spec", str(predecessor), "--out", str(out)
    )
    code = tool.main(args)
    err = capsys.readouterr().err
    assert code == 1 and "SELF_CHECK" in err and "AttendedPreparationError" in err
    # the consumer verified every pin of the spec and failed later, on the synthetic generations
    assert "pin differs" not in err and "is unavailable" not in err
    assert not out.exists() and out.with_name(out.name + ".rejected").is_file()
    out.with_name(out.name + ".rejected").unlink()
    assert tool.main([*args, "--check", "none"]) == 0
    assert json.loads(out.read_bytes()) == world.prep()
    capsys.readouterr()
    other = args[:-1] + [
        str(world.cycle / "out" / "plan.json"),
        "--check",
        "none",
        "--plan",
    ]
    assert tool.main(other) == 0
    assert not (world.cycle / "out" / "plan.json").exists()
    assert json.loads(capsys.readouterr().out)["sha256"] == _sha(out.read_bytes())


def test_cli_prep_spec_accepts_the_named_inputs(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world.selected_file()
    gap = _put(world.data / "gap" / "fills.json", "fills")
    predecessor = _put(tmp_path / "pred-prep.json", world.prep_pred)
    out = world.cycle / "out.json"
    code = tool.main(
        _cli(
            world,
            "prep-spec",
            "--predecessor-spec",
            str(predecessor),
            "--out",
            str(out),
            "--check",
            "none",
            "--baseline-id",
            BASELINE,
            "--history-id",
            HISTORY,
            "--attribution-id",
            ATTRIBUTION,
            "--factors-bundle",
            "factors",
            "--factors-bundle-id",
            "f" * 64,
            "--sector-bundle",
            "sector",
            "--style-bundle",
            "style",
            "--selected-inputs",
            str(world.cycle / tool.SELECTED_INPUTS),
            "--calendar-authority",
            str(world.cycle / tool.CALENDAR_AUTHORITY),
            "--ledger-generation-dir",
            str(world.cycle / "ledger" / "generations" / LEDGER),
            "--pin",
            f"vendor_gap_fills=data:gap/fills.json@{_sha(gap.read_bytes())}",
            "--dir",
            "legacy_panel_root=data:old/legacy",
        )
    )
    assert code == 0, capsys.readouterr().err
    assert json.loads(out.read_bytes())["vendor_gap_fills"] == _pin(gap)


def test_the_resolved_policy_pin_is_verified_before_repinning(world: World) -> None:
    (world.data / "epoch-policy.json").write_bytes(b"policy changed after resolve")
    assert "epoch admission policy" in _refusal("HASH_MISMATCH", world.prep)


def test_the_spec_is_not_published_until_its_check_passed(world: World) -> None:
    out = world.tmp / "out" / "prep-spec.json"
    seen: list[bool] = []

    def check(path: Path, cutoff: str, evaluated_on: str) -> None:
        seen.append(out.exists())
        assert path.is_file() and path != out

    tool.write_prep_spec(world.prep(), out, check)
    assert seen == [False] and out.is_file()
    assert not out.with_name(out.name + ".candidate").exists()


def test_the_selected_inputs_are_read_by_the_real_attended_consumer(
    world: World,
) -> None:
    import portfolio_attended_preparation as consumer

    authorities = consumer.selected_history_authorities(world.selected()["files"])
    assert set(authorities) == {
        "provider_admission_receipt_path",
        "financial_input_contract_path",
        "provider_ledger_manifest_path",
        "provider_ledger_catalog_path",
        "portfolio123_version_evidence_path",
    }
    assert all(path.is_file() for path in authorities.values())


def test_prep_spec_reads_the_crosswalk_and_policy_from_the_release_inputs_root(
    world: World,
) -> None:
    from quant_engine.core.release_inputs import build_manifest

    inputs = world.tmp / "release-inputs"
    for relative in (tool.CROSSWALK, tool.POLICY_MANIFEST):
        (inputs / relative).parent.mkdir(parents=True, exist_ok=True)
        (inputs / relative).write_bytes((world.repo / relative).read_bytes())
        (world.repo / relative).unlink()  # the checkout no longer carries them
    _, digest = build_manifest(
        inputs, [tool.CROSSWALK, tool.POLICY_MANIFEST], cutoff=CUTOFF
    )
    world.selected_file()
    spec = tool.build_prep_spec(
        world.resolved,
        world.prep_pred,
        roots=tool.declared_roots(world.resolved, world.repo, inputs),
        repo_root=world.repo,
        ledger_dir=world.cycle / "ledger" / "generations" / LEDGER,
        release_inputs_root=inputs,
        release_inputs_manifest_sha256=digest,
    )
    assert spec["epoch_crosswalk_template"] == _pin(inputs / tool.CROSSWALK)
    assert spec["sleeve_facts"]["policy_manifest"] == _pin(
        inputs / tool.POLICY_MANIFEST
    )
    assert _refusal(
        "RELEASE_INPUTS",
        tool.build_prep_spec,
        world.resolved,
        world.prep_pred,
        roots=tool.declared_roots(world.resolved, world.repo, inputs),
        repo_root=world.repo,
        ledger_dir=world.cycle / "ledger" / "generations" / LEDGER,
        release_inputs_root=inputs,
    )


def test_a_directory_field_refuses_pin_and_a_file_field_refuses_dir(
    world: World,
) -> None:
    fresh = _put(world.data / "new" / "x.csv", "x")
    sha = _sha(fresh.read_bytes())
    _refusal(
        "OVERRIDE_SHAPE",
        world.prep,
        pins=[f"sleeve_facts.quantbase_source_dir=data:new/x.csv@{sha}"],
    )
    _refusal("OVERRIDE_SHAPE", world.prep, dirs=["base_factor_file=data:new"])


def test_release_inputs_for_another_cutoff_are_refused(world: World) -> None:
    from quant_engine.core.release_inputs import build_manifest

    inputs = world.tmp / "release-inputs-other"
    for relative in (tool.CROSSWALK, tool.POLICY_MANIFEST):
        (inputs / relative).parent.mkdir(parents=True, exist_ok=True)
        (inputs / relative).write_bytes((world.repo / relative).read_bytes())
    _, digest = build_manifest(
        inputs, [tool.CROSSWALK, tool.POLICY_MANIFEST], cutoff="2001-01-01"
    )
    _refusal(
        "CUTOFF_MISMATCH",
        world.prep,
        release_inputs_root=inputs,
        release_inputs_manifest_sha256=digest,
    )


def test_the_shipped_prep_spec_stage_passes_the_release_inputs_to_the_tool() -> None:
    graph = json.loads(
        (tool.REPO_ROOT / "ops/mvp2-release/release-stages.json").read_text(
            encoding="utf-8"
        )
    )
    argv = next(s for s in graph["stages"] if s["id"] == "prep-spec")["argv"]
    assert "--release-inputs-root" in argv
    assert argv[argv.index("--release-inputs-root") + 1] == "{release_inputs_root}"
    assert "--release-inputs-manifest-sha256" in argv
    assert tool._parser().parse_args(  # the tool accepts both flags
        [
            "prep-spec",
            "--resolved-cycle",
            "x",
            "--out",
            "y",
            "--predecessor-spec",
            "z",
            "--release-inputs-root",
            "r",
            "--release-inputs-manifest-sha256",
            "0" * 64,
        ]
    ).release_inputs_root == Path("r")
