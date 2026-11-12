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
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any
import uuid

TRANSPORTS = (
    "BROWSER_SIGNED_IN",
    "API_VAULT",
    "FLEX_TOKEN_VAULT",
    "PUBLIC_HTTP",
    "CREDENTIAL_MANAGER",
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
    "source_id", "cdp_port", "window_hosts", "probe_url", "source_kind", "owner_evidence",
}  # fmt: skip
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
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
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CaptureRefused(code) from exc


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

    @property
    def stage_mode(self) -> str:
        return str(self.capture_stage.get("mode"))

    @property
    def stage_tool(self) -> str | None:
        tool = self.capture_stage.get("tool")
        return str(tool) if tool else None


def closure_check_names() -> tuple[str, ...]:
    return tuple(sorted(CLOSURE_CHECKS))


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
                or r["symbol"] not in f.read_text(encoding="utf-8")
            ):
                raise CaptureRefused("CONTRACT_READER_MISSING", name)
    return {k: dict(v) for k, v in readers.items()}


def load_sources(path: Path, *, repo_root: Path | None = None) -> dict[str, Source]:
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
        if not isinstance(sid, str) or not ID_RE.match(sid):
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
            if not isinstance(ref, str) or not SECRET_NAME_RE.match(ref):
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
                or not all(isinstance(h, str) and HOST_RE.match(h) for h in hosts)
            ):
                raise CaptureRefused("CONTRACT_WINDOW_HOSTS", sid)
        elif port is not None or hosts:
            raise CaptureRefused("CONTRACT_CDP_PORT", sid)
        probe = raw.get("probe_url")
        stage = raw["capture_stage"]
        if not isinstance(stage, dict) or stage.get("mode") not in STAGE_MODES:
            raise CaptureRefused("CONTRACT_STAGE_MODE", sid)
        needs_probe = (
            transport == "PUBLIC_HTTP" and stage["mode"] != "NEW_STAGE_REQUIRED"
        )
        if needs_probe != (probe is not None) or (probe is not None and not (isinstance(probe, str) and probe.startswith("https://"))):  # fmt: skip
            raise CaptureRefused("CONTRACT_PROBE_URL", sid)
        tool = stage.get("tool")
        if stage["mode"] != "NEW_STAGE_REQUIRED" and not tool:
            raise CaptureRefused("CONTRACT_STAGE_TOOL", sid)
        if tool and repo_root is not None and not (Path(repo_root) / tool).exists():
            raise CaptureRefused("CONTRACT_STAGE_TOOL_MISSING", sid)
        fresh = raw["freshness"]
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
        owner_ok, evidence = raw["owner_declared_ok"], raw.get("owner_evidence")
        if not isinstance(owner_ok, bool):
            raise CaptureRefused("CONTRACT_OWNER_DECLARED", sid)
        if owner_ok and not (isinstance(evidence, dict) and HEX64_RE.match(str(evidence.get("sha256"))) and _iso(evidence.get("owner_line_date"), "CONTRACT_OWNER_DECLARED")):  # fmt: skip
            raise CaptureRefused("CONTRACT_OWNER_DECLARED", sid)
        out[sid] = Source(
            sid, raw["provider"], transport, ref, stage, browser, age, tuple(checks), tuple(admits), owner_ok,
            port, tuple(hosts), probe, evidence if owner_ok else None,
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
            return Preflight(
                sid, "UNVERIFIABLE_BY_DESIGN",
                f"credential-manager entry '{source.credential_ref}' (or the owner's {source.provider} sign-in) is checked "
                "by the provider package at S1; if S1 refuses, the owner stores the credential or signs in",
            )  # fmt: skip
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
    p = Path(name)
    if (
        not name
        or p.is_absolute()
        or name.startswith(("/", "\\"))
        or ".." in p.parts
        or "\\" in name
        or p.drive
    ):
        raise CaptureRefused("UNSAFE_PATH")
    return p


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
    """Broker NAV: opening + sum(components) = ending each day and, when ``chain``, each opening = prior ending.
    ``chain=False`` is for sources with published restatements at period boundaries (reported separately)."""
    if not rows:
        return CheckResult("nav_identity", False, "NO_ROWS")
    bad, prev_end = 0, None
    for r in rows:
        comps = list(r["components"])
        if not _finite(r["opening"], r["ending"], *comps):
            return CheckResult("nav_identity", False, "NON_FINITE", 1)
        opening, ending = float(r["opening"]), float(r["ending"])
        if not abs(opening + sum(float(c) for c in comps) - ending) <= tol:
            bad += 1
        if chain and prev_end is not None and not abs(opening - prev_end) <= tol:
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
    """Every expected weekday in [start, end] has a row (``non_trading`` days are not expected and do not hide
    neighbouring gaps); at most ``max_missing`` may be absent in total; rows outside the window are refused."""
    d0, d1 = _iso(start, "BAD_WINDOW"), _iso(end, "BAD_WINDOW")
    if d0 > d1:
        return CheckResult("coverage", False, "BAD_WINDOW")
    have = {_iso(d, "BAD_DATE") for d in dates}
    skip = {_iso(d, "BAD_DATE") for d in non_trading}
    expected = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    expected = [d for d in expected if d.weekday() < 5 and d not in skip]
    if not expected:
        return CheckResult("coverage", False, "NO_EXPECTED_DAYS")
    outside = sum(1 for d in have if d < d0 or d > d1)
    if outside:
        return CheckResult("coverage", False, "OUT_OF_WINDOW", outside)
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
    parts: Sequence[float | Decimal | str], total: float | Decimal | str
) -> CheckResult:
    """Live page or statement total equals the sum of its parts, to the cent (Decimal arithmetic)."""
    if not parts or not _finite(total, *parts):
        return CheckResult(
            "sum_equals_total", False, "NO_ROWS" if not parts else "NON_FINITE"
        )
    diff = abs(sum((Decimal(str(p)) for p in parts), Decimal(0)) - Decimal(str(total)))
    return _result(
        "sum_equals_total", 0 if diff < Decimal("0.005") else 1, "TOTAL_MISMATCH"
    )


