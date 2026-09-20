"""Every tool must be type-checked, or be excluded on the record.

`pyproject.toml` sets `strict = true`, but `.github/workflows/quality.yml` runs mypy over a
hand-maintained list of paths rather than a directory. A hand-maintained list drifts silently:
a new tool is simply absent from it, and nothing fails. That is how four of the five modules
carrying a previously-reported defect class came to be excluded from strict checking while the
repository advertised strict typing.

This test closes the list. A tool is either in the mypy invocation or in `UNCHECKED_TOOLS`
below with a reason, and adding a tool without doing either fails here rather than years later.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "quality.yml"
TOOLS = REPOSITORY_ROOT / "tools"

# Tools deliberately outside strict type-checking, each with the reason it is outside.
# A directory target was tried and rejected: mypy fails on module-path mapping for this
# layout without --explicit-package-bases, so the list stays explicit and this test guards it.
UNCHECKED_TOOLS: dict[str, str] = {
    # mypy refuses these two alongside tools/account_strategy_manifest.py: they import it as
    # a top-level module, so it is "found twice under different module names". Resolving that
    # needs --explicit-package-bases or a MYPYPATH change across the whole invocation.
    "build_account_manifest_generation.py": "module-path conflict with account_strategy_manifest",
    "verify_account_manifest_generation.py": "module-path conflict with account_strategy_manifest",
    # These carry real strict-mode errors. Counted, not waved through: 19 errors across the
    # five, dominated by untyped mappings indexed as if they were concrete.
    "schwab_transaction_capture.py": "7 strict errors",
    "portfolio_rollforward_cycle.py": "5 strict errors",
    "quantbase_account_interval_receipt.py": "4 strict errors",
    "portfolio_control_recompute.py": "2 strict errors",
    "rebind_provider_ledger_strategy_inputs.py": "1 strict error",
}


def _mypy_arguments() -> list[str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"run: uv run --frozen --no-sync mypy (?P<targets>[^\n]+)", text)
    assert match is not None, "the quality workflow no longer invokes mypy as expected"
    return match.group("targets").split()


def test_every_tool_is_type_checked_or_excluded_on_the_record() -> None:
    checked = {
        Path(argument).name
        for argument in _mypy_arguments()
        if argument.startswith("tools/")
    }
    present = {
        path.name for path in TOOLS.glob("*.py") if not path.name.startswith("_")
    }
    missing = sorted(present - checked - set(UNCHECKED_TOOLS))
    assert not missing, (
        "these tools are neither type-checked nor excluded on the record: "
        f"{missing}. Add them to the mypy invocation in .github/workflows/quality.yml, "
        "or to UNCHECKED_TOOLS here with the reason."
    )


def test_exclusions_name_tools_that_exist() -> None:
    absent = sorted(name for name in UNCHECKED_TOOLS if not (TOOLS / name).is_file())
    assert not absent, f"UNCHECKED_TOOLS names tools that do not exist: {absent}"


def test_mypy_targets_that_name_tools_all_exist() -> None:
    stale = sorted(
        argument
        for argument in _mypy_arguments()
        if argument.startswith("tools/") and not (REPOSITORY_ROOT / argument).is_file()
    )
    assert not stale, f"the mypy invocation names tools that no longer exist: {stale}"
