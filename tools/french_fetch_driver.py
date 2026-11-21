"""Governed, config-driven fetch of the Kenneth French library daily feeds.

Same shape as ``tools/yahoo_fetch_driver.py``: config (data, not code) -> ``make-evidence`` ->
``fetch`` -> ``verify``. It wraps the one maintained adapter,
``skills/agent-market-data-terminal/scripts/fetch_famafrench.py`` (v1.2.0), so every staged file
has the format the public-bundle builders already require; it defines no second format.

``fetch`` checks the governance evidence, runs the adapter once per feed with the staging
directory as its working directory (the adapter refuses an ``--output`` outside the current
directory), checks every file against its manifest, the source zip and the cutoff, and writes a
create-only run folder::

    <stage-root>/<run-id>/raw/<feed>.json, .json.manifest.json, .json.source.zip   (per feed)
    <stage-root>/<run-id>/config.json, governance-evidence.json
    <stage-root>/<run-id>/publication-log.json     Last-Modified, last row, lag, required month
    <stage-root>/<run-id>/public-sources.factor-feeds.json   qe-release-public-sources/1 block
    <stage-root>/<run-id>/MANIFEST.json            hash-bound, written after the files
    <stage-root>/<run-id>/SHA256SUMS.txt           written last

A failure removes the partial run. ``verify`` is stdlib only and re-derives the file hashes, the zip
members, the manifest-declared coverage (the last period per section as the adapter's manifest
states it, not re-parsed from the parsed JSON or csv) and every publication state from those
declarations. Duplicate rows, column changes and truncation are caught downstream by the
builder (``portfolio_public_decomposition_sources._load_source`` and
``_admit_publication_boundary``). ``max_requests`` in the evidence is declarative: the driver makes
one adapter call per feed and checks only that the cap covers the feeds.

Staleness is advisory at this layer: a stale feed is a valid capture and a panel state. ``fetch`` and
``verify`` print the non-COMPLIANT feed keys, and ``--require-compliant`` exits 3 when there are
any; the age ceiling lives in the panel-state module (WP2), not here.

Publication rule ``KENNETH_FRENCH_MONTHLY_DAILY_45D_V1``: a month is required at the evaluation date
when its month end plus 45 days is on or before that date; a feed whose last row is in an earlier
month is ``HARD_STALE``, otherwise ``COMPLIANT``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence
import zipfile

SCHEMA_VERSION = 1
POLICY_ID = "KENNETH_FRENCH_MONTHLY_DAILY_45D_V1"
LAG_DAYS = 45
SOURCES_SCHEMA = "qe-release-public-sources/1"
BASE_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp"
HOST = "mba.tuck.dartmouth.edu"
FEED_KEYS = (
    "us_factors",
    "us_momentum",
    "europe_factors",
    "europe_momentum",
    "us_industries",
)
ADAPTER = Path("skills/agent-market-data-terminal/scripts/fetch_famafrench.py")
_FEED_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*_CSV")
_REVIEW = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/\d+#issuecomment-\d+")
_PERIOD = re.compile(r"\d{8}")
_SHA = re.compile(r"[0-9a-f]{64}")
_CONFIG_KEYS = {
    "schema_version",
    "cutoff",
    "evaluation_date",
    "policy_id",
    "feeds",
    "limits",
}
_EVIDENCE_KEYS = {
    "config_sha256",
    "cutoff",
    "max_requests",
    "review",
    "run_id",
    "schema_version",
    "valid_from_utc",
    "valid_until_utc",
}
_TIME = "%Y-%m-%dT%H:%M:%SZ"
Runner = Callable[[str, Path], None]


class FrenchDriverError(RuntimeError):
    """A config, evidence, fetch or verify rule was not met."""


@dataclass(frozen=True)
class Feed:
    feed_key: str
    feed_id: str


@dataclass(frozen=True)
class Config:
    cutoff: date
    evaluation_date: date
    feeds: tuple[Feed, ...]
    max_requests: int
    sha256: str
    document: Mapping[str, Any]


def _date(value: object, what: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise FrenchDriverError(f"{what} is not an ISO date: {value!r}") from exc


def _canonical(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, indent=1) + "\n").encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_config(data: bytes, cutoff_override: str | None = None) -> Config:
    try:
        document = json.loads(data)
    except ValueError as exc:
        raise FrenchDriverError("config is not JSON") from exc
    if not isinstance(document, dict):
        raise FrenchDriverError("config root must be an object")
    unknown = set(document) - _CONFIG_KEYS
    if unknown:
        raise FrenchDriverError(f"unknown key in config: {sorted(unknown)}")
    if set(document) != _CONFIG_KEYS or document["schema_version"] != SCHEMA_VERSION:
        raise FrenchDriverError("config keys or schema_version differ")
    if document["policy_id"] != POLICY_ID:
        raise FrenchDriverError(f"policy_id must be {POLICY_ID}")
    cutoff = _date(cutoff_override or document["cutoff"], "cutoff")
    evaluation = _date(document["evaluation_date"], "evaluation_date")
    if cutoff > evaluation:
        raise FrenchDriverError("cutoff must not be after the evaluation_date")
    rows = document["feeds"]
    feeds: list[Feed] = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"feed_key", "feed_id"}:
                raise FrenchDriverError("each feed needs exactly feed_key and feed_id")
            feed_id = row["feed_id"]
            if not isinstance(feed_id, str) or _FEED_ID.fullmatch(feed_id) is None:
                raise FrenchDriverError(f"feed_id must be a CSV zip stem: {feed_id!r}")
            feeds.append(Feed(str(row["feed_key"]), feed_id))
    if sorted(f.feed_key for f in feeds) != sorted(FEED_KEYS):
        raise FrenchDriverError(f"feeds must be exactly the keys {list(FEED_KEYS)}")
    if len({f.feed_id for f in feeds}) != len(feeds):
        raise FrenchDriverError("feeds must not repeat a feed_id")
    limits = document["limits"]
    cap = limits.get("max_requests") if isinstance(limits, dict) else None
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        raise FrenchDriverError("limits.max_requests must be a positive integer")
    effective = dict(document, cutoff=cutoff.isoformat())
    return Config(
        cutoff=cutoff,
        evaluation_date=evaluation,
        feeds=tuple(sorted(feeds, key=lambda f: FEED_KEYS.index(f.feed_key))),
        max_requests=cap,
        sha256=_sha(_canonical(effective)),
        document=effective,
    )


# ------------------------------------------------------------ the 45-day rule


def _month_end(year: int, month: int) -> date:
    return (date(year + month // 12, month % 12 + 1, 1)) - timedelta(days=1)


def required_month(evaluation: date) -> tuple[int, int]:
    """The latest month whose month end plus 45 days is on or before ``evaluation``."""

    year, month = evaluation.year, evaluation.month
    for _ in range(3):
        if _month_end(year, month) + timedelta(days=LAG_DAYS) <= evaluation:
            return year, month
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return year, month


def publication_state(last_period: str, evaluation: date) -> dict[str, Any]:
    """``last_period`` is ``YYYYMMDD`` (the adapter's last row of the feed)."""

    if _PERIOD.fullmatch(last_period) is None:
        raise FrenchDriverError(f"last period is not YYYYMMDD: {last_period!r}")
    year, month = int(last_period[:4]), int(last_period[4:6])
    need = required_month(evaluation)
    return {
        "last_period": f"{last_period[:4]}-{last_period[4:6]}-{last_period[6:]}",
        "required_month": f"{need[0]}-{need[1]:02d}",
        "state": "COMPLIANT" if (year, month) >= need else "HARD_STALE",
    }


# ---------------------------------------------------------------- evidence


def make_evidence(
    config: Config,
    *,
    run_id: str,
    review: str,
    valid_from: datetime,
    valid_until: datetime,
    max_requests: int,
) -> dict[str, Any]:
    if _REVIEW.fullmatch(review) is None:
        raise FrenchDriverError("review must be a GitHub issue-comment URL")
    if valid_until <= valid_from:
        raise FrenchDriverError("evidence window is empty")
    return {
        "config_sha256": config.sha256,
        "cutoff": config.cutoff.isoformat(),
        "max_requests": max_requests,
        "review": review,
        "run_id": run_id,
        "schema_version": SCHEMA_VERSION,
        "valid_from_utc": valid_from.astimezone(timezone.utc).strftime(_TIME),
        "valid_until_utc": valid_until.astimezone(timezone.utc).strftime(_TIME),
    }


def check_evidence(
    evidence: Mapping[str, Any], config: Config, run_id: str, now: datetime
) -> None:
    if set(evidence) != _EVIDENCE_KEYS or evidence["schema_version"] != SCHEMA_VERSION:
        raise FrenchDriverError("evidence keys differ")
    if evidence["config_sha256"] != config.sha256:
        raise FrenchDriverError("evidence is bound to another config")
    if evidence["run_id"] != run_id:
        raise FrenchDriverError("evidence is bound to another run id")
    if evidence["cutoff"] != config.cutoff.isoformat():
        raise FrenchDriverError("evidence is bound to another cutoff")
    if _REVIEW.fullmatch(str(evidence["review"])) is None:
        raise FrenchDriverError("evidence review is not an issue-comment URL")
    start = datetime.strptime(str(evidence["valid_from_utc"]), _TIME).replace(
        tzinfo=timezone.utc
    )
    end = datetime.strptime(str(evidence["valid_until_utc"]), _TIME).replace(
        tzinfo=timezone.utc
    )
    if now < start:
        raise FrenchDriverError("evidence is not yet valid")
    if now > end:
        raise FrenchDriverError("evidence has expired")
    if config.evaluation_date > end.date():
        raise FrenchDriverError("the evaluation date is after the evidence window")
    cap = evidence["max_requests"]
    if not isinstance(cap, int) or cap < len(config.feeds) or cap > config.max_requests:
        raise FrenchDriverError(
            "evidence max_requests does not cover the feeds or exceeds the config cap"
        )


# ------------------------------------------------------------- feed checks


def _json_file(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise FrenchDriverError(f"{what} is missing or not JSON") from exc


def _check_feed(
    raw: Path, feed: Feed, cutoff: date, window_end: datetime | None = None
) -> dict[str, Any]:
    """Re-derive one feed from its three files; returns the facts the publication log needs."""

    name = feed.feed_id
    files = {
        "json": raw / f"{name}.json",
        "zip": raw / f"{name}.json.source.zip",
        "manifest": raw / f"{name}.json.manifest.json",
    }
    for label, path in files.items():
        if not path.is_file() or path.is_symlink():
            raise FrenchDriverError(f"{name}: {label} file is missing")
    manifest = _json_file(files["manifest"], f"{name} manifest")
    artifact, source, coverage = (
        manifest.get(k) for k in ("artifact", "raw_artifact", "coverage")
    )
    request, response = manifest.get("request"), manifest.get("response")
    if not all(
        isinstance(x, dict) for x in (artifact, source, coverage, request, response)
    ):
        raise FrenchDriverError(f"{name}: manifest shape differs")
    json_bytes, zip_bytes = files["json"].read_bytes(), files["zip"].read_bytes()
    if _sha(json_bytes) != artifact.get("sha256") or len(json_bytes) != artifact.get(
        "size_bytes"
    ):
        raise FrenchDriverError(f"{name}: parsed file differs from its manifest")
    if _sha(zip_bytes) != source.get("sha256") or len(zip_bytes) != source.get(
        "size_bytes"
    ):
        raise FrenchDriverError(f"{name}: source zip differs from its manifest")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            members = {
                m.filename: archive.read(m)
                for m in archive.infolist()
                if not m.is_dir()
            }
    except zipfile.BadZipFile as exc:
        raise FrenchDriverError(f"{name}: source zip is not a zip") from exc
    listed = {m.get("name"): m.get("sha256") for m in source.get("zip_members", [])}
    if {k: _sha(v) for k, v in members.items()} != listed:
        raise FrenchDriverError(f"{name}: zip members differ from the manifest")
    if members.get(coverage.get("csv_member")) is None or _sha(
        members[coverage["csv_member"]]
    ) != coverage.get("csv_member_sha256"):
        raise FrenchDriverError(f"{name}: csv member differs from the manifest")
    if (
        request.get("feed_id") != name
        or request.get("source_url") != f"{BASE_URL}/{name}.zip"
    ):
        raise FrenchDriverError(f"{name}: request differs from the configured feed")
    if response.get("status") != 200:
        raise FrenchDriverError(
            f"{name}: response status is {response.get('status')!r}"
        )
    final = str(response.get("final_url"))
    if final != f"{BASE_URL}/{name}.zip":
        raise FrenchDriverError(f"{name}: final_url differs: {final}")
    try:
        modified = parsedate_to_datetime(str(response["last_modified"]))
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise FrenchDriverError(
            f"{name}: Last-Modified is missing or unreadable"
        ) from exc
    sections = coverage.get("sections")
    if not isinstance(sections, list) or not sections:
        raise FrenchDriverError(f"{name}: no sections in coverage")
    lasts = []
    for section in sections:
        period = str(section.get("last_period"))
        if _PERIOD.fullmatch(period) is None:
            raise FrenchDriverError(f"{name}: a section has no last period")
        if date(int(period[:4]), int(period[4:6]), int(period[6:])) > cutoff:
            raise FrenchDriverError(f"{name}: a last row is after the cutoff {cutoff}")
        lasts.append(period)
    oldest = min(lasts)
    modified_utc = modified.astimezone(timezone.utc)
    if modified_utc.date() < _month_end(int(oldest[:4]), int(oldest[4:6])):
        raise FrenchDriverError(
            f"{name}: Last-Modified {response['last_modified']} is before the data month end"
        )
    if window_end is not None and modified_utc > window_end:
        raise FrenchDriverError(
            f"{name}: Last-Modified is after the evidence window end"
        )
    return {
        "last_modified": str(response["last_modified"]),
        "last_modified_date": modified_utc.date(),
        "last_period": oldest,
        "captured_at_utc": str(response.get("captured_at_utc")),
    }


def _publication_log(
    config: Config, facts: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    rows = []
    for feed in config.feeds:
        fact = facts[feed.feed_key]
        state = publication_state(fact["last_period"], config.evaluation_date)
        end = _month_end(int(fact["last_period"][:4]), int(fact["last_period"][4:6]))
        rows.append(
            {
                "feed_key": feed.feed_key,
                "feed_id": feed.feed_id,
                "last_modified": fact["last_modified"],
                "lag_days_after_month_end": (fact["last_modified_date"] - end).days,
                **state,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_id": POLICY_ID,
        "evaluation_date": config.evaluation_date.isoformat(),
        "feeds": rows,
    }


def _sources_block(
    config: Config, raw: Path, started: str, ended: str
) -> dict[str, Any]:
    def pin(path: Path) -> dict[str, str]:
        return {"path": path.name, "sha256": _sha(path.read_bytes())}

    return {
        "schema_version": SOURCES_SCHEMA,
        "acquisition_run": {
            "run_started_at_utc": started,
            "run_ended_at_utc": ended,
            "evaluation_date": config.evaluation_date.isoformat(),
        },
        "factor_feeds": {
            feed.feed_key: {
                "source": pin(raw / f"{feed.feed_id}.json"),
                "manifest": pin(raw / f"{feed.feed_id}.json.manifest.json"),
            }
            for feed in config.feeds
        },
        "styles": [],
    }


def _inventory(run: Path) -> dict[str, dict[str, Any]]:
    skip = {"MANIFEST.json", "SHA256SUMS.txt"}
    return {
        p.relative_to(run).as_posix(): {
            "sha256": _sha(p.read_bytes()),
            "size_bytes": p.stat().st_size,
        }
        for p in sorted(run.rglob("*"))
        if p.is_file() and p.name not in skip
    }


def _adapter_path() -> Path:
    return Path(__file__).resolve().parents[1] / ADAPTER


def _default_runner(feed_id: str, cwd: Path) -> None:
    adapter = _adapter_path()
    command = [
        sys.executable,
        str(adapter),
        "--feed-id",
        feed_id,
        "--format",
        "parsed",
        "--output",
        f"{feed_id}.json",
    ]
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"adapter exited {result.returncode}: {result.stderr.strip()[-300:]}"
        )


def fetch(
    config: Config,
    *,
    stage_root: Path,
    run_id: str,
    evidence: Mapping[str, Any],
    runner: Runner | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Path:
    now = clock or (lambda: datetime.now(timezone.utc))
    check_evidence(evidence, config, run_id, now())
    window_end = datetime.strptime(str(evidence["valid_until_utc"]), _TIME).replace(
        tzinfo=timezone.utc
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise FrenchDriverError("run id is not a safe name")
    run = stage_root / run_id
    if run.exists():
        raise FrenchDriverError(f"run folder exists: {run}")
    raw = run / "raw"
    started = now().strftime(_TIME)
    raw.mkdir(parents=True)
    try:
        facts: dict[str, dict[str, Any]] = {}
        expected: set[str] = set()
        for feed in config.feeds:
            expected |= {
                f"{feed.feed_id}.json",
                f"{feed.feed_id}.json.manifest.json",
                f"{feed.feed_id}.json.source.zip",
            }
            try:
                (runner or _default_runner)(feed.feed_id, raw)
            except FrenchDriverError:
                raise
            except Exception as exc:  # noqa: BLE001 - any adapter failure is a loud stop
                raise FrenchDriverError(
                    f"{feed.feed_id}: adapter failed: {exc}"
                ) from exc
            present = {p.name for p in raw.iterdir()}
            if present - expected:
                raise FrenchDriverError(
                    f"unexpected file in staging: {sorted(present - expected)}"
                )
            facts[feed.feed_key] = _check_feed(raw, feed, config.cutoff, window_end)
        ended = now().strftime(_TIME)
        (run / "config.json").write_bytes(_canonical(config.document))
        (run / "governance-evidence.json").write_bytes(_canonical(evidence))
        (run / "publication-log.json").write_bytes(
            _canonical(_publication_log(config, facts))
        )
        (run / "public-sources.factor-feeds.json").write_bytes(
            _canonical(_sources_block(config, raw, started, ended))
        )
        adapter = _adapter_path()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "cutoff": config.cutoff.isoformat(),
            "evaluation_date": config.evaluation_date.isoformat(),
            "policy_id": POLICY_ID,
            "config_sha256": config.sha256,
            "evidence_sha256": _sha(_canonical(evidence)),
            "adapter_sha256": _sha(adapter.read_bytes()) if adapter.is_file() else None,
            "fetch_started_utc": started,
            "fetch_completed_utc": ended,
            "files": _inventory(run),
        }
        (run / "MANIFEST.json").write_bytes(_canonical(manifest))
        sums = "".join(
            f"{_sha(p.read_bytes())}  {p.relative_to(run).as_posix()}\n"
            for p in sorted(run.rglob("*"))
            if p.is_file() and p.name != "SHA256SUMS.txt"
        )
        (run / "SHA256SUMS.txt").write_text(sums, encoding="utf-8")
    except BaseException:
        shutil.rmtree(run, ignore_errors=True)
        raise
    return run


# ------------------------------------------------------------------ verify


def verify(run: Path) -> dict[str, Any]:
    """Stdlib-only re-derivation of every hash, the last rows and the publication states."""

    manifest = _json_file(run / "MANIFEST.json", "manifest")
    inventory = _inventory(run)
    if set(inventory) - set(manifest.get("files", {})):
        raise FrenchDriverError(
            f"unexpected file in run: {sorted(set(inventory) - set(manifest['files']))}"
        )
    for relative, pin in manifest["files"].items():
        path = run / relative
        if not path.is_file():
            raise FrenchDriverError(f"file missing from run: {relative}")
        if (
            _sha(path.read_bytes()) != pin["sha256"]
            or path.stat().st_size != pin["size_bytes"]
        ):
            raise FrenchDriverError(f"file differs from the manifest: {relative}")
    config = parse_config((run / "config.json").read_bytes())
    if config.sha256 != manifest["config_sha256"]:
        raise FrenchDriverError("config differs from the manifest")
    window = _json_file(run / "governance-evidence.json", "evidence")
    window_end = datetime.strptime(str(window["valid_until_utc"]), _TIME).replace(
        tzinfo=timezone.utc
    )
    facts = {
        feed.feed_key: _check_feed(run / "raw", feed, config.cutoff, window_end)
        for feed in config.feeds
    }
    log = json.loads((run / "publication-log.json").read_bytes())
    if log != json.loads(_canonical(_publication_log(config, facts))):
        raise FrenchDriverError("publication log differs from the staged bytes")
    block = json.loads((run / "public-sources.factor-feeds.json").read_bytes())
    for key, row in block["factor_feeds"].items():
        for part in ("source", "manifest"):
            path = run / "raw" / row[part]["path"]
            if _sha(path.read_bytes()) != row[part]["sha256"]:
                raise FrenchDriverError(f"builder block pin differs: {key}.{part}")
    sums_path = run / "SHA256SUMS.txt"
    if not sums_path.is_file():
        raise FrenchDriverError("SHA256SUMS.txt is missing")
    listed: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        digest, _, relative = line.partition("  ")
        listed[relative] = digest
    everything = {
        p.relative_to(run).as_posix()
        for p in run.rglob("*")
        if p.is_file() and p.name != "SHA256SUMS.txt"
    }
    if set(listed) != everything:
        raise FrenchDriverError("SHA256SUMS.txt does not list exactly the run files")
    for relative, digest in listed.items():
        if _sha((run / relative).read_bytes()) != digest:
            raise FrenchDriverError(f"SHA256SUMS differs: {relative}")
    return {
        "status": "VERIFIED",
        "run_id": manifest["run_id"],
        "feeds": len(config.feeds),
        "states": {row["feed_key"]: row["state"] for row in log["feeds"]},
        "non_compliant": [
            row["feed_key"] for row in log["feeds"] if row["state"] != "COMPLIANT"
        ],
    }


# --------------------------------------------------------------------- CLI


def _write_new(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("config-check")
    check.add_argument("--config", required=True, type=Path)
    evidence = sub.add_parser("make-evidence")
    evidence.add_argument("--config", required=True, type=Path)
    for name in ("run-id", "review", "valid-from-utc", "valid-until-utc", "out"):
        evidence.add_argument(
            f"--{name}", required=True, type=Path if name == "out" else str
        )
    evidence.add_argument("--max-requests", required=True, type=int)
    evidence.add_argument("--cutoff")
    run = sub.add_parser("fetch")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--stage-root", required=True, type=Path)
    run.add_argument("--run-id", required=True)
    run.add_argument("--governance", required=True, type=Path)
    run.add_argument("--cutoff")
    run.add_argument("--require-compliant", action="store_true")
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("run", type=Path)
    verify_parser.add_argument("--require-compliant", action="store_true")
    return parser


def _report(result: Mapping[str, Any], require_compliant: bool) -> int:
    print(json.dumps(result, sort_keys=True))
    stale = result["non_compliant"]
    if stale:
        print(
            f"NON_COMPLIANT feeds (advisory unless --require-compliant): {stale}",
            file=sys.stderr,
        )
        if require_compliant:
            return 3
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            return _report(verify(args.run), args.require_compliant)
        config = parse_config(args.config.read_bytes(), getattr(args, "cutoff", None))
        if args.command == "config-check":
            print(
                json.dumps({"config_sha256": config.sha256, "feeds": len(config.feeds)})
            )
        elif args.command == "make-evidence":
            document = make_evidence(
                config,
                run_id=args.run_id,
                review=args.review,
                valid_from=datetime.strptime(args.valid_from_utc, _TIME).replace(
                    tzinfo=timezone.utc
                ),
                valid_until=datetime.strptime(args.valid_until_utc, _TIME).replace(
                    tzinfo=timezone.utc
                ),
                max_requests=args.max_requests,
            )
            _write_new(args.out, _canonical(document))
            print(
                json.dumps(
                    {
                        "evidence_sha256": _sha(_canonical(document)),
                        "run_id": args.run_id,
                    }
                )
            )
        else:
            evidence_doc = json.loads(args.governance.read_bytes())
            run = fetch(
                config,
                stage_root=args.stage_root,
                run_id=args.run_id,
                evidence=evidence_doc,
                runner=runner,
                clock=clock,
            )
            return _report(verify(run), args.require_compliant)
    except (FrenchDriverError, OSError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
