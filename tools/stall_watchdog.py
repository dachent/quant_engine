"""Run a command and fail it only when it stops making progress.

A fixed job timeout turns a slow runner into a cancelled required check. This
wrapper instead kills the command when it prints nothing for ``--stall-seconds``
and reports how long the longest silence was, so a hang fails fast with its last
output while a merely slow run is left alone. The hosted job keeps a generous
``timeout-minutes`` as a backstop only.

Usage: ``python tools/stall_watchdog.py --stall-seconds 900 -- python -m pytest -q``
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from collections import deque
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

STALL_EXIT = 124
TAIL_LINES = 25


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.kill()


def _summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def run(command: Sequence[str], stall_seconds: float) -> int:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=sys.platform != "win32",
    )
    assert process.stdout is not None
    lines: queue.Queue[bytes | None] = queue.Queue()

    def pump() -> None:
        assert process.stdout is not None
        for raw in iter(process.stdout.readline, b""):
            lines.put(raw)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    started = last = time.monotonic()
    longest = 0.0
    while True:
        try:
            raw = lines.get(timeout=1.0)
        except queue.Empty:
            if time.monotonic() - last > stall_seconds:
                _kill_tree(process)
                print(
                    f"\nSTALL: no output for {stall_seconds:.0f}s; killed. Last output:",
                    flush=True,
                )
                print("\n".join(tail), flush=True)
                _summary(
                    f"**stall watchdog**: killed after {stall_seconds:.0f}s of silence "
                    f"(run {time.monotonic() - started:.0f}s)"
                )
                return STALL_EXIT
            continue
        if raw is None:
            break
        now = time.monotonic()
        longest = max(longest, now - last)
        last = now
        text = raw.decode("utf-8", errors="replace")
        tail.append(text.rstrip("\n"))
        sys.stdout.write(text)
        sys.stdout.flush()
    code = process.wait()
    longest = max(longest, time.monotonic() - last)
    _summary(
        f"**stall watchdog**: exit {code}, run {time.monotonic() - started:.0f}s, "
        f"longest silence {longest:.0f}s (limit {stall_seconds:.0f}s)"
    )
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stall-seconds", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    if command[0] in ("python", "python3"):
        # Hosted runners put their own Python first on PATH, not the project venv.
        command = [sys.executable, *command[1:]]
    return run(command, args.stall_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
