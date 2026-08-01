from __future__ import annotations


SUBMISSION_DISABLED_MESSAGE = (
    "Schwab order submission and cancellation are disabled in quant_engine. "
    "A separate approved Broker Core issue is required before either action can run."
)


class SubmissionDisabledError(RuntimeError):
    """Raised before any Schwab order mutation can reach a client or state store."""


def require_submission_authority(action: str) -> None:
    """Fail closed for every mutation retained only for source continuity."""

    raise SubmissionDisabledError(f"{action}: {SUBMISSION_DISABLED_MESSAGE}")
