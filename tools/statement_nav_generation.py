"""Hash-bound statement NAV generations for the John Hancock and Composer reserve accounts.

Both providers publish a statement, not a daily series: John Hancock one per calendar quarter
(opening, items, closing balance), Composer one per calendar month (cash identity plus total
market value). The adapter reads the extracted statement text, closes each statement against its
own arithmetic, proves that the statements tile the declared coverage, and emits one
``nav_observations`` point per statement (``as_of_date`` = period end) together with the
``statement_windows`` that declare which dates each point speaks for. Nothing is interpolated. A
live-page balance or any date outside a statement is not admitted here.

python tools/statement_nav_generation.py build SPEC
python tools/statement_nav_generation.py stage SPEC OUTPUT_ROOT   # never writes CURRENT.json
"""

from __future__ import annotations

import argparse
from calendar import monthrange
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes

SPEC_SCHEMA_VERSION = 2
CATALOG_SCHEMA_VERSION = 1
ADAPTER_VERSION = "0.1.0"
PROVIDERS = {
    "john_hancock": {
        "adapter": "quant-engine-johnhancock",
        "catalog": "john-hancock-generation.json",
        "label": "JOHN_HANCOCK",
        "cadence": "QUARTER",
    },
    "composer": {
        "adapter": "quant-engine-composer",
        "catalog": "composer-generation.json",
        "label": "COMPOSER",
        "cadence": "MONTH",
    },
}
_PORTABLE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MONEY = r"-?\$?-?[\d,]+\.\d\d"
_MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "JANUARY",
            "FEBRUARY",
            "MARCH",
            "APRIL",
            "MAY",
            "JUNE",
            "JULY",
            "AUGUST",
            "SEPTEMBER",
            "OCTOBER",
            "NOVEMBER",
            "DECEMBER",
        ),
        start=1,
    )
}
_ZERO = Decimal("0.00")


class StatementGenerationError(ValueError):
    """The statements cannot be closed against the declared spec."""


def _money(text: str) -> Decimal:
    try:
        return Decimal(text.replace("$", "").replace(",", "")).quantize(_ZERO)
    except InvalidOperation as exc:
        raise StatementGenerationError(f"not a money amount: {text!r}") from exc


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise StatementGenerationError(f"{name} must be non-empty trimmed text")
    return value


def _date(value: object, name: str) -> date:
    try:
        return date.fromisoformat(_text(value, name))
    except ValueError as exc:
        raise StatementGenerationError(f"{name} must be an ISO date") from exc


def _dec(value: Decimal) -> str:
    return format(value, "f")


# ---------------------------------------------------------------- statement parsers


def parse_john_hancock(text: str) -> dict[str, Any]:
    """One quarterly statement: period, opening, ordered items, closing, residual."""

    lines = text.split("\n")
    period = re.search(r"STATEMENT PERIOD: (\S+) - (\S+)", lines[0] if lines else "")
    opening = re.search(r"Opening Balance\s+\$\s*([\d,\.]+)", text)
    closing = re.search(r"Closing Balance\s+\$\s*([\d,\.]+)", text)
    anchor = next((i for i, line in enumerate(lines) if "For the period" in line), None)
    if period is None or opening is None or closing is None or anchor is None:
        raise StatementGenerationError("John Hancock statement layout not recognised")
    start = datetime.strptime(period.group(1), "%m/%d/%Y").date()
    end = datetime.strptime(period.group(2), "%m/%d/%Y").date()
    opening_value = _money(opening.group(1))
    closing_value = _money(closing.group(1))
    values: list[Decimal] = []
    for line in lines[anchor : anchor + 12]:
        found = re.findall(_MONEY, line)
        if found and len(line.rstrip()) > 150:
            values.append(_money(found[-1]))
    items: list[Decimal] = []
    for value in values[1:]:
        if value == closing_value:
            break
        items.append(value)
    return {
        "period_start": start,
        "period_end": end,
        "opening": opening_value,
        "closing": closing_value,
        "items": items,
        "labelled_items": _jh_labelled_items(
            lines, items, opening_value, closing_value
        ),
        "holdings": _jh_holdings(text, closing_value),
        "closure_residual": opening_value + sum(items, _ZERO) - closing_value,
    }


_JH_ITEM_LABELS = (
    "Dividends/Interest",
    "Gain/Loss",
    "Administrative Fee",
    "Roth 401(k) Contribution",
    "General Administrative Charge",
)


def _jh_check_fund_split(
    positioned: list[tuple[int, Decimal]],
    items: list[Decimal],
    opening: Decimal,
    closing: Decimal,
    labels: list[str],
) -> None:
    """Tie each amount to its own label through the printed fund split.

    The activity table prints one value column per fund.  The left (index fund) column
    lists, in row order, the opening value, one value per row and the closing value; the
    right (brokerage) column prints the matching values, zeros sometimes omitted.  Row i's
    amount minus its left-column value must therefore be zero or a printed right-column
    value: swapping two amounts breaks that, which a count of labels alone would not see.
    """

    left = [value for start, value in positioned if start < 60]
    right = {value for start, value in positioned if 60 <= start < 120}
    if len(left) < len(items) + 2:
        raise StatementGenerationError("John Hancock fund columns not recognised")
    column = left[: len(items) + 2]
    rows = [("opening balance", opening), *zip(labels, items, strict=True)]
    rows.append(("closing balance", closing))
    for (name, amount), printed in zip(rows, column, strict=True):
        remainder = amount - printed
        if remainder != _ZERO and remainder not in right:
            raise StatementGenerationError(
                f"John Hancock {name} amount is not the sum of its printed fund split"
            )


