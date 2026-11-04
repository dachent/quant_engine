#!/usr/bin/env python3
"""The per-cycle seed files of a release, from the release-inputs root and the cycle's own baseline.

Three files used to be operator inputs that no committed tool wrote:

``stage`` (before ``cycle-resolve``)
    copies ``in/implementation-reconciliation.json`` and ``in/implementation-return-attribution.json`` byte
    for byte from the pinned release-inputs root, and writes ``attribution-spec-template.json``: the root's
    attribution spec with its ``return_source`` resolved from the run's root keys, the return source and its
    manifest re-hashed against the pins the spec carries. The loader refuses a root of another cutoff, an
    unlisted file and a changed byte, so a seed that does not belong to this cycle never reaches a stage.

``derive-attribution-spec`` (after ``baseline``, before ``selected-inputs``)
    writes ``attribution-spec.json``: the template with ``as_of_date`` set to the cutoff and every available
    comparison's ``observed_start``/``observed_end`` derived from data, the first and last date the
    baseline's own ACCOUNT_LIVE series shares with the comparison's reference series (the same two series
    ``portfolio_attribution`` pairs). A baseline of another cutoff, a hash that differs, or an available row
    with no shared date refuses; nothing is defaulted.

Outputs are create-only: an existing file with other bytes refuses, one with the same bytes is left as is.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from quant_engine.core import ReleaseInputsError, resolve_release_input

import portfolio_attribution as attribution
from release_cycle_manifest import CycleManifestError, parse_roots

REPO_ROOT = Path(__file__).resolve().parents[1]
SEEDS = "ops/mvp2-release/inputs/seeds"
# release-input path -> the file name the stage graph reads under the cycle directory
CARRIED = {
    f"{SEEDS}/implementation-reconciliation.json": "in/implementation-reconciliation.json",
    f"{SEEDS}/implementation-return-attribution.json": "in/implementation-return-attribution.json",
}
TEMPLATE_SOURCE = f"{SEEDS}/attribution-spec-template.json"
TEMPLATE_NAME = "attribution-spec-template.json"
SPEC_NAME = "attribution-spec.json"
# a template's return source names its files by root key; ``stage`` resolves them to the paths the stage reads
SOURCE_KEYS = frozenset(
    {
        "root_key",
        "relative",
        "expected_sha256",
        "source_manifest_relative",
        "source_manifest_expected_sha256",
    }
)
ROW_LISTS = ("comparisons", "benchmark_comparisons")
DAILY_PERIODS = 252


class SeedError(ValueError):
    """A seed is missing, does not belong to this cycle, or cannot be derived from its data."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_create_only(path: Path, data: bytes) -> str:
    if path.exists():
        if path.read_bytes() != data:
            raise SeedError(f"{path} exists with other bytes; seeds are create-only")
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return "written"


