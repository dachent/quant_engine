"""Assemble derivation_a expected values from the upstream inputs.

Inputs (relative to ``--qe-root``) are fixed below; nothing else under the qe
root is opened. Definitions come from ``contracts/`` in this repository.
Conventions R1-R7 are the orchestrator rulings appended to the acceptance metric
spec (state/briefs/acceptance-metric-spec.md).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import Any, cast

from . import DERIVATION, SCHEMA
from .numerics import (
    BOD,
    ONE,
    PRECISION,
    ZERO,
    CalendarMisalignmentError,
    IrrResult,
    Member,
    UndefinedReturnError,
    composite_subperiods,
    investor_cash_flows,
    subperiod_return,
    time_weighted_return,
    xirr,
)
from .sources import (
    INCLUDED,
    AccountFlows,
    Admission,
    Assignment,
    Bill,
    Crosswalk,
    Flow,
    InputError,
    InputFile,
    Ledger,
    MatrixportWallet,
    QuantbaseSleeve,
    UnusableFlow,
    csv_rows,
    decimal_or_none,
    iso_date,
    parse_crosswalk,
    parse_flow_policy,
    parse_fx_history,
    parse_ledger,
    parse_matrixport_wallet,
    parse_membership_status,
    parse_quantbase_sleeve,
    parse_quantbase_values,
    read_input,
)

LEDGER = (
    "dataPortfolio/provider-ledger/generations/"
    "3b1445eee410890d1e0d9259ad7543011959208f6091e059eb4daa7c1c8c5a1f/"
    "provider-ledger.json"
)
CROSSWALK = (
    "dataPortfolio/reporting/generations/"
    "d14355537c5e6eeaeb8716c4fe58f4bea2c2b11e3b19a14824359389727509ae/"
    "input/account-strategy-current-epoch-crosswalk.json"
)
MATRIXPORT_CSV = "dataMatrixport/output/20260903T051029.331Z/csv/"
MP_NAV = MATRIXPORT_CSV + "nav_daily.csv"
MP_DEPOSITS = MATRIXPORT_CSV + "wallet_deposits.csv"
MP_BILLS = MATRIXPORT_CSV + "wallet_bills.csv"
MP_DISPOSITIONS = MATRIXPORT_CSV + "wallet_currency_dispositions.csv"
QUANTBASE_CSV = "dataQuantbase/source-runs/refresh-20260902/csv_full/"
QB_ACCOUNTS = QUANTBASE_CSV + "accounts.csv"
QB_ACTIVITY = QUANTBASE_CSV + "account_activity.csv"
# R6 needs raw Quantbase daily values; this raw file of the same capture is the
# source the provider ledger itself cites for the Volatis sleeve.
QB_VALUES = QUANTBASE_CSV + "dashboard_Volatis_Strategy_pricehistory.csv"
# X1: raw Yahoo capture of the contract-bound FX generation 9812f253 (resident;
# reconstructed byte-exact, state/reports/FX-9812f253-report.md), as listed in
# state/briefs/acceptance-inputs.json. Only dates <= the admitted boundary are used.
FX_HISTORY = (
    "dataPortfolio/source-runs/portfolio-fx-yahoo-candidate-20260906T234500Z/"
    "raw/yfinance-history.json"
)

INPUT_ORDER = (
    LEDGER,
    CROSSWALK,
    MP_NAV,
    MP_DEPOSITS,
    MP_BILLS,
    MP_DISPOSITIONS,
    QB_ACCOUNTS,
    QB_ACTIVITY,
    QB_VALUES,
    FX_HISTORY,
)
# Identities pinned by state/briefs/acceptance-inputs.json (LEDGER, CROSSWALK,
# FX_HISTORY) plus the remaining seven consumed sources (T2.8, PR #487 bot
# finding 4113723389): acceptance-inputs.json's v1 record carries a null
# sha256 for these -- captured before the census filled it in -- so there was
# no approved digest to copy. Each was independently computed by sha256'ing
# the exact resident bytes at the path already named in INPUT_ORDER (the same
# identity read_input() would otherwise have trusted unverified); see
# state/reports/T2.8-report.md for how each was derived. Without a pin here,
# read_input() skips verification entirely for that path (only a path
# present in this mapping is checked), so any of these seven could be
# replaced in place and the derivation would silently calculate a new,
# apparently-valid expected result from unapproved bytes.
PINNED_SHA256 = {
    LEDGER: "ba5232acadbbd192e60f1a1b9635cfab05900ac024b0064bdc782a57079d5f13",
    CROSSWALK: "2d1035cabfecdd4c1b53c8f03c6db97417ee27e43b14e8669e5d8b9452ac905f",
    MP_NAV: "f067dbcc382e85154fabd7730c1b9573abfb39ef0817ac9013d3f40867df0c16",
    MP_DEPOSITS: "e5a8926bf0e9dce277d02e0cf9dc934a206e32357b099eaacf4d1680165ca339",
    MP_BILLS: "c8fd298586aea4c4fcf2d8633da8245d057a5b8a849a344923a411ca9c6437f1",
    MP_DISPOSITIONS: "acb2ffc16d2b28aa0e9938494d4990141e87c3eeae1c6c3ace31fad3f6ec4a8a",
    QB_ACCOUNTS: "3fa85ee87de511fe2350e3fea9f1568b3f8506e5e08ef017eacd4d214cdf2385",
    QB_ACTIVITY: "f1960b5338b3db15cff7a98cc8cde20b7a6d09a9bd77e6341657cfb35f968bd0",
    QB_VALUES: "7b12647cbe0b728dcd4808bccf009ee2eaf46569f34cee57963b2eb87fcb749e",
    FX_HISTORY: "416cc69b4e525e2dfba19ed6ffc324527dec8e358f9a886d1a201690c3057b4c",
}
# An acceptance-inputs manifest (state/briefs/acceptance-inputs*.json) names each
# input by path; the role is the file's basename. The account-strategy manifest is
# a production output and is never read (the cutoff is the spec's fixed fact).
ROLE_BY_BASENAME = {
    "provider-ledger.json": LEDGER,
    "account-strategy-current-epoch-crosswalk.json": CROSSWALK,
    "nav_daily.csv": MP_NAV,
    "wallet_deposits.csv": MP_DEPOSITS,
    "wallet_bills.csv": MP_BILLS,
    "wallet_currency_dispositions.csv": MP_DISPOSITIONS,
    "accounts.csv": QB_ACCOUNTS,
    "account_activity.csv": QB_ACTIVITY,
    "dashboard_Volatis_Strategy_pricehistory.csv": QB_VALUES,
    "yfinance-history.json": FX_HISTORY,
}
IGNORED_BASENAMES = frozenset({"account-strategy-manifest.json"})
# Only this manifest schema is accepted: a newer version may redefine field
# semantics (paths, sha256, bytes, cutoff) and must not be silently parsed.
INPUTS_MANIFEST_SCHEMA = "qe-cp-acceptance-inputs/2"
PERFORMANCE_POLICY = "contracts/portfolio-performance-policy-v1.json"
MVP_CONTRACT = "contracts/portfolio-financial-mvp-source-v1.json"
FLOW_POLICY = "contracts/account-strategy-flow-classification-policy-v1.json"
CONTRACTS = (PERFORMANCE_POLICY, MVP_CONTRACT, FLOW_POLICY)
DECLARED_CUTOFF = dt.date(2026, 9, 17)

# --------------------------------------------------------------------- v3 (R6 v3 / R8)
#
# Everything below this marker engages only for a manifest whose schema is
# exactly ``qe-cp-acceptance-inputs/3``; the v1 built-in table and the v2
# manifest path are byte-for-byte unchanged. v3 resolves inputs by authority
# family (not basename), validates the selected contract / FX generation /
# daily-rates / provider-capital policy / paired owner receipts / posted
# response readback together, and adds the R8 custody cash derivation for the
# two exactly bound custody strategies.

V3_INPUTS_MANIFEST_SCHEMA = "qe-cp-acceptance-inputs/3"
# v3-only authority roles. These are role keys, never default filesystem
# locations: under v3 every one of them is read from the pinned manifest path.
FX_GENERATION = "v3-authority:fx-generation-manifest"
FX_USDC = "v3-authority:fx-usdc-usd-daily"
FX_RATES = "v3-authority:fx-daily-rates"
CAPITAL_POLICY = "v3-authority:provider-capital-policy"
RECEIPT_A = "v3-authority:provider-capital-owner-receipt-a"
RECEIPT_B = "v3-authority:provider-capital-owner-receipt-b"
RECEIPT_B_RESPONSE = "v3-authority:provider-capital-owner-receipt-b-response"
V3_AUTHORITY_ORDER = (
    MVP_CONTRACT,
    FX_GENERATION,
    FX_USDC,
    FX_RATES,
    CAPITAL_POLICY,
    RECEIPT_A,
    RECEIPT_B,
    RECEIPT_B_RESPONSE,
)
# Native Matrixport confirmed-source interface
# (native-matrixport-confirmed-source-interface/1), v3 only. When any of the
# five families is selected, all five are required and the native source path
# replaces the bill-heuristic path for the Matrixport custody account; the
# original 19-input v3 selection without them keeps the prior behavior.
MP_NATIVE_GENERATION = "v3-native:matrixport-generation-manifest"
MP_NATIVE_SUBSCRIPTION = "v3-native:matrixport-subscription-details"
MP_NATIVE_CATALOG = "v3-native:matrixport-generation-catalog"
MP_NATIVE_COVERAGE = "v3-native:matrixport-owner-flow-coverage"
MP_NATIVE_REQUESTS = "v3-native:matrixport-owner-flow-request-manifest"
MP_NATIVE_ROLES = (
    MP_NATIVE_GENERATION,
    MP_NATIVE_SUBSCRIPTION,
    MP_NATIVE_CATALOG,
    MP_NATIVE_COVERAGE,
    MP_NATIVE_REQUESTS,
)
# Generation artifact id -> derivation role: the three native outputs plus the
# four Matrixport CSVs, all hash/size-bound to the one selected generation.
MP_NATIVE_ARTIFACT_ROLES = {
    "csv/subscription_details.csv": MP_NATIVE_SUBSCRIPTION,
    "csv/coverage.csv": MP_NATIVE_COVERAGE,
    "reports/request_manifest.jsonl": MP_NATIVE_REQUESTS,
    "csv/nav_daily.csv": MP_NAV,
    "csv/wallet_deposits.csv": MP_DEPOSITS,
    "csv/wallet_bills.csv": MP_BILLS,
    "csv/wallet_currency_dispositions.csv": MP_DISPOSITIONS,
}
MP_NATIVE_WALLET_ENDPOINTS = (
    "GET https://mapi.matrixport.com/mapi/v1/wallet/deposits",
    "GET https://mapi.matrixport.com/mapi/v1/wallet/withdrawals",
)
MP_NATIVE_ORDER_ENDPOINT = "/strategy/api/v2/order"
MP_NATIVE_TERMINAL_STATUS = "300"
# Native wallet deposits admit these normalized terminal states.
MP_NATIVE_DEPOSIT_STATES = frozenset({"completed", "confirmed", "success"})

V3_SINGLETON_FAMILY_ROLES = {
    "financial_source_contract": MVP_CONTRACT,
    "fx_generation_manifest": FX_GENERATION,
    "fx_usdc_usd_daily": FX_USDC,
    "fx_usdt_usd_daily": FX_HISTORY,
    "fx_daily_rates": FX_RATES,
    "provider_capital_policy": CAPITAL_POLICY,
    "provider_capital_owner_receipt_a": RECEIPT_A,
    "provider_capital_owner_receipt_b": RECEIPT_B,
    "provider_capital_owner_receipt_b_response": RECEIPT_B_RESPONSE,
    "matrixport_generation_manifest": MP_NATIVE_GENERATION,
    "matrixport_subscription_details": MP_NATIVE_SUBSCRIPTION,
    "matrixport_generation_catalog": MP_NATIVE_CATALOG,
    "matrixport_owner_flow_coverage": MP_NATIVE_COVERAGE,
    "matrixport_owner_flow_request_manifest": MP_NATIVE_REQUESTS,
}
V3_BASENAME_FAMILY_ROLES = {
    "portfolio_and_strategy_capital_at_cutoff": {
        "provider-ledger.json": LEDGER,
        "nav_daily.csv": MP_NAV,
        "accounts.csv": QB_ACCOUNTS,
    },
    "account_strategy_map_and_active_universe": {
        "account-strategy-current-epoch-crosswalk.json": CROSSWALK,
    },
    "per_account_strategy_daily_values_and_flows_twr_mwr": {
        "account_activity.csv": QB_ACTIVITY,
        "wallet_deposits.csv": MP_DEPOSITS,
        "wallet_bills.csv": MP_BILLS,
        "wallet_currency_dispositions.csv": MP_DISPOSITIONS,
    },
    "per_account_strategy_daily_values_and_flows": {
        "dashboard_Volatis_Strategy_pricehistory.csv": QB_VALUES,
    },
}
# The declared-cutoff entry (a production output) is recorded but never read,
# exactly like the ignored basenames on the v1/v2 paths.
V3_IGNORED_FAMILIES = frozenset({"declared_cutoff_date"})
V3_REQUIRED_ROLES = tuple(INPUT_ORDER) + V3_AUTHORITY_ORDER
V3_RESPONSE_SCHEMA = "github-issue-comment/REST"
# R8: only these exactly bound custody strategies participate in the custody
# cash derivation (strategy_id -> required provider of its single account).
V3_CUSTODY_PROVIDERS = {"volatis-strategy": "QUANTBASE", "robuxio-crypto": "MATRIXPORT"}
CASH_METRIC = "capital.strategy_cash.cutoff"
# One row per custody cash line (entity <strategy>|<provider>|<account>): the
# unclamped date through which every admission the line is derived from attests it.
COVERAGE_METRIC = "date.strategy_cash.coverage_end"
INVESTED_METRIC = "invested_capital.strategy.cutoff"
WEIGHT_METRIC = "weight.strategy.cutoff"
# Raw Quantbase activity rows: deposits/withdrawals are owner funding, fund
# fills are allocations (parsed by the sleeve parser), orders/dividends are
# recognized non-funding rows; anything else is an unsupported custody event.
QUANTBASE_CUSTODY_KNOWN_TYPES = frozenset(
    {"filled_txn", "order", "dividend", "deposit", "withdrawal"}
)
# Native Quantbase owner funding is a completed transfer: the custody account
# is identified by destination_account_id (deposit) or origin_account_id
# (withdrawal).
QUANTBASE_TRANSFER_TYPES = {
    "DEPOSIT": "destination_account_id",
    "WITHDRAWAL": "origin_account_id",
}
# Known internal fee journals (activity_type journal, status COMPLETE, type
# PARTNER_SUB_FEE/AUM_FEE) are investment results outside the owner/allocation
# legs: the owner cash residual is DERIVED (admitted deposits - withdrawals -
# sleeve allocations + admitted redemptions), so no journal-to-sale pairing,
# cash-neutrality, or namespace reconciliation is required of them. Any other
# journal row (or the fee types on a non-journal row) fails R8 closed.
QUANTBASE_FEE_JOURNAL_TYPES = frozenset({"PARTNER_SUB_FEE", "AUM_FEE"})
# A raw Matrixport stablecoin debit is an owner withdrawal only when its
# tx_type says so; any other unpaired non-conversion bill is unsupported.
MATRIXPORT_WITHDRAWAL_TX_TYPES = frozenset({"withdrawal"})
# The known tx_type of a strategy subscription (an unpaired product-currency
# debit); under v3 any other unpaired product-currency debit is an owner
# withdrawal only when withdrawal-typed, else explicitly unsupported. The
# v1/v2 classifier keeps treating every unpaired product-currency debit as a
# subscription.
MATRIXPORT_SUBSCRIPTION_TX_TYPE = "2855"
# Native FX generation interface: artifact schema (name, version) records and
# the daily-rates shape. The "/1" spellings are documentary tags only.
FX_RAW_ARTIFACT_SCHEMA = ("amdt-yfinance-history", "1")
FX_RATES_ARTIFACT_SCHEMA = ("portfolio-fx-daily-close-rates", "1")
FX_RATES_HEADER = (
    "date",
    "currency",
    "base_currency",
    "rate",
    "provider",
    "ticker",
    "field",
    "timing",
)
FX_TICKER_BY_CURRENCY = {"USDT": "USDT-USD", "USDC": "USDC-USD"}
# Native approved-decision / compiled-receipt / readback-record shapes.
POLICY_DECISION_KEYS = frozenset(
    {
        "approval_evidence",
        "calculation_effect",
        "decision_authority",
        "decision_status",
        "examples",
        "plain_language",
        "recommended_treatment",
        "selected_treatment",
    }
)
POLICY_DECISION_AUTHORITIES = frozenset({"OWNER_318", "SHARED_FLOW_POLICY_324"})
COMPILED_RECEIPT_KEYS = frozenset(
    {
        "approved_on",
        "authority_issue",
        "comment_id",
        "decision_id",
        "digest_comment_id",
        "digest_evidence_url",
        "evidence_url",
        "payload_sha256",
        "policy_owner_issue",
    }
)
CUSTODY_DECISION_ID = "PROVIDER_CUSTODY_PERIMETER"
RECEIPT_RECORD_MARKER = "<!-- financial-mvp-owner-decision-receipts-v1 -->"
RECEIPT_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "authority_issue",
        "policy_owner_issue",
        "policy_id",
        "approval_evidence_url",
        "approved_on",
        "decision_payload_sha256",
    }
)


V3_CONVENTION_NOTES = (
    "R6 v3 FX authority: the exact owner-selected generation bound together by "
    "the v3 manifest families fx_generation_manifest / fx_usdc_usd_daily / "
    "fx_usdt_usd_daily / fx_daily_rates and the pinned financial_source_contract "
    "(the contract's portfolio-fx-rates current_generation id and sha256 and its "
    "daily-rates selector pin must match the selected bytes, all four FX inputs "
    "must live under the same generations/<id> directory, and each selected "
    "file's sha256/size must match its generation artifact record under the "
    "native artifact schemas). Raw USDC-USD and USDT-USD daily closes are used "
    "on original UTC event/NAV dates inside the contract's observed interval "
    "only, and each used currency/date must carry a supported daily-rates "
    "mapping (USD base, exact ticker, Close field); a missing close or mapping "
    "fails closed. No interpolation, parity assumption, date relabeling, or "
    "decimal equality against the serialized rates. The v2 FX boundary "
    "behavior applies to v2 selections only.",
    "R8 custody: only the exactly bound custody strategies volatis-strategy "
    "(QUANTBASE) and robuxio-crypto (MATRIXPORT) get owner-cash rows. "
    "invested_capital.strategy.cutoff is owner deposits minus withdrawals, each "
    "converted once at the original UTC event-date close of the selected FX "
    "generation; fund purchases/subscriptions are allocations inside the "
    "strategy with zero invested-capital effect; capital.strategy_cash.cutoff is "
    "net funding less net allocations; capital.strategy.cutoff is the observed "
    "member NAV plus that separate cash line, which earns no fund return. "
    "Fund-only TWR, MWR, HWM and drawdown keep the existing NAV/allocation "
    "path, and capital.account.cutoff remains observed NAV. Cash strictly below "
    "zero on any observed event date fails closed before output rounding or "
    "tolerance. Missing funding, an ambiguous account/strategy binding, "
    "unsupported transfers/redemptions, or unproved custody authority make the "
    "affected invested-capital/cash/strategy-capital rows and the dependent "
    "systematic total and weight.strategy.cutoff rows NOT_DERIVABLE -- never a "
    "partial numeric total and never a zero stand-in.",
    "R8 coverage end: date.strategy_cash.coverage_end (entity "
    "<strategy>|<provider>|<account>) is, for each custody cash line, the end "
    "of the contiguous provider-ledger admission segment that holds the "
    "earliest event, taken separately for owner flows and for sleeve "
    "allocations (capital anchors) over the AUTHORITATIVE ACCOUNT_LIVE "
    "windows of that provider and account, windows whose start is at most one "
    "day after the previous end joining one segment, and the smaller of the "
    "two ends. It is unclamped (it may follow the cutoff). A missing window, "
    "an earliest event outside every window, or a withheld cash line makes "
    "the row NOT_DERIVABLE.",
    "v3 authority: the provider-capital policy must be an approved native "
    "decision (eight keys, APPROVED, owner authority) structurally equal to "
    "the selected contract's provider_custody_perimeter; custody strategies "
    "and accounts are qualified by the contract census (account_sleeves and "
    "provider_lanes) plus the crosswalk, never by policy prose. The compiled "
    "owner receipt must equal the contract's owner_decision_receipts entry and "
    "carry the canonical policy payload digest; comment A binds its "
    "comment_id/evidence_url and the posted response readback "
    "(provider_capital_owner_receipt_b_response, schema github-issue-comment/"
    "REST) binds its digest_comment_id/digest_evidence_url and carries the "
    "canonical policy payload digest in its single fenced machine record. "
    "Receipt and response bodies/prose are never emitted in notes or metric "
    "rows. R9 reserve scope stays deferred: capital.reserve.cutoff and "
    "capital.portfolio.liquid.cutoff remain not derivable.",
)

FUNDED = "FUNDED_MEMBERSHIP"
MATRIXPORT_PRODUCT_CURRENCY = "USDT"
STABLECOINS = frozenset({"USDT", "USDC"})
CONVERSION_WINDOW_MS = 600_000
CONVERSION_TOLERANCE = Decimal("0.01")
WINDOW = "ITD"
CENT = Decimal("0.01")
RATIO_QUANTUM = Decimal("1e-12")

ACCOUNT_METRICS = (
    "capital.account.cutoff",
    "date.source_last_observation",
    "flows.account.net",
    "flows.account.count",
    "twr.account",
    "mwr.account",
)
STRATEGY_METRICS = ("capital.strategy.cutoff", "twr.strategy", "mwr.strategy")
PORTFOLIO_METRICS = (
    "capital.portfolio.systematic.cutoff",
    "capital.portfolio.liquid.cutoff",
    "capital.retained_excluded.cutoff",
    "universe.active_strategy_ids",
    "universe.active_strategy_count",
)
WINDOWED = {
    "flows.account.net",
    "flows.account.count",
    "twr.account",
    "mwr.account",
    "twr.strategy",
    "mwr.strategy",
}

CONVENTION_NOTES = (
    "R7 cutoff gate: the declared cutoff 2026-09-17 (metric spec fixed fact) must "
    "equal the selected cutoff, crosswalk.calculation_cutoff_date, crosswalk."
    "as_of_date and provider-ledger.as_of_date; any mismatch fails closed and every "
    "metric is emitted as not_derivable. The account-strategy manifest is a "
    "production output and is not read.",
    "Windows: contracts/portfolio-performance-policy-v1.json defines no window ids; "
    "only ITD is derived (spec minimum).",
    "R1/R2 membership: contracts/portfolio-hwm-policy-v4.json membership_policy."
    "basis = EFFECTIVE_FUNDED_MEMBERSHIP. Each INCLUDED_PRODUCT_EPOCH crosswalk row "
    "takes the status of the contracts/portfolio-financial-mvp-source-v1.json "
    "current_census.account_sleeves[].assignments[].status with the same (provider, "
    "strategy_id, effective_start, effective_end); sleeves are redacted, so the "
    "join is by interval. Only FUNDED_MEMBERSHIP rows enter account or strategy "
    "windows, the active universe and strategy capital; ASSIGNMENT_ONLY rows never "
    "do; a row with no unique status fails closed.",
    "R1 ITD start S = effective_start of the entity's first FUNDED_MEMBERSHIP "
    "interval. Capital enters at beginning of day S (contracts/portfolio-financial-"
    "mvp-source-v1.json golden_case.inputs.calculation_configuration."
    "initial_contributions_timing and internal_transfers_timing = "
    "BEGINNING_OF_DAY_BEFORE_RETURN), so the opening point is the last observation "
    "before S when its value is positive, otherwise the first positive observation "
    "on or after S. Earlier observations are excluded. Openings are compared with "
    "provider-ledger OPENING_CAPITAL_ANCHOR (nav_recognition_date, opening_nav).",
    "R4 flow dating: provider-ledger nav_recognition_date where present, else "
    "event_date (the date used is counted in each row's notes). Flow timing per "
    "contracts/portfolio-financial-mvp-source-v1.json flow_timing."
    "selected_treatment = EXACT_TIMESTAMP_BOUNDARY_WHEN_AVAILABLE; "
    "DATE_ONLY_END_OF_DAY_WITH_INFERRED_TIMING: date-only flows are EOD and belong "
    "to the sub-period ending at the first observation on or after that date. "
    "Flows on or before the opening point are embedded in the opening value.",
    "Sub-period return: contracts/portfolio-performance-policy-v1.json "
    "twr_cases[date_only_end_of_day] -> (ending - EOD flow) / (beginning + BOD "
    "flow) - 1; chain-link per twr_cases[exact_timestamp_segments]. TWR is "
    "cumulative, not annualized. R3: a sub-period with zero beginning and zero "
    "ending value (net of EOD flows) is neutral (return 0); a sub-period cannot "
    "start at zero capital without a same-day inflow in its opening, so zero or "
    "negative capital with a non-zero result makes the TWR not derivable.",
    "Strategy TWR/MWR sum FUNDED member values and flows on the union of member "
    "observation dates without filling; membership start/end are capital "
    "transfers at those end-of-day points. This equals composite_twr_cases"
    "[beginning_capital_weighted].",
    "MWR = XIRR on investor cash flows (opening value paid in, deposits paid in, "
    "withdrawals received, terminal value received), same-date flows netted, "
    "contracts/portfolio-performance-policy-v1.json mwr_cases day_count = ACTUAL_365; "
    "annualized rate. Uniqueness is proved with Laguerre's partial-sum rule "
    "(r > 0 forward, -1 < r < 0 reversed, plus r = 0) and the end-point signs; "
    "otherwise NO_ROOT or UNIQUENESS_NOT_PROVED per mwr_cases.",
    "R5 flows: a provider-ledger flow enters only if contracts/account-strategy-"
    "flow-classification-policy-v1.json event_dispositions classifies its flow_type "
    "(exact[].flow_type: EXTERNAL_CONTRIBUTION, EXTERNAL_WITHDRAWAL, "
    "EXTERNAL_PROVIDER_TRANSFER, DEPOSIT_OR_WITHDRAWAL = EXTERNAL; ACCOUNT_TRANSFER "
    "= INTERNAL; prefix[].flow_type_prefix TRANSFER:INTERNAL: = INTERNAL) and its "
    "amount satisfies amount_sign; any other in-window flow fails closed. INTERNAL "
    "transfers are entity-level flows for accounts and strategies "
    "(classification_treatments.INTERNAL_TRANSFER.selected_treatment = "
    "BY_STRATEGY_OFFSET_AND_AGGREGATE_ZERO); they net to zero only in aggregate.",
    "Fees, taxes, dividends and interest are investment results, not flows: "
    "contracts/portfolio-financial-mvp-source-v1.json classification_treatments."
    "FEE_TAX_REBATE_EXPENSE.selected_treatment = NO_EFFECT_WHEN_INVESTMENT_RESULT "
    "and DIVIDEND_INTEREST_REINVESTMENT.selected_treatment = "
    "NO_INVESTED_CAPITAL_EFFECT. The provider-ledger flows array carries none of "
    "them; raw Quantbase fee sales, journals and dividends are excluded on this "
    "basis.",
    "IBKR/Schwab/Alpaca/Fidelity values and flows: provider-ledger nav_observations "
    "and classified flows (AVAILABLE USD amounts).",
    "R6 Quantbase (admission under test): values from raw "
    "dashboard_Volatis_Strategy_pricehistory.csv (the file the ledger cites as "
    "source_artifact_id; added to the listed inputs to satisfy R6), flows from raw "
    "account_activity.csv for the fund whose title matches the crosswalk label "
    "(fund BUY +, SELL/MAX_S -, created_to_cover_fees sales = fee, RBLNC = "
    "internal). Fills carry exact timestamps (EXACT_TIMESTAMP_BOUNDARY_WHEN_"
    "AVAILABLE): a fill whose last order filled on UTC date F is absent from the "
    "value dated F and held, at the next close, in the value dated F+1, so it "
    "starts that sub-period: r = ending / (beginning + flow) - 1; MWR dates it F. "
    "Ledger values and ledger account deposits are cross-checks only.",
    "R6 Matrixport (admission under test): the entity is the strategy holding "
    "(the ledger value basis excludes the wallet). Values = units x raw "
    "nav_daily.csv nav (fee-net) x USDT->USD. Raw wallet_bills.csv classes: a "
    "conversion is a USDT<->USDC debit and the other stablecoin's credit within 10 "
    "minutes after it whose amounts agree within 1 %, when that credit is the only "
    "such candidate (bills share no transaction id); several matching or "
    "non-stablecoin counterparts are ambiguous and fail closed; both conversion "
    "legs stay in the wallet. An unpaired USDT debit is a strategy subscription - a "
    "holding inflow of amount x USDT->USD, EOD at its confirm date (first nav_daily "
    "day_timestamp_ms after the bill), adding amount / confirm NAV units; other "
    "currencies' bills are wallet-level; an unpaired USDT credit (e.g. redemption) "
    "is unsupported and fails closed in a window. Raw wallet_deposits.csv deposits "
    "are wallet-level. "
    "wallet_currency_dispositions.csv proves which currencies' history was "
    "readable.",
    "X1 FX: USDT->USD = Yahoo USDT-USD daily Close on the original UTC NAV or "
    "confirm date from the raw yfinance-history.json capture of the contract-bound "
    "FX generation 9812f253 listed in acceptance-inputs, dates <= the admitted "
    "boundary (matrixport-account-generation splice_policy USD_CONVERSION_..._THROUGH) "
    "(contract source_segments[portfolio-fx-rates].splice_policy "
    "ORIGINAL_UTC_DATE; NO_PARITY_INTERPOLATION_OR_FILL); a date without a rate is "
    "dropped. Each Matrixport row carries an fx_provenance note and a cross-check "
    "against the ledger valuation_fx copy.",
    "Inputs: with --inputs, every input path and sha256 (and bytes when listed) "
    "comes from the acceptance-inputs manifest, the role being the file's "
    "basename; an unplaceable entry, a role named by two paths, or a used path "
    "without one pinned sha256 fails closed, as does a manifest cutoff other than "
    "the declared cutoff (R7). Without --inputs the built-in v1 table and pins are "
    "used. The account-strategy manifest entry is a production output and is not "
    "read.",
    "R6 v2: raw = the finalized capture selected by the release run-observation "
    "(acceptance-inputs-v2). Matrixport FX boundary only: raw NAV dates after "
    "the admitted FX boundary are reported in notes as FX-unconverted with no "
    "USD value, and that account's date.source_last_observation is the last raw "
    "(FX-unconverted) NAV date while capital is the last USD-converted valuation "
    "with its true as_of; every other source's date.source_last_observation is "
    "its last value observation on or before the cutoff.",
    "R6 ledger valuations after the last raw observation are not expected values; "
    "their count and last date/value are reported in notes. Capital at cutoff for "
    "Quantbase/Matrixport = last raw observation on or before the cutoff with its "
    "true as_of.",
    "Money: Decimal sums, rounded once to cents (ROUND_HALF_UP). Returns: "
    "Decimal (60 digits), quantized to 1e-12 (ROUND_HALF_UP).",
)


@dataclass(frozen=True)
class InputsManifest:
    """A verified acceptance-inputs manifest resolved to derivation roles."""

    path: str
    sha256: str
    size: int
    schema: str
    cutoff: dt.date | None
    layout: Mapping[str, str]
    pins: Mapping[str, str]
    sizes: Mapping[str, int]
    ignored: tuple[str, ...]
    # v3 native Matrixport selection evidence (None unless the five native
    # families are selected; then the manifest's
    # generation_selection_evidence.matrixport object).
    selection: Any = None


def load_inputs_manifest(data: bytes, label: str) -> InputsManifest:
    """Resolve every derivation input from an acceptance-inputs manifest.

    A manifest whose schema is exactly ``qe-cp-acceptance-inputs/3`` is
    resolved by the v3 family loader (authority families, not basenames).
    Otherwise this fails closed (InputError) unless ``schema`` is exactly
    ``qe-cp-acceptance-inputs/2``, when an entry cannot be placed, a role is
    missing or named by two paths, or a used path carries no single pinned
    sha256.
    """

    try:
        document = json.loads(data.decode("utf-8"))
        entries = document["inputs"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InputError(f"inputs manifest {label} does not parse") from exc
    schema = document.get("schema")
    if schema == V3_INPUTS_MANIFEST_SCHEMA:
        return _load_inputs_manifest_v3(document, entries, data, label)
    if schema != INPUTS_MANIFEST_SCHEMA:
        raise InputError(
            f"inputs manifest {label} schema {schema!r} is not {INPUTS_MANIFEST_SCHEMA}"
        )
    paths: dict[str, set[str]] = {}
    shas: dict[str, set[str]] = {}
    sizes: dict[str, set[int]] = {}
    ignored: list[str] = []
    for entry in entries:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str) or not path:
            raise InputError(f"inputs manifest {label} has an entry without a path")
        basename = re.split(r"[\\/]", path)[-1]
        if basename in IGNORED_BASENAMES:
            ignored.append(path)
            continue
        role = ROLE_BY_BASENAME.get(basename)
        if role is None:
            raise InputError(f"inputs manifest {label} names an unknown input {path}")
        paths.setdefault(role, set()).add(path)
        if entry.get("sha256") is not None:
            shas.setdefault(path, set()).add(str(entry["sha256"]))
        if entry.get("bytes") is not None:
            sizes.setdefault(path, set()).add(int(entry["bytes"]))
    layout: dict[str, str] = {}
    pins: dict[str, str] = {}
    size_of: dict[str, int] = {}
    for role in INPUT_ORDER:
        named = paths.get(role, set())
        if len(named) != 1:
            raise InputError(
                f"inputs manifest {label} names {len(named)} path(s) for {role}"
            )
        (path,) = named
        digests = shas.get(path, set())
        if len(digests) != 1 or not re.fullmatch(r"[0-9a-f]{64}", next(iter(digests))):
            raise InputError(
                f"inputs manifest {label} does not pin one sha256 for {path}"
            )
        layout[role] = path
        pins[path] = next(iter(digests))
        if len(sizes.get(path, set())) > 1:
            raise InputError(
                f"inputs manifest {label} lists conflicting bytes for {path}"
            )
        if sizes.get(path):
            size_of[path] = next(iter(sizes[path]))
    return InputsManifest(
        path=label,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        schema=str(document.get("schema")),
        cutoff=iso_date(document.get("cutoff")),
        layout=layout,
        pins=pins,
        sizes=size_of,
        ignored=tuple(ignored),
    )


def _load_inputs_manifest_v3(
    document: Mapping[str, Any], entries: Sequence[object], data: bytes, label: str
) -> InputsManifest:
    """Resolve a ``qe-cp-acceptance-inputs/3`` manifest by authority family.

    Every derivation input and every authority family is placed by its
    declared ``family`` (by basename only inside the multi-file data
    families). Fails closed on malformed entries, unknown families, a role
    missing or named by two paths, conflicting pins or byte counts, or a
    response-family entry whose declared schema is not
    ``github-issue-comment/REST``. The ``declared_cutoff_date`` entry is a
    production output: recorded as not read, like the v1/v2 ignored basename.
    """

    tag = f"inputs manifest {label} schema {V3_INPUTS_MANIFEST_SCHEMA}"
    paths: dict[str, set[str]] = {}
    shas: dict[str, set[str]] = {}
    sizes: dict[str, set[int]] = {}
    ignored: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise InputError(f"{tag} has a malformed entry")
        path = entry.get("path")
        family = entry.get("family")
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(family, str)
            or not family
        ):
            raise InputError(f"{tag} has an entry without a family/path")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise InputError(f"{tag} does not pin one sha256 for {path}")
        size = entry.get("bytes")
        if size is not None and (
            isinstance(size, bool) or not isinstance(size, int) or size < 0
        ):
            raise InputError(f"{tag} has a malformed bytes value for {path}")
        if family in V3_IGNORED_FAMILIES:
            ignored.append(path)
            continue
        role = V3_SINGLETON_FAMILY_ROLES.get(family)
        if role is None:
            by_name = V3_BASENAME_FAMILY_ROLES.get(family)
            if by_name is None:
                raise InputError(f"{tag} names an unknown family {family!r} ({path})")
            basename = re.split(r"[\\/]", path)[-1]
            role = by_name.get(basename)
            if role is None:
                raise InputError(f"{tag} cannot place {path} in family {family!r}")
        if family == "provider_capital_owner_receipt_b_response":
            declared = entry.get("schema")
            if declared != V3_RESPONSE_SCHEMA:
                raise InputError(
                    f"{tag} response family schema {declared!r} is not "
                    f"{V3_RESPONSE_SCHEMA!r}"
                )
        paths.setdefault(role, set()).add(path)
        shas.setdefault(path, set()).add(digest)
        if size is not None:
            sizes.setdefault(path, set()).add(size)
    layout: dict[str, str] = {}
    pins: dict[str, str] = {}
    size_of: dict[str, int] = {}
    for role in V3_REQUIRED_ROLES:
        named = paths.get(role, set())
        if len(named) != 1:
            raise InputError(f"{tag} names {len(named)} path(s) for {role}")
        (path,) = named
        digests = shas.get(path, set())
        if len(digests) != 1:
            raise InputError(f"{tag} lists conflicting sha256 for {path}")
        if len(sizes.get(path, set())) > 1:
            raise InputError(f"{tag} lists conflicting bytes for {path}")
        layout[role] = path
        pins[path] = next(iter(digests))
        if sizes.get(path):
            size_of[path] = next(iter(sizes[path]))
    # The native Matrixport confirmed-source interface is all-or-none: the
    # base 19-input v3 selection stays on the prior path, a partial selection
    # fails closed, and detached selection evidence is never native authority.
    native_present = [role for role in MP_NATIVE_ROLES if paths.get(role)]
    if native_present and len(native_present) != len(MP_NATIVE_ROLES):
        raise InputError(
            f"{tag} selects {len(native_present)} of {len(MP_NATIVE_ROLES)} "
            "native Matrixport families; all five are required together"
        )
    selection = None
    if native_present:
        for role in MP_NATIVE_ROLES:
            named = paths.get(role, set())
            if len(named) != 1:
                raise InputError(f"{tag} names {len(named)} path(s) for {role}")
            (path,) = named
            digests = shas.get(path, set())
            if len(digests) != 1:
                raise InputError(f"{tag} lists conflicting sha256 for {path}")
            if len(sizes.get(path, set())) > 1:
                raise InputError(f"{tag} lists conflicting bytes for {path}")
            layout[role] = path
            pins[path] = next(iter(digests))
            if sizes.get(path):
                size_of[path] = next(iter(sizes[path]))
        evidence = document.get("generation_selection_evidence")
        selected = evidence.get("matrixport") if isinstance(evidence, dict) else None
        if not isinstance(selected, dict):
            raise InputError(
                f"{tag} lacks generation_selection_evidence.matrixport for the "
                "selected native Matrixport generation"
            )
        selection = selected
    else:
        evidence = document.get("generation_selection_evidence")
        selected = evidence.get("matrixport") if isinstance(evidence, dict) else None
        # Historical v3 already carries six selection/finality fields. Only
        # the native-five interface markers require the corresponding inputs.
        native_markers = {
            "source_run_id",
            "generation_manifest_sha256",
            "catalog_sha256",
        }
        if isinstance(selected, dict) and native_markers.intersection(selected):
            raise InputError(
                f"{tag} carries generation_selection_evidence.matrixport without "
                "the five native Matrixport families"
            )
    return InputsManifest(
        path=label,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        schema=V3_INPUTS_MANIFEST_SCHEMA,
        cutoff=iso_date(document.get("cutoff")),
        layout=layout,
        pins=pins,
        sizes=size_of,
        ignored=tuple(ignored),
        selection=selection,
    )


def _fixed(value: Decimal, quantum: Decimal) -> str:
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded.is_zero():
        rounded = rounded.copy_abs()
    return format(rounded, "f")


def _money(value: Decimal) -> str:
    return _fixed(value, CENT)


def _ratio(value: Decimal) -> str:
    return _fixed(value, RATIO_QUANTUM)


def _day(value: dt.date) -> str:
    return value.isoformat()


@dataclass
class Output:
    metrics: list[dict[str, Any]] = field(default_factory=list)
    not_derivable: list[dict[str, Any]] = field(default_factory=list)

    def metric(
        self,
        metric_id: str,
        entity: str,
        window: str | None,
        as_of: dt.date | None,
        value: object,
        unit: str,
        inputs_used: Sequence[str],
        notes: Sequence[str] = (),
    ) -> None:
        self.metrics.append(
            {
                "metric_id": metric_id,
                "entity": entity,
                "window": window,
                "as_of": _day(as_of) if as_of is not None else None,
                "value": value,
                "unit": unit,
                "inputs_used": sorted(set(inputs_used)),
                "notes": "; ".join(notes),
            }
        )

    def missing(
        self, metric_id: str, entity: str, window: str | None, reason: str
    ) -> None:
        self.not_derivable.append(
            {
                "metric_id": metric_id,
                "entity": entity,
                "window": window,
                "reason": reason,
            }
        )


@dataclass
class Segment:
    """An account's membership in an entity over ``[start, end]``."""

    account_id: str
    start: dt.date
    end: dt.date | None


