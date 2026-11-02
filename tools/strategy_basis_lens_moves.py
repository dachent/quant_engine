"""Which lens models move when a strategy's live series is restated (#624).

A restated strategy's primary decomposition lens runs on its sleeve series, whose window is
shorter than the parent account's.  Every model is therefore decided twice from the same plan
format: on the sleeve lane (the restated run) and on the account lane (the unrestated run of the
same plan, the parent account's series).  This module compares the two, model by model, and
publishes the models whose status changed with the numbers that decided them (train margin,
cross-validation folds, split margins) and the account-lane result that keeps carrying them.

Nothing here is a constant of the data: every count, margin, window and label is read from the
lens results and specifications handed in.  A model that changed status and has no account-lane
figure either is a diminished row; it is published as a finding (code
``NO_ACCOUNT_LANE_FIGURE``), never dropped.

Command line::

    python tools/strategy_basis_lens_moves.py build \\
        --series "AI Factor [P123]" \\
        --sleeve MODE=SPECS_DIR=RESULTS_DIR [...] --account MODE=SPECS_DIR=RESULTS_DIR [...] \\
        --output table.json [--markdown table.md]

``RESULTS_DIR`` holds one ``<spec name>.json`` engine output (``decomposition.json``) per
specification, or ``<spec name>.ai-models.json`` extracts of it.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

SCHEMA: Final = "strategy-basis-lens-moves/1"
FINDING_NO_ACCOUNT_FIGURE: Final = "NO_ACCOUNT_LANE_FIGURE"
FIGURE_NAMES: Final = (
    "target_cumulative_return",
    "target_current_drawdown",
    "target_max_drawdown",
)
HYBRID: Final = "LIVE_PLUS_BACKTEST"


class LensMovesError(ValueError):
    """The lens inputs do not support a table."""


def _unavailable(status: str) -> bool:
    return status.startswith("UNAVAILABLE")


def _surface(spec_name: str, estimator: object) -> str:
    if estimator == "DYNAMIC_FLS" or spec_name.endswith("-dynamic-fls"):
        return "DYNAMIC_FLS"
    return "STATIC"


def _models_by_id(
    spec: Mapping[str, Any], series_id: str
) -> dict[str, Mapping[str, Any]]:
    return {
        str(model["model_id"]): model
        for model in spec.get("models", [])
        if model.get("series_id") == series_id
    }


def _figures(source: Mapping[str, Any] | None) -> dict[str, str] | None:
    if source is None:
        return None
    values = {name: source.get(name) for name in FIGURE_NAMES}
    if any(value is None for value in values.values()):
        return None
    return {name: str(value) for name, value in values.items()}


def _row(
    *,
    mode: str,
    source_key: str,
    spec_name: str,
    model: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """One lane's view of one model: its status, window, split numbers and figures."""

    coverage = result.get("coverage") or {}
    margin = coverage.get("split_margin") or {}
    train = coverage.get("train_observations", result.get("train"))
    test = coverage.get("test_observations", result.get("test"))
    minimum_train = model.get("minimum_train_observations")
    minimum_test = model.get("minimum_test_observations")
    train_margin = margin.get(
        "train_margin",
        None if train is None or minimum_train is None else train - minimum_train,
    )
    test_margin = margin.get(
        "test_margin",
        None if test is None or minimum_test is None else test - minimum_test,
    )
    return {
        "aligned": coverage.get("aligned_observations", result.get("aligned")),
        "cv_folds": model.get("cv_folds"),
        "evidence_mode": mode,
        "factors": list(model.get("factor_ids", [])),
        "figures": _figures(coverage),
        "minimum_test_observations": minimum_test,
        "minimum_train_observations": minimum_train,
        "model_id": str(model["model_id"]),
        "source_key": source_key,
        "status": str(result["status"]),
        "surface": _surface(spec_name, model.get("estimator")),
        "test": test,
        "test_margin": test_margin,
        "train": train,
        "train_margin": train_margin,
        "window_end": coverage.get("target_observed_end"),
        "window_start": coverage.get("target_observed_start"),
        "lane_block": coverage.get("account_basis"),
    }


