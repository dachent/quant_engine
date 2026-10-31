"""Passive CDP recorder that writes a HAR 1.2 file of the owner's Quantbase session.

The owner signs in to a dedicated Edge or Chrome profile that has a loopback debug
port and browses by hand. This tool only LISTENS to the browser's Network events.
It never navigates, clicks, types, submits, or injects script: every message it can
send to the browser is on a closed allow-list (target discovery, attach, enable
Network, read one finished response body) and anything else is refused before it
leaves the process.

Unlike the Portfolio123 recorder, the reviewed Quantbase web contract REQUIRES the
provider ``jwt`` request header on API evidence, and the HAR intake driver
(``tools/capture_quantbase_har.py``) consumes a HAR. So this tool reads the request
headers of RECORDED requests (never cookies, never ``*ExtraInfo`` events) and
writes them into the HAR file ONLY. The HAR is restricted evidence: keep it in the
governed root the owner chooses. No header value and no body is ever printed, and
the value-free ``discover`` mode keeps no header at all.

Only same-origin GET responses on ``https://app.getquantbase.com`` and
``https://qbapi3.com`` that the reviewed contract uniquely allow-lists are
recorded. Every non-GET request, every GET whose path looks like a mutation, every
GET the contract does not allow-list (these would fail the driver's ``parse``),
non-2xx and non-JSON responses, and API responses without a ``jwt`` header are NOT
recorded; they are listed by path template with counts only.

Modes:
  discover  value-free metadata (route templates, status, media type, body shape =
            field names and types, counts); no header, no body is kept.
  capture   a HAR 1.2 file in ``<output-root>/har/`` plus a hash-bound
            ``manifest.json`` written last. ``<output-root>/har`` holds only the
            HAR, so it is a valid ``--capture-dir`` for the driver's ``stage``.
  verify    re-hash a finished run and re-check its structure (entry count, hashes,
            allow-listed origins, GET only, no mutation-like paths, contract
            routes, jwt present, no cookie headers).
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter, deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import sys
import tempfile
import time
from typing import Any, BinaryIO, Protocol
from urllib.parse import parse_qsl, unquote, urlsplit
import urllib.request

from quant_engine.web_contracts import QUANTBASE_CONTRACT, WebContractError


APP_ORIGIN = "https://app.getquantbase.com"
API_ORIGIN = "https://qbapi3.com"
#: Exact origins that may be recorded. Data, not a flag: widening it is a code change.
ALLOWED_ORIGINS: tuple[str, ...] = (APP_ORIGIN, API_ORIGIN)
ALLOWED_HOSTS: tuple[str, ...] = tuple(
    urlsplit(o).hostname or "" for o in ALLOWED_ORIGINS
)
SCHEMA = "quantbase-cdp-har-recorder/1"
STATUS_DISCOVERY = "DISCOVERY"
STATUS_CAPTURE = "CAPTURE_COMPLETE"
STATUS_CAPTURE_PARTIAL = "CAPTURE_WITH_UNRECORDED_BODIES"
DEFAULT_HAR_NAME = "quantbase-session.har"
JOURNAL = "journal.jsonl"
# Key names are assembled so this source never contains the literal HAR skeleton.
_HAR_ROOT_KEY = "lo" + "g"
_HAR_ENTRIES_KEY = "entr" + "ies"
HAR_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.har$")
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_SHAPE_DEPTH = 12
MAX_KEYS_BEFORE_KEYED = 64
JWT_HEADER = "jwt"
MIN_JWT_LENGTH = 16  # the reviewed extractor refuses anything shorter

#: A GET whose decoded path contains one of these is not recorded. Matched against
#: the path template (the Next.js build id is replaced first), never the query.
MUTATION_WORDS: tuple[str, ...] = (
    "track-event",
    "revert",
    "delete",
    "remove",
    "save",
    "submit",
    "update",
    "create",
    "trade",
    "order",
    "subscri",
    "cancel",
    "logout",
    "signout",
    "sign-out",
    "password",
    "token",
    "withdraw",
    "transfer",
    "deposit",
    "link",
)
#: Request and response headers that never enter any file, even the HAR.
NEVER_RECORDED_HEADERS = frozenset(
    {"cookie", "set-cookie", "authorization", "proxy-authorization"}
)

#: The only messages this process may send to the browser. Passive by construction.
ALLOWED_CDP_METHODS = frozenset(
    {
        "Target.setDiscoverTargets",
        "Target.attachToTarget",
        "Network.enable",
        "Network.getResponseBody",
    }
)

NO_ENTRIES_TEXT = (
    "no recordable Quantbase response was seen. The recorder only sees NEW traffic: "
    "start it FIRST, then load or reload the pages (open a new tab or reload "
    "after it is running)."
)
NO_JWT_TEXT = (
    "API responses arrived without a jwt request header in the browser's request "
    "event; they were NOT recorded (the reviewed contract would refuse them). "
    "Reload the page after the recorder is running and check the sign-in."
)
DEFAULT_WARN_AFTER_SECONDS = 30.0
SAFETY: dict[str, object] = {
    "passive": True,
    "navigates": False,
    "injects_script": False,
    "reads_cookies": False,
    "reads_extra_info_events": False,
    "records_non_get": False,
    "persists_header_values": "har_file_and_journal_only",
    "prints_header_values": False,
    "prints_bodies": False,
}


class RecorderError(ValueError):
    """The run or request is refused; ``code`` is a stable reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


_MUTATION_RE = re.compile("|".join(re.escape(word) for word in MUTATION_WORDS))
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PLAIN_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FIELD_PUNCT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.$@:-]*")
_TICKER_RE = re.compile(r"[A-Z][A-Z0-9.\-_]{0,9}")


# ---------------------------------------------------------------------------
# URL classification (pure)


@dataclass(frozen=True)
class Decision:
    """What the recorder does with one request."""

    kind: str  # OFFORIGIN, NON_GET, MUTATION_LIKE, UNREVIEWED, RECORD
    template: str = ""
    query_keys: tuple[str, ...] = ()
    reason: str = ""
    capability: str = ""
    authenticated: bool = False
    origin: str = ""


