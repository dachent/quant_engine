"""Synthetic fixtures for the IBKR Flex capture driver tests.

Every value is invented.  The XML shapes follow the strict reader already in
``quant_engine.ibkr.flex``; real Flex response shapes have not been observed by
this driver and are UNVERIFIED.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from quant_engine.ibkr.capture import (
    CaptureRequest,
    RequestPlan,
    TERMS_SHA256,
    TERMS_VERSION,
    catalog_sha256,
)
from quant_engine.ibkr.capture.credentials import TOKEN_FIELD

FAKE_A = "placeholder-not-a-secret-0001"
FAKE_B = "placeholder-not-a-secret-0002"
FAKE_C = "placeholder-not-a-secret-0003"
ACCOUNT_ONE = "SYNTHACCT1"
ACCOUNT_TWO = "SYNTHACCT2"
REVIEW_URL = "https://github.com/dachent/quant_engine/issues/1#issuecomment-1"
NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)


def weekday_range(start: date, end: date) -> list[date]:
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _statement(account: str, day: date, *, extra_row: str = "") -> str:
    stamp = day.strftime("%Y%m%d")
    return (
        f'<FlexStatement accountId="{account}" fromDate="{stamp}" toDate="{stamp}" '
        'period="LastBusinessDay">'
        f'<AccountInformation accountId="{account}" currency="USD" />'
        "<CashTransactions>"
        f'<CashTransaction accountId="{account}" levelOfDetail="DETAIL" '
        f'dateTime="{stamp};120000" reportDate="{stamp}" '
        f'transactionID="cash-{account}-{stamp}" type="Dividends" '
        'amount="1.25" currency="USD" conid="900001" />'
        "</CashTransactions>"
        f'<ChangeInNAV accountId="{account}" endingValue="1000.50" />'
        "<ConversionRates>"
        f'<ConversionRate reportDate="{stamp}" fromCurrency="USD" '
        'toCurrency="USD" rate="1" />'
        "</ConversionRates>"
        "<CorporateActions />"
        "<EquitySummaryByReportDateInBase "
        f'accountId="{account}" reportDate="{stamp}" total="1000.50" />'
        "<OpenPositions>"
        f'<OpenPosition accountId="{account}" reportDate="{stamp}" '
        'levelOfDetail="SUMMARY" conid="900001" currency="USD" position="2" '
        'positionValue="20" fxRateToBase="1" symbol="SYNA" assetCategory="STK" />'
        f'<OpenPosition accountId="{account}" reportDate="{stamp}" '
        'levelOfDetail="SUMMARY" conid="900002" currency="USD" position="1" '
        'positionValue="30" fxRateToBase="1" symbol="SYNB" assetCategory="OPT" />'
        f'<OpenPosition accountId="{account}" reportDate="{stamp}" '
        'levelOfDetail="LOT" conid="900001" currency="USD" position="2" '
        'positionValue="20" fxRateToBase="1" symbol="SYNA" assetCategory="STK" />'
        "</OpenPositions>"
        "<Trades>"
        f'<Trade accountId="{account}" tradeDate="{stamp}" levelOfDetail="EXECUTION" '
        f'ibExecID="exec-{account}-{stamp}" transactionID="t-{account}-{stamp}" '
        'conid="900003" currency="USD" quantity="1" netCash="-10" '
        'transactionType="ExchTrade" buySell="BUY" assetCategory="STK" />'
        f"{extra_row}"
        "</Trades>"
        "<Transfers />"
        "</FlexStatement>"
    )


def activity_xml(
    accounts: tuple[str, ...],
    days: list[date],
    *,
    extra_row: str = "",
) -> bytes:
    body = "".join(
        _statement(a, d, extra_row=extra_row) for a in accounts for d in days
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<FlexQueryResponse queryName="SYNTHETIC" type="AF">'
        f'<FlexStatements count="{len(accounts) * len(days)}">{body}</FlexStatements>'
        "</FlexQueryResponse>"
    ).encode()


def security_xml(accounts: tuple[str, ...], start: date, end: date) -> bytes:
    stamp_start, stamp_end = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    statements = "".join(
        f'<FlexStatement accountId="{account}" fromDate="{stamp_start}" '
        f'toDate="{stamp_end}"><OpenPositions>'
        f'<OpenPosition reportDate="{stamp_end}" levelOfDetail="SUMMARY" '
        'conid="910001" symbol="SYNX" assetCategory="STK" />'
        f'<OpenPosition reportDate="{stamp_end}" levelOfDetail="SUMMARY" '
        'conid="910002" symbol="" assetCategory="STK" />'
        f"</OpenPositions><Trades>"
        f'<Trade tradeDate="{stamp_end}" levelOfDetail="EXECUTION" conid="910002" '
        'symbol="" assetCategory="STK" />'
        "</Trades></FlexStatement>"
        for account in accounts
    )
    return (
        '<FlexQueryResponse queryName="SYNTHETIC" type="AF"><FlexStatements>'
        f"{statements}</FlexStatements></FlexQueryResponse>"
    ).encode()


def send_ok(reference: str = "REF123", url: str | None = None) -> bytes:
    tail = f"<Url>{url}</Url>" if url else ""
    return (
        "<FlexStatementResponse><Status>Success</Status>"
        f"<ReferenceCode>{reference}</ReferenceCode>{tail}</FlexStatementResponse>"
    ).encode()


def service_error(code: str, status: str = "Fail") -> bytes:
    return (
        f"<FlexStatementResponse><Status>{status}</Status>"
        f"<ErrorCode>{code}</ErrorCode><ErrorMessage>synthetic</ErrorMessage>"
        "</FlexStatementResponse>"
    ).encode()


class FakeHttp:
    """Scripted transport; records (url, params) pairs; never opens a socket."""

    def __init__(self, responses: list[bytes | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(
        self, url: str, params: Mapping[str, str], headers: Mapping[str, str]
    ) -> bytes:
        assert headers["User-Agent"]
        self.calls.append((url, dict(params)))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSource:
    """Source double: returns scripted values, records every name read."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = dict(values)
        self.reads: list[str] = []

    def read(self, name: str, field: str | None) -> str:
        self.reads.append(name)
        return self.values[name]