def rows_from_results(
    *,
    mode: str,
    source_key: str,
    spec_name: str,
    spec: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    series_id: str,
) -> list[dict[str, Any]]:
    """Normalize one specification's results (engine ``model_results`` or RI-style extracts)."""

    models = _models_by_id(spec, series_id)
    rows: list[dict[str, Any]] = []
    for result in results:
        if "series_id" in result and result["series_id"] != series_id:
            continue
        model = models.get(str(result["model_id"]))
        if model is None:
            continue
        if "window" in result and "coverage" not in result:
            # extract form: {model_id, status, window, aligned, train, test, split_margin,
            # target_cum, target_maxdd, target_curdd, account_basis}
            coverage: dict[str, Any] = {
                "aligned_observations": result.get("aligned"),
                "train_observations": result.get("train"),
                "test_observations": result.get("test"),
                "target_observed_start": result["window"][0],
                "target_observed_end": result["window"][1],
                "target_cumulative_return": result.get("target_cum"),
                "target_max_drawdown": result.get("target_maxdd"),
                "target_current_drawdown": result.get("target_curdd"),
                "split_margin": result.get("split_margin"),
                "account_basis": result.get("account_basis"),
            }
            result = {"status": result["status"], "coverage": coverage}
        rows.append(
            _row(
                mode=mode,
                source_key=source_key,
                spec_name=spec_name,
                model=model,
                result=result,
            )
        )
    return rows


def _reason(sleeve: Mapping[str, Any]) -> str:
    status = sleeve["status"]
    train_margin = sleeve["train_margin"]
    if status == "UNAVAILABLE_INSUFFICIENT_TIME_ORDERED_CV":
        return (
            f"train margin {train_margin} is below the {sleeve['cv_folds']} "
            "time-ordered cross-validation folds"
        )
    if status == "UNAVAILABLE_INSUFFICIENT_TRAIN_HISTORY":
        return (
            f"train {sleeve['train']} is below the minimum "
            f"{sleeve['minimum_train_observations']}"
        )
    if status == "UNAVAILABLE_INSUFFICIENT_TEST_HISTORY":
        return (
            f"test {sleeve['test']} is below the minimum "
            f"{sleeve['minimum_test_observations']}"
        )
    return f"status {status}"


def _sleeve_block(row: Mapping[str, Any]) -> dict[str, Any]:
    static = row["surface"] == "STATIC"
    train_margin = row["train_margin"]
    cv_margin = (
        train_margin - row["cv_folds"]
        if static and train_margin is not None and row["cv_folds"] is not None
        else None
    )
    block: dict[str, Any] = {
        "aligned": row["aligned"],
        "cv_folds": row["cv_folds"],
        "cv_margin": cv_margin,
        "minimum_test_observations": row["minimum_test_observations"],
        "minimum_train_observations": row["minimum_train_observations"],
        "reason": "",
        "status": row["status"],
        "test": row["test"],
        "test_margin": row["test_margin"],
        "train": row["train"],
        "train_margin": train_margin,
        "window": {"end": row["window_end"], "start": row["window_start"]},
    }
    block["reason"] = _reason(block)
    return block


def _account_block(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "aligned": row["aligned"],
        "figures": row["figures"],
        "status": row["status"],
        "test": row["test"],
        "train": row["train"],
        "window": {"end": row["window_end"], "start": row["window_start"]},
    }


