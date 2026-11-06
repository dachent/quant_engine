"""Governed capture of the TuringTrader public Firebase Storage objects.

Replaces three hand scripts (JSON objects, per-slug text stamps, market vane) with
one create-only, read-only tool. Every request is an HTTP GET of a public object.
Raw response bytes are stored unchanged; nothing is re-encoded.

Usage:
  python tools/tt_public_capture.py capture --out DIR --cutoff YYYY-MM-DD
      --tokens-file FILE

Tokens: three 36-character UUID object tokens (public, allochist, holdings). They
come from a JSON file given by --tokens-file ({"public": ..., "allochist": ...,
"holdings": ...}) that lies OUTSIDE every git repository, or from the environment
variables TT_PUBLIC_TOKEN, TT_ALLOCHIST_TOKEN and TT_HOLDINGS_TOKEN. Tokens are
never written to disk, never printed, and never recorded: URLs in the manifest
carry no token query and every error text is redacted.

Output layout (create-only; an existing OUT is refused):
  json/  portfolio-comp.json and <slug>__<object>.json
  txt/   <slug>__last-update.txt and <slug>__last-rebalance.txt
  vane/  tt-market-vane__status.json
  capture-manifest.json   written last

Freshness for cutoff D: TuringTrader publishes each trading day's data the next
morning (about 06:50Z). Every performance-chart object must end on date D and
every last-update.txt stamp must be >= D+1 06:00Z. A failed check exits 3 with
the exact reason per object; the capture and manifest are still written with
status STALE so the evidence is kept.

Exit codes: 0 complete and fresh, 1 an object failed, 2 refusal or usage error,
3 stale.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Protocol
import urllib.error
import urllib.request

AUTHORITY = "#283 comment 5515808323; #699 comment 6536613285"
SCHEMA = "tt-public-capture.v1"
BASE = "https://firebasestorage.googleapis.com/v0/b/turingtrader-com.appspot.com/o/"
SLUGS = (
    "tt-hokey-pokey",
    "tt-mach-3-v3",
    "tt-mean-kitty-v2",
    "tt-quick-change-v2",
    "tt-vix-spritz-aggro-v3",
    "tt-hot-sauce",
)
JSON_OBJECTS = (
    ("performance-chart.json", "public"),
    ("performance-table.json", "public"),
    ("holdings-history.json", "allochist"),
    ("portfolio-holdings.json", "holdings"),
)
TXT_OBJECTS = ("last-update.txt", "last-rebalance.txt")
VANE_OBJECT = "tt-market-vane/status.json"
TOKEN_KEYS = ("public", "allochist", "holdings")
TOKEN_ENV = {
    "public": "TT_PUBLIC_TOKEN",
    "allochist": "TT_ALLOCHIST_TOKEN",
    "holdings": "TT_HOLDINGS_TOKEN",
}
GROUPS = ("json", "txt", "vane")
_TOKEN_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_QUERY_TOKEN_RE = re.compile(r"(token=)[^&\s'\"]*", re.IGNORECASE)
_STAMP_FORMAT = "%Y-%m-%dT%H:%MZ"
_MIN_UPDATE_HOUR = 6
STATUS_COMPLETE = "COMPLETE"
STATUS_STALE = "STALE"
STATUS_FAILED = "FAILED_OBJECTS"
EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_STALE = 0, 1, 2, 3

Item = tuple[str, str, str, str]


class CaptureRefusal(Exception):
    """A refusal that is safe to print: it never carries token material."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def get(self, url: str, timeout: float) -> HttpResponse: ...


class UrllibTransport:
    """Plain GET through the standard library; any HTTP status is returned."""

    def get(self, url: str, timeout: float) -> HttpResponse:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    response.status, response.read(), dict(response.headers.items())
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(error.code, b"", dict(error.headers.items()))


def redact(text: str, tokens: Sequence[str]) -> str:
    for value in tokens:
        if value:
            text = text.replace(value, "[REDACTED]")
    return _QUERY_TOKEN_RE.sub(r"\1[REDACTED]", text)


def _inside_git_repo(path: Path) -> bool:
    resolved = path.resolve()
    return any((parent / ".git").exists() for parent in (resolved, *resolved.parents))


