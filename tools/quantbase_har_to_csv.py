"""Governed, create-only conversion of a verified Quantbase HAR intake into CSV files.

Input is an intake directory produced by ``capture_quantbase_har.py`` (stage, parse,
verify). The converter first re-runs that driver's ``verify``, then re-reads the
staged raw HAR through the same reviewed web contract, one entry at a time, and
requires the sanitized form of what it read to equal ``normalized/records.json``
byte for byte. That binds every response it converts to the verified parse output.

The CSV values come from the raw response bodies, not from ``records.json``: the
contract sanitizer redacts every ``id``/``user``/``*_id`` field and any scalar that
looks like a token, which includes every fund NAV value string of 24 or more
characters, so the sanitized records cannot carry the series the CSVs exist for.
The raw bodies stay inside the verified intake; request headers, cookies and the
session JWT are never copied, and no output may contain a captured session value.

Output (``<out-root>/quantbase-har-csv/<intake-id>/``, create-only):

    source/csv/*.csv            the exact layout ``quantbase_generation.py plan`` takes
    source/RUNBOOK_QUANTBASE.md fixed provenance text, no values
    conversion-report.json      counts, hashes and flags, no values
    native-manifest.json        ``plan --native-manifest`` declaration, written last

The tool opens no network connection and reads no credential. Reports carry counts,
hashes and dates only.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
from typing import Any, Final

from capture_quantbase_har import Manifest, QuantbaseHarIntakeError, verify_intake
from quant_engine.web_contracts import (
    QUANTBASE_CONTRACT,
    WebContractError,
    canonical_extraction,
    extract_quantbase_har,
)


SCHEMA_VERSION: Final = 1
CONVERTER_ID: Final = "quantbase-har-to-csv"
VOLATIS_TITLE: Final = "Volatis Strategy"
VOLATIS_MODES: Final = ("public", "account-level")
_REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
_OUTPUT_DIR = "quantbase-har-csv"
_NO_REVIEWED_ROUTE: Final = "capture contains no reviewed Quantbase data routes"
_SAFE_TITLE = re.compile(r"^[A-Za-z0-9_&]+$")
_SESSION_HEADERS: Final = frozenset({"jwt", "authorization", "cookie", "set-cookie"})

# One tuple per generation family, in the generation tool's column order. A test
# pins these against ``quantbase_generation._HEADERS`` so the two cannot drift.
HEADERS: Final[dict[str, tuple[str, ...]]] = {
    "account_activity": (
        "account_id",
        "activity_type",
        "alpaca_activity_id",
        "amount",
        "amount_cents",
        "asset",
        "aum_fee_transaction",
        "broker_journal_id",
        "canceled_at",
        "completed_at",
        "cost_basis",
        "created_at",
        "created_to_cover_fees",
        "date",
        "description",
        "destination_account_id",
        "destination_transaction_id",
        "executed",
        "failure_reason",
        "fees",
        "filled",
        "filled_at",
        "fund",
        "fund_title",
        "id",
        "initiating_account_type",
        "is_debit",
        "is_rebalance",
        "notify_user",
        "order_id",
        "origin_account_id",
        "origin_transaction_id",
        "per_share_amount",
        "quantity",
        "status",
        "submitted_at",
        "symbol",
        "time",
        "transaction",
        "transaction_id",
        "type",
        "updated_at",
        "user",
    ),
    "accounts": (
        "id",
        "type",
        "available_balance",
        "available_balance_cents",
        "settled_balance",
        "settled_balance_cents",
        "restricted_to_liquidation",
        "funds",
    ),
    "dashboard_holdings": ("symbol", "quantity", "value", "latest_price", "percentage"),
    "dashboard_pricehistory": ("time", "value", "cost_basis", "profit_loss"),
    "dashboard_wins": ("metric", "value"),
    "fund_histdata": ("value", "time"),
    "fund_rebalance": ("date", "symbol", "percentage"),
    "fund_stats": (
        "id",
        "title",
        "live",
        "description",
        "additional_details",
        "portfolio_risks",
        "created_at",
        "brokerage",
        "rebalance_time",
        "equity_experience_min",
        "crypto_experience_min",
        "options_experience_min",
        "bonds_experience_min",
        "leverage_experience_min",
        "quantbase_fee",
        "annual_returns",
        "max_drawdown",
        "sharpe_ratio",
        "volatility",
        "correlation",
        "correlation_market",
        "min_investment",
        "key_considerations",
        "qualitative_risk_score",
        "risk_score",
        "last_rebalance",
        "scenarios",
        "partner",
        "backtest_end_date",
    ),
    "key_value": ("column", "value"),
    "ssg_pricehistory": ("fund_title", "fund_id", "value", "time"),
    "volatis_rebalance": ("date", "symbol", "proportion"),
}
REQUIRED_FILES: Final = frozenset(
    {
        "account_activity.csv",
        "accounts.csv",
        "dashboard_Total_holdings.csv",
        "dashboard_Total_pricehistory.csv",
        "dashboard_total_wins.csv",
        "projections_all_live_funds.csv",
        "ssg_all_funds_stats.csv",
        "user_profile.csv",
        "volatis_strategy_rebalance_history.csv",
        "volatis_strategy_stats.csv",
    }
)

# Key sets observed in the retained 2026-09-02 capture. Any other key is refused.
_PROFILE_KEYS: Final = frozenset(
    {
        "alpaca_id",
        "bank_account_nickname",
        "bonds_experience",
        "core_user_id",
        "crypto_agreement",
        "crypto_experience",
        "deactivated",
        "docs_uploaded",
        "docs_uploaded_at",
        "email_address",
        "equity_experience",
        "first_name",
        "id",
        "initial_funding_amount",
        "interests",
        "investment_horizon",
        "investment_objective",
        "is_qualified_investor",
        "last_name",
        "leverage_experience",
        "liquid_net_worth_min",
        "num_referred",
        "ob_complete",
        "options_experience",
        "phone_number",
        "plaid_account_id",
        "recommendations",
        "referral_code",
        "risk_tolerance",
        "source",
        "user_id",
    }
)
_ONBOARDING_KEYS: Final = frozenset(
    {
        "alpaca_app_action_required_at",
        "alpaca_app_approved_at",
        "alpaca_app_rejected_at",
        "alpaca_app_submitted_at",
        "alpaca_app_verification_requested_at",
        "bank_connected_at",
        "cash_transfer_automation_enabled",
        "cash_transfer_completed_at",
        "cash_transfer_failed_at",
        "cash_transfer_initiated_at",
        "force_meeting",
        "funding_amount_declared_at",
        "id",
        "onboarding_completed_at",
        "p13n_completed_at",
        "signed_up_at",
        "state",
        "transactions_completed_at",
        "transactions_initiated_at",
        "user",
    }
)
_DASHBOARD_FUND_KEYS: Final = frozenset(
    {"pricehistory", "fund", "wins", "holdings", "returns"}
)
_WIN_TOTAL_KEYS: Final = frozenset(
    {"percentage", "spy_percentage", "spy_value_delta", "value_delta"}
)
_WIN_MARK_KEYS: Final = frozenset({"days_ago", "fund_id", "percentage"})
_STATS_ORDER: Final = (
    "max_drawdown",
    "volatility",
    "daily_volatility",
    "sharpe",
    "monthly_volatility",
)


_TIMESTAMP_FIELDS: Final = (
    "time",
    "date",
    "completed_at",
    "filled_at",
    "created_at",
    "submitted_at",
    "updated_at",
    "canceled_at",
)
_SNAPSHOT_FILES: Final = frozenset(
    {
        "accounts.csv",
        "dashboard_total_wins.csv",
        "partner_status.csv",
        "projections_all_live_funds.csv",
        "ssg_all_funds_stats.csv",
        "user_profile.csv",
    }
)


class QuantbaseCsvError(ValueError):
    """The Quantbase HAR-to-CSV conversion failed closed."""


def _observation_day(value: object, name: str) -> date:
    if isinstance(value, str):
        text = value.strip()
        try:
            return datetime.strptime(text, "%m/%d/%Y").date()
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    raise QuantbaseCsvError(f"{name} holds a value that is not a date")


class _Cutoff:
    """The financial cutoff: later observations are excluded and counted."""

    def __init__(self, day: date) -> None:
        self.day = day
        self.excluded: Counter[str] = Counter()

    def keeps(self, dataset: str, day: date) -> bool:
        if day <= self.day:
            return True
        self.excluded[dataset] += 1
        return False


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise QuantbaseCsvError(f"{name} must be an object")
    return value


def _array(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise QuantbaseCsvError(f"{name} must be an array")
    return value


def _exact(value: object, keys: frozenset[str], name: str) -> dict[str, Any]:
    record = _object(value, name)
    if set(record) != keys:
        raise QuantbaseCsvError(
            f"{name} keys differ: missing={sorted(keys - set(record))} "
            f"extra={sorted(set(record) - keys)}"
        )
    return record


def _cell(value: object) -> str:
    """The scalar projection the retained converter and the ledger both use."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _csv_bytes(header: Sequence[str], rows: Sequence[Sequence[object]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        if len(row) != len(header):
            raise QuantbaseCsvError("internal row width differs from the header")
        writer.writerow([_cell(item) for item in row])
    return buffer.getvalue().encode("utf-8")


