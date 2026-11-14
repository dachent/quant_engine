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
from quant_engine.core import canonical_json_bytes

SPEC_SCHEMA_VERSION = 1
CATALOG_SCHEMA_VERSION = 1
EXTERNAL_KEY = "external_account_id"
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
        "closure_residual": opening_value + sum(items, _ZERO) - closing_value,
    }


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
    record: dict[str, Any] = {
        "period_start": date(year, month, 1),
        "period_end": date(year, month, monthrange(year, month)[1]),
        "cash": cash,
        "holdings": holdings,
        "cash_residual": cash_residual,
        "total_residual": total_residual,
    }
    record["account_number"] = account.group(1)
    return record


# ---------------------------------------------------------------- spec


def _load_spec(path: Path) -> dict[str, Any]:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StatementGenerationError("spec is not valid UTF-8 JSON") from exc
    keys = {
        "as_of_date",
        "baseline",
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
            detail = {
                "cash_ending": _dec(parsed["cash"]["ending"]),
                "cash_residual": _dec(parsed["cash_residual"]),
                "long_market_value": _dec(parsed["holdings"]["long"]),
                "total_residual": _dec(parsed["total_residual"]),
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
    catalog: dict[str, object] = {
        "accounts": [
            {
                "account_id": internal,
                "base_currency": "USD",
                "provider": meta["label"],
                EXTERNAL_KEY: external,
            }
        ],
        "as_of_date": generation_as_of.isoformat(),
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "continuity_breaks": breaks,
        "coverage": {
            "cadence": meta["cadence"],
            "known_gaps": [],
            "observed_end": coverage_end.isoformat(),
            "observed_start": coverage_start.isoformat(),
            "out_of_boundary": [
                "statement points only: no value is admitted between two period ends",
                "live-page balances are post-statement evidence and are not admitted",
            ],
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
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
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                f"{len(windows)} {meta['cadence'].lower()}ly statements, "
                f"{coverage_start.isoformat()} through {coverage_end.isoformat()}"
            ),
            fact_types=("nav_observations", "statement_windows"),
            observed_start=coverage_start,
            observed_end=coverage_end,
            known_gaps=(),
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
