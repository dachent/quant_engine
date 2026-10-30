"""Sanitized error types for the Alpaca capture driver.

No message in this package ever carries a credential, an account identifier, a
response body, or a native exception string.
"""

from __future__ import annotations


class CaptureError(RuntimeError):
    """The capture driver refused or failed; the message is safe to print."""


class CaptureRefused(CaptureError):
    """A request, method, endpoint, or configuration violated the read-only policy."""


class GovernanceError(CaptureError):
    """Live-read governance evidence is absent, invalid, expired, or exhausted."""


class NormalizationError(CaptureError):
    """A raw response has an unknown or malformed shape; normalization fails closed."""


class RateLimited(CaptureError):
    """The provider answered with a rate-limit response; the driver never retries."""

    def __init__(self, retry_after_seconds: int | None) -> None:
        super().__init__("the provider rate-limited the request (no retry is made)")
        self.retry_after_seconds = retry_after_seconds


class CaptureIncomplete(CaptureError):
    """The run stopped early; its raw evidence and manifest were still written."""

    def __init__(self, reason: str, run_directory: str) -> None:
        super().__init__(f"capture incomplete: {reason}")
        self.reason = reason
        self.run_directory = run_directory