def _safe_name(title: str) -> str:
    name = (
        title.replace(" & ", "_and_")
        .replace(" ", "_")
        .replace("-", "_")
        .replace("'", "")
        .replace(",", "")
        .replace('"', "")
    )
    if _SAFE_TITLE.fullmatch(name) is None:
        raise QuantbaseCsvError(f"fund title has no safe file name: {title!r}")
    return name


class _Response:
    """One raw response body bound to its reviewed capability and request."""

    __slots__ = ("body", "capability", "query", "request_sha256")

    def __init__(
        self,
        capability: str,
        request_sha256: str,
        query: Mapping[str, Any],
        body: object,
    ) -> None:
        self.capability = capability
        self.request_sha256 = request_sha256
        self.query = query
        self.body = body


def _har_entries(raw: Path, names: Sequence[str]) -> list[Any]:
    entries: list[Any] = []
    for name in sorted(names):
        path = raw / name
        if path.is_symlink() or not path.is_file():
            raise QuantbaseCsvError(f"raw HAR is missing or linked: {name}")
        try:
            document = _object(json.loads(path.read_bytes()), name)
            log = _object(document.get("log"), "log")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QuantbaseCsvError(f"raw HAR is not valid JSON: {name}") from exc
        entries.extend(_array(log.get("entries"), "log.entries"))
    return entries