def _jh_check_summary(
    lines: list[str], labels: list[str], items: list[Decimal]
) -> None:
    """The page-1 summary prints the contribution and the market change as totals: the
    rows labelled as such must add up to values printed there."""

    first = next(
        (i for i, line in enumerate(lines) if line.startswith("Opening Balance")), None
    )
    last = next(
        (i for i, line in enumerate(lines) if line.startswith("Closing Balance")), None
    )
    if first is None or last is None or last < first:
        raise StatementGenerationError("John Hancock summary rows not recognised")
    printed = {
        _money(token.replace(" ", ""))
        for line in lines[first : last + 1]
        for token in re.findall(r"-?\$?\s*-?[\d,]+\.\d\d", line[:85])
    }
    amounts = dict(zip(labels, items, strict=True))
    groups = {
        "contribution": amounts.get("Roth 401(k) Contribution", _ZERO),
        "market change": amounts.get("Gain/Loss", _ZERO)
        + amounts.get("Dividends/Interest", _ZERO),
    }
    for name, value in groups.items():
        if value != _ZERO and value not in printed:
            raise StatementGenerationError(
                f"John Hancock {name} rows do not add up to the printed summary total"
            )


def _jh_labelled_items(
    lines: list[str],
    items: list[Decimal],
    opening: Decimal,
    closing: Decimal,
) -> list[tuple[str, Decimal]]:
    """The statement's own row labels, in page order, paired with the ordered items.

    The text layer interleaves the two fund columns, but the label column keeps the
    page order of the rows; a label count that differs from the item count is refused.
    """

    first = next(
        (i for i, line in enumerate(lines) if line.startswith("Balance as of")), None
    )
    last = next(
        (i for i, line in enumerate(lines) if line.startswith("Net Asset Value as of")),
        None,
    )
    if first is None or last is None or last <= first:
        raise StatementGenerationError("John Hancock activity rows not recognised")
    labels = [
        label
        for line in lines[first : last + 1]
        for label in _JH_ITEM_LABELS
        if line.startswith(label)
    ]
    if len(labels) != len(items):
        raise StatementGenerationError(
            "John Hancock activity labels differ from the activity items"
        )
    end = next((i for i, line in enumerate(lines) if "Fee Definition" in line), None)
    if end is None:
        raise StatementGenerationError("John Hancock activity table end not recognised")
    order = [_JH_ITEM_LABELS.index(label) for label in labels]
    if order != sorted(set(order)):
        raise StatementGenerationError(
            "John Hancock activity labels are not in the plan's fixed row order"
        )
    positioned = [
        (match.start(), _money(match.group()))
        for line in lines[first:end]
        for match in re.finditer(_MONEY, line)
    ]
    _jh_check_fund_split(positioned, items, opening, closing, labels)
    _jh_check_summary(lines, labels, items)
    for label, item in zip(labels, items, strict=True):
        # semantics: a contribution is never a negative amount
        if label == "Roth 401(k) Contribution" and item < _ZERO:
            raise StatementGenerationError(
                "John Hancock contribution row is negative: label and amount do not agree"
            )
    return list(zip(labels, items, strict=True))


def _jh_holdings(text: str, closing: Decimal) -> list[tuple[str, Decimal]]:
    """The two fund-level holdings of the Investment Summary; they must sum to closing."""

    holdings: list[tuple[str, Decimal]] = []
    for name, pattern in (
        ("Fidelity 500 Index Fund", r"500 Index Fund\s+\$([\d,]+\.\d\d)\s+[\d.]+%"),
        ("Brokerage Account", r"Brokerage Account\s+\$([\d,]+\.\d\d)\s+[\d.]+%"),
    ):
        match = re.search(pattern, text)
        if match is None:
            raise StatementGenerationError(f"John Hancock holdings lack {name!r}")
        holdings.append((name, _money(match.group(1))))
    if sum((value for _, value in holdings), _ZERO) != closing:
        raise StatementGenerationError("John Hancock holdings differ from closing")
    return holdings


def _composer_line(text: str, label: str) -> list[Decimal]:
    match = re.search(rf"^{label}[ \t]+(.*\$.*)$", text, re.MULTILINE)
    if match is None:
        raise StatementGenerationError(f"Composer statement lacks {label!r}")
    return [
        _ZERO if token.endswith("--") else _money(token.replace(" ", ""))
        for token in re.findall(r"-?\$ ?-?[\d,]+\.\d\d|\$ --", match.group(1))
    ]


def parse_composer(text: str) -> dict[str, Any]:
    """One monthly statement: period, cash identity and total market value."""

    period = re.search(r"Monthly Statement Period: ([A-Z]+) - (\d{4})", text)
    account = re.search(r"Account No: (\S+)", text)
    if period is None or account is None or period.group(1) not in _MONTHS:
        raise StatementGenerationError("Composer statement layout not recognised")
    month, year = _MONTHS[period.group(1)], int(period.group(2))
    cash = {
        key: _composer_line(text, label)[0]
        for key, label in (
            ("beginning", "Beginning Balance"),
            ("addition", "Addition"),
            ("subtraction", "Subtraction"),
            ("trade", "Trade Transaction"),
            ("fees", "Cost and Fees"),
            ("ending", "Ending Value"),
        )
    }
    holdings = {
        key: _composer_line(text, label)[0]
        for key, label in (
            ("long", "Long"),
            ("short", "Short"),
            ("options", "Options"),
            ("fixed_income", "Fixed Income"),
            ("total", "Total Market Value"),
        )
    }
    cash_residual = (
        cash["beginning"]
        + cash["addition"]
        + cash["subtraction"]
        + cash["trade"]
        + cash["fees"]
        - cash["ending"]
    )
    total_residual = (
        holdings["long"]
        + holdings["short"]
        + holdings["options"]
        + holdings["fixed_income"]
        + cash["ending"]
        - holdings["total"]
    )
    income_summary = (
        _composer_line(text, "Dividend")[0] + _composer_line(text, r"Interest\*\*")[0]
    )
    return {
        "account_number": account.group(1),
        "period_start": date(year, month, 1),
        "period_end": date(year, month, monthrange(year, month)[1]),
        "cash": cash,
        "holdings": holdings,
        "income_summary": income_summary,
        "rows": _composer_rows(text),
        "cash_residual": cash_residual,
        "total_residual": total_residual,
    }


