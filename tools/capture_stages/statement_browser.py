"""Statement-site stage (#913 sources ``john_hancock_nav`` and ``composer_balance``).

Read-only capture in the owner's signed-in window: navigate to declared pages, read their text, write it create-only.
The stage never types: a login/2FA/captcha wall stops it (``StageBlocked("BLOCKED_LOGIN")``) and the owner signs in.
The page object is any Playwright-like ``page`` with ``url``, ``goto(url)`` and ``inner_text("body")``; navigation is
limited to the plan's allowed origin, so a link can never take the stage elsewhere.

Parsers and the admission rule are pure functions over statement text (pdftotext -layout), proven by hand on eight
John Hancock quarterly statements (every quarter closes to the cent) and ten Composer monthly statements.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import re
import shutil
from typing import Any
from urllib.parse import urlsplit

LOGIN_MARKERS = (
    "sign in",
    "log in",
    "login id",
    "forgot password",
    "verification code",
    "two-factor",
    "captcha",
    "session has timed out",
)
SEGMENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")
RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
LOGIN_URL_MARKERS = ("login", "signin", "sign-in", "sso", "auth")


class StageBlocked(RuntimeError):
    """The stage stopped at a wall that only the owner can pass; ``code`` is stable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def is_login_wall(url: str, text: str) -> bool:
    """A login page by its URL, or a short page whose text is a sign-in prompt (a long statement page that merely
    contains the words 'log in' in a footer is not a wall)."""
    if any(
        m in urlsplit(url).path.lower() or m in urlsplit(url).netloc.lower()
        for m in LOGIN_URL_MARKERS
    ):
        return True
    low = text.lower()
    return len(text) < 4000 and any(m in low for m in LOGIN_MARKERS)


