"""The governed Kenneth French fetch: config, evidence, create-only staging, verify, 45-day rule.

Synthetic fixtures only: the runner stands in for the adapter and writes invented factor files.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Callable
import zipfile

import pytest

from tools import french_fetch_driver as ffd
from tools import portfolio_public_decomposition_sources as factors
from tools import release_public_bundles as bundles

BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp"
REVIEW = "https://github.com/example/repo/issues/1#issuecomment-2"
NOW = datetime(2026, 11, 21, 4, 0, 0, tzinfo=timezone.utc)
FEEDS = {
    "us_factors": "F-F_Research_Data_5_Factors_2x3_daily_CSV",
    "us_momentum": "F-F_Momentum_Factor_daily_CSV",
    "europe_factors": "Europe_5_Factors_Daily_CSV",
    "europe_momentum": "Europe_Mom_Factor_Daily_CSV",
    "us_industries": "49_Industry_Portfolios_daily_CSV",
}


def _config(**over: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "schema_version": 1,
        "cutoff": "2026-11-19",
        "evaluation_date": "2026-11-19",
        "policy_id": "KENNETH_FRENCH_MONTHLY_DAILY_45D_V1",
        "feeds": [{"feed_key": k, "feed_id": v} for k, v in FEEDS.items()],
        "limits": {"max_requests": 5},
    }
    config.update(over)
    return config


def _bytes(config: dict[str, Any]) -> bytes:
    return json.dumps(config, sort_keys=True).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_feed(
    cwd: Path,
    feed_id: str,
    *,
    last_period: str = "20260930",
    last_modified: str | None = "Mon, 26 Oct 2026 20:51:50 GMT",
    zip_tamper: bool = False,
    status: int = 200,
    sections: tuple[str, ...] | None = None,
) -> None:
    csv = f"period,F\n20260101,0.1\n{last_period},0.2\n".encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{feed_id}.csv", csv)
    zip_bytes = buffer.getvalue()
    parsed = json.dumps({"feed": feed_id, "rows": 2}).encode()
    manifest = {
        "adapter": {"id": "fake", "version": "0"},
        "artifact": {
            "name": f"{feed_id}.json",
            "sha256": _sha(parsed),
            "size_bytes": len(parsed),
        },
        "coverage": {
            "csv_member": f"{feed_id}.csv",
            "csv_member_sha256": _sha(csv),
            "sections": [
                {
                    "columns": ["period", "F"],
                    "first_period": "20260101",
                    "last_period": period,
                    "row_count": 2,
                    "title": f"Daily{index}",
                }
                for index, period in enumerate(sections or (last_period,))
            ],
        },
        "raw_artifact": {
            "name": f"{feed_id}.json.source.zip",
            "sha256": _sha(zip_bytes),
            "size_bytes": len(zip_bytes),
            "zip_members": [
                {"name": f"{feed_id}.csv", "sha256": _sha(csv), "size_bytes": len(csv)}
            ],
        },
        "request": {
            "feed_id": feed_id,
            "format": "parsed",
            "source_url": f"{BASE}/{feed_id}.zip",
        },
        "response": {
            "captured_at_utc": "2026-11-21T04:00:01.000000Z",
            "final_url": f"{BASE}/{feed_id}.zip",
            "last_modified": last_modified,
            "status": status,
        },
        "schema_version": 1,
    }
    if zip_tamper:
        zip_bytes += b"x"
    (cwd / f"{feed_id}.json").write_bytes(parsed)
    (cwd / f"{feed_id}.json.source.zip").write_bytes(zip_bytes)
    (cwd / f"{feed_id}.json.manifest.json").write_bytes(
        json.dumps(manifest, sort_keys=True).encode()
    )


def _runner(**per_feed: dict[str, Any]) -> Callable[[str, Path], None]:
    def run(feed_id: str, cwd: Path) -> None:
        _write_feed(cwd, feed_id, **per_feed.get(feed_id, {}))

    return run


def _evidence(
    config: ffd.Config,
    run_id: str = "R4-FRENCH",
    *,
    start: datetime = NOW - timedelta(minutes=2),
    hours: int = 4,
    max_requests: int = 5,
    review: str = REVIEW,
) -> dict[str, Any]:
    return ffd.make_evidence(
        config,
        run_id=run_id,
        review=review,
        valid_from=start,
        valid_until=start + timedelta(hours=hours),
        max_requests=max_requests,
    )


def _fetch(
    tmp_path: Path, runner: Callable[[str, Path], None] | None = None, **over: Any
) -> Path:
    config = ffd.parse_config(_bytes(_config(**over)))
    return ffd.fetch(
        config,
        stage_root=tmp_path / "stage",
        run_id="R4-FRENCH",
        evidence=_evidence(config),
        runner=runner or _runner(),
        clock=lambda: NOW,
    )


# ------------------------------------------------------------------ config


def test_config_is_deterministic_and_binds_the_five_builder_keys() -> None:
    first = ffd.parse_config(_bytes(_config()))
    assert first.sha256 == ffd.parse_config(_bytes(_config())).sha256
    assert first.sha256 != ffd.parse_config(_bytes(_config(cutoff="2026-11-13"))).sha256
    assert {feed.feed_key for feed in first.feeds} == set(factors.SOURCE_KEYS)
    assert ffd.parse_config(
        _bytes(_config()), cutoff_override="2026-11-13"
    ).cutoff == date(2026, 11, 13)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda c: c.update(extra=1), "unknown key"),
        (lambda c: c.update(policy_id="OTHER"), "policy_id"),
        (lambda c: c.update(cutoff="2026-13-01"), "cutoff"),
        (lambda c: c["feeds"].pop(), "feeds must be exactly"),
        (lambda c: c["feeds"][0].update(feed_id="../x_CSV"), "feed_id"),
        (
            lambda c: c["feeds"][0].update(
                feed_id="F-F_Research_Data_5_Factors_2x3_daily_TXT"
            ),
            "feed_id",
        ),
        (
            lambda c: c["feeds"][1].update(feed_key="us_factors"),
            "feeds must be exactly",
        ),
        (lambda c: c["limits"].update(max_requests=0), "max_requests"),
    ],
)
def test_config_refusals(
    mutation: Callable[[dict[str, Any]], None], message: str
) -> None:
    config = _config()
    mutation(config)
    with pytest.raises(ffd.FrenchDriverError, match=message):
        ffd.parse_config(_bytes(config))


# ------------------------------------------------------- the 45-day rule


@pytest.mark.parametrize(
    ("evaluation", "required", "last_period", "state"),
    [
        # report 2026-11-19: Sep 30 + 45 days = Nov 14 <= Nov 19, so September is required
        ("2026-11-19", (2026, 9), "20260930", "COMPLIANT"),
        ("2026-11-19", (2026, 9), "20260831", "HARD_STALE"),
        # Oct 31 + 45 days = Dec 15, due on that date itself (the builder's own boundary:
        # portfolio_public_decomposition_sources steps back while month end + 45 > evaluation)
        ("2026-12-14", (2026, 9), "20260930", "COMPLIANT"),
        ("2026-12-15", (2026, 10), "20260930", "HARD_STALE"),
        ("2026-12-16", (2026, 10), "20260930", "HARD_STALE"),
        ("2026-12-16", (2026, 10), "20261030", "COMPLIANT"),
        ("2026-12-16", (2026, 10), "20261031", "COMPLIANT"),
    ],
)
def test_the_45_day_publication_rule(
    evaluation: str, required: tuple[int, int], last_period: str, state: str
) -> None:
    assert ffd.required_month(date.fromisoformat(evaluation)) == required
    record = ffd.publication_state(last_period, date.fromisoformat(evaluation))
    assert record["state"] == state
    assert record["required_month"] == f"{required[0]}-{required[1]:02d}"


# ------------------------------------------------------------ evidence


def test_evidence_binds_config_run_window_and_request_cap() -> None:
    config = ffd.parse_config(_bytes(_config()))
    evidence = _evidence(config)
    ffd.check_evidence(evidence, config, "R4-FRENCH", NOW)
    cases: list[tuple[dict[str, Any], str, datetime]] = [
        (dict(evidence, config_sha256="0" * 64), "config", NOW),
        (dict(evidence, run_id="OTHER"), "run id", NOW),
        (evidence, "not yet valid", NOW - timedelta(hours=1)),
        (evidence, "expired", NOW + timedelta(hours=5)),
        (dict(evidence, max_requests=4), "max_requests", NOW),
        (dict(evidence, cutoff="2026-11-13"), "cutoff", NOW),
        (dict(evidence, extra=1), "keys", NOW),
    ]
    for document, message, now in cases:
        with pytest.raises(ffd.FrenchDriverError, match=message):
            ffd.check_evidence(document, config, "R4-FRENCH", now)


def test_the_review_reference_must_be_an_issue_comment_url() -> None:
    config = ffd.parse_config(_bytes(_config()))
    with pytest.raises(ffd.FrenchDriverError, match="review"):
        _evidence(config, review="https://example.com/x")


# --------------------------------------------------------------- fetch


def test_fetch_stages_create_only_files_a_manifest_and_the_builder_block(
    tmp_path: Path,
) -> None:
    run = _fetch(tmp_path)
    raw = run / "raw"
    for feed_id in FEEDS.values():
        assert sorted(p.name for p in raw.glob(f"{feed_id}*")) == [
            f"{feed_id}.json",
            f"{feed_id}.json.manifest.json",
            f"{feed_id}.json.source.zip",
        ]
    manifest = json.loads((run / "MANIFEST.json").read_text())
    assert manifest["config_sha256"] == ffd.parse_config(_bytes(_config())).sha256
    assert manifest["cutoff"] == "2026-11-19" and manifest["run_id"] == "R4-FRENCH"
    assert manifest["fetch_started_utc"] <= manifest["fetch_completed_utc"]
    sums = (run / "SHA256SUMS.txt").read_text()
    assert "MANIFEST.json" in sums and "raw/" in sums
    # the block is exactly what release_public_bundles consumes: its own pin check accepts it
    block = json.loads((run / "public-sources.factor-feeds.json").read_text())
    assert set(block) == {"schema_version", "acquisition_run", "factor_feeds", "styles"}
    assert block["schema_version"] == bundles.SOURCES_SCHEMA and block["styles"] == []
    assert set(block["factor_feeds"]) == set(factors.SOURCE_KEYS)
    for row in block["factor_feeds"].values():
        bundles._pinned(raw, row["source"], "source")
        bundles._pinned(raw, row["manifest"], "manifest")
    assert block["acquisition_run"]["evaluation_date"] == "2026-11-19"
    # the publication log records Last-Modified, last row, lag and the required month per feed
    log = json.loads((run / "publication-log.json").read_text())
    assert [row["feed_key"] for row in log["feeds"]] == list(FEEDS)
    first = log["feeds"][0]
    assert first["last_modified"].startswith("Mon, 26 Oct 2026")
    assert (
        first["last_period"] == "2026-09-30" and first["lag_days_after_month_end"] == 26
    )
    assert first["state"] == "COMPLIANT" and first["required_month"] == "2026-09"
    assert ffd.verify(run)["status"] == "VERIFIED"


def test_fetch_is_create_only(tmp_path: Path) -> None:
    _fetch(tmp_path)
    with pytest.raises(ffd.FrenchDriverError, match="exists"):
        _fetch(tmp_path)


@pytest.mark.parametrize(
    ("per_feed", "message"),
    [
        ({"zip_tamper": True}, "source zip"),
        ({"last_period": "20261120"}, "after the cutoff"),
        ({"last_modified": None}, "Last-Modified"),
        ({"last_modified": "not a date"}, "Last-Modified"),
        ({"status": 404}, "status"),
    ],
)
def test_fetch_refuses_bad_feeds_and_leaves_no_run(
    tmp_path: Path, per_feed: dict[str, Any], message: str
) -> None:
    runner = _runner(**{FEEDS["us_momentum"]: per_feed})
    with pytest.raises(ffd.FrenchDriverError, match=message):
        _fetch(tmp_path, runner)
    assert not (tmp_path / "stage" / "R4-FRENCH").exists()


def test_a_runner_that_writes_outside_staging_or_extra_files_is_refused(
    tmp_path: Path,
) -> None:
    def escaping(feed_id: str, cwd: Path) -> None:
        _write_feed(cwd, feed_id)
        (cwd / "extra.txt").write_text("x")

    with pytest.raises(ffd.FrenchDriverError, match="unexpected file"):
        _fetch(tmp_path, escaping)

    def missing(feed_id: str, cwd: Path) -> None:
        return None

    with pytest.raises(ffd.FrenchDriverError, match="missing"):
        _fetch(tmp_path / "b", missing)


def test_a_runner_failure_is_loud_and_leaves_no_run(tmp_path: Path) -> None:
    def failing(feed_id: str, cwd: Path) -> None:
        raise RuntimeError("adapter exited 1")

    with pytest.raises(ffd.FrenchDriverError, match="adapter"):
        _fetch(tmp_path, failing)
    assert not (tmp_path / "stage" / "R4-FRENCH").exists()


def test_the_wrong_source_url_is_refused(tmp_path: Path) -> None:
    def other_host(feed_id: str, cwd: Path) -> None:
        _write_feed(cwd, feed_id)
        path = cwd / f"{feed_id}.json.manifest.json"
        document = json.loads(path.read_text())
        document["response"]["final_url"] = f"https://example.com/{feed_id}.zip"
        path.write_text(json.dumps(document))

    with pytest.raises(ffd.FrenchDriverError, match="final_url"):
        _fetch(tmp_path, other_host)


# --------------------------------------------------------------- verify


def test_verify_rederives_everything_from_bytes(tmp_path: Path) -> None:
    run = _fetch(tmp_path)
    assert ffd.verify(run)["status"] == "VERIFIED"
    target = run / "raw" / f"{FEEDS['us_factors']}.json"
    original = target.read_bytes()
    target.write_bytes(original + b" ")
    with pytest.raises(ffd.FrenchDriverError, match="differs"):
        ffd.verify(run)
    target.write_bytes(original)
    log_path = run / "publication-log.json"
    log = json.loads(log_path.read_text())
    log["feeds"][0]["state"] = "HARD_STALE"
    log_path.write_text(json.dumps(log, indent=1, sort_keys=True))
    with pytest.raises(ffd.FrenchDriverError, match="differs|publication"):
        ffd.verify(run)


def test_verify_refuses_a_missing_or_extra_file(tmp_path: Path) -> None:
    run = _fetch(tmp_path)
    (run / "raw" / "stray.txt").write_text("x")
    with pytest.raises(ffd.FrenchDriverError, match="unexpected file"):
        ffd.verify(run)
    (run / "raw" / "stray.txt").unlink()
    (run / "raw" / f"{FEEDS['us_momentum']}.json.source.zip").unlink()
    with pytest.raises(ffd.FrenchDriverError, match="missing"):
        ffd.verify(run)


# ------------------------------------------------------------------ CLI


def test_cli_sequence_make_evidence_fetch_verify(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_bytes(_bytes(_config()))
    evidence = tmp_path / "evidence.json"
    base = ["--config", str(config_path)]
    assert (
        ffd.main(
            [
                "make-evidence",
                *base,
                "--run-id",
                "R4-FRENCH",
                "--review",
                REVIEW,
                "--valid-from-utc",
                "2026-11-21T03:58:00Z",
                "--valid-until-utc",
                "2026-11-21T08:00:00Z",
                "--max-requests",
                "5",
                "--out",
                str(evidence),
            ]
        )
        == 0
    )
    assert (
        ffd.main(
            [
                "fetch",
                *base,
                "--stage-root",
                str(tmp_path / "stage"),
                "--run-id",
                "R4-FRENCH",
                "--governance",
                str(evidence),
            ],
            runner=_runner(),
            clock=lambda: NOW,
        )
        == 0
    )
    assert ffd.main(["verify", str(tmp_path / "stage" / "R4-FRENCH")]) == 0
    assert "VERIFIED" in capsys.readouterr().out
    # a second evidence file is create-only
    assert (
        ffd.main(
            [
                "make-evidence",
                *base,
                "--run-id",
                "R4-FRENCH",
                "--review",
                REVIEW,
                "--valid-from-utc",
                "2026-11-21T03:58:00Z",
                "--valid-until-utc",
                "2026-11-21T08:00:00Z",
                "--max-requests",
                "5",
                "--out",
                str(evidence),
            ]
        )
        == 2
    )


def test_required_month_agrees_with_the_builders_own_rule_on_every_day_of_two_years() -> (
    None
):
    day = date(2025, 1, 1)
    while day < date(2027, 1, 1):
        builder = factors._required_observation_month_end(day)
        assert ffd.required_month(day) == (builder.year, builder.month), day
        day += timedelta(days=1)


def test_the_shipped_cycle_config_parses_and_names_the_five_builder_feeds() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "providers"
        / "french-fetch"
        / "r4-d.config.json"
    )
    config = ffd.parse_config(path.read_bytes())
    assert config.cutoff == date(2026, 11, 19)
    assert {feed.feed_key: feed.feed_id for feed in config.feeds} == FEEDS


# --------------------------------------------- review round 1 (P2/P3 fixes)


def _industries(*periods: str) -> Callable[[str, Path], None]:
    return _runner(**{FEEDS["us_industries"]: {"sections": periods}})


def test_freshness_uses_the_oldest_section_not_the_newest(tmp_path: Path) -> None:
    run = _fetch(tmp_path, _industries("20260930", "20260831"))
    row = next(
        r
        for r in json.loads((run / "publication-log.json").read_text())["feeds"]
        if r["feed_key"] == "us_industries"
    )
    assert row["last_period"] == "2026-08-31" and row["state"] == "HARD_STALE"
    assert ffd.verify(run)["non_compliant"] == ["us_industries"]


def test_staleness_is_loud_but_advisory_unless_require_compliant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_bytes(_bytes(_config()))
    evidence = tmp_path / "evidence.json"
    base = ["--config", str(config_path)]
    assert (
        ffd.main(
            [
                "make-evidence",
                *base,
                "--run-id",
                "R4-FRENCH",
                "--review",
                REVIEW,
                "--valid-from-utc",
                "2026-11-21T03:58:00Z",
                "--valid-until-utc",
                "2026-11-21T08:00:00Z",
                "--max-requests",
                "5",
                "--out",
                str(evidence),
            ]
        )
        == 0
    )
    fetch = [
        "fetch",
        *base,
        "--stage-root",
        str(tmp_path / "stage"),
        "--run-id",
        "R4-FRENCH",
        "--governance",
        str(evidence),
    ]
    assert ffd.main(fetch, runner=_industries("20260831"), clock=lambda: NOW) == 0
    out = capsys.readouterr()
    assert "us_industries" in out.out and "NON_COMPLIANT" in out.err
    run = str(tmp_path / "stage" / "R4-FRENCH")
    assert ffd.main(["verify", run]) == 0
    assert ffd.main(["verify", run, "--require-compliant"]) == 3
    # a compliant run passes the flag
    good = _fetch(tmp_path / "ok")
    assert ffd.main(["verify", str(good), "--require-compliant"]) == 0


def test_cutoff_must_not_be_after_the_evaluation_date_and_evaluation_not_after_the_window() -> (
    None
):
    with pytest.raises(ffd.FrenchDriverError, match="cutoff"):
        ffd.parse_config(_bytes(_config(cutoff="2026-11-20")))
    config = ffd.parse_config(_bytes(_config()))
    late = _evidence(config, start=NOW + timedelta(days=-30), hours=24)
    with pytest.raises(ffd.FrenchDriverError, match="evaluation"):
        ffd.check_evidence(
            late, config, "R4-FRENCH", NOW - timedelta(days=29, hours=23)
        )
    early = ffd.parse_config(_bytes(_config(evaluation_date="2026-11-25")))
    with pytest.raises(ffd.FrenchDriverError, match="evaluation"):
        ffd.check_evidence(_evidence(early), early, "R4-FRENCH", NOW)


@pytest.mark.parametrize(
    "last_modified",
    ["Tue, 29 Sep 2026 12:00:00 GMT", "Sun, 22 Nov 2026 12:00:00 GMT"],
)
def test_last_modified_before_the_month_end_or_after_the_window_is_refused(
    tmp_path: Path, last_modified: str
) -> None:
    runner = _runner(**{FEEDS["us_factors"]: {"last_modified": last_modified}})
    with pytest.raises(ffd.FrenchDriverError, match="Last-Modified"):
        _fetch(tmp_path, runner)


def test_verify_refuses_a_missing_empty_or_short_sha256sums(tmp_path: Path) -> None:
    run = _fetch(tmp_path)
    sums = run / "SHA256SUMS.txt"
    original = sums.read_text()
    sums.unlink()
    with pytest.raises(ffd.FrenchDriverError, match="SHA256SUMS"):
        ffd.verify(run)
    sums.write_text("")
    with pytest.raises(ffd.FrenchDriverError, match="SHA256SUMS"):
        ffd.verify(run)
    sums.write_text("".join(original.splitlines(keepends=True)[1:]))
    with pytest.raises(ffd.FrenchDriverError, match="SHA256SUMS"):
        ffd.verify(run)
    sums.write_text(original)
    assert ffd.verify(run)["status"] == "VERIFIED"


def test_the_docs_state_what_verify_checks_and_that_staleness_is_advisory() -> None:
    doc = (
        Path(__file__).resolve().parents[2] / "docs" / "providers" / "french-fetch.md"
    ).read_text()
    for phrase in (
        "advisory",
        "WP2",
        "--require-compliant",
        "manifest-declared",
        "declarative",
    ):
        assert phrase in doc, phrase
    assert "manifest-declared" in str(ffd.__doc__)