@dataclass
class AccountSource:
    account_id: str
    provider: str
    values: Mapping[dt.date, Decimal]
    flows: AccountFlows
    inputs: tuple[str, ...]
    value_inputs: tuple[str, ...]
    notes: list[str] = field(default_factory=list)
    # Non-USD or unclassified raw events: (date, description). Those on or before
    # the opening point are embedded in it; later ones make the window unusable.
    raw_events: list[tuple[dt.date, str]] = field(default_factory=list)
    # Last date covered by the raw flow evidence (None: provider-ledger flows).
    flow_coverage_end: dt.date | None = None
    flow_notes: list[str] = field(default_factory=list)
    value_notes: list[str] = field(default_factory=list)
    # Last raw value observation when it is later than the last usable valuation
    # (Matrixport NAV observed after the admitted FX boundary, R6 v2).
    raw_last_observation: dt.date | None = None
    # Non-empty: the value series itself is unusable (fail closed, never thinned).
    value_issue: str = ""
    anchor: tuple[dt.date, Decimal] | None = None


class NotDerivableError(RuntimeError):
    pass


def opening_point(
    values: Mapping[dt.date, Decimal], start: dt.date, limit: dt.date
) -> tuple[dt.date, str] | None:
    # R1 (orchestrator ruling): the prior-day EOD close is the opening value at the
    # funded boundary; T2.3 verified these openings equal OPENING_CAPITAL_ANCHORs.
    prior = [day for day in values if day < start]
    if prior:
        day = max(prior)
        if values[day] > ZERO:
            return day, "BOD_START=EOD_PRIOR_OBSERVATION"
    later = sorted(day for day in values if start <= day <= limit and values[day] > 0)
    if later:
        return later[0], "FIRST_POSITIVE_OBSERVATION_ON_OR_AFTER_START"
    return None


def _in_limit(flow: Flow, open_day: dt.date, limit: dt.date) -> bool:
    """Whether ``flow`` belongs to the membership window ``(open, limit]``."""

    if flow.timing == BOD:
        return open_day <= flow.effective_date <= limit
    return open_day < flow.effective_date <= limit


def _within(flow: Flow, start: dt.date, end: dt.date) -> bool:
    """Whether ``flow`` lies in the value interval ``(start, end]``."""

    if flow.timing == BOD:
        return start <= flow.effective_date < end
    return start < flow.effective_date <= end


def build_member(
    source: AccountSource, segment: Segment, cutoff: dt.date
) -> tuple[Member, list[str]]:
    limit = min(segment.end, cutoff) if segment.end is not None else cutoff
    opening = opening_point(source.values, segment.start, limit)
    if opening is None:
        raise NotDerivableError(
            f"{source.account_id}: no positive value observation for "
            f"[{_day(segment.start)}, {_day(limit)}]"
        )
    open_day, basis = opening
    if source.value_issue:
        raise NotDerivableError(f"{source.account_id}: {source.value_issue}")
    close_day = max(day for day in source.values if open_day <= day <= limit)
    for event_day, description in source.raw_events:
        if open_day < event_day <= limit:
            raise NotDerivableError(
                f"{source.account_id}: {description} on {_day(event_day)} "
                "inside the window"
            )
    for unusable in source.flows.unusable:
        if (
            unusable.effective_date is None
            or open_day < unusable.effective_date <= limit
        ):
            raise NotDerivableError(
                f"{source.account_id}: unusable flow {unusable.record_id} "
                f"({unusable.reason})"
            )
    in_window = [
        flow for flow in source.flows.flows if _within(flow, open_day, close_day)
    ]
    in_limit = [flow for flow in source.flows.flows if _in_limit(flow, open_day, limit)]
    stranded = [flow for flow in in_limit if flow not in in_window]
    if stranded:
        raise NotDerivableError(
            f"{source.account_id}: {len(stranded)} flow(s) inside the window after the "
            f"last value observation {_day(close_day)} (not reflected in any value), "
            f"first {_day(stranded[0].effective_date)} {stranded[0].timing}"
        )
    values = {
        day: v for day, v in source.values.items() if open_day <= day <= close_day
    }
    member = Member(
        key=source.account_id,
        values=values,
        open_date=open_day,
        close_date=close_day,
        flows=tuple(
            (flow.effective_date, flow.amount, flow.timing) for flow in in_window
        ),
    )
    embedded = [
        flow
        for flow in source.flows.flows
        if flow.effective_date < open_day
        or (flow.effective_date == open_day and flow.timing != BOD)
    ]
    near = [
        flow
        for flow in embedded
        if flow.effective_date >= segment.start - dt.timedelta(days=31)
    ]
    dated = Counter(flow.dated_by for flow in in_window)
    classes = Counter(flow.disposition for flow in in_window)
    notes = [
        f"{source.account_id} funded segment [{_day(segment.start)}, "
        f"{_day(segment.end) if segment.end else 'open'}] opening "
        f"{_day(open_day)} value {_money(source.values[open_day])} ({basis}); "
        f"terminal {_day(close_day)} value {_money(source.values[close_day])}; "
        f"{len(in_window)} window flow(s) net "
        f"{_money(sum((f.amount for f in in_window), ZERO))}"
        + (f" dated by {dict(sorted(dated.items()))}" if dated else "")
        + (f", classes {dict(sorted(classes.items()))}" if classes else "")
    ]
    if source.anchor is not None:
        anchor_day, anchor_value = source.anchor
        agrees = anchor_day == open_day and _money(anchor_value) == _money(
            source.values[open_day]
        )
        notes.append(
            f"OPENING_CAPITAL_ANCHOR {_day(anchor_day)} {_money(anchor_value)} "
            + ("agrees" if agrees else "DIFFERS")
        )
    if near:
        notes.append(
            f"{len(near)} flow(s) net {_money(sum((f.amount for f in near), ZERO))} "
            f"dated {_day(near[0].effective_date)}..{_day(near[-1].effective_date)} "
            "are embedded in the opening value"
        )
    embedded_events = [
        description
        for event_day, description in source.raw_events
        if segment.start - dt.timedelta(days=31) <= event_day <= open_day
    ]
    if embedded_events:
        notes.append(
            f"{len(embedded_events)} raw wallet event(s) on or before the opening are "
            "embedded in it"
        )
    coverage = source.flow_coverage_end
    if coverage is not None and coverage < close_day:
        notes.append(
            f"CAVEAT raw flow evidence ends {_day(coverage)}: flows in "
            f"({_day(coverage)}, {_day(close_day)}] are not observable in the listed "
            "raw inputs and none are assumed"
        )
    notes.extend(source.flow_notes)
    notes.extend(source.value_notes)
    return member, notes


