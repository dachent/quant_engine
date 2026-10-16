"""Strict, read-only Activity Flex parsing and injectable report retrieval."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path
import re
from typing import Protocol
from xml.etree import ElementTree as ET


SEND_REQUEST_URL = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
)
GET_STATEMENT_URL = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/"
    "FlexWebService/GetStatement"
)
_POLL_INITIAL_DELAY_SECONDS = 20.0
_POLL_MAX_DELAY_SECONDS = 120.0
_DATE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})")
_TIME_OF_DAY = re.compile(r"^\d{4}-?\d{2}-?\d{2}[;,T ]+\s*(\d{2}):?(\d{2}):?(\d{2})")
_REQUIRED_SECTIONS = {
    "AccountInformation",
    "CashTransactions",
    "ChangeInNAV",
    "ConversionRates",
    "CorporateActions",
    "EquitySummaryByReportDateInBase",
    "OpenPositions",
    "Trades",
    "Transfers",
}
_CHANGE_IN_NAV_NONADDITIVE_ATTRIBUTES = frozenset(
    {
        "accountAlias",
        "accountId",
        "acctAlias",
        "currency",
        "endingValue",
        "fromDate",
        "model",
        "startingValue",
        "toDate",
        "twr",
    }
)


class FlexValidationError(ValueError):
    """A Flex response is incomplete, ambiguous, or structurally invalid."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(value: str | None, name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise FlexValidationError(f"{name} must not be empty")
    return normalized


