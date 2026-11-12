"""Rendered-UI check of a published panel (#912), for the post-release verification.

Opens the published panel in headless Chromium (``published_render_check.mjs``), compares what
the DOM shows with the panel's own payload as declared in ``render_contract.json`` (banner,
headline figures, chart end labels, banners that contradict the release) and writes one
create-only, value-free receipt plus a screenshot into ``--out-dir``. Exit 0 on PASS, 1 on a
failed check or a missing runtime/page, 2 on a usage error. Nothing is read from the network.

    python tools/published_render_check.py --panel-dir <published panel dir> --out-dir <dir>
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "apps" / "portfolio-control-panel"
SCRIPT = APP / "published_render_check.mjs"
CONTRACT = APP / "render_contract.json"
RECEIPT_SCHEMA = "qe-published-render-receipt/1"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(panel_dir: Path, out_dir: Path, contract: Path = CONTRACT) -> dict[str, object]:
    """Run the check; returns the receipt (also written, create-only, to out_dir)."""

    index = panel_dir / "index.html"
    payload = panel_dir / "data" / "dashboard_data.json"
    receipt_path = out_dir / "published-render-receipt.json"
    if receipt_path.exists():
        raise SystemExit("PUBLISHED_RENDER_OUTPUT_EXISTS: outputs are create-only")
    out_dir.mkdir(parents=True, exist_ok=True)
    node = shutil.which("node")
    module = Path(
        os.environ.get("PLAYWRIGHT_MODULE")
        or APP / "node_modules" / "playwright" / "index.mjs"
    )
    receipt: dict[str, object] = {
        "schema": RECEIPT_SCHEMA,
        "contract_sha256": _sha(contract),
        "script_sha256": _sha(SCRIPT),
    }
    if not index.is_file() or not payload.is_file():
        receipt.update(status="FAIL", code="PUBLISHED_PANEL_FILES_MISSING", checks=[])
    elif node is None or not module.is_file():
        receipt.update(status="FAIL", code="BROWSER_RUNTIME_UNAVAILABLE", checks=[])
    else:
        receipt.update(panel_index_sha256=_sha(index), payload_sha256=_sha(payload))
        env = {**os.environ, "PLAYWRIGHT_MODULE": str(module)}
        done = subprocess.run(
            [
                node,
                str(SCRIPT),
                "--html",
                str(index),
                "--contract",
                str(contract),
                "--output",
                str(out_dir),
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            check=False,
        )
        result_file = out_dir / "render-check.json"
        if done.returncode in (0, 1) and result_file.is_file():
            result = json.loads(result_file.read_text(encoding="utf-8"))
            wanted = {
                c["id"] for c in json.loads(contract.read_text("utf-8"))["checks"]
            }
            got = {c["id"] for c in result["checks"]}
            if not wanted or wanted != got:
                receipt.update(
                    status="FAIL", code="CHECK_RESULTS_INCOMPLETE", checks=[]
                )
            else:
                receipt.update(status=result["status"], checks=result["checks"])
        else:
            receipt.update(status="FAIL", code="RENDER_CHECK_DID_NOT_RUN", checks=[])
    with receipt_path.open("xb") as handle:
        handle.write((json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--panel-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = run(args.panel_dir, args.out_dir)
    for check in receipt.get("checks", []):  # type: ignore[attr-defined]
        if check["status"] != "PASS":
            print(f"PUBLISHED_RENDER: {check['id']} {check['code']}")
    if receipt.get("code"):
        print(f"PUBLISHED_RENDER: {receipt['code']}")
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
