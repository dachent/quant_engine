"""Deployment-local authenticated capture driver for GrizzlyBulls surfaces.

Mirrors the accepted ``schwab_capture`` pattern: a supervised CDP-attached
browser, owner login inside the driven window (the agent never touches
credentials), persist-then-parse artifacts, a create-only run directory, and
a counts/hashes-only manifest.
"""

from __future__ import annotations

__all__ = [
    "CaptureError",
    "CredentialScanError",
    "LoginRequiredError",
    "RouteFailure",
    "RunDirExistsError",
    "SessionError",
]

__version__ = "0.1.0"


class CaptureError(Exception):
    """Base for sanitized driver failures (no page content, no credentials)."""


class SessionError(CaptureError):
    """CDP attach or navigation failed."""


class LoginRequiredError(SessionError):
    """The driven page lacks the authenticated marker; owner login required."""


class RouteFailure(CaptureError):
    """A capture route could not produce a verified artifact."""


class RunDirExistsError(CaptureError):
    """The run directory already exists; run directories are create-only."""


class CredentialScanError(CaptureError):
    """A credential-shaped string was found in a would-be manifest field."""
