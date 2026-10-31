"""Passive, read-only recorder of Portfolio123 responses for one attended sitting.

The owner signs in to a dedicated Edge or Chrome profile that has a loopback debug
port and browses by hand. This tool only LISTENS to the browser's Network events.
It never navigates, clicks, types, submits, or injects script: every message it can
send to the browser is on a closed allow-list (target discovery, attach, enable
Network, read one finished response body) and anything else is refused before it
leaves the process. It never reads or stores request headers, cookies, or the
Authorization value, it records only same-origin GET responses on
https://www.portfolio123.com (exact host allow-list below), and it refuses to record
any non-GET request and any GET whose path or query looks like a mutation; those are
listed as ``MUTATION_LIKE_NOT_RECORDED`` with the path template only.

Modes:
  discover         value-free metadata (method, path template with identifiers
                   replaced by ``{id}``, query key names, status, media type, body
                   shape = field names and types, counts); no body is kept.
  capture          raw bodies of recorded GET JSON, HTML, and CSV responses, one
                   SHA-256 each, in a create-only run directory; manifest last.
  verify           re-hash a finished run, re-derive every metadata field from the
                   raw bytes, and re-check that nothing refused was recorded.
  propose-catalog  turn a verified discover (or capture) run into a DRAFT catalog
                   diff plus a skeleton test file (field names, no values). The
                   draft is never applied.

Raw bodies are restricted evidence (docs/restricted-evidence-output.md): keep them
only in the governed root the owner chooses. Stdout carries counts, route
templates, and status, never contents.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import socket
import struct
import sys
import time
from typing import Any, Protocol
from urllib.parse import parse_qsl, unquote, unquote_plus, urlsplit
import urllib.request

from quant_engine.portfolio123 import catalog_rows


ORIGIN = "https://www.portfolio123.com"
#: Exact hosts that may be recorded. Data, not a flag: widening it is a code change.
ALLOWED_HOSTS: tuple[str, ...] = ("www.portfolio123.com",)
SCHEMA = "p123-cdp-recorder/1"
PROPOSAL_SCHEMA = "p123-cdp-recorder-proposal/1"
STATUS_DISCOVERY = "DISCOVERY"
STATUS_CAPTURE = "CAPTURE_COMPLETE"
STATUS_CAPTURE_PARTIAL = "CAPTURE_WITH_UNRECORDED_BODIES"
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_SHAPE_DEPTH = 12
MAX_KEYS_BEFORE_KEYED = 64

#: A GET whose decoded path or query contains one of these is not recorded.
MUTATION_WORDS: tuple[str, ...] = (
    "revert",
    "delete",
    "remove",
    "save",
    "submit",
    "update",
    "create",
    "rebalanc",
    "trade",
    "order",
    "subscri",
    "cancel",
    "logout",
    "password",
    "token",
)
#: Billing and identity pages: not recorded unless ``--include-identity`` names them.
IDENTITY_WORDS: tuple[str, ...] = (
    "account",
    "profile",
    "billing",
    "invoice",
    "payment",
    "user",
)
#: Integer values of these query keys are pagination positions, kept as counts.
PAGINATION_KEYS = frozenset({"offset", "page", "limit", "start", "size", "count"})

#: The only messages this process may send to the browser. Passive by construction.
ALLOWED_CDP_METHODS = frozenset(
    {
        "Target.setDiscoverTargets",
        "Target.attachToTarget",
        "Network.enable",
        "Network.getResponseBody",
    }
)

#: What the run directory says about itself; verify requires it unchanged.
SAFETY = {
    "passive": True,
    "navigates": False,
    "injects_script": False,
    "reads_request_headers": False,
    "reads_cookies": False,
    "persists_query_values": False,
    "records_non_get": False,
}


class RecorderError(ValueError):
    """The run or request is refused; ``code`` is a stable reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


_MUTATION_RE = re.compile("|".join(MUTATION_WORDS))
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_HEX_RE = re.compile(r"[0-9a-f]{12,}", re.IGNORECASE)
_TOKENISH_RE = re.compile(r"[A-Za-z0-9_~+=-]{24,}")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_US_DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}")
_LABEL_RE = re.compile(r"[A-Za-z][A-Za-z0-9 %/#._()-]{0,40}")
_PLAIN_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------
# URL classification (pure)


@dataclass(frozen=True)
class Decision:
    """What the recorder does with one request."""

    kind: str  # OFFORIGIN, NON_GET, MUTATION_LIKE, IDENTITY, RECORD
    template: str = ""
    query_keys: tuple[str, ...] = ()
    query_flags: tuple[tuple[str, str], ...] = ()
    pagination: tuple[tuple[str, int], ...] = ()
    reason: str = ""
    words: tuple[str, ...] = ()


def _segment_template(segment: str) -> str:
    segment = segment.split(";", 1)[0]
    if not segment:
        return segment
    if segment.isdigit() or _UUID_RE.fullmatch(segment) or _HEX_RE.fullmatch(segment):
        return "{id}"
    if _TOKENISH_RE.fullmatch(segment) and any(c.isdigit() for c in segment):
        return "{id}"
    return re.sub(r"\d{3,}", "{id}", segment)


def path_template(path: str) -> str:
    """Replace identifiers by ``{id}`` and drop path parameters (session ids)."""

    return "/".join(_segment_template(item) for item in unquote(path).split("/"))


def _key_name(key: str) -> str:
    if key.isdigit() or len(key) > 64:
        return "{key}"
    return key


def _screen(path: str, query: str, include_identity: frozenset[str]) -> tuple[str, str]:
    """Return (kind, detail) for a decoded path and query text."""

    text = (unquote_plus(path) + "?" + unquote_plus(query)).lower()
    if _MUTATION_RE.search(text):
        return "MUTATION_LIKE", "PATTERN"
    found = [word for word in IDENTITY_WORDS[:-1] if word in text]
    if "user" in {item.split(";", 1)[0] for item in unquote(path).lower().split("/")}:
        found.append("user")
    if any(word not in include_identity for word in found):
        return "IDENTITY", ",".join(sorted(found))
    return "RECORD", ""


def origin_allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname in ALLOWED_HOSTS
        and port in (None, 443)
        and parts.username is None
        and parts.password is None
    )


