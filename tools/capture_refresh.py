"""Weekly capture refresh (#913): S0 for every source that needs a refresh, one table, every login at once.

Attended period-refresh step (``docs/period-refresh-runbook.md``), not a CI job: CI has no credentials or windows.
Read-only: the CDP probe only lists tabs (one GET to loopback, no proxy) and reads their URLs; the vault probe only
reports success; the public probe is one GET of the declared URL.

    python tools/capture_refresh.py --cutoff 2026-11-06 --pins <pins.json> --evidence-root <dir> [--contracts <file>]

Exit code: 0 fresh; 3 owner action needed (login, window, credential, unreachable, probe error; the table names the
owner step); 4 ready to capture; 5 only not-wired or unverifiable-by-design sources remain.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import json
import re
from pathlib import Path
import subprocess
import sys
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlsplit

try:  # run as a script or imported as ``tools.capture_refresh``
    from capture_pipeline import (
        WINDOW_ABSENT,
        WINDOW_LOGIN_WALL,
        WINDOW_OK,
        Source,
        load_sources,
        refresh_table,
    )
except ImportError:  # pragma: no cover
    from tools.capture_pipeline import (
        WINDOW_ABSENT,
        WINDOW_LOGIN_WALL,
        WINDOW_OK,
        Source,
        load_sources,
        refresh_table,
    )

LOGIN_PATH_MARKERS = (
    "login",
    "signin",
    "sign-in",
    "signon",
    "logon",
    "sso",
    "oauth",
    "session-expired",
    "timeout",
    "timedout",
)
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _host_matches(url: str, suffixes: Sequence[str]) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:  # one malformed tab URL must not break every source
        return False
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _is_wall(url: str) -> bool:
    """A login/sign-on/SSO/timeout page by its path SEGMENTS, or a login marker in the query or fragment."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    segments = [s for s in re.split(r"[/#?&=]+", parts.path.lower()) if s]
    tail = (parts.query + " " + parts.fragment).lower()
    return any(s in LOGIN_PATH_MARKERS for s in segments) or any(
        m in tail for m in ("login", "signin", "signon", "sso")
    )


def cdp_window_state(
    source: Source,
    *,
    fetch: Callable[[int], Sequence[Mapping[str, Any]]] | None = None,
) -> str:
    """Read-only. Only tabs whose HOST is one of the source's declared ``window_hosts`` count (another site, a new-tab page
    or a query string that merely mentions the provider never does). No such tab: ABSENT. A matching tab on a login /
    sign-on / SSO / timeout path (or with such a marker in its query or fragment) is a wall; the window is OK only when
    at least one matching tab is not a wall. A bad CDP reply is ABSENT (never an exception). This is URL evidence only:
    a public page on the same domain still reads OK, and the stage re-checks the page it lands on (``expect`` marker)."""
    if source.cdp_port is None or not source.window_hosts:
        return WINDOW_ABSENT
    try:
        if fetch is None:
            with _NO_PROXY.open(
                f"http://127.0.0.1:{source.cdp_port}/json", timeout=3
            ) as resp:
                tabs = json.load(resp)
        else:
            tabs = fetch(source.cdp_port)
        if not isinstance(tabs, list):
            return WINDOW_ABSENT
    except (OSError, ValueError):
        return WINDOW_ABSENT
    mine = [
        str(t.get("url", ""))
        for t in tabs
        if isinstance(t, dict)
        and t.get("type") == "page"
        and _host_matches(str(t.get("url", "")), source.window_hosts)
    ]
    if not mine:
        return WINDOW_ABSENT
    return WINDOW_LOGIN_WALL if all(_is_wall(u) for u in mine) else WINDOW_OK


def vault_ok(secret: str) -> bool:
    """The sanctioned reader only; its output is discarded, only the exit code is used."""
    kvget = Path("C:/Dev/bin/kvget.ps1")
    if not kvget.exists() or secret.startswith("-"):
        return False
    try:
        r = subprocess.run(  # noqa: S603
            ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(kvget), secret],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def http_ok(source: Source) -> bool:
    """One GET of the declared probe URL, no proxy, any HTTP answer counts as reachable."""
    if not source.probe_url:
        return False
    try:
        with _NO_PROXY.open(source.probe_url, timeout=10):
            return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def cmdkey_ok(name: str) -> bool:
    """Existence of a Windows credential-manager target, never its secret (``cmdkey /list:<target>`` prints only the
    target, type and user)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
        return False
    try:
        r = subprocess.run(  # noqa: S603
            ["cmdkey", f"/list:{name}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and "target:" in r.stdout.lower()


def format_table(rows: list[dict[str, str]]) -> str:
    head = ("source", "reason", "status", "as_of", "owner_step")
    width = (
        [max(len(h), *(len(r[h]) for r in rows)) for h in head]
        if rows
        else [len(h) for h in head]
    )

    def line(vals: Sequence[str]) -> str:
        return "  ".join(v.ljust(w) for v, w in zip(vals, width, strict=True)).rstrip()

    return "\n".join([line(head), *(line([r[h] for h in head]) for r in rows)])


def exit_code(rows: list[dict[str, str]]) -> int:
    """0 every source fresh; 3 the owner has something to do (login, window, credential, unreachable source, probe error:
    ask for all of them in one message); 4 nothing for the owner but at least one source is ready to be captured; 5 only
    sources that cannot run yet (stage not wired) or cannot be checked by design remain."""
    owner = (
        "BLOCKED_LOGIN",
        "BLOCKED_NO_WINDOW",
        "BLOCKED_CREDENTIAL",
        "BLOCKED_UNREACHABLE",
        "BLOCKED_PROBE_ERROR",
    )
    statuses = {r["status"] for r in rows}
    if statuses & set(owner):
        return 3
    if "READY" in statuses:
        return 4
    return 5 if statuses - {"SKIP_FRESH"} else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cutoff", required=True)
    ap.add_argument("--pins", type=Path, required=True)
    ap.add_argument("--evidence-root", type=Path, required=True)
    ap.add_argument(
        "--contracts",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "contracts"
        / "capture-sources-v1.json",
    )
    a = ap.parse_args(argv)
    pins: dict[str, Any] = {}
    if a.pins.exists():
        doc = json.loads(a.pins.read_text(encoding="utf-8"))
        if (
            not isinstance(doc, dict)
            or doc.get("schema_version") != 2
            or not isinstance(doc.get("pins"), dict)
        ):
            raise SystemExit(
                "PIN_SCHEMA: refusing to treat an unknown pin file as empty"
            )
        pins = doc["pins"]
    rows = refresh_table(
        load_sources(a.contracts, repo_root=Path(__file__).resolve().parents[1]),
        pins=pins,
        cutoff=a.cutoff,
        window_state=cdp_window_state,
        vault_ok=vault_ok,
        http_ok=http_ok,
        store_ok=cmdkey_ok,
        evidence_root=a.evidence_root,
    )
    print(format_table(rows))
    return exit_code(rows)


if __name__ == "__main__":
    sys.exit(main())
