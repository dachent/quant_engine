"""G1 consumer-contract rows, part a (#884): the ledger, the attended launch, the sleeve facts, the finality evidence.

Each row calls the CONSUMER's own function on the run's real inputs; none restates a rule (Fable RULING-D 3.5, rule 1).
A row that cannot read its input fails. The rows run after the prep chain (the ledger spec, the run observation and
the preparation spec exist) and before ``attended``; they write nothing outside a scratch directory under the cycle.

* ``g1:ledger-spec``      ``provider_ledger.build_catalog``: R3 refusals 1 (matrixport finality), 2 (quantbase finality
                          shape and label), 3 (quantbase provider header), 4 (strategy evidence generation).
* ``g1:attended-launch``  ``portfolio_pipeline_materialize.check_attended_launch``: refusal 7.
* ``g1:sleeve-facts``     ``portfolio_attended_preparation.build_sleeve_facts`` into a scratch directory: refusal 8.
* ``g1:finality-evidence`` ``portfolio_report.derived_value_index`` over the run observation: refusal 13.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import tempfile
from typing import Any

import release_driver as driver

Row = Callable[[driver.Run, Any], None]


def _path(run: driver.Run, key: str) -> Path:
    if key not in run.values:
        raise driver.Refusal(f"run config lacks {key!r}")
    return Path(run.values[key])


def row_ledger_spec(run: driver.Run, env: Any) -> None:
    """The ledger builder's own validator accepts the ledger spec (finality, provider headers, strategy evidence)."""

    import provider_ledger

    try:
        provider_ledger.build_catalog(_path(run, "ledger_spec"))
    except provider_ledger.ProviderLedgerError as exc:
        raise driver.Refusal(str(exc)) from exc


def row_attended_launch(run: driver.Run, env: Any) -> None:
    """The attended run's launch identity (repository, interpreter, workspace) is accepted by the attended entrypoint."""

    import portfolio_pipeline_materialize as materialize

    try:
        materialize.check_attended_launch(
            _path(run, "python").resolve(),
            _path(run, "repo_root").resolve(),
            _path(run, "workspace_root").resolve(),
        )
    except materialize.MaterializationError as exc:
        raise driver.Refusal(str(exc)) from exc


def row_sleeve_facts(run: driver.Run, env: Any) -> None:
    """The sleeve-facts builder admits the configured sleeve inputs (built into a scratch directory, then discarded)."""

    import portfolio_attended_preparation as preparation

    config = json.loads(_path(run, "preparation_spec").read_bytes())
    scratch = run.cycle / "preflight"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch, prefix="g1-sleeve-") as tmp:
        try:
            preparation.build_sleeve_facts(
                config.get("sleeve_facts"),
                ledger_generation_dir=_path(run, "ledger_generation"),
                destination=Path(tmp),
            )
        except preparation.AttendedPreparationError as exc:
            raise driver.Refusal(str(exc)) from exc


def row_finality_evidence(run: driver.Run, env: Any) -> None:
    """The report's own reader accepts the finality evidence the run observation pins (derived patches included)."""

    import portfolio_report

    observation = json.loads(_path(run, "run_observation").read_bytes())
    try:
        portfolio_report.derived_value_index({"source_provenance": observation})
    except portfolio_report.ReportValidationError as exc:
        raise driver.Refusal(str(exc)) from exc


G1A_ROWS: tuple[tuple[str, Row], ...] = (
    ("g1:ledger-spec", row_ledger_spec),
    ("g1:attended-launch", row_attended_launch),
    ("g1:sleeve-facts", row_sleeve_facts),
    ("g1:finality-evidence", row_finality_evidence),
)
