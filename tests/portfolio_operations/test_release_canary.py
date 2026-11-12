"""Kill tests for the commit-path canary: each fails if the matching check is absent."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

_PATH = Path(__file__).resolve().parents[2] / "tools" / "release_canary.py"
_SPEC = importlib.util.spec_from_file_location("release_canary", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
canary_module = importlib.util.module_from_spec(_SPEC)
sys.modules["release_canary"] = canary_module
_SPEC.loader.exec_module(canary_module)

FP = {"windows_build": "1.1", "volumes": {}}


def _fp(_roots: object) -> dict[str, object]:
    return dict(FP)


def _run(tmp_path: Path, **kwargs: object) -> dict[str, object]:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    return canary_module.canary(
        [root], kwargs.pop("bound_files", None), fingerprint_fn=_fp, **kwargs
    )  # type: ignore[arg-type]


def _status(receipt: dict[str, object], prefix: str) -> str:
    return next(c["status"] for c in receipt["checks"] if c["id"].startswith(prefix))  # type: ignore[index, union-attr]


def _scratch_left(tmp_path: Path) -> list[Path]:
    return list(tmp_path.glob(".canary-*"))


def test_clean_run_passes_and_leaves_roots_untouched(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "CURRENT.json").write_bytes(b'{"g":1}')
    before = hashlib.sha256((root / "CURRENT.json").read_bytes()).hexdigest()
    listing = sorted(p.name for p in root.iterdir())
    receipt = _run(tmp_path)
    assert receipt["status"] == "PASS"
    assert _status(receipt, "handles") == "SKIPPED_NOT_AVAILABLE"
    assert hashlib.sha256((root / "CURRENT.json").read_bytes()).hexdigest() == before
    assert sorted(p.name for p in root.iterdir()) == listing
    assert _scratch_left(tmp_path) == []


@pytest.mark.parametrize("bit", [0x400000, 0x1000, 0x40000])
def test_placeholder_attribute_fails(tmp_path: Path, bit: int) -> None:
    bound = tmp_path / "gen.json"
    bound.write_bytes(b"x")

    class Fs(canary_module.RealFs):
        def attributes(self, path: Path) -> int:
            return bit

    receipt = _run(tmp_path, bound_files=[bound], fs=Fs())
    assert receipt["status"] == "FAIL"
    assert _status(receipt, "bound_files_local") == "FAIL"


def test_bound_file_content_is_never_read(tmp_path: Path) -> None:
    bound = tmp_path / "gen.json"
    bound.write_bytes(b"x")

    class Fs(canary_module.RealFs):
        def attributes(self, path: Path) -> int:
            return 0

    # attributes() is the only per-file access in the check
    assert canary_module.check_bound_files([bound], Fs())["status"] == "PASS"


@pytest.mark.parametrize("failing", ["write", "replace", "lock", "rename", "unlink"])
def test_injected_error_fails_and_scratch_is_removed(
    tmp_path: Path, failing: str
) -> None:
    class Fs(canary_module.RealFs):
        pass

    def boom(self: object, *args: object) -> None:
        raise PermissionError(13, "denied")

    setattr(Fs, failing, boom)
    receipt = _run(tmp_path, fs=Fs())
    assert receipt["status"] == "FAIL"
    assert _status(receipt, "fs_ops") == "FAIL"
    assert _scratch_left(tmp_path) == []


def test_slow_operation_fails_against_baseline(tmp_path: Path) -> None:
    ticks = iter(
        range(0, 10_000, 5)
    )  # every clock read advances 5 s: above the 2 s floor
    receipt = _run(tmp_path, now=lambda: float(next(ticks)))
    assert _status(receipt, "fs_ops") == "FAIL"
    assert _scratch_left(tmp_path) == []


def test_baseline_scales_the_limit(tmp_path: Path) -> None:
    ticks = iter(range(0, 10_000, 5))
    baseline = {
        "fs_ops": {
            n: 1.0 for n in ("create", "write", "replace", "lock", "rename", "delete")
        }
    }
    receipt = _run(tmp_path, now=lambda: float(next(ticks)), baseline=baseline)
    assert _status(receipt, "fs_ops") == "PASS"  # 5 s <= 10 x 1 s


def test_handles_required_without_lister_fails(tmp_path: Path) -> None:
    assert _run(tmp_path, require_handles=True)["status"] == "FAIL"


def test_handle_holder_fails(tmp_path: Path) -> None:
    receipt = _run(tmp_path, handle_lister=lambda roots: ["proc-a"])
    assert receipt["status"] == "FAIL"
    assert _status(receipt, "handles") == "FAIL"


def test_fingerprint_diff_detected(tmp_path: Path) -> None:
    a = {"fingerprint": {"windows_build": "26100.1", "onedrive_client": "1"}}
    b = {"fingerprint": {"windows_build": "26100.2", "onedrive_client": "1"}}
    assert canary_module.diff_fingerprints(a, a) == []
    assert canary_module.diff_fingerprints(a, b) == [
        "windows_build: '26100.1' -> '26100.2'"
    ]
    left, right = tmp_path / "a.json", tmp_path / "b.json"
    left.write_text(json.dumps(a), encoding="utf-8")
    right.write_text(json.dumps(b), encoding="utf-8")
    assert canary_module.main(["diff", str(left), str(right)]) == 1
    assert canary_module.main(["diff", str(left), str(left)]) == 0


def test_real_fingerprint_is_value_free(tmp_path: Path) -> None:
    text = json.dumps(canary_module.fingerprint([tmp_path]))
    assert str(tmp_path) not in text
    assert "windows_build" in text and "packages_sha256" in text


def test_cli_exit_codes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    out = tmp_path / "receipt.json"
    assert canary_module.main(["run", "--root", str(root), "--out", str(out)]) == 0
    assert json.loads(out.read_bytes())["status"] == "PASS"
    assert canary_module.main(["run"]) == 3
    assert (
        canary_module.main(
            ["run", "--root", str(root), "--baseline", str(tmp_path / "missing")]
        )
        == 3
    )


def test_cleanup_failure_fails(tmp_path: Path) -> None:
    class Fs(canary_module.RealFs):
        def rmtree(self, path: Path) -> None:
            raise PermissionError(13, "locked")

    receipt = _run(tmp_path, fs=Fs())
    assert receipt["status"] == "FAIL"
    detail = next(
        c["detail"] for c in receipt["checks"] if c["id"].startswith("fs_ops")
    )  # type: ignore[index, union-attr]
    assert "cleanup" in detail


def test_prior_receipt_is_a_usable_baseline(tmp_path: Path) -> None:
    ticks = iter(range(0, 10_000, 5))
    root = tmp_path / "root"
    root.mkdir()
    steps = ("create", "write", "replace", "lock", "rename", "delete")
    prior = {"timings": {"x": {n: 1.0 for n in steps}}}
    receipt = canary_module.canary(
        [root], None, baseline=prior, now=lambda: float(next(ticks)), fingerprint_fn=_fp
    )
    assert _status(receipt, "fs_ops") == "PASS"


def test_no_roots_is_a_usage_error() -> None:
    with pytest.raises(canary_module.CanaryError):
        canary_module.canary([], None, fingerprint_fn=_fp)


def test_unwritable_receipt_is_usage_error(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    out = tmp_path / "no" / "such" / "receipt.json"
    assert canary_module.main(["run", "--root", str(root), "--out", str(out)]) == 3


def test_bound_directory_is_walked(tmp_path: Path) -> None:
    folder = tmp_path / "gen"
    folder.mkdir()
    (folder / "a.json").write_bytes(b"x")

    class Fs(canary_module.RealFs):
        def attributes(self, path: Path) -> int:
            return 0x400000

    assert canary_module.check_bound_files([folder], Fs())["status"] == "FAIL"