def capture_pages(
    page: Any,
    plan: Sequence[Mapping[str, str]],
    out_dir: Path,
    *,
    allowed_origin: str,
    settle: Any = None,
) -> list[str]:
    """Visit each ``{"name", "url", "expect"}`` in the plan and write ``<name>.txt`` (LF bytes) into a NEW directory.

    Every plan item needs an ``expect`` marker (text the signed-in statement page must contain; without it a long
    sign-in or time-out page could be taken for a capture). Names are plain ``[A-Za-z0-9_][A-Za-z0-9._-]*`` (no path
    syntax, no ``:`` streams). Refuses a URL outside ``allowed_origin`` before navigating and a landing page outside it
    afterwards, stops at the first login wall or page missing its marker, and writes nothing to ``out_dir`` unless the
    WHOLE plan succeeded: pages go to a staging directory next to it that is renamed on completion, so a wall on page
    two leaves no partial capture to be hashed or resumed over. ``out_dir`` must not exist.
    """
    out = Path(out_dir)
    if out.exists():
        raise FileExistsError(str(out))
    staging = out.with_name(f".{out.name}.staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    written: list[str] = []
    try:
        for item in plan:
            url, name = item["url"], item["name"]
            if (
                not SEGMENT_RE.fullmatch(name)
                or name.endswith(".")
                or name.split(".")[0].upper() in RESERVED
            ):
                raise ValueError("PLAN_NAME")
            if not item.get("expect"):
                raise ValueError("PLAN_EXPECT")
            if (
                urlsplit(url)._replace(path="", query="", fragment="").geturl()
                != allowed_origin
            ):
                raise ValueError("PLAN_ORIGIN")
            page.goto(url)
            if settle is not None:
                settle(page)
            text = page.inner_text("body")
            wall = is_login_wall(page.url, text)
            landed = (
                urlsplit(page.url)._replace(path="", query="", fragment="").geturl()
            )
            if landed != allowed_origin and not wall:
                raise ValueError(
                    "REDIRECT_ORIGIN"
                )  # a redirect off the allowed origin is never captured
            if wall or item["expect"] not in text:
                raise StageBlocked(
                    "BLOCKED_LOGIN"
                )  # a wall, or a page without its marker, is the owner's to clear
            with (staging / f"{name}.txt").open(
                "x", encoding="utf-8", newline="\n"
            ) as fh:
                fh.write(text)
            written.append(name)
        staging.rename(out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return written


def _dec(s: str) -> Decimal:
    return Decimal(s.replace(",", "").replace("$", "").strip())


def parse_jh_statement(text: str) -> dict[str, Any]:
    """John Hancock quarterly statement: period, opening and closing balance and the right-column activity items.

    ``items`` are the signed amounts between the opening and closing values of the period summary, so
    ``opening + sum(items) == closing`` is the closure check (:func:`capture_pipeline.check_nav_identity`). A shape that
    does not parse raises ``ValueError`` with a stable code, never a raw ``AttributeError``/``StopIteration``.
    """
    lines = text.split("\n")
    m = re.search(r"STATEMENT PERIOD: (\S+) - (\S+)", lines[0])
    ob = re.search(r"Opening Balance\s+\$\s*([\d,\.]+)", text)
    cb = re.search(r"Closing Balance\s+\$\s*([\d,\.]+)", text)
    start_line = next(
        (k for k, line in enumerate(lines) if "For the period" in line), None
    )
    if not (m and ob and cb) or start_line is None:
        raise ValueError("JH_SHAPE")
    try:
        start = datetime.strptime(m.group(1), "%m/%d/%Y").date()
        end = datetime.strptime(m.group(2), "%m/%d/%Y").date()
    except ValueError as exc:
        raise ValueError("JH_PERIOD") from exc
    opening, closing = _dec(ob.group(1)), _dec(cb.group(1))
    values: list[Decimal] = []
    for line in lines[start_line : start_line + 12]:
        found = re.findall(r"-?\$?-?[\d,]+\.\d\d", line)
        if (
            found and len(line.rstrip()) > 150
        ):  # the right-hand column of the summary table
            values.append(_dec(found[-1]))
    items: list[Decimal] = []
    for v in values[1:]:
        if v == closing:
            break
        items.append(v)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "opening": opening,
        "closing": closing,
        "items": items,
    }


def jh_identity_rows(quarters: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rows for ``check_nav_identity`` (identity per quarter; the chain check is NOT applied: a published
    restatement at a quarter boundary is a documented break, reported by :func:`jh_boundary_gaps`)."""
    return [
        {
            "opening": float(q["opening"]),
            "ending": float(q["closing"]),
            "components": [float(i) for i in q["items"]],
        }
        for q in sorted(quarters, key=lambda q: q["start"])
    ]


def jh_boundary_gaps(quarters: Sequence[Mapping[str, Any]]) -> list[Decimal]:
    """Opening minus the prior closing at each boundary (non-zero = restatement; reported, never absorbed)."""
    qs = sorted(quarters, key=lambda q: q["start"])
    return [b["opening"] - a["closing"] for a, b in zip(qs, qs[1:])]


def parse_composer_statement(text: str) -> dict[str, Any]:
    """Composer (Alpaca-custodied) monthly statement: total market value including cash at the statement end."""
    m = re.search(r"Total Market Value\s+\$?([\d,]+\.\d\d)", text)
    if not m:
        raise ValueError("COMPOSER_TOTAL")
    cash = re.search(r"Ending Value\s+\$?(-?[\d,]+\.\d\d)", text)
    parts: list[Decimal] = []
    in_holdings = False
    for line in text.split(chr(10)):
        if line.startswith("Symbol Description Quantity Market Price Market Value"):
            in_holdings = True
            continue
        if in_holdings:
            if re.fullmatch(r"\d+", line.strip()):
                break
            amounts = re.findall(r"\$\s*(?:--|-?[\d,]+\.\d\d)", line)
            if line.startswith("*Cash") and len(amounts) >= 3:
                parts.append(_dec(amounts[2].replace("--", "0")))
            elif len(amounts) >= 2 and not line.startswith("*"):
                parts.append(_dec(amounts[1].replace("--", "0")))
    return {
        "total_market_value": _dec(m.group(1)),
        "cash_ending": _dec(cash.group(1)) if cash else None,
        "holdings_values": parts,
    }


def latest_on_or_before(
    points: Sequence[Mapping[str, Any]], cutoff: str
) -> Mapping[str, Any] | None:
    """Admission rule for a sparse series: the latest point dated on or before the cutoff (a later live balance is
    post-cutoff evidence, never the cutoff value)."""
    c = date.fromisoformat(cutoff)
    eligible = [p for p in points if date.fromisoformat(p["date"]) <= c]
    return max(eligible, key=lambda p: p["date"], default=None)
