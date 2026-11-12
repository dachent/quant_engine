"""Assemble ``expected-baseline.json`` from the two independent derivations alone.

The retained assemblers (R2.1, R2.2 and the restated R2.2 one, all outside Git) compare the two
derivation outputs with the PREVIOUS baseline: same row set, same values, refusal of any change outside
an expected set. That cannot work at a new cutoff, where no previous baseline exists. This tool admits a
row by one rule only: both derivers returned it and they agree.

    release_baseline_assemble.py --derive-a A.json --derive-b B.json --manifest M.json
        --head SHA --cutoff YYYY-MM-DD --generated-utc TS
        (--admission-decision-text TEXT | --admission-decision-file FILE)
        [--template FILE] [--data-root DIR ...] [--strict-reasons] --out FILE

Row rule (the comparison the retained assemblers used; units and tolerances are those of
``tests/acceptance/test_expected_baseline.py``):

* derivable in both: ``unit`` and ``as_of`` equal, and ``as_of`` an ISO date not after the cutoff (a
  missing or null one is refused: all 165 R2.2 metric rows carry one); USD within 0.01, ratio within 1e-8 (``Decimal``, no
  float), ``list`` as sets of strings, ``count``/``date``/``status`` equal as strings; any other unit is
  refused. The row carries A's value as ``expected`` (never averaged).
* not derivable in both: the row goes to ``not_derivable`` with both reasons. The two derivers write their
  reasons independently, so the texts normally differ; ``--strict-reasons`` refuses any difference.
* anything else (a value disagreement, one side derivable and the other not, a row present on one side
  only, a duplicate row) is a disagreement. Every disagreement is listed and NOTHING is written.

Header refusals: a derivation that is not ``qe-cp-derivation/1`` or is the wrong side; a manifest, A or B
whose cutoff differs from ``--cutoff``; A or B whose ``code_commit`` differs from the other or from
``--head`` (a ``+dirty`` stamp is refused); a manifest whose sha256 is not the one A (and B, when it names
one) recorded; different strategy-basis declarations; one input path pinned to two digests; a manifest schema other than
``qe-cp-acceptance-inputs/2`` or ``/3`` (the versions both derivers accept).

Nothing is read from the clock or from a previous baseline. ``--generated-utc`` is required. Header text
(``schema``, ``metric_spec``, ``spec_version``, ``release_rule``, ``control``, ``conventions``) is the
committed ``DEFAULT_HEADER``; ``--template`` may replace any of it, and may supply the ordered
``authority_pointer`` list (each digest must be one an input pinned, and every pinned digest must appear).
With ``--data-root`` (repeatable; REQUIRED when a template sets ``authority_pointer``) every pointer path is
resolved under a root and the file's bytes must hash to the claimed digest. A template never supplies rows: one that carries ``metrics`` or ``not_derivable`` is refused.

The output is created, never replaced, and is a pure function of the inputs.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
from typing import Any

from quant_engine.core import check_entry_interpreter, strict_json_loads

DERIVATION_SCHEMA = "qe-cp-derivation/1"
# The versions both derivers accept (acceptance/derive_a and derive_b read only these two).
SUPPORTED_MANIFEST_SCHEMAS = frozenset(
    {"qe-cp-acceptance-inputs/2", "qe-cp-acceptance-inputs/3"}
)
CONTRACT_PATH = "contracts/portfolio-financial-mvp-source-v1.json"
CONTRACT_FAMILY = "financial_source_contract"
DECLARED_CUTOFF_FAMILY = "declared_cutoff_date"

# The header text of the R2.2 baseline (the only header text the retained assemblers carried over).
DEFAULT_HEADER: dict[str, str] = {
    "schema": "qe-cp-expected-baseline/1",
    "metric_spec": "C",
    "spec_version": "v5",
    "release_rule": "4.5D",
    "control": "R8",
    "conventions": (
        "Expected values retain agreed derivation A values without averaging; not-derivable rows "
        "retain paired derivation reasons; authority pointers resolve under the QE data root "
        "(contracts/, data*/, evidence/)."
    ),
}
TEMPLATE_KEYS = frozenset({*DEFAULT_HEADER, "authority_pointer"})

# tests/acceptance/test_expected_baseline.py: TOLERANCE_UNITS and EXACT_UNITS, and the per-unit tolerance.
TOLERANCES: dict[str, str] = {"USD": "0.01", "ratio": "0.00000001"}
EXACT_UNITS = frozenset({"count", "date", "list", "status"})
ALLOWED_POINTER_ROOTS = (
    "contracts/",
    "dataPortfolio/",
    "dataQuantbase/",
    "dataMatrixport/",
    "evidence/",
    "schemas/",
)
_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

RowKey = tuple[str, str, str]


class AssemblyError(Exception):
    """A refusal; ``detail`` is one line per finding."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = strict_json_loads(data, label=label)
    except ValueError as exc:
        raise AssemblyError("INPUT", str(exc)) from exc
    if not isinstance(value, dict):
        raise AssemblyError("INPUT", f"{label}: must be a JSON object")
    return value