def _optional(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _day(value: str | None, name: str) -> date:
    raw = _text(value, name)
    match = _DATE.match(raw)
    if match is None:
        raise FlexValidationError(f"{name} must begin with a Flex date")
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError as exc:
        raise FlexValidationError(f"{name} contains an invalid date") from exc


def _time_of_day(value: str | None) -> str | None:
    """The time of day a Flex ``dateTime`` carries, exactly as reported.

    Flex reports the time in the statement's own time zone and does not name
    it, so the value is kept as ``HH:MM:SS`` and never converted.  A date-only
    value has no time of day.
    """

    match = _TIME_OF_DAY.match((value or "").strip())
    if match is None:
        return None
    hours, minutes, seconds = (int(part) for part in match.groups())
    if hours > 23 or minutes > 59 or seconds > 59:
        raise FlexValidationError("dateTime carries an invalid time of day")
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _optional_day(value: str | None, name: str) -> date | None:
    if _optional(value) is None:
        return None
    return _day(value, name)


def _decimal(value: str | None, name: str) -> Decimal:
    raw = _text(value, name)
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise FlexValidationError(f"{name} must be a decimal") from exc
    if not parsed.is_finite():
        raise FlexValidationError(f"{name} must be finite")
    return parsed


def _optional_decimal(value: str | None, name: str) -> Decimal | None:
    if _optional(value) is None:
        return None
    return _decimal(value, name)


def _account_matches(element: ET.Element, raw_account_id: str) -> bool:
    observed = _optional(element.attrib.get("accountId"))
    return observed is None or observed == raw_account_id


def _require_account_match(element: ET.Element, raw_account_id: str) -> None:
    if not _account_matches(element, raw_account_id):
        raise FlexValidationError("account summary differs from FlexStatement")


def _elements(parent: ET.Element, name: str) -> list[ET.Element]:
    return [item for item in parent.iter() if _local(item.tag) == name]


def _single(parent: ET.Element, name: str) -> ET.Element:
    matches = _elements(parent, name)
    if len(matches) != 1:
        raise FlexValidationError(f"FlexStatement requires exactly one {name}")
    return matches[0]


def _event_identity(element: ET.Element, *provider_fields: str) -> str:
    for field in provider_fields:
        if value := _optional(element.attrib.get(field)):
            return value
    payload = "\x1f".join(
        f"{key}={value}" for key, value in sorted(element.attrib.items())
    )
    if not payload:
        raise FlexValidationError("activity row has no provider identity evidence")
    return f"derived-{hashlib.sha256(payload.encode()).hexdigest()}"


@dataclass(frozen=True)
class FlexPosition:
    raw_account_id: str
    report_date: date
    conid: str
    currency: str
    quantity: Decimal
    position_value: Decimal
    fx_rate_to_base: Decimal
    symbol: str | None
    description: str | None
    reported_account_matches_statement: bool
    asset_category: str | None = None
    sub_category: str | None = None
    multiplier: Decimal | None = None
    mark_price: Decimal | None = None
    side: str | None = None
    underlying_conid: str | None = None
    expiry: str | None = None
    strike: Decimal | None = None
    put_call: str | None = None


@dataclass(frozen=True)
class FlexTrade:
    raw_account_id: str
    event_date: date
    execution_id: str
    transaction_id: str | None
    trade_id: str | None
    conid: str | None
    currency: str
    quantity: Decimal
    net_cash: Decimal
    transaction_type: str
    buy_sell: str
    commission: Decimal | None
    taxes: Decimal | None
    reported_account_matches_statement: bool
    commission_currency: str | None = None
    executed_time: str | None = None


@dataclass(frozen=True)
class FlexCashTransaction:
    raw_account_id: str
    event_date: date
    nav_recognition_date: date
    transaction_id: str
    level_of_detail: str
    activity_type: str
    amount: Decimal
    currency: str
    conid: str | None
    code: str | None
    reported_account_matches_statement: bool
    description: str | None = None
    settlement_date: date | None = None
    fx_rate_to_base: Decimal | None = None
    event_time: str | None = None


@dataclass(frozen=True)
class FlexCashReport:
    raw_account_id: str
    currency: str
    level_of_detail: str
    from_date: date
    to_date: date
    starting_cash: Decimal | None
    ending_cash: Decimal | None
    ending_settled_cash: Decimal | None
    ending_cash_sec: Decimal | None
    ending_cash_com: Decimal | None
    ending_settled_cash_sec: Decimal | None
    ending_settled_cash_com: Decimal | None
    broker_interest: Decimal | None
    broker_interest_sec: Decimal | None
    broker_interest_com: Decimal | None
    broker_interest_mtd: Decimal | None
    broker_interest_ytd: Decimal | None
    reported_account_matches_statement: bool


@dataclass(frozen=True)
class FlexInterestAccrual:
    raw_account_id: str
    currency: str
    from_date: date
    to_date: date
    starting_accrual_balance: Decimal | None
    interest_accrued: Decimal | None
    accrual_reversal: Decimal | None
    ending_accrual_balance: Decimal | None
    fx_translation: Decimal | None
    reported_account_matches_statement: bool


@dataclass(frozen=True)
class FlexTierInterest:
    raw_account_id: str
    currency: str
    interest_type: str
    value_date: date
    report_date: date
    tier_break: str | None
    balance_threshold: Decimal | None
    rate: Decimal | None
    securities_principal: Decimal | None
    securities_interest: Decimal | None
    commodities_principal: Decimal | None
    commodities_interest: Decimal | None
    ibukl_principal: Decimal | None
    ibukl_interest: Decimal | None
    total_principal: Decimal | None
    total_interest: Decimal | None
    margin_balance: Decimal | None
    fx_rate_to_base: Decimal | None
    code: str | None
    from_account: str | None
    to_account: str | None
    reported_account_matches_statement: bool


@dataclass(frozen=True)
class FlexTransfer:
    raw_account_id: str
    event_date: date
    nav_recognition_date: date
    transaction_id: str
    transfer_type: str
    direction: str
    cash_amount: Decimal | None
    position_amount_in_base: Decimal | None
    currency: str
    conid: str | None
    quantity: Decimal | None
    reported_account_matches_statement: bool
    event_time: str | None = None


@dataclass(frozen=True)
class FlexCorporateAction:
    raw_account_id: str
    event_date: date
    identity: str
    action_type: str
    conid: str | None
    currency: str
    quantity: Decimal | None
    amount: Decimal | None
    reported_account_matches_statement: bool


@dataclass(frozen=True)
class FlexRate:
    report_date: date
    from_currency: str
    to_currency: str
    rate: Decimal


@dataclass(frozen=True)
class FlexDailyStatement:
    artifact_id: str
    raw_account_id: str
    report_date: date
    base_currency: str
    nav: Decimal
    change_in_nav_ending_value: Decimal
    change_in_nav_accounting_date: date
    change_in_nav_starting_value: Decimal | None
    change_in_nav_deposits_withdrawals: Decimal | None
    change_in_nav_asset_transfers: Decimal | None
    change_in_nav_internal_cash_transfers: Decimal | None
    change_in_nav_mtm: Decimal | None
    change_in_nav_components: tuple[tuple[str, Decimal], ...]
    positions: tuple[FlexPosition, ...]
    trades: tuple[FlexTrade, ...]
    cash_transactions: tuple[FlexCashTransaction, ...]
    transfers: tuple[FlexTransfer, ...]
    corporate_actions: tuple[FlexCorporateAction, ...]
    rates: tuple[FlexRate, ...]
    cash_reports: tuple[FlexCashReport, ...] = ()
    interest_accruals: tuple[FlexInterestAccrual, ...] = ()
    tier_interest: tuple[FlexTierInterest, ...] = ()
    nav_cash: Decimal | None = None
    nav_broker_cash_component: Decimal | None = None
    nav_interest_accruals: Decimal | None = None
    nav_broker_interest_accruals_component: Decimal | None = None
    nav_margin_financing_charge_accruals: Decimal | None = None


@dataclass(frozen=True)
class FlexDocument:
    artifact_id: str
    query_name_present: bool
    statements: tuple[FlexDailyStatement, ...]


def _position(element: ET.Element, raw_account_id: str) -> FlexPosition:
    return FlexPosition(
        raw_account_id=raw_account_id,
        report_date=_day(element.attrib.get("reportDate"), "position.reportDate"),
        conid=_text(element.attrib.get("conid"), "position.conid"),
        currency=_text(element.attrib.get("currency"), "position.currency"),
        quantity=_decimal(element.attrib.get("position"), "position.quantity"),
        position_value=_decimal(
            element.attrib.get("positionValue"), "position.positionValue"
        ),
        fx_rate_to_base=_decimal(
            element.attrib.get("fxRateToBase"), "position.fxRateToBase"
        ),
        symbol=_optional(element.attrib.get("symbol")),
        description=_optional(element.attrib.get("description")),
        reported_account_matches_statement=_account_matches(element, raw_account_id),
        asset_category=_optional(element.attrib.get("assetCategory")),
        sub_category=_optional(element.attrib.get("subCategory")),
        multiplier=_optional_decimal(
            element.attrib.get("multiplier"), "position.multiplier"
        ),
        mark_price=_optional_decimal(
            element.attrib.get("markPrice"), "position.markPrice"
        ),
        side=_optional(element.attrib.get("side")),
        underlying_conid=_optional(element.attrib.get("underlyingConid")),
        expiry=_optional(element.attrib.get("expiry")),
        strike=_optional_decimal(element.attrib.get("strike"), "position.strike"),
        put_call=_optional(element.attrib.get("putCall")),
    )


def _trade(element: ET.Element, raw_account_id: str) -> FlexTrade:
    return FlexTrade(
        raw_account_id=raw_account_id,
        event_date=_day(
            element.attrib.get("tradeDate") or element.attrib.get("dateTime"),
            "trade.event_date",
        ),
        execution_id=_text(
            _event_identity(element, "ibExecID", "transactionID", "tradeID"),
            "trade.execution_identity",
        ),
        transaction_id=_optional(element.attrib.get("transactionID")),
        trade_id=_optional(element.attrib.get("tradeID")),
        conid=_optional(element.attrib.get("conid")),
        currency=_text(element.attrib.get("currency"), "trade.currency"),
        quantity=_decimal(element.attrib.get("quantity"), "trade.quantity"),
        net_cash=_decimal(element.attrib.get("netCash"), "trade.netCash"),
        transaction_type=_text(
            element.attrib.get("transactionType"), "trade.transactionType"
        ),
        buy_sell=_text(element.attrib.get("buySell"), "trade.buySell"),
        commission=_optional_decimal(
            element.attrib.get("ibCommission"), "trade.ibCommission"
        ),
        taxes=_optional_decimal(element.attrib.get("taxes"), "trade.taxes"),
        reported_account_matches_statement=_account_matches(element, raw_account_id),
        commission_currency=_optional(element.attrib.get("ibCommissionCurrency")),
        executed_time=_time_of_day(element.attrib.get("dateTime")),
    )


def _cash_transaction(element: ET.Element, raw_account_id: str) -> FlexCashTransaction:
    return FlexCashTransaction(
        raw_account_id=raw_account_id,
        event_date=_day(
            element.attrib.get("dateTime") or element.attrib.get("reportDate"),
            "cash_transaction.event_date",
        ),
        nav_recognition_date=_day(
            element.attrib.get("reportDate"), "cash_transaction.reportDate"
        ),
        transaction_id=_text(
            _event_identity(element, "transactionID", "actionID"),
            "cash_transaction.identity",
        ),
        level_of_detail=_text(
            element.attrib.get("levelOfDetail"), "cash_transaction.levelOfDetail"
        ),
        activity_type=_text(element.attrib.get("type"), "cash_transaction.type"),
        amount=_decimal(element.attrib.get("amount"), "cash_transaction.amount"),
        currency=_text(element.attrib.get("currency"), "cash_transaction.currency"),
        conid=_optional(element.attrib.get("conid")),
        code=_optional(element.attrib.get("code")),
        reported_account_matches_statement=_account_matches(element, raw_account_id),
        description=_optional(element.attrib.get("description")),
        settlement_date=_optional_day(
            element.attrib.get("settleDate"), "cash_transaction.settleDate"
        ),
        fx_rate_to_base=_optional_decimal(
            element.attrib.get("fxRateToBase"), "cash_transaction.fxRateToBase"
        ),
        event_time=_time_of_day(element.attrib.get("dateTime")),
    )


def _cash_report(element: ET.Element, raw_account_id: str) -> FlexCashReport:
    return FlexCashReport(
        raw_account_id=raw_account_id,
        currency=_text(element.attrib.get("currency"), "cash_report.currency"),
        level_of_detail=_text(
            element.attrib.get("levelOfDetail"), "cash_report.levelOfDetail"
        ),
        from_date=_day(element.attrib.get("fromDate"), "cash_report.fromDate"),
        to_date=_day(element.attrib.get("toDate"), "cash_report.toDate"),
        starting_cash=_optional_decimal(
            element.attrib.get("startingCash"), "cash_report.startingCash"
        ),
        ending_cash=_optional_decimal(
            element.attrib.get("endingCash"), "cash_report.endingCash"
        ),
        ending_settled_cash=_optional_decimal(
            element.attrib.get("endingSettledCash"), "cash_report.endingSettledCash"
        ),
        ending_cash_sec=_optional_decimal(
            element.attrib.get("endingCashSec"), "cash_report.endingCashSec"
        ),
        ending_cash_com=_optional_decimal(
            element.attrib.get("endingCashCom"), "cash_report.endingCashCom"
        ),
        ending_settled_cash_sec=_optional_decimal(
            element.attrib.get("endingSettledCashSec"),
            "cash_report.endingSettledCashSec",
        ),
        ending_settled_cash_com=_optional_decimal(
            element.attrib.get("endingSettledCashCom"),
            "cash_report.endingSettledCashCom",
        ),
        broker_interest=_optional_decimal(
            element.attrib.get("brokerInterest"), "cash_report.brokerInterest"
        ),
        broker_interest_sec=_optional_decimal(
            element.attrib.get("brokerInterestSec"), "cash_report.brokerInterestSec"
        ),
        broker_interest_com=_optional_decimal(
            element.attrib.get("brokerInterestCom"), "cash_report.brokerInterestCom"
        ),
        broker_interest_mtd=_optional_decimal(
            element.attrib.get("brokerInterestMTD"), "cash_report.brokerInterestMTD"
        ),
        broker_interest_ytd=_optional_decimal(
            element.attrib.get("brokerInterestYTD"), "cash_report.brokerInterestYTD"
        ),
        reported_account_matches_statement=_account_matches(element, raw_account_id),
    )


def _interest_accrual(element: ET.Element, raw_account_id: str) -> FlexInterestAccrual:
    return FlexInterestAccrual(
        raw_account_id=raw_account_id,
        currency=_text(element.attrib.get("currency"), "interest_accrual.currency"),
        from_date=_day(element.attrib.get("fromDate"), "interest_accrual.fromDate"),
        to_date=_day(element.attrib.get("toDate"), "interest_accrual.toDate"),
        starting_accrual_balance=_optional_decimal(
            element.attrib.get("startingAccrualBalance"),
            "interest_accrual.startingAccrualBalance",
        ),
        interest_accrued=_optional_decimal(
            element.attrib.get("interestAccrued"), "interest_accrual.interestAccrued"
        ),
        accrual_reversal=_optional_decimal(
            element.attrib.get("accrualReversal"), "interest_accrual.accrualReversal"
        ),
        ending_accrual_balance=_optional_decimal(
            element.attrib.get("endingAccrualBalance"),
            "interest_accrual.endingAccrualBalance",
        ),
        fx_translation=_optional_decimal(
            element.attrib.get("fxTranslation"), "interest_accrual.fxTranslation"
        ),
        reported_account_matches_statement=_account_matches(element, raw_account_id),
    )


def _tier_interest(element: ET.Element, raw_account_id: str) -> FlexTierInterest:
    return FlexTierInterest(
        raw_account_id=raw_account_id,
        currency=_text(element.attrib.get("currency"), "tier_interest.currency"),
        interest_type=_text(
            element.attrib.get("interestType"), "tier_interest.interestType"
        ),
        value_date=_day(element.attrib.get("valueDate"), "tier_interest.valueDate"),
        report_date=_day(element.attrib.get("reportDate"), "tier_interest.reportDate"),
        tier_break=_optional(element.attrib.get("tierBreak")),
        balance_threshold=_optional_decimal(
            element.attrib.get("balanceThreshold"), "tier_interest.balanceThreshold"
        ),
        rate=_optional_decimal(element.attrib.get("rate"), "tier_interest.rate"),
        securities_principal=_optional_decimal(
            element.attrib.get("securitiesPrincipal"),
            "tier_interest.securitiesPrincipal",
        ),
        securities_interest=_optional_decimal(
            element.attrib.get("securitiesInterest"), "tier_interest.securitiesInterest"
        ),
        commodities_principal=_optional_decimal(
            element.attrib.get("commoditiesPrincipal"),
            "tier_interest.commoditiesPrincipal",
        ),
        commodities_interest=_optional_decimal(
            element.attrib.get("commoditiesInterest"),
            "tier_interest.commoditiesInterest",
        ),
        ibukl_principal=_optional_decimal(
            element.attrib.get("ibuklPrincipal"), "tier_interest.ibuklPrincipal"
        ),
        ibukl_interest=_optional_decimal(
            element.attrib.get("ibuklInterest"), "tier_interest.ibuklInterest"
        ),
        total_principal=_optional_decimal(
            element.attrib.get("totalPrincipal"), "tier_interest.totalPrincipal"
        ),
        total_interest=_optional_decimal(
            element.attrib.get("totalInterest"), "tier_interest.totalInterest"
        ),
        margin_balance=_optional_decimal(
            element.attrib.get("marginBalance"), "tier_interest.marginBalance"
        ),
        fx_rate_to_base=_optional_decimal(
            element.attrib.get("fxRateToBase"), "tier_interest.fxRateToBase"
        ),
        code=_optional(element.attrib.get("code")),
        from_account=_optional(element.attrib.get("fromAcct")),
        to_account=_optional(element.attrib.get("toAcct")),
        reported_account_matches_statement=_account_matches(element, raw_account_id),
    )


def _transfer(element: ET.Element, raw_account_id: str) -> FlexTransfer:
    return FlexTransfer(
        raw_account_id=raw_account_id,
        event_date=_day(
            element.attrib.get("dateTime") or element.attrib.get("date"),
            "transfer.event_date",
        ),
        nav_recognition_date=_day(
            element.attrib.get("reportDate"), "transfer.reportDate"
        ),
        transaction_id=_text(
            _event_identity(element, "transactionID", "positionInstructionID"),
            "transfer.identity",
        ),
        transfer_type=_text(element.attrib.get("type"), "transfer.type"),
        direction=_text(element.attrib.get("direction"), "transfer.direction"),
        cash_amount=_optional_decimal(
            element.attrib.get("cashTransfer"), "transfer.cashTransfer"
        ),
        position_amount_in_base=_optional_decimal(
            element.attrib.get("positionAmountInBase"),
            "transfer.positionAmountInBase",
        ),
        currency=_text(element.attrib.get("currency"), "transfer.currency"),
        conid=_optional(element.attrib.get("conid")),
        quantity=_optional_decimal(element.attrib.get("quantity"), "transfer.quantity"),
        reported_account_matches_statement=_account_matches(element, raw_account_id),
        event_time=_time_of_day(element.attrib.get("dateTime")),
    )


def _corporate_action(element: ET.Element, raw_account_id: str) -> FlexCorporateAction:
    return FlexCorporateAction(
        raw_account_id=raw_account_id,
        event_date=_day(
            element.attrib.get("dateTime") or element.attrib.get("reportDate"),
            "corporate_action.event_date",
        ),
        identity=_text(
            _event_identity(element, "transactionID", "actionID"),
            "corporate_action.identity",
        ),
        action_type=_text(element.attrib.get("type"), "corporate_action.type"),
        conid=_optional(element.attrib.get("conid")),
        currency=_text(element.attrib.get("currency"), "corporate_action.currency"),
        quantity=_optional_decimal(
            element.attrib.get("quantity"), "corporate_action.quantity"
        ),
        amount=_optional_decimal(
            element.attrib.get("amount") or element.attrib.get("proceeds"),
            "corporate_action.amount",
        ),
        reported_account_matches_statement=_account_matches(element, raw_account_id),
    )


def _rate(element: ET.Element) -> FlexRate:
    return FlexRate(
        report_date=_day(element.attrib.get("reportDate"), "rate.reportDate"),
        from_currency=_text(element.attrib.get("fromCurrency"), "rate.fromCurrency"),
        to_currency=_text(element.attrib.get("toCurrency"), "rate.toCurrency"),
        rate=_decimal(element.attrib.get("rate"), "rate.rate"),
    )


def _parse_statement(element: ET.Element, artifact_id: str) -> FlexDailyStatement:
    raw_account_id = _text(element.attrib.get("accountId"), "statement.accountId")
    start = _day(element.attrib.get("fromDate"), "statement.fromDate")
    end = _day(element.attrib.get("toDate"), "statement.toDate")
    if start != end:
        raise FlexValidationError("only daily FlexStatement slices are accepted")
    observed_sections = {
        _local(item.tag)
        for item in element.iter()
        if _local(item.tag) in _REQUIRED_SECTIONS
    }
    missing = _REQUIRED_SECTIONS - observed_sections
    if missing:
        raise FlexValidationError(f"FlexStatement sections missing: {sorted(missing)}")

    account = _single(element, "AccountInformation")
    nav = _single(element, "EquitySummaryByReportDateInBase")
    change = _single(element, "ChangeInNAV")
    for item in (account, nav, change):
        _require_account_match(item, raw_account_id)
    report_date = _day(nav.attrib.get("reportDate"), "nav.reportDate")
    if report_date != end:
        raise FlexValidationError("NAV reportDate differs from statement date")
    nav_value = _decimal(nav.attrib.get("total"), "nav.total")
    ending_value = _decimal(change.attrib.get("endingValue"), "change.endingValue")
    if nav_value != ending_value:
        raise FlexValidationError("NAV total differs from ChangeInNAV endingValue")
    change_accounting_date = (
        _day(change.attrib.get("toDate"), "change.toDate")
        if _optional(change.attrib.get("toDate")) is not None
        else report_date
    )
    if change_accounting_date != report_date:
        raise FlexValidationError("ChangeInNAV toDate differs from statement date")
    change_components: list[tuple[str, Decimal]] = []
    for key, value in sorted(change.attrib.items()):
        if key in _CHANGE_IN_NAV_NONADDITIVE_ATTRIBUTES:
            continue
        change_components.append((key, _decimal(value, f"change.{key}")))

    positions = tuple(
        _position(item, raw_account_id)
        for item in _elements(element, "OpenPosition")
        if item.attrib.get("levelOfDetail") == "SUMMARY"
    )
    trades = tuple(
        _trade(item, raw_account_id)
        for item in _elements(element, "Trade")
        if item.attrib.get("levelOfDetail") == "EXECUTION"
    )
    cash_reports = tuple(
        _cash_report(item, raw_account_id)
        for item in _elements(element, "CashReportCurrency")
    )
    interest_accruals = tuple(
        _interest_accrual(item, raw_account_id)
        for item in _elements(element, "InterestAccrualsCurrency")
    )
    tier_interest = tuple(
        _tier_interest(item, raw_account_id)
        for item in _elements(element, "TierInterestDetail")
    )
    return FlexDailyStatement(
        artifact_id=artifact_id,
        raw_account_id=raw_account_id,
        report_date=report_date,
        base_currency=_text(account.attrib.get("currency"), "account.currency"),
        nav=nav_value,
        change_in_nav_ending_value=ending_value,
        change_in_nav_accounting_date=change_accounting_date,
        change_in_nav_starting_value=_optional_decimal(
            change.attrib.get("startingValue"), "change.startingValue"
        ),
        change_in_nav_deposits_withdrawals=_optional_decimal(
            change.attrib.get("depositsWithdrawals"),
            "change.depositsWithdrawals",
        ),
        change_in_nav_asset_transfers=_optional_decimal(
            change.attrib.get("assetTransfers"), "change.assetTransfers"
        ),
        change_in_nav_internal_cash_transfers=_optional_decimal(
            change.attrib.get("internalCashTransfers"),
            "change.internalCashTransfers",
        ),
        change_in_nav_mtm=_optional_decimal(change.attrib.get("mtm"), "change.mtm"),
        change_in_nav_components=tuple(change_components),
        positions=positions,
        trades=trades,
        cash_transactions=tuple(
            _cash_transaction(item, raw_account_id)
            for item in _elements(element, "CashTransaction")
        ),
        transfers=tuple(
            _transfer(item, raw_account_id) for item in _elements(element, "Transfer")
        ),
        corporate_actions=tuple(
            _corporate_action(item, raw_account_id)
            for item in _elements(element, "CorporateAction")
        ),
        rates=tuple(_rate(item) for item in _elements(element, "ConversionRate")),
        cash_reports=cash_reports,
        interest_accruals=interest_accruals,
        tier_interest=tier_interest,
        nav_cash=_optional_decimal(nav.attrib.get("cash"), "nav.cash"),
        nav_broker_cash_component=_optional_decimal(
            nav.attrib.get("brokerCashComponent"), "nav.brokerCashComponent"
        ),
        nav_interest_accruals=_optional_decimal(
            nav.attrib.get("interestAccruals"), "nav.interestAccruals"
        ),
        nav_broker_interest_accruals_component=_optional_decimal(
            nav.attrib.get("brokerInterestAccrualsComponent"),
            "nav.brokerInterestAccrualsComponent",
        ),
        nav_margin_financing_charge_accruals=_optional_decimal(
            nav.attrib.get("marginFinancingChargeAccruals"),
            "nav.marginFinancingChargeAccruals",
        ),
    )


def _reject_declarations(path: Path) -> None:
    carry = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            observed = (carry + chunk).upper()
            if b"<!DOCTYPE" in observed or b"<!ENTITY" in observed:
                raise FlexValidationError(
                    "DTD and entity declarations are not accepted"
                )
            carry = observed[-16:]


def parse_flex_file(path: Path, *, artifact_id: str) -> FlexDocument:
    """Parse one complete Activity Flex XML file without retaining raw XML."""

    _reject_declarations(path)
    try:
        iterator = ET.iterparse(path, events=("start", "end"))
        root: ET.Element | None = None
        statements: list[FlexDailyStatement] = []
        query_name_present = False
        for event, element in iterator:
            tag = _local(element.tag)
            if root is None and event == "start":
                root = element
                if tag != "FlexQueryResponse" or element.attrib.get("type") != "AF":
                    raise FlexValidationError(
                        "root must be an Activity FlexQueryResponse"
                    )
                query_name_present = bool(_optional(element.attrib.get("queryName")))
            if event == "end" and tag == "FlexStatement":
                statements.append(_parse_statement(element, artifact_id))
                element.clear()
    except ET.ParseError as exc:
        raise FlexValidationError("Flex XML is malformed or truncated") from exc
    if root is None or not statements:
        raise FlexValidationError("Flex response contains no statements")
    if not query_name_present:
        raise FlexValidationError("Flex response queryName is missing")
    return FlexDocument(
        artifact_id=artifact_id,
        query_name_present=True,
        statements=tuple(statements),
    )


class HttpGet(Protocol):
    def __call__(
        self, url: str, params: Mapping[str, str], headers: Mapping[str, str]
    ) -> bytes: ...


@dataclass(frozen=True)
class FlexFetchResult:
    report: bytes
    request_attempts: int


def _service_response(value: bytes) -> tuple[str, str | None, str | None]:
    upper = value.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise FlexValidationError("Flex service response contains a DTD or entity")
    try:
        root = ET.fromstring(value)
    except ET.ParseError as exc:
        raise FlexValidationError("Flex service response is malformed") from exc
    if _local(root.tag) == "FlexQueryResponse":
        return "REPORT", None, None
    if _local(root.tag) != "FlexStatementResponse":
        raise FlexValidationError("unexpected Flex service response root")
    fields = {_local(item.tag): (item.text or "").strip() for item in root}
    status = fields.get("Status", "")
    if status == "Success":
        return "SUCCESS", _optional(fields.get("ReferenceCode")), None
    return "FAIL", None, _optional(fields.get("ErrorCode"))


class FlexWebServiceClient:
    """Two-step Flex client with injected credentials and no default network path."""

    def __init__(
        self,
        *,
        credential_provider: Callable[[], str],
        http_get: HttpGet,
        wait: Callable[[float], None],
        user_agent: str,
    ) -> None:
        self._credential_provider = credential_provider
        self._http_get = http_get
        self._wait = wait
        self._user_agent = _text(user_agent, "user_agent")

    def fetch_report(
        self, query_id: str, *, max_get_attempts: int = 5
    ) -> FlexFetchResult:
        query = _text(query_id, "query_id")
        if max_get_attempts < 1 or max_get_attempts > 9:
            raise FlexValidationError("max_get_attempts must be between 1 and 9")
        service_parameter = _text(self._credential_provider(), "credential")
        headers = {"User-Agent": self._user_agent}
        send = self._http_get(
            SEND_REQUEST_URL,
            {"q": query, "t": service_parameter, "v": "3"},
            headers,
        )
        status, reference, error = _service_response(send)
        if status != "SUCCESS" or reference is None:
            raise FlexValidationError(
                f"Flex SendRequest failed with code {error or 'UNKNOWN'}"
            )
        for attempt in range(1, max_get_attempts + 1):
            delay = min(
                _POLL_INITIAL_DELAY_SECONDS * (2 ** (attempt - 1)),
                _POLL_MAX_DELAY_SECONDS,
            )
            self._wait(delay)
            response = self._http_get(
                GET_STATEMENT_URL,
                {"q": reference, "t": service_parameter, "v": "3"},
                headers,
            )
            status, _, error = _service_response(response)
            if status == "REPORT":
                return FlexFetchResult(report=response, request_attempts=attempt)
            if error != "1019":
                raise FlexValidationError(
                    f"Flex GetStatement failed with code {error or 'UNKNOWN'}"
                )
        raise FlexValidationError(
            "Flex statement generation did not complete in bounds"
        )
