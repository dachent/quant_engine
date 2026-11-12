"""G1 rows for the report's attribution contract and the decomposition's factor freshness (#884).

Each row calls the consumer's own check on the file the run will read, so the rule cannot drift from the
consumer: ``portfolio_report.check_implementation_return_attribution`` (R3 refusals 10, 11: an effect that is
legitimately unavailable, and the shape the producer really writes) and
``portfolio_decomposition.factor_freshness_findings`` (R3 refusal 14: factors 49 days old against the build's own
10-day limit, found by QA 47 minutes in). PASS and FAIL only: a row whose input is not configured or not readable
is FAIL. The rows run after the prep chain has written the files; ``release_preflight`` lists them in ``ROW_TABLE``.

Run config keys: ``base_decomposition_spec`` and ``base_decomposition_source_root`` (the decomposition spec and the
root its sources are pinned under); optional ``implementation_policy_evidence`` (JSON file with the policy source
proof, needed only when the attribution file declares a policy-unavailable bridge).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import release_driver as driver


def _config_path(run: driver.Run, key: str) -> Path:
    if key not in run.values:
        raise driver.Refusal(f"run config lacks {key!r}")
    path = Path(run.values[key])
    if not path.exists():
        raise driver.Refusal(f"{key} {path} does not exist")
    return path


def row_attribution_contract(run: driver.Run, env: Any) -> None:
    """R3 refusals 10, 11: the report builder's attribution contract accepts the prep-stage file."""

    import portfolio_report as report

    evidence = None
    if "implementation_policy_evidence" in run.values:
        evidence = json.loads(
            _config_path(run, "implementation_policy_evidence").read_text(
                encoding="utf-8"
            )
        )
    path = Path(run.expand("{impl_return_attribution}"))
    if not path.is_file():
        raise driver.Refusal(f"attribution file {path} is not written yet")
    if "requested_as_of" not in run.values:
        raise driver.Refusal("run config lacks requested_as_of")
    try:
        report.check_implementation_return_attribution(
            path,
            portfolio_as_of=run.values["requested_as_of"],
            expected_policy_evidence=evidence,
        )
    except report.ReportValidationError as exc:
        raise driver.Refusal(f"attribution contract: {exc}") from exc


def row_factor_freshness(run: driver.Run, env: Any) -> None:
    """R3 refusal 14: every model's factors are fresh by the decomposition build's own limit."""

    import portfolio_decomposition as decomposition

    spec = _config_path(run, "base_decomposition_spec")
    root = _config_path(run, "base_decomposition_source_root")
    try:
        findings = decomposition.factor_freshness_findings(spec, root)
    except decomposition.DecompositionError as exc:
        raise driver.Refusal(f"decomposition: {exc}") from exc
    if findings:
        raise driver.Refusal(f"factors refused by the build: {findings[:4]}")


ROWS_G1B = (
    ("g1:attribution-contract", row_attribution_contract),
    ("g1:factor-freshness", row_factor_freshness),
)