_COMPOSER_DATE = r"(\d\d/\d\d/\d{4})"
_COMPOSER_AMOUNT = r"(-?\$[\d,]+\.\d\d)"


def _composer_section(text: str, header: str) -> list[str]:
    """The lines of every ``header`` section (a header repeats at each page break)."""

    out: list[str] = []
    inside = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped in {"Holdings", "Income", "Fees", "Transaction", "Transactions"}:
            inside = stripped == header
            continue
        if stripped.startswith(("Deposit & Withdrawals", "STATEMENT MESSAGE")):
            inside = False
            continue
        if inside:
            out.append(line)
    return out


def _composer_rows(text: str) -> dict[str, list[dict[str, str]]]:
    """Holdings, dated income, fees and trades read from the statement tables."""

    holdings: list[dict[str, str]] = []
    for line in _composer_section(text, "Holdings"):
        if line.startswith(("Symbol ", "*Cash")):
            continue
        tokens = line.split()
        dollars = [
            i for i, token in enumerate(tokens) if token.lstrip("-").startswith("$")
        ]
        if len(tokens) < 8 or len(dollars) < 5:
            continue
        first = dollars[-5]
        holdings.append(
            {
                "symbol": tokens[0],
                "description": " ".join(tokens[1 : first - 1]),
                "quantity": tokens[first - 1],
                "price": tokens[first].replace("$", ""),
                "market_value": tokens[first + 1].replace("$", "").replace(",", ""),
                "cost_basis": tokens[-1].replace("$", "").replace(",", ""),
            }
        )
    income: list[dict[str, str]] = []
    for line in _composer_section(text, "Income"):
        match = re.match(
            rf"^{_COMPOSER_DATE} (\S+) (\S+) (.*?) {_COMPOSER_AMOUNT}$", line.strip()
        )
        if match:
            income.append(
                {
                    "date": match.group(1),
                    "entry": match.group(2),
                    "symbol": match.group(3),
                    "amount": match.group(5).replace("$", "").replace(",", ""),
                }
            )
    fees: list[dict[str, str]] = []
    for line in _composer_section(text, "Fees"):
        match = re.match(rf"^{_COMPOSER_DATE} (.*?) {_COMPOSER_AMOUNT}$", line.strip())
        if match:
            fees.append(
                {
                    "date": match.group(1),
                    "description": match.group(2),
                    "amount": match.group(3).replace("$", "").replace(",", ""),
                }
            )
    trades: list[dict[str, str]] = []
    for line in _composer_section(text, "Transaction"):
        match = re.match(
            rf"^{_COMPOSER_DATE} Trade Entry (buy|sell) (\S+) (-?[\d.]+) "
            rf"\$([\d,.]+) {_COMPOSER_AMOUNT} (?:\$ --|\$[\d,.]+)$",
            line.strip(),
        )
        if match:
            trades.append(
                {
                    "date": match.group(1),
                    "side": match.group(2),
                    "symbol": match.group(3),
                    "quantity": match.group(4),
                    "price": match.group(5).replace(",", ""),
                    "amount": match.group(6).replace("$", "").replace(",", ""),
                }
            )
    deposits: list[dict[str, str]] = []
    block = text.split("Deposit & Withdrawals", 1)
    if len(block) == 2:
        for line in block[1].split("STATEMENT MESSAGE", 1)[0].split("\n"):
            match = re.match(
                rf"^{_COMPOSER_DATE} (.*?) {_COMPOSER_AMOUNT} (\S+)$", line.strip()
            )
            if match:
                deposits.append(
                    {
                        "date": match.group(1),
                        "description": match.group(2),
                        "amount": match.group(3).replace("$", "").replace(",", ""),
                    }
                )
    return {
        "holdings": holdings,
        "income": income,
        "fees": fees,
        "trades": trades,
        "deposits": deposits,
    }


# ---------------------------------------------------------------- spec


def _load_spec(path: Path) -> dict[str, Any]:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StatementGenerationError("spec is not valid UTF-8 JSON") from exc
    keys = {
        "as_of_date",
        "baseline",
        "census_catalog",
        "coverage_end",
        "coverage_start",
        "crosswalk",
        "identity",
        "provider",
        "roots",
        "schema_version",
        "sources",
    }
    if not isinstance(spec, dict) or set(spec) != keys:
        raise StatementGenerationError("spec keys differ from the schema")
    if spec["schema_version"] != SPEC_SCHEMA_VERSION:
        raise StatementGenerationError("spec schema version is unsupported")
    if spec["provider"] not in PROVIDERS:
        raise StatementGenerationError("provider is unsupported")
    return spec