def _returns(
    members: Sequence[Member],
) -> tuple[Decimal | None, str, IrrResult, list[str]]:
    notes: list[str] = []
    twr: Decimal | None = None
    twr_reason = ""
    try:
        periods = composite_subperiods(members)
        twr = time_weighted_return(periods)
        extreme = []
        for period in periods:
            value = subperiod_return(
                period.beginning_value,
                period.ending_value,
                period.net_flow,
                period.beginning_flow,
            )
            if abs(value) > 1:
                extreme.append(f"{_day(period.end)}:{_ratio(value)}")
        notes.append(f"{len(periods)} sub-periods")
        if extreme:
            notes.append("sub-period |r|>1 at " + ", ".join(extreme))
    except UndefinedReturnError:
        undefined = []
        for period in composite_subperiods(members):
            try:
                subperiod_return(
                    period.beginning_value,
                    period.ending_value,
                    period.net_flow,
                    period.beginning_flow,
                )
            except UndefinedReturnError:
                undefined.append(
                    f"({_day(period.start)}, {_day(period.end)}] beginning "
                    f"{period.beginning_value}+{period.beginning_flow} ending "
                    f"{period.ending_value} EOD flow {period.net_flow}"
                )
        twr_reason = (
            "R3 TWR undefined (fail closed): a sub-period starts at zero or negative "
            "capital without a same-day inflow in "
            + "; ".join(undefined[:3])
            + (f" (+{len(undefined) - 3} more)" if len(undefined) > 3 else "")
        )
    except CalendarMisalignmentError as exc:
        twr_reason = f"member calendars misaligned without filling: {exc}"
    irr = xirr(investor_cash_flows(members))
    return twr, twr_reason, irr, notes


def _ledger_cross_check(
    raw: Mapping[dt.date, Decimal],
    ledger: Mapping[dt.date, Decimal],
    cutoff: dt.date,
    boundary: str = "after the last raw observation",
) -> list[str]:
    """R6: the ledger is a cross-check; later ledger valuations are not expected."""

    common = sorted(set(raw).intersection(ledger))
    differences = [day for day in common if abs(raw[day] - ledger[day]) >= CENT]
    notes = [
        f"cross-check provider-ledger values: {len(common)} common dates, "
        f"{len(differences)} differ by >= 0.01"
        + (
            " ("
            + ", ".join(
                f"{_day(day)} raw {_money(raw[day])} ledger {_money(ledger[day])}"
                for day in differences[:3]
            )
            + ")"
            if differences
            else ""
        )
    ]
    last_raw = max(raw) if raw else None
    later = sorted(
        day for day in ledger if day <= cutoff and (last_raw is None or day > last_raw)
    )
    if later:
        notes.append(
            f"R6 NOT EXPECTED VALUES: {len(later)} provider-ledger valuation(s) "
            f"{boundary} {_day(last_raw) if last_raw else '-'}, "
            f"{_day(later[0])}..{_day(later[-1])}, last {_money(ledger[later[-1]])}"
        )
    return notes


def _opening_anchor(ledger: Ledger, account_id: str) -> tuple[dt.date, Decimal] | None:
    for anchor in ledger.anchors.get(account_id, ()):
        if (
            anchor.capital_type == "OPENING_CAPITAL_ANCHOR"
            and anchor.recognition_date is not None
            and anchor.opening_nav is not None
        ):
            return anchor.recognition_date, anchor.opening_nav
    return None


def _account_sources(
    crosswalk: Crosswalk,
    ledger: Ledger,
    files: Mapping[str, InputFile],
    fx: FxSource | None,
    cutoff: dt.date,
    fx_v3: FxSourceV3 | None = None,
    native: MatrixportNative | None = None,
) -> dict[str, AccountSource]:
    providers: dict[str, str] = {}
    labels: dict[str, str] = {}
    for row in crosswalk.rows:
        providers[row.account_id] = row.provider
        labels[row.account_id] = row.label
    sources: dict[str, AccountSource] = {}
    for account_id in sorted(providers):
        provider = providers[account_id]
        ledger_values = {
            day: value
            for day, value in ledger.nav.get(account_id, {}).items()
            if day <= cutoff
        }
        if provider == "QUANTBASE":
            source = _quantbase_source(
                account_id, labels[account_id], ledger_values, ledger, files, cutoff
            )
        elif provider == "MATRIXPORT":
            if fx_v3 is not None and native is not None:
                source = _matrixport_source_native(
                    account_id, ledger_values, ledger, files, fx_v3, native, cutoff
                )
            elif fx_v3 is not None:
                source = _matrixport_source_v3(
                    account_id, ledger_values, ledger, files, fx_v3, cutoff
                )
            elif fx is not None:
                source = _matrixport_source(
                    account_id, ledger_values, ledger, files, fx, cutoff
                )
            else:  # pragma: no cover - defensive: one FX source is required
                raise InputError(f"{account_id}: no FX source selected")
        else:
            source = AccountSource(
                account_id,
                provider,
                ledger_values,
                ledger.flows.get(account_id, AccountFlows()),
                (LEDGER,),
                (LEDGER,),
            )
        issues = sorted(set(ledger.nav_issues.get(account_id, ())))
        if issues and source.value_inputs == (LEDGER,):
            source.value_issue = (
                f"{len(issues)} provider-ledger nav row(s) unusable or conflicting: "
                + ", ".join(issues[:3])
            )
        elif issues:
            source.notes.append(
                "ledger nav issues (cross-check only): " + ", ".join(issues)
            )
        source.anchor = _opening_anchor(ledger, account_id)
        sources[account_id] = source
    return sources


def _quantbase_source(
    account_id: str,
    label: str,
    ledger_values: Mapping[dt.date, Decimal],
    ledger: Ledger,
    files: Mapping[str, InputFile],
    cutoff: dt.date,
) -> AccountSource:
    title = label.split("—", 1)[1].strip() if "—" in label else label
    sleeve: QuantbaseSleeve = parse_quantbase_sleeve(
        files[QB_ACTIVITY].data, files[QB_ACCOUNTS].data, title
    )
    all_raw_values = parse_quantbase_values(files[QB_VALUES].data)
    raw_values = {day: value for day, value in all_raw_values.items() if day <= cutoff}
    source = AccountSource(
        account_id,
        "QUANTBASE",
        raw_values,
        sleeve.flows,
        (QB_ACTIVITY, QB_VALUES),
        (QB_VALUES, QB_ACCOUNTS, LEDGER),
    )
    raw = sleeve.flows.flows
    source.flow_coverage_end = sleeve.coverage_end
    summary = (
        f"raw fund {sleeve.fund_id} {title!r}: {len(raw)} sleeve flow(s) net "
        f"{_money(sum((f.amount for f in raw), ZERO))}, {len(sleeve.fee_sales)} "
        f"fee sale(s) {_money(sum((a for _, a in sleeve.fee_sales), ZERO))} treated "
        f"as expense, {sleeve.rebalances} rebalance(s) internal"
    )
    anchors = [
        (anchor.recognition_date, anchor.amount)
        for anchor in ledger.anchors.get(account_id, ())
        if anchor.amount is not None
    ]
    raw_keys = sorted((f.effective_date + dt.timedelta(days=1), f.amount) for f in raw)
    matched = sum(1 for key in raw_keys if key in anchors)
    anchor_check = (
        f"cross-check provider-ledger capital_anchors: {matched}/{len(raw_keys)} raw "
        f"(fill date + 1, amount) pairs equal an anchor (nav_recognition_date, "
        f"amount) of {len(anchors)}"
    )
    ledger_flows = ledger.flows.get(account_id, AccountFlows()).flows
    ledger_check = (
        f"provider-ledger flows for this account: {len(ledger_flows)} "
        f"{sorted({f.kind for f in ledger_flows})} net "
        f"{_money(sum((f.amount for f in ledger_flows), ZERO))} (account-level "
        "deposits, not sleeve flows; not used)"
    )
    source.flow_notes.extend([summary, anchor_check, ledger_check])
    if sleeve.dividends:
        source.flow_notes.append(
            f"CAVEAT {len(sleeve.dividends)} account dividend row(s) totalling "
            f"{_money(sum((cash for _, cash in sleeve.dividends), ZERO))} "
            f"({_day(sleeve.dividends[0][0])}..{_day(sleeve.dividends[-1][0])}) are "
            "investment results, not flows (DIVIDEND_INTEREST_REINVESTMENT); the listed "
            "inputs do not show whether they were credited inside the sleeve value"
        )
    if sleeve.other_types:
        source.flow_notes.append(f"unknown fund txn types {list(sleeve.other_types)}")
    source.value_notes.extend(_ledger_cross_check(raw_values, ledger_values, cutoff))
    if all_raw_values and sleeve.snapshot_value is not None:
        # The accounts.csv snapshot is taken with the capture, so it is compared
        # with the capture's latest price-history row (which may follow the cutoff).
        last = max(all_raw_values)
        source.value_notes.append(
            f"cross-check raw accounts.csv fund value {_money(sleeve.snapshot_value)} "
            f"vs latest raw price-history row {_day(last)} "
            f"{_money(all_raw_values[last])}"
            + (" (after the cutoff, not used)" if last > cutoff else "")
            + f" diff {_money(sleeve.snapshot_value - all_raw_values[last])}"
        )
    source.notes.extend(source.flow_notes + source.value_notes)
    return source


def _fx_provenance(fx: FxSource, used: Sequence[dt.date]) -> str:
    after = [day for day in used if fx.admitted_through and day > fx.admitted_through]
    return (
        "fx_provenance: USDT->USD Yahoo daily Close from the contract-bound "
        f"{fx.contract_generation[:8]} raw capture {fx.path} (sha256 {fx.sha256}; "
        "contract source_segments[portfolio-fx-rates].current_generation "
        f"{fx.contract_generation} observed through {fx.contract_observed_end}); "
        f"matrixport-account-generation splice_policy {fx.splice_rule}; admitted "
        f"boundary {_day(fx.admitted_through) if fx.admitted_through else 'NONE'}, "
        f"{fx.dropped_after_boundary} raw rate(s) after it ignored; rates used "
        f"{_day(min(used)) if used else '-'}..{_day(max(used)) if used else '-'}, "
        f"{len(after)} after the admitted boundary"
    )


@dataclass(frozen=True)
class FxSource:
    rates: Mapping[dt.date, Decimal]
    sha256: str
    contract_generation: str
    contract_observed_end: str
    admitted_through: dt.date | None
    splice_rule: str
    dropped_after_boundary: int = 0
    path: str = FX_HISTORY


def _fx_source(fx_file: InputFile, contract: bytes) -> FxSource:
    document = json.loads(contract.decode("utf-8"))
    segments = {
        str(segment.get("source_id")): segment
        for segment in document.get("source_segments", [])
    }
    fx_segment = segments.get("portfolio-fx-rates", {})
    generation = str((fx_segment.get("current_generation") or {}).get("generation_id"))
    observed_end = str((fx_segment.get("interval") or {}).get("observed_end"))
    splice = str(
        (segments.get("matrixport-account-generation", {}).get("interval") or {}).get(
            "splice_policy", ""
        )
    )
    rule = next(
        (part.strip() for part in splice.split(";") if "USD_CONVERSION" in part), ""
    )
    through: dt.date | None = None
    if "THROUGH_" in rule:
        through = iso_date(rule.rsplit("THROUGH_", 1)[1].replace("_", "-"))
    through = through or iso_date(observed_end)
    raw = parse_fx_history(fx_file.data, "USDT-USD")
    # Fail closed: without an admitted boundary in the contract no rate is used.
    admitted = {day: rate for day, rate in raw.items() if through and day <= through}
    return FxSource(
        rates=admitted,
        sha256=fx_file.sha256,
        contract_generation=generation,
        contract_observed_end=observed_end,
        admitted_through=through,
        splice_rule=rule or "-",
        dropped_after_boundary=len(raw) - len(admitted),
        path=fx_file.path,
    )


def _classify_bills_full(
    wallet: MatrixportWallet,
) -> tuple[
    list[Bill],
    list[tuple[Bill, str]],
    list[tuple[Bill, str]],
    dict[int, str],
    dict[int, str],
]:
    """Split raw wallet bills for the strategy-holding entity.

    Raw bills carry no shared transaction id across legs (each ``sn`` is unique),
    so a conversion is proved economically:

    - a debit and a credit in the other stablecoin (USDT <-> USDC) within 10
      minutes after it, whose amounts agree within 1 % (the conversion rate),
      are one conversion if that credit is the debit's only such candidate; both
      legs stay in the wallet;
    - two or more matching candidates, or any product-currency counterpart that
      cannot be amount-checked (a non-stablecoin), is ambiguous and fails closed;
    - a stablecoin bill in the window whose amount does not agree is unrelated;
    - an unpaired product-currency (USDT) debit is a strategy subscription;
      an unpaired bill in any other currency is wallet-level;
    - an unpaired USDT credit (e.g. a redemption) is unsupported and fails closed
      when it falls inside a window.

    The fourth return value carries the ambiguity reasons and the fifth the
    conversion-pairing map (used by the v3 custody derivation, which fails
    closed on an ambiguous bill of any currency, and by the native Matrixport
    source, which treats every unpaired numeric-coded bill as an unclassified
    witness). The v1/v2 caller discards them and is byte-for-byte unchanged --
    note the wallet-level reason text is only ever counted, never emitted).
    """

    bills = list(wallet.bills)
    paired: dict[int, str] = {}
    ambiguous: dict[int, str] = {}
    for index, debit in enumerate(bills):
        if debit.direction != "2" or index in paired or debit.amount <= 0:
            continue
        window = [
            (other, credit)
            for other, credit in enumerate(bills)
            if other != index
            and other not in paired
            and credit.direction == "1"
            and credit.currency != debit.currency
            and MATRIXPORT_PRODUCT_CURRENCY in {credit.currency, debit.currency}
            and 0 <= credit.moment_ms - debit.moment_ms <= CONVERSION_WINDOW_MS
        ]
        checkable = [
            (other, credit)
            for other, credit in window
            if {credit.currency, debit.currency} <= STABLECOINS
        ]
        matching = [
            (other, credit)
            for other, credit in checkable
            if abs(credit.amount / debit.amount - 1) <= CONVERSION_TOLERANCE
        ]
        unverifiable = [pair for pair in window if pair not in checkable]
        if len(matching) == 1 and not unverifiable:
            paired[index] = "conversion debit"
            paired[matching[0][0]] = "conversion credit"
        elif matching or unverifiable:
            reason = (
                f"ambiguous conversion pairing ({len(matching)} amount-matching and "
                f"{len(unverifiable)} unverifiable counterpart(s) within 10 minutes)"
            )
            ambiguous[index] = reason
            for other, _ in matching + unverifiable:
                ambiguous.setdefault(other, reason)
    subscriptions: list[Bill] = []
    wallet_level: list[tuple[Bill, str]] = []
    unsupported: list[tuple[Bill, str]] = []
    for index, bill in enumerate(bills):
        if index in paired:
            wallet_level.append((bill, paired[index]))
        elif index in ambiguous and bill.currency == MATRIXPORT_PRODUCT_CURRENCY:
            unsupported.append((bill, ambiguous[index]))
        elif index in ambiguous:
            wallet_level.append((bill, ambiguous[index]))
        elif bill.currency != MATRIXPORT_PRODUCT_CURRENCY:
            wallet_level.append((bill, "non-product currency"))
        elif bill.direction == "2":
            subscriptions.append(bill)
        else:
            unsupported.append((bill, "unpaired product-currency credit"))
    return subscriptions, wallet_level, unsupported, ambiguous, paired


def _classify_bills(
    wallet: MatrixportWallet,
) -> tuple[list[Bill], list[tuple[Bill, str]], list[tuple[Bill, str]]]:
    """The v1/v2 three-bucket view of ``_classify_bills_full`` (unchanged)."""

    subscriptions, wallet_level, unsupported, _, _ = _classify_bills_full(wallet)
    return subscriptions, wallet_level, unsupported


def _classify_bills_v3(
    wallet: MatrixportWallet,
) -> tuple[list[Bill], list[Bill], list[tuple[Bill, str]], list[tuple[Bill, str]]]:
    """The v3 four-bucket split: (subscriptions, withdrawals, wallet_level, unsupported).

    Same economic conversion pairing as the v1/v2 classifier. An unpaired
    product-currency (USDT) debit is a strategy subscription only for the
    known subscription tx_type (2855); it is an owner withdrawal when its
    tx_type says so (leaving the wallet, creating no units and no fund flow);
    any other unpaired product-currency debit (e.g. a transfer) is explicitly
    unsupported and fails closed. The v1/v2 classification is unchanged.
    """

    subscriptions_all, wallet_level, unsupported, _, _ = _classify_bills_full(wallet)
    subscriptions: list[Bill] = []
    withdrawals: list[Bill] = []
    for bill in subscriptions_all:
        if bill.tx_type.strip().lower() in MATRIXPORT_WITHDRAWAL_TX_TYPES:
            withdrawals.append(bill)
        elif bill.tx_type == MATRIXPORT_SUBSCRIPTION_TX_TYPE:
            subscriptions.append(bill)
        else:
            unsupported.append(
                (
                    bill,
                    f"unsupported product-currency debit tx_type {bill.tx_type!r}",
                )
            )
    return subscriptions, withdrawals, wallet_level, unsupported


def _matrixport_source(
    account_id: str,
    ledger_values: Mapping[dt.date, Decimal],
    ledger: Ledger,
    files: Mapping[str, InputFile],
    fx: FxSource,
    cutoff: dt.date,
) -> AccountSource:
    wallet: MatrixportWallet = parse_matrixport_wallet(
        files[MP_DEPOSITS].data,
        files[MP_BILLS].data,
        files[MP_DISPOSITIONS].data,
        files[MP_NAV].data,
    )
    flows = AccountFlows()
    source = AccountSource(
        account_id,
        "MATRIXPORT",
        {},
        flows,
        (MP_BILLS, MP_DEPOSITS, MP_DISPOSITIONS, MP_NAV, FX_HISTORY),
        (MP_NAV, MP_BILLS, FX_HISTORY),
    )
    subscriptions, wallet_level, unsupported = _classify_bills(wallet)
    units_added: list[tuple[dt.date, Decimal]] = []
    fx_days: list[dt.date] = []
    for bill in subscriptions:
        confirm = next(
            (day for moment, day in wallet.nav_times if moment > bill.moment_ms), None
        )
        if confirm is None or wallet.nav_per_unit[confirm] <= 0:
            source.raw_events.append(
                (bill.day, f"subscription {bill.amount} USDT with no later raw NAV")
            )
            continue
        rate = fx.rates.get(confirm)
        if rate is None:
            flows.unusable.append(
                UnusableFlow(confirm, f"bill:{bill.moment_ms}", "no USDT-USD rate")
            )
            continue
        units_added.append((confirm, bill.amount / wallet.nav_per_unit[confirm]))
        fx_days.append(confirm)
        flows.flows.append(
            Flow(
                confirm,
                bill.amount * rate,
                bill.day,
                "STRATEGY_SUBSCRIPTION",
                f"matrixport:bill:{bill.moment_ms}",
                dated_by="nav_confirm_date",
            )
        )
    for bill, why in unsupported:
        source.raw_events.append(
            (
                bill.day,
                f"unsupported wallet bill tx_type {bill.tx_type} "
                f"{'debit' if bill.direction == '2' else 'credit'} {bill.amount} "
                f"{bill.currency}: {why}",
            )
        )
    # Holding value per raw NAV date: units subscribed by then x NAV x USDT-USD.
    missing_fx: list[dt.date] = []
    unconverted: list[dt.date] = []
    values: dict[dt.date, Decimal] = {}
    if units_added:
        first_day = min(day for day, _ in units_added)
        for day in sorted(wallet.nav_per_unit):
            if day < first_day or day > cutoff:
                continue
            rate = fx.rates.get(day)
            if rate is None:
                if fx.admitted_through is not None and day > fx.admitted_through:
                    unconverted.append(day)
                else:
                    missing_fx.append(day)
                continue
            units = sum((u for confirm, u in units_added if confirm <= day), ZERO)
            values[day] = units * wallet.nav_per_unit[day] * rate
            fx_days.append(day)
    source.values = values
    seen = {bill.currency for bill in wallet.bills} | {c for _, _, c in wallet.deposits}
    blocked = sorted(seen.intersection(wallet.rejected_currencies))
    if blocked:
        flows.unusable.append(
            UnusableFlow(None, "wallet", f"history unreadable for {blocked}")
        )
    source.flow_coverage_end = wallet.coverage_end
    units_total = sum((u for _, u in units_added), ZERO)
    source.value_notes.append(
        f"raw holding: {len(subscriptions)} USDT subscription debit(s) "
        f"{sum((b.amount for b in subscriptions), ZERO)} -> units "
        f"{str(units_total)[:22]}; {len(source.values)} raw valuations "
        f"{_day(min(source.values)) if source.values else '-'}.."
        f"{_day(max(source.values)) if source.values else '-'}"
        + (
            f"; {len(missing_fx)} raw NAV date(s) without a raw USDT-USD rate dropped"
            if missing_fx
            else ""
        )
    )
    if unconverted:
        source.raw_last_observation = unconverted[-1]
        source.value_notes.append(
            f"R6 v2 FX-UNCONVERTED: {len(unconverted)} raw NAV date(s) "
            f"{_day(unconverted[0])}..{_day(unconverted[-1])} are after the admitted "
            f"FX boundary {_day(fx.admitted_through) if fx.admitted_through else '-'}; "
            "no USD value is emitted for them (last raw NAV "
            f"{_day(unconverted[-1])} "
            f"{wallet.nav_per_unit[unconverted[-1]]} per unit)"
        )
    source.value_notes.append(_fx_provenance(fx, sorted(set(fx_days))))
    ledger_rates = {
        day: Decimal(str(item["rate"]))
        for day, row in ledger.nav_rows.get(account_id, {}).items()
        for item in row.get("valuation_fx", [])
        if item.get("currency") == MATRIXPORT_PRODUCT_CURRENCY
    }
    common = sorted(set(ledger_rates).intersection(fx.rates).intersection(fx_days))
    differing = [day for day in common if ledger_rates[day] != fx.rates[day]]
    largest = max(
        (abs(ledger_rates[day] - fx.rates[day]) for day in common), default=ZERO
    )
    source.value_notes.append(
        f"cross-check provider-ledger valuation_fx: {len(common) - len(differing)}/"
        f"{len(common)} rates equal the raw Close exactly, max |difference| "
        f"{largest:.1E}"
    )
    source.flow_notes.append(
        f"raw wallet: {len(wallet.deposits)} confirmed deposit(s) "
        f"{sum((a for _, a, _ in wallet.deposits), ZERO)} "
        f"{sorted({c for _, _, c in wallet.deposits})} and "
        f"{len(wallet_level)} wallet-level bill(s) stay outside the strategy holding; "
        f"{len(subscriptions)} subscription(s) are holding inflows (EOD at the NAV "
        f"confirm date); {len(unsupported)} unsupported bill(s); deposit/withdrawal "
        f"history rejected for {len(wallet.rejected_currencies)} currency code(s), "
        + ("none used by the account" if not blocked else f"including {blocked}")
    )
    ledger_flows = ledger.flows.get(account_id, AccountFlows())
    ledger_days = [flow.event_date for flow in ledger_flows.flows] + [
        flow.effective_date
        for flow in ledger_flows.unusable
        if flow.effective_date is not None
    ]
    source.flow_notes.append(
        f"provider-ledger flows for this account: "
        f"{len(ledger_flows.flows) + len(ledger_flows.unusable)} record(s), latest "
        f"{_day(max(ledger_days)) if ledger_days else '-'} (not used)"
    )
    source.value_notes.extend(
        _ledger_cross_check(
            source.values,
            ledger_values,
            cutoff,
            "after the last USD-converted raw valuation"
            if unconverted
            else "after the last raw observation",
        )
    )
    source.notes.extend(source.flow_notes + source.value_notes)
    return source


