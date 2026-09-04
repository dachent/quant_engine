"""Supervised CDP session attachment.

Playwright is imported lazily inside the functions that need it so the
offline test-suite never imports it. The agent drives only pages the owner
has already authenticated inside the driven window; it never sees, types, or
stores credentials.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

from grizzly_capture import LoginRequiredError, SessionError

DEFAULT_CDP_PORT = 9223
DEFAULT_BASE_URL = "https://grizzlybulls.com"
NAVIGATION_TIMEOUT_MS = 60_000

# Presence of any of these selectors on the driven page is treated as the
# authenticated marker. Selectors only; no credential material is involved.
AUTHENTICATED_MARKER_SELECTORS: tuple[str, ...] = (
    "a[href*='logout' i]",
    "a[href*='signout' i]",
    "a[href*='sign-out' i]",
    "[data-testid='account-menu']",
    "[data-testid='user-menu']",
    "text=/sign out/i",
    "text=/log out/i",
)


@dataclass
class BrowserSession:
    """Handle over a CDP-attached, owner-launched browser."""

    playwright: Any
    browser: Any


@contextlib.contextmanager
def browser_session(port: int = DEFAULT_CDP_PORT) -> Iterator[BrowserSession]:
    """Attach to the owner-launched browser over CDP (lazy playwright import)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SessionError("playwright is required for a live capture run") from exc
    playwright = sync_playwright().start()
    try:
        try:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}",
            )
        except Exception as exc:
            raise SessionError("unable to attach to the CDP endpoint") from exc
        yield BrowserSession(playwright=playwright, browser=browser)
    finally:
        playwright.stop()


def first_or_new_page(
    session: BrowserSession, base_url: str = DEFAULT_BASE_URL
) -> Any:
    """Reuse the owner's tab for the target site when one exists, else open a
    new one. With multiple tabs open, pages[0] may belong to a different
    site entirely, so prefer a tab already on the target host."""
    contexts = session.browser.contexts
    context = contexts[0] if contexts else session.browser.new_context()
    pages = context.pages
    host = base_url.split("//")[-1].split("/")[0].lower()
    for page in pages:
        if host in (page.url or "").lower():
            return page
    return pages[0] if pages else context.new_page()


def navigate_and_idle(
    page: Any,
    url: str,
    *,
    timeout_ms: int = NAVIGATION_TIMEOUT_MS,
) -> None:
    """Navigate to ``url`` and wait for the network to go idle."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception as exc:
        raise SessionError("navigation failed") from exc


def is_authenticated(page: Any) -> bool:
    """True when an authenticated marker is present on the driven page."""
    for selector in AUTHENTICATED_MARKER_SELECTORS:
        try:
            if page.query_selector(selector) is not None:
                return True
        except Exception:  # an unsupported selector must not fail open
            continue
    return False


def require_authenticated(page: Any) -> None:
    """Fail closed when the owner has not logged in inside the driven window."""
    if not is_authenticated(page):
        raise LoginRequiredError("owner login required")


def set_download_behavior(page: Any, download_dir: Path) -> None:
    """Route browser downloads into the run directory via CDP."""
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": str(download_dir),
                "eventsEnabled": True,
            },
        )
    except Exception as exc:
        raise SessionError("unable to set CDP download behavior") from exc


def sanitized_page_url(page: Any) -> str:
    """Scheme/host/path only; query and fragment may carry session material."""
    parsed = urlsplit(str(page.url))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
