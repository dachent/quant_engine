"""Weekly capture refresh (#913): S0 for every source that needs a refresh, one table, every login at once.

Attended period-refresh step (``docs/period-refresh-runbook.md``), not a CI job: CI has no credentials or windows.
Read-only: the CDP probe only lists tabs (one GET to loopback, no proxy) and reads their URLs; the vault probe only
reports success; the public probe is one GET of the declared URL.

    python tools/capture_refresh.py --cutoff 2026-11-06 --pins <pins.json> --evidence-root <dir> [--contracts <file>]

Exit code: 0 every source fresh; 3 a login, credential, probe or stage is blocked or unverifiable (the table names the
owner step); 4 nothing is blocked but at least one source is ready to be captured.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import json
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
    "sso",
    "oauth",
    "auth",
    "logon",
    "session-expired",
    "timeout",
)
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _host_matches(url: str, suffixes: Sequence[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in suffixes)


def cdp_window_state(
    source: Source, *, fetch: Callable[[int], Sequence[Mapping[str, Any]]] | None = None
) -> str:
    """Read-only. Only tabs whose HOST is one of the source's declared ``window_hosts`` count (a tab on another site,
    a new-tab page or a query string that merely mentions the provider never does). No such tab: ABSENT. A matching
    tab whose path names a login/sign-on/SSO/timeout page is a wall; the window is OK only when at least one matching
    tab is NOT a wall. A bad or unexpected CDP reply is ABSENT (never an exception)."""
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
    walls = [
        any(m in urlsplit(u).path.lower() for m in LOGIN_PATH_MARKERS) for u in mine
    ]
    return WINDOW_LOGIN_WALL if all(walls) else WINDOW_OK


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
    if any(
        r["status"].startswith("BLOCKED") or r["status"] == "UNVERIFIABLE_BY_DESIGN"
        for r in rows
    ):
        return 3
    return 4 if any(r["status"] == "READY" for r in rows) else 0


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
    doc = json.loads(a.pins.read_text(encoding="utf-8")) if a.pins.exists() else {}
    pins = doc.get("pins", {}) if doc.get("schema_version") == 2 else {}
    rows = refresh_table(
        load_sources(a.contracts, repo_root=Path(__file__).resolve().parents[1]),
        pins=pins,
        cutoff=a.cutoff,
        window_state=cdp_window_state,
        vault_ok=vault_ok,
        http_ok=http_ok,
        evidence_root=a.evidence_root,
    )
    print(format_table(rows))
    return exit_code(rows)


if __name__ == "__main__":
    sys.exit(main())