# --------------------------------------------------------------------- v3 (R6 v3 / R8)


@dataclass(frozen=True)
class FxSourceV3:
    """R6 v3 FX authority: both pinned pairs of the owner-selected generation.

    Rates are admitted only inside the contract segment's observed interval;
    a needed date without an admitted close fails closed at the point of use
    (no interpolation, no parity assumption, no date relabeling). Every used
    (currency, date) must additionally carry a supported daily-rates mapping
    (USD base, exact ticker, Close field) from the pinned rates artifact; the
    serialized rate column is structural evidence only and is never equated
    with a raw close. The v2 admitted-boundary / FX-unconverted behavior does
    not exist under v3.
    """

    usdt: Mapping[dt.date, Decimal]
    usdc: Mapping[dt.date, Decimal]
    mappings: frozenset[tuple[str, dt.date]]
    generation: str
    observed_start: dt.date
    observed_end: dt.date
    usdt_path: str
    usdt_sha256: str
    usdc_path: str
    usdc_sha256: str
    rates_path: str
    rates_sha256: str
    dropped_outside_window: int = 0

    def close(self, currency: str, day: dt.date) -> Decimal | None:
        """The pinned raw daily Close (without the rates-mapping gate)."""

        if currency == MATRIXPORT_PRODUCT_CURRENCY:
            return self.usdt.get(day)
        if currency == "USDC":
            return self.usdc.get(day)
        return None

    def rate(self, currency: str, day: dt.date) -> Decimal | None:
        """The admitted rate: identity for USD, else the raw close gated on a
        supported daily-rates mapping for that exact currency and date."""

        if currency == "USD":
            return ONE
        if (currency, day) not in self.mappings:
            return None
        return self.close(currency, day)


@dataclass(frozen=True)
class V3Authority:
    """The validated v3 authority bundle (comment bodies never emitted)."""

    generation: str
    observed_start: dt.date
    observed_end: dt.date
    policy_id: str
    payload_digest: str
    contract: Mapping[str, Any]
    notes: tuple[str, ...]


def _v3_artifact_id(path: str) -> str:
    """A selected FX file's artifact id: its path below generations/<id>/."""

    parts = re.split(r"[\\/]", path)
    for index, part in enumerate(parts[:-1]):
        if part == "generations":
            return "/".join(parts[index + 2 :])
    return parts[-1]


def _parse_fx_rates(data: bytes, tag: str) -> frozenset[tuple[str, dt.date]]:
    """The supported (currency, UTC date) mappings of the pinned daily rates.

    Every row must map a supported currency to USD with that currency's exact
    ticker and the Close field; any other row is inconsistent authority and
    fails closed. The serialized ``rate`` column is structural evidence only:
    financial values are derived from the raw closes and are never equated
    with this column.
    """

    rows = csv_rows(data)
    if rows and set(rows[0]) != set(FX_RATES_HEADER):
        raise InputError(f"{tag}: daily-rates columns are not {list(FX_RATES_HEADER)}")
    mappings: dict[tuple[str, dt.date], str] = {}
    for index, row in enumerate(rows, start=2):
        currency = row.get("currency", "")
        ticker = row.get("ticker", "")
        day = iso_date(row.get("date"))
        rate_value = decimal_or_none(row.get("rate"))
        if (
            day is None
            or row.get("base_currency") != "USD"
            or row.get("field") != "Close"
            or rate_value is None
            or rate_value <= 0
            or not row.get("provider")
            or not row.get("timing")
        ):
            raise InputError(f"{tag}: daily-rates row {index} does not parse")
        if FX_TICKER_BY_CURRENCY.get(currency) != ticker:
            raise InputError(
                f"{tag}: daily-rates row {index} maps {currency!r} to ticker "
                f"{ticker!r} (unsupported mapping)"
            )
        key = (currency, day)
        if key in mappings:
            raise InputError(
                f"{tag}: daily-rates row {index} duplicates {currency} {_day(day)}"
            )
        mappings[key] = ticker
    return frozenset(mappings)


def _fx_source_v3(
    usdt_file: InputFile,
    usdc_file: InputFile,
    rates_file: InputFile,
    observed_start: dt.date,
    observed_end: dt.date,
    generation: str,
    label: str,
) -> FxSourceV3:
    raw_usdt = parse_fx_history(usdt_file.data, "USDT-USD")
    raw_usdc = parse_fx_history(usdc_file.data, "USDC-USD")
    mappings = _parse_fx_rates(rates_file.data, f"v3 authority {label}")
    usdt = {
        day: r for day, r in raw_usdt.items() if observed_start <= day <= observed_end
    }
    usdc = {
        day: r for day, r in raw_usdc.items() if observed_start <= day <= observed_end
    }
    return FxSourceV3(
        usdt=usdt,
        usdc=usdc,
        mappings=frozenset(
            (currency, day)
            for currency, day in mappings
            if observed_start <= day <= observed_end
        ),
        generation=generation,
        observed_start=observed_start,
        observed_end=observed_end,
        usdt_path=usdt_file.path,
        usdt_sha256=usdt_file.sha256,
        usdc_path=usdc_file.path,
        usdc_sha256=usdc_file.sha256,
        rates_path=rates_file.path,
        rates_sha256=rates_file.sha256,
        dropped_outside_window=(len(raw_usdt) - len(usdt))
        + (len(raw_usdc) - len(usdc)),
    )


def _fx_provenance_v3(fx: FxSourceV3, used: Sequence[dt.date]) -> str:
    return (
        "fx_provenance: R6 v3 owner-selected FX generation "
        f"{fx.generation[:8]} (contract source_segments[portfolio-fx-rates]"
        f".current_generation, observed {_day(fx.observed_start)}.."
        f"{_day(fx.observed_end)}): USDT->USD raw capture {fx.usdt_path} "
        f"(sha256 {fx.usdt_sha256}), USDC->USD raw capture {fx.usdc_path} "
        f"(sha256 {fx.usdc_sha256}); original UTC event/NAV dates only, no "
        "interpolation, parity, or date relabeling; a missing admitted rate "
        f"fails closed; {fx.dropped_outside_window} raw rate(s) outside the "
        f"observed window ignored; rates used "
        f"{_day(min(used)) if used else '-'}..{_day(max(used)) if used else '-'}"
    )


def _v3_generation_of(path: str) -> str | None:
    parts = re.split(r"[\\/]", path)
    for index, part in enumerate(parts[:-1]):
        if part == "generations" and re.fullmatch(r"[0-9a-f]{64}", parts[index + 1]):
            return parts[index + 1]
    return None


def _outcome_calculation_contract(
    contract: Mapping[str, Any], outcome_id: str, tag: str
) -> Mapping[str, Any]:
    """The unique calculation_contract of the named financial outcome."""

    outcomes = contract.get("financial_outcomes")
    if not isinstance(outcomes, list):
        raise InputError(f"{tag}: contract has no financial_outcomes")
    selected = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, dict) and outcome.get("outcome_id") == outcome_id
    ]
    if len(selected) != 1:
        raise InputError(
            f"{tag}: contract has {len(selected)} financial_outcomes named {outcome_id}"
        )
    calculation = selected[0].get("calculation_contract")
    if not isinstance(calculation, dict):
        raise InputError(
            f"{tag}: financial_outcomes[{outcome_id}] has no calculation_contract"
        )
    return calculation


def _validate_policy_decision(policy: object, what: str) -> dict[str, Any]:
    """The native approved-decision shape; unresolved states fail closed."""

    if not isinstance(policy, dict) or set(policy) != POLICY_DECISION_KEYS:
        raise InputError(f"{what} does not carry exactly the approved decision key set")
    if policy["decision_status"] != "APPROVED":
        raise InputError(
            f"{what} decision_status {policy['decision_status']!r} is not APPROVED "
            "(unresolved policy states fail closed)"
        )
    if policy["decision_authority"] not in POLICY_DECISION_AUTHORITIES:
        raise InputError(
            f"{what} decision_authority {policy['decision_authority']!r} is not "
            "an accepted authority"
        )
    for _decision_field in (
        "plain_language",
        "calculation_effect",
        "selected_treatment",
        "approval_evidence",
    ):
        if (
            not isinstance(policy[_decision_field], str)
            or not policy[_decision_field].strip()
        ):
            raise InputError(
                f"{what} field {_decision_field} must be a non-empty string"
            )
    if policy["recommended_treatment"] is not None and not isinstance(
        policy["recommended_treatment"], str
    ):
        raise InputError(f"{what} recommended_treatment must be null or a string")
    if not isinstance(policy["examples"], list) or not all(
        isinstance(item, str) for item in policy["examples"]
    ):
        raise InputError(f"{what} examples must be a list of strings")
    return policy


