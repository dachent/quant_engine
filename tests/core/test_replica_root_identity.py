"""A copied root against an R2-format predecessor: the recorded identities are the REAL
directories'; a marked replica copy is accepted only through the copy's identity map."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from quant_engine.core import release as rel
from quant_engine.core.replica_marker import REPLICA_MARKER_KIND, REPLICA_MARKER_NAME

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "release_replica_identity_tool", ROOT / "tools" / "release_replica.py"
)
assert _SPEC is not None and _SPEC.loader is not None
rr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rr)


def _world(tmp: Path) -> dict[str, Any]:
    """A real workspace (coordination + one root with a companion parent dir) and a spec."""

    real = tmp / "real"
    (real / "root-a" / "sub").mkdir(parents=True)
    (real / "root-a" / "sub" / "file.json").write_text("{}", encoding="utf-8")
    (real / "coord").mkdir()
    spec = {
        "roots": {"root-a": str(real / "root-a")},
        "coordination_root": str(real / "coord"),
        "companions": [
            {"companion_id": "c", "root_id": "root-a", "relative_path": "sub/file.json"}
        ],
    }
    path = tmp / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return {"real": real, "spec": path}


def _manifest(real: Path) -> Any:
    """What a committed R2 manifest recorded: the REAL directories' identities."""

    return SimpleNamespace(
        release_schema_version=rel._SCHEMA_VERSION,
        targets=[
            SimpleNamespace(
                root_id="root-a", root_identity=rel._root_identity(real / "root-a")
            )
        ],
        companions=[
            SimpleNamespace(
                root_id="root-a",
                relative_path="sub/file.json",
                parent_identity=rel._root_identity(real / "root-a" / "sub"),
            )
        ],
    )


def _replica(tmp: Path, w: dict[str, Any]) -> Path:
    replica = tmp / "replica"
    rr.copy_replica(w["spec"], w["real"], replica)
    return replica


def test_real_roots_pass_and_refuse_a_marker_inside_a_root(tmp_path: Path) -> None:
    w = _world(tmp_path)
    manifest = _manifest(w["real"])
    roots = {"root-a": w["real"] / "root-a"}
    rel._validate_root_identities(manifest, roots)
    (w["real"] / "root-a" / REPLICA_MARKER_NAME).write_text(
        json.dumps({"kind": REPLICA_MARKER_KIND, "schema_version": 1}), encoding="utf-8"
    )
    with pytest.raises(rel.ReleaseConflictError, match="contains a replica marker"):
        rel._validate_root_identities(manifest, roots)


def test_copy_with_identity_map_is_accepted_for_root_and_lease_expectations(
    tmp_path: Path,
) -> None:
    w = _world(tmp_path)
    manifest = _manifest(w["real"])
    replica = _replica(tmp_path, w)
    roots = {"root-a": replica / "root-a"}
    rel._validate_root_identities(manifest, roots)
    expected = rel._release_lease_expectations(manifest, roots)
    assert expected[roots["root-a"]] == rel._root_identity(roots["root-a"])
    assert expected[roots["root-a"] / "sub"] == rel._root_identity(
        roots["root-a"] / "sub"
    )


def test_copy_without_a_map_entry_is_refused(tmp_path: Path) -> None:
    w = _world(tmp_path)
    manifest = _manifest(w["real"])
    replica = _replica(tmp_path, w)
    marker_path = replica / REPLICA_MARKER_NAME
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["identity_map"] = []
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(rel.ReleaseConflictError, match="identity changed"):
        rel._validate_root_identities(manifest, {"root-a": replica / "root-a"})


def test_a_copy_replaced_after_the_map_was_written_is_refused(tmp_path: Path) -> None:
    w = _world(tmp_path)
    manifest = _manifest(w["real"])
    replica = _replica(tmp_path, w)
    shutil.rmtree(replica / "root-a")
    shutil.copytree(w["real"] / "root-a", replica / "root-a")
    with pytest.raises(rel.ReleaseConflictError, match="identity changed"):
        rel._validate_root_identities(manifest, {"root-a": replica / "root-a"})


def test_map_whose_source_is_not_the_recorded_identity_is_refused(
    tmp_path: Path,
) -> None:
    w = _world(tmp_path)
    manifest = _manifest(w["real"])
    replica = _replica(tmp_path, w)
    marker_path = replica / REPLICA_MARKER_NAME
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    for entry in marker["identity_map"]:
        entry["source"] = "0" * 64
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(rel.ReleaseConflictError, match="identity changed"):
        rel._validate_root_identities(manifest, {"root-a": replica / "root-a"})


def test_marker_that_overlaps_its_source_workspace_is_not_honored(
    tmp_path: Path,
) -> None:
    w = _world(tmp_path)
    manifest = _manifest(w["real"])
    replica = _replica(tmp_path, w)
    marker_path = replica / REPLICA_MARKER_NAME
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["source_workspace"] = str(tmp_path)  # contains the replica: not disjoint
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(rel.ReleaseConflictError, match="identity changed"):
        rel._validate_root_identities(manifest, {"root-a": replica / "root-a"})