def classify(
    method: str, url: str, include_identity: frozenset[str] = frozenset()
) -> Decision:
    """Decide, from the method and URL alone, whether a response may be recorded."""

    if not origin_allowed(url):
        return Decision("OFFORIGIN")
    parts = urlsplit(url)
    template = path_template(parts.path)
    if method.upper() != "GET":
        return Decision("NON_GET", template, reason="NON_GET")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kind, detail = _screen(parts.path, parts.query, include_identity)
    if kind == "MUTATION_LIKE":
        return Decision("MUTATION_LIKE", template, reason=detail)
    if kind == "IDENTITY":
        return Decision("IDENTITY", template, words=tuple(detail.split(",")))
    flags = tuple(
        sorted(
            {
                (_key_name(k), v.lower())
                for k, v in pairs
                if v.lower() in {"true", "false"}
            }
        )
    )
    pagination = tuple(
        sorted(
            {
                (k, int(v))
                for k, v in pairs
                if k in PAGINATION_KEYS and v.isascii() and v.isdigit() and len(v) <= 9
            }
        )
    )
    return Decision(
        "RECORD",
        template,
        tuple(sorted({_key_name(k) for k, _ in pairs})),
        flags,
        pagination,
    )


def media_class(content_type: str) -> str | None:
    media = content_type.split(";", 1)[0].strip().lower()
    if media in {"application/json", "text/json"} or media.endswith("+json"):
        return "json"
    if media == "text/html":
        return "html"
    if media in {"text/csv", "application/csv"}:
        return "csv"
    return None


# ---------------------------------------------------------------------------
# Value-free body shapes (pure)


def _scalar_kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "num"
    if isinstance(value, str):
        if _ISO_DATE_RE.fullmatch(value):
            try:
                date.fromisoformat(value)
                return "date"
            except ValueError:
                return "str"
        if _US_DATE_RE.fullmatch(value):
            try:
                datetime.strptime(value, "%m/%d/%Y")
                return "us_date"
            except ValueError:
                return "str"
        return "str"
    return "str"


def _keyed(keys: Sequence[str]) -> bool:
    return len(keys) > MAX_KEYS_BEFORE_KEYED or any(
        key.isdigit() or len(key) > 64 or _ISO_DATE_RE.fullmatch(key) for key in keys
    )


def shape_of(value: object, depth: int = 0) -> Any:
    """Field names and types of a JSON value; never a value.

    Objects are ``{name: shape}``; arrays ``{"$array": item, "$len_min": n,
    "$len_max": n}``; objects keyed by data (many keys, numeric or date keys)
    collapse to ``{"$keyed": shape, "$keys": n}`` so identifiers are not emitted.
    """

    if depth > MAX_SHAPE_DEPTH:
        return "deep"
    if isinstance(value, dict):
        keys = [str(key) for key in value]
        if _keyed(keys):
            merged: Any = None
            for item in list(value.values())[:500]:
                merged = merge_shapes(merged, shape_of(item, depth + 1))
            return {"$keyed": merged, "$keys": len(keys)}
        return {key: shape_of(value[key], depth + 1) for key in sorted(keys)}
    if isinstance(value, list):
        item_shape: Any = None
        for item in value[:500]:
            item_shape = merge_shapes(item_shape, shape_of(item, depth + 1))
        return {"$array": item_shape, "$len_min": len(value), "$len_max": len(value)}
    return _scalar_kind(value)


def _type_names(shape: Any) -> set[str]:
    if isinstance(shape, str):
        return set(shape.split("|"))
    if isinstance(shape, dict):
        if "$array" in shape:
            return {"array"}
        if "$keyed" in shape:
            return {"keyed"}
        return {"object"}
    return {"empty"}


def merge_shapes(left: Any, right: Any) -> Any:
    """Combine two shapes; keys missing on one side become ``$optional``."""

    if left is None:
        return right
    if right is None:
        return left
    if isinstance(left, dict) and isinstance(right, dict):
        if "$array" in left and "$array" in right:
            return {
                "$array": merge_shapes(left["$array"], right["$array"]),
                "$len_min": min(left["$len_min"], right["$len_min"]),
                "$len_max": max(left["$len_max"], right["$len_max"]),
            }
        if "$keyed" in left and "$keyed" in right:
            return {
                "$keyed": merge_shapes(left["$keyed"], right["$keyed"]),
                "$keys": max(left["$keys"], right["$keys"]),
            }
        if not any(k in left or k in right for k in ("$array", "$keyed")):
            lkeys = {k for k in left if k != "$optional"}
            rkeys = {k for k in right if k != "$optional"}
            merged: dict[str, Any] = {
                key: merge_shapes(left.get(key), right.get(key))
                for key in sorted(lkeys | rkeys)
            }
            optional = (
                set(left.get("$optional", ()))
                | set(right.get("$optional", ()))
                | (lkeys ^ rkeys)
            )
            if optional:
                merged["$optional"] = sorted(optional)
            return merged
    names = _type_names(left) | _type_names(right)
    if "int" in names and "num" in names:
        names.discard("int")
    return "|".join(sorted(names))


def _label(text: str) -> str:
    cleaned = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())
    return cleaned if _LABEL_RE.fullmatch(cleaned) else "?"


def html_profile(body: bytes) -> dict[str, Any]:
    """Counts and header labels of an HTML page; never cell text."""

    text = body.decode("utf-8", "replace")
    tables: list[dict[str, Any]] = []
    for raw in re.findall(r"<table\b.*?</table>", text, re.IGNORECASE | re.DOTALL)[:10]:
        rows = re.findall(r"<tr\b.*?</tr>", raw, re.IGNORECASE | re.DOTALL)
        headers = re.findall(r"<th\b[^>]*>(.*?)</th>", raw, re.IGNORECASE | re.DOTALL)
        tables.append(
            {
                "data_rows": sum(
                    1 for row in rows if re.search(r"<td\b", row, re.IGNORECASE)
                ),
                "header_cells": [_label(item) for item in headers[:40]],
            }
        )
    return {
        "tables": len(re.findall(r"<table\b", text, re.IGNORECASE)),
        "table_profiles": tables,
        "forms": len(re.findall(r"<form\b", text, re.IGNORECASE)),
        "links": len(re.findall(r"<a\b", text, re.IGNORECASE)),
    }


def csv_profile(body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8-sig", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0] if rows else []
    return {
        "columns": [_label(item) for item in header[:60]],
        "column_count": len(header),
        "data_rows": max(0, len([r for r in rows if r]) - 1),
    }


def describe_body(media: str, body: bytes) -> dict[str, Any]:
    """The metadata derived from one body; verify recomputes exactly this."""

    if media == "json":
        try:
            value = json.loads(body.decode("utf-8-sig"), parse_constant=_no_constant)
        except (ValueError, UnicodeDecodeError):
            return {"body_shape": "not-json"}
        return {"body_shape": shape_of(value)}
    if media == "html":
        return {"html": html_profile(body)}
    return {"csv": csv_profile(body)}


def _no_constant(name: str) -> object:
    raise ValueError(name)


# ---------------------------------------------------------------------------
# Output directory rules and files


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _write_new(path: Path, payload: bytes) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        raise RecorderError("ARTIFACT_EXISTS", path.name) from exc
    return {"sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}


def _sync_roots() -> list[Path]:
    roots = []
    for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value).resolve())
    return roots


