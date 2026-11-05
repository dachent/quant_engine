"""rebind-predecessor: the next release-inputs root, with the committed release as the predecessor.

Every fixture is synthetic: invented ids, invented bytes.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from quant_engine.core.release_inputs import (
    MANIFEST_NAME,
    build_manifest,
    load_release_inputs,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import release_inputs as cli  # noqa: E402
import sleeve_disclosures as disclosures  # noqa: E402

CUTOFF = "2000-01-07"
OLD_RELEASE, OLD_PANEL = "a1" * 32, "b2" * 32
NEW_RELEASE, NEW_PANEL = "c3" * 32, "d4" * 32
DISCLOSURE = cli.DISCLOSURE_INPUTS
POLICY = cli.POLICY_MANIFEST
OTHER = "ops/mvp2-release/inputs/sleeves/sleeve-declared-inputs.json"
POINTERS = {"aggregate": "/invested_capital/current_aggregate_usd", "strategy_id": "s"}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def _root(tmp_path: Path, other: bytes = b'{"declared": 1}\n') -> tuple[Path, str]:
    root = tmp_path / "ri"
    disclosure = cli._canonical(
        {
            "artifact_id": "sleeve-disclosure-inputs",
            "predecessor_comparison": {
                "data_file": {
                    "relative_path": cli.PANEL_DATA_RELATIVE.format(panel=OLD_PANEL),
                    "sha256": "0" * 64,
                },
                "panel_generation_id": OLD_PANEL,
                "pointers": POINTERS,
                "release_id": OLD_RELEASE,
            },
            "schema_version": 1,
        }
    )
    _write(root / DISCLOSURE, disclosure)
    policy = cli._canonical(
        {
            "artifact_id": "sleeve-policy-manifest",
            "disclosure-inputs": {
                "path": "disclosure-inputs.json",
                "sha256": _sha(disclosure),
                "size_bytes": len(disclosure),
            },
        }
    )
    _write(root / POLICY, policy)
    _write(root / OTHER, other)
    _, pin = build_manifest(root, [DISCLOSURE, POLICY, OTHER], cutoff=CUTOFF)
    return root, pin


def _committed(data_root: Path, release_id: str, panel: str, data: bytes) -> None:
    coordination = data_root / disclosures.COORDINATION_RELATIVE
    directory = coordination / "releases" / release_id
    manifest = json.dumps(
        {
            "release_id": release_id,
            "targets": [
                {"root_id": "portfolio-panel", "proposed_generation_id": panel}
            ],
        }
    ).encode()
    _write(directory / "release.json", manifest)
    _write(
        directory / "journal.json",
        json.dumps({"release_id": release_id, "state": "COMMITTED"}).encode(),
    )
    _write(
        coordination / "CURRENT_RELEASE.json",
        json.dumps(
            {
                "release_id": release_id,
                "release_manifest_sha256": _sha(manifest),
                "state": "COMMITTED",
            }
        ).encode(),
    )
    _write(data_root / cli.PANEL_DATA_RELATIVE.format(panel=panel), data)


def _rebind(root: Path, pin: str, data_root: Path, out: Path, *extra: str) -> int:
    return cli.main(
        [
            "rebind-predecessor",
            "--root",
            str(root),
            "--manifest-sha256",
            pin,
            "--cutoff",
            CUTOFF,
            "--data-root",
            str(data_root),
            "--out",
            str(out),
            *extra,
        ]
    )


def test_the_next_root_names_the_committed_release_derived_from_its_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, pin = _root(tmp_path)
    data_root = tmp_path / "workspace"
    dashboard = b'{"panel": "dashboard"}'
    _committed(data_root, NEW_RELEASE, NEW_PANEL, dashboard)
    out = tmp_path / "ri-next"

    assert _rebind(root, pin, data_root, out) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "rebound" and report["stale_references"] == []

    loaded = load_release_inputs(
        out, report["manifest_sha256"]
    )  # loads under the printed pin
    assert report["manifest_sha256"] == _sha((out / MANIFEST_NAME).read_bytes())
    inputs = json.loads((out / DISCLOSURE).read_bytes())
    predecessor = inputs["predecessor_comparison"]
    assert predecessor == {
        "data_file": {
            "relative_path": cli.PANEL_DATA_RELATIVE.format(panel=NEW_PANEL),
            "sha256": _sha(dashboard),
        },
        "panel_generation_id": NEW_PANEL,
        "pointers": POINTERS,  # the authored part is kept
        "release_id": NEW_RELEASE,
    }
    disclosures.check_predecessor_is_current_release(predecessor, data_root)
    # the policy manifest pins the rewritten file, and nothing else in the root moved
    policy = json.loads((out / POLICY).read_bytes())["disclosure-inputs"]
    raw = (out / DISCLOSURE).read_bytes()
    assert (policy["sha256"], policy["size_bytes"]) == (_sha(raw), len(raw))
    assert (out / OTHER).read_bytes() == (root / OTHER).read_bytes()
    assert loaded.paths == load_release_inputs(root, pin).paths
    # the source root is untouched
    assert load_release_inputs(root, pin).sha256(DISCLOSURE) != loaded.sha256(
        DISCLOSURE
    )

    # a root that already names the committed release is left alone (idempotent)
    assert (
        _rebind(out, report["manifest_sha256"], data_root, tmp_path / "ri-again") == 0
    )
    again = json.loads(capsys.readouterr().out)
    assert again == {
        "status": "already_current",
        "manifest_sha256": report["manifest_sha256"],
    }
    assert not (tmp_path / "ri-again").exists()


def test_another_file_naming_the_old_predecessor_is_refused_not_carried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, pin = _root(tmp_path, other=f'{{"note": "vs {OLD_RELEASE}"}}\n'.encode())
    data_root = tmp_path / "workspace"
    _committed(data_root, NEW_RELEASE, NEW_PANEL, b"{}")
    out = tmp_path / "ri-next"
    assert _rebind(root, pin, data_root, out) == 1
    err = capsys.readouterr().err
    assert OTHER in err and "old release a1a1a1a1" in err
    assert not out.exists()
    # the operator can accept it knowingly: it is listed in the report
    assert _rebind(root, pin, data_root, out, "--allow-stale-references") == 0
    assert json.loads(capsys.readouterr().out)["stale_references"] == [
        f"{OTHER}: old release a1a1a1a1"
    ]


def test_refusals_leave_nothing_behind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, pin = _root(tmp_path)
    data_root = tmp_path / "workspace"
    out = tmp_path / "ri-next"
    # no committed release
    assert _rebind(root, pin, data_root, out) == 1
    assert "committed release is unreadable" in capsys.readouterr().err
    # a release whose panel has no dashboard data
    _committed(data_root, NEW_RELEASE, NEW_PANEL, b"{}")
    (data_root / cli.PANEL_DATA_RELATIVE.format(panel=NEW_PANEL)).unlink()
    assert _rebind(root, pin, data_root, out) == 1
    assert "no dashboard data" in capsys.readouterr().err
    _write(data_root / cli.PANEL_DATA_RELATIVE.format(panel=NEW_PANEL), b"{}")
    # a target that is not empty is never written into
    _write(out / "x", b"x")
    assert _rebind(root, pin, data_root, out) == 1
    assert "not empty" in capsys.readouterr().err
    # a wrong pin refuses before anything is read
    assert _rebind(root, "0" * 64, data_root, tmp_path / "other-out") == 1
    capsys.readouterr()
    assert not (tmp_path / "other-out").exists()


def test_a_disclosure_file_in_another_format_is_not_reformatted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root(tmp_path)
    value: dict[str, Any] = json.loads((root / DISCLOSURE).read_bytes())
    (root / DISCLOSURE).write_bytes(
        json.dumps(value).encode()
    )  # compact, not canonical
    _, pin = build_manifest(root, [DISCLOSURE, POLICY, OTHER], cutoff=CUTOFF)
    data_root = tmp_path / "workspace"
    _committed(data_root, NEW_RELEASE, NEW_PANEL, b"{}")
    assert _rebind(root, pin, data_root, tmp_path / "ri-next") == 1
    assert "canonical formatting" in capsys.readouterr().err