def check_cash_walk(
    rows: Sequence[Mapping[str, Any]],
    *,
    over_nav_tol: float = 0.02,
    allowed_negative: float = 0.0,
) -> CheckResult:
    """Derived daily cash: never above NAV (beyond the unsettled-proceeds tolerance) and never below
    ``-allowed_negative`` (a declared debit bound; margin accounts declare one)."""
    if not rows:
        return CheckResult("cash_walk", False, "NO_ROWS")
    if (
        not _finite(over_nav_tol, allowed_negative)
        or over_nav_tol < 0
        or allowed_negative < 0
    ):
        return CheckResult("cash_walk", False, "NON_FINITE")
    bad = 0
    for r in rows:
        if not _finite(r["cash"], r["nav"]):
            return CheckResult("cash_walk", False, "NON_FINITE", 1)
        cash, nav = float(r["cash"]), float(r["nav"])
        if cash > nav * (1 + over_nav_tol) + 0.005 or cash < -allowed_negative - 0.005:
            bad += 1
    return _result("cash_walk", bad, "CASH_WALK_OUT_OF_BOUNDS")


def check_statement_cash(
    walk: Mapping[str, float], statements: Mapping[str, float], *, tol: float = 0.01
) -> CheckResult:
    """Independent check: statement cash equals the walk on the same date; a statement with no walk row is a failure
    (nothing silently skipped) and at least one statement is required."""
    if not statements:
        return CheckResult("statement_cash", False, "NO_STATEMENTS")
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
    """A NAV hole is bridged (``GAP_WITH_BRIDGE``) only by a captured receiving-side receipt: the receipt legs must sum
    to the receipt total to the cent (cash-exact transfers), every leg is positive, and the market move the bridge
    implies (``receipt + first NAV after - last NAV before``, as a fraction of the last NAV) must stay inside the
    declared bound (itself capped at ``MAX_BRIDGE_MOVE``). The unobserved move is bounded, never claimed exact."""
    if not receipt_legs or not _finite(
        last_nav_before, first_nav_after, receipt_total, max_implied_move, *receipt_legs
    ):
        return CheckResult(
            "bridge_receipt", False, "NO_ROWS" if not receipt_legs else "NON_FINITE"
        )
    legs_ok = all(float(x) > 0 for x in receipt_legs) and abs(sum(Decimal(str(x)) for x in receipt_legs) - Decimal(str(receipt_total))) < Decimal("0.005")  # fmt: skip
    implied = float(receipt_total) + float(first_nav_after) - float(last_nav_before)
    ok = (
        legs_ok and last_nav_before > 0 and first_nav_after >= 0 and 0 <= max_implied_move <= MAX_BRIDGE_MOVE
        and abs(implied) <= max_implied_move * float(last_nav_before)
    )  # fmt: skip
    return CheckResult(
        "bridge_receipt", ok, "OK" if ok else "BRIDGE_NOT_CLOSED", 0 if ok else 1
    )


def check_hwm_gap_bound(points: Sequence[Mapping[str, Any]]) -> CheckResult:
    """A derived point is admitted only if it cannot create a new high: margin below the prior high must be positive and
    exceed the largest observed closure error (computed here, never taken from a self-reported flag)."""
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
    """Second pull of one page byte-equal after normalisation: both digests are 64-hex and equal."""
    ok = bool(HEX64_RE.match(str(first_digest))) and first_digest == second_digest
    return CheckResult("stability", ok, "OK" if ok else "PAGE_UNSTABLE", 0 if ok else 1)


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
}