def _canonical(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _load_object(data: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SeedError(f"{what} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SeedError(f"{what} must be an object")
    return value


def _located(roots: Mapping[str, Path], key: str, relative: str, what: str) -> Path:
    base = roots.get(key)
    if base is None:
        raise SeedError(f"{what}: root key {key!r} is not declared by the run")
    parts = relative.split("/")
    if (
        not relative
        or relative.startswith("/")
        or any(p in ("", ".", "..") for p in parts)
    ):
        raise SeedError(f"{what}: {relative!r} is not a safe relative path")
    path = (base / relative).resolve()
    if not path.is_file():
        raise SeedError(f"{what}: no file at {path}")
    return path


def _checked(path: Path, expected: object, what: str) -> None:
    actual = _sha256(path.read_bytes())
    if actual != expected:
        raise SeedError(
            f"{what}: bytes hash to {actual[:12]}, the spec pinned {str(expected)[:12]}"
        )


def resolve_template(
    template: Mapping[str, Any], roots: Mapping[str, Path]
) -> dict[str, Any]:
    """The template with ``return_source`` in the form ``portfolio_attribution`` reads."""

    source = template.get("return_source")
    if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
        raise SeedError(
            f"template return_source must hold exactly {sorted(SOURCE_KEYS)}"
        )
    csv_path = _located(roots, source["root_key"], source["relative"], "return source")
    manifest_path = _located(
        roots,
        source["root_key"],
        source["source_manifest_relative"],
        "return source manifest",
    )
    _checked(csv_path, source["expected_sha256"], "return source")
    _checked(
        manifest_path,
        source["source_manifest_expected_sha256"],
        "return source manifest",
    )
    spec = copy.deepcopy(dict(template))
    spec["return_source"] = {
        "expected_sha256": source["expected_sha256"],
        "path": csv_path.as_posix(),
        "source_manifest_expected_sha256": source["source_manifest_expected_sha256"],
        "source_manifest_path": manifest_path.as_posix(),
    }
    return spec


def stage(
    *,
    repo_root: Path,
    root: Path | None,
    manifest_sha256: str | None,
    cutoff: str,
    roots: Mapping[str, Path],
    cycle: Path,
) -> dict[str, str]:
    def pinned(relative: str) -> Path:
        return resolve_release_input(
            repo_root,
            relative,
            root=root,
            manifest_sha256=manifest_sha256,
            cutoff=cutoff,
        )

    results: dict[str, str] = {}
    for relative, target in CARRIED.items():
        data = pinned(relative).read_bytes()
        results[target] = f"{_write_create_only(cycle / target, data)} {_sha256(data)}"
    template = _load_object(
        pinned(TEMPLATE_SOURCE).read_bytes(), "attribution spec template"
    )
    if template.get("as_of_date") != cutoff:
        raise SeedError(
            f"attribution spec template is for {template.get('as_of_date')!r}, the cycle cutoff is {cutoff}"
        )
    data = _canonical(resolve_template(template, roots))
    results[TEMPLATE_NAME] = (
        f"{_write_create_only(cycle / TEMPLATE_NAME, data)} {_sha256(data)}"
    )
    return results


def _window(
    row: Mapping[str, Any],
    account: Mapping[date, Any],
    reference: Mapping[date, Any],
    cutoff: date,
) -> tuple[date, date]:
    """First and last shared date of the account's ACCOUNT_LIVE series and the reference, up to the cutoff."""

    if int(row.get("periods_per_year", 0)) >= DAILY_PERIODS:
        shared = sorted(d for d in account if d in reference and d <= cutoff)
    else:
        # a coarser reference reports one return per period; the account compounds over the same periods
        # (portfolio_attribution._receipt_bound_periods), so the window is the reference's own dates the
        # account's history spans
        first, last = min(account), min(max(account), cutoff)
        shared = sorted(d for d in reference if first <= d <= last)
    if not shared:
        raise SeedError(
            f"{row.get('strategy_id')}: the account series and {row.get('reference_series_id')} "
            f"share no date up to {cutoff}"
        )
    return shared[0], shared[-1]


def derive_windows(
    template: Mapping[str, Any],
    account_live: Mapping[str, Mapping[date, Any]],
    references: Mapping[str, Mapping[date, Any]],
    cutoff: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """The spec for ``cutoff``: ``as_of_date`` set, every available row's window derived from the data."""

    day = date.fromisoformat(cutoff)
    spec = copy.deepcopy(dict(template))
    spec["as_of_date"] = cutoff
    report: list[dict[str, str]] = []
    for name in ROW_LISTS:
        rows = spec.get(name)
        if not isinstance(rows, list):
            raise SeedError(f"template {name} must be an array")
        for row in rows:
            if row.get("status") not in attribution.AVAILABLE_STATUSES:
                continue
            strategy = row.get("strategy_id")
            reference_id = row.get("reference_series_id")
            account = account_live.get(strategy)
            if not account:
                # a strategy the baseline keeps as history only reads its retained series by id, as the
                # attribution stage does (portfolio_attribution._retained_account_history)
                account = references.get(str(row.get("account_series_id")))
            if not account:
                raise SeedError(
                    f"{strategy}: {name} row is available but neither the baseline nor the return source "
                    f"holds an account series for it"
                )
            if reference_id not in references:
                raise SeedError(
                    f"{strategy}: reference series {reference_id!r} is not in the return source"
                )
            start, end = _window(row, account, references[reference_id], day)
            row["observed_start"], row["observed_end"] = (
                start.isoformat(),
                end.isoformat(),
            )
            report.append(
                {
                    "list": name,
                    "strategy_id": str(strategy),
                    "observed_start": row["observed_start"],
                    "observed_end": row["observed_end"],
                }
            )
    return spec, report


def derive_attribution_spec(
    *,
    template_path: Path,
    baseline_root: Path,
    baseline_generation_id: str | None,
    cutoff: str,
    out: Path,
) -> list[dict[str, str]]:
    template = _load_object(template_path.read_bytes(), "attribution spec template")
    source = template.get("return_source")
    if not isinstance(source, dict):
        raise SeedError("template return_source must be an object")
    csv_path = Path(str(source.get("path")))
    for path_key, sha_key, what in (
        ("path", "expected_sha256", "return source"),
        (
            "source_manifest_path",
            "source_manifest_expected_sha256",
            "return source manifest",
        ),
    ):
        if not Path(str(source.get(path_key))).is_file():
            raise SeedError(f"{what}: no file at {source.get(path_key)}")
        _checked(Path(str(source[path_key])), source.get(sha_key), what)
    try:
        baseline, *_ = attribution.read_baseline(baseline_root, baseline_generation_id)
    except attribution.AttributionValidationError as exc:
        raise SeedError(f"baseline: {exc}") from exc
    if baseline.get("as_of_date") != cutoff:
        raise SeedError(
            f"baseline is for {baseline.get('as_of_date')!r}, the cycle cutoff is {cutoff}"
        )
    references, _ = attribution.read_return_rows(csv_path)
    spec, report = derive_windows(
        template, attribution.account_live_series(baseline), references, cutoff
    )
    _write_create_only(out, _canonical(spec))
    return report


def _single_generation(root: Path) -> str:
    names = sorted(p.name for p in (root / "generations").iterdir() if p.is_dir())
    if len(names) != 1:
        raise SeedError(
            f"{root / 'generations'} must hold exactly one generation, found {len(names)}"
        )
    return names[0]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    staged = commands.add_parser(
        "stage", help="stage the carried seeds and the attribution spec template"
    )
    staged.add_argument("--release-inputs-root", type=Path)
    staged.add_argument("--release-inputs-manifest-sha256")
    staged.add_argument("--cutoff", required=True)
    staged.add_argument("--roots-file", type=Path)
    staged.add_argument("--root", action="append", default=[], metavar="KEY=DIR")
    staged.add_argument("--cycle", type=Path, required=True)
    staged.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    derived = commands.add_parser(
        "derive-attribution-spec", help="derive the attribution spec windows"
    )
    derived.add_argument("--template", type=Path, required=True)
    derived.add_argument("--baseline-root", type=Path, required=True)
    derived.add_argument("--baseline-generation-id")
    derived.add_argument("--cutoff", required=True)
    derived.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "stage":
            roots = parse_roots(args.root, args.roots_file)
            result: Any = stage(
                repo_root=args.repo_root,
                root=args.release_inputs_root,
                manifest_sha256=args.release_inputs_manifest_sha256,
                cutoff=args.cutoff,
                roots=roots,
                cycle=args.cycle,
            )
        else:
            result = derive_attribution_spec(
                template_path=args.template,
                baseline_root=args.baseline_root,
                baseline_generation_id=args.baseline_generation_id
                or _single_generation(args.baseline_root),
                cutoff=args.cutoff,
                out=args.out,
            )
    except (SeedError, ReleaseInputsError, CycleManifestError) as exc:
        print(f"release-cycle-seeds: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
