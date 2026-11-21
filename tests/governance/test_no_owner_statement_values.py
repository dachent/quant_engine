"""Fixtures that mimic an owner statement stay synthetic (F2 item 22).

Structural rules only: no list of the replaced figures is kept anywhere in the repository (a digest of a short
numeric token is brute-forceable, and this repository is public).  A rule names a SHAPE a real statement or
fill-day record has and a synthetic fixture does not:

In the three statement/fill-day fixtures and the fund-days tool: an account string shaped ``dddd-dddd`` (not a
year range; ``0000-`` is the synthetic form), a bare 8-digit identifier, a quantity or price with seven or more
decimal places (the precision of a real fill record), and a real-looking fill-day date.  The same shapes also occur
legitimately across the repository (hashes, ids, timestamps), so a repository-wide scan would be noise; the
repository-wide inventory is a one-off, in the history-scrub proposal.

Scrubbed files are scanned by suffix over the committed text of tests, tools, packages, apps, acceptance, docs,
contracts and ops (not ``ops/preserved-scripts``, byte-for-byte recovered evidence)."""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
SCANNED = (
    "tests",
    "tools",
    "packages",
    "apps",
    "acceptance",
    "docs",
    "contracts",
    "ops",
)
SKIPPED_PREFIXES = ("ops/preserved-scripts/",)
SUFFIXES = {
    ".py",
    ".json",
    ".md",
    ".toml",
    ".txt",
    ".csv",
    ".yml",
    ".yaml",
    ".js",
    ".mjs",
    ".html",
    ".ipynb",
}
RULES = {
    "account-shaped string": re.compile(
        r"(?<![\d-])(?!(?:19|20)\d\d-(?:19|20)\d\d|0000-)\d{4}-\d{4}(?![\d-])"
    ),
    "bare 8-digit identifier": re.compile(r"(?<![\d.])\d{8}(?![\d.])"),
    "seven or more decimal places": re.compile(r"(?<![\d.])\d+\.\d{7,}(?!\d)"),
}
# the fixtures that were scrubbed: every rule applies to them
SCRUBBED = frozenset(
    {
        "tests/core/test_schwab_statement_cash_summaries.py",
        "tests/core/test_zero_financing_ranges.py",
        "tests/core/test_quantbase_fund_days.py",
        "tools/quantbase_fund_days.py",
    }
)
FILL_DAY_FIXTURES = {
    "tests/core/test_quantbase_fund_days.py": re.compile(
        r"2025,\s*8,|2025-08-0\d|2025/08/0\d"
    ),
}


def _files() -> list[Path]:
    found: list[Path] = []
    for name in SCANNED:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            relative = path.relative_to(ROOT).as_posix()
            if (
                path.is_file()
                and path.suffix in SUFFIXES
                and not relative.startswith(SKIPPED_PREFIXES)
                and "node_modules" not in path.parts
                and ".venv" not in path.parts
                and path != Path(__file__).resolve()
            ):
                found.append(path)
    return found


def _hits(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    relative = path.relative_to(ROOT).as_posix()
    if relative not in SCRUBBED:
        return []
    rules = RULES.keys()
    found: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule in rules:
            if RULES[rule].search(line):
                found.append(f"{relative}:{number} {rule}")
        fill_day = FILL_DAY_FIXTURES.get(relative)
        if fill_day is not None and fill_day.search(line):
            found.append(f"{relative}:{number} real-looking fill-day date")
    return found


def test_no_statement_shaped_owner_value_is_in_a_committed_file() -> None:
    offenders = [hit for path in _files() for hit in _hits(path)]
    assert not offenders, (
        "statement-shaped values (shape only, never the value): "
        + "; ".join(offenders[:40])
    )


def test_each_rule_fires_on_a_synthetic_example_of_its_shape(tmp_path: Path) -> None:
    samples = {
        "account-shaped string": "acct 1234-5678 here",
        "bare 8-digit identifier": "id 00000000 here",
        "seven or more decimal places": "qty 1.2345678 here",
    }
    for rule, line in samples.items():
        assert RULES[rule].search(line), rule
    assert not RULES["account-shaped string"].search("range 2025-2026 here")
    assert not RULES["account-shaped string"].search("synthetic 0000-0620 here")
    assert not RULES["bare 8-digit identifier"].search("sha 12345678901234567890")
    assert not RULES["seven or more decimal places"].search("rate 1.2345 here")