def _read_source(
    record: Mapping[str, Any], roots: Mapping[str, Path], generation_as_of: date
) -> tuple[SourceArtifact, bytes]:
    keys = {
        "artifact_id",
        "as_of_date",
        "expected_sha256",
        "expected_size_bytes",
        "relative_path",
        "role",
        "root_id",
        "statement_id",
    }
    if set(record) != keys:
        raise StatementGenerationError("source keys differ from the schema")
    artifact_id = _text(record["artifact_id"], "artifact_id")
    relative = PurePosixPath(_text(record["relative_path"], "relative_path"))
    if (
        relative.is_absolute()
        or "\\" in str(relative)
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise StatementGenerationError("relative_path must be portable")
    root = roots.get(_text(record["root_id"], "root_id"))
    if root is None:
        raise StatementGenerationError("source root is unknown")
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file():
        raise StatementGenerationError("source is absent or linked")
    digest, size = hash_file(path)
    if not _SHA256.fullmatch(str(record["expected_sha256"])) or (
        digest != record["expected_sha256"]
    ):
        raise StatementGenerationError("source hash differs")
    if size != record["expected_size_bytes"]:
        raise StatementGenerationError("source size differs")
    source_as_of = _date(record["as_of_date"], "source.as_of_date")
    if source_as_of > generation_as_of:
        raise StatementGenerationError("source is newer than generation")
    if record["role"] not in {"STATEMENT_TEXT", "STATEMENT_PDF"}:
        raise StatementGenerationError("source role is unsupported")
    schema = "statement-text" if record["role"] == "STATEMENT_TEXT" else "statement-pdf"
    return (
        SourceArtifact(
            artifact_id=artifact_id,
            sha256=digest,
            size_bytes=size,
            as_of_date=source_as_of,
            schema_name=schema,
            schema_version="1",
        ),
        path.read_bytes(),
    )


def _expected_statement_id(provider: str, start: date, end: date) -> str:
    if provider == "john_hancock":
        quarter_start = date(start.year, 3 * ((start.month - 1) // 3) + 1, 1)
        last_month = quarter_start.month + 2
        quarter_end = date(
            start.year, last_month, monthrange(start.year, last_month)[1]
        )
        if (start, end) != (quarter_start, quarter_end):
            raise StatementGenerationError("statement is not one calendar quarter")
        return f"jh-{start.year}q{(start.month - 1) // 3 + 1}"
    return f"composer-{start.year}-{start.month:02d}"


# ---------------------------------------------------------------- build


def _composer_table_closure(
    statement_id: str, parsed: Mapping[str, Any]
) -> dict[str, str]:
    """The statement's dated tables must reproduce its own summaries, else refused."""

    rows = parsed["rows"]

    def total(items: Sequence[Mapping[str, str]], key: str = "amount") -> Decimal:
        return sum((Decimal(item[key]) for item in items), _ZERO)

    cash = parsed["cash"]
    deposits = total(rows["deposits"])
    positive = sum(
        (
            Decimal(item["amount"])
            for item in rows["deposits"]
            if Decimal(item["amount"]) > 0
        ),
        _ZERO,
    )
    holdings = parsed["holdings"]
    position_sum = total(rows["holdings"], "market_value")
    summary_holdings = (
        holdings["long"]
        + holdings["short"]
        + holdings["options"]
        + holdings["fixed_income"]
    )
    residuals = {
        "income": total(rows["income"]) - parsed["income_summary"],
        "fees": total(rows["fees"]) - cash["fees"],
        "trades": total(rows["trades"]) - cash["trade"],
        "addition": cash["addition"] - total(rows["income"]) - positive,
        "subtraction": cash["subtraction"] - (deposits - positive),
    }
    start, end = parsed["period_start"], parsed["period_end"]
    for table in ("income", "fees", "trades", "deposits"):
        for row in rows[table]:
            if not start <= date.fromisoformat(_us_date(row["date"])) <= end:
                raise StatementGenerationError(
                    f"{statement_id}: a {table} row is dated outside the statement period"
                )
    half_cent = Decimal("0.005")
    for row in rows["trades"]:
        quantity, price, amount = (
            Decimal(row["quantity"]),
            Decimal(row["price"]),
            Decimal(row["amount"]),
        )
        buy = row["side"] == "buy"
        if (buy and (quantity <= 0 or amount >= 0)) or (
            not buy and (quantity >= 0 or amount <= 0)
        ):
            raise StatementGenerationError(
                f"{statement_id}: a trade row's side contradicts its quantity or amount"
            )
        if (
            abs(abs(quantity) * price - abs(amount))
            > abs(quantity) * half_cent + half_cent
        ):
            raise StatementGenerationError(
                f"{statement_id}: a trade row's quantity times price differs from its amount"
            )
    symbols = [row["symbol"] for row in rows["holdings"]]
    if len(set(symbols)) != len(symbols):
        raise StatementGenerationError(f"{statement_id}: a holding is listed twice")
    for row in rows["holdings"]:
        quantity, price, value = (
            Decimal(row["quantity"]),
            Decimal(row["price"].replace(",", "")),
            Decimal(row["market_value"]),
        )
        if abs(quantity * price - value) > abs(quantity) * half_cent + half_cent:
            raise StatementGenerationError(
                f"{statement_id}: a holding's quantity times price differs from its value"
            )
    for name, value in residuals.items():
        if value != _ZERO:
            raise StatementGenerationError(
                f"{statement_id}: statement {name} table differs from its summary "
                f"by {value}"
            )
    # each holding row is shown rounded to the cent: the sum may differ by half a cent a row
    rounding = position_sum - summary_holdings
    if abs(rounding) > Decimal("0.005") * max(len(rows["holdings"]), 1):
        raise StatementGenerationError(
            f"{statement_id}: holdings table differs from the account summary "
            f"by {rounding}"
        )
    return {
        **{name: _dec(value) for name, value in residuals.items()},
        "holdings_rounding_residual": _dec(rounding),
    }


def _census_account_start(pin: object, account_id: str, label: str) -> date | None:
    """The account's start in the pinned census (provenance) catalog, or None if it has none.

    The catalog must be the pinned bytes, carry the account as an INVESTMENT account whose
    alias names this provider, and the start is the earliest dated assignment of the account.
    """

    if not isinstance(pin, dict) or set(pin) != {"path", "sha256", "size_bytes"}:
        raise StatementGenerationError("census catalog pin has wrong keys")
    try:
        raw = Path(str(pin["path"])).read_bytes()
    except OSError as exc:
        raise StatementGenerationError("census catalog is unreadable") from exc
    if (
        hashlib.sha256(raw).hexdigest() != pin["sha256"]
        or len(raw) != pin["size_bytes"]
    ):
        raise StatementGenerationError("census catalog differs from its pin")
    try:
        catalog = json.loads(raw)
        accounts = catalog["accounts"]
        assignments = catalog["assignments"]
    except (ValueError, KeyError, TypeError) as exc:
        raise StatementGenerationError("census catalog shape differs") from exc
    matches = [item for item in accounts if item.get("account_id") == account_id]
    if len(matches) != 1:
        raise StatementGenerationError("internal account is not a census account")
    account = matches[0]
    provider = label.replace("_", " ")
    aliases = [str(alias).split("|")[0].strip().upper() for alias in account["aliases"]]
    if account.get("role") != "INVESTMENT" or provider not in aliases:
        raise StatementGenerationError("census account is not this provider's account")
    starts = [
        _date(item["effective_start"], "census assignment start")
        for item in assignments
        if item.get("account_id") == account_id and item.get("effective_start")
    ]
    return min(starts) if starts else None


def _us_date(text: str) -> str:
    try:
        return datetime.strptime(text, "%m/%d/%Y").date().isoformat()
    except ValueError as exc:
        raise StatementGenerationError(
            f"statement row date is invalid: {text!r}"
        ) from exc


def _unique_records(rows: Sequence[Mapping[str, object]], what: str) -> None:
    identifiers = [str(row["record_id"]) for row in rows]
    if len(set(identifiers)) != len(identifiers):
        raise StatementGenerationError(f"{what} record identifiers are not unique")


def _composer_security(
    provider: str, symbol: str, securities: dict[str, dict[str, object]]
) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", symbol.lower()).strip("-")
    security_id = f"security-{provider}-{slug}"
    securities[security_id] = {
        "currency": "USD",
        "provider_id": provider,
        "security_id": security_id,
        "symbol": symbol,
    }
    return security_id


def _window_rows(window: Mapping[str, object], key: str) -> list[Mapping[str, str]]:
    rows = window[key]
    if not isinstance(rows, list):
        raise StatementGenerationError(f"statement window lacks {key}")
    return rows


def _composer_activity(
    provider: str, account_id: str, windows: Sequence[Mapping[str, object]]
) -> dict[str, list[dict[str, object]]]:
    """Dated flows, transactions, positions and securities read from the statements."""

    flows: list[dict[str, object]] = []
    transactions: list[dict[str, object]] = []
    positions: list[dict[str, object]] = []
    securities: dict[str, dict[str, object]] = {}
    for window in windows:
        end = str(window["period_end"])
        statement_id = str(window["statement_id"])
        common = {
            "account_id": account_id,
            "source_artifact_id": window["source_artifact_id"],
            "statement_id": statement_id,
        }
        for index, row in enumerate(_window_rows(window, "deposit_rows"), start=1):
            day = _us_date(row["date"])
            flows.append(
                {
                    **common,
                    "amount": _amount(row["amount"]),
                    "event_date": day,
                    "flow_type": "DEPOSIT_OR_WITHDRAWAL",
                    "record_id": f"{provider}:flow:{day}:{index}",
                    "source_record_id": f"{statement_id}:deposit:{index}",
                    "timing": "EXACT_DATE",
                }
            )
        for index, row in enumerate(_window_rows(window, "income_rows"), start=1):
            day = _us_date(row["date"])
            transactions.append(
                {
                    **common,
                    "amount": _amount(row["amount"]),
                    "event_date": day,
                    "kind": f"CASH_ACTIVITY:{row['entry']}",
                    "record_id": f"{provider}:txn:income:{day}:{row['symbol']}:{index}",
                    "security_id": _composer_security(
                        provider, row["symbol"], securities
                    ),
                    "source_record_id": f"{statement_id}:income:{index}",
                }
            )
        for index, row in enumerate(_window_rows(window, "fee_rows"), start=1):
            day = _us_date(row["date"])
            transactions.append(
                {
                    **common,
                    "amount": _amount(row["amount"]),
                    "event_date": day,
                    "kind": "FEE:Fee",
                    "record_id": f"{provider}:txn:fee:{day}:{index}",
                    "source_record_id": f"{statement_id}:fee:{index}",
                }
            )
        for index, row in enumerate(_window_rows(window, "trade_rows"), start=1):
            day = _us_date(row["date"])
            transactions.append(
                {
                    **common,
                    "amount": _amount(row["amount"]),
                    "event_date": day,
                    "kind": f"TRADE:Entry:{row['side'].upper()}",
                    "price": row["price"],
                    "quantity": row["quantity"],
                    "record_id": f"{provider}:txn:trade:{day}:{row['symbol']}:{index}",
                    "security_id": _composer_security(
                        provider, row["symbol"], securities
                    ),
                    "source_record_id": f"{statement_id}:trade:{index}",
                }
            )
        for row in _window_rows(window, "holding_rows"):
            positions.append(
                {
                    **common,
                    "as_of_date": end,
                    "asset_category": "SECURITY",
                    "cost_basis": _amount(row["cost_basis"]),
                    "mark_price": row["price"],
                    "market_value": _amount(row["market_value"]),
                    "quantity": row["quantity"],
                    "record_id": f"{provider}:pos:{end}:{row['symbol']}",
                    "security_id": _composer_security(
                        provider, row["symbol"], securities
                    ),
                    "source_record_id": f"{statement_id}:holding:{row['symbol']}",
                }
            )
    for rows, what in (
        (flows, "flow"),
        (positions, "position"),
        (transactions, "transaction"),
    ):
        _unique_records(rows, what)
    return {
        "flows": flows,
        "positions": positions,
        "securities": [securities[key] for key in sorted(securities)],
        "transactions": transactions,
    }


_JH_FLOW_LABELS = {"Roth 401(k) Contribution": "CONTRIBUTION"}
_JH_TRANSACTION_LABELS = {
    "Administrative Fee": "FEE:AdministrativeFee",
    "Dividends/Interest": "CASH_ACTIVITY:DividendsInterest",
    "General Administrative Charge": "FEE:GeneralAdministrativeCharge",
}


def _amount(value: str) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


def _john_hancock_activity(
    provider: str, account_id: str, windows: Sequence[Mapping[str, object]]
) -> dict[str, list[dict[str, object]]]:
    """Flows, transactions, positions and securities read from the statement windows.

    Quarterly statements publish period aggregates, so every row is dated at the period
    end (``timing`` PERIOD_AGGREGATE).  Market change is a valuation item, not a
    transaction.  Holdings are fund-level: the plan statement shows the brokerage window
    as one bucket without security detail.
    """

    empty: dict[str, list[dict[str, object]]] = {
        "flows": [],
        "positions": [],
        "securities": [],
        "transactions": [],
    }
    if provider != "john_hancock":
        return empty
    flows: list[dict[str, object]] = []
    transactions: list[dict[str, object]] = []
    positions: list[dict[str, object]] = []
    securities: dict[str, dict[str, object]] = {}
    for window in windows:
        end = str(window["period_end"])
        common = {
            "account_id": account_id,
            "source_artifact_id": window["source_artifact_id"],
            "statement_id": window["statement_id"],
            "timing": "PERIOD_AGGREGATE",
        }
        for item in window["labelled_items"]:  # type: ignore[attr-defined]
            label, amount = str(item["label"]), str(item["amount"])
            if Decimal(amount) == _ZERO:
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
            if label in _JH_FLOW_LABELS:
                flows.append(
                    {
                        **common,
                        "amount": _amount(amount),
                        "event_date": end,
                        "flow_type": _JH_FLOW_LABELS[label],
                        "record_id": f"{provider}:flow:{end}:{slug}",
                        "source_record_id": f"{window['statement_id']}:{slug}",
                    }
                )
            elif label in _JH_TRANSACTION_LABELS:
                transactions.append(
                    {
                        **common,
                        "amount": _amount(amount),
                        "event_date": end,
                        "kind": _JH_TRANSACTION_LABELS[label],
                        "record_id": f"{provider}:txn:{end}:{slug}",
                        "source_record_id": f"{window['statement_id']}:{slug}",
                    }
                )
        for holding in window["holdings"]:  # type: ignore[attr-defined]
            name = str(holding["name"])
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            security_id = f"security-{provider.replace('_', '-')}-{slug}"
            securities[security_id] = {
                "currency": "USD",
                "provider_id": provider,
                "security_id": security_id,
                "symbol": name,
            }
            positions.append(
                {
                    **common,
                    "as_of_date": end,
                    "asset_category": (
                        "PLAN_BROKERAGE_BUCKET" if "Brokerage" in name else "FUND"
                    ),
                    "market_value": _amount(str(holding["market_value"])),
                    "record_id": f"{provider}:pos:{end}:{slug}",
                    "security_id": security_id,
                    "source_record_id": f"{window['statement_id']}:{slug}",
                }
            )
    for rows, what in (
        (flows, "flow"),
        (positions, "position"),
        (transactions, "transaction"),
    ):
        _unique_records(rows, what)
    return {
        "flows": flows,
        "positions": positions,
        "securities": [securities[key] for key in sorted(securities)],
        "transactions": transactions,
    }


def build_generation(
    spec_path: Path,
) -> tuple[dict[str, object], GenerationManifest, bytes]:
    spec = _load_spec(spec_path)
    provider = str(spec["provider"])
    meta = PROVIDERS[provider]
    generation_as_of = _date(spec["as_of_date"], "as_of_date")
    coverage_start = _date(spec["coverage_start"], "coverage_start")
    coverage_end = _date(spec["coverage_end"], "coverage_end")
    roots = {
        _text(key, "root id"): Path(_text(value, "root"))
        for key, value in dict(spec["roots"]).items()
    }
    crosswalk = list(spec["crosswalk"])
    if len(crosswalk) != 1 or set(crosswalk[0]) != {
        "external_account_id",
        "internal_account_id",
    }:
        raise StatementGenerationError("crosswalk must hold exactly one account")
    external = _text(crosswalk[0]["external_account_id"], "external_account_id")
    internal = _text(crosswalk[0]["internal_account_id"], "internal_account_id")
    if re.fullmatch(r"account-[0-9a-f]{16}", internal) is None:
        raise StatementGenerationError("internal account ID is invalid")
    census_start = _census_account_start(
        spec["census_catalog"], internal, str(meta["label"])
    )
    identity = _text(spec["identity"], "identity")

    artifacts: list[SourceArtifact] = []
    texts: dict[str, tuple[str, SourceArtifact]] = {}
    pdfs: dict[str, SourceArtifact] = {}
    seen: set[str] = set()
    for record in spec["sources"]:
        artifact, content = _read_source(record, roots, generation_as_of)
        if artifact.artifact_id in seen:
            raise StatementGenerationError("artifact IDs must be unique")
        seen.add(artifact.artifact_id)
        artifacts.append(artifact)
        statement_id = _text(record["statement_id"], "statement_id")
        if _PORTABLE_ID.fullmatch(statement_id) is None:
            raise StatementGenerationError("statement id is not portable")
        if record["role"] == "STATEMENT_PDF":
            if statement_id in pdfs:
                raise StatementGenerationError("two PDFs for one statement")
            pdfs[statement_id] = artifact
        else:
            if statement_id in texts:
                raise StatementGenerationError("two texts for one statement")
            texts[statement_id] = (content.decode("utf-8", errors="replace"), artifact)
    if set(texts) != set(pdfs):
        raise StatementGenerationError("every statement needs its text and its PDF")

    windows: list[dict[str, object]] = []
    for statement_id, (text, artifact) in texts.items():
        if provider == "john_hancock":
            parsed = parse_john_hancock(text)
            if identity not in text:
                raise StatementGenerationError("statement is not the declared plan")
            if parsed["closure_residual"] != _ZERO:
                raise StatementGenerationError(
                    f"{statement_id}: opening plus items differs from closing"
                )
            opening, closing = parsed["opening"], parsed["closing"]
            detail: dict[str, object] = {
                "items": [_dec(item) for item in parsed["items"]],
                "labelled_items": [
                    {"amount": _dec(amount), "label": label}
                    for label, amount in parsed["labelled_items"]
                ],
                "holdings": [
                    {"market_value": _dec(value), "name": name}
                    for name, value in parsed["holdings"]
                ],
                "closure_residual": _dec(parsed["closure_residual"]),
            }
        else:
            parsed = parse_composer(text)
            if parsed["account_number"] != external:
                raise StatementGenerationError(
                    f"{statement_id}: statement names another account"
                )
            if parsed["cash_residual"] != _ZERO or parsed["total_residual"] != _ZERO:
                raise StatementGenerationError(
                    f"{statement_id}: statement arithmetic does not close"
                )
            opening, closing = parsed["cash"]["beginning"], parsed["holdings"]["total"]
            rows = parsed["rows"]
            detail = {
                "cash_beginning": _dec(parsed["cash"]["beginning"]),
                "cash_ending": _dec(parsed["cash"]["ending"]),
                "cash_residual": _dec(parsed["cash_residual"]),
                "long_market_value": _dec(parsed["holdings"]["long"]),
                "total_residual": _dec(parsed["total_residual"]),
                "table_closure": _composer_table_closure(statement_id, parsed),
                "holding_rows": rows["holdings"],
                "income_rows": rows["income"],
                "fee_rows": rows["fees"],
                "trade_rows": rows["trades"],
                "deposit_rows": rows["deposits"],
            }
        start, end = parsed["period_start"], parsed["period_end"]
        if statement_id != _expected_statement_id(provider, start, end):
            raise StatementGenerationError(
                f"{statement_id}: id does not match the statement period"
            )
        windows.append(
            {
                "account_id": internal,
                "closing": _dec(closing),
                "opening": _dec(opening) if provider == "john_hancock" else None,
                "period_end": end.isoformat(),
                "period_start": start.isoformat(),
                "source_artifact_id": artifact.artifact_id,
                "source_pdf_artifact_id": pdfs[statement_id].artifact_id,
                "statement_id": statement_id,
                **detail,
            }
        )
    windows.sort(key=lambda item: str(item["period_start"]))
    if not windows:
        raise StatementGenerationError("no statements")
    if (
        windows[0]["period_start"] != coverage_start.isoformat()
        or windows[-1]["period_end"] != coverage_end.isoformat()
    ):
        raise StatementGenerationError("statements do not span the declared coverage")
    breaks: list[dict[str, str]] = []
    for prior, current in zip(windows, windows[1:], strict=False):
        expected_start = date.fromisoformat(str(prior["period_end"])) + timedelta(
            days=1
        )
        if str(current["period_start"]) != expected_start.isoformat():
            raise StatementGenerationError("statement windows are not contiguous")
        if provider == "john_hancock":
            gap = Decimal(str(current["opening"])) - Decimal(str(prior["closing"]))
            current["prior_closing_gap"] = _dec(gap)
            if gap != _ZERO:
                breaks.append(
                    {
                        "statement_id": str(current["statement_id"]),
                        "opening_minus_prior_closing": _dec(gap),
                    }
                )
        else:
            # Composer carries the cash identity across months: this month's beginning
            # cash must be last month's ending cash.
            begin = _composer_line(
                texts[str(current["statement_id"])][0], "Beginning Balance"
            )[0]
            if begin != Decimal(str(prior["cash_ending"])):
                raise StatementGenerationError("Composer cash does not chain")
    first = windows[0]
    opened_before = (
        Decimal(str(first["opening"])) != _ZERO
        if provider == "john_hancock"
        else Decimal(str(first["cash_beginning"])) != _ZERO
    )
    known_gaps: list[str] = []
    if opened_before or (census_start is not None and census_start < coverage_start):
        known_gaps.append(
            "statements before "
            f"{coverage_start.isoformat()} are not captured: "
            + (
                "the first captured statement opens with a non-zero balance"
                if opened_before
                else "the census records the account before the first statement"
            )
            + (
                f" (census account start {census_start.isoformat()})"
                if census_start is not None
                else ""
            )
            + "; earlier balances and flows, including the account's funding, "
            "are outside this generation"
        )
    baseline = dict(spec["baseline"])
    if baseline != {"statement_count": len(windows)}:
        raise StatementGenerationError("statement count differs from accepted baseline")

    nav_observations = [
        {
            "account_id": internal,
            "as_of_date": item["period_end"],
            "nav": {"currency": "USD", "status": "AVAILABLE", "value": item["closing"]},
            "origin": "STATEMENT_CLOSING_BALANCE",
            "record_id": f"{provider}:{item['period_end']}",
            "source_artifact_id": item["source_artifact_id"],
            "source_record_id": item["statement_id"],
            "statement_id": item["statement_id"],
            "value_basis": "ACCOUNT_VALUE",
        }
        for item in windows
    ]
    activity = (
        _john_hancock_activity(provider, internal, windows)
        if provider == "john_hancock"
        else _composer_activity(provider, internal, windows)
    )
    catalog: dict[str, object] = {
        **activity,
        "accounts": [
            {
                "account_id": internal,
                "base_currency": "USD",
                "external_account_id": external,
                "provider": meta["label"],
            }
        ],
        "as_of_date": generation_as_of.isoformat(),
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "continuity_breaks": breaks,
        "coverage": {
            "cadence": meta["cadence"],
            "known_gaps": known_gaps,
            "observed_end": coverage_end.isoformat(),
            "observed_start": coverage_start.isoformat(),
            "out_of_boundary": [
                "statement points only: no value is admitted between two period ends",
                "live-page balances are post-statement evidence and are not admitted",
                *(
                    [
                        "positions are fund-level: the self-directed brokerage bucket "
                        "(about 90% of the balance) has no security detail in the plan "
                        "statement",
                        "flows and transactions are period aggregates dated at the "
                        "period end; the statement gives no day within the quarter",
                    ]
                    if provider == "john_hancock"
                    else []
                ),
            ],
            "status": "PARTIAL" if known_gaps else "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        },
        "nav_observations": nav_observations,
        "reconciliation": {
            "statement_count": len(windows),
            "status": "PASS_EXACT",
            "windows_contiguous": True,
        },
        "source_artifacts": [item.to_dict() for item in artifacts],
        "source_provenance": {
            "acquisition": "MANUAL_STATEMENT_CAPTURE",
            "coverage_class": "STATEMENT_POINTS",
            "statement_count": len(windows),
        },
        "statement_windows": windows,
    }
    catalog_bytes = canonical_json_bytes(catalog)
    normalized = SourceArtifact(
        artifact_id=f"normalized/{meta['catalog']}",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        size_bytes=len(catalog_bytes),
        as_of_date=generation_as_of,
        schema_name=f"{provider}-statement-catalog",
        schema_version=str(CATALOG_SCHEMA_VERSION),
    )
    manifest = GenerationManifest.build(
        as_of_date=generation_as_of,
        adapter=AdapterIdentity(str(meta["adapter"]), ADAPTER_VERSION),
        coverage=CoverageClaim(
            status=(
                CoverageStatus.PARTIAL
                if known_gaps
                else CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
            ),
            boundary=(
                f"{len(windows)} {meta['cadence'].lower()}ly statements, "
                f"{coverage_start.isoformat()} through {coverage_end.isoformat()}"
            ),
            fact_types=(
                "nav_observations",
                "statement_windows",
                *sorted({key for key in activity if activity[key]} | {"flows"}),
            ),
            observed_start=coverage_start,
            observed_end=coverage_end,
            known_gaps=tuple(known_gaps),
        ),
        artifacts=(*artifacts, normalized),
        verification=VerificationStatus.VERIFIED,
    )
    return catalog, manifest, catalog_bytes


def latest_on_or_before(
    catalog: Mapping[str, Any], account_id: str, cutoff: date
) -> dict[str, Any] | None:
    """The newest statement point at or before the cutoff, or None before the first."""

    points = [
        item
        for item in catalog["nav_observations"]
        if item["account_id"] == account_id
        and date.fromisoformat(item["as_of_date"]) <= cutoff
    ]
    return max(points, key=lambda item: item["as_of_date"], default=None)


def stage_generation_candidate(spec_path: Path, output_root: Path) -> dict[str, object]:
    """Stage one immutable candidate without advancing CURRENT.json."""

    catalog, manifest, catalog_bytes = build_generation(spec_path)
    provider = str(_load_spec(spec_path)["provider"])
    manifest_path = stage_generation(output_root, manifest)
    catalog_path = manifest_path.parent / str(PROVIDERS[provider]["catalog"])
    if catalog_path.exists() and catalog_path.read_bytes() != catalog_bytes:
        raise StatementGenerationError("staged catalog conflicts with existing bytes")
    catalog_path.write_bytes(catalog_bytes)
    return {**_summary(catalog, manifest, catalog_bytes), "published": False}


def _summary(
    catalog: Mapping[str, Any], manifest: GenerationManifest, catalog_bytes: bytes
) -> dict[str, object]:
    return {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "coverage_status": manifest.coverage.status.value,
        "generation_id": manifest.generation_id,
        "nav_count": len(catalog["nav_observations"]),
        "statement_count": len(catalog["statement_windows"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="verify and build without writes")
    build.add_argument("spec", type=Path)
    stage = sub.add_parser("stage", help="create an immutable candidate")
    stage.add_argument("spec", type=Path)
    stage.add_argument("output_root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = _summary(*build_generation(args.spec))
        else:
            result = stage_generation_candidate(args.spec, args.output_root)
    except StatementGenerationError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
