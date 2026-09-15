"""Do the 20 live generations reference the four store run-trees at all?

Checked two ways, because a path-only check already produced one wrong answer
today: by literal path fragment, and by generation id (a generation cites its
inputs by id, so the path never appears).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys

W = pathlib.Path("C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine")
TREES = ["rollforward", "release-runs", "superseded-pipeline-runs", "repairs"]
STORES = ["dataPortfolio", "dataPortfolio123", "dataIBKR", "dataSchwab",
          "dataMatrixport", "dataQuantbase"]
HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


def ext(p) -> str:
    p = os.path.abspath(str(p))
    return p if p.startswith("\\\\?\\") else "\\\\?\\" + p


def main() -> int:
    keep = json.loads(pathlib.Path(sys.argv[1]).read_bytes().decode("utf-8"))
    ids = {g for v in keep["keep"].values() for g in v}

    live = []
    for store in STORES:
        base = W / store
        if not base.exists():
            continue
        for dirpath, dirnames, _ in os.walk(ext(base), onerror=lambda e: None):
            d = pathlib.Path(dirpath)
            if d.name in ids and d.parent.name in ("generations", "candidates"):
                live.append(d)
                dirnames[:] = []

    cited_ids: set[str] = set()
    path_hits: list[str] = []
    for gen in live:
        for dp, _d, fs in os.walk(ext(gen), onerror=lambda e: None):
            for f in fs:
                p = pathlib.Path(dp) / f
                try:
                    if p.stat().st_size > 200 * 2**20:
                        continue
                    text = p.read_bytes().decode("utf-8", "replace")
                except OSError:
                    continue
                cited_ids.update(HEX64.findall(text))
                for t in TREES:
                    if t in text:
                        path_hits.append(f"{gen.name[:12]} -> {t} in {f}")

    print(f"live generations: {len(live)}   ids they cite: {len(cited_ids):,}")
    print(f"\n=== literal path references into the four trees: {len(path_hits)}")
    for h in path_hits[:15]:
        print(f"  {h}")

    print("\n=== id-level overlap ===")
    for store in STORES:
        for t in TREES:
            base = W / store / t
            if not base.exists():
                continue
            hits = set()
            size = 0
            for dp, _d, fs in os.walk(ext(base), onerror=lambda e: None):
                d = pathlib.Path(dp)
                if d.name in cited_ids:
                    hits.add(d.name)
                for f in fs:
                    try:
                        size += os.lstat(os.path.join(dp, f)).st_size
                    except OSError:
                        pass
            flag = f"CITED x{len(hits)}" if hits else "no overlap"
            print(f"  {size / 2**20:9,.1f} MB  {flag:<12} {store}/{t}")
            for h in sorted(hits)[:5]:
                print(f"        {h}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