def _bound_responses(
    intake: Path, manifest: Manifest, record: Mapping[str, Any]
) -> list[_Response]:
    """Read raw bodies entry by entry and bind them to ``normalized/records.json``."""

    entries = _har_entries(
        intake / "raw", [str(item["name"]) for item in record["files"]]
    )
    responses: list[_Response] = []
    sanitized = []
    for entry in entries:
        single = [entry]
        try:
            extracted = extract_quantbase_har(
                {"log": {"entries": single}}, session=manifest.session
            )
        except WebContractError as exc:
            if str(exc) == _NO_REVIEWED_ROUTE:
                continue
            raise QuantbaseCsvError(f"web contract refused an entry: {exc}") from exc
        if len(extracted) != 1:
            raise QuantbaseCsvError("one entry must yield exactly one response")
        item = extracted[0]
        request = entry["request"]
        query = QUANTBASE_CONTRACT.route(request["method"], request["url"]).query(
            request["url"]
        )
        responses.append(
            _Response(
                item.capability,
                item.request_sha256,
                {
                    key: query[key]
                    for key in ("fund", "title", "end_day")
                    if key in query
                },
                json.loads(entry["response"]["content"]["text"]),
            )
        )
        sanitized.append(item)
    ordered = sorted(
        range(len(sanitized)),
        key=lambda index: (
            sanitized[index].capability,
            sanitized[index].request_sha256,
        ),
    )
    path = intake / "normalized" / "records.json"
    if path.is_symlink() or not path.is_file():
        raise QuantbaseCsvError("normalized records are missing or linked")
    if canonical_extraction(tuple(sanitized[i] for i in ordered)) != path.read_bytes():
        raise QuantbaseCsvError(
            "raw entries differ from the verified normalized records"
        )
    return [responses[i] for i in ordered]


def _single(responses: Sequence[_Response], capability: str) -> object | None:
    """The one body for a capability; repeated identical requests must agree."""

    found = [item for item in responses if item.capability == capability]
    if not found:
        return None
    if len({item.request_sha256 for item in found}) != 1:
        raise QuantbaseCsvError(f"{capability} holds more than one distinct request")
    if len({_canonical(item.body) for item in found}) != 1:
        raise QuantbaseCsvError(f"{capability} holds conflicting repeated responses")
    return found[0].body


def _by_title(
    responses: Sequence[_Response], capability: str, parameter: str
) -> dict[str, object]:
    result: dict[str, object] = {}
    groups: dict[str, list[_Response]] = {}
    for item in responses:
        if item.capability == capability:
            title = item.query.get(parameter)
            if not isinstance(title, str) or not title.strip():
                raise QuantbaseCsvError(f"{capability} request lacks a fund title")
            groups.setdefault(title, []).append(item)
    for title, group in groups.items():
        if len({item.request_sha256 for item in group}) != 1:
            raise QuantbaseCsvError(
                f"{capability} holds more than one request per fund"
            )
        if len({_canonical(item.body) for item in group}) != 1:
            raise QuantbaseCsvError(f"{capability} conflicts for a repeated fund")
        result[title] = group[0].body
    return result


def _key_value(pairs: Sequence[tuple[str, object]]) -> bytes:
    return _csv_bytes(HEADERS["key_value"], [list(pair) for pair in pairs])