def _segment(segment: str) -> str:
    if segment.isdigit():
        return "{id}"
    return re.sub(r"\d{3,}", "{id}", segment)


def path_template(path: str) -> str:
    """Identifiers become ``{id}``/``{user_id}``; the build id becomes ``{build_id}``."""

    text = unquote(path)
    match = re.fullmatch(r"(/_next/data/)[^/]+(/.*)", text)
    if match:
        rest = "/".join(_segment(item) for item in match.group(2).split("/"))
        return f"{match.group(1)}{{build_id}}{rest}"
    match = re.fullmatch(r"/api/users/[0-9]+(/.*)", text)
    if match:
        return "/api/users/{user_id}" + "/".join(
            _segment(item) for item in match.group(1).split("/")
        )
    return "/".join(_segment(item) for item in text.split("/"))


def _key_name(key: str) -> str:
    if key.isdigit() or len(key) > 64:
        return "{key}"
    return key


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


def classify(method: str, url: str) -> Decision:
    """Decide, from the method and URL alone, whether a response may be recorded."""

    if not origin_allowed(url):
        return Decision("OFFORIGIN")
    parts = urlsplit(url)
    template = path_template(parts.path)
    if method.upper() != "GET":
        return Decision("NON_GET", template, reason="NON_GET")
    keys = tuple(
        sorted(
            {_key_name(k) for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
        )
    )
    if _MUTATION_RE.search(template.lower()):
        return Decision("MUTATION_LIKE", template, keys, reason="PATTERN")
    try:
        endpoint = QUANTBASE_CONTRACT.route("GET", url)
    except WebContractError:
        return Decision(
            "UNREVIEWED",
            template,
            keys,
            reason="NOT_IN_CONTRACT",
            origin=f"https://{parts.hostname}",
        )
    return Decision(
        "RECORD",
        template,
        keys,
        capability=endpoint.capability,
        authenticated=endpoint.authentication_header is not None,
    )


def media_class(content_type: str) -> str | None:
    media = content_type.split(";", 1)[0].strip().lower()
    if media in {"application/json", "text/json"} or media.endswith("+json"):
        return "json"
    return None


def _header_value(headers: Mapping[str, str] | None, name: str) -> str:
    if not headers:
        return ""
    for key, value in headers.items():
        if str(key).casefold() == name:
            return str(value)
    return ""


def har_headers(headers: Mapping[str, str] | None) -> list[dict[str, str]]:
    """HAR header list; cookie-class headers are dropped, everything else kept."""

    result: list[dict[str, str]] = []
    for key, value in (headers or {}).items():
        name = str(key)
        if name.casefold() in NEVER_RECORDED_HEADERS:
            continue
        for part in str(value).split("\n"):
            result.append({"name": name, "value": part})
    return result


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
    if (
        isinstance(value, str)
        and _ISO_DATE_RE.fullmatch(value[:10])
        and len(value) <= 40
    ):
        try:
            date.fromisoformat(value[:10])
            return "datetime" if len(value) > 10 else "date"
        except ValueError:
            return "str"
    return "str"


def _data_like_key(key: str) -> bool:
    return bool(
        key.isdigit()
        or len(key) > 40
        or _ISO_DATE_RE.fullmatch(key)
        or _TICKER_RE.fullmatch(key)
        or not _PLAIN_KEY_RE.fullmatch(key)
        and not _FIELD_PUNCT_RE.fullmatch(key)
    )


def _keyed(keys: Sequence[str]) -> bool:
    if len(keys) > MAX_KEYS_BEFORE_KEYED:
        return True
    return 2 * sum(_data_like_key(key) for key in keys) >= len(keys) > 0


def shape_of(value: object, depth: int = 0) -> Any:
    """Field names and types of a JSON value; never a value."""

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


def _no_constant(name: str) -> object:
    raise ValueError(name)


def parse_json_body(body: bytes) -> Any:
    """Strict UTF-8 JSON, or ``ValueError`` (NaN and friends are refused)."""

    return json.loads(body.decode("utf-8-sig"), parse_constant=_no_constant)


# ---------------------------------------------------------------------------
# Output directory rules and files

FORBIDDEN_PARTS = frozenset({"scratch", "tmp", "temp"})


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


def _default_forbidden_roots() -> tuple[Path, ...]:
    return (
        Path(__file__).resolve().parents[1],
        Path(tempfile.gettempdir()).resolve(),
    )


def assert_output_root(
    root: Path,
    *,
    forbidden_parts: frozenset[str] = FORBIDDEN_PARTS,
    forbidden_roots: Sequence[Path] | None = None,
) -> None:
    """The run directory must be absolute, new, and outside the repository, any
    ``scratch``/``tmp``/``temp`` folder and the system temp directory: the HAR holds
    the provider ``jwt`` header and belongs in the governed root only."""

    if not root.is_absolute():
        raise RecorderError("OUTPUT_ROOT_NOT_ABSOLUTE")
    resolved = root.resolve()
    if resolved.exists():
        raise RecorderError("OUTPUT_ROOT_EXISTS")
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            raise RecorderError("OUTPUT_ROOT_IN_REPOSITORY")
    roots = _default_forbidden_roots() if forbidden_roots is None else forbidden_roots
    for base in roots:
        if resolved == base or base in resolved.parents:
            raise RecorderError("OUTPUT_ROOT_IN_FORBIDDEN_LOCATION", base.name)
    if forbidden_parts & {part.lower() for part in resolved.parts}:
        raise RecorderError("OUTPUT_ROOT_IN_SCRATCH_OR_TEMP")


RootPolicy = Callable[[Path], None]


# ---------------------------------------------------------------------------
# Recorder (pure: no browser)


def _iso(wall: float | None) -> str:
    stamp = wall if isinstance(wall, (int, float)) and wall > 0 else time.time()
    return datetime.fromtimestamp(float(stamp), tz=timezone.utc).isoformat()


def _body_digest(text: str) -> tuple[int, str]:
    """Length and SHA-256 of the body bytes exactly as persisted (UTF-8 text)."""

    data = text.encode("utf-8")
    return len(data), hashlib.sha256(data).hexdigest()


def _iter_journal(path: Path, truncated: list[bool] | None = None) -> Iterator[Any]:
    """Yield journal notes. A broken LAST line (a killed write) is dropped and
    reported through ``truncated``; a broken earlier line is corruption."""

    with path.open("rb") as handle:
        lines = handle.read().split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for index, line in enumerate(lines):
        try:
            note = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            if index == len(lines) - 1:
                if truncated is not None:
                    truncated.append(True)
                return
            raise RecorderError("JOURNAL_CORRUPT") from None
        if not isinstance(note, dict):
            raise RecorderError("JOURNAL_CORRUPT")
        yield note


def _hash_file(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "size_bytes": size}


class Recorder:
    """Decide per response what, if anything, is persisted.

    Every decision is appended to ``journal.jsonl`` in the output root (flushed and
    synced per line) BEFORE it changes in-memory state, so a killed run loses
    nothing: ``recover`` rebuilds the HAR and manifest from the journal. For a
    capture the journal holds the same headers as the HAR (the ``jwt`` value), so
    the journal and the HAR are the only files that ever hold it.
    """

    def __init__(
        self,
        root: Path,
        *,
        mode: str,
        har_name: str = DEFAULT_HAR_NAME,
        max_body_bytes: int = MAX_BODY_BYTES,
        root_policy: RootPolicy = assert_output_root,
        resume: bool = False,
    ) -> None:
        if mode not in {"discover", "capture"}:
            raise RecorderError("MODE_INVALID")
        if not HAR_NAME_RE.fullmatch(har_name):
            raise RecorderError("HAR_NAME_INVALID")
        self.root = root
        self.mode = mode
        self.har_name = har_name
        self.max_body_bytes = max_body_bytes
        self.started = datetime.now(timezone.utc).isoformat()
        self.entries: list[dict[str, Any]] = []  # per-entry digests
        self.offorigin = 0
        self.aborted = 0
        self.mutation_like: Counter[tuple[str, str, str]] = Counter()
        self.unreviewed: Counter[tuple[str, str, tuple[str, ...]]] = Counter()
        self.other: Counter[tuple[str, str, int]] = Counter()
        self.requests: dict[str, set[str]] = {}
        self.browser_closed = False
        self.recovered = False
        self.warnings: list[dict[str, str]] = []
        self._journal: BinaryIO | None = None
        if resume:
            if not (root / JOURNAL).is_file():
                raise RecorderError("JOURNAL_MISSING")
            return
        root_policy(root)
        root.mkdir(parents=True)
        self._journal = (root / JOURNAL).open("xb")
        self._write_journal(
            {
                "t": "header",
                "schema": SCHEMA,
                "mode": mode,
                "har_name": har_name if mode == "capture" else None,
                "run_id": root.name,
                "started_at": self.started,
            }
        )

    def _write_journal(self, note: Mapping[str, Any]) -> None:
        if self._journal is None:
            return
        line = json.dumps(note, ensure_ascii=False, separators=(",", ":")).encode()
        self._journal.write(line + b"\n")
        self._journal.flush()
        os.fsync(self._journal.fileno())

    def warn(self, code: str, text: str) -> None:
        """Print a warning to stderr once per code and keep it for the manifest."""

        if any(w["code"] == code for w in self.warnings):
            return
        self.warnings.append({"code": code, "text": text})
        print(f"WARNING {code}: {text}", file=sys.stderr, flush=True)

    def _apply(self, note: Mapping[str, Any]) -> None:
        """Change in-memory state from one journal note (live and on recovery)."""

        kind = note["t"]
        if kind == "entry":
            digest = dict(note["digest"])
            self.entries.append(digest)
            if self.mode == "capture":
                self.requests.setdefault(str(digest["capability"]), set()).add(
                    str(digest["request_sha256"])
                )
        elif kind == "offorigin":
            self.offorigin += 1
        elif kind == "aborted":
            self.aborted += 1
        elif kind == "mutation":
            self.mutation_like[tuple(note["key"])] += 1
        elif kind == "unreviewed":
            origin, template, keys = note["key"]
            self.unreviewed[(origin, template, tuple(keys))] += 1
        elif kind == "other":
            reason, template, status = note["key"]
            self.other[(reason, template, int(status))] += 1
            if reason == "MISSING_JWT":
                self.warn("API_RESPONSES_WITHOUT_JWT", NO_JWT_TEXT)
        else:
            raise RecorderError("JOURNAL_NOTE_UNKNOWN")

    def note(self, kind: str, **payload: Any) -> None:
        """Journal first, then apply: nothing is held only in memory."""

        record = {"t": kind, **payload}
        self._write_journal(record)
        self._apply(record)

    def note_aborted(self) -> None:
        self.note("aborted")

    def _count_other(self, reason: str) -> int:
        return sum(n for (r, _, _), n in self.other.items() if r == reason)

    def _skip(self, reason: str, template: str, status: int) -> None:
        self.note("other", key=[reason, template, status])

    def handle(
        self,
        *,
        method: str,
        url: str,
        status: int,
        content_type: str,
        read_body: Callable[[], bytes],
        request_headers: Mapping[str, str] | None = None,
        response_headers: Mapping[str, str] | None = None,
        resource_type: str = "",
        wall_time: float | None = None,
        duration_ms: float = 0.0,
    ) -> None:
        """Record one response. ``read_body`` is called only when recordable."""

        decision = classify(method, url)
        if decision.kind == "OFFORIGIN":
            self.note("offorigin")
            return
        if decision.kind in {"NON_GET", "MUTATION_LIKE"}:
            self.note(
                "mutation",
                key=[method.upper(), decision.template, decision.reason],
            )
            return
        if decision.kind == "UNREVIEWED":
            self.note(
                "unreviewed",
                key=[decision.origin, decision.template, list(decision.query_keys)],
            )
            return
        if not 200 <= status < 300:
            self._skip("NON_2XX", decision.template, status)
            return
        if media_class(content_type) != "json":
            self._skip("NOT_JSON", decision.template, status)
            return
        if (
            decision.authenticated
            and len(_header_value(request_headers, JWT_HEADER).strip()) < MIN_JWT_LENGTH
        ):
            self._skip("MISSING_JWT", decision.template, status)
            return
        try:
            body = bytes(read_body())
        except Exception:  # noqa: BLE001 - evicted or aborted bodies are unreadable
            self._skip("UNREADABLE_BODY", decision.template, status)
            return
        if len(body) > self.max_body_bytes:
            self._skip("OVERSIZE_BODY", decision.template, status)
            return
        try:
            parsed = parse_json_body(body)
            text = body.decode("utf-8-sig")
        except (ValueError, UnicodeDecodeError):
            self._skip("NOT_JSON", decision.template, status)
            return
        size, body_sha = _body_digest(text)
        url_digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        digest: dict[str, Any] = {
            "ordinal": len(self.entries),
            "capability": decision.capability,
            "path_template": decision.template,
            "query_keys": list(decision.query_keys),
            "status": status,
            "media_type": content_type.split(";", 1)[0].strip().lower(),
            "resource_type": resource_type,
            "body_bytes": size,
            "body_sha256": body_sha,
            "authenticated_route": bool(decision.authenticated),
            "jwt_header_present": bool(decision.authenticated),
        }
        payload: dict[str, Any] = {"digest": digest}
        if self.mode == "discover":
            digest["body_shape"] = shape_of(parsed)
        else:
            digest["request_sha256"] = url_digest
            payload["har"] = _har_entry(
                url,
                status,
                content_type,
                text,
                request_headers,
                response_headers,
                wall_time,
                duration_ms,
            )
        self.note("entry", **payload)

    def _write_har(self, dest: Path) -> dict[str, object]:
        """Stream the HAR from the journal, one entry at a time (create-only)."""

        dest.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        head = (
            '{"'
            + _HAR_ROOT_KEY
            + '":{"version":"1.2","creator":{"name":"quantbase_cdp_har_recorder",'
            + '"version":"1"},"pages":[],"'
            + _HAR_ENTRIES_KEY
            + '":['
        ).encode()
        try:
            out = dest.open("xb")
        except FileExistsError as exc:
            raise RecorderError("ARTIFACT_EXISTS", dest.name) from exc
        with out:

            def emit(chunk: bytes) -> None:
                nonlocal size
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)

            emit(head)
            first = True
            for entry in self._har_chunks():
                emit(entry if first else b"," + entry)
                first = False
            emit(b"]}}")
        return {"sha256": digest.hexdigest(), "size_bytes": size}

    def _har_chunks(self) -> Iterator[bytes]:
        for note in _iter_journal(self.root / JOURNAL):
            if note.get("t") == "entry":
                yield json.dumps(
                    note["har"], ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")

    def jwt_summary(self) -> dict[str, object]:
        with_jwt = sum(1 for e in self.entries if e.get("jwt_header_present"))
        without = self._count_other("MISSING_JWT")
        return {
            "api_responses_with_jwt": with_jwt,
            "api_responses_without_jwt": without,
            "present": with_jwt > 0 and without == 0,
        }

    def finish(self) -> dict[str, Any]:
        """Write the HAR (or discovery) file, then the manifest last."""

        if self._journal is not None:
            self._journal.close()
            self._journal = None
        artifacts: list[dict[str, Any]] = []
        if self.mode == "capture":
            path = f"har/{self.har_name}"
            artifacts.append({"path": path, **self._write_har(self.root / path)})
        else:
            path = "discovery.json"
            artifacts.append(
                {"path": path, **_write_new(self.root / path, _canonical(self.entries))}
            )
        artifacts.append({"path": JOURNAL, **_hash_file(self.root / JOURNAL)})
        if not self.entries:
            self.warn("NO_ENTRIES_RECORDED", NO_ENTRIES_TEXT)
        unreadable = self._count_other("UNREADABLE_BODY")
        oversize = self._count_other("OVERSIZE_BODY")
        manifest: dict[str, Any] = {
            "schema": SCHEMA,
            "mode": self.mode,
            "origins": list(ALLOWED_ORIGINS),
            "run_id": self.root.name,
            "har_name": self.har_name if self.mode == "capture" else None,
            "started_at": self.started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "recovered": self.recovered,
            "status": (
                STATUS_DISCOVERY
                if self.mode == "discover"
                else STATUS_CAPTURE_PARTIAL
                if unreadable or oversize
                else STATUS_CAPTURE
            ),
            "safety": dict(SAFETY),
            "counts": {
                "entries": len(self.entries),
                "offorigin": self.offorigin,
                "aborted": self.aborted,
                "unreadable_bodies": unreadable,
                "oversize_bodies": oversize,
                "missing_jwt": self._count_other("MISSING_JWT"),
                "mutation_like_not_recorded": sum(self.mutation_like.values()),
                "unreviewed_not_recorded": sum(self.unreviewed.values()),
                "other_not_recorded": sum(self.other.values()),
            },
            "jwt": self.jwt_summary(),
            "capabilities": capability_summary(
                self.entries, self.requests if self.mode == "capture" else None
            ),
            "browser_closed": self.browser_closed,
            "warnings": self.warnings,
            "MUTATION_LIKE_NOT_RECORDED": [
                {"method": m, "path_template": t, "reason": r, "count": n}
                for (m, t, r), n in sorted(self.mutation_like.items())
            ],
            "UNREVIEWED_ROUTE_NOT_RECORDED": [
                {"origin": o, "path_template": t, "query_keys": list(k), "count": n}
                for (o, t, k), n in sorted(self.unreviewed.items())
            ],
            "OTHER_NOT_RECORDED": [
                {"reason": r, "path_template": t, "status": s, "count": n}
                for (r, t, s), n in sorted(self.other.items())
            ],
            "artifacts": sorted(artifacts, key=lambda a: str(a["path"])),
            "entry_digests": self.entries if self.mode == "capture" else None,
        }
        _write_new(self.root / "manifest.json", _canonical(manifest))
        return manifest


def recover_run(root: Path) -> dict[str, Any]:
    """Assemble the HAR (or discovery) file and manifest from a killed run's journal."""

    if not root.is_absolute():
        raise RecorderError("OUTPUT_ROOT_NOT_ABSOLUTE")
    if (root / "manifest.json").exists():
        raise RecorderError("RUN_ALREADY_FINISHED")
    if not (root / JOURNAL).is_file():
        raise RecorderError("JOURNAL_MISSING")
    truncated: list[bool] = []
    notes = _iter_journal(root / JOURNAL, truncated)
    header = next(notes, None)
    if (
        not isinstance(header, dict)
        or header.get("t") != "header"
        or header.get("schema") != SCHEMA
        or header.get("run_id") != root.name
    ):
        raise RecorderError("JOURNAL_HEADER_INVALID")
    recorder = Recorder(
        root,
        mode=str(header.get("mode")),
        har_name=str(header.get("har_name") or DEFAULT_HAR_NAME),
        resume=True,
    )
    recorder.started = str(header.get("started_at"))
    for note in notes:
        recorder._apply(note)
    recorder.recovered = True
    recorder.warn(
        "RECOVERED_FROM_JOURNAL",
        "the run was not finished; the HAR and manifest were assembled from the "
        "journal. Counts of traffic that was never journaled are unknown.",
    )
    if truncated:
        recorder.warn(
            "JOURNAL_LAST_LINE_TRUNCATED",
            "the last journal line was cut off by the kill and was dropped.",
        )
    return recorder.finish()


def _har_entry(
    url: str,
    status: int,
    content_type: str,
    text: str,
    request_headers: Mapping[str, str] | None,
    response_headers: Mapping[str, str] | None,
    wall_time: float | None,
    duration_ms: float,
) -> dict[str, Any]:
    wait = max(0.0, round(duration_ms, 3))
    return {
        "startedDateTime": _iso(wall_time),
        "time": wait,
        "request": {
            "method": "GET",
            "url": url,
            "httpVersion": "HTTP/1.1",
            "cookies": [],
            "headers": har_headers(request_headers),
            "queryString": [
                {"name": k, "value": v}
                for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)
            ],
            "headersSize": -1,
            "bodySize": 0,
        },
        "response": {
            "status": status,
            "statusText": "",
            "httpVersion": "HTTP/1.1",
            "cookies": [],
            "headers": har_headers(response_headers),
            "content": {
                "size": len(text.encode("utf-8")),
                "mimeType": content_type.split(";", 1)[0].strip().lower(),
                "text": text,
            },
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": -1,
        },
        "cache": {},
        "timings": {"send": 0, "wait": wait, "receive": 0},
    }


