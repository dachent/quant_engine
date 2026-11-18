"""Owner-eyes rendered-panel checks with numeric assertions (#912, review Q1).

Live (needs Playwright's Chromium, present on hosted CI):
    python tools/owner_eyes_render.py --panel-dir <generation dir> --out-dir <dir>
Dry (no browser; evaluates a captured DOM snapshot against the payload, used by the tests and
on a machine without Chromium):
    python tools/owner_eyes_render.py --snapshot <snapshot.json> --payload <dashboard_data.json> --out-dir <dir>

Exit 0 on PASS, 1 on a failed check, 3 when the live browser runtime is unavailable (the
release run treats 3 as NOT RUN, never as a pass), 2 on a usage error.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "apps" / "portfolio-control-panel" / "owner_eyes_render.mjs"
EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_NO_RUNTIME = 0, 1, 2, 3
RESULT = "owner-eyes-render.json"


def run(args: argparse.Namespace) -> int:
    node = shutil.which("node")
    if node is None:
        print("OWNER_EYES_RENDER: node is unavailable")
        return EXIT_NO_RUNTIME
    args.out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if args.snapshot:
        command = [
            node,
            str(SCRIPT),
            "--snapshot",
            str(args.snapshot),
            "--payload",
            str(args.payload),
            "--output",
            str(args.out_dir),
        ]
    else:
        module = Path(
            os.environ.get("PLAYWRIGHT_MODULE")
            or ROOT
            / "apps"
            / "portfolio-control-panel"
            / "node_modules"
            / "playwright"
            / "index.mjs"
        )
        if not module.is_file():
            print("OWNER_EYES_RENDER: the Playwright module is unavailable")
            return EXIT_NO_RUNTIME
        env["PLAYWRIGHT_MODULE"] = str(module)
        command = [
            node,
            str(SCRIPT),
            "--html",
            str(args.panel_dir / "index.html"),
            "--output",
            str(args.out_dir),
        ]
    done = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        check=False,
    )
    if done.stderr:
        print(done.stderr.strip())
    result = args.out_dir / RESULT
    if done.returncode in (0, 1) and result.is_file():
        for check in json.loads(result.read_text("utf-8"))["checks"]:
            if check["status"] != "PASS":
                print(
                    f"OWNER_EYES_RENDER: {check['id']} {check['code']} ({check['count']})"
                )
        return EXIT_PASS if done.returncode == 0 else EXIT_FAIL
    print("OWNER_EYES_RENDER: the check did not run")
    return EXIT_FAIL


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--panel-dir", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.snapshot and not args.panel_dir:
        parser.error(
            "--panel-dir (live) or --snapshot with --payload (dry) is required"
        )
    if args.snapshot and not args.payload:
        parser.error("--payload is required with --snapshot")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