def _user_profile(body: object) -> bytes:
    users = _array(body, "users.profile")
    if len(users) != 1:
        raise QuantbaseCsvError("users.profile must hold exactly one user")
    user = dict(_exact(users[0], _PROFILE_KEYS | {"ob_status"}, "users.profile[0]"))
    onboarding = _exact(
        user.pop("ob_status"), _ONBOARDING_KEYS, "users.profile.ob_status"
    )
    flat = {**user, **{f"ob_status.{key}": value for key, value in onboarding.items()}}
    return _key_value(sorted(flat.items()))


def _accounts(body: object) -> bytes:
    keys = frozenset(HEADERS["accounts"])
    document = _exact(body, frozenset({"data"}), "accounts.linked")
    rows = []
    for item in _array(document["data"], "accounts.linked.data"):
        account = _exact(item, keys, "accounts.linked.data[]")
        if not isinstance(account["funds"], list):
            raise QuantbaseCsvError("accounts.linked funds must be an array")
        rows.append([account[column] for column in HEADERS["accounts"]])
    return _csv_bytes(HEADERS["accounts"], rows)


def _account_activity(body: object, cutoff: _Cutoff) -> bytes:
    header = HEADERS["account_activity"]
    allowed = frozenset(header) - {"activity_type"}
    document = _exact(body, frozenset({"data"}), "accounts.activity")
    rows = []
    for entry in _array(document["data"], "accounts.activity.data"):
        record = _exact(entry, frozenset({"item", "type"}), "accounts.activity.data[]")
        item = _object(record["item"], "accounts.activity.item")
        if not isinstance(record["type"], str) or not set(item) <= allowed:
            raise QuantbaseCsvError(
                "accounts.activity item keys differ: "
                f"extra={sorted(set(item) - allowed)}"
            )
        days = [
            _observation_day(item[field], f"accounts.activity.{field}")
            for field in _TIMESTAMP_FIELDS
            if item.get(field) not in (None, "")
        ]
        if days and not cutoff.keeps("account_activity", max(days)):
            continue
        rows.append(
            [
                record["type"] if column == "activity_type" else item.get(column)
                for column in header
            ]
        )
    return _csv_bytes(header, rows)


def _price_rows(body: object, name: str, cutoff: _Cutoff) -> list[list[object]]:
    keys = frozenset(HEADERS["dashboard_pricehistory"])
    rows: list[list[object]] = []
    for item in _array(body, name):
        record = _exact(item, keys, name)
        if cutoff.keeps(
            "dashboard_pricehistory", _observation_day(record["time"], name)
        ):
            rows.append(
                [record[column] for column in HEADERS["dashboard_pricehistory"]]
            )
    return rows


def _dashboard(body: object, cutoff: _Cutoff) -> tuple[dict[str, bytes], str | None]:
    document = _exact(body, frozenset({"data"}), "portfolio.dashboard")
    data = _exact(
        document["data"], frozenset({"funds_data", "top_wins"}), "dashboard.data"
    )
    funds = _object(data["funds_data"], "dashboard.funds_data")
    files: dict[str, bytes] = {}
    holdings_keys = frozenset(HEADERS["dashboard_holdings"])
    for title, value in funds.items():
        fund = _exact(value, _DASHBOARD_FUND_KEYS, f"dashboard fund {title!r}")
        stem = _safe_name(title)
        name = f"dashboard_{stem}_pricehistory.csv"
        if name in files:
            raise QuantbaseCsvError("dashboard fund titles collide on file names")
        files[name] = _csv_bytes(
            HEADERS["dashboard_pricehistory"],
            _price_rows(fund["pricehistory"], "dashboard.pricehistory", cutoff),
        )
        holdings = _array(fund["holdings"], "dashboard.holdings")
        if holdings:
            files[f"dashboard_{stem}_holdings.csv"] = _csv_bytes(
                HEADERS["dashboard_holdings"],
                [
                    [
                        _exact(item, holdings_keys, "dashboard.holdings[]")[column]
                        for column in HEADERS["dashboard_holdings"]
                    ]
                    for item in holdings
                ],
            )
    # Legacy rule, kept so the retained dashboard_total_wins.csv reproduces: the
    # first fund in response order with a non-null ``wins.total`` supplies it.
    source: str | None = None
    metrics: dict[str, object] = {
        "total_value_delta": "",
        "total_percentage": "",
        "total_spy_percentage": "",
        "total_spy_value_delta": "",
    }
    for title, value in funds.items():
        wins = value["wins"]
        if wins is None or (isinstance(wins, dict) and not wins.get("total")):
            continue
        marks = _exact(
            wins, frozenset({"total", "biggest", "longest"}), "dashboard.wins"
        )
        _exact(marks["biggest"], _WIN_MARK_KEYS, "dashboard.wins.biggest")
        _exact(marks["longest"], _WIN_MARK_KEYS, "dashboard.wins.longest")
        total = _exact(marks["total"], _WIN_TOTAL_KEYS, "dashboard.wins.total")
        if source is None:
            source = title
            metrics = {
                "total_value_delta": total["value_delta"],
                "total_percentage": total["percentage"],
                "total_spy_percentage": total["spy_percentage"],
                "total_spy_value_delta": total["spy_value_delta"],
            }
    top = _exact(
        data["top_wins"], frozenset({"biggest", "longest"}), "dashboard.top_wins"
    )
    for key in ("biggest", "longest"):
        mark = _exact(top[key], _WIN_MARK_KEYS, f"dashboard.top_wins.{key}")
        metrics[f"{key}_percentage"] = mark["percentage"]
        metrics[f"{key}_days_ago"] = mark["days_ago"]
        metrics[f"{key}_fund_id"] = mark["fund_id"]
    files["dashboard_total_wins.csv"] = _csv_bytes(
        HEADERS["dashboard_wins"], [[key, metrics[key]] for key in metrics]
    )
    return files, source


