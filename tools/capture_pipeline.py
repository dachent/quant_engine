"""Capture pipeline core (#913): one declared source = one stage chain S0..S5.

Stdlib only, value-free logging (ids, counts, codes; never an account value or a credential).

* S0 :func:`preflight`   transport reachable / wall / not wired / credential, with the exact owner step.
* S2 :func:`hash_files`  SHA-256 manifest of a capture run (create-only, atomic).
* S3 :func:`verify`      freshness against the release cutoff plus every closure check, no first-failure masking;
  the verdict is bound to the manifest digest, as-of and cutoff it verified.
* S4 :func:`admit_input` stage + rename the hashed files under the retained evidence root and pin them (create-only,
  per source and cutoff, under a lock).
* S5 :func:`captured_input` / :func:`captured_bytes` the only way a reader opens a captured file: resolves the pin,
  checks the reader is admitted, re-hashes, refuses ``UNCAPTURED_INPUT``, ``STALE_INPUT``, ``POST_CUTOFF``,
  ``READER_NOT_ADMITTED`` or ``HASH_MISMATCH``.

S1 (the recorder) is a declared tool run under ``capture_action_guard``; this module never types credentials.
Closure checks are pure functions over plain rows so they run on a synthetic capture.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
import functools
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any
from urllib.parse import urlsplit
import uuid

TRANSPORTS = (
    "BROWSER_SIGNED_IN",
    "API_VAULT",
    "FLEX_TOKEN_VAULT",
    "PUBLIC_HTTP",
    "CREDENTIAL_MANAGER",
    "DERIVED",  # built from retained, hashed captures: no window, no credential, no network
)
VAULT_TRANSPORTS = ("API_VAULT", "FLEX_TOKEN_VAULT")
# kind "owner-login-or-credential-manager": the credential lives in the operator's credential store (or the owner
# signs in); S0 cannot check it without reading the store, so it reports UNVERIFIABLE_BY_DESIGN with the manual step
NAMED_STORE_TRANSPORTS = VAULT_TRANSPORTS + ("CREDENTIAL_MANAGER",)
STAGE_MODES = ("EXISTING_RECORDER", "DERIVE_FROM_RETAINED", "NEW_STAGE_REQUIRED")
REQUIRED_KEYS = (
    "provider", "transport", "credential_ref", "capture_stage", "login_stop", "freshness",
    "closure_checks", "admits", "owner_declared_ok",
)  # fmt: skip
SOURCE_KEYS = set(REQUIRED_KEYS) | {
    "source_id", "cdp_port", "window_hosts", "probe_url", "source_kind", "owner_evidence", "check_params",
}  # fmt: skip
ID_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")
SECRET_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)
HEX64_RE = re.compile(r"[0-9a-f]{64}")
SEGMENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")
RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
MANIFEST_NAME = "capture-manifest.json"
STAGE_KEYS = {
    "mode",
    "tool",
    "derivation",
    "pull_tool",
    "margin_setting",
    "recorder",
    "blocked_on",
}
MAX_TOL = 0.05
MAX_OVER_NAV_TOL = 0.1
MAX_MISSING_CAP = 10
LOCK_STALE_SECONDS = 600
MAX_AGE_CAP_DAYS = 400
MAX_BRIDGE_MOVE = 0.05


class CaptureRefused(ValueError):
    """A pipeline rule was broken; ``code`` is a stable reason and the message carries no value."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _iso(value: object, code: str) -> date:
    if not isinstance(value, str):
        raise CaptureRefused(code)
    try:
        d = date.fromisoformat(value)
    except ValueError as exc:
        raise CaptureRefused(code) from exc
    if (
        d.isoformat() != value
    ):  # one spelling per day: 20261106 and 2026-W45-5 are refused
        raise CaptureRefused(code)
    return d


# ----------------------------------------------------------------------------- contract


@dataclass(frozen=True)
class Source:
    source_id: str
    provider: str
    transport: str
    credential_ref: str | None
    capture_stage: Mapping[str, Any]
    login_stop: bool
    max_age_days: int
    closure_checks: tuple[str, ...]
    admits: tuple[str, ...]
    owner_declared_ok: bool
    cdp_port: int | None = None
    window_hosts: tuple[str, ...] = ()
    probe_url: str | None = None
    owner_evidence: Mapping[str, Any] | None = None
    check_params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def stage_mode(self) -> str:
        return str(self.capture_stage.get("mode"))

    @property
    def stage_tool(self) -> str | None:
        tool = self.capture_stage.get("tool")
        return str(tool) if tool else None


def closure_check_names() -> tuple[str, ...]:
    return tuple(sorted(CLOSURE_CHECKS))


