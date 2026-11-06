"""``release_inputs.py new-cutoff``: a root at a NEW cutoff from a source root plus explicit entries.

Synthetic fixtures (invented cutoffs and bytes); one optional test on a retained real root, skipped when absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from quant_engine.core.release_inputs import (
    ReleaseInputsError,
    build_manifest,
    is_cutoff_dependent,
    load_release_inputs,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import release_inputs as cli  # noqa: E402

OLD, NEW = "2000-01-07", "2000-02-04"
BASELINE = "acceptance/expected-baseline.json"
HISTORIC = "acceptance/baselines/expected-baseline-old.json"
POLICY = "ops/mvp2-release/inputs/sleeves/sleeve-policy-manifest.json"
CALENDAR = "ops/mvp2-release/inputs/hwm/calendar-authority.json"
TAGMAP = "ops/mvp2-release/inputs/sleeves/nested-strategy-tag-map.json"
FILES = {
    BASELINE: json.dumps({"cutoff": OLD}).encode(),
    HISTORIC: json.dumps({"cutoff": "1999-12-31"}).encode(),
    POLICY: b'{"policy": 1}\n',
    CALENDAR: b'{"calendar": 1}\n',
    TAGMAP: b'{"tags": 1}\n',
}


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "src"
    for relative, data in FILES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root, build_manifest(root, list(FILES), cutoff=OLD)[1]


def _file(tmp_path: Path, name: str, data: bytes) -> str:
    path = tmp_path / "new-files" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _run(
    source: tuple[Path, str],
    out: Path,
    *extra: str,
    cutoff: str = NEW,
) -> int:
    root, pin = source
    return cli.main(
        [
            "new-cutoff",
            "--root",
            str(root),
            "--manifest-sha256",
            pin,
            "--cutoff",
            cutoff,
            "--out",
            str(out),
            *extra,
        ]
    )


def _full_replacement(tmp_path: Path) -> list[str]:
    baseline = _file(tmp_path, "b.json", json.dumps({"cutoff": NEW}).encode())
    return [
        "--baseline",
        baseline,
        "--file",
        f"{POLICY}={_file(tmp_path, 'p.json', b'{}')}",
        "--file",
        f"{CALENDAR}={_file(tmp_path, 'c.json', b'{}')}",
    ]


def test_registry_flag() -> None:
    assert is_cutoff_dependent(BASELINE)
    assert is_cutoff_dependent(CALENDAR)
    assert not is_cutoff_dependent(HISTORIC)
    assert not is_cutoff_dependent(TAGMAP)
    assert not is_cutoff_dependent("contracts/x.json")


def test_replace_everything_seals_new_cutoff(
    source: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "new"
    assert _run(source, out, *_full_replacement(tmp_path)) == 0
    report = json.loads(capsys.readouterr().out)
    loaded = load_release_inputs(out, report["manifest_sha256"])
    assert loaded.cutoff == NEW and report["source_cutoff"] == OLD
    assert set(report["replaced"]) == {BASELINE, POLICY, CALENDAR}
    assert report["carried_unchanged"] == {}
    # non-dependent entries are carried byte for byte
    for name in (HISTORIC, TAGMAP):
        assert (out / name).read_bytes() == FILES[name]
    assert (out / BASELINE).read_bytes() == json.dumps({"cutoff": NEW}).encode()


def test_new_entry_is_added_and_drop_removes(
    source: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extra = "ops/mvp2-release/inputs/capital/invested-capital.json"
    out = tmp_path / "new"
    args = [
        *_full_replacement(tmp_path),
        "--file",
        f"{extra}={_file(tmp_path, 'x.json', b'{}')}",
        "--drop",
        TAGMAP,
    ]
    assert _run(source, out, *args) == 0
    report = json.loads(capsys.readouterr().out)
    loaded = load_release_inputs(out, report["manifest_sha256"])
    assert extra in loaded.paths and TAGMAP not in loaded.paths
    assert list(report["added"]) == [extra] and report["dropped"] == [TAGMAP]


def test_stale_carry_refused_listing_entries(
    source: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "new"
    assert _run(source, out) == 1
    err = capsys.readouterr().err
    for name in (BASELINE, POLICY, CALENDAR):
        assert f"CARRIED_UNCHANGED {name}" in err
    assert HISTORIC not in err and TAGMAP not in err
    assert not out.exists()
    assert [p.name for p in tmp_path.iterdir() if "building" in p.name] == []


def test_carry_stale_with_reason_and_glob(
    source: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "new"
    baseline = _file(tmp_path, "b.json", json.dumps({"cutoff": NEW}).encode())
    args = [
        "--baseline",
        baseline,
        "--carry-stale",
        f"{POLICY}=policy text is not dated",
        "--carry-stale",
        "ops/mvp2-release/inputs/hwm/*=calendar reviewed",
    ]
    assert _run(source, out, *args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["carried_unchanged"] == {
        POLICY: "policy text is not dated",
        CALENDAR: "calendar reviewed",
    }
    assert any(w.startswith(f"CARRIED_UNCHANGED {POLICY}") for w in report["warnings"])
    assert (out / POLICY).read_bytes() == FILES[POLICY]


@pytest.mark.parametrize(
    "extra",
    [
        ["--carry-stale", f"{HISTORIC}=why"],  # not cutoff-dependent
        ["--carry-stale", "acceptance/nothing.json=why"],  # unknown
        ["--carry-stale", f"{POLICY}= "],  # empty reason
        ["--carry-stale", f"{POLICY}"],  # no reason separator
    ],
)
def test_carry_stale_negatives(
    source: tuple[Path, str], tmp_path: Path, extra: list[str]
) -> None:
    assert _run(source, tmp_path / "new", *extra) == 1
    assert not (tmp_path / "new").exists()


def test_carry_stale_of_replaced_entry_refused(
    source: tuple[Path, str], tmp_path: Path
) -> None:
    args = [*_full_replacement(tmp_path), "--carry-stale", f"{POLICY}=why"]
    assert _run(source, tmp_path / "new", *args) == 1


def test_same_cutoff_carry_is_byte_identical(
    source: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "same"
    assert _run(source, out, cutoff=OLD) == 0
    assert json.loads(capsys.readouterr().out)["manifest_sha256"] == source[1]
    assert (
        hashlib.sha256((out / "release-inputs.manifest.json").read_bytes()).hexdigest()
        == source[1]
    )


def test_deterministic(
    source: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pins = []
    for name in ("a", "b"):
        assert _run(source, tmp_path / name, *_full_replacement(tmp_path)) == 0
        pins.append(json.loads(capsys.readouterr().out)["manifest_sha256"])
    assert pins[0] == pins[1]


@pytest.mark.parametrize(
    "bad", ["2000-2-4", "20000204", "2000-13-01", "2000-02-30", "x", ""]
)
def test_bad_cutoff_refused(source: tuple[Path, str], tmp_path: Path, bad: str) -> None:
    assert _run(source, tmp_path / "new", *_full_replacement(tmp_path), cutoff=bad) == 1
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize(
    "entry",
    [
        "contracts/x.json",  # not a release input
        "ops/mvp2-release/inputs/../x.json",  # unsafe
        "/abs.json",
        "ops\\mvp2-release\\inputs\\x.json",
    ],
)
def test_unknown_or_unsafe_entry_refused(
    source: tuple[Path, str], tmp_path: Path, entry: str
) -> None:
    args = [
        *_full_replacement(tmp_path),
        "--file",
        f"{entry}={_file(tmp_path, 'u', b'{}')}",
    ]
    assert _run(source, tmp_path / "new", *args) == 1


def test_drop_unknown_and_drop_replaced_refused(
    source: tuple[Path, str], tmp_path: Path
) -> None:
    unknown = ["--drop", "ops/mvp2-release/inputs/capital/none.json"]
    assert _run(source, tmp_path / "n1", *_full_replacement(tmp_path), *unknown) == 1
    both = ["--drop", POLICY]
    assert _run(source, tmp_path / "n2", *_full_replacement(tmp_path), *both) == 1


def test_duplicate_file_refused(source: tuple[Path, str], tmp_path: Path) -> None:
    args = [
        *_full_replacement(tmp_path),
        "--file",
        f"{POLICY}={_file(tmp_path, 'p2.json', b'{}')}",
    ]
    assert _run(source, tmp_path / "new", *args) == 1
    twice = [
        *_full_replacement(tmp_path),
        "--file",
        f"{BASELINE}={_file(tmp_path, 'b2', b'{}')}",
    ]
    assert (
        _run(source, tmp_path / "new2", *twice) == 1
    )  # --baseline and --file for one entry


def test_missing_or_symlink_source_file_refused(
    source: tuple[Path, str], tmp_path: Path
) -> None:
    missing = ["--file", f"{POLICY}={tmp_path / 'nope.json'}"]
    assert _run(source, tmp_path / "new", *missing) == 1


@pytest.mark.parametrize("header", [OLD, "2001-01-01", None])
def test_baseline_cutoff_mismatch_refused(
    source: tuple[Path, str],
    tmp_path: Path,
    header: str | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = {} if header is None else {"cutoff": header}
    baseline = _file(tmp_path, "bad.json", json.dumps(body).encode())
    rest = _full_replacement(tmp_path)[2:]
    assert _run(source, tmp_path / "new", "--baseline", baseline, *rest) == 1
    assert "baseline" in capsys.readouterr().err
    assert not (tmp_path / "new").exists()
    # the same mismatch through --file is refused alike
    assert _run(source, tmp_path / "n2", "--file", f"{BASELINE}={baseline}", *rest) == 1


def test_existing_output_refused(source: tuple[Path, str], tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _run(source, empty, *_full_replacement(tmp_path)) == 1
    assert (
        list(empty.iterdir()) == []
    )  # create-only: even an empty directory is refused
    held = tmp_path / "held"
    held.mkdir()
    (held / "keep.txt").write_bytes(b"x")
    assert _run(source, held, *_full_replacement(tmp_path)) == 1
    assert (held / "keep.txt").read_bytes() == b"x"


def test_output_inside_or_around_source_refused(
    source: tuple[Path, str], tmp_path: Path
) -> None:
    assert _run(source, source[0] / "inner", *_full_replacement(tmp_path)) == 1
    assert _run(source, tmp_path, *_full_replacement(tmp_path)) == 1


def test_wrong_source_pin_refused(source: tuple[Path, str], tmp_path: Path) -> None:
    with pytest.raises(ReleaseInputsError):
        load_release_inputs(source[0], "0" * 64)
    assert (
        _run((source[0], "0" * 64), tmp_path / "new", *_full_replacement(tmp_path)) == 1
    )


# -- the retained R2.2 root (short-path copy), skipped when absent ---------------------------
R22 = Path(os.environ.get("QE_TEST_R22_ROOT", "C:/Dev/scratch/qe-run/ri-r22v9"))


def _r22_pin() -> str | None:
    manifest = R22 / "release-inputs.manifest.json"
    if not manifest.is_file():
        return None
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short path (the R2.2 entries are deep: the default pytest temp path exceeds MAX_PATH)."""

    base = Path(tempfile.mkdtemp(prefix="nc-", dir=R22.parent))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.skipif(
    _r22_pin() is None, reason="retained R2.2 root is not on this machine"
)
def test_r22_same_cutoff_reproduces_source_pin(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pin = _r22_pin()
    assert pin is not None
    loaded = load_release_inputs(R22, pin)
    out = short_tmp / "r22-same"
    code = cli.main(
        [
            "new-cutoff",
            "--root",
            str(R22),
            "--manifest-sha256",
            pin,
            "--cutoff",
            loaded.cutoff,
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["manifest_sha256"] == pin
    for name in loaded.paths:
        assert (out / name).read_bytes() == (R22 / name).read_bytes()


@pytest.mark.skipif(
    _r22_pin() is None, reason="retained R2.2 root is not on this machine"
)
def test_r22_new_cutoff_refuses_stale_entries(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pin = _r22_pin()
    assert pin is not None
    loaded = load_release_inputs(R22, pin)
    code = cli.main(
        [
            "new-cutoff",
            "--root",
            str(R22),
            "--manifest-sha256",
            pin,
            "--cutoff",
            "2026-11-06",
            "--out",
            str(short_tmp / "r22-new"),
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    dependent = [n for n in loaded.paths if is_cutoff_dependent(n)]
    assert BASELINE in dependent and dependent
    assert all(f"CARRIED_UNCHANGED {n}" in err for n in dependent)
    assert not (short_tmp / "r22-new").exists()