def credential_config(*query_fields: str) -> dict[str, Any]:
    fields = {
        TOKEN_FIELD: [{"source": "windows-credential-manager", "name": "target-flex-a"}]
    }
    for index, field in enumerate(query_fields):
        fields[field] = [
            {"source": "windows-credential-manager", "name": f"target-query-{index}"}
        ]
    return {"credentials": {"ibkr": fields}}


def fake_source_for(*query_fields: str) -> FakeSource:
    values = {"target-flex-a": FAKE_A}
    for index, _ in enumerate(query_fields):
        values[f"target-query-{index}"] = FAKE_B if index == 0 else FAKE_C
    return FakeSource(values)


def make_plan(
    *,
    query: str = "activity-history",
    start: date = date(2026, 10, 5),
    end: date = date(2026, 10, 9),
    labels: tuple[str, ...] = (),
    non_trading: tuple[date, ...] = (),
) -> RequestPlan:
    return RequestPlan(
        pull_date=date(2026, 11, 3),
        requests=(CaptureRequest(query, start, end),),
        expected_account_labels=labels,
        known_non_trading_days=non_trading,
    )


def write_evidence(
    directory: Path,
    plan: RequestPlan,
    *,
    name: str = "evidence.json",
    mutate: Callable[[dict[str, Any]], None] | None = None,
    max_get: int = 5,
) -> Path:
    document: dict[str, Any] = {
        "schema_version": 1,
        "operation": "flex-capture",
        "plan": {"sha256": plan.sha256},
        "review": {"identity": "synthetic-reviewer", "reference": REVIEW_URL},
        "authority": {
            "status": "CURRENT",
            "issued_at": "2026-11-03T00:00:00Z",
            "expires_at": "2026-11-04T00:00:00Z",
        },
        "terms": {"version": TERMS_VERSION, "sha256": TERMS_SHA256},
        "catalog": {"sha256": catalog_sha256()},
        "metering": {
            "max_send_requests": len(plan.requests),
            "max_get_attempts": max_get,
            "retry_count": 0,
        },
    }
    if mutate is not None:
        mutate(document)
    path = directory / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class Harness:
    """Wires governance, credentials, transport and staging for one capture."""

    def __init__(
        self,
        tmp_path: Path,
        responses: list[bytes | Exception],
        *,
        plan: RequestPlan | None = None,
        max_get: int = 5,
    ) -> None:
        from quant_engine.ibkr.capture import (
            RawStage,
            authorize_live_read,
            capture_plan,
            get_query,
            resolve_flex_credentials,
        )

        self.plan = plan or make_plan()
        self.stage_root = tmp_path / "stage"
        self.receipts = tmp_path / "receipts"
        self.evidence_dir = tmp_path / "evidence"
        for directory in (self.stage_root, self.receipts, self.evidence_dir):
            directory.mkdir()
        evidence = write_evidence(self.evidence_dir, self.plan, max_get=max_get)
        self.governance = authorize_live_read(
            evidence, plan=self.plan, receipt_root=self.receipts, now=lambda: NOW
        )
        fields = tuple(get_query(item.query).query_field for item in self.plan.requests)
        self.source = fake_source_for(*fields)
        self.credentials = resolve_flex_credentials(
            credential_config(*fields),
            self.plan,
            sources={"windows-credential-manager": self.source},
        )
        self.http = FakeHttp(responses)
        self.waits: list[float] = []
        self._capture = capture_plan
        self._stage = RawStage(self.stage_root)

    def run(self) -> Any:
        return self._capture(
            self.plan,
            governance=self.governance,
            credentials=self.credentials,
            http_get=self.http,
            wait=self.waits.append,
            stage=self._stage,
            user_agent="quant-engine-synthetic-test",
            now=lambda: NOW,
            clock=lambda: 1000.0 + len(self.http.calls) * 5.0,
        )

    def run_dirs(self) -> list[Path]:
        return sorted(self.stage_root.iterdir())