# ----------------------------------------------------------------------------- S3 verify

_VERDICT_KEY = object()  # only verify() can build a Verdict that admit_input accepts


@dataclass(frozen=True)
class Verdict:
    source_id: str
    status: str  # PASS | FAIL
    codes: tuple[str, ...]
    results: tuple[CheckResult, ...] = field(default=())
    manifest_sha256: str = ""
    as_of: str = ""
    cutoff: str = ""
    _key: object = field(default=None, repr=False, compare=False)


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
    """S3. Runs the freshness rule and EVERY declared closure check (no first-failure masking). A declared check with
    no adapter is ``CHECK_NOT_RUN``; an adapter that raises is ``CHECK_ERROR`` and one that returns the wrong thing
    (wrong type, name, or ok/code disagreement) is ``CHECK_BAD_RESULT``; an undeclared adapter is refused. The verdict
    carries the manifest digest, as-of and cutoff it verified: admission accepts nothing else."""
    if manifest.get("source_id") != source.source_id:
        raise CaptureRefused("VERIFY_SOURCE_MISMATCH", source.source_id)
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
            and res.ok == (res.code == "OK")
        ):
            codes.append(f"{name}:CHECK_BAD_RESULT")
            results.append(CheckResult(name, False, "CHECK_BAD_RESULT"))
            continue
        results.append(res)
        if not res.ok:
            codes.append(f"{name}:{res.code}")
    return Verdict(
        source.source_id, "PASS" if not codes else "FAIL", tuple(codes), tuple(results),
        manifest_digest(manifest), as_of, cutoff, _VERDICT_KEY,
    )  # fmt: skip


# ----------------------------------------------------------------------------- S4 admit / S5 consume


class _PinLock:
    """Exclusive lock file next to the pin file (create-or-wait), so concurrent admissions cannot lose a pin."""

    def __init__(self, pin_path: Path) -> None:
        self.path = Path(str(pin_path) + ".lock")

    def __enter__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(600):
            try:
                self.path.open("x").close()
                return
            except FileExistsError:
                time.sleep(0.05)
        raise CaptureRefused("PIN_LOCK_TIMEOUT")

    def __exit__(self, *_: object) -> None:
        self.path.unlink(missing_ok=True)


def _read_pins(pin_path: Path) -> dict[str, dict[str, Any]]:
    p = Path(pin_path)
    if not p.exists():
        return {}
    doc = json.loads(p.read_text(encoding="utf-8"))
    return doc.get("pins", {}) if doc.get("schema_version") == 2 else {}