def capability_summary(
    entries: Sequence[Mapping[str, Any]], requests: Mapping[str, set[str]] | None = None
) -> dict[str, dict[str, int]]:
    """Per capability: recorded responses and, for a capture, distinct request URLs
    (counts only). A discover run keeps no request hash, so it has no distinct count."""

    responses = Counter(str(e["capability"]) for e in entries)
    result: dict[str, dict[str, int]] = {}
    for cap, n in sorted(responses.items()):
        result[cap] = {"responses": n}
        if requests is not None:
            result[cap]["distinct_requests"] = len(requests.get(cap, ()))
    return result


# ---------------------------------------------------------------------------
# Verify


def _load_run(root: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RecorderError("MANIFEST_UNREADABLE") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise RecorderError("MANIFEST_SCHEMA")
    if manifest.get("safety") != SAFETY:
        raise RecorderError("MANIFEST_SAFETY_BLOCK_DIFFERS")
    if manifest.get("origins") != list(ALLOWED_ORIGINS):
        raise RecorderError("MANIFEST_ORIGINS_DIFFER")
    if manifest.get("mode") not in {"discover", "capture"}:
        raise RecorderError("MODE_INVALID")
    return manifest


def _verify_har_entry(index: int, entry: object) -> tuple[str, str, str]:
    """Return (capability, body sha256, request sha256) or raise a coded refusal."""

    if not isinstance(entry, dict):
        raise RecorderError("HAR_ENTRY_SHAPE", str(index))
    request, response = entry.get("request"), entry.get("response")
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise RecorderError("HAR_ENTRY_SHAPE", str(index))
    for key in ("startedDateTime", "time", "cache", "timings"):
        if key not in entry:
            raise RecorderError("HAR_ENTRY_SHAPE", str(index))
    method, url = request.get("method"), request.get("url")
    if method != "GET":
        raise RecorderError("NON_GET_RECORDED", str(index))
    if not isinstance(url, str):
        raise RecorderError("HAR_ENTRY_SHAPE", str(index))
    decision = classify("GET", url)
    if decision.kind == "OFFORIGIN":
        raise RecorderError("OFFORIGIN_RECORDED", str(index))
    if decision.kind == "MUTATION_LIKE":
        raise RecorderError("MUTATION_LIKE_RECORDED", decision.template)
    if decision.kind != "RECORD":
        raise RecorderError("UNREVIEWED_ROUTE_RECORDED", decision.template)
    status = response.get("status")
    if (
        isinstance(status, bool)
        or not isinstance(status, int)
        or not 200 <= status < 300
    ):
        raise RecorderError("NON_2XX_RECORDED", str(index))
    headers = request.get("headers")
    if not isinstance(headers, list) or not isinstance(response.get("headers"), list):
        raise RecorderError("HAR_ENTRY_SHAPE", str(index))
    seen: dict[str, str] = {}
    for side in (headers, response["headers"]):
        for item in side:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("value"), str)
            ):
                raise RecorderError("HAR_HEADER_SHAPE", str(index))
            name = item["name"].casefold()
            if name in NEVER_RECORDED_HEADERS:
                raise RecorderError("COOKIE_CLASS_HEADER_RECORDED", str(index))
            if side is headers:
                seen.setdefault(name, item["value"])
    if (
        decision.authenticated
        and len(seen.get(JWT_HEADER, "").strip()) < MIN_JWT_LENGTH
    ):
        raise RecorderError("JWT_HEADER_MISSING", decision.template)
    content = response.get("content")
    text = content.get("text") if isinstance(content, dict) else None
    if not isinstance(text, str) or not text:
        raise RecorderError("BODY_TEXT_MISSING", str(index))
    try:
        parse_json_body(text.encode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise RecorderError("BODY_NOT_JSON", str(index)) from None
    return (
        decision.capability,
        _body_digest(text)[1],
        hashlib.sha256(url.encode("utf-8")).hexdigest(),
    )


def _verify_journal(
    root: Path, manifest: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (journal digests, journal HAR entries) after checking the header."""

    truncated: list[bool] = []
    notes = list(_iter_journal(root / JOURNAL, truncated))
    if truncated and not manifest.get("recovered"):
        raise RecorderError("JOURNAL_TRUNCATED")
    header = notes[0] if notes else None
    if (
        not isinstance(header, dict)
        or header.get("t") != "header"
        or header.get("schema") != SCHEMA
        or header.get("mode") != manifest["mode"]
        or header.get("run_id") != manifest.get("run_id")
        or header.get("har_name") != manifest.get("har_name")
    ):
        raise RecorderError("JOURNAL_HEADER_INVALID")
    digests = [n["digest"] for n in notes if n.get("t") == "entry"]
    hars = [n["har"] for n in notes if n.get("t") == "entry" and "har" in n]
    return digests, hars


def verify_run(root: Path) -> dict[str, object]:
    """Re-hash every artifact, reject strays, re-derive and re-check everything."""

    manifest = _load_run(root)
    mode = manifest["mode"]
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
    if mode == "discover":
        entries = json.loads((root / "discovery.json").read_text(encoding="utf-8"))
        if not isinstance(entries, list) or set(listed) != {"discovery.json", JOURNAL}:
            raise RecorderError("ENTRIES_UNREADABLE")
        known = {e.capability for e in QUANTBASE_CONTRACT.endpoints}
        for entry in entries:
            if _MUTATION_RE.search(str(entry["path_template"]).lower()):
                raise RecorderError("MUTATION_LIKE_RECORDED", entry["path_template"])
            if entry["capability"] not in known:
                raise RecorderError("UNREVIEWED_ROUTE_RECORDED")
            if not 200 <= int(entry["status"]) < 300:
                raise RecorderError("NON_2XX_RECORDED")
            if set(entry) & {"cookie", "headers", "request_sha256"}:
                raise RecorderError("DISCOVER_RUN_HOLDS_HEADERS")
        discovered, _ = _verify_journal(root, manifest)
        if discovered != entries:
            raise RecorderError("JOURNAL_DIFFERS_FROM_ENTRIES")
        summary = capability_summary(entries, None)
        if manifest["counts"]["entries"] != len(entries) or (
            manifest["capabilities"] != summary
        ):
            raise RecorderError("CAPABILITY_SUMMARY_DIFFERS")
        return {
            "status": manifest["status"],
            "mode": mode,
            "artifact_count": len(listed),
            "entries": len(entries),
            "capabilities": summary,
        }
    name = manifest.get("har_name")
    if not isinstance(name, str) or not HAR_NAME_RE.fullmatch(name):
        raise RecorderError("HAR_NAME_INVALID")
    if set(listed) != {f"har/{name}", JOURNAL}:
        raise RecorderError("RUN_DIRECTORY_DIFFERS_FROM_MANIFEST")
    try:
        document = json.loads((root / "har" / name).read_bytes().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise RecorderError("HAR_UNREADABLE") from None
    log = document.get("log") if isinstance(document, dict) else None
    if (
        not isinstance(log, dict)
        or log.get("version") != "1.2"
        or not isinstance(log.get("creator"), dict)
        or not isinstance(log.get("entries"), list)
    ):
        raise RecorderError("HAR_STRUCTURE")
    entries = log["entries"]
    digests = manifest.get("entry_digests")
    if not isinstance(digests, list) or len(digests) != len(entries):
        raise RecorderError("ENTRY_COUNT_DIFFERS")
    if manifest["counts"]["entries"] != len(entries):
        raise RecorderError("ENTRY_COUNT_DIFFERS")
    requests: dict[str, set[str]] = {}
    for index, (entry, digest) in enumerate(zip(entries, digests, strict=True)):
        capability, body_sha, request_sha = _verify_har_entry(index, entry)
        if (
            digest.get("body_sha256") != body_sha
            or digest.get("capability") != capability
        ):
            raise RecorderError("ENTRY_HASH_DIFFERS", str(index))
        if digest.get("request_sha256") != request_sha:
            raise RecorderError("REQUEST_HASH_DIFFERS", str(index))
        if digest.get("body_bytes") != entry["response"]["content"]["size"]:
            raise RecorderError("ENTRY_HASH_DIFFERS", str(index))
        requests.setdefault(capability, set()).add(request_sha)
    journal_digests, journal_hars = _verify_journal(root, manifest)
    if journal_hars != entries or journal_digests != digests:
        raise RecorderError("JOURNAL_DIFFERS_FROM_HAR")
    summary = capability_summary(digests, requests)
    if manifest["capabilities"] != summary:
        raise RecorderError("CAPABILITY_SUMMARY_DIFFERS")
    jwt = manifest.get("jwt")
    if not isinstance(jwt, dict) or jwt.get("api_responses_with_jwt") != sum(
        1 for d in digests if d.get("jwt_header_present")
    ):
        raise RecorderError("JWT_SUMMARY_DIFFERS")
    return {
        "status": manifest["status"],
        "mode": mode,
        "recovered": bool(manifest.get("recovered")),
        "artifact_count": len(listed),
        "entries": len(entries),
        "capabilities": summary,
    }


# ---------------------------------------------------------------------------
# propose-contract (offline; reads runs, writes a DRAFT, applies nothing)

DRAFT_SCHEMA = "quantbase-contract-extension-draft/1"
DRAFT_BANNER = (
    "DRAFT contract extension. Built from route templates and query key NAMES only "
    "(never a value); applied by no tool. A human reviews every route and key against "
    "the raw capture before it becomes part of a reviewed contract extension."
)
_PORTABLE_KEY = re.compile(r"[a-z][a-z0-9_.-]*")


def _pattern_for(template: str) -> str | None:
    if "{" not in template:
        return None
    pieces = re.split(r"(\{[a-z_]+\})", template)
    out = []
    for piece in pieces:
        if piece == "{build_id}":
            out.append("[^/]+")
        elif piece in {"{id}", "{user_id}"}:
            out.append("[0-9]+")
        else:
            out.append(re.escape(piece))
    return "".join(out)


def suggest_capability(template: str, taken: set[str]) -> str:
    """A portable capability name from path words only, unique among ``taken``."""

    path = re.sub(r"^/(_next/data/\{build_id\}|api)/", "", template)
    words = []
    for segment in path.split("/"):
        if not segment or segment.startswith("{"):
            continue
        word = re.sub(r"\.json$", "", segment).replace("_", "-").lower()
        word = re.sub(r"[^a-z0-9-]", "", word)
        if word:
            words.append(word)
    base = ".".join(words) or "route"
    if not base[0].isalpha():
        base = "r" + base
    name, counter = base, 1
    while name in taken:
        counter += 1
        name = f"{base}.{counter}"
    return name


def build_contract_draft(manifests: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pure: propose declared extra routes and extra query keys from the unreviewed
    same-origin GET routes that finished runs listed (templates and key names only)."""

    observed: dict[tuple[str, str, tuple[str, ...]], int] = {}
    for manifest in manifests:
        for item in manifest["UNREVIEWED_ROUTE_NOT_RECORDED"]:
            key = (
                str(item["origin"]),
                str(item["path_template"]),
                tuple(item["query_keys"]),
            )
            observed[key] = observed.get(key, 0) + int(item["count"])
    reviewed = {(e.origin, e.path): e for e in QUANTBASE_CONTRACT.endpoints}
    taken = {e.capability for e in QUANTBASE_CONTRACT.endpoints}
    extra_keys: dict[str, set[str]] = {}
    routes: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for (origin, template, keys), count in sorted(observed.items()):
        row: dict[str, Any] = {
            "origin": origin,
            "path_template": template,
            "query_keys": list(keys),
            "count": count,
        }
        unportable = [k for k in keys if not _PORTABLE_KEY.fullmatch(k)]
        endpoint = reviewed.get((origin, template))
        if unportable:
            row["proposal"] = "UNPORTABLE_QUERY_KEY"
        elif endpoint is not None:
            base = set(endpoint.query_keys)
            if base <= set(keys) and set(keys) - base:
                extra_keys.setdefault(endpoint.capability, set()).update(
                    set(keys) - base
                )
                row["proposal"] = "EXTENDS_REVIEWED_ROUTE"
                row["capability"] = endpoint.capability
            else:
                row["proposal"] = "QUERY_SHAPE_DIFFERS_REVIEW_BY_HAND"
                row["capability"] = endpoint.capability
        else:
            capability = suggest_capability(template, taken)
            taken.add(capability)
            pattern = _pattern_for(template)
            route: dict[str, Any] = {
                "capability": capability,
                "origin": origin,
                "path": template,
                "query_keys": sorted(keys),
                "authentication_header": "jwt" if origin == API_ORIGIN else None,
            }
            if pattern is not None:
                route["path_pattern"] = pattern
            routes[(origin, template, keys)] = route
            row["proposal"] = "NEW_ROUTE"
            row["capability"] = capability
        rows.append(row)
    return {
        "schema": DRAFT_SCHEMA,
        "applied": False,
        "banner": DRAFT_BANNER,
        "extension": {
            "schema_version": 1,
            "routes": list(routes.values()),
            "extra_query_keys": {k: sorted(v) for k, v in sorted(extra_keys.items())},
        },
        "observed": rows,
    }


def render_draft_markdown(draft: Mapping[str, Any]) -> str:
    lines = [
        "# Quantbase contract extension (DRAFT, not applied)",
        "",
        draft["banner"],
        "",
    ]
    lines += ["## Observed unreviewed same-origin GET routes", ""]
    for row in draft["observed"]:
        lines.append(
            f"- `{row['path_template']}` on {row['origin']} keys {row['query_keys']} "
            f"x{row['count']}: {row['proposal']}"
            + (f" ({row['capability']})" if "capability" in row else "")
        )
    if not draft["observed"]:
        lines.append("- none")
    extension = draft["extension"]
    lines += ["", "## Proposed declaration", "", "```json"]
    lines += [json.dumps(extension, indent=2, sort_keys=True), "```", ""]
    return "\n".join(lines)


def propose_contract(
    run_roots: Sequence[Path],
    out: Path,
    *,
    root_policy: RootPolicy = assert_output_root,
) -> dict[str, Any]:
    """Verify the input runs, then write the DRAFT files into a new directory."""

    if not run_roots:
        raise RecorderError("NO_RUNS")
    manifests = []
    for run in run_roots:
        verify_run(run)
        manifests.append(_load_run(run))
    root_policy(out)
    draft = build_contract_draft(manifests)
    out.mkdir(parents=True)
    artifacts = []
    for name, payload in (
        ("contract-extension.draft.json", _canonical(draft)),
        ("contract-extension.draft.md", render_draft_markdown(draft).encode("utf-8")),
    ):
        artifacts.append({"path": name, **_write_new(out / name, payload)})
    manifest = {
        "schema": "quantbase-contract-extension-proposal/1",
        "applied": False,
        "inputs": [run.name for run in run_roots],
        "artifacts": artifacts,
    }
    _write_new(out / "proposal-manifest.json", _canonical(manifest))
    return manifest


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


def _headers_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


class Listener:
    """Attach to signed-in Quantbase page targets and feed finished responses.

    Reads the method and URL of every request, and the header objects ONLY of
    requests the contract would record (for the HAR file). ``*ExtraInfo`` events
    (cookies, raw headers) are never opened, and request post data is never read.
    """

    def __init__(
        self,
        transport: CdpTransport,
        recorder: Recorder,
        *,
        max_seconds: float = 0.0,
        warn_after: float = DEFAULT_WARN_AFTER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        call_timeout: float = 20.0,
    ) -> None:
        self._transport = GuardedTransport(transport)
        self._recorder = recorder
        self._max_seconds = max_seconds
        self._warn_after = warn_after
        self._warned = False
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
                elapsed = self._clock() - started
                if self._max_seconds and elapsed >= self._max_seconds:
                    return
                if (
                    self._warn_after
                    and not self._warned
                    and elapsed >= self._warn_after
                    and not self._recorder.entries
                ):
                    self._warned = True
                    self._recorder.warn(
                        "NO_ENTRIES_RECORDED",
                        f"after {self._warn_after:g} s: " + NO_ENTRIES_TEXT,
                    )
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
            current: dict[str, Any] = {
                "method": str(request["method"]),
                "url": str(request["url"]),
                "wall": params.get("wallTime"),
                "ts": params.get("timestamp"),
            }
            if classify(current["method"], current["url"]).kind == "RECORD":
                current["request_headers"] = _headers_dict(request.get("headers"))
            self._requests[key] = current
        elif method == "Network.responseReceived":
            current_req = self._requests.get(key)
            response = params["response"]
            if current_req is not None:
                current_req["status"] = int(response["status"])
                current_req["mime"] = str(response.get("mimeType", ""))
                current_req["type"] = str(params.get("type", ""))
                if "request_headers" in current_req:
                    current_req["response_headers"] = _headers_dict(
                        response.get("headers")
                    )
        elif method == "Network.loadingFinished":
            done = self._requests.pop(key, None)
            if done is not None and "status" in done:
                started, ended = done.get("ts"), params.get("timestamp")
                if isinstance(started, (int, float)) and isinstance(
                    ended, (int, float)
                ):
                    done["duration_ms"] = max(0.0, (ended - started) * 1000.0)
                self._finish(
                    done,
                    done["status"],
                    done.get("mime", ""),
                    (sid, request_id),
                    done.get("type", ""),
                )
        elif method == "Network.loadingFailed":
            if self._requests.pop(key, None) is not None:
                self._recorder.note_aborted()

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
            request_headers=current.get("request_headers"),
            response_headers=current.get("response_headers"),
            resource_type=resource_type,
            wall_time=current.get("wall"),
            duration_ms=float(current.get("duration_ms", 0.0)),
        )


# ---------------------------------------------------------------------------
# CLI


def _live(args: argparse.Namespace) -> dict[str, Any]:  # pragma: no cover
    recorder = Recorder(
        args.output_root,
        mode=args.command,
        har_name=getattr(args, "har_name", DEFAULT_HAR_NAME),
    )
    transport = WebSocketTransport(args.cdp_url)
    try:
        Listener(
            transport,
            recorder,
            max_seconds=args.max_seconds,
            warn_after=args.warn_after_seconds,
        ).run()
    except KeyboardInterrupt:
        pass
    finally:
        transport.close()
    return recorder.finish()


def _summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Aggregate output only: counts and path templates, never a header or a body."""

    return {
        "status": manifest["status"],
        "mode": manifest["mode"],
        "jwt": manifest["jwt"],
        "counts": manifest["counts"],
        "capabilities": manifest["capabilities"],
        "warnings": [w["code"] for w in manifest.get("warnings", [])],
        "mutation_like_not_recorded": [
            {k: m[k] for k in ("method", "path_template", "count")}
            for m in manifest["MUTATION_LIKE_NOT_RECORDED"]
        ],
        "unreviewed_routes_not_recorded": [
            {k: m[k] for k in ("origin", "path_template", "query_keys", "count")}
            for m in manifest["UNREVIEWED_ROUTE_NOT_RECORDED"]
        ],
        "other_not_recorded": manifest["OTHER_NOT_RECORDED"],
    }


def jwt_lines(manifest: Mapping[str, Any]) -> list[str]:
    """The FIRST output lines of discover, capture and recover: whether the page-set
    jwt header reached the recorder. False means STOP."""

    jwt = manifest["jwt"]
    lines = [f"jwt_header_present: {str(bool(jwt['present'])).lower()}"]
    if not jwt["present"]:
        if jwt["api_responses_without_jwt"]:
            why = "some API responses arrived without a jwt request header"
        else:
            why = "no API response with a jwt header was recorded"
        lines.append(
            f"STOP: {why}. Do not continue; tell the coordinator before "
            "loading more pages."
        )
    return lines


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("discover", "capture"):
        item = sub.add_parser(name)
        item.add_argument("--cdp-url", required=True)
        item.add_argument("--output-root", required=True, type=Path)
        item.add_argument("--max-seconds", type=float, default=0.0)
        item.add_argument(
            "--warn-after-seconds",
            type=float,
            default=DEFAULT_WARN_AFTER_SECONDS,
            help="warn once if nothing was recorded after this many seconds "
            "(default 30; 0 disables)",
        )
        if name == "capture":
            item.add_argument("--har-name", default=DEFAULT_HAR_NAME)
    check = sub.add_parser("verify")
    check.add_argument("--run-root", required=True, type=Path)
    recover = sub.add_parser(
        "recover", help="assemble the HAR and manifest from a killed run's journal"
    )
    recover.add_argument("--run-root", required=True, type=Path)
    contract = sub.add_parser(
        "propose-contract",
        help="DRAFT contract extension from the unreviewed routes of finished runs",
    )
    contract.add_argument("--run-root", required=True, type=Path, action="append")
    contract.add_argument("--out", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            result: dict[str, Any] = verify_run(args.run_root)
            lead: list[str] = []
        elif args.command == "propose-contract":
            written = propose_contract(args.run_root, args.out)
            result = {
                "status": "DRAFT_WRITTEN",
                "applied": False,
                "artifacts": [a["path"] for a in written["artifacts"]],
            }
            lead = []
        else:
            manifest = (
                recover_run(args.run_root) if args.command == "recover" else _live(args)
            )
            result = _summary(manifest)
            lead = jwt_lines(manifest)
    except RecorderError as exc:
        print(f"REFUSED {exc.code}")
        return 2
    for line in lead:
        print(line)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
