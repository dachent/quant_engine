"""John Hancock and Composer statement NAV generations: closure, coverage, cutoff rule."""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tools import statement_nav_generation as gen

ACCOUNT = "account-0123456789abcdef"
PLAN = "Fertility Specialists Network 401(k) Plan"


def _money(value: str) -> str:
    return f"${value}"


def _composer_text(
    year: int,
    month: int,
    *,
    begin: str,
    trade: str = "$ --",
    ending: str,
    long: str,
    total: str,
    account: str = "900000001",
) -> str:
    name = list(gen._MONTHS)[month - 1]
    return "\n".join(
        [
            f"Account No: {account}",
            f"Monthly Statement Period: {name} - {year}",
            f"Beginning Balance {_money(begin)} {_money(begin)}",
            "Addition $ -- $ --",
            "Subtraction $ -- $ --",
            f"Trade Transaction {trade} {trade}",
            "Cost and Fees $ -- $ --",
            f"Ending Value {_money(ending)} {_money(ending)}",
            f"Long {_money(long)}",
            "Short $ --",
            "Options $ --",
            "Fixed Income $ --",
            f"Total Market Value {_money(total)}",
            "Short Term",
            "Long Term",
        ]
    )


def _jh_text(
    start: date, end: date, *, opening: str, items: list[str], closing: str
) -> str:
    pad = " " * 160
    rows = [f"{pad}{_money(value)}" for value in [opening, *items, closing]]
    lines = [
        f"STATEMENT PERIOD: {start:%m/%d/%Y} - {end:%m/%d/%Y}   {PLAN}",
        f"Opening Balance   $ {opening}",
        f"Closing Balance   ${closing}",
        "For the period",
        *rows,
    ]
    return "\n".join(lines)


def _write(root: Path, name: str, content: str) -> dict[str, Any]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode()
    path.write_bytes(data)
    return {
        "relative_path": name,
        "expected_sha256": hashlib.sha256(data).hexdigest(),
        "expected_size_bytes": len(data),
    }


def _spec(
    tmp_path: Path,
    provider: str,
    statements: list[tuple[str, date, str]],
    *,
    external: str,
    coverage: tuple[str, str],
) -> Path:
    sources = []
    for statement_id, end, text in statements:
        for role, suffix, body in (
            ("STATEMENT_TEXT", "txt", text),
            ("STATEMENT_PDF", "pdf", f"pdf of {statement_id}"),
        ):
            name = f"{statement_id}.{suffix}"
            sources.append(
                {
                    "artifact_id": f"{provider}/{name}",
                    "as_of_date": end.isoformat(),
                    "role": role,
                    "root_id": "statements",
                    "statement_id": statement_id,
                    **_write(tmp_path / "statements", name, body),
                }
            )
    spec = {
        "as_of_date": "2026-11-06",
        "baseline": {"statement_count": len(statements)},
        "coverage_end": coverage[1],
        "coverage_start": coverage[0],
        "crosswalk": [
            {"external_account_id": external, "internal_account_id": ACCOUNT}
        ],
        "identity": PLAN,
        "provider": provider,
        "roots": {"statements": str(tmp_path / "statements")},
        "schema_version": 1,
        "sources": sources,
    }
    path = tmp_path / f"spec-{provider}.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def _composer(tmp_path: Path, **override: Any) -> Path:
    months = [
        ("composer-2026-01", 1, "2.86", "$ --", "2.80", "1,087.10", "1,089.90"),
        ("composer-2026-02", 2, "2.80", "$ --", "2.80", "1,006.48", "1,009.28"),
        ("composer-2026-03", 3, "2.80", "$ --", "2.80", "1,031.20", "1,034.00"),
    ]
    statements = []
    for statement_id, month, begin, trade, ending, long, total in months:
        # month 1 starts at 2.86 and ends 2.80, so it needs a -0.06 trade line to close
        trade_text = "-$0.06" if month == 1 else trade
        text = _composer_text(
            2026,
            month,
            begin=begin,
            trade=trade_text,
            ending=ending,
            long=long,
            total=total,
        )
        statements.append(
            (statement_id, date(2026, month, monthrange(2026, month)[1]), text)
        )
    for index, text in override.items():
        statements[int(index)] = (
            statements[int(index)][0],
            statements[int(index)][1],
            text,
        )
    return _spec(
        tmp_path,
        "composer",
        statements,
        external="900000001",
        coverage=("2026-01-01", "2026-03-31"),
    )