@functools.lru_cache(maxsize=64)
def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_readers(
    path: Path, *, repo_root: Path | None = None
) -> dict[str, dict[str, str]]:
    """The contract's reader table ``{reader: {"file", "symbol"[, "config_key"]}}``. With ``repo_root`` each reader's
    file must exist and contain its symbol, so ``admits`` can only name a reader that really exists."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    readers = doc.get("readers")
    if not isinstance(readers, dict) or not readers:
        raise CaptureRefused("CONTRACT_READERS")
    for name, r in readers.items():
        if (
            not isinstance(r, dict)
            or not {"file", "symbol"} <= set(r)
            or set(r) - {"file", "symbol", "config_key"}
        ):
            raise CaptureRefused("CONTRACT_READERS", name)
        if repo_root is not None:
            root = Path(repo_root).resolve()
            f = (root / r["file"]).resolve()
            if (
                not f.is_relative_to(root)
                or not f.is_file()
                or not re.search(
                    rf"^\s*def {re.escape(r['symbol'])}\b",
                    _read_text(f),
                    re.M,
                )
            ):
                raise CaptureRefused("CONTRACT_READER_MISSING", name)
    return {k: dict(v) for k, v in readers.items()}


def load_sources(path: Path, *, repo_root: Path | None = None) -> dict[str, Source]:
    try:
        return _load_sources(path, repo_root=repo_root)
    except (TypeError, AttributeError, KeyError) as exc:
        raise CaptureRefused("CONTRACT_SHAPE", type(exc).__name__) from exc


def _load_sources(path: Path, *, repo_root: Path | None) -> dict[str, Source]:
    """Load and lint ``contracts/capture-sources-v1.json`` (strict types, no unknown keys).

    Lint: ids unique and well formed; transport in the enum; a named-store transport names a bare secret (no value
    shape, no leading dash) and any other transport names none; ``login_stop`` true exactly for browser transports;
    browser sources declare an integer CDP port and the host suffixes that prove a signed-in window; public sources
    declare a probe URL; stage mode in the enum and, with ``repo_root``, the stage tool and every reader must exist;
    closure checks non-empty and known; ``owner_declared_ok`` a real bool and, when true, an evidence record.
    """
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema_version") != 1 or not isinstance(doc.get("sources"), list):
        raise CaptureRefused("CONTRACT_SHAPE")
    readers = load_readers(path, repo_root=repo_root)
    out: dict[str, Source] = {}
    for raw in doc["sources"]:
        if not isinstance(raw, dict):
            raise CaptureRefused("CONTRACT_SHAPE")
        sid = raw.get("source_id")
        if not isinstance(sid, str) or not ID_RE.fullmatch(sid):
            raise CaptureRefused("CONTRACT_SOURCE_ID", str(sid)[:40])
        missing = [k for k in REQUIRED_KEYS if k not in raw]
        if missing:
            raise CaptureRefused("CONTRACT_FIELD_MISSING", f"{sid}:{','.join(missing)}")
        if set(raw) - SOURCE_KEYS:
            raise CaptureRefused("CONTRACT_UNKNOWN_KEY", sid)
        if sid in out:
            raise CaptureRefused("CONTRACT_DUPLICATE_SOURCE", sid)
        transport = raw["transport"]
        if transport not in TRANSPORTS:
            raise CaptureRefused("CONTRACT_TRANSPORT", sid)
        browser = transport == "BROWSER_SIGNED_IN"
        if raw["login_stop"] is not browser:
            raise CaptureRefused("CONTRACT_LOGIN_STOP", sid)
        ref = raw["credential_ref"]
        if transport in NAMED_STORE_TRANSPORTS:
            if not isinstance(ref, str) or not SECRET_NAME_RE.fullmatch(ref):
                raise CaptureRefused("CONTRACT_CREDENTIAL_REF", sid)
        elif ref is not None:
            raise CaptureRefused("CONTRACT_CREDENTIAL_REF", sid)
        port, hosts = raw.get("cdp_port"), raw.get("window_hosts", [])
        if browser:
            if (
                not isinstance(port, int)
                or isinstance(port, bool)
                or not 1024 <= port <= 65535
            ):
                raise CaptureRefused("CONTRACT_CDP_PORT", sid)
            if (
                not isinstance(hosts, list)
                or not hosts
                or not all(isinstance(h, str) and HOST_RE.fullmatch(h) for h in hosts)
            ):
                raise CaptureRefused("CONTRACT_WINDOW_HOSTS", sid)
        elif port is not None or hosts:
            raise CaptureRefused("CONTRACT_CDP_PORT", sid)
        probe = raw.get("probe_url")
        stage = raw["capture_stage"]
        if (
            not isinstance(stage, dict)
            or stage.get("mode") not in STAGE_MODES
            or set(stage) - STAGE_KEYS
        ):
            raise CaptureRefused("CONTRACT_STAGE_MODE", sid)
        if not isinstance(raw["provider"], str) or not raw["provider"]:
            raise CaptureRefused("CONTRACT_PROVIDER", sid)
        needs_probe = (
            transport == "PUBLIC_HTTP" and stage["mode"] != "NEW_STAGE_REQUIRED"
        )
        if needs_probe != (probe is not None) or (probe is not None and not (isinstance(probe, str) and probe.startswith("https://") and urlsplit(probe).username is None and urlsplit(probe).password is None)):  # fmt: skip
            raise CaptureRefused("CONTRACT_PROBE_URL", sid)
        tool = stage.get("tool")
        if stage["mode"] != "NEW_STAGE_REQUIRED" and not tool:
            raise CaptureRefused("CONTRACT_STAGE_TOOL", sid)
        if tool and (
            not isinstance(tool, str)
            or ".." in Path(tool).parts
            or Path(tool).is_absolute()
        ):
            raise CaptureRefused("CONTRACT_STAGE_TOOL_MISSING", sid)
        if tool and repo_root is not None and not (Path(repo_root) / tool).exists():
            raise CaptureRefused("CONTRACT_STAGE_TOOL_MISSING", sid)
        fresh = raw["freshness"]
        if isinstance(fresh, dict) and set(fresh) - {"max_age_days"}:
            raise CaptureRefused("CONTRACT_FRESHNESS", sid)
        age = fresh.get("max_age_days") if isinstance(fresh, dict) else None
        if (
            not isinstance(age, int)
            or isinstance(age, bool)
            or not 0 <= age <= MAX_AGE_CAP_DAYS
        ):
            raise CaptureRefused("CONTRACT_FRESHNESS", sid)
        checks = raw["closure_checks"]
        if (
            not isinstance(checks, list)
            or not checks
            or len(set(map(str, checks))) != len(checks)
        ):
            raise CaptureRefused("CONTRACT_CHECKS", sid)
        unknown = [c for c in checks if c not in CLOSURE_CHECKS]
        if unknown:
            raise CaptureRefused(
                "CONTRACT_UNKNOWN_CHECK", f"{sid}:{','.join(map(str, unknown))}"
            )
        admits = raw["admits"]
        if not isinstance(admits, list) or not all(isinstance(a, str) for a in admits):
            raise CaptureRefused("CONTRACT_ADMITS", sid)
        bad = [a for a in admits if a not in readers]
        if bad:
            raise CaptureRefused("CONTRACT_UNKNOWN_READER", f"{sid}:{','.join(bad)}")
        params = raw.get("check_params", {})
        if not isinstance(params, dict) or set(params) - set(checks):
            raise CaptureRefused("CONTRACT_CHECKS", sid)
        owner_ok, evidence = raw["owner_declared_ok"], raw.get("owner_evidence")
        if not isinstance(owner_ok, bool):
            raise CaptureRefused("CONTRACT_OWNER_DECLARED", sid)
        if owner_ok and not (isinstance(evidence, dict) and HEX64_RE.fullmatch(str(evidence.get("sha256"))) and _iso(evidence.get("owner_line_date"), "CONTRACT_OWNER_DECLARED")):  # fmt: skip
            raise CaptureRefused("CONTRACT_OWNER_DECLARED", sid)
        out[sid] = Source(
            sid, raw["provider"], transport, ref, stage, browser, age, tuple(checks), tuple(admits), owner_ok,
            port, tuple(hosts), probe, evidence if owner_ok else None, params,
        )  # fmt: skip
    return out


# ----------------------------------------------------------------------------- S0 preflight

WINDOW_OK, WINDOW_LOGIN_WALL, WINDOW_ABSENT = "OK", "LOGIN_WALL", "ABSENT"


@dataclass(frozen=True)
class Preflight:
    source_id: str
    # READY | BLOCKED_LOGIN | BLOCKED_NO_WINDOW | BLOCKED_CREDENTIAL | BLOCKED_NOT_WIRED | BLOCKED_UNREACHABLE
    # | BLOCKED_PROBE_ERROR | UNVERIFIABLE_BY_DESIGN
    status: str
    owner_step: str


def preflight(
    source: Source,
    *,
    window_state: Callable[[Source], str],
    vault_ok: Callable[[str], bool],
    http_ok: Callable[[Source], bool] = lambda _s: False,
    store_ok: Callable[[str], bool] | None = None,
) -> Preflight:
    """S0. Probes are injected and read-only: ``window_state`` reads the CDP tab list, ``vault_ok`` reports only whether
    the sanctioned vault reader succeeded, ``http_ok`` is one GET of the declared probe URL. A probe that raises blocks
    only this source (``BLOCKED_PROBE_ERROR``). READY means "may be captured now", not "fresh"."""
    sid = source.source_id
    if source.stage_mode == "NEW_STAGE_REQUIRED":
        return Preflight(
            sid,
            "BLOCKED_NOT_WIRED",
            "no capture stage is built for this source yet (declared so the refresh lists it)",
        )
    try:
        if source.transport == "BROWSER_SIGNED_IN":
            state = window_state(source)
            if state == WINDOW_ABSENT:
                return Preflight(
                    sid,
                    "BLOCKED_NO_WINDOW",
                    f"open a signed-in {source.provider} tab on the capture port",
                )
            if state == WINDOW_LOGIN_WALL:
                return Preflight(
                    sid,
                    "BLOCKED_LOGIN",
                    f"sign in to {source.provider} (login/2FA is the owner's)",
                )
            if state != WINDOW_OK:
                raise CaptureRefused("PREFLIGHT_UNKNOWN_WINDOW_STATE", sid)
        elif source.transport == "CREDENTIAL_MANAGER":
            manual = (
                f"store the credential-manager entry '{source.credential_ref}' (or sign in to {source.provider}); the "
                "provider package checks the credential itself at S1"
            )
            if (
                store_ok is None
            ):  # no existence probe available: the credential cannot be checked without reading it
                return Preflight(sid, "UNVERIFIABLE_BY_DESIGN", manual)
            if not store_ok(
                str(source.credential_ref)
            ):  # existence only (never the secret)
                return Preflight(sid, "BLOCKED_CREDENTIAL", manual)
        elif (
            source.transport in VAULT_TRANSPORTS
            and source.stage_mode != "DERIVE_FROM_RETAINED"
        ):
            assert source.credential_ref is not None
            if not vault_ok(source.credential_ref):
                return Preflight(
                    sid,
                    "BLOCKED_CREDENTIAL",
                    "the vault secret could not be read (az login / secret expiry)",
                )
        elif source.transport == "PUBLIC_HTTP" and not http_ok(source):
            return Preflight(
                sid, "BLOCKED_UNREACHABLE", "the public source did not answer its probe"
            )
    except CaptureRefused:
        raise
    except Exception:  # noqa: BLE001 - a broken probe blocks one source, never the table; no detail is logged
        return Preflight(
            sid, "BLOCKED_PROBE_ERROR", "the preflight probe itself failed"
        )
    return Preflight(sid, "READY", "")


# ----------------------------------------------------------------------------- S2 hash


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_rel(name: str) -> Path:
    """A manifest/plan name: relative, forward slashes, every segment ``[A-Za-z0-9_][A-Za-z0-9._-]*`` (so no ``:`` stream
    syntax, no control characters, no spaces), no ``..``, no Windows device names, no trailing dot, not the manifest
    copy's own name."""
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or name.startswith("/")
        or name == MANIFEST_NAME
    ):
        raise CaptureRefused("UNSAFE_PATH")
    for seg in name.split("/"):
        if (
            not SEGMENT_RE.fullmatch(seg)
            or seg.endswith(".")
            or seg.split(".")[0].upper() in RESERVED_NAMES
        ):
            raise CaptureRefused("UNSAFE_PATH")
    return Path(name)


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def hash_files(
    run_dir: Path, manifest_path: Path, *, source_id: str, captured_on: str, tool: str
) -> dict[str, Any]:
    """S2. Hash every regular file under ``run_dir`` (symlinks refused) into a create-only, atomically written manifest."""
    run_dir = Path(run_dir)
    files = []
    for p in sorted(run_dir.rglob("*")):
        if p.is_symlink():
            raise CaptureRefused("SYMLINK_IN_CAPTURE", source_id)
        if p.is_file() and p.resolve() != Path(manifest_path).resolve():
            files.append(p)
    if not files:
        raise CaptureRefused("EMPTY_CAPTURE", source_id)
    manifest = {
        "schema_version": 1, "source_id": source_id, "captured_on": captured_on, "tool": tool,
        "files": {p.relative_to(run_dir).as_posix(): {"sha256": sha256_file(p), "bytes": p.stat().st_size} for p in files},
    }  # fmt: skip
    _create_only(
        Path(manifest_path), json.dumps(manifest, indent=1, sort_keys=True) + "\n"
    )
    return manifest


