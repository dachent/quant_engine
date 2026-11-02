"""Regression: tests/conftest.py keeps the module registry independent of collection order.

Each case builds a throwaway project with the real ``tests/conftest.py`` and runs pytest on
it in a subprocess. A control run without the conftest proves the scenario really breaks
the registry, so a passing case cannot be a vacuous one.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFTEST = REPO / "tests" / "conftest.py"

TOOL = "VALUE = 1\n"

# Loads the tool under its importable name at module level and never restores it, the
# pattern used by many test modules.
SWAPPER = (
    "import importlib.util, pathlib, sys\n"
    "SPEC = importlib.util.spec_from_file_location(\n"
    "    'hazmod', pathlib.Path(__file__).resolve().parents[1] / 'tools' / 'hazmod.py')\n"
    "FRESH = importlib.util.module_from_spec(SPEC)\n"
    "sys.modules[SPEC.name] = FRESH\n"
    "SPEC.loader.exec_module(FRESH)\n"
    "def test_own_binding_is_usable():\n"
    "    assert FRESH.VALUE == 1\n"
)

SWAPPER_PINNED = SWAPPER + (
    "def test_registry_holds_my_own_binding():\n"
    "    assert sys.modules['hazmod'] is FRESH\n"
)

FIRST_IMPORTER = (
    "import hazmod\n"
    "ORIGINAL = hazmod\n"
    "def test_first_importer_sees_registered_module():\n"
    "    assert hazmod.VALUE == 1\n"
)

REGISTRY_CHECK = (
    "import sys\n"
    "import test_a_first\n"
    "def test_registry_still_holds_the_first_importers_instance():\n"
    "    assert sys.modules['hazmod'] is test_a_first.ORIGINAL\n"
)

RUNTIME_SWAP = (
    "import sys\n"
    "import test_a_first\n"
    "def test_swaps_inside_a_test_body():\n"
    "    sys.modules['hazmod'] = type(sys)('hazmod')\n"
    "def test_next_test_sees_the_original():\n"
    "    assert sys.modules['hazmod'] is test_a_first.ORIGINAL\n"
)

UNRELATED = (
    "import sys\n"
    "def test_unowned_entries_are_left_alone(monkeypatch):\n"
    "    sys.modules['not_repo_owned'] = type(sys)('not_repo_owned')\n"
)

UNRELATED_CHECK = (
    "import sys\n"
    "def test_added_entry_from_previous_test_is_kept():\n"
    "    assert 'not_repo_owned' in sys.modules\n"
)


def _project(root: Path, files: dict[str, str], *, with_conftest: bool) -> Path:
    root.mkdir()
    (root / "tools").mkdir()
    (root / "tools" / "hazmod.py").write_text(TOOL, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "pytest.ini").write_text(
        "[pytest]\npythonpath = tools\naddopts = -p no:cacheprovider\n",
        encoding="utf-8",
    )
    if with_conftest:
        shutil.copyfile(CONFTEST, root / "tests" / "conftest.py")
    for name, text in files.items():
        (root / "tests" / name).write_text(text, encoding="utf-8")
    return root


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_collection_time_swap_cannot_replace_an_earlier_importers_module(
    tmp_path: Path,
) -> None:
    files = {
        "test_a_first.py": FIRST_IMPORTER,
        "test_b_swap.py": SWAPPER,
        "test_c_check.py": REGISTRY_CHECK,
    }
    control = _run(_project(tmp_path / "control", files, with_conftest=False))
    assert control.returncode != 0, control.stdout
    assert "test_registry_still_holds_the_first_importers_instance" in control.stdout

    guarded = _run(_project(tmp_path / "guarded", files, with_conftest=True))
    assert guarded.returncode == 0, guarded.stdout + guarded.stderr
    assert "3 passed" in guarded.stdout


def test_swap_inside_a_test_body_is_undone_before_the_next_test(tmp_path: Path) -> None:
    files = {"test_a_first.py": FIRST_IMPORTER, "test_b_runtime.py": RUNTIME_SWAP}
    control = _run(_project(tmp_path / "control", files, with_conftest=False))
    assert control.returncode != 0, control.stdout
    assert "test_next_test_sees_the_original" in control.stdout

    guarded = _run(_project(tmp_path / "guarded", files, with_conftest=True))
    assert guarded.returncode == 0, guarded.stdout + guarded.stderr


def test_entries_the_repository_does_not_own_are_not_touched(tmp_path: Path) -> None:
    files = {"test_a_add.py": UNRELATED, "test_b_check.py": UNRELATED_CHECK}
    guarded = _run(_project(tmp_path / "guarded", files, with_conftest=True))
    assert guarded.returncode == 0, guarded.stdout + guarded.stderr


def test_each_test_sees_the_modules_its_own_file_bound(tmp_path: Path) -> None:
    files = {
        "test_a_first.py": FIRST_IMPORTER,
        "test_b_swap.py": SWAPPER_PINNED,
        "test_c_swap.py": SWAPPER_PINNED,
    }
    control = _run(_project(tmp_path / "control", files, with_conftest=False))
    assert control.returncode != 0, control.stdout
    assert "test_b_swap.py::test_registry_holds_my_own_binding" in control.stdout

    guarded = _run(_project(tmp_path / "guarded", files, with_conftest=True))
    assert guarded.returncode == 0, guarded.stdout + guarded.stderr