def build_table(
    sleeve_rows: Sequence[Mapping[str, Any]],
    account_rows: Sequence[Mapping[str, Any]],
    *,
    series_id: str,
) -> dict[str, Any]:
    """The closed ``strategy-basis-lens-moves/1`` block for one restated series."""

    def key(row: Mapping[str, Any]) -> tuple[str, str, str]:
        return (row["evidence_mode"], row["surface"], row["model_id"])

    account_by_key = {key(row): row for row in account_rows}
    if len(account_by_key) != len(account_rows):
        raise LensMovesError("account-lane models are duplicated")
    if len({key(row) for row in sleeve_rows}) != len(sleeve_rows):
        raise LensMovesError("sleeve-lane models are duplicated")
    if not sleeve_rows:
        raise LensMovesError("no sleeve-lane models")
    rows: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    unchanged_unavailable: list[dict[str, Any]] = []
    for sleeve in sorted(sleeve_rows, key=key):
        account = account_by_key.get(key(sleeve))
        if account is not None and account["status"] == sleeve["status"]:
            if _unavailable(sleeve["status"]):
                unchanged_unavailable.append(
                    {
                        "account_train_margin": account["train_margin"],
                        "evidence_mode": sleeve["evidence_mode"],
                        "model_id": sleeve["model_id"],
                        "sleeve_train_margin": sleeve["train_margin"],
                        "status": sleeve["status"],
                        "surface": sleeve["surface"],
                    }
                )
            continue
        carried = (
            account is not None
            and not _unavailable(account["status"])
            and account["figures"] is not None
        )
        # a sleeve-lane model that is unavailable AND whose account lane keeps no figure is the
        # diminished row; an account lane that was itself unavailable never reaches this point
        finding = None
        if _unavailable(sleeve["status"]) and not carried:
            finding = FINDING_NO_ACCOUNT_FIGURE
        entry = {
            "account": _account_block(account),
            "evidence_mode": sleeve["evidence_mode"],
            "factors": sleeve["factors"],
            "finding": finding,
            "model_id": sleeve["model_id"],
            "move": (
                f"{account['status'] if account is not None else 'ABSENT'}"
                f" -> {sleeve['status']}"
            ),
            "sleeve": _sleeve_block(sleeve),
            "source_key": sleeve["source_key"],
            "surface": sleeve["surface"],
        }
        rows.append(entry)
        if finding is not None:
            findings.append(
                {
                    "code": finding,
                    "evidence_mode": sleeve["evidence_mode"],
                    "model_id": sleeve["model_id"],
                    "surface": sleeve["surface"],
                }
            )
    lane_blocks = [
        row["lane_block"]
        for row in sleeve_rows
        if isinstance(row.get("lane_block"), dict) and row["evidence_mode"] != HYBRID
    ]
    labels = sorted({str(block.get("label")) for block in lane_blocks})
    hybrid_margins = [
        row["train_margin"]
        for row in sleeve_rows
        if row["evidence_mode"] == HYBRID and row["train_margin"] is not None
    ]
    live_margins = [
        row["train_margin"]
        for row in sleeve_rows
        if row["evidence_mode"] != HYBRID and row["train_margin"] is not None
    ]
    unavailable = [row for row in rows if _unavailable(row["sleeve"]["status"])]
    windows = sorted(
        {
            (str(block["window"]["start"]), str(block["window"]["end"]))
            for block in lane_blocks
            if isinstance(block.get("window"), dict)
        }
    )
    summary = {
        "account_lane_labels": labels,
        "account_lane_observations": sorted(
            {
                int(block["observations"])
                for block in lane_blocks
                if "observations" in block
            }
        ),
        "account_lane_windows": [
            {"start": start, "end": end} for start, end in windows
        ],
        "changed_models": len(rows),
        "finding_count": len(findings),
        "hybrid_min_train_margin": min(hybrid_margins) if hybrid_margins else None,
        "live_only_min_train_margin": min(live_margins) if live_margins else None,
        "models_compared": len(sleeve_rows),
        "unavailable_on_both_lanes": unchanged_unavailable,
        "unavailable_by_status": dict(
            sorted(Counter(row["sleeve"]["status"] for row in unavailable).items())
        ),
        "unavailable_on_sleeve_lane": len(unavailable),
    }
    block = {
        "findings": findings,
        "rows": rows,
        "schema": SCHEMA,
        "sentence": "",
        "series_id": series_id,
        "summary": summary,
    }
    block["sentence"] = sentence(block)
    return block


