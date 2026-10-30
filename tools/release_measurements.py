"""Release evidence measurements that are NOT part of the hash critical path.

The #540 payload sizes and interaction QA are cheap and always run; the two browser
loads (6-7 min each, they measure load time) run only when the generation differs from
the previous one by more than ``--threshold`` of its bytes, or on ``--force-loads``, and
never while a replica ceremony is running in ``--replica-out-dir`` (the ceremony writes
``ceremony.running`` there). Everything written here is evidence: ``gate`` is always
false and no exit code of a measurement can block a release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from quant_engine.core import canonical_json_bytes

CEREMONY_RUNNING = "ceremony.running"
DATA = Path("data") / "dashboard_data.json"


class MeasurementError(RuntimeError):
    """A named refusal of this tool."""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(root: Path) -> dict[str, tuple[int, str]]:
    return {
        p.relative_to(root).as_posix(): (p.stat().st_size, _digest(p))
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def sizes(generation: Path, top: int = 14) -> dict[str, Any]:
    path = generation / DATA
    data = json.loads(path.read_bytes())
    total = path.stat().st_size
    rows = sorted(
        ((len(json.dumps(v, separators=(",", ":"))), k) for k, v in data.items()),
        reverse=True,
    )
    return {
        "dashboard_data_bytes": total,
        "files": sum(1 for p in generation.rglob("*") if p.is_file()),
        "generation_bytes": sum(
            p.stat().st_size for p in generation.rglob("*") if p.is_file()
        ),
        "largest_blocks": [
            {"block": k, "bytes": n, "share": round(n / total, 4)}
            for n, k in rows[:top]
        ],
    }


def diff(old: Path, new: Path) -> dict[str, Any]:
    """Byte diff of two generations and, for the dashboard payload, per-block changes."""

    a, b = _tree(old), _tree(new)
    changed = sorted(k for k in a.keys() & b.keys() if a[k][1] != b[k][1])
    added, removed = sorted(b.keys() - a.keys()), sorted(a.keys() - b.keys())
    total = sum(v[0] for v in b.values())
    data_key = DATA.as_posix()
    # The dashboard payload is one file; count only its changed blocks, not the whole file.
    whole = [k for k in (*changed, *added) if k != data_key]
    changed_bytes = sum(b[k][0] for k in whole)
    blocks: list[str] = []
    if (old / DATA).is_file() and (new / DATA).is_file():
        da, db = (
            json.loads((old / DATA).read_bytes()),
            json.loads((new / DATA).read_bytes()),
        )
        blocks = sorted(
            k
            for k in da.keys() | db.keys()
            if canonical_json_bytes([da.get(k)]) != canonical_json_bytes([db.get(k)])
        )
        changed_bytes += sum(
            len(json.dumps(db.get(k), separators=(",", ":"))) for k in blocks
        )
    elif data_key in changed or data_key in added:
        changed_bytes += b[data_key][0]
    return {
        "added": added,
        "changed": changed,
        "changed_blocks": blocks,
        "changed_bytes": changed_bytes,
        "changed_fraction": round(changed_bytes / total, 6) if total else 0.0,
        "removed": removed,
        "unchanged": len(a.keys() & b.keys()) - len(changed),
    }


def _run(argv: Sequence[str], log: Path) -> int:
    with log.open("wb") as handle:
        return subprocess.run(
            list(argv), stdout=handle, stderr=subprocess.STDOUT, check=False
        ).returncode


def run(a: argparse.Namespace) -> dict[str, Any]:
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    gen = Path(a.generation)
    result: dict[str, Any] = {
        "gate": False,
        "generation": gen.name,
        "sizes": sizes(gen),
    }
    fraction = 1.0
    if a.previous is not None:
        result["diff"] = diff(Path(a.previous), gen)
        fraction = float(result["diff"]["changed_fraction"])
    if a.epoch_manifest is not None:
        qa = [
            "node", "apps/portfolio-control-panel/interaction_qa.mjs",
            "--html", str(gen / "index.html"),
            "--output", str(out / "interaction-epoch.json"),
            "--expected-calculation-cutoff", a.cutoff,
            "--epoch-manifest", str(a.epoch_manifest),
        ]  # fmt: skip
        result["interaction_epoch_rc"] = _run(qa, out / "interaction-epoch.log")
    want_loads = a.load_cmd is not None and (a.force_loads or fraction > a.threshold)
    result["loads"] = {"threshold": a.threshold, "ran": False, "reason": "not needed"}
    if want_loads:
        if (
            a.replica_out_dir is not None
            and (Path(a.replica_out_dir) / CEREMONY_RUNNING).exists()
        ):
            raise MeasurementError(
                "a replica ceremony is running; loads measure load time and must not overlap it"
            )
        template = json.loads(a.load_cmd)
        rcs = []
        for i in (1, 2):
            argv = [
                s.replace("{html}", str(gen / "index.html")).replace(
                    "{out}", str(out / f"load-{i}.json")
                )
                for s in template
            ]
            rcs.append(_run(argv, out / f"load-{i}.log"))
        result["loads"] = {"threshold": a.threshold, "ran": True, "rcs": rcs}
    elif a.load_cmd is not None:
        result["loads"]["reason"] = (
            f"changed fraction {fraction} <= threshold {a.threshold}"
        )
    (out / "measurements.json").write_bytes(canonical_json_bytes(result))
    return result


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--generation", type=Path, required=True)
    p.add_argument("--previous", type=Path)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--cutoff", default="")
    p.add_argument("--epoch-manifest", type=Path)
    p.add_argument(
        "--load-cmd",
        help='JSON argv template with {html} and {out}, e.g. ["node","load.mjs","{html}","{out}"]',
    )
    p.add_argument("--threshold", type=float, default=0.01)
    p.add_argument("--force-loads", action="store_true")
    p.add_argument("--replica-out-dir", type=Path)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    try:
        print(json.dumps(run(a), sort_keys=True))
    except (MeasurementError, OSError, ValueError) as exc:
        print(f"release-measurements: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