def _is_data_root_relative(path: str) -> bool:
    """The pointer-path gate of ``test_expected_baseline.py`` (no audit allowlist)."""

    if PureWindowsPath(path).anchor or PurePosixPath(path).anchor or "\\" in path:
        return False
    parts = PurePosixPath(path).parts
    if not parts or any(part in {".", ".."} for part in parts):
        return False
    return path == str(PurePosixPath(path)) and path.startswith(ALLOWED_POINTER_ROOTS)


def _as_decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def values_agree(left: object, right: object, unit: str) -> bool:
    """Whether two derivations of one row agree under the unit's rule."""

    if unit in TOLERANCES:
        a, b = _as_decimal(left), _as_decimal(right)
        return (
            a is not None and b is not None and abs(a - b) <= Decimal(TOLERANCES[unit])
        )
    if unit == "list":
        a_list = left if isinstance(left, list) else [left]
        b_list = right if isinstance(right, list) else [right]
        # multiset: order is free, multiplicity is not (['a', 'a'] != ['a'])
        return sorted(map(str, a_list)) == sorted(map(str, b_list))
    return str(left) == str(right)


def _row_key(row: Mapping[str, Any]) -> RowKey:
    return (str(row["metric_id"]), str(row["entity"]), str(row.get("window") or ""))


def _side_rows(
    doc: Mapping[str, Any], side: str, problems: list[str]
) -> tuple[dict[RowKey, dict[str, Any]], dict[RowKey, dict[str, Any]]]:
    metrics: dict[RowKey, dict[str, Any]] = {}
    not_derivable: dict[RowKey, dict[str, Any]] = {}
    for name, target in (("metrics", metrics), ("not_derivable", not_derivable)):
        rows = doc.get(name)
        if not isinstance(rows, list):
            raise AssemblyError("INPUT", f"derive {side}: {name} must be a list")
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("metric_id"), str)
                or not isinstance(row.get("entity"), str)
            ):
                raise AssemblyError(
                    "INPUT", f"derive {side}: a {name} row has no metric_id and entity"
                )
            key = _row_key(row)
            if key in target:
                problems.append(f"{key}: derive {side} lists it twice in {name}")
            target[key] = row
    for key in sorted(set(metrics) & set(not_derivable)):
        problems.append(
            f"{key}: derive {side} lists it as both a metric and not derivable"
        )
    return metrics, not_derivable


