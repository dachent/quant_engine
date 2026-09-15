"""Reduce a captured HAR to the part that is data, then drop the original.

A capture is ~2% JSON and ~65% JavaScript, with a further ~36% of its bytes
belonging to third-party trackers (#422). Deleting whole HARs would throw away
the 2%; keeping them costs 13.5 GB to hold 270 MB of signal. So each HAR is
rewritten keeping only entries whose response body is JSON, and only from the
provider's own origin, which is exactly what #422's `targets` mode will capture
natively.

The slim file keeps the HAR shape so existing parsers still read it, and records
what was dropped so the reduction is auditable rather than silent.

One file is parsed at a time and the original is removed only after the slim
version is written and re-read, so an interrupted run loses nothing.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

KEEP_HOSTS = ("portfolio123.com",)
KEEP_MIME = ("json",)


def ext(p) -> str:
    p = os.path.abspath(str(p))
    return p if p.startswith("\\\\?\\") else "\\\\?\\" + p


def slim(path: pathlib.Path, keep_original: bool) -> tuple[int, int, str]:
    original = path.stat().st_size
    doc = json.loads(path.read_bytes().decode("utf-8", "replace"))
    log = doc["log"]
    entries = log.get("entries", [])

    kept = []
    dropped_bytes = 0
    for e in entries:
        content = e.get("response", {}).get("content", {})
        mime = (content.get("mimeType") or "").lower()
        url = e.get("request", {}).get("url", "")
        host = url.split("/")[2] if "://" in url else ""
        body = content.get("text") or ""
        if any(m in mime for m in KEEP_MIME) and any(h in host for h in KEEP_HOSTS):
            kept.append(e)
        else:
            dropped_bytes += len(body)
            # Keep the request/response metadata, drop only the body: the call
            # sequence is provenance, the minified bundle is not.
            if body:
                content["text"] = ""
                content["_bodyDroppedBytes"] = len(body)
            kept.append(e)

    log["entries"] = kept
    log["_slimmed"] = {
        "tool": "slim_har.py",
        "kept_bodies_from": list(KEEP_HOSTS),
        "kept_bodies_mime": list(KEEP_MIME),
        "dropped_body_bytes": dropped_bytes,
        "original_bytes": original,
        "issue": "https://github.com/dachent/quant_engine/issues/422",
    }

    out = path.with_suffix(".slim.har")
    data = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(ext(out), "wb") as fh:
        fh.write(data)

    # Re-read before removing anything.
    check = json.loads(pathlib.Path(ext(out)).read_bytes().decode("utf-8"))
    assert len(check["log"]["entries"]) == len(kept), "slim file did not round-trip"

    if not keep_original:
        os.remove(ext(path))
    return original, len(data), out.name


def main() -> int:
    root = pathlib.Path(sys.argv[1])
    keep = {a for a in sys.argv[2:]}
    hars = sorted(
        (p for p in root.rglob("*.har") if not p.name.endswith(".slim.har")),
        key=lambda p: p.stat().st_size,
    )
    print(f"{len(hars)} HAR files, {sum(p.stat().st_size for p in hars) / 2**30:.2f} GB\n")

    before = after = 0
    for i, p in enumerate(hars, 1):
        keep_original = any(k in p.name for k in keep)
        try:
            o, n, name = slim(p, keep_original)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(hars)}] ERROR {p.name}: {e}", flush=True)
            continue
        before += o
        after += n
        tag = "  (original kept: evidence for #412)" if keep_original else ""
        print(
            f"  [{i}/{len(hars)}] {o / 2**20:9,.1f} MB -> {n / 2**20:7,.1f} MB  {name}{tag}",
            flush=True,
        )

    print(f"\n=== {before / 2**30:.2f} GB -> {after / 2**30:.2f} GB")
    print(f"=== reclaimed {(before - after) / 2**30:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
