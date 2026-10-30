"""Classification of Flex Web Service responses (fail closed).

Rules from docs/providers/ibkr-read-capabilities.md: poll only for error 1019;
any other non-report response is terminal for that request; documented
incomplete, settlement-not-ready and P&L-not-ready responses are refused and
never become evidence of a statement; polling never issues a second SendRequest.

The error-code table below follows IBKR's published Flex Web Service error
list.  Real response bodies have not been observed by this driver, so every
shape assumption here is UNVERIFIED until a governed live read confirms it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from .catalog import ALLOWED_HOSTS

STEP_SEND = "send"
STEP_GET = "get"

_REFERENCE = re.compile(r"^[A-Za-z0-9]{1,64}$")
_ERROR_CODE = re.compile(r"^\d{4}$")


class ResponseKind(str, Enum):
    REPORT = "REPORT"
    SEND_ACCEPTED = "SEND_ACCEPTED"
    POLL_AGAIN = "POLL_AGAIN"
    REFUSED_RETRY_LATER = "REFUSED_RETRY_LATER"
    REFUSED_INCOMPLETE = "REFUSED_INCOMPLETE"
    REFUSED_SETTLEMENT_NOT_READY = "REFUSED_SETTLEMENT_NOT_READY"
    REFUSED_PNL_NOT_READY = "REFUSED_PNL_NOT_READY"
    REFUSED_REQUEST = "REFUSED_REQUEST"
    REFUSED_UNKNOWN = "REFUSED_UNKNOWN"
    REFUSED_MALFORMED = "REFUSED_MALFORMED"


_POLL_CODE = "1019"
_RETRY_LATER = frozenset({"1001", "1009", "1018", "1021"})
_INCOMPLETE = frozenset({"1004"})
_SETTLEMENT = frozenset({"1005"})
_PNL = frozenset({"1006", "1007", "1008"})
_REQUEST_ERRORS = frozenset(
    {"1003", "1010", "1011", "1012", "1013", "1014", "1015", "1016", "1017", "1020"}
)


@dataclass(frozen=True)
class ServiceResponse:
    kind: ResponseKind
    status: str | None = None
    error_code: str | None = None
    reference_code: str | None = None
    statement_url: str | None = None

    @property
    def refused(self) -> bool:
        return self.kind.value.startswith("REFUSED_")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def validated_statement_url(value: str | None) -> str | None:
    """The GetStatement URL a SendRequest response names, if it is acceptable."""

    if value is None or not value.strip():
        return None
    parts = urlsplit(value.strip())
    if (
        parts.scheme != "https"
        or parts.hostname not in ALLOWED_HOSTS
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or parts.query
        or parts.fragment
        or not parts.path.endswith("GetStatement")
    ):
        raise ValueError("the statement URL is not an approved Flex host")
    return f"https://{parts.hostname}{parts.path}"


def classify_response(body: bytes, *, step: str) -> ServiceResponse:
    """Classify one raw response body; never raises for provider content."""

    if step not in (STEP_SEND, STEP_GET):
        raise ValueError("unknown protocol step")
    if not body.strip():
        return ServiceResponse(ResponseKind.REFUSED_MALFORMED)
    upper = body.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        return ServiceResponse(ResponseKind.REFUSED_MALFORMED)
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        # Includes HTML error pages and truncated responses.
        return ServiceResponse(ResponseKind.REFUSED_MALFORMED)
    tag = _local(root.tag)
    if tag == "FlexQueryResponse":
        if step != STEP_GET:
            return ServiceResponse(ResponseKind.REFUSED_UNKNOWN)
        return ServiceResponse(ResponseKind.REPORT)
    if tag != "FlexStatementResponse":
        return ServiceResponse(ResponseKind.REFUSED_MALFORMED)
    fields = {_local(item.tag): (item.text or "").strip() for item in root}
    status = fields.get("Status") or None
    code = fields.get("ErrorCode") or None
    if code is not None and _ERROR_CODE.fullmatch(code) is None:
        return ServiceResponse(ResponseKind.REFUSED_MALFORMED, status=status)
    if status == "Success":
        reference = fields.get("ReferenceCode") or None
        if step == STEP_SEND and reference and _REFERENCE.fullmatch(reference):
            try:
                url = validated_statement_url(fields.get("Url"))
            except ValueError:
                return ServiceResponse(
                    ResponseKind.REFUSED_MALFORMED, status=status, reference_code=None
                )
            return ServiceResponse(
                ResponseKind.SEND_ACCEPTED,
                status=status,
                reference_code=reference,
                statement_url=url,
            )
        return ServiceResponse(ResponseKind.REFUSED_UNKNOWN, status=status)
    if status not in ("Warn", "Fail") or code is None:
        return ServiceResponse(ResponseKind.REFUSED_UNKNOWN, status=status)
    if code == _POLL_CODE:
        kind = (
            ResponseKind.POLL_AGAIN
            if step == STEP_GET
            else ResponseKind.REFUSED_UNKNOWN
        )
    elif code in _RETRY_LATER:
        kind = ResponseKind.REFUSED_RETRY_LATER
    elif code in _INCOMPLETE:
        kind = ResponseKind.REFUSED_INCOMPLETE
    elif code in _SETTLEMENT:
        kind = ResponseKind.REFUSED_SETTLEMENT_NOT_READY
    elif code in _PNL:
        kind = ResponseKind.REFUSED_PNL_NOT_READY
    elif code in _REQUEST_ERRORS:
        kind = ResponseKind.REFUSED_REQUEST
    else:
        kind = ResponseKind.REFUSED_UNKNOWN
    return ServiceResponse(kind, status=status, error_code=code)