def _volatis_stats(body: object) -> bytes:
    document = _exact(body, frozenset({"data"}), "funds.statistics")
    data = _object(document["data"], "funds.statistics.data")
    if set(data) != set(_STATS_ORDER):
        raise QuantbaseCsvError(
            f"funds.statistics keys differ: missing={sorted(set(_STATS_ORDER) - set(data))} "
            f"extra={sorted(set(data) - set(_STATS_ORDER))}"
        )
    return _key_value([(key, data[key]) for key in _STATS_ORDER])


def _rebalance_rows(body: object, cutoff: _Cutoff) -> list[list[object]]:
    document = _exact(body, frozenset({"data"}), "funds.rebalance-history")
    data = _object(document["data"], "funds.rebalance-history.data")
    rows: list[list[object]] = []
    for day, entry in sorted(data.items()):
        if not cutoff.keeps(
            "rebalance_history", _observation_day(day, "rebalance date")
        ):
            continue
        composition = _array(
            _exact(entry, frozenset({"composition"}), "rebalance entry")["composition"],
            "rebalance composition",
        )
        for item in composition:
            leg = _exact(item, frozenset({"symbol", "proportion"}), "rebalance leg")
            rows.append([day, leg["symbol"], leg["proportion"]])
    return rows


def _partner_status(body: object) -> bytes:
    document = _exact(body, frozenset({"data"}), "partners.status")
    data = _exact(
        document["data"],
        frozenset({"status", "next_period_end"}),
        "partners.status.data",
    )
    return _key_value(
        [("status", data["status"]), ("next_period_end", data["next_period_end"])]
    )


def _invest(
    body: object, mode: str, dashboard: object, cutoff: _Cutoff
) -> tuple[dict[str, bytes], dict[str, int]]:
    document = _exact(body, frozenset({"pageProps", "__N_SSG"}), "funds.invest")
    props = _exact(
        document["pageProps"],
        frozenset({"fundPriceData", "fundStatsData"}),
        "pageProps",
    )
    price_keys = frozenset(HEADERS["ssg_pricehistory"])
    series: dict[str, list[list[object]]] = {}
    identifiers: dict[str, object] = {}
    seen: set[tuple[str, object]] = set()
    for item in _array(props["fundPriceData"], "fundPriceData"):
        row = _exact(item, price_keys, "fundPriceData[]")
        title = row["fund_title"]
        if not isinstance(title, str) or not title:
            raise QuantbaseCsvError("fundPriceData fund_title must be text")
        if identifiers.setdefault(title, row["fund_id"]) != row["fund_id"]:
            raise QuantbaseCsvError("a fund title maps to more than one fund id")
        if (title, row["time"]) in seen:
            raise QuantbaseCsvError("fundPriceData repeats a fund and time")
        seen.add((title, row["time"]))
        rows = series.setdefault(title, [])
        if cutoff.keeps(
            "ssg_pricehistory", _observation_day(row["time"], "fundPriceData")
        ):
            rows.append([title, row["fund_id"], row["value"], row["time"]])
    if mode == "account-level":
        if VOLATIS_TITLE not in series:
            raise QuantbaseCsvError("public series lacks the Volatis fund id")
        funds = _object(
            _exact(dashboard, frozenset({"data"}), "portfolio.dashboard")["data"],
            "dashboard.data",
        )["funds_data"]
        account = _object(funds, "dashboard.funds_data").get(VOLATIS_TITLE)
        if account is None:
            raise QuantbaseCsvError("dashboard lacks the Volatis account series")
        series[VOLATIS_TITLE] = [
            [VOLATIS_TITLE, identifiers[VOLATIS_TITLE], row[1], row[0]]
            for row in _price_rows(
                _object(account, "dashboard Volatis")["pricehistory"],
                "dashboard.pricehistory",
                _Cutoff(cutoff.day),
            )
        ]
    files: dict[str, bytes] = {}
    for title in sorted(series):
        name = f"ssg_{_safe_name(title)}_pricehistory.csv"
        if name in files:
            raise QuantbaseCsvError("fund titles collide on file names")
        files[name] = _csv_bytes(HEADERS["ssg_pricehistory"], series[title])
    stats = _object(props["fundStatsData"], "fundStatsData")
    if set(stats) != {"data"}:
        raise QuantbaseCsvError("fundStatsData keys differ")
    stat_keys = frozenset(HEADERS["fund_stats"])
    rows = [
        [
            _exact(item, stat_keys, "fundStatsData.data[]")[column]
            for column in HEADERS["fund_stats"]
        ]
        for item in _array(stats["data"], "fundStatsData.data")
    ]
    if not rows:
        raise QuantbaseCsvError("fundStatsData holds no funds")
    # The retained converter wrote one fund-metadata table under both names.
    table = _csv_bytes(HEADERS["fund_stats"], rows)
    files["ssg_all_funds_stats.csv"] = table
    files["projections_all_live_funds.csv"] = table
    return files, {"price_series": len(series), "fund_stats_rows": len(rows)}