def _check_header(
    a: Mapping[str, Any],
    b: Mapping[str, Any],
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    head: str,
    cutoff: str,
) -> None:
    for side, doc in (("a", a), ("b", b)):
        if doc.get("schema") != DERIVATION_SCHEMA:
            raise AssemblyError(
                "DERIVATION_SCHEMA",
                f"derive {side}: schema is {doc.get('schema')!r}, not {DERIVATION_SCHEMA}",
            )
        if doc.get("derivation") != side:
            raise AssemblyError(
                "DERIVATION_SIDE",
                f"derive {side}: derivation field is {doc.get('derivation')!r}",
            )
    try:
        date.fromisoformat(cutoff)
    except ValueError as exc:
        raise AssemblyError("USAGE", f"--cutoff {cutoff!r} is not a date") from exc
    if manifest.get("schema") not in SUPPORTED_MANIFEST_SCHEMAS:
        raise AssemblyError(
            "MANIFEST_SCHEMA", f"manifest schema is {manifest.get('schema')!r}"
        )
    if manifest.get("cutoff") != cutoff:
        raise AssemblyError(
            "CUTOFF_MISMATCH",
            f"manifest cutoff {manifest.get('cutoff')!r} differs from --cutoff {cutoff}",
        )
    if a.get("cutoff") != cutoff:
        raise AssemblyError(
            "CUTOFF_MISMATCH",
            f"derive a cutoff {a.get('cutoff')!r} differs from --cutoff {cutoff}",
        )
    if b.get("cutoff") != cutoff:
        raise AssemblyError(
            "CUTOFF_MISMATCH",
            f"derive b cutoff {b.get('cutoff')!r} differs from --cutoff {cutoff}",
        )
    if not _COMMIT.fullmatch(head):
        raise AssemblyError("USAGE", "--head must be a 40-hex commit sha")
    for side, doc in (("a", a), ("b", b)):
        commit = doc.get("code_commit")
        if commit != head:
            raise AssemblyError(
                "COMMIT_MISMATCH",
                f"derive {side} code_commit {commit!r} is not --head {head}",
            )
    for side, doc in (("a", a), ("b", b)):
        named = doc.get("inputs_manifest")
        if not isinstance(named, dict):
            raise AssemblyError(
                "MANIFEST_MISMATCH",
                f"derive {side} does not record the manifest it read",
            )
        if named.get("sha256") != manifest_sha256:
            raise AssemblyError(
                "MANIFEST_MISMATCH",
                f"derive {side} read manifest {named.get('sha256')!r}, not {manifest_sha256}",
            )
    if manifest.get("schema") == "qe-cp-acceptance-inputs/3":
        declared = [
            e
            for e in manifest.get("inputs", [])
            if isinstance(e, dict) and e.get("family") == DECLARED_CUTOFF_FAMILY
        ]
        if len(declared) != 1 or not _SHA256.fullmatch(str(declared[0].get("sha256"))):
            raise AssemblyError(
                "CUTOFF_ARTIFACT",
                f"manifest names {len(declared)} pinned {DECLARED_CUTOFF_FAMILY} "
                "entries, not one",
            )
    if _basis(a) != _basis(b):
        raise AssemblyError(
            "BASIS_MISMATCH",
            "derive a and derive b ran with different strategy-basis declarations",
        )


def _basis(doc: Mapping[str, Any]) -> object:
    """The strategy-basis declaration a derivation recorded, less its machine-local path."""

    value = doc.get("strategy_basis")
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if key != "path"}
    return value