def _below_sync_root(resolved: Path) -> tuple[str, ...] | None:
    """Path parts below a cloud-sync root, or None when the path is not synced."""

    for base in _sync_roots():
        if base in resolved.parents:
            return resolved.relative_to(base).parts
    for index, part in enumerate(resolved.parts):
        if part.lower().startswith("onedrive") or "sharepoint" in part.lower():
            return resolved.parts[index + 1 :]
    return None


def assert_output_root(root: Path) -> None:
    """The run directory must be absolute, new, outside Git, outside synced scratch."""

    if not root.is_absolute():
        raise RecorderError("OUTPUT_ROOT_NOT_ABSOLUTE")
    resolved = root.resolve()
    if resolved.exists():
        raise RecorderError("OUTPUT_ROOT_EXISTS")
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            raise RecorderError("OUTPUT_ROOT_IN_REPOSITORY")
    below = _below_sync_root(resolved)
    if below is not None and {"scratch", "tmp", "temp"} & {p.lower() for p in below}:
        raise RecorderError("OUTPUT_ROOT_IN_SYNCED_SCRATCH")


# ---------------------------------------------------------------------------
# Recorder (pure: no browser)


class Recorder:
    """Decide per response what, if anything, is persisted."""

    def __init__(
        self,
        root: Path,
        *,
        mode: str,
        include_identity: Sequence[str] = (),
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        if mode not in {"discover", "capture"}:
            raise RecorderError("MODE_INVALID")
        include = frozenset(include_identity)
        if not include <= set(IDENTITY_WORDS):
            raise RecorderError("INCLUDE_IDENTITY_INVALID")
        assert_output_root(root)
        root.mkdir(parents=True)
        self.root = root
        self.mode = mode
        self.include_identity = include
        self.max_body_bytes = max_body_bytes
        self.started = datetime.now(timezone.utc).isoformat()
        self.entries: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        self.offorigin = 0
        self.aborted = 0
        self.mutation_like: Counter[tuple[str, str, str]] = Counter()
        self.identity: Counter[tuple[str, str]] = Counter()
        self.oversize: Counter[str] = Counter()
        self.unreadable = 0
        self.browser_closed = False

    def handle(
        self,
        *,
        method: str,
        url: str,
        status: int,
        content_type: str,
        read_body: Callable[[], bytes],
        resource_type: str = "",
    ) -> None:
        """Record one response. ``read_body`` is called only when allowed."""

        decision = classify(method, url, self.include_identity)
        if decision.kind == "OFFORIGIN":
            self.offorigin += 1
            return
        if decision.kind in {"NON_GET", "MUTATION_LIKE"}:
            self.mutation_like[
                (method.upper(), decision.template, decision.reason)
            ] += 1
            return
        if decision.kind == "IDENTITY":
            self.identity[(decision.template, ",".join(decision.words))] += 1
            return
        media = media_class(content_type)
        entry: dict[str, Any] = {
            "ordinal": len(self.entries),
            "method": "GET",
            "path_template": decision.template,
            "query_keys": list(decision.query_keys),
            "query_flags": [list(item) for item in decision.query_flags],
            "pagination": [list(item) for item in decision.pagination],
            "status": status,
            "media_type": content_type.split(";", 1)[0].strip().lower(),
            "media_class": media,
            "resource_type": resource_type,
        }
        self.entries.append(entry)
        if media is None or not 200 <= status < 300:
            return
        try:
            body = bytes(read_body())
        except Exception:  # noqa: BLE001 - evicted or aborted bodies are unreadable
            self.unreadable += 1
            entry["body_note"] = "unreadable"
            return
        if len(body) > self.max_body_bytes:
            self.oversize[decision.template] += 1
            entry["body_note"] = "oversize"
            return
        entry["body_bytes"] = len(body)
        entry["body_sha256"] = hashlib.sha256(body).hexdigest()
        entry.update(describe_body(media, body))
        if self.mode == "capture":
            name = f"raw/{entry['ordinal']:05d}.body"
            record = _write_new(self.root / name, body)
            entry["raw_path"] = name
            self.artifacts.append({"path": name, **record})

    def finish(self) -> dict[str, Any]:
        """Write the entries file, then the manifest last."""

        name = "discovery.json" if self.mode == "discover" else "entries.json"
        self.artifacts.append(
            {"path": name, **_write_new(self.root / name, _canonical(self.entries))}
        )
        recorded = [e for e in self.entries if "body_sha256" in e]
        failures = bool(self.unreadable or self.oversize)
        manifest: dict[str, Any] = {
            "schema": SCHEMA,
            "mode": self.mode,
            "origin": ORIGIN,
            "allowed_hosts": list(ALLOWED_HOSTS),
            "run_id": self.root.name,
            "started_at": self.started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": (
                STATUS_DISCOVERY
                if self.mode == "discover"
                else STATUS_CAPTURE_PARTIAL
                if failures
                else STATUS_CAPTURE
            ),
            "safety": dict(SAFETY),
            "include_identity": sorted(self.include_identity),
            "counts": {
                "entries": len(self.entries),
                "bodies_seen": len(recorded),
                "raw_bodies": sum(1 for e in self.entries if "raw_path" in e),
                "offorigin": self.offorigin,
                "aborted": self.aborted,
                "unreadable_bodies": self.unreadable,
                "mutation_like_not_recorded": sum(self.mutation_like.values()),
                "identity_not_recorded": sum(self.identity.values()),
            },
            "browser_closed": self.browser_closed,
            "routes": route_summary(self.entries),
            "pagination": pagination_summary(self.entries),
            "MUTATION_LIKE_NOT_RECORDED": [
                {"method": m, "path_template": t, "reason": r, "count": n}
                for (m, t, r), n in sorted(self.mutation_like.items())
            ],
            "IDENTITY_BILLING_NOT_RECORDED": [
                {"path_template": t, "words": w.split(","), "count": n}
                for (t, w), n in sorted(self.identity.items())
            ],
            "OVERSIZE_NOT_RECORDED": [
                {"path_template": t, "count": n}
                for t, n in sorted(self.oversize.items())
            ],
            "artifacts": sorted(self.artifacts, key=lambda a: str(a["path"])),
        }
        _write_new(self.root / "manifest.json", _canonical(manifest))
        return manifest


