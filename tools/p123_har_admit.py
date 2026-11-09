"""Admit Portfolio123 recorder or fetcher bodies into a HAR root, with a checked id binding.

The generation reads only ``csv/`` and ``downloads/`` files of the plan's HAR root, family by
file name (``strategy_<id>_app_investment_perfdata_<id>.csv`` is managed performance, and so
on).  The passive recorder (``tools/p123_cdp_recorder.py``) saves raw bodies under id-less
path templates, so nothing in a recorder body's name says which investment it belongs to; the
fetcher (``tools/p123_cdp_fetcher.py routes``) names its bodies with the id.  This tool is the
one place that turns either into HAR CSVs, offline and read-only on its inputs:

``admit``           copy a base HAR root into a NEW root, add the admitted CSVs, and write an
                    admission manifest (outside the root) that records the binding evidence,
                    the sha256 of every source body and output CSV, and the final root layout.
``stage-designer``  stage a capture directory for ``tools/portfolio_designer_model_curve.py``
                    (``capture-manifest.json`` + ``raw/r2g_<id>_dm_true.json`` +
                    ``raw/r2g_<id>_perf-full.json``) from a full-history curve.

Id binding (recorder bodies), every step checked; a body that cannot be bound is a refusal,
never a skip:

1. The account's investment list (``/app/investment/resource`` with ``f``/``s``) must be one
   set of bytes in every visit; it names each live item (``portfolioUid``).
2. An item's own resource body (``/app/investment/resource/{id}``) is matched to exactly one
   list row on ``name``, ``inceptionDt`` and ``posCnt``; zero or several rows refuse, and a
   ``portfolioUid`` in the body must agree.
3. A ``perf-data`` or ``holdings`` body binds to the item whose resource body is the nearest
   one before it (within ``WINDOW`` ordinals, no other item in between), and is corroborated by
   the body itself: perf-data ``date`` equals the item's ``inceptionDt``, the holdings position
   count equals ``posCnt``.  An uncorroborated body refuses.
4. Fetcher bodies carry the id in the file name; where the same (id, kind) is also in a
   recorder run the two must be byte-identical.

Designer curves: the model body states its own id (``portUid``).  A curve is only admitted when
it starts on the model's ``inceptionDt``, ends on its ``endDt`` and has its launch line on
``launchDt`` -- the 15-year chart window starts years after inception and is refused here with
the same reason the curve stager would give.

``--dry-run`` is the scratch mode: the output root must sit under a ``scratch`` folder, the
manifest says ``evidence_eligible: false``, and the same checks and messages apply.  A real run
refuses a scratch/temp root and any root inside a Git worktree.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Sequence

from quant_engine.core import InterpreterRefusal, require_current_interpreter

SCHEMA = "p123-har-admission/1"
WINDOW = 40  # ordinals between an item's resource body and the bodies bound to it
LIST_TEMPLATE = "/app/investment/resource"
ITEM_RESOURCE = "/app/investment/resource/{id}"
ITEM_PERF = "/app/investment/perf-data/{id}"
ITEM_HOLDINGS = "/app/investment/holdings/{id}"
ACCOUNT_RESOURCE = "/app/account/resource/{id}"
MODEL = "/spr/strategy/model/{id}"
MODEL_PERF = "/spr/strategy/model/{id}/perf"
MODEL_HOLDINGS = "/spr/strategy/model/{id}/holdings"
_TEMP_PARTS = {"tmp", "temp", "scratch"}
_ITEM_KINDS = ("resource", "perf", "holdings")
_FETCHER_NAMES = {
    "perf": re.compile(r"^strategy_(\d+)_app_investment_perfdata_init\.json$"),
    "resource": re.compile(r"^strategy_(\d+)_app_investment_resource\.json$"),
    "holdings": re.compile(r"^strategy_(\d+)_app_investment_holdings\.json$"),
}
_DESIGNER_NAMES = {
    "model": re.compile(r"^r2g_(\d+)_dm_true\.json$"),
    "perf": re.compile(r"^r2g_(\d+)_perf-full\.json$"),
    "holdings": re.compile(r"^r2g_(\d+)_holdings\.json$"),
}


class AdmissionRefused(ValueError):
    """An input cannot be admitted; the message names the first reason."""


@dataclass(frozen=True)
class Body:
    source: str  # "recorder" or "fetcher"
    label: str  # recorder ordinal or fetcher file name
    template: str
    sha256: str
    data: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdmissionRefused(
                f"{self.source} body {self.label} is not JSON"
            ) from exc


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _regular(path: Path, what: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise AdmissionRefused(f"{what} is not a regular file: {path.name}")
    return path.read_bytes()


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdmissionRefused(f"{what} is not a JSON object")
    return value


def _day(value: object, what: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise AdmissionRefused(f"{what} is not an ISO date: {value!r}") from exc


def _us_day(value: object, what: str) -> date:
    parts = str(value).split("/")
    try:
        month, day, year = (int(p) for p in parts)
        return date(year, month, day)
    except ValueError as exc:
        raise AdmissionRefused(f"{what} is not a provider date: {value!r}") from exc


# ---------------------------------------------------------------- sources


def load_recorder(run: Path) -> tuple[list[tuple[int, Body]], dict[str, Any]]:
    """Verify a recorder run against its manifest and return its recorded GET 200 bodies."""

    manifest_bytes = _regular(run / "manifest.json", "recorder manifest")
    manifest = _object(json.loads(manifest_bytes), "recorder manifest")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise AdmissionRefused("recorder manifest lists no artifacts")
    listed: dict[str, str] = {}
    for item in artifacts:
        item = _object(item, "recorder artifact")
        relative = str(item.get("path") or "")
        data = _regular(run / relative, f"recorder artifact {relative}")
        if _sha(data) != item.get("sha256") or len(data) != item.get("size_bytes"):
            raise AdmissionRefused(
                f"recorder artifact differs from its manifest: {relative}"
            )
        listed[relative] = _sha(data)
    stray = sorted(
        p.relative_to(run).as_posix()
        for p in (run / "raw").glob("*")
        if p.relative_to(run).as_posix() not in listed
    )
    if stray:
        raise AdmissionRefused(
            f"recorder run holds a body its manifest does not list: {stray[0]}"
        )
    entries = json.loads(_regular(run / "entries.json", "recorder entries"))
    if not isinstance(entries, list):
        raise AdmissionRefused("recorder entries are not a list")
    bodies: list[tuple[int, Body]] = []
    for entry in entries:
        entry = _object(entry, "recorder entry")
        relative = f"raw/{int(entry['ordinal']):05d}.body"
        if (
            entry.get("method") != "GET"
            or entry.get("status") != 200
            or relative not in listed
        ):
            continue
        data = (run / relative).read_bytes()
        bodies.append(
            (
                int(entry["ordinal"]),
                Body(
                    "recorder",
                    str(entry["ordinal"]),
                    str(entry["path_template"]),
                    _sha(data),
                    data,
                ),
            )
        )
    bodies.sort(key=lambda pair: pair[0])
    return bodies, {
        "manifest_sha256": _sha(manifest_bytes),
        "entries_sha256": listed["entries.json"],
        "bodies": len(bodies),
    }


def load_fetcher(capture: Path) -> tuple[dict[str, Body], dict[str, Any]]:
    """Verify a fetcher capture directory; return its bodies by file name."""

    manifest_bytes = _regular(
        capture / "capture-manifest.json", "fetcher capture manifest"
    )
    manifest = _object(json.loads(manifest_bytes), "fetcher capture manifest")
    out: dict[str, Body] = {}
    for item in manifest.get("artifacts") or []:
        item = _object(item, "fetcher artifact")
        relative = str(item.get("relative_path") or "")
        data = _regular(capture / relative, f"fetcher artifact {relative}")
        if _sha(data) != item.get("sha256") or len(data) != item.get("observed_length"):
            raise AdmissionRefused(
                f"fetcher artifact differs from its manifest: {relative}"
            )
        out[Path(relative).name] = Body(
            "fetcher", Path(relative).name, "", _sha(data), data
        )
    return out, {"manifest_sha256": _sha(manifest_bytes), "bodies": len(out)}


# ---------------------------------------------------------------- binding


@dataclass
class Item:
    uid: int
    row: dict[str, Any]
    bodies: dict[str, Body]
    evidence: dict[str, str]


def _list_rows(bodies: Sequence[tuple[int, Body]]) -> tuple[list[dict[str, Any]], Body]:
    found = [b for _, b in bodies if b.template == LIST_TEMPLATE]
    if not found:
        raise AdmissionRefused("the run holds no account investment list")
    if len({b.sha256 for b in found}) != 1:
        raise AdmissionRefused("the account investment list differs between visits")
    rows = found[0].json()
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise AdmissionRefused("the account investment list is not a list of objects")
    uids = [r.get("portfolioUid") for r in rows]
    if any(not isinstance(u, int) for u in uids) or len(set(uids)) != len(uids):
        raise AdmissionRefused(
            "the account investment list does not name each item once"
        )
    return rows, found[0]


_IDENTITY_FIELDS = (
    "portfolioUid",
    "name",
    "assetName",
    "assetUid",
    "assetType",
    "accountUid",
    "inceptionDt",
    "isAccountLive",
)


def _identity(rows: Sequence[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted(tuple(r.get(f) for f in _IDENTITY_FIELDS) for r in rows)


def _stated_accounts(bodies: Sequence[tuple[int, Body]]) -> set[Any]:
    return {
        _object(b.json(), "account resource").get("id")
        for _, b in bodies
        if b.template == ACCOUNT_RESOURCE
    }


def _check_accounts(seen: set[Any], stated: Sequence[int]) -> list[int]:
    """The investment list is account-agnostic, so the account ids name only the file(s) it is
    saved under: each must be an account some run itself stated (``/app/account/resource/{id}``)."""
    if not stated:
        raise AdmissionRefused(
            f"name the account(s) the list is saved under with --account-id; the runs state {sorted(map(str, seen))}"
        )
    for account in stated:
        if account not in seen:
            raise AdmissionRefused(
                f"--account-id {account} is not an account the runs state: {sorted(map(str, seen))}"
            )
    return sorted(set(stated))


def bind_recorder(
    bodies: Sequence[tuple[int, Body]],
) -> tuple[dict[int, Item], Body]:
    rows, list_body = _list_rows(bodies)
    items: dict[int, Item] = {}
    current: tuple[int, int] | None = None  # (item uid, ordinal of its resource body)
    for ordinal, body in bodies:
        if body.template == ITEM_RESOURCE:
            doc = _object(body.json(), f"item resource body {ordinal}")
            hits = [
                r
                for r in rows
                if r.get("name") == doc.get("name")
                and r.get("inceptionDt") == doc.get("inceptionDt")
                and r.get("posCnt") == doc.get("posCnt")
            ]
            if len(hits) != 1:
                raise AdmissionRefused(
                    f"resource body {ordinal} matches {len(hits)} list rows on name, inception and position count"
                )
            uid = int(hits[0]["portfolioUid"])
            if "portfolioUid" in doc and doc["portfolioUid"] != uid:
                raise AdmissionRefused(
                    f"resource body {ordinal} names another item than its list row"
                )
            current = (uid, ordinal)
            _put(
                items,
                uid,
                hits[0],
                "resource",
                body,
                f"list row on name+inceptionDt+posCnt (ordinal {ordinal})",
            )
        elif body.template in (ITEM_PERF, ITEM_HOLDINGS):
            kind = "perf" if body.template == ITEM_PERF else "holdings"
            if current is None or ordinal - current[1] > WINDOW:
                raise AdmissionRefused(
                    f"{kind} body {ordinal} has no item resource body within {WINDOW} ordinals before it"
                )
            row = items[current[0]].row
            if kind == "perf":
                doc = _object(body.json(), f"perf body {ordinal}")
                if doc.get("date") != row.get("inceptionDt"):
                    raise AdmissionRefused(
                        f"perf body {ordinal} starts {doc.get('date')}, not the inception {row.get('inceptionDt')} of the item it follows"
                    )
            else:
                doc = _object(body.json(), f"holdings body {ordinal}")
                positions = doc.get("positions")
                if not isinstance(positions, list) or len(positions) != row.get(
                    "posCnt"
                ):
                    raise AdmissionRefused(
                        f"holdings body {ordinal} has {len(positions) if isinstance(positions, list) else '?'} positions, the item has {row.get('posCnt')}"
                    )
            _put(
                items,
                current[0],
                row,
                kind,
                body,
                f"nearest preceding resource body {current[1]}, corroborated (ordinal {ordinal})",
            )
    return items, list_body


def _put(
    items: dict[int, Item],
    uid: int,
    row: dict[str, Any],
    kind: str,
    body: Body,
    why: str,
) -> None:
    item = items.setdefault(uid, Item(uid, row, {}, {}))
    if kind in item.bodies and item.bodies[kind].sha256 != body.sha256:
        raise AdmissionRefused(f"item {uid} has two different {kind} bodies")
    item.bodies.setdefault(kind, body)
    item.evidence.setdefault(kind, why)


def bind_fetcher(
    files: dict[str, Body], items: dict[int, Item], rows: dict[int, dict[str, Any]]
) -> None:
    for name, body in sorted(files.items()):
        for kind, rx in _FETCHER_NAMES.items():
            match = rx.match(name)
            if match is None:
                continue
            uid = int(match.group(1))
            if uid not in rows:
                raise AdmissionRefused(
                    f"fetcher body {name} names an item the account list does not hold"
                )
            row = rows[uid]
            doc = _object(body.json(), f"fetcher body {name}")
            if kind == "perf" and doc.get("date") != row.get("inceptionDt"):
                raise AdmissionRefused(
                    f"fetcher body {name} starts {doc.get('date')}, not the inception {row.get('inceptionDt')}"
                )
            if kind == "resource" and (doc.get("name"), doc.get("inceptionDt")) != (
                row.get("name"),
                row.get("inceptionDt"),
            ):
                raise AdmissionRefused(
                    f"fetcher body {name} is not the item {uid} named in the list"
                )
            prior = items.get(uid)
            if (
                prior is not None
                and kind in prior.bodies
                and prior.bodies[kind].sha256 != body.sha256
            ):
                raise AdmissionRefused(
                    f"fetcher body {name} differs from the recorder body of the same item"
                )
            _put(items, uid, row, kind, body, f"file name carries the id ({name})")


# ---------------------------------------------------------------- outputs


def _weekdays(start: date, end: date) -> list[date]:
    out, day = [], start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _check_perf(uid: int, doc: dict[str, Any], as_of: date) -> date:
    start, last = (
        _day(doc.get("date"), f"item {uid} perf date"),
        _day(doc.get("dateLast"), f"item {uid} perf dateLast"),
    )
    data = doc.get("data")
    if not isinstance(data, list) or len(_weekdays(start, last)) != len(data):
        raise AdmissionRefused(
            f"item {uid} perf rows differ from the weekday calendar {start}..{last}"
        )
    graphs = [
        g
        for p in doc.get("panels") or []
        if isinstance(p, dict)
        for g in p.get("graphs", [])
        if isinstance(g, dict)
    ]
    if not (
        {(g.get("idx"), g.get("s")) for g in graphs}
        >= {(0, "Strategy"), (1, "Total Value")}
    ):
        raise AdmissionRefused(
            f"item {uid} perf panels lack the Strategy / Total Value graphs"
        )
    if last < as_of:
        raise AdmissionRefused(f"item {uid} perf ends {last}, before the as-of {as_of}")
    return last


def _equity_rows(doc: dict[str, Any]) -> list[list[str]]:
    """Code-1 rows on consecutive business days from the start (the preserved extractor rule)."""
    day = date.fromisoformat(str(doc["date"]))
    while day.weekday() >= 5:
        day += timedelta(days=1)
    rows = []
    for code, equity, cash in doc["data"]:
        if code == 1:
            rows.append([day.isoformat(), str(equity), str(cash)])
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    return rows


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _csv_bytes(header: Sequence[str], rows: Sequence[Sequence[str]]) -> bytes:
    import io

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def build_outputs(
    items: dict[int, Item],
    list_rows: list[dict[str, Any]],
    accounts: Sequence[int],
    as_of: date,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    files: dict[str, bytes] = {}
    columns = sorted({k for r in list_rows for k in r})
    for account in accounts:
        files[f"csv/strategy_{account}_app_investment_resource.csv"] = _csv_bytes(
            columns, [[_scalar(r.get(k)) for k in columns] for r in list_rows]
        )
    summary: dict[str, Any] = {}
    for uid, item in sorted(items.items()):
        entry: dict[str, Any] = {"kinds": sorted(item.bodies), "binding": item.evidence}
        res = (
            _object(item.bodies["resource"].json(), "resource")
            if "resource" in item.bodies
            else None
        )
        if res is not None:
            files[f"csv/strategy_{uid}_app_investment_resource_{uid}.csv"] = _csv_bytes(
                ["field", "value"], [[k, _scalar(v)] for k, v in res.items()]
            )
        if "holdings" in item.bodies:
            doc = _object(item.bodies["holdings"].json(), "holdings")
            files[f"csv/strategy_{uid}_app_investment_holdings_{uid}.csv"] = _csv_bytes(
                ["field", "value"], [[k, _scalar(v)] for k, v in doc.items()]
            )
        if "perf" in item.bodies:
            doc = _object(item.bodies["perf"].json(), "perf")
            entry["last_date"] = _check_perf(uid, doc, as_of).isoformat()
            files[f"csv/strategy_{uid}_app_investment_perfdata_{uid}.csv"] = _csv_bytes(
                ["field", "value"],
                [
                    ["data", json.dumps(doc["data"])],
                    ["date", doc["date"]],
                    ["dateLast", doc["dateLast"]],
                    ["panels", json.dumps(doc["panels"])],
                ],
            )
            eq = _equity_rows(doc)
            files[f"csv/strategy_{uid}_perf.csv"] = _csv_bytes(
                ["Date", "Total Equity", "Cash Added"], eq
            )
            entry["equity_rows"] = len(eq)
            entry["perf_rows"] = len(doc["data"])
        else:
            entry["last_date"] = None
        summary[str(uid)] = entry
    return files, summary


# ---------------------------------------------------------------- roots


def _inventory(root: Path) -> list[dict[str, Any]]:
    out = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise AdmissionRefused(
                f"base root holds a link: {path.relative_to(root).as_posix()}"
            )
        if path.is_file():
            data = path.read_bytes()
            out.append(
                {
                    "relative_path": path.relative_to(root).as_posix(),
                    "sha256": _sha(data),
                    "bytes": len(data),
                }
            )
    return out


def check_output_root(out: Path, dry_run: bool) -> None:
    if not out.is_absolute():
        raise AdmissionRefused("the output root must be absolute")
    if out.exists():
        raise AdmissionRefused("the output root must not exist yet")
    parts = {p.casefold() for p in out.parts}
    if dry_run and "scratch" not in parts:
        raise AdmissionRefused("a dry run writes under a scratch folder only")
    if not dry_run and parts & _TEMP_PARTS:
        raise AdmissionRefused(
            "a real run refuses a scratch, tmp or temp root; name the governed root"
        )
    for parent in out.parents:
        if (parent / ".git").exists():
            raise AdmissionRefused("the output root must not be inside a Git worktree")


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)


def admit(
    *,
    base: Path,
    out: Path,
    recorders: Sequence[Path],
    fetchers: Sequence[Path],
    as_of: date,
    account_ids: Sequence[int],
    dry_run: bool,
) -> dict[str, Any]:
    if not recorders:
        raise AdmissionRefused(
            "a recorder run is required: it carries the account's investment list"
        )
    check_output_root(out, dry_run)
    if base.is_symlink() or not base.is_dir():
        raise AdmissionRefused("the base HAR root is missing or linked")
    items: dict[int, Item] = {}
    recorder_summaries = []
    list_body: Body | None = None
    list_rows_raw: list[dict[str, Any]] = []
    seen_accounts: set[Any] = set()
    list_differs: list[dict[str, str]] = []
    for run in recorders:
        bodies, run_summary = load_recorder(run)
        seen_accounts |= _stated_accounts(bodies)
        run_items, run_list = bind_recorder(bodies)
        if list_body is None:
            list_body, list_rows_raw = run_list, _list_rows(bodies)[0]
        elif list_body.sha256 != run_list.sha256:
            # live columns (values, timestamps) move between sittings; the identity of every
            # item must not.  The first run's list is the one saved; the others are recorded.
            if _identity(list_rows_raw) != _identity(_list_rows(bodies)[0]):
                raise AdmissionRefused(
                    f"the investment list of {run.name} names different items than the first run's"
                )
            list_differs.append({"run": run.name, "sha256": run_list.sha256})
        for uid, item in run_items.items():
            for kind, body in item.bodies.items():
                _put(
                    items,
                    uid,
                    item.row,
                    kind,
                    body,
                    f"{item.evidence[kind]} [run {run.name}]",
                )
        recorder_summaries.append({"name": run.name, **run_summary})
    assert list_body is not None
    accounts = _check_accounts(seen_accounts, account_ids)
    list_rows = {int(r["portfolioUid"]): r for r in list_rows_raw}
    fetcher_summaries = []
    for capture in fetchers:
        files, summary = load_fetcher(capture)
        bind_fetcher(files, items, list_rows)
        fetcher_summaries.append(
            {"path_name": capture.parent.name + "/" + capture.name, **summary}
        )
    outputs, summary = build_outputs(items, list_rows_raw, accounts, as_of)
    base_inventory = _inventory(base)
    base_by_path = {e["relative_path"]: e for e in base_inventory}
    replaced = {}
    for relative in outputs:
        if relative in base_by_path:
            if relative not in {
                f"csv/strategy_{a}_app_investment_resource.csv" for a in accounts
            }:
                raise AdmissionRefused(
                    f"admitted file would replace a base file: {relative}"
                )
            replaced[relative] = base_by_path[relative]["sha256"]
    stale_lists = sorted(
        r
        for r in base_by_path
        if re.fullmatch(r"csv/strategy_[a-z0-9]+_app_investment_resource\.csv", r)
        and r not in outputs
    )
    root = out / "har-root"
    root.mkdir(parents=True)
    for entry in base_inventory:
        relative = entry["relative_path"]
        if relative in outputs:
            continue
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(base / relative, destination)
        if _sha(destination.read_bytes()) != entry["sha256"]:
            raise AdmissionRefused(f"base file changed while copying: {relative}")
    for relative, data in sorted(outputs.items()):
        _write_new(root / relative, data)
    layout = _inventory(root)
    origin = {relative: "admitted" for relative in outputs}
    manifest = {
        "schema": SCHEMA,
        "evidence_eligible": not dry_run,
        "as_of": as_of.isoformat(),
        "list_saved_for_accounts": accounts,
        "sources": {
            "recorders": recorder_summaries,
            "investment_list_sha256": list_body.sha256,
            "investment_lists_of_other_runs": list_differs,
            "fetchers": fetcher_summaries,
            "base_root_files": len(base_inventory),
        },
        "items": summary,
        "bodies": {
            f"{uid}/{kind}": {"source": b.source, "label": b.label, "sha256": b.sha256}
            for uid, item in sorted(items.items())
            for kind, b in sorted(item.bodies.items())
        },
        "outputs": {
            r: {"sha256": _sha(d), "bytes": len(d)} for r, d in sorted(outputs.items())
        },
        "replaced_base_files": replaced,
        "stale_base_lists_left_in_root": stale_lists,
        "final_root_layout": [
            {**e, "origin": origin.get(e["relative_path"], "base")} for e in layout
        ],
        "har_root": "har-root",
    }
    _write_new(
        out / "p123-har-admission-manifest.json",
        json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8") + b"\n",
    )
    return manifest


# ---------------------------------------------------------------- designer staging


def _designer_curve(
    model: dict[str, Any], perf: dict[str, Any], uid: int
) -> dict[str, str]:
    block = _object(perf.get("perfData"), "designer perfData")
    rows = block.get("data")
    if not isinstance(rows, list) or len(rows) < 2:
        raise AdmissionRefused("designer performance data path differs")
    start = _day(block.get("date"), "designer perf start")
    inception = _us_day(model.get("inceptionDt"), "designer inceptionDt")
    if start != inception:
        raise AdmissionRefused(
            f"designer {uid} curve starts {start}, not the model inception {inception}: this is a windowed curve, not the full history"
        )
    last, observed = start, start
    for index, row in enumerate(rows):
        if index:
            observed = observed + timedelta(days=int(row[0]))
        if row[1] is not None:
            last = observed
    end = _us_day(model.get("endDt"), "designer endDt")
    if last != end:
        raise AdmissionRefused(
            f"designer {uid} curve ends {last}, the model states endDt {end}"
        )
    lines = block.get("plotLines")
    if (
        not isinstance(lines, list)
        or len(lines) != 1
        or _day(lines[0].get("dt"), "launch line")
        != _us_day(model.get("launchDt"), "launchDt")
    ):
        raise AdmissionRefused(
            f"designer {uid} launch line differs from the model's launchDt"
        )
    return {"start": start.isoformat(), "end": last.isoformat()}


def stage_designer(
    *, out: Path, uid: int, recorder: Path | None, fetcher: Path | None, dry_run: bool
) -> dict[str, Any]:
    if (recorder is None) == (fetcher is None):
        raise AdmissionRefused("give exactly one of --recorder and --fetcher")
    check_output_root(out, dry_run)
    picked: dict[str, Body] = {}
    source: dict[str, Any]
    if fetcher is not None:
        files, source = load_fetcher(fetcher)
        for kind, rx in _DESIGNER_NAMES.items():
            for name, body in files.items():
                match = rx.match(name)
                if match and int(match.group(1)) == uid:
                    picked[kind] = body
    else:
        assert recorder is not None
        bodies, source = load_recorder(recorder)
        current: int | None = None
        for ordinal, body in bodies:
            if body.template == MODEL:
                doc = _object(body.json(), f"model body {ordinal}")
                current = ordinal if doc.get("portUid") == uid else None
                if current is not None:
                    if "model" in picked and picked["model"].sha256 != body.sha256:
                        raise AdmissionRefused(f"two different model bodies for {uid}")
                    picked["model"] = body
            elif (
                body.template in (MODEL_PERF, MODEL_HOLDINGS)
                and current is not None
                and ordinal - current <= WINDOW
            ):
                kind = "perf" if body.template == MODEL_PERF else "holdings"
                if kind in picked and picked[kind].sha256 != body.sha256:
                    raise AdmissionRefused(
                        f"two different {kind} bodies for designer {uid}"
                    )
                picked[kind] = body
    if "model" not in picked or "perf" not in picked:
        raise AdmissionRefused(
            f"designer {uid}: the model body or the curve is missing"
        )
    model = _object(picked["model"].json(), "designer model")
    if model.get("portUid") != uid:
        raise AdmissionRefused(
            f"designer model body names {model.get('portUid')}, not {uid}"
        )
    curve = _designer_curve(model, _object(picked["perf"].json(), "designer perf"), uid)
    names = {
        "model": f"r2g_{uid}_dm_true.json",
        "perf": f"r2g_{uid}_perf-full.json",
        "holdings": f"r2g_{uid}_holdings.json",
    }
    artifacts = []
    for kind, body in sorted(picked.items()):
        _write_new(out / "raw" / names[kind], body.data)
        artifacts.append(
            {
                "relative_path": f"raw/{names[kind]}",
                "sha256": body.sha256,
                "observed_length": len(body.data),
            }
        )
    manifest = {
        "artifacts": artifacts,
        "schema": "p123-designer-capture-staged/1",
        "evidence_eligible": not dry_run,
        "designer_model_id": uid,
        "curve": curve,
        "source": source,
    }
    _write_new(
        out / "capture-manifest.json",
        json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8") + b"\n",
    )
    return manifest


# ---------------------------------------------------------------- CLI


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    adm = sub.add_parser("admit", help="admit bodies into a new HAR root")
    adm.add_argument(
        "--base", type=Path, required=True, help="the HAR root to extend (read-only)"
    )
    adm.add_argument("--out", type=Path, required=True, help="new absolute directory")
    adm.add_argument(
        "--recorder",
        type=Path,
        action="append",
        required=True,
        help="recorder run directory (repeatable; one sitting)",
    )
    adm.add_argument(
        "--fetcher",
        type=Path,
        action="append",
        default=[],
        help="fetcher capture directory",
    )
    adm.add_argument(
        "--as-of", required=True, help="every admitted perf series must reach this date"
    )
    adm.add_argument(
        "--account-id",
        type=int,
        action="append",
        default=[],
        help="account id(s) the investment list is saved under (repeatable)",
    )
    adm.add_argument("--dry-run", action="store_true")
    des = sub.add_parser("stage-designer", help="stage a designer capture directory")
    des.add_argument("--out", type=Path, required=True)
    des.add_argument("--model-id", type=int, required=True)
    des.add_argument("--recorder", type=Path)
    des.add_argument("--fetcher", type=Path)
    des.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        require_current_interpreter("the P123 HAR admission")
    except InterpreterRefusal as exc:
        print(f"p123-har-admit: REFUSED: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "admit":
            manifest = admit(
                base=args.base,
                out=args.out,
                recorders=args.recorder,
                fetchers=args.fetcher,
                as_of=_day(args.as_of, "--as-of"),
                account_ids=args.account_id,
                dry_run=args.dry_run,
            )
            print(
                f"admitted {len(manifest['outputs'])} files for {len(manifest['items'])} items; evidence_eligible={manifest['evidence_eligible']}"
            )
        else:
            manifest = stage_designer(
                out=args.out,
                uid=args.model_id,
                recorder=args.recorder,
                fetcher=args.fetcher,
                dry_run=args.dry_run,
            )
            print(
                f"staged designer {args.model_id}: {manifest['curve']['start']}..{manifest['curve']['end']}"
            )
    except (AdmissionRefused, OSError, ValueError, KeyError) as exc:
        print(f"p123-har-admit: REFUSED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