def sentence(block: Mapping[str, Any]) -> str:
    """The panel sentence, generated from the table alone."""

    summary = block["summary"]
    series = block["series_id"]
    unavailable = summary["unavailable_on_sleeve_lane"]
    changed = summary["changed_models"]
    if changed == 0:
        return (
            f"No {series} lens model changes status between the sleeve lane and the "
            "account lane."
        )
    reasons = ", ".join(
        f"{count} {status.removeprefix('UNAVAILABLE_').lower().replace('_', ' ')}"
        for status, count in summary["unavailable_by_status"].items()
    )
    labels = "; ".join(summary["account_lane_labels"]) or "the parent account"
    observations = summary["account_lane_observations"]
    windows = summary["account_lane_windows"]
    span = (
        f"{min(observations)} to {max(observations)} observations"
        if observations
        else "its own observations"
    )
    first = min((w["start"] for w in windows), default=None)
    last = max((w["end"] for w in windows), default=None)
    bounds = f" ({first} to {last})" if first and last else ""
    hybrid = summary["hybrid_min_train_margin"]
    parts = [
        f"On the sleeve basis {unavailable} of the {summary['models_compared']} {series} "
        f"lens models are unavailable ({reasons}) and {changed - unavailable} more change "
        "status: the sleeve series is shorter than the parent account's, so fewer "
        "observations remain after the sleeve's first return."
        if unavailable
        else f"On the sleeve basis {changed} of the {summary['models_compared']} {series} "
        "lens models change status: the sleeve series is shorter than the parent "
        "account's."
    ]
    parts.append(
        f"Each keeps its full result on the account lane ({labels}; {span}{bounds}); "
        "see the table."
        if summary["finding_count"] == 0
        else f"{summary['finding_count']} of them have no account-lane figure and are "
        "listed as findings."
    )
    if hybrid is not None:
        parts.append(
            "The hybrid (live plus backtest) mode is not affected by the minimums: its "
            f"smallest train margin is {hybrid} observations."
        )
    return " ".join(parts)


def markdown(block: Mapping[str, Any]) -> str:
    lines = [
        f"# Lens model moves on the sleeve basis: {block['series_id']}",
        "",
        block["sentence"],
        "",
        "| mode | surface | model | factors | account lane | sleeve lane | train / min / margin | cv folds / margin | finding |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in block["rows"]:
        sleeve = row["sleeve"]
        account = row["account"]
        lines.append(
            "| {mode} | {surface} | {model} | {k} | {a} | {s} | {t} / {m} / {tm} | {cf} / {cm} | {f} |".format(
                mode=row["evidence_mode"],
                surface=row["surface"],
                model=row["model_id"],
                k=len(row["factors"]),
                a=account["status"] if account else "absent",
                s=sleeve["status"],
                t=sleeve["train"],
                m=sleeve["minimum_train_observations"],
                tm=sleeve["train_margin"],
                cf=sleeve["cv_folds"],
                cm=sleeve["cv_margin"],
                f=row["finding"] or "",
            )
        )
    lines += [
        "",
        "```json",
        json.dumps(block["summary"], indent=2, sort_keys=True),
        "```",
        "",
    ]
    return "\n".join(lines)


def _read_lane(spec: str, series_id: str) -> list[dict[str, Any]]:
    mode, specs_dir, results_dir = spec.split("=", 2)
    rows: list[dict[str, Any]] = []
    for spec_path in sorted(Path(specs_dir).glob("*.json")):
        name = spec_path.stem
        document = json.loads(spec_path.read_text("utf-8"))
        full = Path(results_dir) / f"{name}.json"
        extract = Path(results_dir) / f"{name}.ai-models.json"
        if full.is_file():
            payload = json.loads(full.read_text("utf-8"))
            results = payload["model_results"]
        elif extract.is_file():
            results = json.loads(extract.read_text("utf-8"))
        else:
            continue
        rows.extend(
            rows_from_results(
                mode=mode,
                source_key=name.removesuffix("-dynamic-fls"),
                spec_name=name,
                spec=document,
                results=results,
                series_id=series_id,
            )
        )
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--series", required=True)
    build.add_argument("--sleeve", action="append", required=True)
    build.add_argument("--account", action="append", required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    sleeve = [row for spec in args.sleeve for row in _read_lane(spec, args.series)]
    account = [row for spec in args.account for row in _read_lane(spec, args.series)]
    block = build_table(sleeve, account, series_id=args.series)
    args.output.write_bytes(
        (json.dumps(block, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    if args.markdown is not None:
        args.markdown.write_bytes(markdown(block).encode("utf-8"))
    print(
        json.dumps(
            {
                "changed": block["summary"]["changed_models"],
                "findings": len(block["findings"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