def _jh(
    tmp_path: Path, *, residual: bool = False, second_opening: str = "1,010.00"
) -> Path:
    quarters = [
        (
            "jh-2026q1",
            date(2026, 1, 1),
            date(2026, 3, 31),
            "1,000.00",
            ["30.00", "-20.00"],
            "1,010.00",
        ),
        (
            "jh-2026q2",
            date(2026, 4, 1),
            date(2026, 6, 30),
            second_opening,
            ["50.00"],
            f"{Decimal(second_opening.replace(',', '')) + (51 if residual else 50):,.2f}",
        ),
    ]
    statements = []
    for statement_id, start, end, opening, items, closing in quarters:
        statements.append(
            (
                statement_id,
                end,
                _jh_text(start, end, opening=opening, items=items, closing=closing),
            )
        )
    return _spec(
        tmp_path,
        "john_hancock",
        statements,
        external="FSN-401K-PLAN",
        coverage=("2026-01-01", "2026-06-30"),
    )


def test_composer_points_are_the_statement_totals_and_identity_closes(
    tmp_path: Path,
) -> None:
    catalog, manifest, _ = gen.build_generation(_composer(tmp_path))
    points = {p["as_of_date"]: p["nav"]["value"] for p in catalog["nav_observations"]}  # type: ignore[index, attr-defined]
    assert points == {
        "2026-01-31": "1089.90",
        "2026-02-28": "1009.28",
        "2026-03-31": "1034.00",
    }
    assert manifest.coverage.status.value == "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    assert catalog["coverage"]["known_gaps"] == []  # type: ignore[index]


def test_latest_on_or_before_is_the_cutoff_rule(tmp_path: Path) -> None:
    catalog, _, _ = gen.build_generation(_composer(tmp_path))
    pick = gen.latest_on_or_before
    assert pick(catalog, ACCOUNT, date(2026, 3, 30))["as_of_date"] == "2026-02-28"  # type: ignore[index]
    assert pick(catalog, ACCOUNT, date(2026, 3, 31))["as_of_date"] == "2026-03-31"  # type: ignore[index]
    assert pick(catalog, ACCOUNT, date(2026, 11, 6))["as_of_date"] == "2026-03-31"  # type: ignore[index]
    assert pick(catalog, ACCOUNT, date(2026, 1, 30)) is None
    assert pick(catalog, "account-ffffffffffffffff", date(2026, 3, 31)) is None


def test_composer_refuses_a_statement_whose_cash_does_not_close(tmp_path: Path) -> None:
    bad = _composer_text(
        2026, 2, begin="2.80", ending="2.99", long="1,006.48", total="1,009.47"
    )
    with pytest.raises(gen.StatementGenerationError, match="does not close"):
        gen.build_generation(_composer(tmp_path, **{"1": bad}))


def test_composer_refuses_total_that_is_not_long_plus_cash(tmp_path: Path) -> None:
    bad = _composer_text(
        2026, 2, begin="2.80", ending="2.80", long="1,006.48", total="1,009.00"
    )
    with pytest.raises(gen.StatementGenerationError, match="does not close"):
        gen.build_generation(_composer(tmp_path, **{"1": bad}))


def test_composer_refuses_cash_that_does_not_chain_between_months(
    tmp_path: Path,
) -> None:
    bad = _composer_text(
        2026,
        3,
        begin="2.70",
        trade="$0.10",
        ending="2.80",
        long="1,031.20",
        total="1,034.00",
    )
    with pytest.raises(gen.StatementGenerationError, match="does not chain"):
        gen.build_generation(_composer(tmp_path, **{"2": bad}))


def test_composer_refuses_another_account(tmp_path: Path) -> None:
    bad = _composer_text(
        2026,
        2,
        begin="2.80",
        ending="2.80",
        long="1,006.48",
        total="1,009.28",
        account="1",
    )
    with pytest.raises(gen.StatementGenerationError, match="another account"):
        gen.build_generation(_composer(tmp_path, **{"1": bad}))


