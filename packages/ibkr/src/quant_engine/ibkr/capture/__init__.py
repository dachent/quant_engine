"""Governed IBKR Flex Web Service capture driver (offline-tested, never live here)."""

from .catalog import FlexQuery, catalog_rows, catalog_sha256, get_query
from .credentials import (
    PROVIDER,
    TOKEN_FIELD,
    required_fields,
    resolve_flex_credentials,
)
from .governance import (
    GovernanceError,
    LiveReadGovernance,
    TERMS_SHA256,
    TERMS_VERSION,
    authorize_live_read,
)
from .normalize import NormalizationError, build_normalized, normalize_run
from .plan import CaptureRequest, PlanError, RequestPlan, load_plan, plan_from_document
from .responses import ResponseKind, ServiceResponse, classify_response
from .securities import (
    SymbolEntry,
    SymbolTable,
    ledger_security_id,
    scan_securities,
    security_key,
)
from .session import CaptureError, RunSummary, capture_plan
from .staging import RawStage, RunDirectory, StagingError
from .transport import Pacer, TransportError, UrllibHttpGet
from .verify import VerificationError, VerificationReport, verify_run

__all__ = [
    "CaptureError",
    "CaptureRequest",
    "FlexQuery",
    "GovernanceError",
    "LiveReadGovernance",
    "NormalizationError",
    "Pacer",
    "PROVIDER",
    "PlanError",
    "RawStage",
    "RequestPlan",
    "ResponseKind",
    "RunDirectory",
    "RunSummary",
    "ServiceResponse",
    "StagingError",
    "SymbolEntry",
    "SymbolTable",
    "TERMS_SHA256",
    "TERMS_VERSION",
    "TOKEN_FIELD",
    "TransportError",
    "UrllibHttpGet",
    "VerificationError",
    "VerificationReport",
    "authorize_live_read",
    "build_normalized",
    "capture_plan",
    "catalog_rows",
    "catalog_sha256",
    "classify_response",
    "get_query",
    "ledger_security_id",
    "load_plan",
    "normalize_run",
    "plan_from_document",
    "required_fields",
    "resolve_flex_credentials",
    "scan_securities",
    "security_key",
    "verify_run",
]
