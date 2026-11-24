"""Capture proofs for release inputs with no daily series (the VTMA trade histories).

A trade history shows no sign of how far it reaches, so a root may seal one only with a proof of a capture
taken at least the vendor's publication lag after the cutoff.  Issues 1078 and 1097: release D carried
files whose newest confirmation was one day after its cutoff and they lacked two trades dated before it.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from quant_engine.core.release_inputs import (
    CAPTURE_PROOF_LAG_DAYS,
    CAPTURE_PROOF_SCHEMA,
    ReleaseInputsError,
    build_manifest,
    capture_proof_entry,
    check_capture_proof,
    load_release_inputs,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import release_inputs as cli  # noqa: E402

IMPL = "ops/mvp2-release/inputs/implementation"
SIGNALS = f"{IMPL}/grizzly-signals.csv"
BLENDED = f"{IMPL}/grizzly-blended-history.csv"
OLD, NEW = "2000-01-07", "2000-02-04"
BASELINE = "acceptance/expected-baseline.json"

# release D (cutoff 2026-11-19) and release E (cutoff 2026-11-20), as their provenance records them:
# D's files were R3's bytes, confirmed unchanged on 2026-11-20 (one day after the cutoff) and lacked a
# 2026-11-16 Sell and a 2026-11-18 Rebuy; E's files hold them and were captured on 2026-11-23.
D = {
    SIGNALS: ("943734fe89b3a06d3d89410e1b07757a0e945329e7170f7f249cd2974eb487ae"),
    BLENDED: ("228760167bcbc5febc2ccf5ed88d0764e8dd0cfa555cb789c5aa2914b0da44e1"),
}
E = {
    SIGNALS: ("1a05e6ed6c6eca95fa7fda9e26a025b936a925e146782f610587e8a9adac8c61"),
    BLENDED: ("5562306c9a7d18110979b467f754dc9f0d3da703ef62ae9c22d06292f8f18212"),
}


def _proof(
    entry: str,
    sha: str,
    captured: str,
    method: str = "NO_CHANGE_CONFIRMATION",
) -> bytes:
    return json.dumps(
        {
            "schema": CAPTURE_PROOF_SCHEMA,
            "entry": entry,
            "sha256": sha,
            "captured_at_utc": captured,
            "method": method,
            "source": "signed-in vendor capture, governed driver",
        }
    ).encode()


@pytest.mark.parametrize("entry", sorted(CAPTURE_PROOF_LAG_DAYS))
def test_release_d_pins_are_refused_and_release_e_pins_pass(entry: str) -> None:
    with pytest.raises(ReleaseInputsError, match="vendor publication lag"):
        check_capture_proof(
            entry,
            D[entry],
            _proof(entry, D[entry], "2026-11-20T17:13:00Z"),
            "2026-11-19",
        )
    # E passes exactly at the floor (cutoff + 3 days); only the date of its capture is recorded
    check_capture_proof(
        entry,
        E[entry],
        _proof(entry, E[entry], "2026-11-23T12:00:00Z", "FRESH_CAPTURE"),
        "2026-11-20",
    )


def test_capture_date_boundary_and_timezone() -> None:
    sha = "a" * 64
    lag = CAPTURE_PROOF_LAG_DAYS[SIGNALS]
    assert lag == 3
    check_capture_proof(SIGNALS, sha, _proof(SIGNALS, sha, "2000-02-07T00:00:00Z"), NEW)
    with pytest.raises(ReleaseInputsError, match="before 2000-02-07"):
        check_capture_proof(
            SIGNALS, sha, _proof(SIGNALS, sha, "2000-02-06T23:59:59Z"), NEW
        )
    # the date is the UTC date: 2000-02-06 20:00 at -06:00 is 2000-02-07 02:00Z
    check_capture_proof(
        SIGNALS, sha, _proof(SIGNALS, sha, "2000-02-06T20:00:00-06:00"), NEW
    )
    with pytest.raises(ReleaseInputsError, match="timezone-aware"):
        check_capture_proof(
            SIGNALS, sha, _proof(SIGNALS, sha, "2000-02-09T00:00:00"), NEW
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: {**b, "sha256": "b" * 64},  # other bytes than the sealed file
        lambda b: {**b, "entry": BLENDED},  # another entry's proof
        lambda b: {**b, "method": "GUESS"},
        lambda b: {**b, "source": " "},
        lambda b: {**b, "schema": "x"},
        lambda b: {k: v for k, v in b.items() if k != "source"},
        lambda b: {**b, "extra": 1},
    ],
)
def test_malformed_or_mismatched_proof_refused(mutate: object) -> None:
    sha = "a" * 64
    body = json.loads(_proof(SIGNALS, sha, "2000-03-01T00:00:00Z"))
    with pytest.raises(ReleaseInputsError):
        check_capture_proof(
            SIGNALS,
            sha,
            json.dumps(mutate(body)).encode(),  # type: ignore[operator]
            NEW,
        )
    with pytest.raises(ReleaseInputsError):
        check_capture_proof(SIGNALS, sha, b"not json", NEW)
    with pytest.raises(ReleaseInputsError, match="takes no capture proof"):
        check_capture_proof(f"{IMPL}/spxl-market-data.json", sha, b"{}", NEW)


# -- the CLI: a proof is required wherever a root seals one of these inputs ------------------------
SOURCE_FILES = {
    BASELINE: json.dumps({"cutoff": OLD}).encode(),
    SIGNALS: b"signals-old\n",
    BLENDED: b"blended-old\n",
}


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "src"
    for relative, data in SOURCE_FILES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root, build_manifest(root, list(SOURCE_FILES), cutoff=OLD)[1]


def _file(tmp_path: Path, name: str, data: bytes) -> str:
    path = tmp_path / "new-files" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _new_cutoff(
    source: tuple[Path, str], out: Path, *extra: str, cutoff: str = NEW
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


def _baseline(tmp_path: Path) -> list[str]:
    return [
        "--baseline",
        _file(tmp_path, "b.json", json.dumps({"cutoff": NEW}).encode()),
    ]


def _replacement(tmp_path: Path, captured: str) -> list[str]:
    args = list(_baseline(tmp_path))
    for entry in (SIGNALS, BLENDED):
        data = b"fresh-" + entry.rsplit("/", 1)[-1].encode()
        sha = hashlib.sha256(data).hexdigest()
        args += [
            "--file",
            f"{entry}={_file(tmp_path, entry.rsplit('/', 1)[-1], data)}",
            "--capture-proof",
            f"{entry}={_file(tmp_path, entry.rsplit('/', 1)[-1] + '.proof', _proof(entry, sha, captured, 'FRESH_CAPTURE'))}",
        ]
    return args


def test_stale_pins_carried_into_a_later_cutoff_are_refused(
    source: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [
        *_baseline(tmp_path),
        "--carry-stale",
        f"{IMPL}/*=unchanged, the vendor file was not captured again",
    ]
    assert _new_cutoff(source, tmp_path / "new", *args) == 1
    err = capsys.readouterr().err
    assert f"CAPTURE_PROOF_MISSING {SIGNALS}" in err
    assert f"CAPTURE_PROOF_MISSING {BLENDED}" in err
    assert not (tmp_path / "new").exists()


def test_stale_pin_with_an_early_no_change_proof_is_refused(
    source: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # D's shape: carried bytes, confirmed unchanged one day after the cutoff
    root, _ = source
    proofs = []
    for entry in (SIGNALS, BLENDED):
        sha = hashlib.sha256((root / entry).read_bytes()).hexdigest()
        proofs += [
            "--capture-proof",
            f"{entry}={_file(tmp_path, entry.rsplit('/', 1)[-1] + '.p', _proof(entry, sha, '2000-02-05T17:13:00Z'))}",
        ]
    args = [*_baseline(tmp_path), "--carry-stale", f"{IMPL}/*=unchanged", *proofs]
    assert _new_cutoff(source, tmp_path / "new", *args) == 1
    assert "vendor publication lag" in capsys.readouterr().err
    assert not (tmp_path / "new").exists()


def test_fresh_capture_with_proof_seals_and_pins_the_proof(
    source: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "new"
    assert (
        _new_cutoff(source, out, *_replacement(tmp_path, "2000-02-07T10:00:00Z")) == 0
    )
    report = json.loads(capsys.readouterr().out)
    loaded = load_release_inputs(out, report["manifest_sha256"])
    for entry in (SIGNALS, BLENDED):
        proof = capture_proof_entry(entry)
        assert proof in loaded.paths and entry in loaded.paths
        check_capture_proof(
            entry, loaded.sha256(entry), (out / proof).read_bytes(), NEW
        )


def test_no_change_proof_lets_the_carried_bytes_through(
    source: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = source
    proofs = []
    for entry in (SIGNALS, BLENDED):
        sha = hashlib.sha256((root / entry).read_bytes()).hexdigest()
        proofs += [
            "--capture-proof",
            f"{entry}={_file(tmp_path, entry.rsplit('/', 1)[-1] + '.p', _proof(entry, sha, '2000-02-07T00:00:00Z'))}",
        ]
    args = [*_baseline(tmp_path), "--carry-stale", f"{IMPL}/*=unchanged", *proofs]
    out = tmp_path / "new"
    assert _new_cutoff(source, out, *args) == 0
    capsys.readouterr()
    assert (out / SIGNALS).read_bytes() == SOURCE_FILES[SIGNALS]


def test_early_capture_with_a_fresh_file_is_refused(
    source: tuple[Path, str], tmp_path: Path
) -> None:
    # a file replaced at the new cutoff still needs a late enough capture
    args = _replacement(tmp_path, "2000-02-06T10:00:00Z")
    assert _new_cutoff(source, tmp_path / "new", *args) == 1
    assert not (tmp_path / "new").exists()


def test_drop_needs_no_proof_and_a_proof_for_a_dropped_entry_is_refused(
    source: tuple[Path, str], tmp_path: Path
) -> None:
    drops = ["--drop", SIGNALS, "--drop", BLENDED]
    out = tmp_path / "new"
    assert _new_cutoff(source, out, *_baseline(tmp_path), *drops) == 0
    sha = "a" * 64
    stray = _file(tmp_path, "stray", _proof(SIGNALS, sha, "2000-03-01T00:00:00Z"))
    assert (
        _new_cutoff(
            source,
            tmp_path / "n2",
            *_baseline(tmp_path),
            *drops,
            "--capture-proof",
            f"{SIGNALS}={stray}",
        )
        == 1
    )


def test_proof_for_other_bytes_is_refused(
    source: tuple[Path, str], tmp_path: Path
) -> None:
    wrong = _file(tmp_path, "wrong", _proof(SIGNALS, "c" * 64, "2000-02-07T10:00:00Z"))
    args = [
        *_baseline(tmp_path),
        "--drop",
        BLENDED,
        "--file",
        f"{SIGNALS}={_file(tmp_path, 'signals.csv', b'fresh')}",
        "--capture-proof",
        f"{SIGNALS}={wrong}",
    ]
    assert _new_cutoff(source, tmp_path / "new", *args) == 1
    # the same arguments with a proof of the right bytes seal
    right = _file(
        tmp_path,
        "right",
        _proof(SIGNALS, hashlib.sha256(b"fresh").hexdigest(), "2000-02-07T10:00:00Z"),
    )
    args[-1] = f"{SIGNALS}={right}"
    assert _new_cutoff(source, tmp_path / "new2", *args) == 0


def test_same_cutoff_carry_needs_no_proof(
    source: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "same"
    assert _new_cutoff(source, out, cutoff=OLD) == 0
    assert json.loads(capsys.readouterr().out)["manifest_sha256"] == source[1]


def test_add_requires_a_proof_for_each_trade_history(
    source: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, pin = source
    data = b"fresh-signals\n"
    new_file = _file(tmp_path, "signals.csv", data)
    base = [
        "add",
        "--root",
        str(root),
        "--manifest-sha256",
        pin,
        "--replace",
        "--file",
        f"{SIGNALS}={new_file}",
    ]
    assert cli.main([*base, "--out", str(tmp_path / "a1")]) == 1
    assert f"CAPTURE_PROOF_MISSING {SIGNALS}" in capsys.readouterr().err
    sha = hashlib.sha256(data).hexdigest()
    early = _file(tmp_path, "early", _proof(SIGNALS, sha, "2000-01-09T00:00:00Z"))
    assert (
        cli.main(
            [
                *base,
                "--capture-proof",
                f"{SIGNALS}={early}",
                "--out",
                str(tmp_path / "a2"),
            ]
        )
        == 1
    )
    good = _file(tmp_path, "good", _proof(SIGNALS, sha, "2000-01-10T00:00:00Z"))
    assert (
        cli.main(
            [
                *base,
                "--capture-proof",
                f"{SIGNALS}={good}",
                "--out",
                str(tmp_path / "a3"),
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    loaded = load_release_inputs(tmp_path / "a3", report["manifest_sha256"])
    assert capture_proof_entry(SIGNALS) in loaded.paths


def test_a_proof_path_cannot_be_supplied_as_a_plain_file(
    source: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # a proof sealed through --file would skip the proof check, so the path is reserved
    root, pin = source
    forged = _file(tmp_path, "forged", b"not json")
    reserved = capture_proof_entry(SIGNALS)
    assert (
        cli.main(
            [
                "add",
                "--root",
                str(root),
                "--manifest-sha256",
                pin,
                "--file",
                f"{reserved}={forged}",
                "--out",
                str(tmp_path / "a"),
            ]
        )
        == 1
    )
    assert "--capture-proof" in capsys.readouterr().err
    args = [*_baseline(tmp_path), "--drop", SIGNALS, "--drop", BLENDED]
    assert (
        _new_cutoff(source, tmp_path / "n", *args, "--file", f"{reserved}={forged}")
        == 1
    )
    assert not (tmp_path / "n").exists() and not (tmp_path / "a").exists()
