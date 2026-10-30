"""Governed, read-only Alpaca capture driver (account, positions, activities)."""

from quant_engine.alpaca.capture.catalog import authorize_request, catalog_sha256
from quant_engine.alpaca.capture.config import (
    CaptureConfig,
    capture_config_from_mapping,
    credential_plan,
)
from quant_engine.alpaca.capture.errors import (
    CaptureError,
    CaptureIncomplete,
    CaptureRefused,
    GovernanceError,
    NormalizationError,
)
from quant_engine.alpaca.capture.fetch import (
    GovernedFetcher,
    HttpsFetcher,
    RawFetcher,
    RawResponse,
)
from quant_engine.alpaca.capture.governance import authorize_live_read
from quant_engine.alpaca.capture.run import RunResult, run_capture
from quant_engine.alpaca.capture.verify import VerificationReport, verify_run

__all__ = [
    "CaptureConfig",
    "CaptureError",
    "CaptureIncomplete",
    "CaptureRefused",
    "GovernanceError",
    "GovernedFetcher",
    "HttpsFetcher",
    "NormalizationError",
    "RawFetcher",
    "RawResponse",
    "RunResult",
    "VerificationReport",
    "authorize_live_read",
    "authorize_request",
    "capture_config_from_mapping",
    "catalog_sha256",
    "credential_plan",
    "run_capture",
    "verify_run",
]
