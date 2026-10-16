"""The stall watchdog fails a silent command fast and leaves a slow one alone."""

from __future__ import annotations

from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

import stall_watchdog  # noqa: E402


def test_slow_but_chatty_command_is_not_killed() -> None:
    code = "import time\nfor i in range(4):\n print(i, flush=True)\n time.sleep(0.6)"
    assert stall_watchdog.run([sys.executable, "-c", code], 2.0) == 0


def test_silent_command_is_killed_with_stall_exit(capsys: object) -> None:
    code = "import time\nprint('last words', flush=True)\ntime.sleep(60)"
    assert stall_watchdog.run([sys.executable, "-c", code], 2.0) == 124
    assert "last words" in capsys.readouterr().out  # type: ignore[attr-defined]


def test_exit_code_is_passed_through() -> None:
    assert stall_watchdog.run([sys.executable, "-c", "raise SystemExit(3)"], 5.0) == 3


def test_leading_python_runs_the_current_interpreter(capsys: object) -> None:
    code = "import sys; print(sys.executable)"
    assert (
        stall_watchdog.main(["--stall-seconds", "5", "--", "python", "-c", code]) == 0
    )
    assert sys.executable in capsys.readouterr().out  # type: ignore[attr-defined]


def test_progress_without_a_newline_counts_as_output() -> None:
    code = (
        "import sys, time\nfor i in range(4):\n"
        " sys.stdout.write('.'); sys.stdout.flush(); time.sleep(0.6)"
    )
    assert stall_watchdog.run([sys.executable, "-c", code], 2.0) == 0
