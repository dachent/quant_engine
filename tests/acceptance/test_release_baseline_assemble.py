"""The expected-baseline assembler: agree-or-fail from two derivations, no previous baseline."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import release_baseline_assemble as tool

HEAD = "a" * 40
CUTOFF = "2026-11-06"
STAMP = "2026-11-07T08:00:00.000+00:00"
CONTRACT_SHA = "c" * 64
DECISION = b"R3 admission: derive_a and derive_b agree on every row"
LEDGER = "dataPortfolio/provider-ledger/generations/g1/provider-ledger.json"
LEDGER_SHA = "1" * 64
DECLARED_SHA = "d" * 64
DECLARED = "evidence/declared-cutoff.json"


def _dump(value: object) -> bytes:
    return (json.dumps(value, indent=1) + "\n").encode()


def _manifest(cutoff: str = CUTOFF) -> dict[str, Any]:
    return {
        "schema": "qe-cp-acceptance-inputs/3",
        "cutoff": cutoff,
        "inputs": [
            {
                "family": "portfolio_and_strategy_capital_at_cutoff",
                "path": LEDGER,
                "sha256": LEDGER_SHA,
            },
            {
                "family": tool.CONTRACT_FAMILY,
                "path": tool.CONTRACT_PATH,
                "sha256": CONTRACT_SHA,
            },
            {
                "family": tool.DECLARED_CUTOFF_FAMILY,
                "path": DECLARED,
                "sha256": DECLARED_SHA,
            },
        ],
    }


def _metric(
    metric_id: str, entity: str, value: object, unit: str, **extra: Any
) -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "entity": entity,
        "window": None,
        "as_of": CUTOFF,
        "value": value,
        "unit": unit,
        "inputs_used": [LEDGER],
        **extra,
    }


def _derivation(
    side: str,
    manifest_sha: str,
    metrics: list[dict[str, Any]],
    nd: list[dict[str, Any]],
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema": tool.DERIVATION_SCHEMA,
        "derivation": side,
        "code_commit": HEAD,
        "cutoff": CUTOFF,
        "inputs": [
            {"path": LEDGER, "sha256": LEDGER_SHA},
            {"path": tool.CONTRACT_PATH, "sha256": CONTRACT_SHA},
        ],
        "metrics": metrics,
        "not_derivable": nd,
        "strategy_basis": None,
    }
    doc["inputs_manifest"] = {"sha256": manifest_sha}
    return doc


ROWS_A = [
    _metric("capital.account.cutoff", "acct-1", "100.00", "USD"),
    _metric("twr.account", "acct-1", "0.12345678", "ratio", window="ITD"),
    _metric("flows.account.count", "acct-1", "7", "count", window="ITD"),
    _metric("weights", "portfolio", ["x", "y"], "list"),
]
ROWS_B = [
    _metric("capital.account.cutoff", "acct-1", "100.01", "USD"),
    _metric("twr.account", "acct-1", "0.12345679", "ratio", window="ITD"),
    _metric("flows.account.count", "acct-1", "7", "count", window="ITD"),
    _metric("weights", "portfolio", ["y", "x"], "list"),
]
ND_A = [
    {
        "metric_id": "capital.reserve.cutoff",
        "entity": "reserve",
        "window": None,
        "reason": "no balance in inputs ",
    }
]
ND_B = [
    {
        "metric_id": "capital.reserve.cutoff",
        "entity": "reserve",
        "window": "",
        "reason": "capital_anchors do not identify it",
    }
]


def _build(
    a_rows: list[dict[str, Any]] | None = None,
    b_rows: list[dict[str, Any]] | None = None,
    a_nd: list[dict[str, Any]] | None = None,
    b_nd: list[dict[str, Any]] | None = None,
) -> dict[str, bytes]:
    manifest = _dump(_manifest())
    sha = hashlib.sha256(manifest).hexdigest()
    a = _derivation(
        "a",
        sha,
        copy.deepcopy(ROWS_A if a_rows is None else a_rows),
        copy.deepcopy(ND_A if a_nd is None else a_nd),
    )
    b = _derivation(
        "b",
        sha,
        copy.deepcopy(ROWS_B if b_rows is None else b_rows),
        copy.deepcopy(ND_B if b_nd is None else b_nd),
    )
    return {"derive_a": _dump(a), "derive_b": _dump(b), "manifest": manifest}


def _run(files: dict[str, bytes], **over: Any) -> bytes:
    args: dict[str, Any] = {
        **files,
        "head": HEAD,
        "cutoff": CUTOFF,
        "generated_utc": STAMP,
        "decision": DECISION,
    }
    args.update(over)
    return tool.assemble(**args)


def _refusal(files: dict[str, bytes], **over: Any) -> tool.AssemblyError:
    with pytest.raises(tool.AssemblyError) as caught:
        _run(files, **over)
    return caught.value


def _rebound(files: dict[str, bytes], raw: bytes) -> dict[str, bytes]:
    """``files`` with ``raw`` as the manifest, both derivations naming its digest."""

    out = {**files, "manifest": raw}
    for side in ("derive_a", "derive_b"):
        doc = json.loads(files[side])
        doc["inputs_manifest"] = {"sha256": hashlib.sha256(raw).hexdigest()}
        out[side] = _dump(doc)
    return out


def _doc(files: dict[str, bytes] | None = None, **over: Any) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(_run(files or _build(), **over))
    return value


def test_agreeing_rows_are_admitted_with_a_value_and_the_unit_tolerance() -> None:
    doc = _doc()
    assert [r["metric_id"] for r in doc["metrics"]] == [
        "capital.account.cutoff",
        "flows.account.count",
        "twr.account",
        "weights",
    ]
    usd = doc["metrics"][0]
    assert (
        usd["expected"] == "100.00"
        and usd["b_actual"] == "100.01"
        and usd["tolerance"] == "0.01"
    )
    ratio = doc["metrics"][2]
    assert ratio["tolerance"] == "0.00000001" and ratio["window"] == "ITD"
    assert "tolerance" not in doc["metrics"][1] and doc["metrics"][1]["window"] == "ITD"
    for row in doc["metrics"]:
        assert row["expected"] == row["derivation_a"]["value"] == row["a_actual"]
        assert (
            row["derivation_a"]["code_commit"]
            == row["derivation_b"]["code_commit"]
            == HEAD
        )
        assert row["critical"] is True
        assert row["authority_pointer"][-1] != {} and len(row["authority_pointer"]) == 3


def test_header_fields_match_the_committed_schema_and_hashes() -> None:
    files = _build()
    doc = _doc(files)
    assert (
        doc["schema"] == "qe-cp-expected-baseline/1"
        and doc["cutoff"] == CUTOFF
        and doc["head"] == HEAD
    )
    assert doc["generated_utc"] == STAMP
    for key, value in tool.DEFAULT_HEADER.items():
        assert doc[key] == value
    manifest_sha = hashlib.sha256(files["manifest"]).hexdigest()
    assert doc["derivations"] == {
        "a": {
            "code_commit": HEAD,
            "manifest_sha256": manifest_sha,
            "raw_output_sha256": hashlib.sha256(files["derive_a"]).hexdigest(),
        },
        "b": {
            "code_commit": HEAD,
            "manifest_sha256": manifest_sha,
            "raw_output_sha256": hashlib.sha256(files["derive_b"]).hexdigest(),
        },
    }
    keys = sorted(
        [r["metric_id"], r["entity"], r["window"]]
        for r in [*doc["metrics"], *doc["not_derivable"]]
    )
    inventory = json.dumps(keys, separators=(",", ":")).encode()
    assert doc["inventory_sha256"] == hashlib.sha256(inventory).hexdigest()
    pointers = doc["metrics"][0]["authority_pointer"]
    assert pointers == sorted(pointers, key=lambda p: (p["path"], p["sha256"]))
    pointer_map = json.dumps(pointers, sort_keys=True, separators=(",", ":")).encode()
    assert doc["pointer_map_sha256"] == hashlib.sha256(pointer_map).hexdigest()
    assert doc["admission_decision_sha256"] == hashlib.sha256(DECISION).hexdigest()
    assert [p for p in pointers if p["path"] == tool.CONTRACT_PATH] == [
        {"path": tool.CONTRACT_PATH, "sha256": CONTRACT_SHA}
    ]
    assert all(p["sha256"] for p in pointers)


def test_output_satisfies_the_committed_baseline_invariants() -> None:
    doc = _doc()
    for row in doc["metrics"]:
        unit = row["unit"]
        assert (row.get("tolerance") is None) == (unit in tool.EXACT_UNITS)
        assert tool.values_agree(
            row["derivation_a"]["value"], row["derivation_b"]["value"], unit
        )
    for row in doc["not_derivable"]:
        assert "expected" not in row and row["reason_a"] and row["reason_b"]
    assert all(
        tool._is_data_root_relative(p["path"])
        for p in doc["metrics"][0]["authority_pointer"]
    )


def test_not_derivable_in_both_keeps_both_reasons_trimmed() -> None:
    (row,) = _doc()["not_derivable"]
    assert row["reason_a"] == "no balance in inputs"
    assert row["reason_b"] == "capital_anchors do not identify it"
    assert row["reason"] == "no balance in inputs; capital_anchors do not identify it"
    assert row["window"] == "" and row["critical"] is True


def test_strict_reasons_refuses_a_difference_and_accepts_equal_text() -> None:
    assert "reason" in str(_refusal(_build(), strict_reasons=True))
    same = [{"metric_id": "m", "entity": "e", "window": None, "reason": "same"}]
    assert (
        _doc(_build(a_nd=same, b_nd=same), strict_reasons=True)["not_derivable"][0][
            "reason"
        ]
        == "same; same"
    )


def test_value_disagreement_beyond_tolerance_is_refused() -> None:
    bad = copy.deepcopy(ROWS_B)
    bad[0]["value"] = "100.02"
    bad[1]["value"] = "0.123457"
    error = _refusal(_build(b_rows=bad))
    assert error.code == "DISAGREEMENT"
    assert "capital.account.cutoff" in error.detail and "twr.account" in error.detail


def test_exact_units_compare_as_text_and_lists_as_sets() -> None:
    assert tool.values_agree("7", 7, "count")
    assert not tool.values_agree("7", "8", "count")
    assert not tool.values_agree("2026-11-06", "2026-11-05", "date")
    assert tool.values_agree(["a", "b"], ["b", "a"], "list")
    assert tool.values_agree(["a", "a", "b"], ["a", "b", "a"], "list")
    assert not tool.values_agree(["a", "a"], ["a"], "list")  # multiplicity counts
    assert not tool.values_agree(["a", "a", "b"], ["a", "b", "b"], "list")
    assert not tool.values_agree(["a"], ["a", "b"], "list")
    assert tool.values_agree("1.00", "1.01", "USD") and not tool.values_agree(
        "1.00", "1.011", "USD"
    )
    assert tool.values_agree("1", "1.00000001", "ratio") and not tool.values_agree(
        "1", "1.0000001", "ratio"
    )
    assert not tool.values_agree("1", "not-a-number", "USD")
    assert not tool.values_agree("NaN", "NaN", "ratio")


def test_unit_as_of_and_unknown_unit_are_refused() -> None:
    other_unit = copy.deepcopy(ROWS_B)
    other_unit[2]["unit"] = "status"
    assert "unit" in _refusal(_build(b_rows=other_unit)).detail
    other_day = copy.deepcopy(ROWS_B)
    other_day[2]["as_of"] = "2026-11-05"
    assert "as_of" in _refusal(_build(b_rows=other_day)).detail
    odd_a, odd_b = copy.deepcopy(ROWS_A), copy.deepcopy(ROWS_B)
    odd_a[2]["unit"] = odd_b[2]["unit"] = "pct"
    assert "no comparison rule" in _refusal(_build(a_rows=odd_a, b_rows=odd_b)).detail


def test_one_sided_rows_and_derivable_versus_not_derivable_are_refused() -> None:
    error = _refusal(_build(b_rows=ROWS_B[:-1]))
    assert "present in derive a only" in error.detail
    extra = [*ROWS_B, _metric("capital.account.cutoff", "acct-2", "1", "USD")]
    assert "present in derive b only" in _refusal(_build(b_rows=extra)).detail
    moved = [
        {
            "metric_id": "weights",
            "entity": "portfolio",
            "window": None,
            "reason": "cannot",
        },
    ]
    detail = _refusal(_build(b_rows=ROWS_B[:-1], b_nd=[*ND_B, *moved])).detail
    assert "derive a has it as metric, derive b as not derivable" in detail


def test_every_disagreement_is_listed_and_nothing_is_written(tmp_path: Path) -> None:
    bad = copy.deepcopy(ROWS_B[:-1])
    bad[0]["value"] = "999"
    files = _build(b_rows=bad)
    error = _refusal(files)
    assert error.detail.splitlines()[0].startswith("2 disagreement(s)")
    paths = {}
    for name, data in files.items():
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_bytes(data)
    out = tmp_path / "out.json"
    code = tool.main(_argv(paths, out))
    assert code == 1 and not out.exists() and not list(tmp_path.glob(".*.tmp"))


def test_duplicate_rows_and_rows_in_both_lists_are_refused() -> None:
    twice = [*ROWS_A, ROWS_A[0]]
    assert "twice" in _refusal(_build(a_rows=twice)).detail
    both = [
        {"metric_id": "weights", "entity": "portfolio", "window": None, "reason": "r"}
    ]
    assert (
        "both a metric and not derivable"
        in _refusal(_build(a_nd=[*ND_A, *both])).detail
    )


def test_cutoff_mismatches_are_refused() -> None:
    files = _build()
    assert _refusal(files, cutoff="2026-09-17").code == "CUTOFF_MISMATCH"
    manifest = _manifest("2026-09-17")
    raw = _dump(manifest)
    assert _refusal(_rebound(files, raw)).code == "CUTOFF_MISMATCH"
    a = json.loads(files["derive_a"])
    a["cutoff"] = "2026-09-17"
    assert "derive a cutoff" in _refusal({**files, "derive_a": _dump(a)}).detail
    b = json.loads(files["derive_b"])
    b["cutoff"] = "2026-09-17"
    assert "derive b cutoff" in _refusal({**files, "derive_b": _dump(b)}).detail
    b["cutoff"] = CUTOFF
    assert _doc({**files, "derive_b": _dump(b)})["cutoff"] == CUTOFF
    del b["cutoff"]  # B must declare its cutoff, not merely not contradict it
    assert _refusal({**files, "derive_b": _dump(b)}).code == "CUTOFF_MISMATCH"


def test_b_must_record_the_manifest_it_read_and_the_manifest_the_cutoff_artifact() -> (
    None
):
    files = _build()
    b = json.loads(files["derive_b"])
    del b["inputs_manifest"]
    refusal = _refusal({**files, "derive_b": _dump(b)})
    assert refusal.code == "MANIFEST_MISMATCH" and "derive b" in refusal.detail
    manifest = _manifest()
    manifest["inputs"] = [
        e for e in manifest["inputs"] if e.get("family") != tool.DECLARED_CUTOFF_FAMILY
    ]
    raw = _dump(manifest)
    assert _refusal(_rebound(files, raw)).code == "CUTOFF_ARTIFACT"


def test_commit_and_manifest_identity_are_refused_on_mismatch() -> None:
    files = _build()
    b = json.loads(files["derive_b"])
    b["code_commit"] = "b" * 40
    assert _refusal({**files, "derive_b": _dump(b)}).code == "COMMIT_MISMATCH"
    assert _refusal(files, head="b" * 40).code == "COMMIT_MISMATCH"
    assert _refusal(files, head="abc").code == "USAGE"
    dirty = {}
    for side in ("derive_a", "derive_b"):
        doc = json.loads(files[side])
        doc["code_commit"] = HEAD + "+dirty"
        dirty[side] = _dump(doc)
    assert _refusal({**files, **dirty}).code == "COMMIT_MISMATCH"
    a = json.loads(files["derive_a"])
    a["inputs_manifest"]["sha256"] = "0" * 64
    assert _refusal({**files, "derive_a": _dump(a)}).code == "MANIFEST_MISMATCH"
    b = json.loads(files["derive_b"])
    b["inputs_manifest"] = {"sha256": "0" * 64}
    assert _refusal({**files, "derive_b": _dump(b)}).code == "MANIFEST_MISMATCH"
    a = json.loads(files["derive_a"])
    del a["inputs_manifest"]
    assert _refusal({**files, "derive_a": _dump(a)}).code == "MANIFEST_MISMATCH"


def test_schema_side_basis_and_pin_mismatches_are_refused() -> None:
    files = _build()
    for key, value, code in (
        ("schema", "qe-cp-derivation/2", "DERIVATION_SCHEMA"),
        ("derivation", "b", "DERIVATION_SIDE"),
    ):
        a = json.loads(files["derive_a"])
        a[key] = value
        assert _refusal({**files, "derive_a": _dump(a)}).code == code
    b = json.loads(files["derive_b"])
    b["strategy_basis"] = {"sha256": "5" * 64, "path": "C:/x"}
    assert _refusal({**files, "derive_b": _dump(b)}).code == "BASIS_MISMATCH"
    a = json.loads(files["derive_a"])
    a["strategy_basis"] = {"sha256": "5" * 64, "path": "C:/other"}
    b["strategy_basis"] = {"sha256": "5" * 64, "path": "C:/x"}
    assert _doc(
        {**files, "derive_a": _dump(a), "derive_b": _dump(b)}
    )  # the local path is not compared
    b = json.loads(files["derive_b"])
    b["inputs"][0]["sha256"] = "2" * 64
    assert _refusal({**files, "derive_b": _dump(b)}).code == "PIN_MISMATCH"


def test_manifest_without_exactly_one_contract_is_refused() -> None:
    manifest = _manifest()
    manifest["inputs"].pop(1)  # the contract entry
    raw = _dump(manifest)
    files = _build()
    assert _refusal(_rebound(files, raw)).code == "CONTRACT"


def _real_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, bytes], Path, bytes]:
    """A world whose pins are the digests of real files under ``tmp_path/root``, plus an FX file."""

    root = tmp_path / "root"
    contents = {
        LEDGER: b"ledger bytes",
        tool.CONTRACT_PATH: b"contract bytes",
        "evidence/fx/rates.csv": b"rates bytes",
        DECLARED: b"declared cutoff bytes",
    }
    for rel, data in contents.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    sha = {rel: hashlib.sha256(data).hexdigest() for rel, data in contents.items()}
    monkeypatch.setattr(sys.modules[__name__], "LEDGER_SHA", sha[LEDGER])
    monkeypatch.setattr(sys.modules[__name__], "CONTRACT_SHA", sha[tool.CONTRACT_PATH])
    monkeypatch.setattr(sys.modules[__name__], "DECLARED_SHA", sha[DECLARED])
    files = _build()
    for side in ("derive_a", "derive_b"):
        doc = json.loads(files[side])
        doc["inputs"].append(
            {"path": "scratch/fx/rates.csv", "sha256": sha["evidence/fx/rates.csv"]}
        )
        files[side] = _dump(doc)
    template = _dump(
        {
            "authority_pointer": [
                {
                    "path": "evidence/fx/rates.csv",
                    "sha256": sha["evidence/fx/rates.csv"],
                },
                {"path": tool.CONTRACT_PATH, "sha256": sha[tool.CONTRACT_PATH]},
                {"path": LEDGER, "sha256": sha[LEDGER]},
                {"path": DECLARED, "sha256": sha[DECLARED]},
            ]
        }
    )
    return files, root, template


def test_pointer_outside_the_data_root_needs_a_template_and_a_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files, root, template = _real_world(tmp_path, monkeypatch)
    error = _refusal(files)
    assert error.code == "POINTER_PATH" and "scratch/fx/rates.csv" in error.detail
    assert _refusal(files, template=template).code == "DATA_ROOT"
    pointers = _doc(files, template=template, data_roots=[root])["metrics"][0][
        "authority_pointer"
    ]
    assert [p["path"] for p in pointers] == [
        "evidence/fx/rates.csv",
        tool.CONTRACT_PATH,
        LEDGER,
        DECLARED,
    ]


def test_remapped_pointer_bytes_must_exist_and_hash_to_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files, root, template = _real_world(tmp_path, monkeypatch)
    (root / "evidence/fx/rates.csv").write_bytes(b"tampered")
    error = _refusal(files, template=template, data_roots=[root])
    assert error.code == "POINTER_BYTES" and "evidence/fx/rates.csv" in error.detail
    (root / "evidence/fx/rates.csv").unlink()
    error = _refusal(files, template=template, data_roots=[root])
    assert (
        error.code == "POINTER_BYTES"
        and "not a file under any --data-root" in error.detail
    )
    # a digest reassigned to a path that does not exist is refused, whatever the path spells
    moved = json.loads(template)
    moved["authority_pointer"][0]["path"] = "evidence/nowhere/other.csv"
    assert (
        _refusal(files, template=_dump(moved), data_roots=[root]).code
        == "POINTER_BYTES"
    )


def test_data_roots_are_searched_in_order_and_cannot_be_escaped_by_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files, root, template = _real_world(tmp_path, monkeypatch)
    split = tmp_path / "second"
    (split / "contracts").mkdir(parents=True)
    (root / tool.CONTRACT_PATH).replace(split / tool.CONTRACT_PATH)
    assert _refusal(files, template=template, data_roots=[root]).code == "POINTER_BYTES"
    assert _doc(files, template=template, data_roots=[root, split])
    # with roots and no template, the pins themselves are verified
    plain = _build()
    assert _refusal(plain, data_roots=[root]).code == "POINTER_BYTES"


def test_cli_requires_a_data_root_with_a_pointer_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool, "check_entry_interpreter", lambda *_: None)
    files, root, template = _real_world(tmp_path, monkeypatch)
    paths = _stage(tmp_path, files)
    tpl = tmp_path / "template.json"
    tpl.write_bytes(template)
    out = tmp_path / "out.json"
    assert (
        tool.main(_argv(paths, out, "--template", str(tpl))) == 1 and not out.exists()
    )
    assert (
        tool.main(_argv(paths, out, "--template", str(tpl), "--data-root", str(root)))
        == 0
    )


def test_as_of_must_be_a_date_not_after_the_cutoff() -> None:
    for bad in ("2026-11-07", "2027-01-01", "garbage", None, 20261106):
        rows_a, rows_b = copy.deepcopy(ROWS_A), copy.deepcopy(ROWS_B)
        rows_a[2]["as_of"] = rows_b[2]["as_of"] = bad  # both sides agree on it
        error = _refusal(_build(a_rows=rows_a, b_rows=rows_b))
        assert error.code == "DISAGREEMENT" and "as_of" in error.detail, bad
    missing_a, missing_b = copy.deepcopy(ROWS_A), copy.deepcopy(ROWS_B)
    del missing_a[2]["as_of"], missing_b[2]["as_of"]
    assert "as_of" in _refusal(_build(a_rows=missing_a, b_rows=missing_b)).detail
    early_a, early_b = copy.deepcopy(ROWS_A), copy.deepcopy(ROWS_B)
    early_a[2]["as_of"] = early_b[2]["as_of"] = (
        "2026-07-17"  # a stale account: before the cutoff
    )
    assert (
        _doc(_build(a_rows=early_a, b_rows=early_b))["metrics"][1]["as_of"]
        == "2026-07-17"
    )


def test_only_the_supported_manifest_schemas_are_accepted() -> None:
    files = _build()
    for version, accepted in (
        ("2", True),
        ("3", True),
        ("4", False),
        ("garbage", False),
        ("1", False),
    ):
        manifest = _manifest()
        manifest["schema"] = f"qe-cp-acceptance-inputs/{version}"
        raw = _dump(manifest)
        swapped = _rebound(files, raw)
        if accepted:
            assert _doc(swapped)
        else:
            assert _refusal(swapped).code == "MANIFEST_SCHEMA", version


def test_template_supplies_header_text_only_and_pointers_must_cover_the_pins() -> None:
    files = _build()
    doc = _doc(
        files,
        template=_dump({"control": "R9", "conventions": "text", "spec_version": "v6"}),
    )
    assert (doc["control"], doc["conventions"], doc["spec_version"]) == (
        "R9",
        "text",
        "v6",
    )
    assert doc["metric_spec"] == tool.DEFAULT_HEADER["metric_spec"]
    assert _refusal(files, template=_dump({"metrics": []})).code == "TEMPLATE"
    assert _refusal(files, template=_dump({"not_derivable": []})).code == "TEMPLATE"
    assert _refusal(files, template=_dump({"control": ""})).code == "TEMPLATE"
    short = _dump(
        {"authority_pointer": [{"path": tool.CONTRACT_PATH, "sha256": CONTRACT_SHA}]}
    )
    assert _refusal(files, template=short).code == "POINTER_TEMPLATE"
    pointer_only = _dump(
        {"authority_pointer": _doc(files)["metrics"][0]["authority_pointer"]}
    )
    assert _refusal(files, template=pointer_only).code == "DATA_ROOT"


def test_usage_errors_are_refused() -> None:
    files = _build()
    assert _refusal(files, generated_utc="yesterday").code == "USAGE"
    assert _refusal(files, generated_utc="2026-11-07T08:00:00").code == "USAGE"
    assert _refusal(files, decision=b"  ").code == "USAGE"
    assert _refusal(files, cutoff="not-a-date").code in {"USAGE", "CUTOFF_MISMATCH"}
    assert _refusal({**files, "derive_a": b"[]"}).code == "INPUT"
    assert _refusal({**files, "derive_a": b'{"a":1,"a":2}'}).code == "INPUT"
    assert _refusal(_build(a_rows=[], b_rows=[])).code == "EMPTY"


def _argv(paths: dict[str, Path], out: Path, *extra: str) -> list[str]:
    return [
        "--derive-a",
        str(paths["derive_a"]),
        "--derive-b",
        str(paths["derive_b"]),
        "--manifest",
        str(paths["manifest"]),
        "--head",
        HEAD,
        "--cutoff",
        CUTOFF,
        "--generated-utc",
        STAMP,
        "--admission-decision-text",
        DECISION.decode(),
        "--out",
        str(out),
        *extra,
    ]


def _stage(tmp_path: Path, files: dict[str, bytes]) -> dict[str, Path]:
    paths = {}
    for name, data in files.items():
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_bytes(data)
    return paths


def test_cli_is_deterministic_and_create_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool, "check_entry_interpreter", lambda *_: None)
    paths = _stage(tmp_path, _build())
    one, two = tmp_path / "one.json", tmp_path / "two" / "two.json"
    assert tool.main(_argv(paths, one)) == 0 and tool.main(_argv(paths, two)) == 0
    assert one.read_bytes() == two.read_bytes() == _run(_build())
    assert b"\r" not in one.read_bytes() and one.read_bytes().endswith(b"}\n")
    before = one.read_bytes()
    assert tool.main(_argv(paths, one)) == 1
    assert one.read_bytes() == before and not list(tmp_path.glob(".*.tmp"))


def test_cli_accepts_a_decision_file_and_a_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool, "check_entry_interpreter", lambda *_: None)
    paths = _stage(tmp_path, _build())
    decision = tmp_path / "decision.txt"
    decision.write_bytes(DECISION)
    template = tmp_path / "template.json"
    template.write_bytes(_dump({"control": "R9"}))
    argv = _argv(paths, tmp_path / "out.json", "--template", str(template))
    at = argv.index("--admission-decision-text")
    argv[at : at + 2] = ["--admission-decision-file", str(decision)]
    assert tool.main(argv) == 0
    doc = json.loads((tmp_path / "out.json").read_bytes())
    assert (
        doc["control"] == "R9"
        and doc["admission_decision_sha256"] == hashlib.sha256(DECISION).hexdigest()
    )


def test_cli_stops_on_a_refused_entry_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool, "check_entry_interpreter", lambda *_: 2)
    paths = _stage(tmp_path, _build())
    assert tool.main(_argv(paths, tmp_path / "out.json")) == 2
    assert not (tmp_path / "out.json").exists()


# --- reproduction on the retained R2.2 world (outside Git; skipped without it) ---

R22_HEAD = "110fe44db9a5e51492e40cb2dbe7901c84ea3e6b"
R22_BASELINE_SHA256 = "35f7851f06ccbfb280d554b4024a175314cd5250d963e7755bd335d379257b3f"
R22_DECISION = (
    "R2.2 restatement (#624 option B): derive_a and derive_b re-run on the pinned R2.1 inputs with "
    "--strategy-basis (strategy-basis-declaration/1 sha 93db30c8) agree on every row; exactly 18 rows "
    "move from the R2.1 baseline and no other row changes"
)
_EVIDENCE = Path(
    os.environ.get(
        "QE_EVIDENCE_ROOT",
        str(
            Path.home()
            / "V Capital"
            / "investing - Documents"
            / "_quant_engine"
            / "evidence"
        ),
    )
)
_RESTATED = (
    _EVIDENCE
    / "releases"
    / "2026-10-26-r2"
    / "r3-plan"
    / "ai624"
    / "restated-baseline-r22"
)
_RELEASE_BASELINE = _EVIDENCE / "ri-r22v9" / "acceptance" / "expected-baseline.json"
_R22_FILES = (
    _RESTATED / "derive_a.json",
    _RESTATED / "derive_b.json",
    _RESTATED / "acceptance-inputs-manifest.json",
    _RELEASE_BASELINE,
)
needs_r22 = pytest.mark.skipif(
    not all(p.is_file() for p in _R22_FILES),
    reason="the retained R2.2 evidence is not on this machine",
)


@needs_r22
def test_reproduces_the_r22_release_baseline_byte_for_byte(tmp_path: Path) -> None:
    target = _RELEASE_BASELINE.read_bytes()
    assert hashlib.sha256(target).hexdigest() == R22_BASELINE_SHA256
    files = {
        "derive_a": (_RESTATED / "derive_a.json").read_bytes(),
        "derive_b": (_RESTATED / "derive_b.json").read_bytes(),
        "manifest": (_RESTATED / "acceptance-inputs-manifest.json").read_bytes(),
    }
    old = json.loads(target)
    # The retained R2.2 derive_b predates the cutoff and manifest attribution the assembler now
    # requires of B; the test records them as the run's own values, nothing else is touched.
    legacy_b = json.loads(files["derive_b"])
    assert "cutoff" not in legacy_b and "inputs_manifest" not in legacy_b
    legacy_b["cutoff"] = old["cutoff"]
    legacy_b["inputs_manifest"] = {
        "sha256": hashlib.sha256(files["manifest"]).hexdigest()
    }
    files["derive_b"] = _dump(legacy_b)
    common: dict[str, Any] = {
        "head": R22_HEAD,
        "cutoff": old["cutoff"],
        "generated_utc": old["generated_utc"],
        "decision": R22_DECISION.encode(),
    }
    # The header text is the committed DEFAULT_HEADER: no template is needed for it.
    pointers = old["metrics"][0]["authority_pointer"]
    assert {k: old[k] for k in tool.DEFAULT_HEADER} == tool.DEFAULT_HEADER
    # The pinned inputs name three FX files under scratch/, which is not a data-root path: without a
    # template the assembler refuses, as the baseline schema test would.
    with pytest.raises(tool.AssemblyError) as refused:
        tool.assemble(**files, **common)
    assert refused.value.code == "POINTER_PATH"
    # The retained pointer list carries the data-root copies of those files, in the order the earlier
    # assemblers appended them. Only its paths and order are used, and --data-root makes the assembler
    # hash each file it names: the QE root holds every file but the repository contract, which is the
    # CRLF form of the file at main 865bf905 (its digest is the pinned one).
    template = _dump({"authority_pointer": pointers})
    with pytest.raises(tool.AssemblyError) as unrooted:
        tool.assemble(**files, **common, template=template)
    assert unrooted.value.code == "DATA_ROOT"
    contract_root = tmp_path / "repo"
    contract = contract_root / tool.CONTRACT_PATH
    contract.parent.mkdir(parents=True)
    shown = subprocess.run(
        ["git", "show", f"865bf905:{tool.CONTRACT_PATH}"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        check=False,
    )
    if shown.returncode != 0:
        pytest.skip(
            "main 865bf905 is not in this clone, so the R2.2 contract bytes are unavailable"
        )
    lf = shown.stdout.replace(b"\r\n", b"\n")
    contract.write_bytes(lf.replace(b"\n", b"\r\n"))
    roots = [_EVIDENCE.parent, contract_root]
    built = tool.assemble(**files, **common, template=template, data_roots=roots)
    # Identical to the committed baseline except the one digest of the patched derive_b bytes.
    expected = json.loads(target)
    expected["derivations"]["b"]["raw_output_sha256"] = hashlib.sha256(
        files["derive_b"]
    ).hexdigest()
    assert json.loads(built) == expected
    assert (
        tool.assemble(**files, **common, template=template, data_roots=roots) == built
    )
