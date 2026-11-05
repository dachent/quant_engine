"""RELEASE-DRIVER-001 block B1b: the predecessor spec is remapped into the cycle's roots, or refused."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import release_ledger_spec  # noqa: E402
import release_spec_remap as tool  # noqa: E402

PROVIDERS = (
    "alpaca",
    "fidelity",
    "ibkr",
    "matrixport",
    "portfolio123",
    "quantbase",
    "schwab",
)
NATIVE = {"matrixport", "quantbase", "schwab"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest() -> dict[str, Any]:
    """A schema-valid cycle manifest that declares the root keys ``data`` and ``scratch``."""

    zero = _sha(b"x")
    providers: dict[str, Any] = {}
    for provider_id in PROVIDERS:
        entry: dict[str, Any] = {
            "root": {"root_key": "data", "relative": f"data-{provider_id}"},
            "generation": {"mode": "single"},
            "manifest_sha256": zero,
            "finality_source": "GENERATION_MANIFEST",
            "qualification": "STAGED_COMPLETE",
        }
        if provider_id in NATIVE or provider_id == "ibkr":
            entry["finality_source"] = (
                "CYCLE_MANIFEST_NATIVE"
                if provider_id in NATIVE
                else "SELECTED_SOURCE_ARTIFACT"
            )
            entry["finalized_through"] = "2026-01-02"
            entry["finality_evidence"] = {
                "path": {"root_key": "data", "relative": f"native/{provider_id}.json"},
                "sha256": zero,
                "size_bytes": 1,
            }
        if provider_id == "alpaca":
            entry["qualification"] = "PUBLISHED_COMPLETE"
        if provider_id == "fidelity":
            entry["qualification"] = "FINITE_STATIC"
        providers[provider_id] = entry
    return {
        "schema_version": "qe-release-cycle-manifest/1",
        "cycle_id": "cycle-x",
        "cutoff": "2026-01-02",
        "evaluated_on": "2026-01-05",
        "captured_at_utc": "2026-01-04T10:00:00Z",
        "predecessor": {"release_id": "rel-prev"},
        "scratch": {"root_key": "scratch", "relative": "cycle-x"},
        "providers": providers,
        "strategy_evidence": {
            "path": {"root_key": "data", "relative": "evidence/strategy.json"},
            "sha256": zero,
            "size_bytes": 1,
        },
    }


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    data, scratch, other = tmp_path / "data", tmp_path / "scratch", tmp_path / "other"
    for directory in (data, scratch, other):
        directory.mkdir()
    (data / "src" / "fx").mkdir(parents=True)
    (data / "src" / "targets.csv").write_text("a\n", encoding="utf-8")
    (data / "chain").mkdir()
    (data / "chain" / "ev.json").write_text("{}", encoding="utf-8")
    (data / "repo").mkdir()
    spec = {
        "schema_version": 1,
        "strategy_inputs": {
            "returns_path": "D:\\legacy\\src\\targets.csv",
            "strategy_evidence_path": "D:\\legacy\\chain\\ev.json",
            "strategy_evidence_sha256": "ab" * 32,
        },
        "strategy_provider_map": {"strategy-one": "ibkr"},
        "portfolio_fx_rates": {"root": "d:/legacy/src/fx", "note": "fx"},
        "output_root": "D:\\legacy\\c\\ledger",
        "financial_mvp": {"contract_path": "D:\\legacy\\repo\\contracts\\one.json"},
    }
    mapping = {
        "schema_version": tool.MAPPING_VERSION,
        "mappings": [
            {"old_prefix": "D:\\legacy\\src", "root_key": "data", "relative": "src"},
            {
                "old_prefix": "D:\\legacy\\chain",
                "root_key": "data",
                "relative": "chain",
            },
            {"old_prefix": "D:\\legacy\\c", "root_key": "scratch"},
            {"old_prefix": "D:\\legacy\\repo", "root_key": "data", "relative": "repo"},
        ],
    }
    return {
        "tmp": tmp_path,
        "manifest": _manifest(),
        "roots": {"data": data.resolve(), "scratch": scratch.resolve(), "other": other},
        "spec": spec,
        "mapping": mapping,
        "data": data.resolve(),
        "scratch": scratch.resolve(),
    }


def _remap(world: dict[str, Any], **changes: Any) -> tuple[Any, list[str]]:
    args = {
        "spec": world["spec"],
        "mapping": world["mapping"],
        "manifest": world["manifest"],
        "bound_roots": world["roots"],
    }
    args.update(changes)
    return tool.remap_spec(**args)


def _refused(world: dict[str, Any], code: str, **changes: Any) -> str:
    with pytest.raises(tool.SpecRemapError) as info:
        _remap(world, **changes)
    assert info.value.code == code, str(info.value)
    return str(info.value)


def test_a_predecessor_spec_is_remapped_into_the_declared_roots(
    world: dict[str, Any],
) -> None:
    outcome, dropped = _remap(world)
    document = outcome.document
    data, scratch = world["data"].as_posix(), world["scratch"].as_posix()
    assert dropped == []
    assert document["strategy_inputs"]["returns_path"] == f"{data}/src/targets.csv"
    assert (
        document["strategy_inputs"]["strategy_evidence_path"] == f"{data}/chain/ev.json"
    )
    assert document["strategy_inputs"]["strategy_evidence_sha256"] == "ab" * 32
    assert document["portfolio_fx_rates"] == {"root": f"{data}/src/fx", "note": "fx"}
    assert document["output_root"] == f"{scratch}/ledger"
    assert (
        document["financial_mvp"]["contract_path"] == f"{data}/repo/contracts/one.json"
    )
    assert document["strategy_provider_map"] == {"strategy-one": "ibkr"}
    assert len(outcome.rewritten) == 5
    # the input document is not modified
    assert world["spec"]["output_root"] == "D:\\legacy\\c\\ledger"


def test_the_remapped_spec_is_one_the_ledger_spec_builder_reads_as_is(
    world: dict[str, Any],
) -> None:
    assert tool.CARRIED_KEYS == release_ledger_spec.CARRIED
    assert tool.REGENERATED_KEYS == release_ledger_spec.REGENERATED
    outcome, _ = _remap(world)
    allowed = set(release_ledger_spec.CARRIED) | set(release_ledger_spec.REGENERATED)
    assert set(outcome.document) <= allowed
    assert {"strategy_inputs", "strategy_provider_map"} <= set(outcome.document)
    carried, dropped = _remap(world, carried_only=True)
    assert set(carried.document) == {
        "strategy_inputs",
        "strategy_provider_map",
        "portfolio_fx_rates",
    }
    assert dropped == sorted({"schema_version", "output_root", "financial_mvp"})


def test_carried_only_does_not_need_the_regenerated_blocks_to_be_mappable(
    world: dict[str, Any],
) -> None:
    world["spec"]["providers"] = {"x": {"root": "D:\\elsewhere\\x"}}
    message = _refused(world, "FOREIGN_PATH")
    assert "D:\\elsewhere\\x" in message
    outcome, dropped = _remap(world, carried_only=True)
    assert "providers" in dropped and "providers" not in outcome.document


def test_a_path_that_stays_under_a_foreign_root_is_refused_and_every_offender_listed(
    world: dict[str, Any],
) -> None:
    world["spec"]["strategy_inputs"]["extra"] = "E:\\unmapped\\one.csv"
    world["spec"]["portfolio_fx_rates"]["more"] = "/srv/unmapped/two"
    world["spec"]["note"] = "see F:\\hidden\\inside\\text for it"
    message = _refused(world, "FOREIGN_PATH")
    assert "3 absolute path(s)" in message
    for offender in ("E:\\unmapped\\one.csv", "/srv/unmapped/two", "F:\\hidden"):
        assert offender in message
    assert "/strategy_inputs/extra" in message


def test_a_path_already_under_a_declared_root_is_kept_byte_for_byte(
    world: dict[str, Any],
) -> None:
    current = f"{world['data'].as_posix()}/chain/ev.json"
    world["spec"]["strategy_inputs"]["strategy_evidence_path"] = current
    outcome, _ = _remap(world)
    assert outcome.document["strategy_inputs"]["strategy_evidence_path"] == current
    assert outcome.kept == 1


def test_a_root_that_is_bound_but_not_declared_by_the_manifest_is_not_a_target(
    world: dict[str, Any],
) -> None:
    world["mapping"]["mappings"][0]["root_key"] = "other"
    world["mapping"]["mappings"][0]["relative"] = ""
    _refused(world, "UNDECLARED_ROOT")
    # nor does a path under it count as already current
    world["mapping"] = {
        "schema_version": tool.MAPPING_VERSION,
        "mappings": [
            {"old_prefix": "D:\\legacy", "root_key": "data", "relative": "src"}
        ],
    }
    world["spec"]["strategy_inputs"]["extra"] = (
        f"{world['roots']['other'].as_posix()}/x"
    )
    message = _refused(world, "FOREIGN_PATH", carried_only=True)
    assert "/strategy_inputs/extra" in message


def test_a_manifest_root_key_the_run_does_not_bind_is_refused(
    world: dict[str, Any],
) -> None:
    del world["roots"]["scratch"]
    _refused(world, "UNKNOWN_ROOT")


@pytest.mark.parametrize(
    "second",
    [
        "d:/legacy/src",  # the same prefix, other slash style and drive case
        "D:\\legacy\\src\\",  # trailing separator
        "D:\\legacy\\src\\fx",  # nested inside the first
        "D:\\legacy",  # contains the first
    ],
)
def test_two_old_prefixes_that_overlap_are_refused(
    world: dict[str, Any], second: str
) -> None:
    world["mapping"]["mappings"].append(
        {"old_prefix": second, "root_key": "data", "relative": "chain"}
    )
    message = _refused(world, "AMBIGUOUS_PREFIX")
    assert "equal or nested" in message


def test_an_old_prefix_that_overlaps_a_declared_root_is_refused(
    world: dict[str, Any],
) -> None:
    inside = world["data"].as_posix() + "/src"
    world["mapping"]["mappings"].append(
        {"old_prefix": inside, "root_key": "data", "relative": "chain"}
    )
    message = _refused(world, "AMBIGUOUS_PREFIX")
    assert "declared root" in message
    world["mapping"]["mappings"][-1]["old_prefix"] = str(world["tmp"])
    _refused(world, "AMBIGUOUS_PREFIX")


def test_a_mapping_target_that_does_not_exist_is_refused(world: dict[str, Any]) -> None:
    world["mapping"]["mappings"][1]["relative"] = "chain-missing"
    message = _refused(world, "MISSING_TARGET")
    assert "chain-missing" in message


def test_a_remapped_carried_path_that_does_not_exist_is_refused(
    world: dict[str, Any],
) -> None:
    (world["data"] / "src" / "targets.csv").unlink()
    message = _refused(world, "MISSING_TARGET")
    assert "/strategy_inputs/returns_path" in message
    # a regenerated block names files that are written later: not checked
    world["spec"]["strategy_inputs"]["returns_path"] = "D:\\legacy\\src"
    _remap(world)


def test_a_mapping_may_not_leave_its_root_or_be_a_sibling_prefix_match(
    world: dict[str, Any],
) -> None:
    world["mapping"]["mappings"][0]["relative"] = "../other"
    _refused(world, "MAPPING")
    world["mapping"]["mappings"][0]["relative"] = "src"
    # a different directory that merely starts with the old prefix is not under it
    world["spec"]["strategy_inputs"]["returns_path"] = "D:\\legacy\\src-copy\\t.csv"
    message = _refused(world, "FOREIGN_PATH")
    assert "src-copy" in message


@pytest.mark.parametrize(
    "written",
    [
        "D:\\legacy\\src\\targets.csv",
        "d:\\legacy\\src\\targets.csv",
        "D:/legacy/src/targets.csv",
        "d:/legacy\\src/targets.csv",
        "D:\\\\legacy\\src\\\\targets.csv",
    ],
)
def test_both_slash_styles_and_either_drive_letter_case_remap_the_same(
    world: dict[str, Any], written: str
) -> None:
    world["spec"]["strategy_inputs"]["returns_path"] = written
    outcome, _ = _remap(world)
    expected = f"{world['data'].as_posix()}/src/targets.csv"
    assert outcome.document["strategy_inputs"]["returns_path"] == expected


def test_old_prefixes_are_matched_in_any_slash_style_and_drive_case(
    world: dict[str, Any],
) -> None:
    world["mapping"]["mappings"][0]["old_prefix"] = "d:/legacy/src/"
    outcome, _ = _remap(world)
    assert outcome.document["strategy_inputs"]["returns_path"].endswith(
        "/src/targets.csv"
    )


@pytest.mark.parametrize(
    "mapping",
    [
        [],
        {"schema_version": "other", "mappings": [1]},
        {"schema_version": tool.MAPPING_VERSION, "mappings": []},
        {"schema_version": tool.MAPPING_VERSION, "mappings": [{"old_prefix": "D:\\a"}]},
        {
            "schema_version": tool.MAPPING_VERSION,
            "mappings": [{"old_prefix": "relative/path", "root_key": "data"}],
        },
        {
            "schema_version": tool.MAPPING_VERSION,
            "mappings": [
                {"old_prefix": "D:\\a", "root_key": "data", "relative": "a\\b"}
            ],
        },
    ],
)
def test_a_malformed_mapping_is_refused(world: dict[str, Any], mapping: Any) -> None:
    with pytest.raises(tool.SpecRemapError):
        _remap(world, mapping=mapping)


def test_a_spec_that_is_not_an_object_is_refused(world: dict[str, Any]) -> None:
    _refused(world, "SPEC", spec=["D:\\legacy\\src"])


def _files(world: dict[str, Any]) -> dict[str, Path]:
    tmp: Path = world["tmp"]
    (tmp / "manifest.json").write_text(json.dumps(world["manifest"]), encoding="utf-8")
    (tmp / "mapping.json").write_text(json.dumps(world["mapping"]), encoding="utf-8")
    (tmp / "spec.json").write_text(json.dumps(world["spec"]), encoding="utf-8")
    (tmp / "roots.json").write_text(
        json.dumps({k: str(v) for k, v in world["roots"].items()}), encoding="utf-8"
    )
    return {
        name: tmp / f"{name}.json" for name in ("manifest", "mapping", "spec", "roots")
    }


def _cli(world: dict[str, Any], out: Path, *extra: str) -> int:
    files = _files(world)
    return tool.main(
        [
            "remap",
            "--manifest",
            str(files["manifest"]),
            "--roots-file",
            str(files["roots"]),
            "--mapping",
            str(files["mapping"]),
            "--spec",
            str(files["spec"]),
            "--out",
            str(out),
            *extra,
        ]
    )


def test_the_command_writes_the_remapped_spec_atomically_and_create_only(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    out = world["tmp"] / "out" / "pred.json"
    assert _cli(world, out, "--plan") == 0
    assert not out.exists()
    assert _cli(world, out) == 0
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    data = out.read_bytes()
    assert summary["sha256"] == _sha(data) and summary["rewritten"] == 5
    assert data.endswith(b"\n") and b"\r" not in data
    assert json.loads(data)["output_root"].endswith("/ledger")
    assert not list(out.parent.glob("*.tmp"))
    # the same bytes again are accepted (a repeated step); other bytes are never overwritten
    assert _cli(world, out) == 0
    world["spec"]["strategy_provider_map"] = {"strategy-one": "other"}
    assert _cli(world, out) == 1
    assert "OUTPUT_EXISTS" in capsys.readouterr().err
    assert out.read_bytes() == data


def test_the_command_refuses_a_rewritten_count_other_than_the_one_expected(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    # a predecessor selected-inputs or preparation spec is remapped with the same command; the count of the
    # previous pass (42 and 44 for the R2.2 files) makes a mapping that matched too few or too many refuse
    out = world["tmp"] / "pred.json"
    assert _cli(world, out, "--expect-rewritten", "4", "--plan") == 1
    assert "COUNT: 5 path(s) rewritten, expected 4" in capsys.readouterr().err
    assert not out.exists()
    assert _cli(world, out, "--expect-rewritten", "5") == 0
    assert out.is_file()


def test_a_partial_temporary_file_from_an_interrupted_run_does_not_block_the_output(
    world: dict[str, Any],
) -> None:
    out = world["tmp"] / "out" / "pred.json"
    out.parent.mkdir()
    out.with_name("pred.json.tmp").write_bytes(b"{trunc")
    assert _cli(world, out) == 0
    assert json.loads(out.read_bytes())["strategy_provider_map"]
    assert not out.with_name("pred.json.tmp").exists()


def test_the_command_refuses_with_a_code_and_writes_nothing(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    world["spec"] = copy.deepcopy(world["spec"])
    world["spec"]["strategy_inputs"]["extra"] = "E:\\unmapped\\one.csv"
    out = world["tmp"] / "pred.json"
    assert _cli(world, out) == 1
    assert "FOREIGN_PATH" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize(
    "written",
    [
        "D:\\legacy\\src\\..\\..\\elsewhere\\x.csv",
        "D:\\legacy\\src\\.\\targets.csv",
        "D:/legacy/src/fx/../../../escape",
    ],
)
def test_a_path_with_dot_segments_is_refused_even_when_it_would_stay_inside(
    world: dict[str, Any], written: str
) -> None:
    world["spec"]["strategy_inputs"]["returns_path"] = written
    message = _refused(world, "FOREIGN_PATH")
    assert "/strategy_inputs/returns_path" in message


def test_a_declared_root_path_with_dot_segments_is_not_kept(
    world: dict[str, Any],
) -> None:
    sneaky = f"{world['data'].as_posix()}/chain/../../outside/x"
    world["spec"]["strategy_inputs"]["strategy_evidence_path"] = sneaky
    _refused(world, "FOREIGN_PATH")


def test_a_link_below_the_old_prefix_that_leaves_the_target_is_refused(
    world: dict[str, Any],
) -> None:
    outside = world["tmp"] / "outside"
    outside.mkdir()
    (outside / "t.csv").write_text("x", encoding="utf-8")
    link = world["data"] / "src" / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    world["spec"]["strategy_inputs"]["returns_path"] = "D:\\legacy\\src\\linked\\t.csv"
    message = _refused(world, "FOREIGN_PATH")
    assert "linked" in message


def test_posix_and_unc_paths_embedded_in_longer_strings_are_refused(
    world: dict[str, Any],
) -> None:
    world["spec"]["note"] = "see /srv/old/file for it"
    world["spec"]["other"] = "copy \\\\fileserver\\share\\x"
    world["spec"]["quoted"] = 'path="/srv/old/dir"'
    message = _refused(world, "FOREIGN_PATH")
    assert "3 absolute path(s)" in message


def test_ordinary_text_is_not_mistaken_for_an_embedded_path(
    world: dict[str, Any],
) -> None:
    world["spec"]["fine"] = [
        "and/or",
        "2026/01/02",
        "https://example.test/a/b",
        "./relative/path",
        "a/b/c",
        "plain",
    ]
    _remap(world)


def test_the_old_prefix_is_counted_in_both_slash_styles_and_must_equal_the_rewritten_count(
    world: dict[str, Any],
) -> None:
    # R2.2 predecessor files: 42 and 44 occurrences, in the plain and the JSON-escaped backslash form; a mapping
    # that rewrites fewer paths than the prefix occurs leaves an old path in the new spec
    rules = tool.build_rules(
        world["mapping"], tool.declared_roots(world["manifest"], world["roots"])
    )
    assert (
        tool.count_old_prefix_occurrences(world["spec"], rules) == 5
    )  # 4 backslash paths + 1 d:/ path
    outcome, _ = _remap(world)
    assert len(outcome.rewritten) == 5
    # a key is never rewritten: the occurrence in it is found and the remap refuses
    keyed = copy.deepcopy(world["spec"])
    keyed["strategy_inputs"]["D:\\legacy\\src\\old-key"] = "value"
    assert "5 path(s) were rewritten" in _refused(world, "COUNT_FOUND", spec=keyed)
    # a sibling directory with a common name prefix is not the old prefix
    sibling = copy.deepcopy(world["spec"])
    sibling["note"] = "see D:\\legacy\\srcX\\file"
    assert tool.count_old_prefix_occurrences(sibling, rules) == 5
