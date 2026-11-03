"""The stall watchdog fails a silent command fast and leaves a slow one alone."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
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


def _cp1252_stdout(monkeypatch: pytest.MonkeyPatch) -> io.BytesIO:
    """A stdout that, like a Windows console, cannot encode most of Unicode."""

    raw = io.BytesIO()
    wrapper = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", wrapper)
    return raw


def _emit(*chunks: bytes, gap: float = 0.4) -> list[str]:
    code = (
        "import sys, time\n"
        f"for chunk in {list(chunks)!r}:\n"
        " sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush(); "
        f"time.sleep({gap})"
    )
    return [sys.executable, "-c", code]


def test_characters_outside_the_console_code_page_do_not_kill_the_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _cp1252_stdout(monkeypatch)
    summary = "== 1 failed, 5 passed in 3.2s =="
    command = _emit(
        "start \u2192 \u2713 \u65e5\u672c\n".encode(),
        f"FAILED tests/x.py::test_y\n{summary}\n".encode(),
    )
    assert stall_watchdog.run(command, 5.0) == 0
    sys.stdout.flush()
    out = raw.getvalue().decode("cp1252")
    assert "\\u2192" in out and "\\u65e5" in out
    assert "FAILED tests/x.py::test_y" in out and summary in out


def test_a_multibyte_character_split_across_two_reads_is_decoded_whole(
    capsys: pytest.CaptureFixture[str],
) -> None:
    euro = "\u20ac".encode()
    command = _emit(b"price ", euro[:2], euro[2:], b" done\n")
    assert stall_watchdog.run(command, 5.0) == 0
    out = capsys.readouterr().out
    assert "price \u20ac done" in out
    assert "\ufffd" not in out


def test_invalid_bytes_are_replaced_and_later_output_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _cp1252_stdout(monkeypatch)
    assert stall_watchdog.run(_emit(b"bad \xff\xfe bytes\n", b"tail line\n"), 5.0) == 0
    sys.stdout.flush()
    out = raw.getvalue().decode("cp1252")
    assert "bad " in out and "tail line" in out


def test_a_stalled_child_with_unencodable_last_words_still_exits_124(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _cp1252_stdout(monkeypatch)
    code = (
        "import sys, time\n"
        "sys.stdout.buffer.write(('last ' + chr(0x2713) + ' words\\n').encode()); "
        "sys.stdout.buffer.flush()\ntime.sleep(60)"
    )
    assert stall_watchdog.run([sys.executable, "-c", code], 2.0) == 124
    sys.stdout.flush()
    out = raw.getvalue().decode("cp1252")
    assert "STALL" in out and "last " in out and "words" in out


def test_the_child_exit_code_survives_unencodable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cp1252_stdout(monkeypatch)
    code = (
        "import sys\nsys.stdout.buffer.write((chr(0x2713) + '\\n').encode())\n"
        "sys.stdout.buffer.flush()\nraise SystemExit(7)"
    )
    assert stall_watchdog.run([sys.executable, "-c", code], 5.0) == 7


def test_an_incomplete_sequence_before_a_stall_is_flushed_not_lost(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'end of output ' + bytes([0xE2, 0x82]))\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(60)"
    )
    assert stall_watchdog.run([sys.executable, "-c", code], 2.0) == 124
    out = capsys.readouterr().out
    assert "end of output \ufffd" in out
    assert "STALL" in out
