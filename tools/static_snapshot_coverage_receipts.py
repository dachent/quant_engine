"""Issue exact static-snapshot coverage receipts for current product epochs.

This deliberately records coverage of a retained, hash-bound provider ledger.
It does not assert API pagination or live-provider exhaustiveness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


_MODE = "STATIC_SNAPSHOT_COVERAGE_V1"
_CONVENTION = "EOD_PROVIDER_NAV_RECOGNITION_DATE"
_LEDGER_ARTIFACT_ID = "normalized/provider-ledger.json"
_AUTHORITY_ARTIFACT_ID = "authority/account-interval-retrieval-receipt-authority.json"
_SHA256_LENGTH = 64


class StaticSnapshotReceiptError(ValueError):
    """Raised when a static-snapshot receipt cannot be proven from its inputs."""


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _read_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        contents = path.read_bytes()
        payload = json.loads(contents)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StaticSnapshotReceiptError(f"cannot read {label}") from exc
    if not isinstance(payload, dict) or _canonical_bytes(payload) != contents:
        raise StaticSnapshotReceiptError(f"{label} must use canonical JSON bytes")
    return payload, contents


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _date(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 10:
        raise StaticSnapshotReceiptError(f"{label} must be a canonical ISO date")
    try:
        from datetime import date

        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise StaticSnapshotReceiptError(
            f"{label} must be a canonical ISO date"
        ) from exc
    if parsed.isoformat() != value:
        raise StaticSnapshotReceiptError(f"{label} must be a canonical ISO date")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise StaticSnapshotReceiptError(f"{label} must be a non-empty string")
    return value


def _artifact(
    payload: bytes, *, artifact_id: str, as_of_date: str, name: str, version: str
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "as_of_date": as_of_date,
        "schema": {"name": name, "version": version},
        "sha256": _sha256(payload),
        "size_bytes": len(payload),
    }


def _ledger_generation(
    ledger: Mapping[str, Any], manifest: Mapping[str, Any], ledger_bytes: bytes
) -> tuple[str, dict[str, Any]]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise StaticSnapshotReceiptError(
            "provider ledger generation artifacts are invalid"
        )
    matches = [
        item
        for item in artifacts
        if isinstance(item, Mapping) and item.get("artifact_id") == _LEDGER_ARTIFACT_ID
    ]
    if len(matches) != 1:
        raise StaticSnapshotReceiptError(
            "provider ledger generation does not select provider ledger"
        )
    selected = dict(matches[0])
    if selected.get("sha256") != _sha256(ledger_bytes) or selected.get(
        "size_bytes"
    ) != len(ledger_bytes):
        raise StaticSnapshotReceiptError(
            "provider ledger differs from staged generation"
        )
    ledger_as_of = _date(ledger.get("as_of_date"), "provider ledger.as_of_date")
    if selected.get("as_of_date") != ledger_as_of:
        raise StaticSnapshotReceiptError("provider ledger generation date differs")
    generation_id = _text(
        manifest.get("generation_id"), "provider ledger generation_id"
    )
    if len(generation_id) != _SHA256_LENGTH:
        raise StaticSnapshotReceiptError("provider ledger generation_id is invalid")
    return generation_id, selected


def _included_assignments(
    baseline: Mapping[str, Any], crosswalk: Mapping[str, Any], cutoff: str
) -> list[dict[str, str | None]]:
    accounts = baseline.get("accounts")
    assignments = baseline.get("assignments")
    rows = crosswalk.get("rows")
    if (
        not isinstance(accounts, list)
        or not isinstance(assignments, list)
        or not isinstance(rows, list)
    ):
        raise StaticSnapshotReceiptError("baseline/crosswalk rows are invalid")
    providers = {
        row.get("account_id"): row.get("provider")
        for row in accounts
        if isinstance(row, Mapping) and isinstance(row.get("account_id"), str)
    }
    by_assignment = {
        row.get("assignment_id"): row
        for row in assignments
        if isinstance(row, Mapping)
        and row.get("strategy_mapping_disposition") == "STRATEGY_MAPPED"
    }
    result: list[dict[str, str | None]] = []
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or row.get("product_epoch_disposition") != "INCLUDED_PRODUCT_EPOCH"
        ):
            continue
        assignment = by_assignment.get(row.get("assignment_id"))
        if not isinstance(assignment, Mapping):
            raise StaticSnapshotReceiptError(
                "crosswalk assignment is not mapped by baseline"
            )
        fields = ("account_id", "strategy_id", "effective_start", "effective_end")
        if any(row.get(field) != assignment.get(field) for field in fields):
            raise StaticSnapshotReceiptError(
                "crosswalk assignment differs from baseline"
            )
        account_id = _text(assignment.get("account_id"), "assignment.account_id")
        provider = _text(providers.get(account_id), "account.provider").upper()
        if row.get("provider") != provider:
            raise StaticSnapshotReceiptError("crosswalk provider differs from baseline")
        start = _date(assignment.get("effective_start"), "assignment.effective_start")
        end = assignment.get("effective_end")
        if end is not None:
            end = _date(end, "assignment.effective_end")
        requested_end = end or cutoff
        if requested_end > cutoff:
            raise StaticSnapshotReceiptError(
                "assignment interval exceeds calculation cutoff"
            )
        result.append(
            {
                "assignment_id": _text(
                    assignment.get("assignment_id"), "assignment.assignment_id"
                ),
                "account_id": account_id,
                "strategy_id": _text(
                    assignment.get("strategy_id"), "assignment.strategy_id"
                ),
                "provider": provider,
                "requested_start": start,
                "requested_end": requested_end,
            }
        )
    if not result:
        raise StaticSnapshotReceiptError(
            "crosswalk contains no included product epochs"
        )
    if len({item["assignment_id"] for item in result}) != len(result):
        raise StaticSnapshotReceiptError(
            "crosswalk contains duplicate included assignment"
        )
    return sorted(result, key=lambda item: str(item["assignment_id"]))


def _fact_date(row: Mapping[str, Any], kind: str) -> str:
    if kind == "nav":
        return _date(row.get("as_of_date"), "NAV as_of_date")
    # The ledger retains the provider event date as source evidence, but EOD
    # calculation inclusion follows the provider NAV-recognition date.  Older
    # normalized flow rows without an explicit recognition field mean same-day
    # recognition and retain their original event date unchanged.
    return _date(
        row.get("nav_recognition_date", row.get("event_date")),
        f"{kind} NAV recognition date",
    )


def _fact_summary(rows: list[Mapping[str, Any]], kind: str) -> dict[str, object]:
    dates = sorted(_fact_date(row, kind) for row in rows)
    return {
        "row_count": len(rows),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
    }


def _missing_intervals(
    ledger: Mapping[str, Any], provider: str, start: str, end: str
) -> list[dict[str, str]]:
    provider_rows = ledger.get("provider_coverage")
    if not isinstance(provider_rows, list):
        return []
    result: list[dict[str, str]] = []
    for row in provider_rows:
        if (
            not isinstance(row, Mapping)
            or str(row.get("provider_id", "")).upper() != provider
        ):
            continue
        coverage = row.get("coverage")
        if not isinstance(coverage, Mapping):
            continue
        for item in coverage.get("declared_exclusions", []):
            if not isinstance(item, Mapping) or not isinstance(item.get("item"), str):
                continue
            prefix = "nav_missing_date:"
            if not item["item"].startswith(prefix):
                continue
            missing = _date(
                item["item"][len(prefix) :], "provider declared missing NAV date"
            )
            if start <= missing <= end:
                result.append(
                    {
                        "start": missing,
                        "end": missing,
                        "reason": "PROVIDER_DECLARED_NAV_MISSING",
                    }
                )
    return sorted(result, key=lambda item: item["start"])


def _receipt_id(item: Mapping[str, str | None]) -> str:
    return "receipt/static-snapshot/{account_id}/{strategy_id}/{requested_start}-{requested_end}.json".format(
        **item
    )


def _receipt_for_assignment(
    item: Mapping[str, str | None],
    *,
    ledger: Mapping[str, Any],
    ledger_generation_id: str,
    ledger_artifact: Mapping[str, Any],
    baseline_bytes: bytes,
    crosswalk_bytes: bytes,
    code_bytes: bytes,
    cutoff: str,
) -> dict[str, object]:
    start = str(item["requested_start"])
    end = str(item["requested_end"])
    account_id = str(item["account_id"])
    strategy_id = str(item["strategy_id"])
    nav_rows = [
        row
        for row in ledger.get("nav_observations", [])
        if isinstance(row, Mapping)
        and row.get("account_id") == account_id
        and start <= _fact_date(row, "nav") <= end
    ]
    flow_rows = [
        row
        for row in ledger.get("flows", [])
        if isinstance(row, Mapping)
        and row.get("account_id") == account_id
        # Explicitly unassigned wallet activity remains hash-bound source
        # evidence but is not an observed flow of this funded strategy epoch.
        and not (
            row.get("provider_account_scope") == "ACCOUNT_ONLY"
            and row.get("mapping_status") == "UNMAPPED"
            and "strategy_id" in row
            and row["strategy_id"] is None
        )
        and start <= _fact_date(row, "flows") <= end
    ]
    anchor_rows = [
        row
        for row in ledger.get("capital_anchors", [])
        if isinstance(row, Mapping)
        and row.get("account_id") == account_id
        and row.get("strategy_id") == strategy_id
        and start <= _fact_date(row, "capital_anchors") <= end
    ]
    provider_generations = ledger.get("provider_generations")
    if not isinstance(provider_generations, list):
        raise StaticSnapshotReceiptError(
            "provider ledger provider_generations are invalid"
        )
    matches = [
        row
        for row in provider_generations
        if isinstance(row, Mapping)
        and str(row.get("provider_id", "")).upper() == item["provider"]
    ]
    if len(matches) != 1:
        raise StaticSnapshotReceiptError("provider generation is not uniquely selected")
    provider_generation = matches[0]
    for field in (
        "generation_id",
        "catalog_sha256",
        "manifest_sha256",
        "generation_as_of_date",
    ):
        if field not in provider_generation:
            raise StaticSnapshotReceiptError("provider generation is invalid")
    ledger_as_of = _date(ledger.get("as_of_date"), "provider ledger.as_of_date")
    return {
        "schema_version": 3,
        "artifact_id": _receipt_id(item),
        "coverage_mode": _MODE,
        "issuer": {
            "adapter_name": "static-snapshot-coverage",
            "adapter_version": "1",
            "code_sha256": _sha256(code_bytes),
        },
        "provider": item["provider"],
        "account_id": account_id,
        "strategy_id": strategy_id,
        "requested_start": start,
        "requested_end": end,
        "calculation_cutoff_date": cutoff,
        "date_convention": _CONVENTION,
        "provider_generation": {
            "generation_id": provider_generation["generation_id"],
            "catalog_sha256": provider_generation["catalog_sha256"],
            "manifest_sha256": provider_generation["manifest_sha256"],
            "generation_as_of_date": provider_generation["generation_as_of_date"],
        },
        "staged_inputs": {
            "provider_ledger_generation_id": ledger_generation_id,
            "provider_ledger_artifact": dict(ledger_artifact),
            "baseline_sha256": _sha256(baseline_bytes),
            "baseline_size_bytes": len(baseline_bytes),
            "crosswalk_sha256": _sha256(crosswalk_bytes),
            "crosswalk_size_bytes": len(crosswalk_bytes),
        },
        "observed_facts": {
            "nav": _fact_summary(nav_rows, "nav"),
            "flows": _fact_summary(flow_rows, "flows"),
            "capital_anchors": _fact_summary(anchor_rows, "capital_anchors"),
        },
        "missing_intervals": _missing_intervals(
            ledger, str(item["provider"]), start, end
        ),
        "coverage_as_of_date": ledger_as_of,
        "completeness": "RETAINED_STATIC_SNAPSHOT_ONLY",
    }


def issue_static_snapshot_receipts(
    *,
    ledger_path: Path,
    provider_ledger_manifest_path: Path,
    baseline_path: Path,
    crosswalk_path: Path,
    issuer_code_path: Path,
    parent_window_starts: Mapping[str, str] | None = None,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    """Return one canonical authority and all included-epoch static receipts.

    ``parent_window_starts`` maps a sleeve parent account to the first day of its
    earliest governed sleeve.  When that day precedes the parent's own earliest included
    epoch, one more receipt covers the earlier window through the parent's last
    included epoch: the flow-completeness evidence those sleeves cite.
    """
    ledger, ledger_bytes = _read_object(ledger_path, "provider ledger")
    manifest, _manifest_bytes = _read_object(
        provider_ledger_manifest_path, "provider ledger generation"
    )
    baseline, baseline_bytes = _read_object(baseline_path, "baseline")
    crosswalk, crosswalk_bytes = _read_object(crosswalk_path, "crosswalk")
    try:
        code_bytes = issuer_code_path.read_bytes()
    except OSError as exc:
        raise StaticSnapshotReceiptError("cannot read receipt issuer source") from exc
    ledger_generation_id, ledger_artifact = _ledger_generation(
        ledger, manifest, ledger_bytes
    )
    cutoff = _date(
        crosswalk.get("calculation_cutoff_date"), "crosswalk calculation_cutoff_date"
    )
    if cutoff > _date(ledger.get("as_of_date"), "provider ledger.as_of_date"):
        raise StaticSnapshotReceiptError("calculation cutoff exceeds provider ledger")
    assignments = _included_assignments(baseline, crosswalk, cutoff)
    for account_id, window_start in sorted((parent_window_starts or {}).items()):
        own = sorted(
            (item for item in assignments if item["account_id"] == account_id),
            key=lambda item: str(item["requested_start"]),
        )
        if not own:
            continue
        window_start = _date(window_start, "parent window start")
        if window_start >= str(own[0]["requested_start"]):
            continue
        assignments.append(
            {
                **own[0],
                "requested_start": window_start,
                "requested_end": max(str(item["requested_end"]) for item in own),
            }
        )
    receipts = {
        _receipt_id(item): _receipt_for_assignment(
            item,
            ledger=ledger,
            ledger_generation_id=ledger_generation_id,
            ledger_artifact=ledger_artifact,
            baseline_bytes=baseline_bytes,
            crosswalk_bytes=crosswalk_bytes,
            code_bytes=code_bytes,
            cutoff=cutoff,
        )
        for item in assignments
    }
    authority = {
        "schema_version": 1,
        "artifact_id": _AUTHORITY_ARTIFACT_ID,
        "as_of_date": cutoff,
        "receipts": [
            {
                "artifact_id": receipt_id,
                "sha256": _sha256(_canonical_bytes(receipt)),
                "size_bytes": len(_canonical_bytes(receipt)),
                "contract": {
                    "coverage_mode": _MODE,
                    "account_id": receipt["account_id"],
                    "strategy_id": receipt["strategy_id"],
                    "requested_start": receipt["requested_start"],
                    "requested_end": receipt["requested_end"],
                    "provider_generation": receipt["provider_generation"],
                    "staged_inputs": receipt["staged_inputs"],
                },
            }
            for receipt_id, receipt in sorted(
                receipts.items(), key=lambda item: item[0].casefold()
            )
        ],
    }
    return authority, receipts


def verify_static_snapshot_receipt(
    receipt: Mapping[str, object],
    *,
    ledger_path: Path,
    provider_ledger_manifest_path: Path,
    baseline_path: Path,
    crosswalk_path: Path,
) -> None:
    """Recompute receipt facts from independently selected staged snapshots."""
    ledger, ledger_bytes = _read_object(ledger_path, "provider ledger")
    manifest, _manifest_bytes = _read_object(
        provider_ledger_manifest_path, "provider ledger generation"
    )
    baseline, baseline_bytes = _read_object(baseline_path, "baseline")
    crosswalk, crosswalk_bytes = _read_object(crosswalk_path, "crosswalk")
    ledger_generation_id, ledger_artifact = _ledger_generation(
        ledger, manifest, ledger_bytes
    )
    cutoff = _date(
        crosswalk.get("calculation_cutoff_date"), "crosswalk calculation_cutoff_date"
    )
    assignments = _included_assignments(baseline, crosswalk, cutoff)
    matching = [
        item for item in assignments if _receipt_id(item) == receipt.get("artifact_id")
    ]
    if len(matching) != 1:
        raise StaticSnapshotReceiptError("receipt does not route to included epoch")
    expected = _receipt_for_assignment(
        matching[0],
        ledger=ledger,
        ledger_generation_id=ledger_generation_id,
        ledger_artifact=ledger_artifact,
        baseline_bytes=baseline_bytes,
        crosswalk_bytes=crosswalk_bytes,
        code_bytes=Path(__file__).read_bytes(),
        cutoff=cutoff,
    )
    if dict(receipt) != expected:
        raise StaticSnapshotReceiptError(
            "static snapshot receipt facts differ from staged inputs"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-ledger", type=Path, required=True)
    parser.add_argument("--provider-ledger-generation", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--crosswalk", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    authority, receipts = issue_static_snapshot_receipts(
        ledger_path=args.provider_ledger,
        provider_ledger_manifest_path=args.provider_ledger_generation,
        baseline_path=args.baseline,
        crosswalk_path=args.crosswalk,
        issuer_code_path=Path(__file__),
    )
    for artifact_id, receipt in receipts.items():
        path = args.output_dir / artifact_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_canonical_bytes(receipt))
    authority_path = args.output_dir / _AUTHORITY_ARTIFACT_ID
    authority_path.parent.mkdir(parents=True, exist_ok=True)
    authority_path.write_bytes(_canonical_bytes(authority))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