def _pins(
    a: Mapping[str, Any], b: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[tuple[str, str], str]:
    """Every (path, sha256) the three documents pin, refusing one path with two digests."""

    pinned: dict[str, str] = {}
    out: dict[tuple[str, str], str] = {}
    for label, entries in (
        ("manifest", manifest.get("inputs")),
        ("derive a", a.get("inputs")),
        ("derive b", b.get("inputs")),
    ):
        if not isinstance(entries, list):
            raise AssemblyError("INPUT", f"{label}: inputs must be a list")
        for entry in entries:
            path = entry.get("path") if isinstance(entry, dict) else None
            sha = entry.get("sha256") if isinstance(entry, dict) else None
            if (
                not isinstance(path, str)
                or not isinstance(sha, str)
                or not _SHA256.fullmatch(sha)
            ):
                raise AssemblyError(
                    "INPUT", f"{label}: an input lacks a path or a sha256"
                )
            if pinned.setdefault(path, sha) != sha:
                raise AssemblyError(
                    "PIN_MISMATCH",
                    f"{path} is pinned to {pinned[path]} and to {sha} ({label})",
                )
            out[(path, sha)] = label
    return out


def _contract_sha(manifest: Mapping[str, Any]) -> str:
    entries = [
        e
        for e in manifest["inputs"]
        if isinstance(e, dict) and e.get("family") == CONTRACT_FAMILY
    ]
    if len(entries) != 1:
        raise AssemblyError(
            "CONTRACT",
            f"manifest has {len(entries)} {CONTRACT_FAMILY} entries, not one",
        )
    return str(entries[0]["sha256"])


def _pointers(
    pins: Mapping[tuple[str, str], str],
    template: Mapping[str, Any] | None,
    contract_sha: str,
) -> list[dict[str, str]]:
    required = {sha for _, sha in pins}
    if template is not None and "authority_pointer" in template:
        listed = template["authority_pointer"]
        if not isinstance(listed, list) or not all(
            isinstance(p, dict)
            and isinstance(p.get("path"), str)
            and isinstance(p.get("sha256"), str)
            for p in listed
        ):
            raise AssemblyError(
                "TEMPLATE", "authority_pointer must be a list of {path, sha256}"
            )
        pointers = [{"path": p["path"], "sha256": p["sha256"]} for p in listed]
        have = {p["sha256"] for p in pointers}
        if have != required:
            raise AssemblyError(
                "POINTER_TEMPLATE",
                f"template digests differ from the pinned inputs: missing {sorted(required - have)}, "
                f"extra {sorted(have - required)}",
            )
        if len({(p["path"], p["sha256"]) for p in pointers}) != len(pointers):
            raise AssemblyError("POINTER_TEMPLATE", "template lists a pointer twice")
    else:
        pointers = [{"path": path, "sha256": sha} for path, sha in sorted(pins)]
    offenders = [p["path"] for p in pointers if not _is_data_root_relative(p["path"])]
    if offenders:
        raise AssemblyError(
            "POINTER_PATH",
            "inputs outside the data root cannot be authority pointers; stage them under it or give "
            "--template an authority_pointer list: " + ", ".join(offenders),
        )
    at_contract = [p for p in pointers if p["path"] == CONTRACT_PATH]
    if at_contract != [{"path": CONTRACT_PATH, "sha256": contract_sha}]:
        raise AssemblyError(
            "CONTRACT",
            f"the pointers must name {CONTRACT_PATH} once, at {contract_sha}; found {at_contract}",
        )
    return pointers


def _as_of_problem(
    x: Mapping[str, Any], y: Mapping[str, Any], cutoff: str
) -> str | None:
    """Why a derivable row's ``as_of`` cannot be admitted, or ``None``.

    Every derivable row must name the day its value is as of: an ISO date that is not after the
    cutoff. In the R2.2 world every one of the 165 metric rows carries one (the latest observation on
    or before the cutoff, e.g. 2026-07-17 for a stale account, and the cutoff itself for the rest), so a
    missing or null ``as_of`` has no legitimate case here and is refused, as is a date after the cutoff.
    """

    for side, row in (("a", x), ("b", y)):
        raw = row.get("as_of")
        try:
            day = date.fromisoformat(raw) if isinstance(raw, str) else None
        except ValueError:
            day = None
        if day is None:
            return f"derive {side} as_of {raw!r} is missing or not an ISO date"
        if day > date.fromisoformat(cutoff):
            return f"derive {side} as_of {raw} is after the cutoff {cutoff}"
    return None


def _verify_pointer_bytes(
    pointers: Sequence[Mapping[str, str]], data_roots: Sequence[Path]
) -> None:
    """Each pointer path must resolve to a file under a data root whose bytes have the claimed digest."""

    resolved = [root.resolve() for root in data_roots]
    problems: list[str] = []
    for pointer in pointers:
        found: Path | None = None
        for root in resolved:
            candidate = root.joinpath(*PurePosixPath(pointer["path"]).parts)
            try:
                inside = candidate.resolve().is_relative_to(root)
            except OSError:
                inside = False
            if inside and candidate.is_file():
                found = candidate
                break
        if found is None:
            problems.append(f"{pointer['path']}: not a file under any --data-root")
            continue
        digest = hashlib.sha256()
        with found.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != pointer["sha256"]:
            problems.append(
                f"{pointer['path']}: bytes hash to {digest.hexdigest()}, not {pointer['sha256']}"
            )
    if problems:
        raise AssemblyError("POINTER_BYTES", "\n".join(problems))


def _merge_rows(
    a: tuple[dict[RowKey, dict[str, Any]], dict[RowKey, dict[str, Any]]],
    b: tuple[dict[RowKey, dict[str, Any]], dict[RowKey, dict[str, Any]]],
    strict_reasons: bool,
    cutoff: str,
    problems: list[str],
) -> tuple[
    list[tuple[RowKey, dict[str, Any], dict[str, Any]]],
    list[tuple[RowKey, str, str, str | None]],
]:
    am, an = a
    bm, bn = b
    agreed: list[tuple[RowKey, dict[str, Any], dict[str, Any]]] = []
    undone: list[tuple[RowKey, str, str, str | None]] = []
    for key in sorted(set(am) | set(an) | set(bm) | set(bn)):
        in_a = "metric" if key in am else "not derivable" if key in an else None
        in_b = "metric" if key in bm else "not derivable" if key in bn else None
        if in_a is None or in_b is None:
            problems.append(
                f"{key}: row present in derive {'b' if in_a is None else 'a'} only"
            )
        elif in_a != in_b:
            problems.append(f"{key}: derive a has it as {in_a}, derive b as {in_b}")
        elif in_a == "metric":
            x, y = am[key], bm[key]
            unit = x.get("unit")
            if unit != y.get("unit"):
                problems.append(f"{key}: unit {unit!r} vs {y.get('unit')!r}")
            elif unit not in TOLERANCES and unit not in EXACT_UNITS:
                problems.append(f"{key}: unit {unit!r} has no comparison rule")
            elif (bad := _as_of_problem(x, y, cutoff)) is not None:
                problems.append(f"{key}: {bad}")
            elif x.get("as_of") != y.get("as_of"):
                problems.append(
                    f"{key}: as_of {x.get('as_of')!r} vs {y.get('as_of')!r}"
                )
            elif x.get("value") is None or y.get("value") is None:
                problems.append(f"{key}: a metric row has no value")
            elif not values_agree(x["value"], y["value"], str(unit)):
                problems.append(
                    f"{key}: value {x['value']!r} vs {y['value']!r} ({unit})"
                )
            else:
                agreed.append((key, x, y))
        else:
            ra, rb = an[key].get("reason"), bn[key].get("reason")
            if (
                not isinstance(ra, str)
                or not isinstance(rb, str)
                or not ra.strip()
                or not rb.strip()
            ):
                problems.append(f"{key}: a not-derivable row has no reason")
            elif strict_reasons and ra.strip() != rb.strip():
                problems.append(f"{key}: reason {ra.strip()!r} vs {rb.strip()!r}")
            else:
                # applicability survives only when both independent derivations state it
                flag_a, flag_b = (
                    an[key].get("applicability"),
                    bn[key].get("applicability"),
                )
                applicability = flag_a if flag_a == flag_b else None
                undone.append((key, ra.strip(), rb.strip(), applicability))
    return agreed, undone


def _template(data: bytes | None) -> dict[str, Any] | None:
    if data is None:
        return None
    template = _load_object(data, "template")
    if "metrics" in template or "not_derivable" in template:
        raise AssemblyError(
            "TEMPLATE", "a template supplies header text only, never rows"
        )
    for key, value in template.items():
        if key in DEFAULT_HEADER and not (isinstance(value, str) and value):
            raise AssemblyError(
                "TEMPLATE", f"template {key} must be a non-empty string"
            )
    return {key: value for key, value in template.items() if key in TEMPLATE_KEYS}


def assemble(
    *,
    derive_a: bytes,
    derive_b: bytes,
    manifest: bytes,
    head: str,
    cutoff: str,
    generated_utc: str,
    decision: bytes,
    template: bytes | None = None,
    strict_reasons: bool = False,
    data_roots: Sequence[Path] = (),
) -> bytes:
    """The bytes of ``expected-baseline.json``, or an ``AssemblyError`` naming every refusal."""

    try:
        stamp = datetime.fromisoformat(generated_utc)
    except ValueError as exc:
        raise AssemblyError(
            "USAGE", f"--generated-utc {generated_utc!r} is not ISO 8601"
        ) from exc
    if stamp.tzinfo is None:
        raise AssemblyError("USAGE", "--generated-utc must carry a UTC offset")
    if not decision.strip():
        raise AssemblyError("USAGE", "the admission decision text is empty")
    a, b = _load_object(derive_a, "derive a"), _load_object(derive_b, "derive b")
    man = _load_object(manifest, "manifest")
    _check_header(a, b, man, _sha256(manifest), head, cutoff)
    pins = _pins(a, b, man)
    supplied = _template(template)
    header = {
        **DEFAULT_HEADER,
        **{k: v for k, v in (supplied or {}).items() if k in DEFAULT_HEADER},
    }
    pointers = _pointers(pins, supplied, _contract_sha(man))
    if supplied is not None and "authority_pointer" in supplied and not data_roots:
        raise AssemblyError(
            "DATA_ROOT",
            "a template that sets authority_pointer needs --data-root, to verify each path's bytes",
        )
    if data_roots:
        _verify_pointer_bytes(pointers, data_roots)

    problems: list[str] = []
    rows_a = _side_rows(a, "a", problems)
    rows_b = _side_rows(b, "b", problems)
    agreed, undone = _merge_rows(rows_a, rows_b, strict_reasons, cutoff, problems)
    if problems:
        raise AssemblyError(
            "DISAGREEMENT",
            f"{len(problems)} disagreement(s); no baseline written\n"
            + "\n".join(problems),
        )
    if not agreed:
        raise AssemblyError("EMPTY", "both derivations agree on no metric row")

    metrics: list[dict[str, Any]] = []
    for key, x, y in agreed:
        row = dict(x)
        row["window"] = key[2]
        row.update(
            expected=x["value"],
            a_actual=x["value"],
            b_actual=y["value"],
            a_head=head,
            b_head=head,
            derivation_a={"value": x["value"], "code_commit": head},
            derivation_b={"value": y["value"], "code_commit": head},
            critical=True,
            authority_pointer=pointers,
        )
        if x["unit"] in TOLERANCES:
            row["tolerance"] = TOLERANCES[x["unit"]]
        metrics.append(row)
    not_derivable = [
        {
            "metric_id": key[0],
            "entity": key[1],
            "window": key[2],
            "reason": f"{ra}; {rb}",
            "reason_a": ra,
            "reason_b": rb,
            "critical": True,
            "authority_pointer": pointers,
            **({"applicability": applicability} if applicability else {}),
        }
        for key, ra, rb, applicability in undone
    ]
    inventory = json.dumps(
        sorted(
            [list(key) for key, _, _ in agreed] + [list(key) for key, _, _, _ in undone]
        ),
        separators=(",", ":"),
    ).encode()
    pointer_map = json.dumps(pointers, sort_keys=True, separators=(",", ":")).encode()
    manifest_sha = _sha256(manifest)
    document = {
        **header,
        "head": head,
        "cutoff": cutoff,
        "derivations": {
            "a": {
                "code_commit": head,
                "manifest_sha256": manifest_sha,
                "raw_output_sha256": _sha256(derive_a),
            },
            "b": {
                "code_commit": head,
                "manifest_sha256": manifest_sha,
                "raw_output_sha256": _sha256(derive_b),
            },
        },
        "inventory_sha256": _sha256(inventory),
        "pointer_map_sha256": _sha256(pointer_map),
        "admission_decision_sha256": _sha256(decision),
        "generated_utc": generated_utc,
        "metrics": metrics,
        "not_derivable": not_derivable,
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _create_only(path: Path, data: bytes) -> None:
    """Write beside the target, then link or rename into place without replacing anything."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    published = False
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            temporary.rename(path)  # refuses an existing target
        else:
            os.link(temporary, path)  # refuses an existing target
            temporary.unlink()
        published = True
    except FileExistsError as exc:
        raise AssemblyError(
            "OUTPUT_EXISTS", f"{path.name} already exists; outputs are create-only"
        ) from exc
    except OSError as exc:
        raise AssemblyError(
            "WRITE_FAILED", f"cannot write {path.name}: {exc.__class__.__name__}"
        ) from exc
    finally:
        if not published:
            temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_baseline_assemble.py",
        description=(__doc__ or "").split("\n\n")[0],
    )
    parser.add_argument("--derive-a", type=Path, required=True)
    parser.add_argument("--derive-b", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="the acceptance-inputs manifest both read",
    )
    parser.add_argument(
        "--head", required=True, help="the code_commit both derivations stamped"
    )
    parser.add_argument("--cutoff", required=True)
    parser.add_argument(
        "--generated-utc",
        required=True,
        help="ISO 8601 with offset; never the wall clock",
    )
    decision = parser.add_mutually_exclusive_group(required=True)
    decision.add_argument("--admission-decision-text")
    decision.add_argument("--admission-decision-file", type=Path)
    parser.add_argument(
        "--template", type=Path, help="header text and authority_pointer order only"
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        action="append",
        default=[],
        help="directory the pointer paths resolve under (repeatable); every pointer's bytes are "
        "hashed against its digest; required with a template that sets authority_pointer",
    )
    parser.add_argument("--strict-reasons", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    refused = check_entry_interpreter(
        "the expected-baseline assembly", "release-baseline-assemble"
    )
    if refused is not None:
        return refused
    try:
        decision = (
            args.admission_decision_text.encode("utf-8")
            if args.admission_decision_text is not None
            else args.admission_decision_file.read_bytes()
        )
        data = assemble(
            derive_a=args.derive_a.read_bytes(),
            derive_b=args.derive_b.read_bytes(),
            manifest=args.manifest.read_bytes(),
            head=args.head,
            cutoff=args.cutoff,
            generated_utc=args.generated_utc,
            decision=decision,
            template=args.template.read_bytes() if args.template is not None else None,
            strict_reasons=args.strict_reasons,
            data_roots=args.data_root,
        )
        _create_only(args.out, data)
    except AssemblyError as exc:
        print(f"release-baseline-assemble: FAIL: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"release-baseline-assemble: FAIL: INPUT: {exc}", file=sys.stderr)
        return 1
    print(f"{args.out}: sha256 {_sha256(data)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