def load_tokens(
    tokens_file: Path | None, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return the three tokens; messages never contain token values."""
    source: Mapping[str, Any]
    if tokens_file is not None:
        if _inside_git_repo(tokens_file):
            raise CaptureRefusal("tokens file lies inside a git repository; refused")
        try:
            raw = json.loads(tokens_file.read_bytes().decode("utf-8"))
        except (OSError, ValueError):
            raise CaptureRefusal("tokens file is unreadable or not JSON") from None
        if not isinstance(raw, dict):
            raise CaptureRefusal("tokens file must hold a JSON object")
        source = raw
    else:
        env = os.environ if environ is None else environ
        source = {key: env.get(name) for key, name in TOKEN_ENV.items()}
    tokens: dict[str, str] = {}
    for key in TOKEN_KEYS:
        value = source.get(key)
        if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
            raise CaptureRefusal(f"token '{key}' is missing or is not a 36-char UUID")
        tokens[key] = value
    return tokens


def parse_cutoff(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise CaptureRefusal("cutoff must be YYYY-MM-DD") from None


def required_min_update(cutoff: date) -> datetime:
    day = cutoff + timedelta(days=1)
    return datetime(day.year, day.month, day.day, _MIN_UPDATE_HOUR, tzinfo=timezone.utc)


def chart_last_date(body: bytes) -> date:
    """Last row date of a performance-chart object ({"data": [[date, ...], ...]})."""
    rows = json.loads(body.decode("utf-8"))["data"]
    text = str(rows[-1][0])[:10]
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError("unrecognised date format")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _plan(group: str) -> list[Item]:
    """Items as (object, relative path under OUT, storage path, token key)."""
    if group == "json":
        items: list[Item] = [
            (
                "portfolio-comp.json",
                "json/portfolio-comp.json",
                "portfolio-comp.json",
                "public",
            )
        ]
        for slug in SLUGS:
            for name, key in JSON_OBJECTS:
                items.append(
                    (f"{slug}__{name}", f"json/{slug}__{name}", f"{slug}%2F{name}", key)
                )
        return items
    if group == "txt":
        return [
            (f"{slug}/{name}", f"txt/{slug}__{name}", f"{slug}%2F{name}", "public")
            for slug in SLUGS
            for name in TXT_OBJECTS
        ]
    return [
        (
            VANE_OBJECT,
            "vane/tt-market-vane__status.json",
            "tt-market-vane%2Fstatus.json",
            "public",
        )
    ]


def _fetch(
    item: Item,
    group: str,
    out: Path,
    tokens: Mapping[str, str],
    transport: Transport,
    timeout: float,
) -> dict[str, Any]:
    name, rel, storage, key = item
    known = list(tokens.values())
    record: dict[str, Any] = {
        "group": group,
        "object": name,
        "file": rel,
        "url": f"{BASE}{storage}?alt=media",
    }
    try:
        response = transport.get(
            f"{BASE}{storage}?alt=media&token={tokens[key]}", timeout
        )
    except Exception as error:  # network or transport failure
        record["error"] = redact(f"{type(error).__name__}: {error}", known)[:200]
        return record
    record["http_status"] = response.status
    if response.status != 200:
        record["error"] = f"HTTP {response.status}"
        return record
    body = bytes(response.body)
    (out / rel).write_bytes(body)
    record.update(
        bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        etag=_header(response.headers, "ETag"),
        last_modified=_header(response.headers, "Last-Modified"),
    )
    return record


def check_freshness(
    out: Path, objects: Sequence[Mapping[str, Any]], cutoff: date
) -> dict[str, Any]:
    want_chart = cutoff.isoformat()
    want_stamp = required_min_update(cutoff)
    want_stamp_text = want_stamp.strftime(_STAMP_FORMAT)
    checks: list[dict[str, Any]] = []
    for obj in objects:
        name = str(obj["object"])
        found: str
        ok: bool
        if name.endswith("__performance-chart.json"):
            required = f"last chart date == {want_chart}"
            if "sha256" not in obj:
                found, ok = "not captured", False
            else:
                try:
                    last = chart_last_date((out / str(obj["file"])).read_bytes())
                    found, ok = last.isoformat(), last == cutoff
                except (OSError, ValueError, KeyError, IndexError, TypeError):
                    found, ok = "unreadable chart", False
        elif name.endswith("/last-update.txt"):
            required = f"stamp >= {want_stamp_text}"
            if "sha256" not in obj:
                found, ok = "not captured", False
            else:
                text = (
                    (out / str(obj["file"]))
                    .read_bytes()
                    .decode("utf-8", "replace")
                    .strip()
                )
                try:
                    parsed = datetime.strptime(text, _STAMP_FORMAT).replace(
                        tzinfo=timezone.utc
                    )
                    found, ok = text, parsed >= want_stamp
                except ValueError:
                    found, ok = "unparseable stamp: " + text[:40], False
        else:
            continue
        checks.append({"object": name, "found": found, "required": required, "ok": ok})
    stale = [c for c in checks if not c["ok"]]
    return {
        "result": "STALE" if stale else "FRESH",
        "required_chart_date": want_chart,
        "required_min_last_update": want_stamp_text,
        "checks": checks,
        "reasons": [
            f"{c['object']}: found {c['found']}; required {c['required']}"
            for c in stale
        ],
    }


def run_capture(
    out: Path,
    cutoff: date,
    tokens: Mapping[str, str],
    transport: Transport,
    *,
    groups: Sequence[str] = GROUPS,
    timeout: float = 90.0,
    clock: Callable[[], datetime] = _now,
    stream: Any = None,
) -> tuple[int, dict[str, Any]]:
    stream = sys.stdout if stream is None else stream
    if out.exists():
        raise CaptureRefusal("output directory already exists; capture is create-only")
    out.mkdir(parents=True, exist_ok=False)
    for group in GROUPS:
        (out / group).mkdir()
    captured_at: dict[str, str] = {}
    objects: list[dict[str, Any]] = []
    for group in GROUPS:
        if group not in groups:
            continue
        captured_at[group] = _stamp(clock())
        for item in _plan(group):
            record = _fetch(item, group, out, tokens, transport, timeout)
            objects.append(record)
            print(
                f"{record['object']}\t{record.get('http_status')}\t{record.get('bytes')}"
                f"\t{record.get('error')}",
                file=stream,
            )
    freshness = check_freshness(out, objects, cutoff)
    failed = [o for o in objects if "error" in o]
    if failed:
        status = STATUS_FAILED
    elif freshness["result"] == "STALE":
        status = STATUS_STALE
    else:
        status = STATUS_COMPLETE
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "status": status,
        "authority": AUTHORITY,
        "cutoff": cutoff.isoformat(),
        "captured_at_utc": captured_at,
        "freshness": freshness,
        "objects": objects,
        "sha256_list": sorted(
            (
                {"file": o["file"], "sha256": o["sha256"]}
                for o in objects
                if "sha256" in o
            ),
            key=lambda entry: str(entry["file"]),
        ),
    }
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (out / "capture-manifest.json").write_bytes(payload)
    print(f"status {status}", file=stream)
    for reason in freshness["reasons"]:
        print(f"STALE {reason}", file=stream)
    for obj in failed:
        print(f"FAILED {obj['object']}: {obj['error']}", file=stream)
    code = EXIT_FAILED if failed else EXIT_STALE if status == STATUS_STALE else EXIT_OK
    return code, manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TuringTrader public capture")
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture", help="capture all three groups")
    cap.add_argument("--out", type=Path, required=True)
    cap.add_argument("--cutoff", required=True, help="YYYY-MM-DD")
    cap.add_argument("--tokens-file", type=Path, default=None)
    cap.add_argument("--groups", default=",".join(GROUPS))
    cap.add_argument("--timeout", type=float, default=90.0)
    return parser


def main(argv: Sequence[str] | None = None, transport: Transport | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        groups = [g for g in args.groups.split(",") if g]
        if not groups or any(g not in GROUPS for g in groups):
            raise CaptureRefusal("groups must be a subset of json,txt,vane")
        cutoff = parse_cutoff(args.cutoff)
        if args.out.exists():
            raise CaptureRefusal(
                "output directory already exists; capture is create-only"
            )
        tokens = load_tokens(args.tokens_file)
        code, _ = run_capture(
            args.out,
            cutoff,
            tokens,
            transport or UrllibTransport(),
            groups=groups,
            timeout=args.timeout,
        )
    except CaptureRefusal as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return EXIT_REFUSED
    return code


if __name__ == "__main__":
    raise SystemExit(main())