def _create_only(path: Path, text: str) -> None:
    """Atomic create-only write: a crash never leaves a truncated target, and an existing target is refused."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(
        text, encoding="utf-8", newline="\n"
    )  # LF bytes: digests must not depend on the OS
    try:
        os.link(tmp, path)
    except FileExistsError as exc:
        raise CaptureRefused("CREATE_ONLY_EXISTS", path.name) from exc
    finally:
        tmp.unlink(missing_ok=True)


# ----------------------------------------------------------------------------- closure checks


@dataclass(frozen=True)
class CheckResult:
    check: str
    ok: bool
    code: str  # stable code; "OK" when ok
    count: int = 0  # number of offending rows, never a value


def _result(name: str, bad: int, code: str) -> CheckResult:
    return CheckResult(name, bad == 0, "OK" if bad == 0 else code, bad)


def _finite(*values: object) -> bool:
    try:
        return all(math.isfinite(float(v)) for v in values)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def check_nav_identity(
    rows: Sequence[Mapping[str, Any]], *, tol: float = 0.01, chain: bool = True
) -> CheckResult:
    """Broker NAV: opening + sum(components) = ending each day (Decimal, within ``tol``) and, when ``chain``, each opening =
    prior ending. ``chain=False`` is for sources with published restatements at period boundaries (declared in the
    contract's ``check_params`` and reported separately). ``tol`` is capped at ``MAX_TOL``."""
    if not rows:
        return CheckResult("nav_identity", False, "NO_ROWS")
    if not _finite(tol) or not 0 <= tol <= MAX_TOL:
        return CheckResult("nav_identity", False, "TOLERANCE_OUT_OF_RANGE")
    t = Decimal(str(tol)) + Decimal("0.0000001")
    bad, prev_end = 0, None
    for r in rows:
        comps = list(r["components"])
        if not _finite(r["opening"], r["ending"], *comps):
            return CheckResult("nav_identity", False, "NON_FINITE", 1)
        opening, ending = Decimal(str(r["opening"])), Decimal(str(r["ending"]))
        if (
            abs(opening + sum((Decimal(str(c)) for c in comps), Decimal(0)) - ending)
            > t
        ):
            bad += 1
        if chain and prev_end is not None and abs(opening - prev_end) > t:
            bad += 1
        prev_end = ending
    return _result("nav_identity", bad, "NAV_IDENTITY_BREAK")


def check_coverage(
    dates: Iterable[str],
    start: str,
    end: str,
    *,
    non_trading: Iterable[str] = (),
    max_missing: int = 0,
) -> CheckResult:
    """Every expected weekday in [start, end] has a row (``non_trading`` days are not expected); at least one row must
    exist; at most ``max_missing`` (capped at ``MAX_MISSING_CAP`` and at a tenth of the window) may be absent in total;
    rows outside the window are refused."""
    d0, d1 = _iso(start, "BAD_WINDOW"), _iso(end, "BAD_WINDOW")
    if d0 > d1:
        return CheckResult("coverage", False, "BAD_WINDOW")
    have = {_iso(d, "BAD_DATE") for d in dates}
    skip = {_iso(d, "BAD_DATE") for d in non_trading}
    expected = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    expected = [d for d in expected if d.weekday() < 5 and d not in skip]
    if not expected:
        return CheckResult("coverage", False, "NO_EXPECTED_DAYS")
    if not have:
        return CheckResult("coverage", False, "NO_ROWS")
    outside = sum(1 for d in have if d < d0 or d > d1)
    if outside:
        return CheckResult("coverage", False, "OUT_OF_WINDOW", outside)
    if max_missing and not 0 < max_missing <= min(MAX_MISSING_CAP, len(expected) // 10):
        return CheckResult("coverage", False, "TOLERANCE_OUT_OF_RANGE")
    missing = sum(1 for d in expected if d not in have)
    return _result("coverage", 0 if missing <= max_missing else missing, "COVERAGE_GAP")


def check_expected_dates(dates: Iterable[str], expected: Iterable[str]) -> CheckResult:
    """A sparse series (quarterly or monthly statements): exactly the expected dates are present."""
    have, want = (
        {_iso(d, "BAD_DATE") for d in dates},
        {_iso(d, "BAD_DATE") for d in expected},
    )
    if not want:
        return CheckResult("expected_dates", False, "NO_EXPECTED_DAYS")
    return _result(
        "expected_dates", len(want - have) + len(have - want), "DATES_DIFFER"
    )


def check_sum_equals_total(
    parts: Sequence[float | Decimal | str],
    total: float | Decimal | str,
    *,
    tol_per_part: float | Decimal | str = 0,
) -> CheckResult:
    """A total equals the sum of its parts to the cent (Decimal). ``tol_per_part`` (default 0, capped at half a cent) is for
    sources that round each line independently (declared in the contract's ``check_params``): the allowed difference is
    half a cent plus ``tol_per_part`` for every part."""
    if not parts or not _finite(total, tol_per_part, *parts):
        return CheckResult(
            "sum_equals_total", False, "NO_ROWS" if not parts else "NON_FINITE"
        )
    tp = Decimal(str(tol_per_part))
    if not 0 <= tp <= Decimal("0.005"):
        return CheckResult("sum_equals_total", False, "TOLERANCE_OUT_OF_RANGE")
    diff = abs(sum((Decimal(str(p)) for p in parts), Decimal(0)) - Decimal(str(total)))
    return _result(
        "sum_equals_total",
        0 if diff < Decimal("0.005") + tp * len(parts) else 1,
        "TOTAL_MISMATCH",
    )


def check_cash_walk(
    rows: Sequence[Mapping[str, Any]],
    *,
    over_nav_tol: float = 0.02,
    allowed_negative: float = 0.0,
) -> CheckResult:
    """Derived daily cash: never above NAV (beyond the unsettled-proceeds tolerance, capped at ``MAX_OVER_NAV_TOL``) and
    never below ``-allowed_negative`` (a declared debit bound; margin accounts declare one, and it may not exceed the
    row's own NAV)."""
    if not rows:
        return CheckResult("cash_walk", False, "NO_ROWS")
    if (
        not _finite(over_nav_tol, allowed_negative)
        or not 0 <= over_nav_tol <= MAX_OVER_NAV_TOL
        or allowed_negative < 0
    ):
        return CheckResult("cash_walk", False, "TOLERANCE_OUT_OF_RANGE")
    bad = 0
    for r in rows:
        if not _finite(r["cash"], r["nav"]):
            return CheckResult("cash_walk", False, "NON_FINITE", 1)
        cash, nav = float(r["cash"]), float(r["nav"])
        if (
            cash > nav * (1 + over_nav_tol) + 0.005
            or cash < -min(allowed_negative, max(nav, 0.0)) - 0.005
        ):
            bad += 1
    return _result("cash_walk", bad, "CASH_WALK_OUT_OF_BOUNDS")


def check_statement_cash(
    walk: Mapping[str, float], statements: Mapping[str, float], *, tol: float = 0.01
) -> CheckResult:
    """Independent check: statement cash equals the walk on the same date (``tol`` capped at ``MAX_TOL``); a statement with
    no walk row is a failure (nothing silently skipped) and at least one statement is required."""
    if not statements:
        return CheckResult("statement_cash", False, "NO_STATEMENTS")
    if not _finite(tol) or not 0 <= tol <= MAX_TOL:
        return CheckResult("statement_cash", False, "TOLERANCE_OUT_OF_RANGE")
    bad = 0
    for d, v in statements.items():
        if d not in walk or not _finite(walk[d], v) or not abs(walk[d] - v) <= tol:
            bad += 1
    return _result("statement_cash", bad, "STATEMENT_CASH_MISMATCH")


def check_bridge_receipt(
    *,
    last_nav_before: float,
    first_nav_after: float,
    receipt_legs: Sequence[float],
    receipt_total: float,
    max_implied_move: float,
) -> CheckResult:
    """A NAV hole is bridged (``GAP_WITH_BRIDGE``) only by a captured receiving-side receipt: the legs sum to the total to
    the cent, every leg is positive, the receipt is material (at least half the last NAV before the hole: a one-cent
    receipt cannot bridge anything), and the market move the bridge implies (``receipt + first NAV after - last NAV
    before``, as a fraction of the last NAV) stays inside the declared bound (capped at ``MAX_BRIDGE_MOVE``). The
    unobserved move is bounded, never claimed exact. The adapter runs this only where coverage found a hole."""
    if not receipt_legs or not _finite(
        last_nav_before, first_nav_after, receipt_total, max_implied_move, *receipt_legs
    ):
        return CheckResult(
            "bridge_receipt", False, "NO_ROWS" if not receipt_legs else "NON_FINITE"
        )
    legs_ok = all(float(x) > 0 for x in receipt_legs) and abs(
        sum((Decimal(str(x)) for x in receipt_legs), Decimal(0))
        - Decimal(str(receipt_total))
    ) < Decimal("0.005")
    implied = float(receipt_total) + float(first_nav_after) - float(last_nav_before)
    ok = (
        legs_ok
        and last_nav_before > 0
        and first_nav_after >= 0
        and receipt_total >= 0.5 * last_nav_before
        and 0 <= max_implied_move <= MAX_BRIDGE_MOVE
        and abs(implied) <= max_implied_move * float(last_nav_before)
    )
    return CheckResult(
        "bridge_receipt", ok, "OK" if ok else "BRIDGE_NOT_CLOSED", 0 if ok else 1
    )


def check_hwm_gap_bound(points: Sequence[Mapping[str, Any]]) -> CheckResult:
    """A derived point is admitted only if it cannot create a new high: the margin below the prior high must be positive and
    exceed the largest observed closure error. Both numbers come from the derivation that produced the point (its own
    closure receipt); a self-reported ``cannot_create_new_high`` flag is ignored."""
    if not points:
        return CheckResult("hwm_gap_bound", False, "NO_ROWS")
    bad = 0
    for p in points:
        if not _finite(p["margin_below_prior_high"], p["max_observed_closure_error"]):
            return CheckResult("hwm_gap_bound", False, "NON_FINITE", 1)
        m, e = (
            float(p["margin_below_prior_high"]),
            float(p["max_observed_closure_error"]),
        )
        if not (m > 0 and e >= 0 and m > e):
            bad += 1
    return _result("hwm_gap_bound", bad, "GAP_CAN_HIDE_PEAK")


def check_stability(first_digest: str, second_digest: str) -> CheckResult:
    """Second pull of one page byte-equal after normalisation: both digests are exactly 64 lower-case hex and equal."""
    ok = (
        isinstance(first_digest, str)
        and HEX64_RE.fullmatch(first_digest) is not None
        and first_digest == second_digest
    )
    return CheckResult("stability", ok, "OK" if ok else "PAGE_UNSTABLE", 0 if ok else 1)


def not_applicable(name: str) -> CheckResult:
    """For a check whose precondition is absent (no NAV hole to bridge, no derived HWM point): passes, and says why."""
    return CheckResult(name, True, "NOT_APPLICABLE")


def check_reader_parses(parse: Callable[[], object]) -> CheckResult:
    """The producer/consumer seam: the artifact is accepted only if the REAL reader's own parser accepts it (``parse`` calls it).
    Any exception is a failure with a stable code; the message is never carried."""
    try:
        parse()
    except Exception:  # noqa: BLE001 - the reader's refusal is the verdict, whatever its type
        return CheckResult("reader_parses", False, "READER_REFUSES", 1)
    return CheckResult("reader_parses", True, "OK")


CLOSURE_CHECKS: dict[str, Callable[..., CheckResult]] = {
    "nav_identity": check_nav_identity,
    "coverage": check_coverage,
    "expected_dates": check_expected_dates,
    "sum_equals_total": check_sum_equals_total,
    "cash_walk": check_cash_walk,
    "statement_cash": check_statement_cash,
    "bridge_receipt": check_bridge_receipt,
    "hwm_gap_bound": check_hwm_gap_bound,
    "stability": check_stability,
    "reader_parses": check_reader_parses,
}

# ----------------------------------------------------------------------------- S3 verify


@dataclass(frozen=True)
class Verdict:
    source_id: str
    status: str  # PASS | FAIL
    codes: tuple[str, ...]
    results: tuple[CheckResult, ...] = field(default=())
    manifest_sha256: str = ""
    as_of: str = ""
    cutoff: str = ""
    token: str = ""

    def issued_fields(self) -> tuple[Any, ...]:
        return (
            self.source_id,
            self.status,
            self.codes,
            self.manifest_sha256,
            self.as_of,
            self.cutoff,
        )


_ISSUED: dict[
    str, tuple[Any, ...]
] = {}  # token -> the fields verify() issued; admission compares every field


def freshness_code(source: Source, *, as_of: str, cutoff: str) -> str:
    """Freshness is against the release cutoff (not the wall clock): as-of must not be after the cutoff
    (``POST_CUTOFF``) and no older than ``max_age_days`` before it (``STALE_INPUT``)."""
    a, c = _iso(as_of, "BAD_AS_OF"), _iso(cutoff, "BAD_CUTOFF")
    if a > c:
        return "POST_CUTOFF"
    if (c - a).days > source.max_age_days:
        return "STALE_INPUT"
    return "OK"


def verify(
    source: Source,
    *,
    manifest: Mapping[str, Any],
    as_of: str,
    cutoff: str,
    checks: Mapping[str, Callable[[], CheckResult]],
) -> Verdict:
    """S3. Runs the freshness rule and EVERY declared closure check (no first-failure masking). A declared check with no
    adapter is ``CHECK_NOT_RUN``; an adapter that raises is ``CHECK_ERROR`` and one that returns the wrong thing (wrong
    type or name, or ok/code disagreement) is ``CHECK_BAD_RESULT``; an undeclared adapter is refused; a source with no
    declared checks or a manifest with no files is refused; ``as_of`` may not be after the manifest's capture date.
    The verdict is registered under a private token with the exact fields issued: admission accepts only a verdict whose
    every field still equals the registered one (``dataclasses.replace`` or a hand-built copy does not)."""
    if manifest.get("source_id") != source.source_id:
        raise CaptureRefused("VERIFY_SOURCE_MISMATCH", source.source_id)
    if not source.closure_checks:
        raise CaptureRefused("VERIFY_NO_CHECKS", source.source_id)
    if not isinstance(manifest.get("files"), Mapping) or not manifest["files"]:
        raise CaptureRefused("VERIFY_EMPTY_MANIFEST", source.source_id)
    extra = [n for n in checks if n not in source.closure_checks]
    if extra:
        raise CaptureRefused(
            "UNDECLARED_CHECK", f"{source.source_id}:{','.join(sorted(extra))}"
        )
    codes: list[str] = []
    results: list[CheckResult] = []
    fc = freshness_code(source, as_of=as_of, cutoff=cutoff)
    if fc != "OK":
        codes.append(fc)
    if _iso(as_of, "BAD_AS_OF") > _iso(manifest.get("captured_on"), "BAD_CAPTURED_ON"):
        codes.append("AS_OF_AFTER_CAPTURE")
    for name in source.closure_checks:
        if name not in checks:
            codes.append(f"{name}:CHECK_NOT_RUN")
            results.append(CheckResult(name, False, "CHECK_NOT_RUN"))
            continue
        try:
            res = checks[name]()
        except Exception:  # noqa: BLE001 - one broken adapter must not mask the other checks
            codes.append(f"{name}:CHECK_ERROR")
            results.append(CheckResult(name, False, "CHECK_ERROR"))
            continue
        if not (
            isinstance(res, CheckResult)
            and res.check == name
            and res.ok == (res.code in ("OK", "NOT_APPLICABLE"))
        ):
            codes.append(f"{name}:CHECK_BAD_RESULT")
            results.append(CheckResult(name, False, "CHECK_BAD_RESULT"))
            continue
        results.append(res)
        if not res.ok:
            codes.append(f"{name}:{res.code}")
    token = uuid.uuid4().hex
    verdict = Verdict(
        source.source_id,
        "PASS" if not codes else "FAIL",
        tuple(codes),
        tuple(results),
        manifest_digest(manifest),
        as_of,
        cutoff,
        token,
    )
    _ISSUED[token] = verdict.issued_fields()
    return verdict


# ----------------------------------------------------------------------------- S4 admit / S5 consume


class _PinLock:
    """Exclusive lock file next to the pin file (create-or-wait). It records the holder's pid and time; a lock older than
    ``LOCK_STALE_SECONDS`` (a crashed holder) is broken, so one crash cannot wedge every later admission."""

    def __init__(self, pin_path: Path) -> None:
        self.path = Path(str(pin_path) + ".lock")

    def __enter__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(1200):
            try:
                with self.path.open("x") as fh:
                    fh.write(f"{os.getpid()} {time.time()}")
                return
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                time.sleep(0.05)
        raise CaptureRefused("PIN_LOCK_TIMEOUT")

    def __exit__(self, *_: object) -> None:
        self.path.unlink(missing_ok=True)


def _read_pins(pin_path: Path) -> dict[str, dict[str, Any]]:
    p = Path(pin_path)
    if not p.exists():
        return {}
    doc = json.loads(p.read_text(encoding="utf-8"))
    if (
        not isinstance(doc, dict)
        or doc.get("schema_version") != 2
        or not isinstance(doc.get("pins"), dict)
    ):
        raise CaptureRefused("PIN_SCHEMA")  # never treated as empty, never overwritten
    return doc["pins"]


def admit_input(
    source: Source,
    verdict: Verdict,
    *,
    run_dir: Path,
    manifest_path: Path,
    evidence_root: Path,
    pin_path: Path,
) -> dict[str, Any]:
    """S4. Only a PASS verdict issued by :func:`verify` and unchanged since (every field equals the registered one) for
    this exact manifest is admitted; as-of and cutoff come from the verdict, never from the caller. Every hash is checked
    before anything is copied; files are copied into a staging directory, re-hashed, and renamed into
    ``<evidence_root>/<source_id>/<cutoff>`` (create-only). Under the pin lock an unpinned leftover directory (a crash
    between rename and pin) is removed first; the pin (keyed by source and cutoff, relative root, manifest digest) is
    written atomically, and the copy is rolled back if that write fails."""
    if (
        _ISSUED.get(verdict.token) != verdict.issued_fields()
        or verdict.status != "PASS"
    ):
        raise CaptureRefused("ADMIT_NOT_PASS", source.source_id)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if (
        verdict.source_id != source.source_id
        or manifest.get("source_id") != source.source_id
    ):
        raise CaptureRefused("ADMIT_SOURCE_MISMATCH", source.source_id)
    if manifest_digest(manifest) != verdict.manifest_sha256:
        raise CaptureRefused("ADMIT_VERDICT_MISMATCH", source.source_id)
    _iso(verdict.cutoff, "BAD_CUTOFF")
    run, ev = Path(run_dir).resolve(), Path(evidence_root).resolve()
    jobs: list[tuple[Path, str, str]] = []
    for rel, meta in manifest["files"].items():
        _safe_rel(rel)
        src = (run / rel).resolve()
        if (
            not src.is_relative_to(run)
            or src.is_symlink()
            or not src.is_file()
            or sha256_file(src) != meta["sha256"]
        ):
            raise CaptureRefused("ADMIT_HASH_MISMATCH", f"{source.source_id}:{rel}")
        jobs.append((src, rel, meta["sha256"]))
    rel_root = f"{source.source_id}/{verdict.cutoff}"
    dest = ev / rel_root
    staging = ev / f".staging-{uuid.uuid4().hex}"
    entry = {
        "as_of": verdict.as_of,
        "cutoff": verdict.cutoff,
        "root": rel_root,
        "manifest_sha256": verdict.manifest_sha256,
        "files": {rel: digest for _, rel, digest in jobs},
        "owner_declared": source.owner_declared_ok,
    }
    with _PinLock(Path(pin_path)):
        pins = _read_pins(pin_path)
        by_cutoff = pins.setdefault(source.source_id, {})
        if verdict.cutoff in by_cutoff:
            raise CaptureRefused("CREATE_ONLY_EXISTS", f"pin {rel_root}")
        if dest.exists():
            shutil.rmtree(
                dest
            )  # unpinned leftover of a crash between rename and pin: garbage, not evidence
        try:
            for src, rel, digest in jobs:
                tgt = staging / rel
                tgt.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, tgt)
                if sha256_file(tgt) != digest:
                    raise CaptureRefused(
                        "ADMIT_COPY_MISMATCH", f"{source.source_id}:{rel}"
                    )
            shutil.copyfile(manifest_path, staging / MANIFEST_NAME)
            dest.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(dest)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        try:
            by_cutoff[verdict.cutoff] = entry
            tmp = Path(pin_path).with_name(
                f".{Path(pin_path).name}.{uuid.uuid4().hex}.tmp"
            )
            payload = (
                json.dumps(
                    {"schema_version": 2, "pins": pins}, indent=1, sort_keys=True
                )
                + "\n"
            )
            tmp.write_text(payload, encoding="utf-8", newline="\n")
            tmp.replace(pin_path)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
    return entry


def _entry_for(
    pins: Mapping[str, Any], source_id: str, cutoff: str
) -> Mapping[str, Any] | None:
    """The admitted entry for the requested cutoff: the entry admitted for exactly that cutoff when there is one, otherwise
    the latest-as-of entry admitted for an EARLIER cutoff whose as-of is on or before this one (a later cycle's capture
    can never replace the capture an older cycle was run on); None when there is none."""
    c = _iso(cutoff, "BAD_CUTOFF")
    by_cutoff = pins.get(source_id, {})
    exact = by_cutoff.get(cutoff)
    if exact is not None and _iso(exact["as_of"], "BAD_AS_OF") <= c:
        return exact  # type: ignore[no-any-return]
    cands = [
        e
        for e in by_cutoff.values()
        if _iso(e["cutoff"], "BAD_CUTOFF") < c and _iso(e["as_of"], "BAD_AS_OF") <= c
    ]
    return max(cands, key=lambda e: _iso(e["as_of"], "BAD_AS_OF"), default=None)  # type: ignore[no-any-return]


def _resolve(
    source_id: str,
    name: str,
    *,
    pin_path: Path,
    evidence_root: Path,
    cutoff: str,
    sources: Mapping[str, Source],
    reader: str,
    manifest_sha256: str | None = None,
) -> tuple[Path, str]:
    src = sources.get(source_id)
    if src is None:
        raise CaptureRefused("UNCAPTURED_INPUT", f"{source_id}/{name}")
    pins = _read_pins(pin_path)
    entry = _entry_for(pins, source_id, cutoff)
    if entry is None:
        raise CaptureRefused(
            "POST_CUTOFF" if pins.get(source_id) else "UNCAPTURED_INPUT",
            f"{source_id}/{name}",
        )
    if name not in entry["files"]:
        raise CaptureRefused("UNCAPTURED_INPUT", f"{source_id}/{name}")
    if reader not in src.admits:
        raise CaptureRefused("READER_NOT_ADMITTED", f"{source_id}:{reader}")
    fc = freshness_code(src, as_of=entry["as_of"], cutoff=cutoff)
    if fc != "OK":
        raise CaptureRefused(fc, source_id)
    if manifest_sha256 is not None and entry["manifest_sha256"] != manifest_sha256:
        raise CaptureRefused("MANIFEST_MISMATCH", source_id)
    ev = Path(evidence_root).resolve()
    root = (ev / entry["root"]).resolve()
    manifest_file = root / MANIFEST_NAME
    if not root.is_relative_to(ev) or not manifest_file.is_file():
        raise CaptureRefused("HASH_MISMATCH", f"{source_id}/manifest")
    try:
        on_disk = json.loads(manifest_file.read_text(encoding="utf-8"))
        listed = on_disk["files"][name]["sha256"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CaptureRefused("HASH_MISMATCH", f"{source_id}/manifest") from exc
    if (
        manifest_digest(on_disk) != entry["manifest_sha256"]
        or listed != entry["files"][name]
    ):
        raise CaptureRefused(
            "HASH_MISMATCH", f"{source_id}/manifest"
        )  # a hand-built pin has no admitted manifest
    path = (root / _safe_rel(name)).resolve()
    if (
        not path.is_relative_to(ev)
        or path.is_symlink()
        or not path.is_file()
        or sha256_file(path) != entry["files"][name]
    ):
        raise CaptureRefused("HASH_MISMATCH", f"{source_id}/{name}")
    return path, str(entry["files"][name])


def captured_input(
    source_id: str,
    name: str,
    *,
    pin_path: Path,
    evidence_root: Path,
    cutoff: str,
    sources: Mapping[str, Source],
    reader: str,
    manifest_sha256: str | None = None,
) -> Path:
    """S5. A reader opens a captured file only here: the file must be pinned (``UNCAPTURED_INPUT``), the reader admitted
    by the source (``READER_NOT_ADMITTED``), fresh against the cutoff (``STALE_INPUT`` / ``POST_CUTOFF``), listed in the
    admitted ``capture-manifest.json`` whose digest the pin records (a hand-built pin has none) and unchanged since
    admission (``HASH_MISMATCH``). ``manifest_sha256`` lets a spec pin the exact capture it was planned against.
    Prefer :func:`captured_bytes` where the content, not the path, is used."""
    return _resolve(
        source_id, name, pin_path=pin_path, evidence_root=evidence_root, cutoff=cutoff, sources=sources,
        reader=reader, manifest_sha256=manifest_sha256,
    )[0]  # fmt: skip


def captured_bytes(
    source_id: str,
    name: str,
    *,
    pin_path: Path,
    evidence_root: Path,
    cutoff: str,
    sources: Mapping[str, Source],
    reader: str,
) -> bytes:
    """Same refusals as :func:`captured_input`, but returns the verified bytes (no time-of-check gap)."""
    path, digest = _resolve(source_id, name, pin_path=pin_path, evidence_root=evidence_root, cutoff=cutoff, sources=sources, reader=reader)  # fmt: skip
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise CaptureRefused("HASH_MISMATCH", f"{source_id}/{name}")
    return data


# ----------------------------------------------------------------------------- refresh plan


def needs_refresh(
    source: Source, *, pins: Mapping[str, Any], evidence_root: Path | None, cutoff: str
) -> str:
    """Why a source must be (re)captured, or ``FRESH``: ``NEVER_CAPTURED``, ``STALE_INPUT``, ``POST_CUTOFF`` or
    ``PIN_TAMPERED`` (a pinned file or the admitted manifest is missing or edited: bytes are re-hashed, not trusted)."""
    entry = _entry_for(pins, source.source_id, cutoff)
    if entry is None:
        return "POST_CUTOFF" if pins.get(source.source_id) else "NEVER_CAPTURED"
    fc = freshness_code(source, as_of=entry["as_of"], cutoff=cutoff)
    if fc != "OK":
        return fc
    if evidence_root is not None:
        root = Path(evidence_root) / entry["root"]
        mf = root / MANIFEST_NAME
        try:
            ok = (
                mf.is_file()
                and manifest_digest(json.loads(mf.read_text(encoding="utf-8")))
                == entry["manifest_sha256"]
            )
        except (OSError, ValueError):
            ok = False
        if not ok:
            return "PIN_TAMPERED"
        for name, digest in entry["files"].items():
            p = root / name
            if not p.is_file() or sha256_file(p) != digest:
                return "PIN_TAMPERED"
    return "FRESH"


def refresh_table(
    sources: Mapping[str, Source],
    *,
    pins: Mapping[str, Any],
    cutoff: str,
    window_state: Callable[[Source], str],
    vault_ok: Callable[[str], bool],
    http_ok: Callable[[Source], bool] = lambda _s: False,
    store_ok: Callable[[str], bool] | None = None,
    evidence_root: Path | None = None,
) -> list[dict[str, str]]:
    """The weekly refresh's first step: S0 for every source that needs a refresh, all at once, so every needed login is
    requested in one message. Rows: source, reason, status, owner_step, as_of (the age is visible)."""
    rows: list[dict[str, str]] = []
    for sid in sorted(sources):
        s = sources[sid]
        why = needs_refresh(s, pins=pins, evidence_root=evidence_root, cutoff=cutoff)
        entry = _entry_for(pins, sid, cutoff)
        as_of = str(entry["as_of"]) if entry else ""
        if why == "FRESH":
            rows.append(
                {
                    "source": sid,
                    "reason": why,
                    "status": "SKIP_FRESH",
                    "owner_step": "",
                    "as_of": as_of,
                }
            )
            continue
        pf = preflight(
            s,
            window_state=window_state,
            vault_ok=vault_ok,
            http_ok=http_ok,
            store_ok=store_ok,
        )
        rows.append(
            {
                "source": sid,
                "reason": why,
                "status": pf.status,
                "owner_step": pf.owner_step,
                "as_of": as_of,
            }
        )
    return rows


# ----------------------------------------------------------------------------- reader switch-over


def file_pin(path: Path) -> dict[str, Any]:
    """The preparation-spec file pin shape (``path``, ``sha256``, ``size_bytes``) of one file."""
    p = Path(path)
    return {"path": str(p), "sha256": sha256_file(p), "size_bytes": p.stat().st_size}


def verify_file_pin(pin: object) -> Path:
    if not isinstance(pin, Mapping) or set(pin) != {"path", "sha256", "size_bytes"}:
        raise CaptureRefused("CAPTURED_INPUTS_SHAPE")
    p = Path(str(pin["path"])).resolve()
    if (
        p.is_symlink()
        or not p.is_file()
        or sha256_file(p) != pin["sha256"]
        or p.stat().st_size != pin["size_bytes"]
    ):
        raise CaptureRefused("HASH_MISMATCH", "pin file")
    return p


def resolve_captured_inputs(
    config: Mapping[str, Any],
    *,
    cutoff: str,
    sources: Mapping[str, Source],
    readers: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """Switch a preparation spec's readers to captured, admitted inputs.

    A spec with no ``captured_inputs`` key is returned unchanged (legacy specs keep working); ``null`` is a shape error.
    With the block ``{"pins": <pin file path>, "evidence_root": <dir>, "bindings": {config_key: {"source_id", "name",
    "reader", "manifest_sha256"}}}`` each bound key is replaced by the file pin of the admitted, fresh, hash-verified
    captured file (:func:`captured_input`), which must belong to the exact capture the spec was planned against
    (``manifest_sha256``, recorded at admission and re-hashed against the admitted manifest on disk), so admitting other
    sources later does not move the spec. A plain pin supplied for any config key that the contract assigns to a reader
    (``readers[...]["config_key"]``) is ``UNCAPTURED_INPUT``: no hand-built path can stand in for a captured one."""
    if "captured_inputs" not in config:
        return dict(config)
    block = config["captured_inputs"]
    if (
        not isinstance(block, Mapping)
        or set(block) != {"pins", "evidence_root", "bindings"}
        or not isinstance(block["bindings"], Mapping)
        or not isinstance(block["pins"], str)
        or not isinstance(block["evidence_root"], str)
    ):
        raise CaptureRefused("CAPTURED_INPUTS_SHAPE")
    out = {k: v for k, v in config.items() if k != "captured_inputs"}
    governed = {r["config_key"] for r in readers.values() if "config_key" in r}
    for key in sorted(governed & set(config)):
        raise CaptureRefused("UNCAPTURED_INPUT", key)
    for key, b in block["bindings"].items():
        if not isinstance(b, Mapping) or set(b) != {
            "source_id",
            "name",
            "reader",
            "manifest_sha256",
        }:
            raise CaptureRefused("CAPTURED_INPUTS_SHAPE", str(key))
        if not all(isinstance(b[k], str) for k in b) or not HEX64_RE.fullmatch(
            b["manifest_sha256"]
        ):
            raise CaptureRefused("CAPTURED_INPUTS_SHAPE", str(key))
        rd = readers.get(b["reader"])
        if rd is None or rd.get("config_key") != key:
            raise CaptureRefused("CAPTURED_INPUTS_SHAPE", str(key))
        out[key] = file_pin(
            captured_input(
                b["source_id"], b["name"], pin_path=Path(block["pins"]), evidence_root=Path(block["evidence_root"]),
                cutoff=cutoff, sources=sources, reader=b["reader"], manifest_sha256=b["manifest_sha256"],
            )
        )  # fmt: skip
    return out