def route_summary(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate counts per (template, query keys, status, media type)."""

    counts: Counter[tuple[str, tuple[str, ...], int, str]] = Counter(
        (
            str(e["path_template"]),
            tuple(e["query_keys"]),
            int(e["status"]),
            str(e["media_type"]),
        )
        for e in entries
    )
    return [
        {
            "method": "GET",
            "path_template": t,
            "query_keys": list(k),
            "status": s,
            "media_type": m,
            "count": n,
        }
        for (t, k, s, m), n in sorted(counts.items())
    ]


def pagination_summary(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per route and pagination key: distinct positions and whether they are gapless."""

    seen: dict[tuple[str, str], set[int]] = {}
    for entry in entries:
        if not 200 <= int(entry["status"]) < 300:
            continue
        for key, value in entry.get("pagination", ()):
            seen.setdefault((str(entry["path_template"]), str(key)), set()).add(
                int(value)
            )
    result = []
    for (template, key), values in sorted(seen.items()):
        ordered = sorted(values)
        diffs = {b - a for a, b in zip(ordered, ordered[1:])}
        result.append(
            {
                "path_template": template,
                "key": key,
                "distinct": len(ordered),
                "min": ordered[0],
                "max": ordered[-1],
                "step": min(diffs) if diffs else None,
                "contiguous": len(diffs) <= 1,
            }
        )
    return result


# ---------------------------------------------------------------------------
# Verify


def _load_run(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RecorderError("MANIFEST_UNREADABLE") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise RecorderError("MANIFEST_SCHEMA")
    if manifest.get("safety") != SAFETY:
        raise RecorderError("MANIFEST_SAFETY_BLOCK_DIFFERS")
    name = "discovery.json" if manifest.get("mode") == "discover" else "entries.json"
    try:
        entries = json.loads((root / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RecorderError("ENTRIES_UNREADABLE") from None
    if not isinstance(entries, list):
        raise RecorderError("ENTRIES_UNREADABLE")
    return manifest, entries


def verify_run(root: Path) -> dict[str, object]:
    """Re-hash every artifact, reject strays, re-derive metadata, re-screen routes."""

    manifest, entries = _load_run(root)
    mode = manifest["mode"]
    if mode not in {"discover", "capture"}:
        raise RecorderError("MODE_INVALID")
    listed = {item["path"]: item for item in manifest["artifacts"]}
    on_disk = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    if on_disk != set(listed):
        raise RecorderError("RUN_DIRECTORY_DIFFERS_FROM_MANIFEST")
    for path, item in listed.items():
        if hashlib.sha256((root / path).read_bytes()).hexdigest() != item["sha256"]:
            raise RecorderError("ARTIFACT_HASH_DIFFERS", path)
    include = frozenset(manifest.get("include_identity", ()))
    raw_count = 0
    for entry in entries:
        query_text = "&".join(f"{key}=x" for key in entry["query_keys"])
        if entry["method"] != "GET":
            raise RecorderError("NON_GET_RECORDED")
        if _screen(entry["path_template"], query_text, include)[0] != "RECORD":
            raise RecorderError("REFUSED_ROUTE_RECORDED", entry["path_template"])
        raw_path = entry.get("raw_path")
        if mode == "discover" and raw_path is not None:
            raise RecorderError("DISCOVER_RUN_HOLDS_RAW")
        if isinstance(raw_path, str):
            raw_count += 1
            raw = (root / raw_path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry["body_sha256"]:
                raise RecorderError("RAW_HASH_DIFFERS", raw_path)
            media = entry["media_class"]
            fresh = describe_body(media, raw)
            if any(entry.get(key) != value for key, value in fresh.items()):
                raise RecorderError("DERIVED_METADATA_DIFFERS", raw_path)
    if manifest["routes"] != route_summary(entries):
        raise RecorderError("ROUTE_SUMMARY_DIFFERS")
    return {
        "status": manifest["status"],
        "mode": mode,
        "artifact_count": len(listed),
        "entries": len(entries),
        "raw_bodies": raw_count,
    }


# ---------------------------------------------------------------------------
# CDP transport: stdlib WebSocket client + the allow-list guard


class CdpTransport(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...

    def receive(self, timeout: float) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


class GuardedTransport:
    """Wrap a transport so only :data:`ALLOWED_CDP_METHODS` can ever be sent."""

    def __init__(self, inner: CdpTransport) -> None:
        self._inner = inner

    def send(self, message: dict[str, Any]) -> None:
        if message.get("method") not in ALLOWED_CDP_METHODS:
            raise RecorderError("CDP_METHOD_REFUSED", str(message.get("method")))
        self._inner.send(message)

    def receive(self, timeout: float) -> dict[str, Any] | None:
        return self._inner.receive(timeout)

    def close(self) -> None:
        self._inner.close()


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_MESSAGE = 128 * 1024 * 1024
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def encode_frame(opcode: int, payload: bytes, mask_key: bytes) -> bytes:
    """One masked client frame (RFC 6455)."""

    length = len(payload)
    head = bytes([0x80 | opcode])
    if length < 126:
        head += bytes([0x80 | length])
    elif length < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack(">H", length)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", length)
    masked = bytes(byte ^ mask_key[i % 4] for i, byte in enumerate(payload))
    return head + mask_key + masked


def decode_message(
    read_exact: Callable[[int], bytes],
) -> tuple[int, bytes]:
    """Read one complete (possibly fragmented) message: (opcode, payload)."""

    message = b""
    first_opcode = 0
    while True:
        b0, b1 = read_exact(2)
        fin, opcode = b0 & 0x80, b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", read_exact(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", read_exact(8))
        if length + len(message) > _MAX_MESSAGE:
            raise RecorderError("CDP_MESSAGE_TOO_LARGE")
        masked = b1 & 0x80
        key = read_exact(4) if masked else b""
        payload = read_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
        if opcode in {0x8, 0x9, 0xA}:
            return opcode, payload
        if opcode != 0:
            first_opcode = opcode
        message += payload
        if fin:
            return first_opcode, message


def parse_cdp_url(cdp_url: str) -> tuple[str, int]:
    """Only a loopback ``http://host:port`` debug endpoint is accepted."""

    parts = urlsplit(cdp_url)
    try:
        port = parts.port
    except ValueError:
        port = None
    if parts.scheme != "http" or parts.hostname not in _LOOPBACK or port is None:
        raise RecorderError("CDP_URL_NOT_LOOPBACK_HTTP")
    return str(parts.hostname), port


class WebSocketTransport:  # pragma: no cover - needs a live browser
    """Minimal blocking WebSocket client for a loopback CDP endpoint."""

    def __init__(self, cdp_url: str) -> None:
        host, port = parse_cdp_url(cdp_url)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://{host}:{port}/json/version", timeout=10) as reply:
                ws_url = str(json.loads(reply.read())["webSocketDebuggerUrl"])
        except Exception as exc:  # noqa: BLE001
            raise RecorderError("CDP_ENDPOINT_UNREACHABLE") from exc
        ws = urlsplit(ws_url)
        if ws.scheme != "ws" or ws.hostname not in _LOOPBACK:
            raise RecorderError("CDP_ENDPOINT_NOT_LOOPBACK")
        self._sock = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {ws.path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode("ascii"))
        reply_head = b""
        while b"\r\n\r\n" not in reply_head:
            chunk = self._sock.recv(1)
            if not chunk:
                raise RecorderError("CDP_HANDSHAKE_FAILED")
            reply_head += chunk
        text = reply_head.decode("latin-1")
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode()
        if " 101 " not in text.split("\r\n", 1)[0] or accept not in text:
            raise RecorderError("CDP_HANDSHAKE_FAILED")

    def _read_exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            try:
                chunk = self._sock.recv(count - len(data))
            except socket.timeout:
                continue
            if not chunk:
                raise RecorderError("CDP_CLOSED")
            data += chunk
        return data

    def send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        self._sock.sendall(encode_frame(0x1, payload, os.urandom(4)))

    def receive(self, timeout: float) -> dict[str, Any] | None:
        self._sock.settimeout(timeout)
        try:
            first = self._sock.recv(1)
        except socket.timeout:
            return None
        if not first:
            raise RecorderError("CDP_CLOSED")
        pending = [first]

        def read_exact(count: int) -> bytes:
            if pending:
                head = pending.pop()
                return head + self._read_exact(count - 1) if count > 1 else head
            return self._read_exact(count)

        self._sock.settimeout(30)
        opcode, payload = decode_message(read_exact)
        if opcode == 0x8:
            raise RecorderError("CDP_CLOSED")
        if opcode == 0x9:
            self._sock.sendall(encode_frame(0xA, payload, os.urandom(4)))
            return self.receive(timeout)
        if opcode != 0x1:
            return self.receive(timeout)
        decoded = json.loads(payload)
        return decoded if isinstance(decoded, dict) else None

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Network event listener (passive)


_HANDLED_NETWORK_EVENTS = frozenset(
    {
        "Network.requestWillBeSent",
        "Network.responseReceived",
        "Network.loadingFinished",
        "Network.loadingFailed",
    }
)


class Listener:
    """Attach to signed-in P123 page targets and feed finished responses.

    Reads only: request method and URL, response status and mime type, and the
    finished body. Header objects, ``*ExtraInfo`` events (cookies), and request
    post data are never touched.
    """

    def __init__(
        self,
        transport: CdpTransport,
        recorder: Recorder,
        *,
        max_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        call_timeout: float = 20.0,
    ) -> None:
        self._transport = GuardedTransport(transport)
        self._recorder = recorder
        self._max_seconds = max_seconds
        self._clock = clock
        self._call_timeout = call_timeout
        self._next_id = 0
        self._pending: deque[dict[str, Any]] = deque()
        self._attached: dict[str, str] = {}
        self._sessions: set[str] = set()
        self._requests: dict[tuple[str, str], dict[str, Any]] = {}

    def _call(
        self, method: str, params: dict[str, Any], sid: str | None
    ) -> dict[str, Any]:
        self._next_id += 1
        message: dict[str, Any] = {
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        if sid is not None:
            message["sessionId"] = sid
        self._transport.send(message)
        deadline = self._clock() + self._call_timeout
        while True:
            reply = self._transport.receive(0.5)
            if reply is None:
                if self._clock() > deadline:
                    raise RecorderError("CDP_CALL_TIMEOUT", method)
                continue
            if reply.get("id") == self._next_id:
                if "error" in reply:
                    raise RecorderError("CDP_CALL_FAILED", method)
                result = reply.get("result", {})
                return result if isinstance(result, dict) else {}
            if "id" not in reply:
                self._pending.append(reply)

    def run(self) -> None:
        started = self._clock()
        try:
            self._call("Target.setDiscoverTargets", {"discover": True}, None)
            while True:
                if self._max_seconds and self._clock() - started >= self._max_seconds:
                    return
                message = (
                    self._pending.popleft()
                    if self._pending
                    else self._transport.receive(0.5)
                )
                if message is not None:
                    self._dispatch(message)
        except RecorderError as exc:
            if exc.code != "CDP_CLOSED":
                raise
            self._recorder.browser_closed = True

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return
        if method in {"Target.targetCreated", "Target.targetInfoChanged"}:
            self._maybe_attach(message)
            return
        if method == "Target.targetDestroyed":
            params = message.get("params")
            if isinstance(params, dict):
                self._attached.pop(str(params.get("targetId")), None)
            return
        if method not in _HANDLED_NETWORK_EVENTS:
            # Includes every *ExtraInfo event (cookies, raw headers): never opened.
            return
        sid = message.get("sessionId")
        if isinstance(sid, str) and sid in self._sessions:
            self._network_event(sid, method, message.get("params"))

    def _maybe_attach(self, message: dict[str, Any]) -> None:
        params = message.get("params")
        info = params.get("targetInfo") if isinstance(params, dict) else None
        if not isinstance(info, dict) or info.get("type") != "page":
            return
        target_id, url = info.get("targetId"), info.get("url")
        if not isinstance(target_id, str) or target_id in self._attached:
            return
        if not isinstance(url, str) or not origin_allowed(url):
            return
        try:
            result = self._call(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}, None
            )
            sid = str(result["sessionId"])
            self._call(
                "Network.enable",
                {
                    "maxTotalBufferSize": 256 * 1024 * 1024,
                    "maxResourceBufferSize": 64 * 1024 * 1024,
                },
                sid,
            )
        except (RecorderError, KeyError) as exc:
            if isinstance(exc, RecorderError) and exc.code == "CDP_CLOSED":
                raise
            return
        self._attached[target_id] = sid
        self._sessions.add(sid)

    def _network_event(self, sid: str, method: str, params: object) -> None:
        if not isinstance(params, dict):
            return
        request_id = params.get("requestId")
        if not isinstance(request_id, str):
            return
        key = (sid, request_id)
        if method == "Network.requestWillBeSent":
            request = params["request"]
            redirect = params.get("redirectResponse")
            previous = self._requests.pop(key, None)
            if previous is not None and isinstance(redirect, dict):
                self._finish(previous, int(redirect["status"]), "", None)
            self._requests[key] = {
                "method": str(request["method"]),
                "url": str(request["url"]),
            }
        elif method == "Network.responseReceived":
            current = self._requests.get(key)
            response = params["response"]
            if current is not None:
                current["status"] = int(response["status"])
                current["mime"] = str(response.get("mimeType", ""))
                current["type"] = str(params.get("type", ""))
        elif method == "Network.loadingFinished":
            current = self._requests.pop(key, None)
            if current is not None and "status" in current:
                self._finish(
                    current,
                    current["status"],
                    current.get("mime", ""),
                    (sid, request_id),
                    current.get("type", ""),
                )
        elif method == "Network.loadingFailed":
            if self._requests.pop(key, None) is not None:
                self._recorder.aborted += 1

    def _finish(
        self,
        current: dict[str, Any],
        status: int,
        mime: str,
        body_key: tuple[str, str] | None,
        resource_type: str = "",
    ) -> None:
        def read_body() -> bytes:
            if body_key is None:
                raise RecorderError("NO_BODY")
            result = self._call(
                "Network.getResponseBody", {"requestId": body_key[1]}, body_key[0]
            )
            body = str(result.get("body", ""))
            if result.get("base64Encoded"):
                return base64.b64decode(body)
            return body.encode("utf-8")

        self._recorder.handle(
            method=current["method"],
            url=current["url"],
            status=status,
            content_type=mime,
            read_body=read_body,
            resource_type=resource_type,
        )


# ---------------------------------------------------------------------------
# propose-catalog (offline; reads runs, writes a DRAFT, applies nothing)

#: How observed GET routes are mapped to capabilities. Heuristic and reviewed by a
#: human; ``--map CAPABILITY=TEMPLATE`` pins a route explicitly.
CAPABILITY_HINTS: Mapping[str, tuple[str, ...]] = {
    "aifactor.predictor": (r"ai[-_ ]?factor", r"predict|prediction|forecast"),
    "aifactor.model": (r"ai[-_ ]?factor", r"model|validation|feature|dataset"),
    "screen.definition": (r"screen",),
    "simulation.result": (r"simul|/sim(/|$)",),
    "transactions.account": (r"^/transact_list\.jsp$",),
}
CAPABILITY_NOUN = {
    "aifactor.predictor": "predictor",
    "aifactor.model": "model",
    "screen.definition": "screen",
    "simulation.result": "simulation",
    "transactions.account": "account",
    "designer-model.json": "model",
}
FAMILY_ONLY = {
    "aifactor.predictor": r"ai[-_ ]?factor",
    "aifactor.model": r"ai[-_ ]?factor",
}
REVIEW_BANNER = (
    "DRAFT catalog diff. Generated from value-free recorder output; never applied "
    "automatically. A human reviews every row against the raw capture before any "
    "row becomes CONFIRMED."
)


def _spec_source(shape: Any, indent: int = 0) -> str:
    pad = "    " * indent
    if isinstance(shape, str):
        parts = shape.split("|")
        if shape in {"int", "num", "str", "date", "us_date", "bool", "null"}:
            return f'"{shape}"'
        if (
            len(parts) == 2
            and "null" in parts
            and (base := next(p for p in parts if p != "null"))
            in {"int", "num", "str", "date", "us_date", "bool"}
        ):
            return f'"{base}?"'
        return f'"<MIXED:{shape}>"'
    if "$keyed" in shape:
        return '"<KEYED:field names are data; review by hand>"'
    if "$array" in shape:
        item = shape["$array"]
        minimum = 1 if int(shape["$len_min"]) >= 1 else 0
        inner = '"<MIXED:empty-array>"' if item is None else _spec_source(item, indent)
        return f"Seq({inner}, min_items={minimum})"
    optional = set(shape.get("$optional", ()))
    required = {
        k: v for k, v in shape.items() if k != "$optional" and k not in optional
    }
    opt = {k: v for k, v in shape.items() if k in optional}

    def block(items: Mapping[str, Any]) -> str:
        lines = [
            f'{pad}        "{k}": {_spec_source(items[k], indent + 2)},' for k in items
        ]
        return "{\n" + "\n".join(lines) + f"\n{pad}    }}"

    text = f"Obj(\n{pad}    {block(required)}"
    if opt:
        text += f",\n{pad}    optional={block(opt)}"
    return text + f"\n{pad})"


def _field_names(shape: Any) -> Any:
    if isinstance(shape, str):
        return shape
    if "$array" in shape:
        return [] if shape["$array"] is None else [_field_names(shape["$array"])]
    if "$keyed" in shape:
        return {"<keyed>": _field_names(shape["$keyed"])}
    return {k: _field_names(v) for k, v in shape.items() if k != "$optional"}


def _template_pattern(template: str, noun: str) -> tuple[str, str]:
    path = template.replace("{id}", "{" + noun + "_id}")
    pattern = "/".join(
        "[1-9][0-9]*" if part == "{id}" else re.escape(part)
        for part in template.split("/")
    )
    return path, pattern


def _current_row(capability: str) -> dict[str, Any] | None:
    for row in catalog_rows():
        if row["capability"] == capability:
            return dict(row)
    return None


def _shape_top_keys(described: Any) -> set[str]:
    if not isinstance(described, dict) or "required" not in described:
        return set()
    return set(described["required"]) | set(described["optional"])


def _collect(
    entries: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, tuple[str, ...]], dict[str, Any]]:
    routes: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for entry in entries:
        if entry.get("method") != "GET":
            continue
        key = (str(entry["path_template"]), tuple(entry["query_keys"]))
        slot = routes.setdefault(
            key,
            {
                "status": Counter(),
                "media": Counter(),
                "shape": None,
                "html": [],
                "csv": [],
                "bodies": 0,
                "flags": set(),
                "pagination": [],
            },
        )
        slot["status"][int(entry["status"])] += 1
        slot["media"][str(entry["media_type"])] += 1
        slot["flags"].update(tuple(item) for item in entry.get("query_flags", ()))
        slot["pagination"].append(entry)
        if not 200 <= int(entry["status"]) < 300 or "body_sha256" not in entry:
            continue
        slot["bodies"] += 1
        shape = entry.get("body_shape")
        if shape is not None and shape != "not-json":
            slot["shape"] = merge_shapes(slot["shape"], shape)
        if "html" in entry:
            slot["html"].append(entry["html"])
        if "csv" in entry:
            slot["csv"].append(entry["csv"])
    return routes


def _html_summary(profiles: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    rows: Counter[int] = Counter()
    headers: list[str] = []
    for profile in profiles:
        for table in profile["table_profiles"]:
            if table["data_rows"] > 0:
                rows[int(table["data_rows"])] += 1
                if not headers or len(table["header_cells"]) > len(headers):
                    headers = list(table["header_cells"])
    if not rows:
        return None
    return {
        "header_cells": headers,
        "max_data_rows_per_page": max(rows),
        "most_common_data_rows_per_page": rows.most_common(1)[0][0],
        "pages_with_data_rows": sum(rows.values()),
    }


def _candidate(
    capability: str,
    key: tuple[str, tuple[str, ...]],
    slot: Mapping[str, Any],
    drift: bool,
    pagination: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    template, query_keys = key
    noun = CAPABILITY_NOUN[capability]
    path, pattern = _template_pattern(template, noun)
    media = "json" if slot["shape"] is not None else None
    html_info = _html_summary(slot["html"]) if slot["html"] else None
    if media is None and slot["html"]:
        media = "html"
    if media is None and slot["csv"]:
        media = "csv"
    page_keys = [p["key"] for p in pagination if p["path_template"] == template]
    row: dict[str, Any] = {
        "capability": capability,
        "route_template": template,
        "method": "GET",
        "path": path,
        "path_pattern": pattern,
        "query_keys": list(query_keys),
        "query_value_flags": sorted([list(f) for f in slot["flags"]]),
        "response_media_type": media,
        "status_counts": {str(k): v for k, v in sorted(slot["status"].items())},
        "media_types_seen": dict(sorted(slot["media"].items())),
        "bodies_observed": slot["bodies"],
        "paginated": bool(page_keys),
        "page_param": page_keys[0] if page_keys else None,
        "top_level_shape": _field_names(slot["shape"]) if slot["shape"] else None,
        "shape_spec_source": _spec_source(slot["shape"]) if slot["shape"] else None,
        "html_profile": html_info,
        "csv_profile": slot["csv"][0] if slot["csv"] else None,
        "shape_differs_between_runs": drift,
        "proposed_verdict": "CONFIRMED_AFTER_REVIEW",
        "requires_code_change": [],
        "evidence": (
            "Observed in an attended passive recording: "
            f"{sum(slot['status'].values())} responses, statuses "
            f"{dict(sorted(slot['status'].items()))}; field names and types only, "
            "no values. Reviewer to confirm against the raw capture."
        ),
    }
    if media == "html":
        row["requires_code_change"].append(
            "HTML response admission: the capture extractor and CaptureRoute admit "
            "JSON shapes and CSV columns only"
        )
    if media == "json" and not slot["status"].get(200):
        row["requires_code_change"].append("no 200 observed")
    return row


def _compare_existing(
    row: Mapping[str, Any], current: dict[str, Any] | None
) -> dict[str, Any]:
    if current is None:
        return {"change": "NEW_ROW"}
    same_route = current.get("path_pattern") == row["path_pattern"] and current.get(
        "query_keys"
    ) == sorted(row["query_keys"])
    info: dict[str, Any] = {
        "current_verdict": current["verdict"],
        "current_path_pattern": current.get("path_pattern"),
        "current_query_keys": current.get("query_keys"),
        "same_route": same_route,
    }
    new_keys = (
        set(row["top_level_shape"] or ())
        if isinstance(row["top_level_shape"], dict)
        else set()
    )
    old_keys = _shape_top_keys(current.get("shape"))
    if old_keys and new_keys:
        info["fields_added"] = sorted(new_keys - old_keys)
        info["fields_removed"] = sorted(old_keys - new_keys)
    if same_route and current["verdict"] == "CONFIRMED":
        info["change"] = (
            "CONFIRMS_EXISTING"
            if not info.get("fields_added") and not info.get("fields_removed")
            else "DIFFERS_FROM_EXISTING"
        )
    else:
        info["change"] = (
            "REPLACE_PLACEHOLDER"
            if current["verdict"] != "CONFIRMED"
            else "CHANGED_ROW"
        )
    return info


def _matches(capability: str, template: str, query_keys: Sequence[str]) -> int:
    lowered = template.lower()
    hints = CAPABILITY_HINTS.get(capability, ())
    if hints and all(re.search(h, lowered) for h in hints):
        return 2
    family = FAMILY_ONLY.get(capability)
    if family and re.search(family, lowered):
        return 1
    return 0


def build_proposal(
    discover_entries: Sequence[Mapping[str, Any]],
    raw_entries: Sequence[Mapping[str, Any]] = (),
    pins: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Pure: build the draft diff from recorder entries."""

    pins = dict(pins or {})
    combined = [*discover_entries, *raw_entries]
    first = _collect(discover_entries)
    allr = _collect(combined)
    pagination = pagination_summary(combined)
    capabilities = [
        *CAPABILITY_HINTS,
        "designer-model.json",
    ]
    rows = []
    for capability in capabilities:
        candidates = []
        for key, slot in sorted(allr.items()):
            template = key[0]
            if pins.get(capability) not in (None, template):
                continue
            if capability == "designer-model.json":
                score = (
                    2
                    if (
                        re.fullmatch(r"/spr/strategy/model/\{id\}", template)
                        and "dm" in key[1]
                    )
                    else 0
                )
            elif pins.get(capability) == template:
                score = 3
            else:
                score = _matches(capability, template, key[1])
            if score == 0 or not slot["status"].get(200):
                continue
            drift = (
                bool(raw_entries) and first.get(key, {}).get("shape") != slot["shape"]
            )
            candidate = _candidate(capability, key, slot, drift, pagination)
            candidate["match"] = {3: "PINNED", 2: "KEYWORD", 1: "FAMILY_ONLY"}[score]
            candidates.append((score, candidate))
        candidates.sort(key=lambda item: (-item[0], item[1]["route_template"]))
        picked = [c for _, c in candidates]
        current = _current_row(capability)
        entry: dict[str, Any] = {
            "capability": capability,
            "candidates": picked,
            "needs_owner_pick": len(picked) > 1,
            "no_candidate": not picked,
        }
        if picked:
            entry["change"] = _compare_existing(picked[0], current)
        else:
            entry["change"] = {"change": "NO_CANDIDATE"}
        rows.append(entry)
    return {
        "banner": REVIEW_BANNER,
        "applied": False,
        "capabilities": rows,
        "unmatched_routes": sorted(
            {
                key[0]
                for key, slot in allr.items()
                if slot["status"].get(200)
                and all(_matches(c, key[0], key[1]) == 0 for c in CAPABILITY_HINTS)
                and not re.fullmatch(r"/spr/strategy/model/\{id\}", key[0])
            }
        ),
    }


def render_markdown(proposal: Mapping[str, Any]) -> str:
    lines = [
        "# P123 catalog diff (DRAFT, not applied)",
        "",
        str(proposal["banner"]),
        "",
    ]
    for row in proposal["capabilities"]:
        lines.append(f"## {row['capability']}: {row['change']['change']}")
        if row["no_candidate"]:
            lines += ["", "No 2xx GET route matched. Pin one with `--map`.", ""]
            continue
        if row["needs_owner_pick"]:
            lines += ["", "Several candidates; the owner picks one.", ""]
        for cand in row["candidates"]:
            lines += [
                "",
                f"- route `{cand['route_template']}` ({cand['match']}), "
                f"query keys {cand['query_keys']}, media {cand['response_media_type']}, "
                f"statuses {cand['status_counts']}, bodies {cand['bodies_observed']}",
            ]
            if cand["paginated"]:
                lines.append(f"  - paginated by `{cand['page_param']}`")
            if cand["html_profile"]:
                lines.append(
                    f"  - HTML header cells {cand['html_profile']['header_cells']}, "
                    f"max data rows per page {cand['html_profile']['max_data_rows_per_page']}"
                )
            for need in cand["requires_code_change"]:
                lines.append(f"  - REQUIRES CODE CHANGE: {need}")
        lines.append("")
    lines += ["## Routes seen but not mapped", ""]
    lines += [f"- `{t}`" for t in proposal["unmatched_routes"]] or ["- none"]
    return "\n".join(lines) + "\n"


def render_test_skeleton(proposal: Mapping[str, Any]) -> str:
    """A pytest skeleton: confirmed field names and types, never values."""

    fields: dict[str, Any] = {}
    html_cols: dict[str, Any] = {}
    for row in proposal["capabilities"]:
        if row["no_candidate"] or row["capability"] == "designer-model.json":
            continue
        cand = row["candidates"][0]
        if cand["top_level_shape"] is not None:
            fields[row["capability"]] = cand["top_level_shape"]
        if cand["html_profile"]:
            html_cols[row["capability"]] = {
                "header_cells": cand["html_profile"]["header_cells"],
                "rows_per_page": cand["html_profile"]["most_common_data_rows_per_page"],
                "page_param": cand["page_param"],
            }
    return (
        '"""DRAFT skeleton: finish after the reviewed rows are applied.\n\n'
        "Field names and types come from a value-free recording; no value appears\n"
        'here. Remove the skip markers once the catalog rows exist."""\n\n'
        "from __future__ import annotations\n\n"
        "import pytest\n\n"
        "from quant_engine.portfolio123.capture_catalog import (\n"
        "    route_for,\n"
        "    verify_confirmed_shape,\n"
        ")\n\n"
        f"FIELDS = {json.dumps(fields, indent=4, sort_keys=True)}\n"
        f"HTML = {json.dumps(html_cols, indent=4, sort_keys=True)}\n\n\n"
        "def _synthetic(expected: object) -> object:\n"
        "    if isinstance(expected, dict):\n"
        "        return {k: _synthetic(v) for k, v in expected.items()}\n"
        "    if isinstance(expected, list):\n"
        "        return [_synthetic(expected[0])] if expected else []\n"
        "    base = str(expected).split('|')[0]\n"
        "    return {\n"
        "        'int': 1, 'num': 1.5, 'bool': True, 'null': None,\n"
        "        'date': '2000-01-31', 'us_date': '01/31/2000',\n"
        "    }.get(base, 'x')\n\n\n"
        "@pytest.mark.skip(reason='DRAFT skeleton')\n"
        "@pytest.mark.parametrize('capability', sorted(FIELDS))\n"
        "def test_confirmed_row_accepts_synthetic_payload(capability: str) -> None:\n"
        "    assert route_for(capability).verdict == 'CONFIRMED'\n"
        "    verify_confirmed_shape(capability, _synthetic(FIELDS[capability]))\n\n\n"
        "@pytest.mark.skip(reason='DRAFT skeleton')\n"
        "@pytest.mark.parametrize('capability', sorted(HTML))\n"
        "def test_html_row_pins_columns_and_paging(capability: str) -> None:\n"
        "    assert route_for(capability).verdict == 'CONFIRMED'\n"
        "    assert HTML[capability]['header_cells']\n"
    )


def propose_catalog(
    discover_dir: Path,
    out: Path,
    *,
    raw_run: Path | None = None,
    pins: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Verify the input runs, then write the DRAFT files into a new directory."""

    verify_run(discover_dir)
    _, discover_entries = _load_run(discover_dir)
    raw_entries: list[dict[str, Any]] = []
    sources = [discover_dir.name]
    if raw_run is not None:
        verify_run(raw_run)
        raw_entries = _load_run(raw_run)[1]
        sources.append(raw_run.name)
    assert_output_root(out)
    proposal = build_proposal(discover_entries, raw_entries, pins)
    out.mkdir(parents=True)
    artifacts = []
    for name, payload in (
        ("catalog-diff.draft.json", _canonical(proposal)),
        ("catalog-diff.draft.md", render_markdown(proposal).encode("utf-8")),
        (
            "test_capture_new_rows.py.draft",
            render_test_skeleton(proposal).encode("utf-8"),
        ),
    ):
        artifacts.append({"path": name, **_write_new(out / name, payload)})
    manifest = {
        "schema": PROPOSAL_SCHEMA,
        "applied": False,
        "banner": REVIEW_BANNER,
        "inputs": sources,
        "artifacts": artifacts,
    }
    _write_new(out / "proposal-manifest.json", _canonical(manifest))
    return manifest


# ---------------------------------------------------------------------------
# CLI


def _live(args: argparse.Namespace) -> dict[str, Any]:  # pragma: no cover
    recorder = Recorder(
        args.output_root, mode=args.command, include_identity=args.include_identity
    )
    transport = WebSocketTransport(args.cdp_url)
    try:
        Listener(transport, recorder, max_seconds=args.max_seconds).run()
    except KeyboardInterrupt:
        pass
    finally:
        transport.close()
    return recorder.finish()


def _summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": manifest["status"],
        "mode": manifest["mode"],
        "counts": manifest["counts"],
        "routes": [
            {k: r[k] for k in ("path_template", "status", "media_type", "count")}
            for r in manifest["routes"][:80]
        ],
        "pagination": manifest["pagination"],
        "mutation_like_not_recorded": [
            {k: m[k] for k in ("method", "path_template", "count")}
            for m in manifest["MUTATION_LIKE_NOT_RECORDED"]
        ],
        "identity_billing_not_recorded": [
            m["path_template"] for m in manifest["IDENTITY_BILLING_NOT_RECORDED"]
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("discover", "capture"):
        item = sub.add_parser(name)
        item.add_argument("--cdp-url", required=True)
        item.add_argument("--output-root", required=True, type=Path)
        item.add_argument("--max-seconds", type=float, default=0.0)
        item.add_argument(
            "--include-identity",
            action="append",
            default=[],
            choices=IDENTITY_WORDS,
            help="record a billing/identity path family (default: never)",
        )
    check = sub.add_parser("verify")
    check.add_argument("--run-root", required=True, type=Path)
    propose = sub.add_parser("propose-catalog")
    propose.add_argument("--discover", required=True, type=Path)
    propose.add_argument("--raw-run", type=Path)
    propose.add_argument("--out", required=True, type=Path)
    propose.add_argument("--map", action="append", default=[], metavar="CAP=TEMPLATE")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            result: dict[str, Any] = verify_run(args.run_root)
        elif args.command == "propose-catalog":
            pins = dict(item.split("=", 1) for item in args.map)
            written = propose_catalog(
                args.discover, args.out, raw_run=args.raw_run, pins=pins
            )
            result = {
                "status": "DRAFT_WRITTEN",
                "applied": False,
                "artifacts": [a["path"] for a in written["artifacts"]],
            }
        else:
            result = _summary(_live(args))
    except RecorderError as exc:
        print(f"REFUSED {exc.code}")
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