_SUPPORTED: Final = frozenset(
    {
        "users.profile",
        "accounts.linked",
        "accounts.activity",
        "portfolio.dashboard",
        "partners.status",
        "funds.statistics",
        "funds.rebalance-history",
        "funds.invest",
    }
)


def derive_csv_files(
    responses: Sequence[_Response], volatis_pricehistory: str, cutoff_day: date
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Return ``{file name: bytes}`` and value-free derivation facts.

    Time-series rows dated after ``cutoff_day`` are excluded (the raw HAR stays as
    source evidence); capture-time snapshots are written as captured.
    """

    cutoff = _Cutoff(cutoff_day)
    if volatis_pricehistory not in VOLATIS_MODES:
        raise QuantbaseCsvError("volatis price history mode is not recognised")
    present = {item.capability for item in responses}
    if present - _SUPPORTED:
        raise QuantbaseCsvError(
            f"capability has no CSV conversion: {sorted(present - _SUPPORTED)}"
        )
    files: dict[str, bytes] = {}

    def add(name: str, content: bytes) -> None:
        if name in files:
            raise QuantbaseCsvError(f"CSV file produced twice: {name}")
        files[name] = content

    def need(capability: str) -> object:
        body = _single(responses, capability)
        if body is None:
            raise QuantbaseCsvError(f"capture lacks a required response: {capability}")
        return body

    add("user_profile.csv", _user_profile(need("users.profile")))
    add("accounts.csv", _accounts(need("accounts.linked")))
    add("account_activity.csv", _account_activity(need("accounts.activity"), cutoff))
    dashboard_body = need("portfolio.dashboard")
    dashboard_files, wins_source = _dashboard(dashboard_body, cutoff)
    for name, content in dashboard_files.items():
        add(name, content)
    partner = _single(responses, "partners.status")
    if partner is not None:
        add("partner_status.csv", _partner_status(partner))
    stats = _by_title(responses, "funds.statistics", "fund")
    if set(stats) != {VOLATIS_TITLE}:
        raise QuantbaseCsvError("funds.statistics must hold exactly the Volatis fund")
    for item in responses:
        if item.capability == "funds.statistics" and (
            _observation_day(item.query.get("end_day"), "statistics end_day")
            > cutoff.day
        ):
            raise QuantbaseCsvError("funds.statistics window ends after the cutoff")
    add("volatis_strategy_stats.csv", _volatis_stats(stats[VOLATIS_TITLE]))
    rebalances = _by_title(responses, "funds.rebalance-history", "title")
    if VOLATIS_TITLE not in rebalances:
        raise QuantbaseCsvError("capture lacks the Volatis rebalance history")
    for title, body in sorted(rebalances.items()):
        rows = _rebalance_rows(body, cutoff)
        if title == VOLATIS_TITLE:
            add(
                "volatis_strategy_rebalance_history.csv",
                _csv_bytes(HEADERS["volatis_rebalance"], rows),
            )
        else:
            add(
                f"fund_rebalance_{_safe_name(title)}.csv",
                _csv_bytes(HEADERS["fund_rebalance"], rows),
            )
    invest_files, invest_facts = _invest(
        need("funds.invest"), volatis_pricehistory, dashboard_body, cutoff
    )
    for name, content in invest_files.items():
        add(name, content)
    if not REQUIRED_FILES <= set(files):
        raise QuantbaseCsvError(
            f"required datasets missing: {sorted(REQUIRED_FILES - set(files))}"
        )
    facts: dict[str, object] = {
        "excluded_after_cutoff": dict(sorted(cutoff.excluded.items())),
        "total_wins_source_fund": wins_source,
        "volatis_pricehistory": volatis_pricehistory,
        **invest_facts,
    }
    return files, facts


def _row_count(content: bytes) -> int:
    return (
        sum(1 for _ in csv.reader(io.StringIO(content.decode("utf-8"), newline=""))) - 1
    )


def _runbook(intake_id: str) -> bytes:
    text = (
        "# Quantbase HAR-derived CSV source\n\n"
        f"Produced by tools/quantbase_har_to_csv.py (schema {SCHEMA_VERSION}) from the\n"
        f"verified HAR intake {intake_id}. The CSV files are deterministic\n"
        "projections of the staged raw HAR response bodies; no live access, login or\n"
        "credential was used. Row values live only in the private source run; this\n"
        "file carries none.\n"
    )
    return text.encode("utf-8")


def _session_values(entries: Sequence[Any]) -> set[str]:
    values: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for side in ("request", "response"):
            part = entry.get(side)
            headers = part.get("headers") if isinstance(part, dict) else None
            for header in headers if isinstance(headers, list) else []:
                if (
                    isinstance(header, dict)
                    and isinstance(header.get("name"), str)
                    and header["name"].casefold() in _SESSION_HEADERS
                    and isinstance(header.get("value"), str)
                ):
                    value = header["value"]
                    values.add(value)
                    values.update(
                        piece.split("=", 1)[-1].strip() for piece in value.split(";")
                    )
    return {value for value in values if len(value) >= 8}


def _load_intake(
    intake_dir: Path,
) -> tuple[Path, Manifest, dict[str, Any], dict[str, Any]]:
    root = intake_dir
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise QuantbaseCsvError("intake must be a real absolute directory")
    root = root.resolve(strict=True)
    try:
        result = verify_intake(root)
    except QuantbaseHarIntakeError as exc:
        raise QuantbaseCsvError(f"intake does not verify: {exc}") from exc
    if not str(result.get("state", "")).startswith("VERIFIED"):
        raise QuantbaseCsvError("intake is not verified")
    try:
        manifest = Manifest(
            _object(json.loads((root / "manifest.json").read_bytes()), "manifest")
        )
        record = _object(
            json.loads((root / "intake-manifest.json").read_bytes()), "intake"
        )
        report = _object(
            json.loads((root / "parse-report.json").read_bytes()), "parse report"
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        QuantbaseHarIntakeError,
    ) as exc:
        raise QuantbaseCsvError("intake metadata is unreadable") from exc
    return root, manifest, record, report


def _hash_file(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return _sha256(data), len(data)


def _derive(
    intake_dir: Path, volatis_pricehistory: str
) -> tuple[Path, dict[str, bytes], dict[str, Any]]:
    """Every output file name (relative to the output directory) to its bytes."""

    root, manifest, record, report = _load_intake(intake_dir)
    responses = _bound_responses(root, manifest, record)
    csv_files, facts = derive_csv_files(
        responses, volatis_pricehistory, manifest.cutoff
    )
    intake_id = str(record["intake_id"])
    outputs: dict[str, bytes] = {
        f"source/csv/{name}": content for name, content in csv_files.items()
    }
    outputs["source/RUNBOOK_QUANTBASE.md"] = _runbook(intake_id)
    datasets: list[dict[str, Any]] = [
        {
            "bytes": len(content),
            "name": name,
            "rows": _row_count(content),
            "sha256": _sha256(content),
        }
        for name, content in sorted(csv_files.items())
    ]
    conversion = {
        "capture_date": manifest.session.captured_at.date().isoformat(),
        "converter": CONVERTER_ID,
        "csv_file_count": len(datasets),
        "csv_record_count": sum(int(item["rows"]) for item in datasets),
        "cutoff": manifest.cutoff.isoformat(),
        "datasets": datasets,
        "derivation": facts,
        "flags": sorted(report.get("flags", [])),
        "intake_id": intake_id,
        "manifest_sha256": record["manifest_sha256"],
        "records_sha256": report["records_sha256"],
        "snapshot_files": sorted(
            name
            for name in csv_files
            if name in _SNAPSHOT_FILES
            or (name.startswith("dashboard_") and name.endswith("_holdings.csv"))
        ),
        "runbook_sha256": _sha256(outputs["source/RUNBOOK_QUANTBASE.md"]),
        "schema_version": SCHEMA_VERSION,
    }
    outputs["conversion-report.json"] = _canonical(conversion)
    files: list[dict[str, object]] = []
    for item in sorted(record["files"], key=lambda entry: str(entry["name"])):
        files.append(
            {
                "bytes": item["bytes"],
                "path": str(root / "raw" / str(item["name"])),
                "role": "native-har",
                "sha256": item["sha256"],
            }
        )
    digest, size = _hash_file(root / "intake-manifest.json")
    files.append(
        {
            "bytes": size,
            "path": str(root / "intake-manifest.json"),
            "role": "har-intake-manifest",
            "sha256": digest,
        }
    )
    outputs["native-manifest.json"] = _canonical(
        {"cutoff": manifest.cutoff.isoformat(), "files": files, "schema_version": 1}
    )
    names = [str(item["name"]) for item in record["files"]]
    blob = b"".join(outputs.values())
    for value in _session_values(_har_entries(root / "raw", names)):
        if value.encode("utf-8") in blob:
            raise QuantbaseCsvError("derived output would carry session material")
    return root, outputs, conversion


def _output_dir(root: Path, out_root: Path, intake_id: str) -> Path:
    base = out_root
    if not base.is_absolute() or base.is_symlink() or not base.is_dir():
        raise QuantbaseCsvError("out-root must be a real absolute directory")
    base = base.resolve(strict=True)
    for other in (_REPOSITORY_ROOT, root):
        if base.is_relative_to(other) or other.is_relative_to(base):
            raise QuantbaseCsvError(
                "out-root must be disjoint from the repository and the intake"
            )
    holder = base / _OUTPUT_DIR
    if holder.is_symlink() or (holder.exists() and not holder.is_dir()):
        raise QuantbaseCsvError("output directory is linked or not a directory")
    return holder / intake_id


def convert(
    intake_dir: Path, out_root: Path, volatis_pricehistory: str
) -> dict[str, object]:
    """Derive the CSV source root and the native manifest; create-only."""

    root, outputs, conversion = _derive(intake_dir, volatis_pricehistory)
    target = _output_dir(root, out_root, str(conversion["intake_id"]))
    if target.exists():
        raise QuantbaseCsvError("output already exists; conversion is create-only")
    order = [
        *sorted(name for name in outputs if name.startswith("source/")),
        "conversion-report.json",
        "native-manifest.json",
    ]
    target.parent.mkdir(exist_ok=True)
    target.mkdir()
    try:
        for relative in order:
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(outputs[relative])
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return _summary(target, conversion)


def _summary(target: Path, conversion: Mapping[str, Any]) -> dict[str, object]:
    return {
        "csv_file_count": conversion["csv_file_count"],
        "csv_record_count": conversion["csv_record_count"],
        "cutoff": conversion["cutoff"],
        "flags": conversion["flags"],
        "intake_id": conversion["intake_id"],
        "native_manifest": str(target / "native-manifest.json"),
        "source_root": str(target / "source"),
        "state": "CONVERTED",
    }


def verify_conversion(
    intake_dir: Path, out_root: Path, volatis_pricehistory: str
) -> dict[str, object]:
    """Re-derive every output from the verified intake and compare byte for byte."""

    root, outputs, conversion = _derive(intake_dir, volatis_pricehistory)
    target = _output_dir(root, out_root, str(conversion["intake_id"]))
    if target.is_symlink() or not target.is_dir():
        raise QuantbaseCsvError("conversion output is missing or linked")
    actual = {
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual != set(outputs):
        raise QuantbaseCsvError("conversion output holds undeclared or missing paths")
    for relative, content in outputs.items():
        path = target / relative
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise QuantbaseCsvError(f"derived output differs: {relative}")
    manifest = json.loads(outputs["native-manifest.json"])
    for item in manifest["files"]:
        digest, size = _hash_file(Path(item["path"]))
        if (digest, size) != (item["sha256"], item["bytes"]):
            raise QuantbaseCsvError("native manifest file differs from its hash")
    result = _summary(target, conversion)
    result["state"] = "VERIFIED"
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("convert", "verify"):
        child = commands.add_parser(name)
        child.add_argument("--intake", type=Path, required=True)
        child.add_argument("--out-root", type=Path, required=True)
        child.add_argument(
            "--volatis-pricehistory", choices=VOLATIS_MODES, required=True
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run = convert if args.command == "convert" else verify_conversion
        result = run(args.intake, args.out_root, args.volatis_pricehistory)
    except (OSError, QuantbaseCsvError, ValueError) as exc:
        print(f"quantbase-har-to-csv: FAIL: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