def test_a_missing_month_is_a_refusal_not_an_interpolation(tmp_path: Path) -> None:
    spec_path = _composer(tmp_path)
    spec = json.loads(spec_path.read_text())
    drop = "composer-2026-02"
    spec["sources"] = [s for s in spec["sources"] if s["statement_id"] != drop]
    spec["baseline"] = {"statement_count": 2}
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(gen.StatementGenerationError, match="not contiguous"):
        gen.build_generation(spec_path)


def test_statements_must_span_the_declared_coverage(tmp_path: Path) -> None:
    spec_path = _composer(tmp_path)
    spec = json.loads(spec_path.read_text())
    spec["coverage_end"] = "2026-04-30"
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(
        gen.StatementGenerationError, match="span the declared coverage"
    ):
        gen.build_generation(spec_path)


def test_a_statement_without_its_pdf_is_refused(tmp_path: Path) -> None:
    spec_path = _composer(tmp_path)
    spec = json.loads(spec_path.read_text())
    spec["sources"] = [
        s
        for s in spec["sources"]
        if s["artifact_id"] != "composer/composer-2026-03.pdf"
    ]
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(gen.StatementGenerationError, match="text and its PDF"):
        gen.build_generation(spec_path)


def test_a_changed_source_is_refused_by_hash(tmp_path: Path) -> None:
    spec_path = _composer(tmp_path)
    (tmp_path / "statements" / "composer-2026-01.txt").write_text("tampered")
    with pytest.raises(gen.StatementGenerationError, match="source hash differs"):
        gen.build_generation(spec_path)


def test_john_hancock_points_and_carried_difference(tmp_path: Path) -> None:
    catalog, _, _ = gen.build_generation(_jh(tmp_path, second_opening="1,000.00"))
    points = {p["as_of_date"]: p["nav"]["value"] for p in catalog["nav_observations"]}  # type: ignore[index, attr-defined]
    assert points == {"2026-03-31": "1010.00", "2026-06-30": "1050.00"}
    # the second opening is 10.00 below the first closing: carried, never filled
    assert catalog["continuity_breaks"] == [  # type: ignore[comparison-overlap]
        {"statement_id": "jh-2026q2", "opening_minus_prior_closing": "-10.00"}
    ]
    assert catalog["coverage"]["known_gaps"] == []  # type: ignore[index]


def test_john_hancock_refuses_opening_plus_items_not_equal_closing(
    tmp_path: Path,
) -> None:
    with pytest.raises(gen.StatementGenerationError, match="differs from closing"):
        gen.build_generation(_jh(tmp_path, residual=True))


def test_john_hancock_refuses_a_period_that_is_not_one_calendar_quarter() -> None:
    with pytest.raises(gen.StatementGenerationError, match="calendar quarter"):
        gen._expected_statement_id("john_hancock", date(2026, 2, 1), date(2026, 4, 30))


def test_statement_id_must_match_the_statement_period(tmp_path: Path) -> None:
    spec_path = _jh(tmp_path)
    spec = json.loads(spec_path.read_text())
    for source in spec["sources"]:
        if source["statement_id"] == "jh-2026q2":
            source["statement_id"] = "jh-2026q3"
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(gen.StatementGenerationError, match="id does not match"):
        gen.build_generation(spec_path)


def test_generation_is_deterministic_and_stage_never_writes_current(
    tmp_path: Path,
) -> None:
    spec_path = _composer(tmp_path)
    first = gen.build_generation(spec_path)
    second = gen.build_generation(spec_path)
    assert first[1].generation_id == second[1].generation_id
    assert first[2] == second[2]
    out = tmp_path / "out"
    result = gen.stage_generation_candidate(spec_path, out)
    assert result["published"] is False
    assert result["nav_count"] == 3
    assert not list(out.rglob("CURRENT.json"))
    assert (
        out / "generations" / str(result["generation_id"]) / "composer-generation.json"
    ).is_file()
    assert Decimal(str(first[0]["nav_observations"][0]["nav"]["value"])) == Decimal(
        "1089.90"
    )  # type: ignore[index]