def admit_input(
    source: Source,
    verdict: Verdict,
    *,
    run_dir: Path,
    manifest_path: Path,
    evidence_root: Path,
    pin_path: Path,
) -> dict[str, Any]:
    """S4. Only a PASS verdict issued by :func:`verify` for this exact manifest is admitted; as-of and cutoff come from
    the verdict, never from the caller. Every hash is checked before anything is copied; files are copied into a staging
    directory, re-hashed, and renamed into ``<evidence_root>/<source_id>/<cutoff>`` (create-only); the pin (keyed by
    source and cutoff, relative root, manifest digest) is written under a lock, and the copy is rolled back if that
    write fails. Manifest names are relative, without ``..``, regular files inside the run directory."""
    if verdict._key is not _VERDICT_KEY or verdict.status != "PASS":
        raise CaptureRefused("ADMIT_NOT_PASS", source.source_id)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if (
        verdict.source_id != source.source_id
        or manifest.get("source_id") != source.source_id
    ):
        raise CaptureRefused("ADMIT_SOURCE_MISMATCH", source.source_id)
    if manifest_digest(manifest) != verdict.manifest_sha256:
        raise CaptureRefused("ADMIT_VERDICT_MISMATCH", source.source_id)
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
    if dest.exists():
        raise CaptureRefused("CREATE_ONLY_EXISTS", rel_root)
    staging = ev / f".staging-{uuid.uuid4().hex}"
    try:
        for src, rel, digest in jobs:
            tgt = staging / rel
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, tgt)
            if sha256_file(tgt) != digest:
                raise CaptureRefused("ADMIT_COPY_MISMATCH", f"{source.source_id}:{rel}")
        shutil.copyfile(manifest_path, staging / "capture-manifest.json")
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    entry = {
        "as_of": verdict.as_of, "cutoff": verdict.cutoff, "root": rel_root, "manifest_sha256": verdict.manifest_sha256,
        "files": {rel: digest for _, rel, digest in jobs}, "owner_declared": source.owner_declared_ok,
    }  # fmt: skip
    try:
        with _PinLock(Path(pin_path)):
            pins = _read_pins(pin_path)
            by_cutoff = pins.setdefault(source.source_id, {})
            if verdict.cutoff in by_cutoff:
                raise CaptureRefused("CREATE_ONLY_EXISTS", f"pin {rel_root}")
            by_cutoff[verdict.cutoff] = entry
            tmp = Path(pin_path).with_name(
                f".{Path(pin_path).name}.{uuid.uuid4().hex}.tmp"
            )
            tmp.write_text(json.dumps({"schema_version": 2, "pins": pins}, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")  # fmt: skip
            tmp.replace(pin_path)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return entry


def _entry_for(
    pins: Mapping[str, Any], source_id: str, cutoff: str
) -> Mapping[str, Any] | None:
    """The admitted entry with the latest as-of on or before the requested cutoff (re-runs of an older cycle keep
    resolving to the capture that was current then); None when there is none."""
    c = _iso(cutoff, "BAD_CUTOFF")
    cands = [
        e
        for e in pins.get(source_id, {}).values()
        if date.fromisoformat(e["as_of"]) <= c
    ]
    return max(cands, key=lambda e: e["as_of"], default=None)


def _resolve(
    source_id: str,
    name: str,
    *,
    pin_path: Path,
    evidence_root: Path,
    cutoff: str,
    sources: Mapping[str, Source],
    reader: str,
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
    ev = Path(evidence_root).resolve()
    path = (ev / entry["root"] / _safe_rel(name)).resolve()
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
) -> Path:
    """S5. A reader opens a captured file only here: the file must be pinned (``UNCAPTURED_INPUT``), the reader admitted
    by the source (``READER_NOT_ADMITTED``), fresh against the cutoff (``STALE_INPUT`` / ``POST_CUTOFF``) and unchanged
    since admission (``HASH_MISMATCH``). Prefer :func:`captured_bytes` where the content, not the path, is used."""
    return _resolve(
        source_id,
        name,
        pin_path=pin_path,
        evidence_root=evidence_root,
        cutoff=cutoff,
        sources=sources,
        reader=reader,
    )[0]


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
    ``PIN_TAMPERED`` (a pinned file is missing or edited: the pin's bytes are re-hashed, not trusted)."""
    entry = _entry_for(pins, source.source_id, cutoff)
    if entry is None:
        return "POST_CUTOFF" if pins.get(source.source_id) else "NEVER_CAPTURED"
    fc = freshness_code(source, as_of=entry["as_of"], cutoff=cutoff)
    if fc != "OK":
        return fc
    if evidence_root is not None:
        for name, digest in entry["files"].items():
            p = Path(evidence_root) / entry["root"] / name
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
        pf = preflight(s, window_state=window_state, vault_ok=vault_ok, http_ok=http_ok)
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

    A spec with no ``captured_inputs`` block is returned unchanged (legacy specs keep working). With the block
    ``{"pins": <file pin of the pin file>, "evidence_root": <dir>, "bindings": {config_key: {"source_id", "name",
    "reader"}}}`` the pin file itself is verified against its hash, each bound key is replaced by the file pin of the
    admitted, fresh, hash-verified captured file (:func:`captured_input`), and a plain pin supplied for any config key
    that the contract assigns to a reader (``readers[...]["config_key"]``) is ``UNCAPTURED_INPUT``: no hand-built path
    can stand in for a captured one. The block is dropped from the returned config.
    """
    block = config.get("captured_inputs")
    if block is None:
        return dict(config)
    if (
        not isinstance(block, Mapping)
        or set(block) != {"pins", "evidence_root", "bindings"}
        or not isinstance(block["bindings"], Mapping)
    ):
        raise CaptureRefused("CAPTURED_INPUTS_SHAPE")
    pin_path = verify_file_pin(block["pins"])
    out = {k: v for k, v in config.items() if k != "captured_inputs"}
    governed = {r["config_key"] for r in readers.values() if "config_key" in r}
    for key in sorted(governed & set(config)):
        raise CaptureRefused("UNCAPTURED_INPUT", key)
    for key, b in block["bindings"].items():
        if not isinstance(b, Mapping) or set(b) != {"source_id", "name", "reader"}:
            raise CaptureRefused("CAPTURED_INPUTS_SHAPE", str(key))
        rd = readers.get(b["reader"])
        if rd is None or rd.get("config_key") != key:
            raise CaptureRefused("CAPTURED_INPUTS_SHAPE", str(key))
        out[key] = file_pin(
            captured_input(
                b["source_id"], b["name"], pin_path=pin_path, evidence_root=Path(str(block["evidence_root"])),
                cutoff=cutoff, sources=sources, reader=b["reader"],
            )
        )  # fmt: skip
    return out
