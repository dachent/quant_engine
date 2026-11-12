"""Derive tool for the model-vs-actual audit input: determinism and refusals."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from typing import Any
import zipfile

from jsonschema import Draft202012Validator
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import portfolio_model_vs_actual as mva  # noqa: E402
import portfolio_model_vs_actual_derive as derive  # noqa: E402

SCHEMAS = ROOT / "schemas" / "portfolio"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _jb(obj: Any) -> bytes:
    return json.dumps(obj, indent=1).encode()


def _comp(cid: str, pp: float) -> dict[str, Any]:
    return {
        "component_id": cid,
        "label": cid,
        "pp": pp,
        "usd": None,
        "origin_tag": "D",
        "estimated": False,
        "uncertainty_pp_1sigma": None,
        "source_ref": {"file": "f", "sha256": "0" * 64, "columns": ["c"]},
        "replacing_pull": None,
        "sub_components": [],
    }


def _win(wid: str, acc: float, mod: float, comps: list[Any]) -> dict[str, Any]:
    return {
        "window_id": wid,
        "label": wid,
        "kind": "ALIGNED",
        "start": "2026-01-01",
        "end": "2026-08-03",
        "days": 10,
        "account_return_pct": acc,
        "model_return_pct": mod,
        "model_currency": "USD",
        "account_currency": "USD",
        "audit_gap_pp": round(acc - mod, 4),
        "view": "V",
        "memo": [],
        "components": comps,
        "capital_base_usd": 100.0,
        "capital_base_basis": "B",
    }


def _prev() -> dict[str, Any]:
    pin = {"path": "p", "sha256": "0" * 64}
    return {
        "schema_version": 1,
        "artifact": "model-vs-actual-audit-bridges",
        "audit_written": "2026-01-01",
        "extra_pinned_files": {},
        "source_manifest": pin,
        "terms_csv": pin,
        "pull_list": pin,
        "closure_tolerance_pp": 0.02,
        "pulls": [{"pull_id": "P1"}, {"pull_id": "P2"}],
        "strategies": [
            {
                "strategy_id": "s1",
                "windows": [_win("W0", 10.0, 8.0, [_comp("other", 2.0)])],
                "unavailable": [{"term": "t", "replacing_pull": "P1"}],
                "notes": ["old"],
            },
            {
                "strategy_id": "s2",
                "primary_window_id": "W2",
                "windows": [_win("W2", 10.0, 8.0, [_comp("other", 2.0)])],
                "unavailable": [],
                "notes": ["old"],
            },
        ],
    }


def _zip(prices: dict[str, float]) -> bytes:
    days = sorted(prices)
    stamps = [
        int(dt.datetime.fromisoformat(d + "T14:30:00+00:00").timestamp()) for d in days
    ]
    chart = {
        "chart": {
            "result": [
                {
                    "timestamp": stamps,
                    "indicators": {
                        "adjclose": [{"adjclose": [prices[d] for d in days]}]
                    },
                }
            ]
        }
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SPXL.json", json.dumps(chart))
    return buf.getvalue()


def _recipe() -> dict[str, Any]:
    cols = [
        {"component_id": "execution", "label": "Exec", "sources": ["a"]},
        {"component_id": "other", "label": "Rest", "sources": ["b", "c"]},
    ]
    return {
        "base_input": "prev",
        "ledger_input": "ledger",
        "completed_pulls": ["P1"],
        "strategies": [
            {
                "strategy_id": "s1",
                "ops": [
                    {
                        "op": "window_bridge",
                        "input": "bridge",
                        "drop_windows": [],
                        "model_key": "model_usd_pct",
                        "view": "VIEW",
                        "account_id": "acct",
                        "capital_basis": "BASIS",
                        "components": cols,
                        "windows": [
                            {
                                "window_id": "F",
                                "label": "Full",
                                "kind": "ALIGNED",
                                "key": "FULL",
                                "start": "2026-01-01",
                                "end": "2026-11-06",
                            }
                        ],
                    },
                    {
                        "op": "extend_epoch",
                        "input": "epoch",
                        "epoch_path": "e",
                        "base_window": "W0",
                        "new_window": "E1",
                        "full_window": "W0_cut",
                        "label": "Epoch",
                        "start": "2026-08-04",
                        "end": "2026-08-13",
                        "account_key": "account_pct",
                        "model_key": "model_pct",
                        "days_from_source": True,
                        "linking_label": "Link",
                        "components": {
                            "list_key": "components",
                            "id_key": "component",
                            "pp_key": "pp",
                            "tag_key": "tag",
                            "label_prefix": "L: ",
                            "columns_prefix": "e.",
                        },
                    },
                    {"op": "clear_unavailable"},
                    {"op": "set_notes", "notes": ["new"]},
                ],
            },
            {
                "strategy_id": "s2",
                "ops": [
                    {
                        "op": "adjclose_extension",
                        "input": "zip",
                        "member": "SPXL.json",
                        "base_window": "W2",
                        "after": "2026-08-03",
                        "start": "2026-08-04",
                        "end": "2026-08-06",
                        "new_window": "W3",
                        "new_label": "Epoch",
                        "full_window": "W_full",
                        "full_label": "Aligned",
                        "replacing_pull": "P2",
                        "column": "adj",
                        "epoch_label": "E",
                        "full_epoch_label": "FE",
                        "linking_label": "Link",
                        "linking_columns": ["W2", "W3"],
                    },
                    {"op": "append_notes", "notes": ["more"]},
                ],
            },
        ],
    }


def _nav(day: str, value: str) -> dict[str, Any]:
    return {"account_id": "acct", "as_of_date": day, "nav": {"value": value}}


class World:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.files = {
            "prev": _jb(_prev()),
            "bridge": _jb(
                {
                    "FULL": {
                        "days": 10,
                        "account_pct": 5.0,
                        "model_usd_pct": 3.0,
                        "gap_pp": 2.0,
                        "components_pp": {"a": 1.5, "b": 0.3, "c": 0.2},
                    }
                }
            ),
            "epoch": _jb(
                {
                    "e": {
                        "days": 10,
                        "account_pct": 2.0,
                        "model_pct": 1.0,
                        "components": [
                            {"component": "timing", "pp": 0.6, "tag": "D"},
                            {"component": "other", "pp": 0.4, "tag": "O"},
                        ],
                    }
                }
            ),
            "ledger": _jb(
                {
                    "nav_observations": [
                        _nav("2026-03-01", "10"),
                        _nav("2026-03-02", "20"),
                    ],
                    "strategy_returns": [
                        {"strategy_id": "s2", "date": d, "return": 0.01}
                        for d in ("2026-08-04", "2026-08-05", "2026-08-06")
                    ],
                }
            ),
            "zip": _zip(
                {"2026-08-03": 100.0, "2026-08-04": 101.0, "2026-08-06": 102.0}
            ),
        }
        self.recipe = _recipe()
        self.audit_written = "2026-11-12"
        self.sync()

    def sync(self) -> None:
        for name, raw in self.files.items():
            (self.tmp / name).write_bytes(raw)

    def put(self, name: str, obj: Any) -> None:
        self.files[name] = _jb(obj)
        (self.tmp / name).write_bytes(self.files[name])

    def spec(self) -> dict[str, Any]:
        return {
            "artifact": "model-vs-actual-derive-spec",
            "schema_version": 1,
            "audit_written": self.audit_written,
            "inputs": {
                k: {"path": k, "sha256": _sha(v), "ref_label": "lbl/" + k}
                for k, v in self.files.items()
            },
            "recipe": self.recipe,
        }

    def run(self) -> tuple[bytes, dict[str, Any]]:
        return derive.derive(self.spec(), self.tmp)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_derive_is_deterministic_lf_and_consumable(world: World) -> None:
    raw, manifest = world.run()
    assert world.run()[0] == raw
    assert b"\r" not in raw
    mva.parse_audit_input(raw, _sha(raw))
    assert manifest["output"]["sha256"] == _sha(raw)
    art = json.loads(raw)
    schema = json.loads(
        (SCHEMAS / "model-vs-actual-audit-input.schema.json").read_bytes()
    )
    Draft202012Validator(schema).validate(art)
    s1, s2 = art["strategies"]
    assert art["audit_written"] == "2026-11-12"
    assert [p["pull_id"] for p in art["pulls"]] == ["P2"]
    assert s1["unavailable"] == [] and s1["notes"] == ["new"]
    assert [w["window_id"] for w in s1["windows"]] == ["W0", "F", "E1", "W0_cut"]
    f = s1["windows"][1]
    assert f["capital_base_usd"] == 15.0
    assert [c["pp"] for c in f["components"]] == [1.5, 0.5]
    assert f["components"][1]["source_ref"]["columns"] == ["b", "c"]
    assert s2["primary_window_id"] == "W_full" and s2["notes"] == ["old", "more"]
    assert s2["windows"][1]["components"][0]["replacing_pull"] == "P2"
    assert s2["windows"][1]["components"][0]["origin_tag"] == "I"


def test_manifest_has_no_paths_and_sorted_keys(world: World) -> None:
    _, manifest = world.run()
    text = derive.manifest_bytes(manifest)
    assert text.endswith(b"\n") and b"\r" not in text
    assert json.loads(text) == manifest
    assert all("path" not in v for v in manifest["inputs"].values())


def test_spec_schema_accepts_spec_and_rejects_bad_hash(world: World) -> None:
    schema = json.loads(
        (SCHEMAS / "model-vs-actual-derive-spec.schema.json").read_bytes()
    )
    v = Draft202012Validator(schema)
    v.validate(world.spec())
    bad = world.spec()
    bad["inputs"]["prev"]["sha256"] = "xyz"
    assert list(v.iter_errors(bad))


def test_kill_tampered_input_hash_refused(world: World) -> None:
    (world.tmp / "bridge").write_bytes(world.files["bridge"] + b" ")
    with pytest.raises(derive.DeriveError, match="bridge sha256 differs"):
        world.run()


def test_kill_missing_input_file_refused(world: World) -> None:
    (world.tmp / "epoch").unlink()
    with pytest.raises(derive.DeriveError, match="cannot be read"):
        world.run()


def test_kill_window_not_summing_to_gap_refused(world: World) -> None:
    bridge = json.loads(world.files["bridge"])
    bridge["FULL"]["components_pp"]["a"] = 1.0
    world.put("bridge", bridge)
    with pytest.raises(derive.DeriveError, match="does not close"):
        world.run()


def test_closure_tolerance_is_the_boundary(world: World) -> None:
    bridge = json.loads(world.files["bridge"])
    bridge["FULL"]["components_pp"]["a"] = 1.5 - 0.019
    world.put("bridge", bridge)
    world.run()
    bridge["FULL"]["components_pp"]["a"] = 1.5 - 0.021
    world.put("bridge", bridge)
    with pytest.raises(derive.DeriveError, match="does not close"):
        world.run()


def test_kill_unknown_component_id_refused(world: World) -> None:
    world.recipe["strategies"][0]["ops"][0]["components"][0]["component_id"] = "bogus"
    with pytest.raises(derive.DeriveError, match="vocabulary"):
        world.run()


@pytest.mark.parametrize("tag", ["X", "o", "", None])
def test_kill_origin_tag_must_be_o_d_or_i(world: World, tag: Any) -> None:
    epoch = json.loads(world.files["epoch"])
    epoch["e"]["components"][0]["tag"] = tag
    world.put("epoch", epoch)
    with pytest.raises(derive.DeriveError, match="origin_tag"):
        world.run()


def test_kill_replacing_pull_missing_refused(world: World) -> None:
    world.recipe["strategies"][1]["ops"][0]["replacing_pull"] = "NOPE"
    with pytest.raises(derive.DeriveError, match="unknown pull"):
        world.run()


def test_completed_pull_reference_is_cleared_not_dangling(world: World) -> None:
    world.recipe["strategies"][1]["ops"][0]["replacing_pull"] = "P1"
    raw, _ = world.run()
    comp = json.loads(raw)["strategies"][1]["windows"][1]["components"][0]
    assert comp["replacing_pull"] is None


def test_kill_audit_gap_must_equal_account_minus_model(world: World) -> None:
    bridge = json.loads(world.files["bridge"])
    bridge["FULL"]["gap_pp"] = 1.0
    world.put("bridge", bridge)
    with pytest.raises(derive.DeriveError, match="audit gap"):
        world.run()


def test_kill_undeclared_input_unknown_op_unknown_strategy(world: World) -> None:
    world.recipe["strategies"][0]["ops"][0]["input"] = "nope"
    with pytest.raises(derive.DeriveError, match="undeclared input"):
        world.run()
    world.recipe = _recipe()
    world.recipe["strategies"][0]["ops"][0]["op"] = "shell"
    with pytest.raises(derive.DeriveError, match="unknown recipe op"):
        world.run()
    world.recipe = _recipe()
    world.recipe["strategies"][0]["strategy_id"] = "ghost"
    with pytest.raises(derive.DeriveError, match="unknown strategy"):
        world.run()


def test_kill_wrong_spec_artifact_refused(world: World) -> None:
    spec = world.spec()
    spec["artifact"] = "other"
    with pytest.raises(derive.DeriveError, match="artifact or schema_version"):
        derive.derive(spec, world.tmp)


def _use_id_map(world: World, mapping: dict[str, list[str]]) -> None:
    comps = world.recipe["strategies"][0]["ops"][1]["components"]
    comps.pop("label_prefix")
    comps.pop("columns_prefix")
    comps["id_map"] = mapping
    comps["columns"] = "col"


def test_kill_unknown_epoch_component_in_id_map_refused(world: World) -> None:
    _use_id_map(world, {"timing": ["timing", "T"]})
    with pytest.raises(derive.DeriveError, match="unknown epoch component"):
        world.run()


def test_id_map_merges_same_target_component(world: World) -> None:
    _use_id_map(world, {"timing": ["other", "T"], "other": ["other", "T"]})
    raw, _ = world.run()
    e1 = json.loads(raw)["strategies"][0]["windows"][2]
    assert [(c["component_id"], c["pp"]) for c in e1["components"]] == [("other", 1.0)]


def test_cli_writes_bytes_and_refuses_on_tamper(world: World) -> None:
    spec_path = world.tmp / "spec.json"
    spec_path.write_bytes(json.dumps(world.spec()).encode())
    out, man = world.tmp / "out.json", world.tmp / "man.json"
    args = ["--spec", str(spec_path), "--out", str(out), "--manifest", str(man)]
    assert derive.main(args) == 0
    assert out.read_bytes() == world.run()[0]
    assert json.loads(man.read_bytes())["output"]["sha256"] == _sha(out.read_bytes())
    out.unlink()
    man.unlink()
    (world.tmp / "zip").write_bytes(b"tampered")
    assert derive.main(args) == 2
    assert not out.exists() and not man.exists()


def test_base_input_file_is_not_mutated(world: World) -> None:
    before = copy.deepcopy(json.loads(world.files["prev"]))
    world.run()
    assert json.loads((world.tmp / "prev").read_bytes()) == before


REAL_SPEC_ENV = "QE_MVA_DERIVE_SPEC"
REAL_SHA_ENV = "QE_MVA_DERIVE_EXPECTED_SHA256"


@pytest.mark.skipif(
    not (os.environ.get(REAL_SPEC_ENV) and os.environ.get(REAL_SHA_ENV)),
    reason=(
        f"{REAL_SPEC_ENV} and {REAL_SHA_ENV} do not name the real declared-inputs "
        "spec and the expected artifact sha256 (explicit opt-in)"
    ),
)
def test_real_inputs_reproduce_the_pinned_artifact_byte_for_byte() -> None:
    spec_path = Path(os.environ[REAL_SPEC_ENV])
    spec = json.loads(spec_path.read_bytes())
    raw, _ = derive.derive(spec, spec_path.parent)
    assert _sha(raw) == os.environ[REAL_SHA_ENV]


def test_as_of_emitted_only_when_the_spec_asks(world: World) -> None:
    plain = json.loads(world.run()[0])
    assert "as_of" not in plain
    spec = world.spec()
    spec["as_of"] = "2026-11-06"
    raw, manifest = derive.derive(spec, world.tmp)
    art = json.loads(raw)
    assert art["as_of"] == "2026-11-06" and manifest["as_of"] == "2026-11-06"
    del art["as_of"]
    assert art == plain
    mva.parse_audit_input(raw, _sha(raw))
    schema = json.loads(
        (SCHEMAS / "model-vs-actual-audit-input.schema.json").read_bytes()
    )
    Draft202012Validator(schema).validate(json.loads(raw))
    spec["as_of"] = "not-a-date"
    with pytest.raises(derive.DeriveError, match="as_of"):
        derive.derive(spec, world.tmp)


def test_run_dir_writes_audit_input_and_manifest(world: World) -> None:
    spec = world.spec()
    spec["as_of"] = "2026-11-06"
    spec_path = world.tmp / "spec.json"
    spec_path.write_bytes(json.dumps(spec).encode())
    run = world.tmp / "run"
    assert derive.main(["--spec", str(spec_path), "--run-dir", str(run)]) == 0
    raw = (run / "audit-input.json").read_bytes()
    mva.parse_audit_input(raw, _sha(raw))
    man = json.loads((run / "audit-input.manifest.json").read_bytes())
    assert man["output"]["sha256"] == _sha(raw)