def _policy_payload_digest(policy: Mapping[str, Any]) -> str:
    """The canonical owner-decision payload digest (sorted-keys JSON + NL)."""

    canonical = (
        json.dumps(
            {"decision": policy},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def owner_decision_receipt_digests(
    calculation: Mapping[str, Any], tag: str
) -> dict[str, str]:
    """The contract's decision_id -> payload digest map."""

    receipts = calculation.get("owner_decision_receipts")
    if not isinstance(receipts, dict) or not receipts:
        raise InputError(
            f"{tag}: contract calculation_contract has no owner_decision_receipts"
        )
    digests: dict[str, str] = {}
    for decision_id, receipt in receipts.items():
        payload = receipt.get("payload_sha256") if isinstance(receipt, dict) else None
        if not isinstance(payload, str) or not re.fullmatch(r"[0-9a-f]{64}", payload):
            raise InputError(
                f"{tag}: contract owner_decision_receipts[{decision_id}] has no "
                "pinned payload_sha256"
            )
        digests[str(decision_id)] = payload
    return digests


def owner_receipt_evidence_record(body: str, tag: str) -> dict[str, Any]:
    """The single machine record fenced inside a digest-readback comment body."""

    if body.count(RECEIPT_RECORD_MARKER) != 1:
        raise InputError(
            f"{tag}: posted response body does not contain exactly one "
            "owner-decision-receipts marker"
        )
    fences = re.findall(r"```json\n(.*?)\n```", body, re.DOTALL)
    if len(fences) != 1:
        raise InputError(
            f"{tag}: posted response body does not contain exactly one json fence"
        )
    try:
        record = json.loads(fences[0])
    except json.JSONDecodeError as exc:
        raise InputError(f"{tag}: posted response json fence does not parse") from exc
    if not isinstance(record, dict) or set(record) != RECEIPT_RECORD_KEYS:
        raise InputError(
            f"{tag}: posted response record keys are not the receipt record key set"
        )
    if record["schema_version"] != 1:
        raise InputError(
            f"{tag}: posted response record schema_version "
            f"{record['schema_version']!r} is not 1"
        )
    return record


def _utc_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _validate_comment(document: object, what: str, tag: str) -> dict[str, Any]:
    """The GitHub comment object shape for owner receipt evidence."""

    if (
        not isinstance(document, dict)
        or not isinstance(document.get("id"), int)
        or isinstance(document.get("id"), bool)
        or not isinstance(document.get("html_url"), str)
        or not document["html_url"]
        or not isinstance(document.get("body"), str)
    ):
        raise InputError(f"{tag}: {what} is not a GitHub comment object")
    if document.get("author_association") != "OWNER":
        raise InputError(f"{tag}: {what} author_association is not OWNER")
    user = document.get("user")
    if (
        not isinstance(user, dict)
        or not isinstance(user.get("login"), str)
        or not user["login"]
    ):
        raise InputError(f"{tag}: {what} has no user identity")
    created = _utc_timestamp(document.get("created_at"))
    updated = _utc_timestamp(document.get("updated_at"))
    if created is None or updated is None or updated < created:
        raise InputError(
            f"{tag}: {what} created_at/updated_at timestamps are missing or "
            "inconsistent"
        )
    return document


def reconcile_owner_receipt_evidence(
    calculation: Mapping[str, Any],
    digest: str,
    policy_id: str,
    compiled: object,
    comment_a: Mapping[str, Any],
    comment_b: Mapping[str, Any],
    tag: str,
) -> None:
    """Bind the compiled receipt, both posted comments and the canonical digest.

    The compiled receipt must equal the contract's owner_decision_receipts
    entry and carry the canonical policy payload digest; comment A binds its
    comment_id/evidence_url; the posted response readback binds its
    digest_comment_id/digest_evidence_url and carries the digest in its single
    fenced machine record. No body or prose is emitted anywhere.
    """

    if not isinstance(compiled, dict) or set(compiled) != COMPILED_RECEIPT_KEYS:
        raise InputError(
            f"{tag}: compiled owner receipt does not carry the compiled receipt key set"
        )
    if compiled["decision_id"] != CUSTODY_DECISION_ID:
        raise InputError(
            f"{tag}: compiled owner receipt decision_id {compiled['decision_id']!r} "
            f"is not {CUSTODY_DECISION_ID}"
        )
    digests = owner_decision_receipt_digests(calculation, tag)
    if CUSTODY_DECISION_ID not in digests:
        raise InputError(
            f"{tag}: contract owner_decision_receipts lacks {CUSTODY_DECISION_ID}"
        )
    if digests[CUSTODY_DECISION_ID] != digest:
        raise InputError(
            f"{tag}: contract owner_decision_receipts payload does not match the "
            "canonical policy payload digest"
        )
    if compiled["payload_sha256"] != digest:
        raise InputError(
            f"{tag}: compiled owner receipt payload_sha256 does not match the "
            "canonical policy payload digest"
        )
    contract_receipt = calculation["owner_decision_receipts"][CUSTODY_DECISION_ID]
    if compiled != contract_receipt:
        raise InputError(
            f"{tag}: compiled owner receipt is not structurally equal to the "
            "contract's owner_decision_receipts entry"
        )
    if (
        comment_a["id"] != compiled["comment_id"]
        or comment_a["html_url"] != compiled["evidence_url"]
    ):
        raise InputError(
            f"{tag}: comment A id/html_url do not match the compiled receipt "
            "comment_id/evidence_url"
        )
    if (
        comment_b["id"] != compiled["digest_comment_id"]
        or comment_b["html_url"] != compiled["digest_evidence_url"]
    ):
        raise InputError(
            f"{tag}: posted response id/html_url do not match the compiled "
            "receipt digest_comment_id/digest_evidence_url"
        )
    record = owner_receipt_evidence_record(comment_b["body"], tag)
    if (
        record["authority_issue"] != compiled["authority_issue"]
        or record["policy_owner_issue"] != compiled["policy_owner_issue"]
        or record["approved_on"] != compiled["approved_on"]
    ):
        raise InputError(
            f"{tag}: posted response record does not bind the compiled receipt "
            "(issues/date)"
        )
    if record["approval_evidence_url"] != comment_a["html_url"]:
        raise InputError(
            f"{tag}: posted response record approval_evidence_url does not bind "
            "comment A"
        )
    if record["policy_id"] != policy_id:
        raise InputError(
            f"{tag}: posted response record policy_id {record['policy_id']!r} does "
            f"not match the selected policy identity {policy_id!r}"
        )
    record_digests = record["decision_payload_sha256"]
    if (
        not isinstance(record_digests, dict)
        or record_digests.get(CUSTODY_DECISION_ID) != digest
    ):
        raise InputError(
            f"{tag}: posted response readback does not bind the canonical policy "
            "payload digest"
        )


def _v3_authority(files: Mapping[str, InputFile], label: str) -> V3Authority:
    """Validate the v3 authority bundle together; any inconsistency fails closed.

    Identity (sha256/bytes) of every family is enforced by the manifest pins.
    Here the selected contract, the FX generation identity (including its
    artifact records), the daily-rates identity, the native approved
    provider-capital policy, and the paired owner receipts are checked against
    each other; the posted response readback must bind the canonical policy
    payload digest. Comment bodies are parsed only for the fenced machine
    record; bodies and prose are never emitted in notes or metric rows.
    """

    tag = f"v3 authority {label}"
    try:
        contract = json.loads(files[MVP_CONTRACT].data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{tag}: financial_source_contract does not parse") from exc
    if not isinstance(contract, dict):
        raise InputError(f"{tag}: financial_source_contract is not a JSON object")
    segments = {
        str(segment.get("source_id")): segment
        for segment in contract.get("source_segments", [])
        if isinstance(segment, dict)
    }
    fx_segment = segments.get("portfolio-fx-rates")
    if fx_segment is None:
        raise InputError(f"{tag}: contract has no portfolio-fx-rates source segment")
    current = fx_segment.get("current_generation")
    current = current if isinstance(current, dict) else {}
    contract_generation = current.get("generation_id")
    if not isinstance(contract_generation, str) or not re.fullmatch(
        r"[0-9a-f]{64}", contract_generation
    ):
        raise InputError(
            f"{tag}: contract fx current_generation.generation_id is missing or "
            "malformed"
        )
    interval = fx_segment.get("interval")
    interval = interval if isinstance(interval, dict) else {}
    observed_start = iso_date(interval.get("observed_start"))
    observed_end = iso_date(interval.get("observed_end"))
    if observed_start is None or observed_end is None or observed_start > observed_end:
        raise InputError(
            f"{tag}: contract fx observed interval is missing or malformed"
        )
    generations: dict[str, str] = {}
    for role in (FX_GENERATION, FX_USDC, FX_HISTORY, FX_RATES):
        generation = _v3_generation_of(files[role].path)
        if generation is None:
            raise InputError(
                f"{tag}: {role} path {files[role].path} has no "
                "generations/<sha256> component"
            )
        generations[role] = generation
    if len(set(generations.values())) != 1:
        raise InputError(
            f"{tag}: FX inputs span multiple generations "
            f"{sorted(set(generations.values()))}"
        )
    (generation,) = tuple(set(generations.values()))
    if generation != contract_generation:
        raise InputError(
            f"{tag}: contract selects fx generation {contract_generation} but the "
            f"pinned FX inputs live under {generation}"
        )
    if current.get("sha256") != files[FX_GENERATION].sha256:
        raise InputError(
            f"{tag}: contract fx current_generation.sha256 does not match the "
            "selected generation manifest bytes"
        )
    selector = str(fx_segment.get("endpoint_or_artifact_selector") or "")
    rates_pin = re.search(r"\brates ([0-9a-f]{64})\b", selector)
    if rates_pin is None or rates_pin.group(1) != files[FX_RATES].sha256:
        raise InputError(
            f"{tag}: contract fx selector does not pin the selected daily-rates bytes"
        )
    try:
        generation_doc = json.loads(files[FX_GENERATION].data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{tag}: fx generation manifest does not parse") from exc
    if not isinstance(generation_doc, dict):
        raise InputError(f"{tag}: fx generation manifest is not a JSON object")
    missing_fields = [
        field
        for field in (
            "adapter",
            "artifacts",
            "as_of_date",
            "coverage",
            "generation_id",
            "manifest_schema_version",
            "verification",
        )
        if field not in generation_doc
    ]
    if missing_fields:
        raise InputError(
            f"{tag}: fx generation manifest lacks required field(s) {missing_fields}"
        )
    if generation_doc.get("generation_id") != generation:
        raise InputError(
            f"{tag}: fx generation manifest generation_id "
            f"{generation_doc.get('generation_id')!r} does not match the "
            f"selected generation {generation}"
        )
    artifacts = generation_doc.get("artifacts")
    if not isinstance(artifacts, list):
        raise InputError(f"{tag}: fx generation manifest artifacts is not a list")
    # Bind each selected FX file's identity to its generation artifact record
    # (both raw currency families may share one raw artifact record).
    for role, (expected_name, expected_version) in (
        (FX_HISTORY, FX_RAW_ARTIFACT_SCHEMA),
        (FX_USDC, FX_RAW_ARTIFACT_SCHEMA),
        (FX_RATES, FX_RATES_ARTIFACT_SCHEMA),
    ):
        artifact_id = _v3_artifact_id(files[role].path)
        records = [
            record
            for record in artifacts
            if isinstance(record, dict) and record.get("artifact_id") == artifact_id
        ]
        if len(records) != 1:
            raise InputError(
                f"{tag}: artifact {artifact_id!r} is not recorded exactly once in "
                "the selected generation manifest"
            )
        record = records[0]
        schema = record.get("schema")
        if (
            not isinstance(schema, dict)
            or schema.get("name") != expected_name
            or str(schema.get("version")) != expected_version
        ):
            raise InputError(
                f"{tag}: artifact {artifact_id!r} schema {schema!r} is not "
                f"{expected_name!r} version {expected_version!r}"
            )
        if (
            record.get("sha256") != files[role].sha256
            or record.get("size_bytes") != files[role].size
        ):
            raise InputError(
                f"{tag}: artifact {artifact_id!r} sha256/size_bytes does not match "
                "the selected bytes"
            )
    # The native approved provider-capital policy: an approved-decision object
    # structurally equal to the selected contract's provider_custody_perimeter.
    calculation = _outcome_calculation_contract(contract, "INVESTED_CAPITAL", tag)
    # Native placement: the policy decision lives directly at
    # calculation_contract.provider_custody_perimeter. A missing direct field,
    # a nested-only placement, or a conflicting nested copy is never authority.
    perimeter = calculation.get("provider_custody_perimeter")
    if perimeter is None:
        raise InputError(
            f"{tag}: contract calculation_contract has no direct "
            "provider_custody_perimeter (native placement)"
        )
    treatments = calculation.get("classification_treatments")
    nested = (
        treatments.get("provider_custody_perimeter")
        if isinstance(treatments, dict)
        else None
    )
    if nested is not None and nested != perimeter:
        raise InputError(
            f"{tag}: contract carries a conflicting nested "
            "classification_treatments.provider_custody_perimeter"
        )
    _validate_policy_decision(perimeter, f"{tag}: contract provider_custody_perimeter")
    policy_id = calculation.get("policy_id")
    if not isinstance(policy_id, str) or not policy_id:
        raise InputError(f"{tag}: contract calculation_contract has no policy_id")
    try:
        policy_doc = json.loads(files[CAPITAL_POLICY].data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{tag}: provider-capital policy does not parse") from exc
    _validate_policy_decision(policy_doc, f"{tag}: provider-capital policy")
    if policy_doc != perimeter:
        raise InputError(
            f"{tag}: the selected provider-capital policy is not structurally "
            "equal to the contract's approved provider_custody_perimeter"
        )
    digest = _policy_payload_digest(perimeter)
    # The paired receipts: comment A and the compiled digest receipt are two
    # distinct pinned identities, and the posted response readback is a third.
    receipt_a = files[RECEIPT_A]
    receipt_b = files[RECEIPT_B]
    if receipt_a.path == receipt_b.path or receipt_a.sha256 == receipt_b.sha256:
        raise InputError(
            f"{tag}: the paired owner receipts are not two distinct identities"
        )
    response = files[RECEIPT_B_RESPONSE]
    if response.path in {receipt_a.path, receipt_b.path} or response.sha256 in {
        receipt_a.sha256,
        receipt_b.sha256,
    }:
        raise InputError(
            f"{tag}: the posted response readback is not distinct from the receipt pair"
        )
    try:
        comment_a = json.loads(receipt_a.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{tag}: comment A does not parse") from exc
    try:
        compiled = json.loads(receipt_b.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{tag}: compiled owner receipt does not parse") from exc
    try:
        comment_b = json.loads(response.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{tag}: posted response readback does not parse") from exc
    _validate_comment(comment_a, "comment A", tag)
    _validate_comment(comment_b, "posted response readback", tag)
    reconcile_owner_receipt_evidence(
        calculation, digest, policy_id, compiled, comment_a, comment_b, tag
    )
    notes = (
        "v3 authority: financial_source_contract "
        f"{files[MVP_CONTRACT].path} (sha256 {files[MVP_CONTRACT].sha256}) "
        f"selects FX generation {generation} observed {_day(observed_start)}.."
        f"{_day(observed_end)}; the four FX inputs share that generation and "
        "the contract's generation-manifest and daily-rates pins match the "
        "selected bytes; every selected FX file matches its generation "
        "artifact record (schema, sha256, size)",
        "v3 authority: provider-capital policy "
        f"{files[CAPITAL_POLICY].path} (sha256 {files[CAPITAL_POLICY].sha256}) "
        "is structurally the selected contract's approved "
        f"provider_custody_perimeter (policy {policy_id}, canonical payload "
        f"digest {digest[:12]}...); custody strategies are qualified by the "
        "contract census and the crosswalk, not by policy prose",
        "v3 authority: compiled owner receipt "
        f"{receipt_b.path} (sha256 {receipt_b.sha256}) equals the contract's "
        f"owner_decision_receipts entry; comment A {receipt_a.path} (sha256 "
        f"{receipt_a.sha256}) binds its comment_id/evidence_url; the posted "
        f"response readback {response.path} (sha256 {response.sha256}) binds "
        "its digest_comment_id/digest_evidence_url and carries the canonical "
        "policy payload digest in its single fenced record; bodies and prose "
        "are never emitted",
    )
    return V3Authority(
        generation, observed_start, observed_end, policy_id, digest, contract, notes
    )


@dataclass(frozen=True)
class MatrixportNativeSubscription:
    """One native subscription order (terminal status, direct share/NAV fields)."""

    event_day: dt.date
    recognition_day: dt.date
    shares: Decimal
    amount: Decimal
    currency: str
    confirm_nav: Decimal
    order_id: str


@dataclass(frozen=True)
class MatrixportNative:
    """The validated native Matrixport source bundle for the custody account."""

    product_id: str
    subscriptions: tuple[MatrixportNativeSubscription, ...]
    nav: Mapping[dt.date, Decimal]
    deposits: tuple[tuple[dt.date, Decimal, str, str], ...]
    unsupported: tuple[tuple[dt.date, str], ...]
    witnesses: int
    generation: str
    source_run: str


def _v3_native_matrixport(
    files: Mapping[str, InputFile],
    selection: Mapping[str, Any],
    cutoff: dt.date,
    label: str,
) -> MatrixportNative:
    """Validate and load the admitted native Matrixport source interface.

    native-matrixport-confirmed-source-interface/1: all five families are
    hash/size-bound to one selected generation's artifact records; the
    generation-selection evidence binds the generation id, qualification,
    finality, run and the manifest/catalog digests; the owner history is
    complete with zero withdrawals in this admitted perimeter; subscriptions
    carry native shares and confirm NAV with exact same-product
    confirmation-date agreement. Bill rows are witnesses or unsupported
    transfer/redemption evidence only: unknown numeric codes are never
    assigned a subscription meaning and never create a cash event.
    """

    tag = f"v3 native matrixport {label}"
    for required_field in (
        "generation_id",
        "qualification",
        "finalized_through",
        "source_run_id",
        "generation_manifest_sha256",
        "catalog_sha256",
        "finality_evidence_path",
        "finality_evidence_sha256",
        "finality_evidence_sha256_verified_V",
    ):
        if required_field not in selection:
            raise InputError(
                f"{tag}: generation_selection_evidence.matrixport lacks {required_field}"
            )
    generation_id = selection["generation_id"]
    if not isinstance(generation_id, str) or not re.fullmatch(
        r"[0-9a-f]{64}", generation_id
    ):
        raise InputError(f"{tag}: native generation_id is missing or malformed")
    if selection["qualification"] != "STAGED_COMPLETE":
        raise InputError(
            f"{tag}: native Matrixport qualification "
            f"{selection['qualification']!r} is not STAGED_COMPLETE"
        )
    finalized = iso_date(selection["finalized_through"])
    if finalized is None or finalized < cutoff:
        raise InputError(
            f"{tag}: native Matrixport generation is finalized through "
            f"{selection['finalized_through']!r}, before the cutoff"
        )
    run = selection["source_run_id"]
    if not isinstance(run, str) or not run:
        raise InputError(f"{tag}: native source_run_id is missing")
    generation_file = files[MP_NATIVE_GENERATION]
    catalog_file = files[MP_NATIVE_CATALOG]
    if selection["generation_manifest_sha256"] != generation_file.sha256:
        raise InputError(
            f"{tag}: selection generation_manifest_sha256 does not match the "
            "selected generation manifest bytes"
        )
    if selection["catalog_sha256"] != catalog_file.sha256:
        raise InputError(
            f"{tag}: selection catalog_sha256 does not match the selected catalog bytes"
        )
    if (
        not isinstance(selection["finality_evidence_path"], str)
        or not selection["finality_evidence_path"]
    ):
        raise InputError(f"{tag}: finality_evidence_path is missing")
    if not re.fullmatch(r"[0-9a-f]{64}", str(selection["finality_evidence_sha256"])):
        raise InputError(f"{tag}: finality_evidence_sha256 is malformed")
    if selection["finality_evidence_sha256_verified_V"] is not True:
        raise InputError(f"{tag}: finality evidence is not verified")
    # Path bindings: generation/catalog under generations/<id>/, the three
    # outputs under output/<run>/.
    for role in (MP_NATIVE_GENERATION, MP_NATIVE_CATALOG):
        if _v3_generation_of(files[role].path) != generation_id:
            raise InputError(
                f"{tag}: {files[role].path} is outside the selected native "
                f"generation {generation_id}"
            )
    for role in (MP_NATIVE_SUBSCRIPTION, MP_NATIVE_COVERAGE, MP_NATIVE_REQUESTS):
        normalized = files[role].path.replace("\\", "/")
        if f"output/{run}/" not in normalized:
            raise InputError(
                f"{tag}: {files[role].path} is outside the selected native "
                f"source run {run}"
            )
    try:
        generation_doc = json.loads(generation_file.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{tag}: native generation manifest does not parse") from exc
    if not isinstance(generation_doc, dict):
        raise InputError(f"{tag}: native generation manifest is not a JSON object")
    if generation_doc.get("generation_id") != generation_id:
        raise InputError(
            f"{tag}: native generation manifest generation_id "
            f"{generation_doc.get('generation_id')!r} does not match the "
            f"selected generation {generation_id}"
        )
    artifacts = generation_doc.get("artifacts")
    if not isinstance(artifacts, list):
        raise InputError(f"{tag}: native generation manifest artifacts is not a list")
    for artifact_id, role in MP_NATIVE_ARTIFACT_ROLES.items():
        records = [
            record
            for record in artifacts
            if isinstance(record, dict) and record.get("artifact_id") == artifact_id
        ]
        if len(records) != 1:
            raise InputError(
                f"{tag}: artifact {artifact_id!r} is not recorded exactly once in "
                "the selected native generation manifest"
            )
        record = records[0]
        expected_name = (
            "matrixport-acceptance-jsonl"
            if artifact_id.endswith(".jsonl")
            else "matrixport-canonical-csv"
        )
        schema = record.get("schema")
        if (
            not isinstance(schema, dict)
            or schema.get("name") != expected_name
            or str(schema.get("version")) != "1"
        ):
            raise InputError(
                f"{tag}: artifact {artifact_id!r} schema {schema!r} is not "
                f"{expected_name!r} version '1'"
            )
        if (
            record.get("sha256") != files[role].sha256
            or record.get("size_bytes") != files[role].size
        ):
            raise InputError(
                f"{tag}: artifact {artifact_id!r} sha256/size_bytes does not "
                "match the selected bytes"
            )
    # Catalog: run and observed boundary agreement.
    try:
        catalog = json.loads(catalog_file.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{tag}: native generation catalog does not parse") from exc
    if not isinstance(catalog, dict):
        raise InputError(f"{tag}: native generation catalog is not a JSON object")
    if catalog.get("source_run_id") != run:
        raise InputError(
            f"{tag}: catalog source_run_id {catalog.get('source_run_id')!r} does "
            f"not match the selected run {run!r}"
        )
    if catalog.get("provider_id") != "matrixport":
        raise InputError(f"{tag}: catalog provider_id is not matrixport")
    boundary = iso_date(catalog.get("observed_boundary_date"))
    if boundary is None:
        raise InputError(f"{tag}: catalog observed_boundary_date is missing")
    if catalog.get("coverage_status") != "COMPLETE_WITHIN_DECLARED_BOUNDARY":
        raise InputError(f"{tag}: catalog coverage is not complete")
    if catalog.get("known_gaps") != []:
        raise InputError(f"{tag}: catalog declares known gaps")
    # Owner-flow coverage: exactly one row for each admitted endpoint, selected
    # from a possibly larger coverage set (unrelated endpoints are allowed);
    # missing or duplicate required endpoints remain rejected.
    coverage_rows = csv_rows(files[MP_NATIVE_COVERAGE].data)
    raw_rows: dict[str, int] = {}
    for endpoint in MP_NATIVE_WALLET_ENDPOINTS:
        matching = [row for row in coverage_rows if row.get("endpoint") == endpoint]
        if len(matching) != 1:
            raise InputError(
                f"{tag}: owner-flow coverage has {len(matching)} row(s) for the "
                f"required endpoint {endpoint}"
            )
        row = matching[0]
        if row.get("status") != "complete_to_observed_boundary":
            raise InputError(
                f"{tag}: coverage for {endpoint} is not complete_to_observed_boundary"
            )
        if (
            row.get("terminal_reason")
            != "structured_parameter_rejections_dispositioned"
        ):
            raise InputError(
                f"{tag}: coverage for {endpoint} has an unexpected terminal_reason"
            )
        updated = _utc_timestamp(row.get("updated_at_utc"))
        if updated is None:
            raise InputError(
                f"{tag}: coverage for {endpoint} has no UTC capture timestamp"
            )
        if updated.date() != boundary:
            raise InputError(
                f"{tag}: coverage for {endpoint} captured "
                f"{updated.date().isoformat()} does not match the catalog "
                f"observed boundary {_day(boundary)}"
            )
        try:
            details = json.loads(row.get("details_json") or "")
        except json.JSONDecodeError as exc:
            raise InputError(f"{tag}: coverage details_json does not parse") from exc
        count = details.get("raw_rows") if isinstance(details, dict) else None
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise InputError(f"{tag}: coverage for {endpoint} lacks raw_rows")
        raw_rows[endpoint] = count
    if raw_rows[MP_NATIVE_WALLET_ENDPOINTS[1]] != 0:
        raise InputError(
            f"{tag}: native owner history lists "
            f"{raw_rows[MP_NATIVE_WALLET_ENDPOINTS[1]]} withdrawal row(s); this "
            "admitted perimeter requires zero"
        )
    # Native owner funding: complete confirmed deposits with transaction
    # identity and original UTC times.
    deposits: list[tuple[dt.date, Decimal, str, str]] = []
    for row in csv_rows(files[MP_DEPOSITS].data):
        amount = decimal_or_none(row.get("amount"))
        moment = _utc_timestamp(row.get("created_at_utc"))
        txid = row.get("transaction_id", "")
        if (
            row.get("state") not in MP_NATIVE_DEPOSIT_STATES
            or amount is None
            or amount <= 0
            or not row.get("currency")
            or moment is None
            or not txid
        ):
            raise InputError(
                f"{tag}: native deposit row does not parse as a complete "
                "confirmed deposit"
            )
        deposits.append((moment.date(), amount, str(row["currency"]), txid))
    if len(deposits) != raw_rows[MP_NATIVE_WALLET_ENDPOINTS[0]]:
        raise InputError(
            f"{tag}: coverage declares {raw_rows[MP_NATIVE_WALLET_ENDPOINTS[0]]} "
            f"deposit row(s) but the deposits artifact carries {len(deposits)}"
        )
    # Product scope: exactly one product in the native NAV evidence.
    nav_rows = csv_rows(files[MP_NAV].data)
    products = {row.get("product_id", "") for row in nav_rows}
    if len(products) != 1 or not next(iter(products), ""):
        raise InputError(
            f"{tag}: native nav_daily carries products {sorted(products)}; "
            "exactly one is required"
        )
    product = next(iter(products))
    nav: dict[dt.date, Decimal] = {}
    for row in nav_rows:
        day = iso_date(row.get("day_timestamp_utc"))
        value = decimal_or_none(row.get("nav"))
        if day is None or value is None or value <= 0:
            raise InputError(f"{tag}: native nav_daily row does not parse")
        if day in nav and nav[day] != value:
            raise InputError(f"{tag}: conflicting native NAV on {_day(day)}")
        nav[day] = value
    # Native subscriptions: terminal orders with direct shares and confirm NAV.
    subscriptions: list[MatrixportNativeSubscription] = []
    for row in csv_rows(files[MP_NATIVE_SUBSCRIPTION].data):
        if row.get("source_endpoint") != MP_NATIVE_ORDER_ENDPOINT:
            raise InputError(
                f"{tag}: subscription row source_endpoint "
                f"{row.get('source_endpoint')!r} is not {MP_NATIVE_ORDER_ENDPOINT}"
            )
        if row.get("status") != MP_NATIVE_TERMINAL_STATUS:
            raise InputError(
                f"{tag}: subscription row status {row.get('status')!r} is not terminal"
            )
        shares = decimal_or_none(row.get("subscription_share"))
        confirm_nav = decimal_or_none(row.get("confirm_nav"))
        amount = decimal_or_none(row.get("actual_subscription_amount"))
        event = _utc_timestamp(row.get("subscription_time_utc"))
        confirm = _utc_timestamp(row.get("confirm_nav_time_utc"))
        if (
            shares is None
            or shares <= 0
            or confirm_nav is None
            or confirm_nav <= 0
            or amount is None
            or amount <= 0
            or event is None
            or confirm is None
        ):
            raise InputError(
                f"{tag}: native subscription row {row.get('order_id')!r} does not parse"
            )
        currency = row.get("subscription_currency", "")
        if currency not in FX_TICKER_BY_CURRENCY:
            raise InputError(
                f"{tag}: subscription currency {currency!r} is not a supported "
                "currency with a selected FX authority"
            )
        if row.get("product_id") != product:
            raise InputError(
                f"{tag}: subscription product {row.get('product_id')!r} is "
                f"outside the admitted product scope {product!r}"
            )
        confirm_day = confirm.date()
        if nav.get(confirm_day) != confirm_nav:
            raise InputError(
                f"{tag}: subscription confirm_nav {confirm_nav} does not equal "
                f"the same-product NAV on the confirmation date "
                f"{_day(confirm_day)} (no stitching, interpolation, or "
                "recognition-date moves)"
            )
        subscriptions.append(
            MatrixportNativeSubscription(
                event.date(),
                confirm_day,
                shares,
                amount,
                currency,
                confirm_nav,
                str(row.get("order_id")),
            )
        )
    # Wallet bills under the native interface: conversion legs stay internal;
    # unknown numeric codes stay unclassified witnesses (never a subscription
    # mapping, never a cash event); anything explicitly labeled (e.g. a
    # transfer or redemption) is unsupported evidence.
    wallet = parse_matrixport_wallet(
        files[MP_DEPOSITS].data,
        files[MP_BILLS].data,
        files[MP_DISPOSITIONS].data,
        files[MP_NAV].data,
    )
    seen = {bill.currency for bill in wallet.bills} | {c for _, _, c, _ in deposits}
    blocked = sorted(seen.intersection(wallet.rejected_currencies))
    unsupported: list[tuple[dt.date, str]] = []
    if blocked:
        unsupported.append(
            (cutoff, f"wallet history unreadable for {blocked} (currency dispositions)")
        )
    _, _, _, ambiguous, paired = _classify_bills_full(wallet)
    witnesses = 0
    for index, bill in enumerate(wallet.bills):
        if index in paired:
            continue
        if index in ambiguous:
            unsupported.append(
                (bill.day, f"matrixport:bill:{bill.moment_ms}: {ambiguous[index]}")
            )
            continue
        if bill.tx_type.strip().isdigit():
            witnesses += 1
            continue
        unsupported.append(
            (
                bill.day,
                f"matrixport:bill:{bill.moment_ms}: unsupported wallet bill "
                f"tx_type {bill.tx_type!r} ({bill.amount} {bill.currency})",
            )
        )
    return MatrixportNative(
        product_id=product,
        subscriptions=tuple(subscriptions),
        nav=nav,
        deposits=tuple(sorted(deposits)),
        unsupported=tuple(unsupported),
        witnesses=witnesses,
        generation=generation_id,
        source_run=run,
    )


def _matrixport_source_native(
    account_id: str,
    ledger_values: Mapping[dt.date, Decimal],
    ledger: Ledger,
    files: Mapping[str, InputFile],
    fx: FxSourceV3,
    native: MatrixportNative,
    cutoff: dt.date,
) -> AccountSource:
    """Native Matrixport source: direct subscription shares and confirm NAV
    drive the fund path; recognition is the native confirmation date (never a
    guessed later NAV date); FX gates per the selected generation."""

    flows = AccountFlows()
    source = AccountSource(
        account_id,
        "MATRIXPORT",
        {},
        flows,
        (
            MP_NAV,
            MP_DEPOSITS,
            MP_BILLS,
            MP_DISPOSITIONS,
            FX_HISTORY,
            FX_USDC,
            MP_NATIVE_GENERATION,
            MP_NATIVE_CATALOG,
            MP_NATIVE_SUBSCRIPTION,
            MP_NATIVE_COVERAGE,
            MP_NATIVE_REQUESTS,
        ),
        (
            MP_NAV,
            MP_NATIVE_SUBSCRIPTION,
            FX_HISTORY,
            MP_NATIVE_GENERATION,
            MP_NATIVE_CATALOG,
        ),
    )
    units_added: list[tuple[dt.date, Decimal]] = []
    fx_days: list[dt.date] = []
    missing_fx: list[dt.date] = []
    missing_mapping: list[dt.date] = []
    for sub in native.subscriptions:
        rate = fx.rate(sub.currency, sub.recognition_day)
        if rate is None:
            if fx.close(sub.currency, sub.recognition_day) is None:
                missing_fx.append(sub.recognition_day)
            else:
                missing_mapping.append(sub.recognition_day)
            continue
        units_added.append((sub.recognition_day, sub.shares))
        fx_days.append(sub.recognition_day)
        flows.flows.append(
            Flow(
                sub.recognition_day,
                sub.amount * rate,
                sub.event_day,
                "STRATEGY_SUBSCRIPTION",
                f"matrixport:order:{sub.order_id}",
                dated_by="nav_confirm_date",
            )
        )
    values: dict[dt.date, Decimal] = {}
    if units_added:
        first_day = min(day for day, _ in units_added)
        for day in sorted(native.nav):
            if day < first_day or day > cutoff:
                continue
            rate = fx.rate(MATRIXPORT_PRODUCT_CURRENCY, day)
            if rate is None:
                if fx.close(MATRIXPORT_PRODUCT_CURRENCY, day) is None:
                    missing_fx.append(day)
                else:
                    missing_mapping.append(day)
                continue
            units = sum((s for rec, s in units_added if rec <= day), ZERO)
            values[day] = units * native.nav[day] * rate
            fx_days.append(day)
    source.values = values
    if missing_fx or missing_mapping:
        parts = []
        if missing_fx:
            parts.append(
                f"{len(missing_fx)} raw NAV/confirm date(s) "
                + ", ".join(_day(day) for day in sorted(set(missing_fx))[:5])
                + " have no pinned USDT-USD close in the selected generation "
                "(no interpolation, parity, or date relabeling)"
            )
        if missing_mapping:
            parts.append(
                f"{len(missing_mapping)} raw NAV/confirm date(s) "
                + ", ".join(_day(day) for day in sorted(set(missing_mapping))[:5])
                + " have no supported USDT-USD daily-rates mapping in the "
                "selected generation"
            )
        source.value_issue = "R6 v3 FX fail closed: " + "; ".join(parts)
    for day, description in native.unsupported:
        source.raw_events.append((day, description))
    source.flow_coverage_end = max(native.nav) if native.nav else None
    units_total = sum((s for _, s in units_added), ZERO)
    source.value_notes.append(
        f"native holding: {len(native.subscriptions)} native subscription(s) -> "
        f"units {str(units_total)[:22]}; product {native.product_id}; "
        f"{len(source.values)} raw valuations "
        f"{_day(min(source.values)) if source.values else '-'}.."
        f"{_day(max(source.values)) if source.values else '-'}"
    )
    source.value_notes.append(_fx_provenance_v3(fx, sorted(set(fx_days))))
    ledger_rates = {
        day: Decimal(str(item["rate"]))
        for day, row in ledger.nav_rows.get(account_id, {}).items()
        for item in row.get("valuation_fx", [])
        if item.get("currency") == MATRIXPORT_PRODUCT_CURRENCY
    }
    common = sorted(set(ledger_rates).intersection(fx.usdt).intersection(fx_days))
    differing = [day for day in common if ledger_rates[day] != fx.usdt[day]]
    largest = max(
        (abs(ledger_rates[day] - fx.usdt[day]) for day in common), default=ZERO
    )
    source.value_notes.append(
        f"cross-check provider-ledger valuation_fx: {len(common) - len(differing)}/"
        f"{len(common)} rates equal the raw Close exactly, max |difference| "
        f"{largest:.1E}"
    )
    source.flow_notes.append(
        f"native owner history: {len(native.deposits)} confirmed deposit(s) "
        f"{sum((a for _, a, _, _ in native.deposits), ZERO)} "
        f"{sorted({c for _, _, c, _ in native.deposits})} with transaction "
        "identity, complete to the observed boundary (zero withdrawals in this "
        f"admitted perimeter); {native.witnesses} unclassified numeric-code "
        "wallet bill(s) are witnesses only (no subscription mapping, no cash "
        f"event); {len(native.unsupported)} unsupported transfer/redemption "
        f"evidence row(s); native generation {native.generation[:8]} run "
        f"{native.source_run} (STAGED_COMPLETE)"
    )
    source.value_notes.extend(
        _ledger_cross_check(
            source.values,
            ledger_values,
            cutoff,
            "after the last raw observation",
        )
    )
    source.notes.extend(source.flow_notes + source.value_notes)
    return source


def _matrixport_native_cash_events(
    native: MatrixportNative, fx: FxSourceV3, cutoff: dt.date
) -> tuple[list[CashEvent], list[str]]:
    """R8 custody events under the native interface: confirmed deposits are
    owner funding; the native subscription's original amount at its event-date
    FX is the allocation; unsupported transfer/redemption evidence fails
    closed. Withdrawals do not exist in this admitted perimeter."""

    events: list[CashEvent] = []
    failures: list[str] = []
    for day, amount, currency, txid in native.deposits:
        if day > cutoff:
            continue
        record = f"matrixport:deposit:{txid}"
        usd = _convert_event(fx, currency, day, amount, record, failures)
        if usd is not None:
            events.append(CashEvent(day, usd, "deposit", record))
    for sub in native.subscriptions:
        if sub.event_day > cutoff:
            continue
        record = f"matrixport:order:{sub.order_id}"
        usd = _convert_event(
            fx, sub.currency, sub.event_day, sub.amount, record, failures
        )
        if usd is not None:
            events.append(CashEvent(sub.event_day, -usd, "allocation", record))
    for day, description in native.unsupported:
        if day <= cutoff:
            failures.append(description)
    return events, failures


def _matrixport_source_v3(
    account_id: str,
    ledger_values: Mapping[dt.date, Decimal],
    ledger: Ledger,
    files: Mapping[str, InputFile],
    fx: FxSourceV3,
    cutoff: dt.date,
) -> AccountSource:
    """R6 v3 Matrixport source: the same raw evidence as the v2 path, but FX is
    the owner-selected generation with original-UTC-date closes only; a missing
    admitted rate on a NAV or confirm date fails the value series closed (no
    v2 boundary, no FX-unconverted carry, no interpolation)."""

    wallet: MatrixportWallet = parse_matrixport_wallet(
        files[MP_DEPOSITS].data,
        files[MP_BILLS].data,
        files[MP_DISPOSITIONS].data,
        files[MP_NAV].data,
    )
    flows = AccountFlows()
    source = AccountSource(
        account_id,
        "MATRIXPORT",
        {},
        flows,
        (MP_BILLS, MP_DEPOSITS, MP_DISPOSITIONS, MP_NAV, FX_HISTORY),
        (MP_NAV, MP_BILLS, FX_HISTORY),
    )
    subscriptions, withdrawals, wallet_level, unsupported = _classify_bills_v3(wallet)
    units_added: list[tuple[dt.date, Decimal]] = []
    fx_days: list[dt.date] = []
    missing_fx: list[dt.date] = []
    missing_mapping: list[dt.date] = []
    for bill in subscriptions:
        confirm = next(
            (day for moment, day in wallet.nav_times if moment > bill.moment_ms), None
        )
        if confirm is None or wallet.nav_per_unit[confirm] <= 0:
            source.raw_events.append(
                (bill.day, f"subscription {bill.amount} USDT with no later raw NAV")
            )
            continue
        rate = fx.rate(MATRIXPORT_PRODUCT_CURRENCY, confirm)
        if rate is None:
            if fx.close(MATRIXPORT_PRODUCT_CURRENCY, confirm) is None:
                missing_fx.append(confirm)
            else:
                missing_mapping.append(confirm)
            continue
        units_added.append((confirm, bill.amount / wallet.nav_per_unit[confirm]))
        fx_days.append(confirm)
        flows.flows.append(
            Flow(
                confirm,
                bill.amount * rate,
                bill.day,
                "STRATEGY_SUBSCRIPTION",
                f"matrixport:bill:{bill.moment_ms}",
                dated_by="nav_confirm_date",
            )
        )
    for bill, why in unsupported:
        source.raw_events.append(
            (
                bill.day,
                f"unsupported wallet bill tx_type {bill.tx_type} "
                f"{'debit' if bill.direction == '2' else 'credit'} {bill.amount} "
                f"{bill.currency}: {why}",
            )
        )
    # Holding value per raw NAV date: units subscribed by then x NAV x USDT-USD.
    values: dict[dt.date, Decimal] = {}
    if units_added:
        first_day = min(day for day, _ in units_added)
        for day in sorted(wallet.nav_per_unit):
            if day < first_day or day > cutoff:
                continue
            rate = fx.rate(MATRIXPORT_PRODUCT_CURRENCY, day)
            if rate is None:
                if fx.close(MATRIXPORT_PRODUCT_CURRENCY, day) is None:
                    missing_fx.append(day)
                else:
                    missing_mapping.append(day)
                continue
            units = sum((u for confirm, u in units_added if confirm <= day), ZERO)
            values[day] = units * wallet.nav_per_unit[day] * rate
            fx_days.append(day)
    source.values = values
    if missing_fx or missing_mapping:
        parts = []
        if missing_fx:
            parts.append(
                f"{len(missing_fx)} raw NAV/confirm date(s) "
                + ", ".join(_day(day) for day in sorted(set(missing_fx))[:5])
                + " have no pinned USDT-USD close in the selected generation "
                "(no interpolation, parity, or date relabeling)"
            )
        if missing_mapping:
            parts.append(
                f"{len(missing_mapping)} raw NAV/confirm date(s) "
                + ", ".join(_day(day) for day in sorted(set(missing_mapping))[:5])
                + " have no supported USDT-USD daily-rates mapping in the "
                "selected generation"
            )
        source.value_issue = "R6 v3 FX fail closed: " + "; ".join(parts)
    seen = {bill.currency for bill in wallet.bills} | {c for _, _, c in wallet.deposits}
    blocked = sorted(seen.intersection(wallet.rejected_currencies))
    if blocked:
        flows.unusable.append(
            UnusableFlow(None, "wallet", f"history unreadable for {blocked}")
        )
    source.flow_coverage_end = wallet.coverage_end
    units_total = sum((u for _, u in units_added), ZERO)
    source.value_notes.append(
        f"raw holding: {len(subscriptions)} USDT subscription debit(s) "
        f"{sum((b.amount for b in subscriptions), ZERO)} -> units "
        f"{str(units_total)[:22]}; {len(source.values)} raw valuations "
        f"{_day(min(source.values)) if source.values else '-'}.."
        f"{_day(max(source.values)) if source.values else '-'}"
    )
    source.value_notes.append(_fx_provenance_v3(fx, sorted(set(fx_days))))
    ledger_rates = {
        day: Decimal(str(item["rate"]))
        for day, row in ledger.nav_rows.get(account_id, {}).items()
        for item in row.get("valuation_fx", [])
        if item.get("currency") == MATRIXPORT_PRODUCT_CURRENCY
    }
    common = sorted(set(ledger_rates).intersection(fx.usdt).intersection(fx_days))
    differing = [day for day in common if ledger_rates[day] != fx.usdt[day]]
    largest = max(
        (abs(ledger_rates[day] - fx.usdt[day]) for day in common), default=ZERO
    )
    source.value_notes.append(
        f"cross-check provider-ledger valuation_fx: {len(common) - len(differing)}/"
        f"{len(common)} rates equal the raw Close exactly, max |difference| "
        f"{largest:.1E}"
    )
    source.flow_notes.append(
        f"raw wallet: {len(wallet.deposits)} confirmed deposit(s) "
        f"{sum((a for _, a, _ in wallet.deposits), ZERO)} "
        f"{sorted({c for _, _, c in wallet.deposits})} and "
        f"{len(wallet_level)} wallet-level bill(s) stay outside the strategy holding; "
        f"{len(subscriptions)} subscription(s) are holding inflows (EOD at the NAV "
        f"confirm date); {len(withdrawals)} owner withdrawal(s) leave the wallet "
        f"(no units, no fund flow); {len(unsupported)} unsupported bill(s); "
        f"deposit/withdrawal "
        f"history rejected for {len(wallet.rejected_currencies)} currency code(s), "
        + ("none used by the account" if not blocked else f"including {blocked}")
    )
    ledger_flows = ledger.flows.get(account_id, AccountFlows())
    ledger_days = [flow.event_date for flow in ledger_flows.flows] + [
        flow.effective_date
        for flow in ledger_flows.unusable
        if flow.effective_date is not None
    ]
    source.flow_notes.append(
        f"provider-ledger flows for this account: "
        f"{len(ledger_flows.flows) + len(ledger_flows.unusable)} record(s), latest "
        f"{_day(max(ledger_days)) if ledger_days else '-'} (not used)"
    )
    source.value_notes.extend(
        _ledger_cross_check(
            source.values,
            ledger_values,
            cutoff,
            "after the last raw observation",
        )
    )
    source.notes.extend(source.flow_notes + source.value_notes)
    return source


@dataclass(frozen=True)
class CashEvent:
    """One R8 custody cash movement in USD (funding +, allocation -)."""

    day: dt.date
    amount: Decimal
    kind: str
    record: str


def _convert_event(
    fx: FxSourceV3,
    currency: str,
    day: dt.date,
    amount: Decimal,
    record: str,
    failures: list[str],
) -> Decimal | None:
    """Convert one custody event at its original UTC event-date close.

    USD converts natively at one. A supported non-USD currency needs both the
    pinned raw close on that exact date and a supported daily-rates mapping
    for that (currency, date); either gap fails closed.
    """

    if currency == "USD":
        return amount
    if currency not in ("USDT", "USDC"):
        failures.append(
            f"{record}: unsupported currency {currency!r} for owner funding"
        )
        return None
    close = fx.close(currency, day)
    if close is None:
        failures.append(
            f"{record}: no pinned {currency}-USD close on {_day(day)} in the "
            "selected FX generation (no interpolation, parity, or date relabeling)"
        )
        return None
    if (currency, day) not in fx.mappings:
        failures.append(
            f"{record}: no supported {currency}-USD daily-rates mapping on "
            f"{_day(day)} in the selected generation (missing or inconsistent "
            "authority)"
        )
        return None
    return amount * close


def _quantbase_cash_events(
    activity: bytes, accounts: bytes, allocations: Sequence[Flow], cutoff: dt.date
) -> tuple[list[CashEvent], list[str], bool]:
    """R8 owner funding + allocation events for the Quantbase custody account.

    Legacy deposits/withdrawals keep their existing semantics (account-level
    owner flows in USD, dated by ``time``). The native owner-funding schema is
    a completed transfer: ``activity_type`` transfer with ``type``
    DEPOSIT/WITHDRAWAL, status C, the original aware UTC timestamp in
    ``created_at`` (``time``/``filled_at`` may be blank), a positive USD
    ``amount`` with ``amount_cents`` integral and exactly ``amount * 100``, a
    nonempty source ``id``, and the custody account identified by
    ``destination_account_id`` (deposit) or ``origin_account_id``
    (withdrawal) inside the pinned accounts.csv scope. Identical source events
    are deduplicated; a conflicting duplicate identity, a malformed qualifying
    event, an unknown subtype, or an ambiguous account scope fails closed.
    Completed native fee journals (activity_type journal, status COMPLETE,
    type PARTNER_SUB_FEE/AUM_FEE) are known internal fees -- investment
    results outside the owner/allocation legs that never enter the DERIVED
    cash residual; any other journal row fails closed explicitly. Sleeve
    BUY/SELL fills
    (already parsed into ``allocations``) move cash into/out of the fund. The
    third return value reports whether any native transfer row was seen (the
    accounts.csv scope binding was consulted).
    """

    events: list[CashEvent] = []
    unsupported: list[str] = []
    scope = {row.get("id", "") for row in csv_rows(accounts) if row.get("id")}
    native_seen = False
    seen_native: dict[str, tuple[str, ...]] = {}
    bound_ids: set[str] = set()
    for row in csv_rows(activity):
        kind = row.get("activity_type", "")
        subtype = row.get("type", "")
        record = f"quantbase:{kind}:{row.get('id')}"
        time_day = iso_date(row.get("time"))
        if time_day is not None and time_day > cutoff:
            continue
        if subtype in QUANTBASE_FEE_JOURNAL_TYPES or kind == "journal":
            # Known internal fees (completed native journals of the two admitted
            # types) are investment results: never owner funding, allocations,
            # or redemptions, and never part of the DERIVED cash residual. The
            # qualification is exact (journal kind + COMPLETE status + known
            # fee type); anything else fails closed -- no blanket journal skip.
            if (
                kind == "journal"
                and subtype in QUANTBASE_FEE_JOURNAL_TYPES
                and row.get("status", "") == "COMPLETE"
            ):
                continue
            day = time_day or iso_date(row.get("created_at"))
            if day is None or day <= cutoff:
                unsupported.append(
                    f"{record}: activity_type {kind!r} type {subtype!r} status "
                    f"{row.get('status', '')!r} is not an admitted internal fee "
                    "journal (journal kind, COMPLETE status, and a known fee "
                    "type are required)"
                )
            continue
        if kind == "transfer":
            moment = _utc_timestamp(row.get("created_at"))
            day = moment.date() if moment is not None else None
            if day is not None and day > cutoff:
                continue
            if subtype not in QUANTBASE_TRANSFER_TYPES:
                unsupported.append(
                    f"{record}: activity_type 'transfer' type {subtype!r} is not "
                    "a recognized custody event"
                )
                continue
            native_seen = True
            amount = decimal_or_none(row.get("amount"))
            cents = decimal_or_none(row.get("amount_cents"))
            custody_id = row.get(QUANTBASE_TRANSFER_TYPES[subtype], "")
            source_id = row.get("id", "")
            if (
                not source_id
                or day is None
                or amount is None
                or amount <= 0
                or cents is None
                or cents != cents.to_integral_value()
                or cents != amount * 100
                or row.get("status", "") != "C"
                or not custody_id
            ):
                unsupported.append(
                    f"{record}: malformed qualifying owner transfer (source id, "
                    "aware UTC created_at, positive amount, integral "
                    "amount_cents == amount * 100, completed status C, and a "
                    "custody account id are all required)"
                )
                continue
            if custody_id not in scope:
                unsupported.append(
                    f"{record}: custody account {custody_id!r} is outside the "
                    "admitted account scope"
                )
                continue
            bound_ids.add(custody_id)
            fingerprint = (
                subtype,
                str(amount),
                str(cents),
                row.get("status", ""),
                row.get("created_at", ""),
                custody_id,
            )
            if source_id in seen_native:
                if seen_native[source_id] != fingerprint:
                    unsupported.append(
                        f"{record}: conflicting duplicate source identity {source_id!r}"
                    )
                continue
            seen_native[source_id] = fingerprint
            events.append(
                CashEvent(
                    day,
                    amount if subtype == "DEPOSIT" else -amount,
                    "deposit" if subtype == "DEPOSIT" else "withdrawal",
                    record,
                )
            )
        elif kind in ("deposit", "withdrawal"):
            amount = decimal_or_none(row.get("amount"))
            if time_day is None or amount is None or amount <= 0:
                unsupported.append(f"{record} does not parse as an owner {kind}")
                continue
            events.append(
                CashEvent(
                    time_day, amount if kind == "deposit" else -amount, kind, record
                )
            )
        elif kind not in QUANTBASE_CUSTODY_KNOWN_TYPES:
            unsupported.append(
                f"{record}: activity_type {kind!r} is not a recognized custody event"
            )
    if len(bound_ids) > 1:
        unsupported.append(
            f"native owner transfers bind multiple custody accounts "
            f"{sorted(bound_ids)} (ambiguous account scope)"
        )
    for flow in allocations:
        if flow.event_date <= cutoff:
            events.append(
                CashEvent(flow.event_date, -flow.amount, "allocation", flow.record_id)
            )
    return events, unsupported, native_seen


def _matrixport_cash_events(
    wallet: MatrixportWallet, fx: FxSourceV3, cutoff: dt.date
) -> tuple[list[CashEvent], list[str]]:
    """R8 owner funding + allocation events for the Matrixport custody wallet.

    Confirmed wallet deposits are owner inflows; their wallet credit legs
    (same currency, amount and UTC day) are not counted twice. Conversion legs
    stay internal (same pairing rule as the fund-only path). An unpaired
    product-currency debit with the subscription tx_type is a strategy
    subscription (allocation out of cash); a stablecoin debit whose tx_type
    marks a withdrawal (product or non-product) is an owner outflow. Anything
    else -- an unmatched credit, a redemption-style product-currency credit,
    an explicitly unsupported product-currency debit (e.g. a transfer), an
    ambiguous pairing, or a non-stablecoin bill -- is unsupported and fails
    the custody derivation closed.
    """

    events: list[CashEvent] = []
    failures: list[str] = []
    subscriptions, withdrawals, wallet_level, unsupported_bills = _classify_bills_v3(
        wallet
    )
    seen = {bill.currency for bill in wallet.bills} | {c for _, _, c in wallet.deposits}
    blocked = sorted(seen.intersection(wallet.rejected_currencies))
    if blocked:
        failures.append(
            f"wallet history unreadable for {blocked} (currency dispositions)"
        )
    credit_legs = [
        bill
        for bill, why in wallet_level
        if why == "non-product currency" and bill.direction == "1"
    ]
    for day, amount, currency in wallet.deposits:
        if day > cutoff:
            continue
        record = f"matrixport:deposit:{day.isoformat()}:{amount}:{currency}"
        if amount <= 0:
            failures.append(f"{record} has a non-positive amount")
            continue
        usd = _convert_event(fx, currency, day, amount, record, failures)
        if usd is None:
            continue
        leg = next(
            (
                bill
                for bill in credit_legs
                if bill.currency == currency
                and bill.amount == amount
                and bill.day == day
            ),
            None,
        )
        if leg is not None:
            credit_legs.remove(leg)
        events.append(CashEvent(day, usd, "deposit", record))
    for bill in credit_legs:
        if bill.day <= cutoff:
            failures.append(
                f"unpaired {bill.currency} credit bill {bill.amount} on "
                f"{_day(bill.day)} matches no confirmed deposit "
                f"(tx_type {bill.tx_type!r})"
            )
    for bill, why in wallet_level:
        if bill.day > cutoff:
            continue
        record = f"matrixport:bill:{bill.moment_ms}"
        if why in ("conversion debit", "conversion credit"):
            continue
        if why != "non-product currency":
            failures.append(f"{record}: {why}")
            continue
        if bill.direction == "1":
            # A stablecoin credit was already accounted for above: consumed as
            # a confirmed deposit's wallet leg or reported as unmatched.
            continue
        if (
            bill.currency in STABLECOINS
            and bill.tx_type.strip().lower() in MATRIXPORT_WITHDRAWAL_TX_TYPES
        ):
            usd = _convert_event(
                fx, bill.currency, bill.day, bill.amount, record, failures
            )
            if usd is not None:
                events.append(CashEvent(bill.day, -usd, "withdrawal", record))
        else:
            failures.append(
                f"unsupported wallet bill {record}: {bill.amount} {bill.currency} "
                f"{'debit' if bill.direction == '2' else 'credit'} "
                f"tx_type {bill.tx_type!r}"
            )
    for bill in withdrawals:
        # A supported USDT owner withdrawal: reduces the owner cash line and
        # invested capital, and never creates units or fund flows.
        if bill.day > cutoff:
            continue
        record = f"matrixport:bill:{bill.moment_ms}"
        usd = _convert_event(fx, bill.currency, bill.day, bill.amount, record, failures)
        if usd is not None:
            events.append(CashEvent(bill.day, -usd, "withdrawal", record))
    for bill, why in unsupported_bills:
        if bill.day <= cutoff:
            failures.append(f"matrixport:bill:{bill.moment_ms}: {why}")
    for bill in subscriptions:
        if bill.day > cutoff:
            continue
        record = f"matrixport:bill:{bill.moment_ms}"
        usd = _convert_event(
            fx, MATRIXPORT_PRODUCT_CURRENCY, bill.day, bill.amount, record, failures
        )
        if usd is not None:
            events.append(CashEvent(bill.day, -usd, "allocation", record))
    return events, failures


@dataclass(frozen=True)
class CustodyFacts:
    """The derived R8 custody facts for one bound custody account."""

    invested: Decimal
    allocations: Decimal
    cash: Decimal
    events: int
    last_event: dt.date | None
    inputs: tuple[str, ...]
    notes: tuple[str, ...]
    # The earliest owner-funding event (the ledger's flows fact type) and the
    # earliest sleeve allocation (its capital_anchors fact type) of this oracle's
    # own event set, or None when the line has none of that type.
    first_flow: dt.date | None = None
    first_allocation: dt.date | None = None


def _custody_facts(
    strategy_id: str,
    source: AccountSource,
    files: Mapping[str, InputFile],
    fx: FxSourceV3,
    cutoff: dt.date,
    native: MatrixportNative | None = None,
) -> CustodyFacts | str:
    """R8 custody facts for one bound custody account, or a failure reason.

    Owner funding (deposits minus withdrawals) and fund allocations come only
    from the pinned raw provider files, each converted once at the original
    UTC event-date close of the selected FX generation. Events are aggregated
    per UTC date (no invented intraday ordering); a cash line strictly below
    zero on any observed event date fails closed before any output rounding.
    When the native Matrixport interface is selected, its validated deposits,
    subscriptions and evidence bundle replace the bill-heuristic events.
    """

    failures: list[str] = []
    if source.provider == "QUANTBASE":
        if source.flows.unusable:
            failures.append(
                f"{len(source.flows.unusable)} unclassified or unusable fund "
                "transaction(s): "
                + ", ".join(
                    f"{u.record_id} ({u.reason})" for u in source.flows.unusable[:3]
                )
            )
        events, unsupported, native_transfers = _quantbase_cash_events(
            files[QB_ACTIVITY].data, files[QB_ACCOUNTS].data, source.flows.flows, cutoff
        )
        failures.extend(unsupported)
        inputs: tuple[str, ...] = (
            (QB_ACTIVITY, QB_ACCOUNTS) if native_transfers else (QB_ACTIVITY,)
        )
    elif source.provider == "MATRIXPORT":
        if native is not None:
            events, native_failures = _matrixport_native_cash_events(native, fx, cutoff)
            failures.extend(native_failures)
            inputs = (
                MP_DEPOSITS,
                MP_NATIVE_SUBSCRIPTION,
                MP_BILLS,
                MP_DISPOSITIONS,
                FX_HISTORY,
                FX_USDC,
                MP_NATIVE_GENERATION,
                MP_NATIVE_CATALOG,
                MP_NATIVE_COVERAGE,
                MP_NATIVE_REQUESTS,
            )
        else:
            wallet = parse_matrixport_wallet(
                files[MP_DEPOSITS].data,
                files[MP_BILLS].data,
                files[MP_DISPOSITIONS].data,
                files[MP_NAV].data,
            )
            events, wallet_failures = _matrixport_cash_events(wallet, fx, cutoff)
            failures.extend(wallet_failures)
            inputs = (
                MP_DEPOSITS,
                MP_BILLS,
                MP_DISPOSITIONS,
                MP_NAV,
                FX_HISTORY,
                FX_USDC,
            )
    else:
        return f"R8 custody provider {source.provider} is not supported"
    if failures:
        return (
            f"R8 custody proof for {strategy_id} is incomplete (fail closed): "
            + "; ".join(failures)
        )
    if not any(event.kind in ("deposit", "withdrawal") for event in events):
        return (
            f"R8 custody proof for {strategy_id} is incomplete (fail closed): no "
            "qualifying owner funding (deposit/withdrawal) in the listed raw inputs"
        )
    by_day: dict[dt.date, Decimal] = {}
    invested = ZERO
    allocations = ZERO
    for event in events:
        by_day[event.day] = by_day.get(event.day, ZERO) + event.amount
        if event.kind == "allocation":
            allocations -= event.amount
        else:
            invested += event.amount
    running = ZERO
    for day in sorted(by_day):
        running += by_day[day]
        if running < ZERO:
            return (
                f"R8 owner cash line for {strategy_id} is {running} (below zero) "
                f"on {_day(day)} after aggregating that day's selected events; "
                "fails closed before any output rounding or tolerance"
            )
    notes = (
        f"R8 {strategy_id} custody: owner net funding {_money(invested)} less net "
        f"fund allocations {_money(allocations)} = cash line {_money(running)}; "
        f"{len(events)} event(s) on {len(by_day)} UTC date(s) through the "
        "cutoff; non-USD amounts converted once at the original UTC event-date "
        "close of the selected FX generation; the cash line earns no fund "
        "return and is derived (the account cash balance is not an admitted "
        "observation)",
    )
    return CustodyFacts(
        invested=invested,
        allocations=allocations,
        cash=running,
        events=len(events),
        last_event=max(by_day) if by_day else None,
        inputs=inputs,
        notes=notes,
        first_flow=min(
            (e.day for e in events if e.kind in ("deposit", "withdrawal")),
            default=None,
        ),
        first_allocation=min(
            (e.day for e in events if e.kind == "allocation"), default=None
        ),
    )


def _contiguous_segment_end(
    windows: Sequence[tuple[dt.date, dt.date]],
    earliest: dt.date | None,
    fact_type: str,
) -> dt.date | str:
    """End of the merged window segment that holds the earliest event of one type.

    Windows join when ``next.start <= previous.end + 1 day``: a day between two
    disjoint windows is attested by nothing, so a later window's end is never
    bridged to. A fact type with no event uses the earliest segment. Returns the
    reason when the type has no window or its earliest event is in none.
    """

    if not windows:
        return (
            f"the pinned provider ledger has no AUTHORITATIVE ACCOUNT_LIVE "
            f"{fact_type} admission window for this provider and account"
        )
    segments: list[list[dt.date]] = []
    for start, end in sorted(windows):
        if segments and start <= segments[-1][1] + dt.timedelta(days=1):
            segments[-1][1] = max(segments[-1][1], end)
        else:
            segments.append([start, end])
    if earliest is None:
        return segments[0][1]
    for start, end in segments:
        if start <= earliest <= end:
            return end
    return (
        f"the earliest {fact_type} event {_day(earliest)} lies inside no "
        "AUTHORITATIVE ACCOUNT_LIVE admission window of the pinned provider ledger"
    )


def _custody_coverage_end(
    admissions: Sequence[Admission],
    provider_id: str,
    account_id: str,
    facts: CustodyFacts,
    generation_end: dt.date | None = None,
) -> dt.date | str:
    """The unclamped coverage end of one custody cash line, or why there is none.

    Owner flows and sleeve allocations are attested by different admissions
    (ledger fact types ``flows`` and ``capital_anchors``): each end is computed on
    its own and the line is proven only through the smaller one.

    ``generation_end`` is the date of the provider generation the ledger states.  An
    admission cannot have observed a day after the generation that admitted it, so a
    window ending later is bounded by it (never extended to it): a future end in the
    ledger is not coverage.  Without a stated generation date the windows are read as
    admitted, and derivation b, which reads the raw captures, is the independent check.
    """

    ends: list[dt.date] = []
    for fact_type, earliest in (
        ("flows", facts.first_flow),
        ("capital_anchors", facts.first_allocation),
    ):
        windows = [
            (
                item.start,
                item.end if generation_end is None else min(item.end, generation_end),
            )
            for item in admissions
            if item.provider_id == provider_id
            and item.account_id == account_id
            and item.fact_type == fact_type
            # A window that begins after the generation observed nothing at all.
            and (generation_end is None or item.start <= generation_end)
        ]
        end = _contiguous_segment_end(windows, earliest, fact_type)
        if isinstance(end, str):
            return end
        ends.append(end)
    return min(ends)


def _coverage_entity(strategy_id: str, holders: Sequence[str]) -> str:
    """``<strategy>|<provider>|<account>``; the account is ``-`` when not exactly one."""

    provider = V3_CUSTODY_PROVIDERS[strategy_id].lower()
    account = holders[0] if len(holders) == 1 else "-"
    return f"{strategy_id}|{provider}|{account}"


def derive(
    qe_root: Path,
    repo_root: Path,
    *,
    cutoff: dt.date,
    model: str,
    code_commit: str,
    expected_sha256: Mapping[str, str] | None = None,
    layout: Mapping[str, str] | None = None,
    declared_cutoff: dt.date = DECLARED_CUTOFF,
    manifest: InputsManifest | None = None,
) -> dict[str, Any]:
    """Derive every metric.

    ``manifest`` (``--inputs``) supplies every input path and pin; without it the
    built-in v1 table and ``PINNED_SHA256`` are used. ``layout`` and
    ``expected_sha256`` may relocate inputs directly (synthetic tests only); a
    manifest is mutually exclusive with both (fail closed), since it must
    override them and silent overwriting would hide a conflicting source
    selection.
    """

    if manifest is not None:
        if layout is not None or expected_sha256 is not None:
            raise InputError(
                "inputs manifest (--inputs) cannot be combined with an explicit "
                "layout or expected_sha256"
            )
        layout = manifest.layout
        expected_sha256 = manifest.pins
    with localcontext() as context:
        context.prec = PRECISION
        return _derive(
            qe_root,
            repo_root,
            cutoff,
            model,
            code_commit,
            expected_sha256,
            layout,
            declared_cutoff,
            manifest,
        )


def _derive(
    qe_root: Path,
    repo_root: Path,
    cutoff: dt.date,
    model: str,
    code_commit: str,
    expected_sha256: Mapping[str, str] | None,
    layout: Mapping[str, str] | None,
    declared_cutoff: dt.date,
    manifest: InputsManifest | None,
) -> dict[str, Any]:
    pins = PINNED_SHA256 if expected_sha256 is None else expected_sha256
    moved = layout or {}
    v3 = manifest is not None and manifest.schema == V3_INPUTS_MANIFEST_SCHEMA
    files = {
        path: read_input(qe_root, moved.get(path, path), pins) for path in INPUT_ORDER
    }
    if v3:
        # The v3 authority families are pinned inputs too; under a v3 selection
        # nothing is read from a built-in default location.
        for role in V3_AUTHORITY_ORDER:
            files[role] = read_input(qe_root, moved[role], pins)
    native_selection = manifest.selection if v3 and manifest is not None else None
    if native_selection is not None:
        # The five native Matrixport families (all-or-none per the loader).
        for role in MP_NATIVE_ROLES:
            files[role] = read_input(qe_root, moved[role], pins)
    if manifest is not None:
        for item in files.values():
            listed = manifest.sizes.get(item.path)
            if listed is not None and listed != item.size:
                raise InputError(
                    f"{item.path} is {item.size} bytes, manifest lists {listed}"
                )
    authority: V3Authority | None = None
    if v3 and manifest is not None:
        authority = _v3_authority(files, manifest.path)
    native: MatrixportNative | None = None
    if native_selection is not None and manifest is not None:
        native = _v3_native_matrixport(files, native_selection, cutoff, manifest.path)
    contract_bytes: dict[str, bytes] = {}
    contract_paths: dict[str, str] = {}
    for relative in CONTRACTS:
        if v3 and relative == MVP_CONTRACT:
            # The selected financial source contract is itself a pinned v3
            # input; the repository copy is not authority (and is not read)
            # under a v3 selection.
            contract_bytes[MVP_CONTRACT] = files[MVP_CONTRACT].data
            contract_paths[MVP_CONTRACT] = files[MVP_CONTRACT].path
            continue
        contract_bytes[relative] = repo_root.joinpath(*relative.split("/")).read_bytes()
        contract_paths[relative] = relative
    crosswalk = parse_crosswalk(files[CROSSWALK].data)
    ledger = parse_ledger(
        files[LEDGER].data, parse_flow_policy(contract_bytes[FLOW_POLICY])
    )
    statuses = parse_membership_status(contract_bytes[MVP_CONTRACT])
    output = Output()
    notes = list(CONVENTION_NOTES)
    if v3 and authority is not None:
        notes.extend(V3_CONVENTION_NOTES)
        notes.extend(authority.notes)
    mismatches = [
        f"{name} {value} != {declared_cutoff}"
        for name, value in (
            ("selected cutoff", cutoff),
            ("crosswalk.calculation_cutoff_date", crosswalk.calculation_cutoff_date),
            ("crosswalk.as_of_date", crosswalk.as_of_date),
            ("provider-ledger.as_of_date", ledger.as_of_date),
        )
        + ((("inputs manifest cutoff", manifest.cutoff),) if manifest else ())
        if value != declared_cutoff
    ]
    if mismatches:
        reason = "R7 INPUT_CUTOFF_MISMATCH (fail closed): " + "; ".join(mismatches)
        notes.append(reason)
        _fail_closed(output, crosswalk, reason, v3=v3)
    else:
        membership = _membership(crosswalk, statuses, cutoff)
        if v3 and authority is not None:
            fx_v3 = _fx_source_v3(
                files[FX_HISTORY],
                files[FX_USDC],
                files[FX_RATES],
                authority.observed_start,
                authority.observed_end,
                authority.generation,
                cast(InputsManifest, manifest).path,
            )
            sources = _account_sources(
                crosswalk, ledger, files, None, cutoff, fx_v3=fx_v3, native=native
            )
            _derive_accounts(output, crosswalk, membership, sources, cutoff, ledger)
            _derive_strategies_and_portfolio_v3(
                output,
                crosswalk,
                membership,
                sources,
                cutoff,
                files,
                fx_v3,
                authority.contract,
                native,
                ledger,
            )
        else:
            fx = _fx_source(files[FX_HISTORY], contract_bytes[MVP_CONTRACT])
            sources = _account_sources(crosswalk, ledger, files, fx, cutoff)
            _derive_accounts(output, crosswalk, membership, sources, cutoff, ledger)
            _derive_strategies_and_portfolio(
                output, crosswalk, membership, sources, cutoff
            )
    if manifest is not None:
        # Per-row source provenance: every inputs_used reference names the actual
        # selected bytes. The sources are built from the fixed v1 role paths; with
        # --inputs those roles resolved through the manifest, so rebind each
        # reference to files[role].path (top-level inputs records) before emitting.
        # Under v3 the authority roles resolve the same way.
        selected = {role: item.path for role, item in files.items()}
        for metric in output.metrics:
            metric["inputs_used"] = sorted(
                {
                    selected.get(reference, reference)
                    for reference in metric["inputs_used"]
                }
            )
    output.metrics.sort(key=lambda m: (m["metric_id"], m["entity"], m["window"] or ""))
    output.not_derivable.sort(key=lambda m: (m["metric_id"], m["entity"]))
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "derivation": DERIVATION,
        "model": model,
        "code_commit": code_commit,
        "cutoff": _day(cutoff),
    }
    if manifest is not None:
        document["inputs_manifest"] = {
            "path": manifest.path,
            "sha256": manifest.sha256,
            "bytes": manifest.size,
            "schema": manifest.schema,
            "not_read": list(manifest.ignored),
        }
    if v3 and authority is not None:
        document["authority"] = {
            "profile": V3_INPUTS_MANIFEST_SCHEMA,
            "fx_generation": authority.generation,
            "fx_observed_window": [
                _day(authority.observed_start),
                _day(authority.observed_end),
            ],
            "custody_strategies": sorted(V3_CUSTODY_PROVIDERS),
            "policy_id": authority.policy_id,
            "policy_payload_digest": authority.payload_digest,
            "families": {
                family: {
                    "path": files[role].path,
                    "sha256": files[role].sha256,
                    "bytes": files[role].size,
                }
                for family, role in sorted(V3_SINGLETON_FAMILY_ROLES.items())
                if role in files
            },
        }
        if native is not None:
            document["authority"]["matrixport_native"] = {
                "interface": "native-matrixport-confirmed-source-interface/1",
                "generation": native.generation,
                "source_run": native.source_run,
            }
    input_roles = (
        list(INPUT_ORDER)
        + (list(V3_AUTHORITY_ORDER) if v3 else [])
        + (list(MP_NATIVE_ROLES) if native is not None else [])
    )
    input_records: list[dict[str, Any]] = []
    seen_input_paths: set[str] = set()
    for role in input_roles:
        item = files[role]
        if item.path in seen_input_paths:
            continue
        seen_input_paths.add(item.path)
        input_records.append(
            {"path": item.path, "sha256": item.sha256, "bytes": item.size}
        )
    return document | {
        "inputs": input_records,
        "contracts": [
            {
                "path": contract_paths[relative],
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
            for relative, data in contract_bytes.items()
        ],
        "convention_notes": notes,
        "metrics": output.metrics,
        "not_derivable": output.not_derivable,
    }


def _fail_closed(
    output: Output, crosswalk: Crosswalk, reason: str, v3: bool = False
) -> None:
    for account_id in sorted({row.account_id for row in crosswalk.rows}):
        for metric_id in ACCOUNT_METRICS:
            window = WINDOW if metric_id in WINDOWED else None
            output.missing(metric_id, account_id, window, reason)
    for strategy_id in sorted({row.strategy_id for row in crosswalk.rows}):
        for metric_id in STRATEGY_METRICS:
            window = WINDOW if metric_id in WINDOWED else None
            output.missing(metric_id, strategy_id, window, reason)
        if v3:
            output.missing(WEIGHT_METRIC, strategy_id, None, reason)
            if strategy_id in V3_CUSTODY_PROVIDERS:
                output.missing(CASH_METRIC, strategy_id, None, reason)
                output.missing(INVESTED_METRIC, strategy_id, None, reason)
                output.missing(
                    COVERAGE_METRIC,
                    _coverage_entity(
                        strategy_id,
                        sorted(
                            {
                                row.account_id
                                for row in crosswalk.rows
                                if row.strategy_id == strategy_id
                                and row.disposition == INCLUDED
                                and row.effective_end is None
                            }
                        ),
                    ),
                    None,
                    reason,
                )
    for metric_id in PORTFOLIO_METRICS:
        output.missing(metric_id, "portfolio", None, reason)
    output.missing(
        "capital.reserve.cutoff", "reserve-capital" if v3 else "reserve", None, reason
    )


@dataclass(frozen=True)
class Membership:
    funded: tuple[Assignment, ...]
    assignment_only: tuple[Assignment, ...]
    unknown: tuple[tuple[Assignment, str], ...]


def _membership(
    crosswalk: Crosswalk,
    statuses: Mapping[tuple[str, str, str, str], set[str]],
    cutoff: dt.date,
) -> Membership:
    funded: list[Assignment] = []
    assignment_only: list[Assignment] = []
    unknown: list[tuple[Assignment, str]] = []
    for row in crosswalk.rows:
        if row.disposition != INCLUDED or row.effective_start > cutoff:
            continue
        key = (
            row.provider,
            row.strategy_id,
            _day(row.effective_start),
            _day(row.effective_end) if row.effective_end else "",
        )
        found = statuses.get(key, set())
        if found == {FUNDED}:
            funded.append(row)
        elif found == {"ASSIGNMENT_ONLY"}:
            assignment_only.append(row)
        else:
            unknown.append((row, f"contract statuses {sorted(found)} for {key}"))
    return Membership(tuple(funded), tuple(assignment_only), tuple(unknown))


def _account_span(rows: Sequence[Assignment]) -> Segment | None:
    """Merge an account's funded assignments into one contiguous span."""

    ordered = sorted(rows, key=lambda row: row.effective_start)
    for earlier, later in zip(ordered, ordered[1:]):
        if earlier.effective_end is None or (
            later.effective_start != earlier.effective_end + dt.timedelta(days=1)
        ):
            return None
    return Segment(
        ordered[0].account_id, ordered[0].effective_start, ordered[-1].effective_end
    )


def _last_value(
    source: AccountSource, cutoff: dt.date
) -> tuple[dt.date, Decimal] | None:
    if source.value_issue:
        return None
    days = [day for day in source.values if day <= cutoff]
    if not days:
        return None
    day = max(days)
    return day, source.values[day]


UNMAPPED_NAV_REASON = (
    "no provider-ledger NAV observation of this account on or before the cutoff carries "
    "an admitted (MAPPED) strategy mapping, so no admitted source date exists"
)


def _mapping_withheld(ledger: Ledger, account_id: str, cutoff: dt.date) -> bool:
    """V15: the account has ledger NAV observations in the cutoff window and none MAPPED.

    Only observations dated on or before the cutoff count (a later MAPPED row cannot admit
    an account whose valued window is unmapped); a row that states no mapping_status is
    MISSING, which is not admitted either. An account with no observations in the window
    (raw-source accounts) is not judged here.
    """

    window = [
        status
        for day, status in ledger.nav_mapping.get(account_id, ())
        if day is not None and day <= cutoff
    ]
    return bool(window) and "MAPPED" not in window


def _derive_accounts(
    output: Output,
    crosswalk: Crosswalk,
    membership: Membership,
    sources: Mapping[str, AccountSource],
    cutoff: dt.date,
    ledger: Ledger,
) -> None:
    for account_id in sorted({row.account_id for row in crosswalk.rows}):
        source = sources[account_id]
        dispositions = sorted(
            {row.disposition for row in crosswalk.rows if row.account_id == account_id}
        )
        funded = [row for row in membership.funded if row.account_id == account_id]
        skipped = [
            row for row in membership.assignment_only if row.account_id == account_id
        ]
        unknown = [
            reason for row, reason in membership.unknown if row.account_id == account_id
        ]
        last = _last_value(source, cutoff)
        base_notes = [
            f"provider {source.provider}",
            "disposition " + "/".join(dispositions),
        ]
        if last is None:
            reason = (
                source.value_issue
                or "no value observation on or before the cutoff in listed inputs"
            )
            output.missing("capital.account.cutoff", account_id, None, reason)
            output.missing("date.source_last_observation", account_id, None, reason)
        else:
            day, value = last
            stale = (cutoff - day).days
            extra = [f"observation {stale} day(s) before cutoff"] if stale else []
            output.metric(
                "capital.account.cutoff",
                account_id,
                None,
                day,
                _money(value),
                "USD",
                source.value_inputs,
                base_notes + extra + source.notes,
            )
            # V15: an account whose ledger NAV rows are all UNMAPPED has no admitted
            # source; its date is not derived from the unmapped rows.
            if _mapping_withheld(ledger, account_id, cutoff):
                output.missing(
                    "date.source_last_observation",
                    account_id,
                    None,
                    UNMAPPED_NAV_REASON,
                )
            else:
                # R6 v2: the source date is the last raw observation, which for
                # Matrixport can be later than the last USD-convertible valuation.
                observed = source.raw_last_observation or day
                observed_stale = (cutoff - observed).days
                output.metric(
                    "date.source_last_observation",
                    account_id,
                    None,
                    observed,
                    _day(observed),
                    "date",
                    source.value_inputs,
                    base_notes
                    + (
                        [f"observation {observed_stale} day(s) before cutoff"]
                        if observed_stale
                        else []
                    )
                    + (
                        [f"last USD-converted valuation {_day(day)} (FX boundary)"]
                        if observed != day
                        else []
                    )
                    + source.value_notes,
                )
        windowed = (
            "flows.account.net",
            "flows.account.count",
            "twr.account",
            "mwr.account",
        )
        if unknown:
            for metric_id in windowed:
                output.missing(
                    metric_id,
                    account_id,
                    WINDOW,
                    "membership status not determinable: " + "; ".join(unknown),
                )
            continue
        if not funded:
            reason = (
                "account has no FUNDED_MEMBERSHIP interval "
                f"({'/'.join(dispositions)}); it is outside the governed product window"
            )
            for metric_id in windowed:
                output.missing(metric_id, account_id, WINDOW, reason)
            continue
        span = _account_span(funded)
        if span is None:
            for metric_id in windowed:
                output.missing(
                    metric_id,
                    account_id,
                    WINDOW,
                    "funded assignments are not contiguous",
                )
            continue
        try:
            member, member_notes = build_member(source, span, cutoff)
        except NotDerivableError as exc:
            for metric_id in windowed:
                output.missing(metric_id, account_id, WINDOW, str(exc))
            continue
        if skipped:
            member_notes = member_notes + [
                "R1/R2 excluded ASSIGNMENT_ONLY "
                + ", ".join(
                    f"{row.strategy_id} [{_day(row.effective_start)}, "
                    f"{_day(row.effective_end) if row.effective_end else 'open'}]"
                    for row in skipped
                )
            ]
        flow_total = sum((amount for _, amount, _ in member.flows), ZERO)
        inputs = source.inputs + (CROSSWALK, MVP_CONTRACT)
        output.metric(
            "flows.account.net",
            account_id,
            WINDOW,
            member.close_date,
            _money(flow_total),
            "USD",
            inputs,
            member_notes,
        )
        output.metric(
            "flows.account.count",
            account_id,
            WINDOW,
            member.close_date,
            str(len(member.flows)),
            "count",
            inputs,
            member_notes,
        )
        _emit_returns(output, "account", account_id, [member], inputs, member_notes)


def _emit_returns(
    output: Output,
    level: str,
    entity: str,
    members: Sequence[Member],
    inputs: Sequence[str],
    notes: Sequence[str],
) -> None:
    twr, twr_reason, irr, return_notes = _returns(members)
    as_of = max(member.close_date for member in members)
    if twr is None:
        output.missing(f"twr.{level}", entity, WINDOW, twr_reason)
    else:
        output.metric(
            f"twr.{level}",
            entity,
            WINDOW,
            as_of,
            _ratio(twr),
            "ratio",
            inputs,
            list(notes) + return_notes,
        )
    if irr.rate is None:
        output.missing(
            f"mwr.{level}",
            entity,
            WINDOW,
            f"XIRR {irr.status} (root bound {irr.root_bound}, "
            f"{irr.cash_flow_count} netted cash flows)",
        )
    else:
        output.metric(
            f"mwr.{level}",
            entity,
            WINDOW,
            as_of,
            _ratio(irr.rate),
            "ratio",
            inputs,
            list(notes)
            + [
                f"XIRR ACTUAL_365 annualized; {irr.cash_flow_count} netted cash "
                f"flows; root bound {irr.root_bound} (unique)"
            ],
        )


def _derive_strategies_and_portfolio(
    output: Output,
    crosswalk: Crosswalk,
    membership: Membership,
    sources: Mapping[str, AccountSource],
    cutoff: dt.date,
) -> None:
    current = [row for row in membership.funded if row.effective_end is None]
    unknown_current = [
        (row, reason) for row, reason in membership.unknown if row.effective_end is None
    ]
    unresolved = sorted({row.strategy_id for row, _ in unknown_current})
    strategies = sorted({row.strategy_id for row in current} | set(unresolved))
    portfolio_inputs = (CROSSWALK, MVP_CONTRACT)
    unknown_notes = [reason for _, reason in membership.unknown]
    if unknown_current:
        reason = (
            "current membership status not determinable (fail closed): "
            + "; ".join(reason for _, reason in unknown_current)
        )
        output.missing("universe.active_strategy_ids", "portfolio", None, reason)
        output.missing("universe.active_strategy_count", "portfolio", None, reason)
    else:
        output.metric(
            "universe.active_strategy_ids",
            "portfolio",
            None,
            cutoff,
            strategies,
            "list",
            portfolio_inputs,
            [
                f"{len(current)} current FUNDED_MEMBERSHIP rows "
                f"({len({row.account_id for row in current})} accounts) with "
                "effective_end null and effective_start <= cutoff"
            ],
        )
        output.metric(
            "universe.active_strategy_count",
            "portfolio",
            None,
            cutoff,
            str(len(strategies)),
            "count",
            portfolio_inputs,
        )
    systematic = ZERO
    systematic_ok = not unknown_notes
    systematic_inputs: set[str] = set(portfolio_inputs)
    stale_members: list[str] = []
    for strategy_id in strategies:
        if strategy_id in unresolved:
            output.missing(
                "capital.strategy.cutoff",
                strategy_id,
                None,
                "current membership status not determinable: "
                + "; ".join(
                    r for row, r in unknown_current if row.strategy_id == strategy_id
                ),
            )
            _strategy_returns(output, strategy_id, membership, sources, cutoff)
            continue
        holders = sorted(
            row.account_id for row in current if row.strategy_id == strategy_id
        )
        total = ZERO
        days: list[dt.date] = []
        strategy_inputs: set[str] = set(portfolio_inputs)
        missing = []
        for account_id in holders:
            last = _last_value(sources[account_id], cutoff)
            if last is None:
                missing.append(account_id)
                continue
            days.append(last[0])
            total += last[1]
            strategy_inputs.update(sources[account_id].value_inputs)
        if missing:
            systematic_ok = False
            output.missing(
                "capital.strategy.cutoff",
                strategy_id,
                None,
                f"no usable value observation for {missing}"
                + "".join(
                    f"; {sources[a].value_issue}"
                    for a in missing
                    if sources[a].value_issue
                ),
            )
        else:
            systematic += total
            systematic_inputs.update(strategy_inputs)
            stale_members.extend(
                f"{strategy_id} {_day(day)}"
                for day in sorted(set(days))
                if day != cutoff
            )
            output.metric(
                "capital.strategy.cutoff",
                strategy_id,
                None,
                max(days),
                _money(total),
                "USD",
                sorted(strategy_inputs),
                [f"members {holders}"]
                + (
                    [f"member dates {sorted(set(map(_day, days)))}"]
                    if len(set(days)) > 1
                    else []
                ),
            )
        _strategy_returns(output, strategy_id, membership, sources, cutoff)
    if systematic_ok:
        output.metric(
            "capital.portfolio.systematic.cutoff",
            "portfolio",
            None,
            cutoff,
            _money(systematic),
            "USD",
            sorted(systematic_inputs),
            [f"sum of {len(strategies)} active strategies"]
            + (
                [
                    "members observed before the cutoff (last raw observation, R6): "
                    + ", ".join(stale_members)
                ]
                if stale_members
                else []
            ),
        )
    else:
        output.missing(
            "capital.portfolio.systematic.cutoff",
            "portfolio",
            None,
            "a strategy capital or membership status is not derivable",
        )
    reserve_reason = (
        "no listed input carries a reserve balance: the crosswalk has no reserve "
        "row, contracts/portfolio-financial-mvp-source-v1.json account_sleeves name "
        "reserve sleeves only by redacted alias (ibkr-reserve-01, ibkr-reserve-02, "
        "schwab-reserve-01, john-hancock-reserve-01) with no account_id mapping, and "
        "JOHN_HANCOCK has no source segment"
    )
    output.missing("capital.reserve.cutoff", "reserve", None, reserve_reason)
    output.missing(
        "capital.portfolio.liquid.cutoff",
        "portfolio",
        None,
        "systematic + reserve; capital.reserve.cutoff is not derivable",
    )
    excluded = sorted(
        {row.account_id for row in crosswalk.rows if row.disposition != INCLUDED}
    )
    parts = []
    lacking = []
    retained_total = ZERO
    retained_inputs: set[str] = {CROSSWALK}
    for account_id in excluded:
        last = _last_value(sources[account_id], cutoff)
        if last is None:
            lacking.append(account_id)
        else:
            retained_total += last[1]
            retained_inputs.update(sources[account_id].value_inputs)
            parts.append(f"{account_id} {_money(last[1])} at {_day(last[0])}")
    if lacking:
        output.missing(
            "capital.retained_excluded.cutoff",
            "portfolio",
            None,
            f"excluded rows without any value observation in listed inputs: {lacking}; "
            f"observed part: {parts}",
        )
    else:
        output.metric(
            "capital.retained_excluded.cutoff",
            "portfolio",
            None,
            cutoff,
            _money(retained_total),
            "USD",
            sorted(retained_inputs),
            parts,
        )


def _custody_census_binding(
    contract: Mapping[str, Any], strategy_id: str, expected_provider: str
) -> str:
    """Qualify the custody strategy/account through the selected contract census.

    Exactly one current-census account sleeve must carry the strategy as its
    current ACTIVE_STRATEGY sleeve of the expected provider, and that
    provider's lane must be an active account provider listing the sleeve and
    sharing its source segment. Returns "" when qualified, else the failure
    reason. Generic opening-basis accounts never qualify.
    """

    census = contract.get("current_census")
    census = census if isinstance(census, dict) else {}
    sleeves = [
        sleeve
        for sleeve in census.get("account_sleeves", [])
        if isinstance(sleeve, dict) and sleeve.get("current_strategy_id") == strategy_id
    ]
    if len(sleeves) != 1:
        return f"R8 custody census binds {len(sleeves)} sleeve(s) to {strategy_id}"
    sleeve = sleeves[0]
    if sleeve.get("role") != "ACTIVE_STRATEGY":
        return (
            f"R8 custody sleeve for {strategy_id} has role "
            f"{sleeve.get('role')!r}, not ACTIVE_STRATEGY"
        )
    if sleeve.get("provider_id") != expected_provider:
        return (
            f"R8 custody sleeve for {strategy_id} maps to provider "
            f"{sleeve.get('provider_id')}, expected {expected_provider}"
        )
    lanes = [
        lane
        for lane in census.get("provider_lanes", [])
        if isinstance(lane, dict) and lane.get("provider_id") == expected_provider
    ]
    if len(lanes) != 1:
        return (
            f"R8 custody census binds {len(lanes)} provider lane(s) to "
            f"{expected_provider}"
        )
    lane = lanes[0]
    if (
        lane.get("lane_role") != "ACCOUNT_PROVIDER"
        or lane.get("lane_status") != "ACTIVE_CURRENT_MEMBERS"
    ):
        return (
            f"R8 custody provider lane {expected_provider} is not an active "
            "account provider"
        )
    if sleeve.get("sleeve_alias") not in (lane.get("account_sleeve_ids") or []):
        return (
            f"R8 custody sleeve {sleeve.get('sleeve_alias')} is not in the "
            f"{expected_provider} lane account_sleeve_ids"
        )
    segment = lane.get("source_segment_id")
    if not segment or segment not in (sleeve.get("source_segment_ids") or []):
        return (
            f"R8 custody sleeve source segments "
            f"{sleeve.get('source_segment_ids')} do not contain the lane "
            f"segment {segment!r}"
        )
    return ""


def _derive_strategies_and_portfolio_v3(
    output: Output,
    crosswalk: Crosswalk,
    membership: Membership,
    sources: Mapping[str, AccountSource],
    cutoff: dt.date,
    files: Mapping[str, InputFile],
    fx: FxSourceV3,
    contract: Mapping[str, Any],
    native: MatrixportNative | None = None,
    ledger: Ledger | None = None,
) -> None:
    """v3 strategy/portfolio rows: the v2 fund-NAV path plus the R8 custody cash.

    For the two exactly bound custody strategies, capital.strategy.cutoff is the
    observed member NAV plus the separate owner cash line, and the invested /
    cash rows are emitted when their own upstream facts are complete. Every
    other strategy keeps the v2 NAV sum. weight.strategy.cutoff rows cover the
    whole active universe and exist only when the systematic total is complete;
    a custody failure makes them and the total NOT_DERIVABLE, never a partial
    numeric total or a zero stand-in.
    """

    current = [row for row in membership.funded if row.effective_end is None]
    unknown_current = [
        (row, reason) for row, reason in membership.unknown if row.effective_end is None
    ]
    unresolved = sorted({row.strategy_id for row, _ in unknown_current})
    strategies = sorted({row.strategy_id for row in current} | set(unresolved))
    portfolio_inputs = (CROSSWALK, MVP_CONTRACT)
    unknown_notes = [reason for _, reason in membership.unknown]
    if unknown_current:
        reason = (
            "current membership status not determinable (fail closed): "
            + "; ".join(reason for _, reason in unknown_current)
        )
        output.missing("universe.active_strategy_ids", "portfolio", None, reason)
        output.missing("universe.active_strategy_count", "portfolio", None, reason)
    else:
        output.metric(
            "universe.active_strategy_ids",
            "portfolio",
            None,
            cutoff,
            strategies,
            "list",
            portfolio_inputs,
            [
                f"{len(current)} current FUNDED_MEMBERSHIP rows "
                f"({len({row.account_id for row in current})} accounts) with "
                "effective_end null and effective_start <= cutoff"
            ],
        )
        output.metric(
            "universe.active_strategy_count",
            "portfolio",
            None,
            cutoff,
            str(len(strategies)),
            "count",
            portfolio_inputs,
        )
    systematic = ZERO
    systematic_ok = not unknown_notes
    systematic_inputs: set[str] = set(portfolio_inputs)
    stale_members: list[str] = []
    capitals: dict[str, Decimal] = {}
    for strategy_id in strategies:
        if strategy_id in unresolved:
            reason = "current membership status not determinable: " + "; ".join(
                r for row, r in unknown_current if row.strategy_id == strategy_id
            )
            output.missing("capital.strategy.cutoff", strategy_id, None, reason)
            if strategy_id in V3_CUSTODY_PROVIDERS:
                output.missing(CASH_METRIC, strategy_id, None, reason)
                output.missing(INVESTED_METRIC, strategy_id, None, reason)
                output.missing(
                    COVERAGE_METRIC,
                    _coverage_entity(
                        strategy_id,
                        sorted(
                            {
                                row.account_id
                                for row, _ in unknown_current
                                if row.strategy_id == strategy_id
                            }
                        ),
                    ),
                    None,
                    reason,
                )
            _strategy_returns(output, strategy_id, membership, sources, cutoff)
            continue
        holders = sorted(
            row.account_id for row in current if row.strategy_id == strategy_id
        )
        total = ZERO
        days: list[dt.date] = []
        strategy_inputs: set[str] = set(portfolio_inputs)
        missing = []
        for account_id in holders:
            last = _last_value(sources[account_id], cutoff)
            if last is None:
                missing.append(account_id)
                continue
            days.append(last[0])
            total += last[1]
            strategy_inputs.update(sources[account_id].value_inputs)
        # R8: the custody strategies need an exact single-account binding to
        # the provider the perimeter expects, qualified by the selected
        # contract's census, before any cash fact is derived.
        facts: CustodyFacts | None = None
        custody_reason = ""
        if strategy_id in V3_CUSTODY_PROVIDERS:
            expected = V3_CUSTODY_PROVIDERS[strategy_id]
            if len(holders) != 1:
                custody_reason = (
                    f"R8 custody binding for {strategy_id} is not exactly one "
                    f"current INCLUDED_PRODUCT_EPOCH account: {holders}"
                )
            elif sources[holders[0]].provider != expected:
                custody_reason = (
                    f"R8 custody binding for {strategy_id} maps to provider "
                    f"{sources[holders[0]].provider}, expected {expected}"
                )
            else:
                census_reason = _custody_census_binding(contract, strategy_id, expected)
                if census_reason:
                    custody_reason = census_reason
                else:
                    result = _custody_facts(
                        strategy_id, sources[holders[0]], files, fx, cutoff, native
                    )
                    if isinstance(result, CustodyFacts):
                        facts = result
                    else:
                        custody_reason = result
        if strategy_id in V3_CUSTODY_PROVIDERS:
            row_inputs = sorted(
                set(facts.inputs if facts is not None else ())
                | {CROSSWALK, MVP_CONTRACT, CAPITAL_POLICY}
            )
            coverage_entity = _coverage_entity(strategy_id, holders)
            if custody_reason:
                systematic_ok = False
                output.missing(CASH_METRIC, strategy_id, None, custody_reason)
                output.missing(INVESTED_METRIC, strategy_id, None, custody_reason)
                output.missing(COVERAGE_METRIC, coverage_entity, None, custody_reason)
            elif facts is not None:
                output.metric(
                    INVESTED_METRIC,
                    strategy_id,
                    None,
                    cutoff,
                    _money(facts.invested),
                    "USD",
                    row_inputs,
                    facts.notes,
                )
                output.metric(
                    CASH_METRIC,
                    strategy_id,
                    None,
                    cutoff,
                    _money(facts.cash),
                    "USD",
                    row_inputs,
                    facts.notes,
                )
                coverage = _custody_coverage_end(
                    ledger.admissions if ledger is not None else (),
                    V3_CUSTODY_PROVIDERS[strategy_id].lower(),
                    holders[0],
                    facts,
                    ledger.generation_dates.get(
                        V3_CUSTODY_PROVIDERS[strategy_id].lower()
                    )
                    if ledger is not None
                    else None,
                )
                if isinstance(coverage, str):
                    output.missing(COVERAGE_METRIC, coverage_entity, None, coverage)
                else:
                    output.metric(
                        COVERAGE_METRIC,
                        coverage_entity,
                        None,
                        cutoff,
                        _day(coverage),
                        "date",
                        sorted({*row_inputs, LEDGER}),
                        [
                            "unclamped end of the contiguous AUTHORITATIVE "
                            "ACCOUNT_LIVE admission segment holding the earliest "
                            "owner-flow and allocation events, the smaller of the "
                            "two; the cash line is proven through min(cutoff, "
                            "this date)"
                        ],
                    )
        if missing or custody_reason:
            systematic_ok = False
            reasons = []
            if missing:
                reasons.append(
                    f"no usable value observation for {missing}"
                    + "".join(
                        f"; {sources[a].value_issue}"
                        for a in missing
                        if sources[a].value_issue
                    )
                )
            if custody_reason:
                reasons.append(custody_reason)
            output.missing(
                "capital.strategy.cutoff", strategy_id, None, "; ".join(reasons)
            )
        else:
            value = total + (facts.cash if facts is not None else ZERO)
            systematic += value
            capitals[strategy_id] = value
            systematic_inputs.update(strategy_inputs)
            row_notes = [f"members {holders}"]
            if facts is not None:
                systematic_inputs.update(facts.inputs)
                systematic_inputs.add(CAPITAL_POLICY)
                row_notes.append(
                    f"observed member NAV {_money(total)} plus separate owner "
                    f"cash {_money(facts.cash)} (R8: the cash line earns no "
                    "fund return)"
                )
            stale_members.extend(
                f"{strategy_id} {_day(day)}"
                for day in sorted(set(days))
                if day != cutoff
            )
            output.metric(
                "capital.strategy.cutoff",
                strategy_id,
                None,
                max(days),
                _money(value),
                "USD",
                sorted(
                    strategy_inputs
                    | (set(facts.inputs) if facts is not None else set())
                    | ({CAPITAL_POLICY} if facts is not None else set())
                ),
                row_notes
                + (
                    [f"member dates {sorted(set(map(_day, days)))}"]
                    if len(set(days)) > 1
                    else []
                ),
            )
        _strategy_returns(output, strategy_id, membership, sources, cutoff)
    if systematic_ok:
        output.metric(
            "capital.portfolio.systematic.cutoff",
            "portfolio",
            None,
            cutoff,
            _money(systematic),
            "USD",
            sorted(systematic_inputs),
            [
                f"sum of {len(strategies)} active strategies (custody "
                "strategies include the separate owner cash line, R8)"
            ]
            + (
                [
                    "members observed before the cutoff (last raw observation, R6): "
                    + ", ".join(stale_members)
                ]
                if stale_members
                else []
            ),
        )
        if systematic > ZERO:
            for strategy_id in strategies:
                output.metric(
                    WEIGHT_METRIC,
                    strategy_id,
                    None,
                    cutoff,
                    _ratio(capitals[strategy_id] / systematic),
                    "ratio",
                    sorted(systematic_inputs),
                    [
                        f"capital.strategy.cutoff {_money(capitals[strategy_id])} "
                        f"/ capital.portfolio.systematic.cutoff {_money(systematic)}"
                    ],
                )
        else:
            for strategy_id in strategies:
                output.missing(
                    WEIGHT_METRIC,
                    strategy_id,
                    None,
                    "systematic capital is zero; weights are undefined",
                )
    else:
        output.missing(
            "capital.portfolio.systematic.cutoff",
            "portfolio",
            None,
            "a strategy capital, custody cash line, or membership status is "
            "not derivable",
        )
        for strategy_id in strategies:
            output.missing(
                WEIGHT_METRIC,
                strategy_id,
                None,
                "dependent capital.portfolio.systematic.cutoff is not derivable",
            )
    reserve_reason = (
        "no listed input carries a reserve balance: the crosswalk has no reserve "
        "row, contracts/portfolio-financial-mvp-source-v1.json account_sleeves name "
        "reserve sleeves only by redacted alias (ibkr-reserve-01, ibkr-reserve-02, "
        "schwab-reserve-01, john-hancock-reserve-01) with no account_id mapping, and "
        "JOHN_HANCOCK has no source segment"
    )
    output.missing("capital.reserve.cutoff", "reserve-capital", None, reserve_reason)
    output.missing(
        "capital.portfolio.liquid.cutoff",
        "portfolio",
        None,
        "systematic + reserve; capital.reserve.cutoff is not derivable",
    )
    excluded = sorted(
        {row.account_id for row in crosswalk.rows if row.disposition != INCLUDED}
    )
    parts = []
    lacking = []
    retained_total = ZERO
    retained_inputs: set[str] = {CROSSWALK}
    for account_id in excluded:
        last = _last_value(sources[account_id], cutoff)
        if last is None:
            lacking.append(account_id)
        else:
            retained_total += last[1]
            retained_inputs.update(sources[account_id].value_inputs)
            parts.append(f"{account_id} {_money(last[1])} at {_day(last[0])}")
    if lacking:
        output.missing(
            "capital.retained_excluded.cutoff",
            "portfolio",
            None,
            f"excluded rows without any value observation in listed inputs: {lacking}; "
            f"observed part: {parts}",
        )
    else:
        output.metric(
            "capital.retained_excluded.cutoff",
            "portfolio",
            None,
            cutoff,
            _money(retained_total),
            "USD",
            sorted(retained_inputs),
            parts,
        )


def _strategy_returns(
    output: Output,
    strategy_id: str,
    membership: Membership,
    sources: Mapping[str, AccountSource],
    cutoff: dt.date,
) -> None:
    unknown = [
        reason for row, reason in membership.unknown if row.strategy_id == strategy_id
    ]
    if unknown:
        for metric_id in ("twr.strategy", "mwr.strategy"):
            output.missing(
                metric_id,
                strategy_id,
                WINDOW,
                "membership status not determinable: " + "; ".join(unknown),
            )
        return
    members: list[Member] = []
    member_notes: list[str] = []
    member_inputs: set[str] = {CROSSWALK, MVP_CONTRACT}
    rows = sorted(
        (row for row in membership.funded if row.strategy_id == strategy_id),
        key=lambda row: (row.effective_start, row.account_id),
    )
    for row in rows:
        try:
            member, notes = build_member(
                sources[row.account_id],
                Segment(row.account_id, row.effective_start, row.effective_end),
                cutoff,
            )
        except NotDerivableError as exc:
            for metric_id in ("twr.strategy", "mwr.strategy"):
                output.missing(metric_id, strategy_id, WINDOW, str(exc))
            return
        members.append(member)
        member_notes.extend(notes)
        member_inputs.update(sources[row.account_id].inputs)
        member_inputs.update(sources[row.account_id].value_inputs)
    if not members:
        for metric_id in ("twr.strategy", "mwr.strategy"):
            output.missing(metric_id, strategy_id, WINDOW, "no funded members")
        return
    skipped = [
        row for row in membership.assignment_only if row.strategy_id == strategy_id
    ]
    if skipped:
        member_notes.append(
            "R2 excluded ASSIGNMENT_ONLY "
            + ", ".join(
                f"{row.account_id} [{_day(row.effective_start)}, "
                f"{_day(row.effective_end) if row.effective_end else 'open'}]"
                for row in skipped
            )
        )
    _emit_returns(
        output, "strategy", strategy_id, members, sorted(member_inputs), member_notes
    )


def dumps(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, indent=1, sort_keys=False, ensure_ascii=False) + "\n"
    ).encode("utf-8")
