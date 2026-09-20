"""Mechanically rebind one explicit current-epoch crosswalk to a baseline.

The crosswalk remains an owner-provided policy input.  This tool neither
selects strategies nor changes product dispositions: it only replaces fields
that the exact staged baseline proves have changed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

from quant_engine.core.contracts import canonical_json_bytes


_GENERATION_ID = re.compile(r"[0-9a-f]{64}")
_LAST_FOUR = re.compile(r"[0-9]{4}")
_CROSSWALK_KEYS = {
    "schema_version",
    "artifact_id",
    "as_of_date",
    "calculation_cutoff_date",
    "disposition_counts",
    "selected_baseline",
    "rows",
}
_ROW_KEYS = {
    "assignment_id",
    "account_id",
    "provider",
    "strategy_id",
    "effective_start",
    "effective_end",
    "account_label",
    "last_four",
    "product_epoch_disposition",
    "owner_reason",
}
_DISPOSITIONS = {
    "INCLUDED_PRODUCT_EPOCH",
    "EXCLUDED_DEPRECATED_HISTORY_ONLY",
    "EXCLUDED_IMMATERIAL",
}


class CurrentEpochCrosswalkRebindError(ValueError):
    """The policy input or named baseline cannot form an exact rebind."""


def _object_from_path(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentEpochCrosswalkRebindError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise CurrentEpochCrosswalkRebindError(f"{label} must be an object")
    return value


def _canonical_date(
    value: object, label: str, *, allow_none: bool = False
) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise CurrentEpochCrosswalkRebindError(f"{label} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise CurrentEpochCrosswalkRebindError(f"{label} is invalid") from exc
    if parsed.isoformat() != value:
        raise CurrentEpochCrosswalkRebindError(f"{label} is invalid")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CurrentEpochCrosswalkRebindError(f"{label} is invalid")
    return value


def _last_four(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _LAST_FOUR.fullmatch(value) is None:
        raise CurrentEpochCrosswalkRebindError(f"{label} is invalid")
    return value


def _epoch_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str | None]:
    return (
        _text(row.get("account_id"), "crosswalk account_id"),
        _text(row.get("provider"), "crosswalk provider"),
        _text(row.get("strategy_id"), "crosswalk strategy_id"),
        str(_canonical_date(row.get("effective_start"), "crosswalk effective_start")),
        _canonical_date(
            row.get("effective_end"), "crosswalk effective_end", allow_none=True
        ),
    )


def _validate_template(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if set(payload) != _CROSSWALK_KEYS:
        raise CurrentEpochCrosswalkRebindError("explicit crosswalk shape is invalid")
    if (
        payload.get("schema_version") != 1
        or payload.get("artifact_id") != "account-strategy-current-epoch-crosswalk"
    ):
        raise CurrentEpochCrosswalkRebindError("explicit crosswalk identity is invalid")
    _canonical_date(payload.get("as_of_date"), "crosswalk as_of_date")
    cutoff = _canonical_date(
        payload.get("calculation_cutoff_date"), "crosswalk calculation_cutoff_date"
    )
    assert cutoff is not None
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != 15:
        raise CurrentEpochCrosswalkRebindError(
            "explicit crosswalk must contain 15 rows"
        )
    validated: list[dict[str, Any]] = []
    keys: set[tuple[str, str, str, str, str | None]] = set()
    labels: dict[str, tuple[str, str | None]] = {}
    actual_counts: Counter[str] = Counter()
    for raw_row in rows:
        if not isinstance(raw_row, dict) or set(raw_row) != _ROW_KEYS:
            raise CurrentEpochCrosswalkRebindError(
                "explicit crosswalk row shape is invalid"
            )
        row = dict(raw_row)
        _text(row.get("assignment_id"), "crosswalk assignment_id")
        key = _epoch_key(row)
        if key in keys:
            raise CurrentEpochCrosswalkRebindError(
                "explicit crosswalk epochs are ambiguous"
            )
        keys.add(key)
        start = date.fromisoformat(key[3])
        end_text = key[4]
        if end_text is not None and date.fromisoformat(end_text) < start:
            raise CurrentEpochCrosswalkRebindError(
                "crosswalk effective interval is invalid"
            )
        label = _text(row.get("account_label"), "crosswalk account_label")
        last_four = _last_four(row.get("last_four"), "crosswalk last_four")
        account_id = key[0]
        if account_id in labels and labels[account_id] != (label, last_four):
            raise CurrentEpochCrosswalkRebindError(
                "crosswalk account identity is inconsistent"
            )
        labels[account_id] = (label, last_four)
        disposition = row.get("product_epoch_disposition")
        if disposition not in _DISPOSITIONS:
            raise CurrentEpochCrosswalkRebindError("crosswalk disposition is invalid")
        _text(row.get("owner_reason"), "crosswalk owner_reason")
        actual_counts[str(disposition)] += 1
        validated.append(row)
    expected_counts = {
        "INCLUDED_PRODUCT_EPOCH": 13,
        "EXCLUDED_DEPRECATED_HISTORY_ONLY": 1,
        "EXCLUDED_IMMATERIAL": 1,
    }
    if (
        payload.get("disposition_counts") != expected_counts
        or dict(actual_counts) != expected_counts
    ):
        raise CurrentEpochCrosswalkRebindError(
            "crosswalk disposition counts are invalid"
        )
    if date.fromisoformat(cutoff) > date.fromisoformat(str(payload["as_of_date"])):
        raise CurrentEpochCrosswalkRebindError(
            "crosswalk calculation cutoff is invalid"
        )
    return validated


def _baseline_binding(
    baseline_generation_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    generation_id = baseline_generation_dir.name
    if _GENERATION_ID.fullmatch(generation_id) is None:
        raise CurrentEpochCrosswalkRebindError(
            "baseline generation directory is invalid"
        )
    manifest = _object_from_path(
        baseline_generation_dir / "generation.json", "baseline generation manifest"
    )
    if manifest.get("generation_id") != generation_id:
        raise CurrentEpochCrosswalkRebindError("baseline generation identity differs")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise CurrentEpochCrosswalkRebindError(
            "baseline generation manifest artifacts are invalid"
        )
    matches = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict)
        and artifact.get("artifact_id") == "normalized/portfolio-baseline.json"
    ]
    if len(matches) != 1:
        raise CurrentEpochCrosswalkRebindError(
            "baseline generation lacks one catalog artifact"
        )
    artifact = matches[0]
    schema = artifact.get("schema")
    if not isinstance(schema, dict) or schema != {
        "name": "portfolio-baseline-catalog",
        "version": "1",
    }:
        raise CurrentEpochCrosswalkRebindError("baseline catalog schema differs")
    baseline_path = baseline_generation_dir / "portfolio-baseline.json"
    try:
        baseline_bytes = baseline_path.read_bytes()
    except OSError as exc:
        raise CurrentEpochCrosswalkRebindError(
            "baseline catalog is unreadable"
        ) from exc
    digest = hashlib.sha256(baseline_bytes).hexdigest()
    if artifact.get("sha256") != digest or artifact.get("size_bytes") != len(
        baseline_bytes
    ):
        raise CurrentEpochCrosswalkRebindError("baseline catalog identity differs")
    try:
        baseline = json.loads(baseline_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentEpochCrosswalkRebindError(
            "baseline catalog is unreadable"
        ) from exc
    if not isinstance(baseline, dict):
        raise CurrentEpochCrosswalkRebindError("baseline catalog must be an object")
    as_of_date = _canonical_date(baseline.get("as_of_date"), "baseline as_of_date")
    if baseline.get("baseline_schema_version") != 1:
        raise CurrentEpochCrosswalkRebindError("baseline catalog schema differs")
    assert as_of_date is not None
    binding = {
        "generation_id": generation_id,
        "artifact_id": "normalized/portfolio-baseline.json",
        "sha256": digest,
        "size_bytes": len(baseline_bytes),
        "as_of_date": as_of_date,
        "schema_name": "portfolio-baseline-catalog",
        "schema_version": "1",
    }
    return baseline, binding


def _baseline_mapped_rows(
    baseline: Mapping[str, Any],
) -> tuple[
    dict[tuple[str, str, str, str, str | None], dict[str, Any]],
    dict[str, tuple[str, str | None]],
]:
    accounts = baseline.get("accounts")
    assignments = baseline.get("assignments")
    if not isinstance(accounts, list) or not isinstance(assignments, list):
        raise CurrentEpochCrosswalkRebindError("baseline account census is invalid")
    account_displays: dict[str, tuple[str, str | None]] = {}
    account_providers: dict[str, str] = {}
    for raw_account in accounts:
        if not isinstance(raw_account, dict):
            raise CurrentEpochCrosswalkRebindError("baseline account census is invalid")
        account_id = _text(raw_account.get("account_id"), "baseline account_id")
        provider = _text(raw_account.get("provider"), "baseline provider")
        if (
            account_id in account_providers
            and account_providers[account_id] != provider
        ):
            raise CurrentEpochCrosswalkRebindError(
                "baseline account provider is ambiguous"
            )
        account_providers[account_id] = provider
        label_present = "account_label" in raw_account
        last_four_present = "last_four" in raw_account
        if label_present != last_four_present:
            raise CurrentEpochCrosswalkRebindError(
                "baseline account display authority is incomplete"
            )
        if label_present:
            display = (
                _text(raw_account.get("account_label"), "baseline account_label"),
                _last_four(raw_account.get("last_four"), "baseline last_four"),
            )
            if (
                account_id in account_displays
                and account_displays[account_id] != display
            ):
                raise CurrentEpochCrosswalkRebindError(
                    "baseline account display is ambiguous"
                )
            account_displays[account_id] = display
    mapped: dict[tuple[str, str, str, str, str | None], dict[str, Any]] = {}
    for raw_assignment in assignments:
        if (
            not isinstance(raw_assignment, dict)
            or raw_assignment.get("strategy_mapping_disposition") != "STRATEGY_MAPPED"
        ):
            continue
        account_id = _text(
            raw_assignment.get("account_id"), "baseline assignment account_id"
        )
        provider = account_providers.get(account_id)
        if provider is None:
            raise CurrentEpochCrosswalkRebindError(
                "baseline assignment account is missing"
            )
        row = {
            "assignment_id": _text(
                raw_assignment.get("assignment_id"), "baseline assignment_id"
            ),
            "account_id": account_id,
            "provider": provider,
            "strategy_id": _text(
                raw_assignment.get("strategy_id"), "baseline strategy_id"
            ),
            "effective_start": _canonical_date(
                raw_assignment.get("effective_start"), "baseline effective_start"
            ),
            "effective_end": _canonical_date(
                raw_assignment.get("effective_end"),
                "baseline effective_end",
                allow_none=True,
            ),
        }
        key = _epoch_key(row)
        if key in mapped:
            raise CurrentEpochCrosswalkRebindError(
                "baseline mapped epochs are ambiguous"
            )
        mapped[key] = row
    if len(mapped) != 15:
        raise CurrentEpochCrosswalkRebindError("baseline mapped epoch count differs")
    return mapped, account_displays


def _candidate_payload(
    template: dict[str, Any],
    baseline: dict[str, Any],
    binding: dict[str, Any],
    calculation_cutoff_date: str | None,
) -> dict[str, Any]:
    template_rows = _validate_template(template)
    mapped, account_displays = _baseline_mapped_rows(baseline)
    template_keys = {_epoch_key(row) for row in template_rows}
    if set(mapped) != template_keys:
        raise CurrentEpochCrosswalkRebindError(
            "baseline mapping differs from explicit crosswalk"
        )
    rebound_rows: list[dict[str, Any]] = []
    for template_row in template_rows:
        row = dict(template_row)
        baseline_row = mapped[_epoch_key(row)]
        row["assignment_id"] = baseline_row["assignment_id"]
        display = account_displays.get(str(row["account_id"]))
        if display is not None:
            row["account_label"], row["last_four"] = display
        rebound_rows.append(row)
    candidate = dict(template)
    candidate["as_of_date"] = binding["as_of_date"]
    candidate["selected_baseline"] = binding
    candidate["rows"] = rebound_rows
    cutoff = _canonical_date(
        calculation_cutoff_date
        if calculation_cutoff_date is not None
        else candidate.get("calculation_cutoff_date"),
        "crosswalk calculation_cutoff_date",
    )
    assert cutoff is not None
    if date.fromisoformat(cutoff) > date.fromisoformat(str(binding["as_of_date"])):
        raise CurrentEpochCrosswalkRebindError(
            "crosswalk calculation cutoff is invalid"
        )
    candidate["calculation_cutoff_date"] = cutoff
    return candidate


def _write_new(path: Path, payload: Mapping[str, Any]) -> None:
    contents = canonical_json_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(contents)
    except FileExistsError as exc:
        raise CurrentEpochCrosswalkRebindError(
            "candidate crosswalk already exists"
        ) from exc


def rebind_current_epoch_crosswalk(
    *,
    template_path: Path,
    baseline_generation_dir: Path,
    output_path: Path,
    calculation_cutoff_date: str | None = None,
) -> dict[str, object]:
    """Produce a new bound candidate without changing policy or topology choices."""

    template = _object_from_path(template_path, "explicit crosswalk")
    baseline, binding = _baseline_binding(baseline_generation_dir)
    candidate = _candidate_payload(
        template, baseline, binding, calculation_cutoff_date
    )
    _write_new(output_path, candidate)
    return {
        "baseline_generation_id": binding["generation_id"],
        "output_path": str(output_path),
        "row_count": len(candidate["rows"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--baseline-generation-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--calculation-cutoff-date")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = rebind_current_epoch_crosswalk(
            template_path=args.template,
            baseline_generation_dir=args.baseline_generation_dir,
            output_path=args.output,
            calculation_cutoff_date=args.calculation_cutoff_date,
        )
    except (CurrentEpochCrosswalkRebindError, OSError, ValueError) as exc:
        print(f"current-epoch-crosswalk-rebind: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
