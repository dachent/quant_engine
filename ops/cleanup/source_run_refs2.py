"""Which source-runs entries the live product cites, matched by id not by path.

The first pass matched the literal string "source-runs/<name>" and reported the
890 MB bundle that the panel demonstrably binds as unreferenced. Generations
cite each other by 64-hex generation id, so the path never appears. This pass
collects every id the 20 live generations mention and asks which source-runs
entries contain a directory named by one of them.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys

PORTFOLIO = pathlib.Path(
    "C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
)
STORES = ["dataPortfolio", "dataPortfolio123", "dataIBKR", "dataSchwab",
          "dataMatrixport", "dataQuantbase"]
HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


def live_generation_dirs(keep_path: pathlib.Path) -> tuple[list[pathlib.Path], set[str]]:
    keep = json.loads(keep_path.read_bytes().decode("utf-8"))
    ids = {g for v in keep["keep"].values() for g in v}
    found = []
    for store in STORES:
        base = PORTFOLIO / store
        if not base.exists():
            continue
        for dirpath, dirnames, _ in os.walk(base, onerror=lambda e: None):
            d = pathlib.Path(dirpath)
            if d.name in ids and d.parent.name in ("generations", "candidates"):
                found.append(d)
                dirnames[:] = []
    return found, ids


def main() -> int:
    live, keep_ids = live_generation_dirs(pathlib.Path(sys.argv[1]))
    cited: set[str] = set(keep_ids)
    for gen in live:
        for dirpath, _d, filenames in os.walk(gen, onerror=lambda e: None):
            for name in filenames:
                p = pathlib.Path(dirpath) / name
                try:
                    if p.stat().st_size > 200 * 2**20:
                        continue
                    text = p.read_bytes().decode("utf-8", "replace")
                except OSError:
                    continue
                cited.update(HEX64.findall(text))
    print(f"live generations: {len(live)}")
    print(f"distinct ids they cite: {len(cited):,}\n")

    total_cited = total_unref = 0
    rows = []
    for store in STORES:
        sr = PORTFOLIO / store / "source-runs"
        if not sr.exists():
            continue
        for child in sorted(sr.iterdir()):
            if not child.is_dir():
                continue
            size = 0
            hits = set()
            for dp, _d, fs in os.walk(child, onerror=lambda e: None):
                d = pathlib.Path(dp)
                if d.name in cited:
                    hits.add(d.name)
                for f in fs:
                    try:
                        size += (d / f).lstat().st_size
                    except OSError:
                        pass
            rows.append((bool(hits), size, f"{store}/source-runs/{child.name}", len(hits)))
            if hits:
                total_cited += size
            else:
                total_unref += size

    rows.sort(key=lambda r: (-r[0], -r[1]))
    for is_cited, size, rel, nhits in rows:
        mark = f"CITED({nhits})" if is_cited else "unref   "
        print(f"  {mark:<10} {size / 2**20:9,.1f} MB  {rel}")

    print(f"\ncited by id  {total_cited / 2**30:.2f} GB")
    print(f"unreferenced {total_unref / 2**30:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
