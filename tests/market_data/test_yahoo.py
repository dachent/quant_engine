"""Offline tests for the single canonical Yahoo market-data adapter."""

from __future__ import annotations

from argparse import Namespace
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pandas as pd
import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPOSITORY_ROOT
    / "skills"
    / "agent-market-data-terminal"
    / "scripts"
    / "fetch_yfinance.py"
)
SPEC = importlib.util.spec_from_file_location("quant_engine_yahoo_adapter", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("unable to load canonical Yahoo adapter")
yahoo = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = yahoo
SPEC.loader.exec_module(yahoo)


def _args(dataset: str = "history_adjusted") -> Namespace:
    return Namespace(
        dataset=dataset,
        tickers=["AAPL", "MSFT", "AAPL"],
        start="2020-01-01",
        end="2020-01-03",
        interval="1d",
        prepost=False,
        batch_size=2,
        threads=1,
        timeout_seconds=30,
        retry_attempts=1,
        retry_backoff_seconds=0.0,
        resolve_names=False,
        search_count=5,
        cache_dir=None,
        format="parsed",
        output="outputs/example.json",
    )


def _history() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Ticker": "AAPL",
                "Date": pd.Timestamp("2020-01-02"),
                "Open": 100.0,
                "High": 110.0,
                "Low": 95.0,
                "Close": 105.0,
                "Adj Close": 84.0,
                "Volume": 1000,
                "Dividends": 0.0,
                "Stock Splits": 0.0,
            },
            {
                "Ticker": "MSFT",
                "Date": pd.Timestamp("2020-01-02"),
                "Open": 50.0,
                "High": 55.0,
                "Low": 49.0,
                "Close": 50.0,
                "Adj Close": 50.0,
                "Volume": 500,
                "Dividends": 0.0,
                "Stock Splits": 0.0,
            },
        ]
    )


def test_dataset_contract_and_argument_validation() -> None:
    assert {
        "history",
        "history_yf",
        "history_raw",
        "history_adjusted",
        "history_summary",
        "ticker_search",
        "current_snapshot",
    } == set(yahoo.DATASET_CHOICES)
    assert yahoo.validate_args(_args()) == ["AAPL", "MSFT"]

    invalid = _args()
    invalid.start = None
    with pytest.raises(ValueError, match="start"):
        yahoo.validate_args(invalid)


def test_workbook_lineage_adjustment_and_summary() -> None:
    history_yf = yahoo.build_history_yf_frame(
        _history(), {"AAPL": "Apple Inc.", "MSFT": "Microsoft"}
    )
    raw = yahoo.build_history_raw_frame(history_yf)
    adjusted = yahoo.build_history_adjusted_frame(raw)
    summary = yahoo.build_history_summary_frame(raw)

    aapl = adjusted.loc[adjusted["Ticker"] == "AAPL"].iloc[0]
    assert aapl["Ticker.Name"] == "Apple Inc."
    assert aapl["Open"] == pytest.approx(80.0)
    assert aapl["High"] == pytest.approx(88.0)
    assert aapl["Low"] == pytest.approx(76.0)
    assert aapl["Close"] == pytest.approx(84.0)
    assert set(summary["Observations"]) == {1}
    assert list(summary["Ticker"]) == ["AAPL", "MSFT"]


def test_search_rows_are_deterministic_and_nested_values_are_serialized() -> None:
    frame = yahoo.build_ticker_search_frame(
        {
            "MSFT": [{"symbol": "MSFT", "nested": {"rank": 2}}],
            "AAPL": [{"symbol": "AAPL", "nested": {"rank": 1}}],
        }
    )
    assert list(frame["RequestedTicker"]) == ["AAPL", "MSFT"]
    assert frame.loc[0, "nested"] == '{"rank": 1}'


def test_manifest_is_hash_bound_and_deterministic(tmp_path: Path) -> None:
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text('{"synthetic":true}\n', encoding="utf-8")
    request = yahoo.build_request_payload(_args(), ["AAPL", "MSFT"])
    coverage = yahoo.build_coverage(_history(), ["AAPL", "MSFT", "MISSING"])

    first_path = yahoo.write_run_manifest(artifact_path, request, coverage)
    first = first_path.read_bytes()
    second_path = yahoo.write_run_manifest(artifact_path, request, coverage)

    assert first == second_path.read_bytes()
    manifest = json.loads(first)
    assert (
        manifest["artifact"]["sha256"] == sha256(artifact_path.read_bytes()).hexdigest()
    )
    assert manifest["coverage"]["claim"] == "PARTIAL"
    assert manifest["coverage"]["missing_tickers"] == ["MISSING"]
    assert manifest["source_run_id"] == yahoo.request_id(request)


def test_output_and_cache_paths_must_stay_in_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert yahoo.resolve_output_path("outputs/result.json", workspace).is_relative_to(
        workspace
    )
    assert yahoo.resolve_cache_dir("cache", workspace).is_relative_to(workspace)
    with pytest.raises(ValueError, match="inside"):
        yahoo.resolve_output_path(str(tmp_path / "outside.json"), workspace)
    with pytest.raises(ValueError, match="inside"):
        yahoo.resolve_cache_dir(str(tmp_path / "outside-cache"), workspace)


def test_all_absorbed_source_histories_are_ancestors_and_manifested() -> None:
    manifest = json.loads(
        (
            REPOSITORY_ROOT
            / "docs"
            / "providers"
            / "market-data-terminal-source-manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["governing_issue"] == 107
    for source in manifest["sources"]:
        commit = source["commit"]
        subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=REPOSITORY_ROOT,
            check=True,
        )
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
        )
        if "sanitized_head_tree" in source:
            tree = subprocess.check_output(
                ["git", "rev-parse", f"{commit}^{{tree}}"],
                cwd=REPOSITORY_ROOT,
                text=True,
            ).strip()
            assert tree == source["source_head_tree"]
            assert tree == source["sanitized_head_tree"]
            history_paths = subprocess.check_output(
                ["git", "log", "--format=", "--name-only", commit],
                cwd=REPOSITORY_ROOT,
                text=True,
            ).splitlines()
            assert not any(path.startswith(".vendor/") for path in history_paths)
        assert (REPOSITORY_ROOT / source["retained_path"]).exists()


def test_no_duplicate_yahoo_runtime_survives() -> None:
    implementations = [
        path
        for directory in ("apps", "imports", "packages", "skills", "tools")
        for path in (REPOSITORY_ROOT / directory).rglob("fetch_yfinance.py")
    ]
    assert implementations == [SCRIPT_PATH]
    legacy_import = REPOSITORY_ROOT / "imports" / "yf-marketdata"
    assert not any(legacy_import.rglob("*.py"))


class _FakeYahoo:
    def __init__(self, responses: list[pd.DataFrame | Exception]):
        self.responses = list(responses)
        self.calls: list[tuple[object, ...]] = []
        self.cache = SimpleNamespace(set_cache_location=self._set_cache_location)
        self.cache_location: str | None = None

    def _set_cache_location(self, cache_dir: str) -> None:
        self.cache_location = cache_dir

    def download(self, **kwargs):
        self.calls.append(
            tuple(
                kwargs["tickers"]
                if isinstance(kwargs["tickers"], list)
                else [kwargs["tickers"]]
            )
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        requested = (
            kwargs["tickers"]
            if isinstance(kwargs["tickers"], list)
            else [kwargs["tickers"]]
        )
        frames = {}
        for ticker in requested:
            subset = (
                response.loc[response["Ticker"] == ticker]
                .drop(columns=["Ticker"])
                .set_index("Date")
            )
            frames[ticker] = subset
        if len(requested) == 1:
            return frames[requested[0]]
        return pd.concat(frames, axis=1)


def _fake_history(*rows: tuple[str, str, object]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Ticker": ticker,
                "Date": pd.Timestamp(day),
                "Open": price,
                "High": price,
                "Low": price,
                "Close": price,
                "Adj Close": price,
                "Volume": 1,
                "Dividends": 0.0,
                "Stock Splits": 0.0,
            }
            for ticker, day, price in rows
        ]
    )


def test_history_fetch_initializes_library_cache_and_uses_resolved_date_key(
    tmp_path: Path,
) -> None:
    fake = _FakeYahoo([_fake_history(("AAPL", "2020-01-02", 105.0))])
    args = _args()
    args.cache_dir = "cache"
    cache_dir = yahoo.resolve_cache_dir(args.cache_dir, tmp_path)

    frame = yahoo.fetch_history_frame(["AAPL"], args, cache_dir, yf=fake)

    assert not frame.empty
    assert fake.cache_location == str(cache_dir)
    token_args = Namespace(**{**vars(args), "start": "today", "end": "today"})
    assert (
        yahoo.history_cache_path(cache_dir, ["AAPL"], token_args).name
        == yahoo.history_cache_path(
            cache_dir,
            ["AAPL"],
            Namespace(
                **{
                    **vars(token_args),
                    "start": yahoo.parse_date_token("today", "--start").isoformat(),
                    "end": yahoo.parse_date_token("today", "--end").isoformat(),
                }
            ),
        ).name
    )


def test_history_fetch_falls_back_for_unusable_partial_batch(tmp_path: Path) -> None:
    fake = _FakeYahoo(
        [
            _fake_history(
                ("AAPL", "2020-01-02", 105.0),
                ("MSFT", "2020-01-02", None),
            ),
            _fake_history(("MSFT", "2020-01-02", 205.0)),
        ]
    )
    args = _args()
    frame = yahoo.fetch_history_frame(
        ["AAPL", "MSFT"], args, tmp_path / "cache", yf=fake
    )

    assert set(frame["Ticker"]) == {"AAPL", "MSFT"}
    assert fake.calls == [("AAPL", "MSFT"), ("MSFT",)]


def test_fallback_success_reuses_selected_cache_without_provider_calls(
    tmp_path: Path,
) -> None:
    fake = _FakeYahoo(
        [
            _fake_history(
                ("AAPL", "2020-01-02", 105.0),
                ("MSFT", "2020-01-02", None),
            ),
            _fake_history(("MSFT", "2020-01-02", 205.0)),
        ]
    )
    args = _args()
    cache_dir = tmp_path / "cache"
    first = yahoo.fetch_history_frame(["AAPL", "MSFT"], args, cache_dir, yf=fake)
    calls_after_first = list(fake.calls)

    fake.download = lambda **kwargs: pytest.fail("provider called on cache hit")
    second = yahoo.fetch_history_frame(["AAPL", "MSFT"], args, cache_dir, yf=fake)

    pd.testing.assert_frame_equal(first, second)
    assert fake.calls == calls_after_first


@pytest.mark.parametrize(
    "response, match",
    [
        (
            _fake_history(("AAPL", "2020-01-02", 105.0)).assign(Date=["bad"]),
            "invalid required date",
        ),
        (
            _fake_history(("AAPL", "2020-01-02", 105.0)).assign(
                **{"Adj Close": [None]}
            ),
            "invalid in-window price",
        ),
        (pd.DataFrame(), "missing required ticker"),
    ],
)
def test_failed_history_retains_rejected_attempt_receipt(
    tmp_path: Path, response: pd.DataFrame, match: str
) -> None:
    fake = _FakeYahoo([response, response])
    args = _args("history_adjusted")

    with pytest.raises(ValueError, match=match):
        yahoo.fetch_history_frame(["AAPL"], args, tmp_path / "cache", yf=fake)

    receipts = list((tmp_path / "cache").glob("history_failure_*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["reason"]
    assert receipt["source_raw_artifact"]["sha256"]
    assert not list((tmp_path / "cache").glob("history_*.csv"))


@pytest.mark.parametrize(
    "response, match",
    [
        (pd.DataFrame(), "missing required ticker"),
        (_fake_history(("AAPL", "2020-01-02", 0)), "invalid in-window price"),
    ],
)
def test_history_fetch_rejects_allfail_or_invalid_required_data(
    tmp_path: Path, response: pd.DataFrame, match: str
) -> None:
    fake = _FakeYahoo([response, response])
    args = _args()

    with pytest.raises((ValueError, RuntimeError), match=match):
        yahoo.fetch_history_frame(["AAPL"], args, tmp_path / "cache", yf=fake)
    assert not list((tmp_path / "cache").glob("*.csv"))


def test_invalid_cached_history_is_refetched_and_valid_cache_is_reused(
    tmp_path: Path,
) -> None:
    args = _args()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_path = yahoo.history_cache_path(cache_dir, ["AAPL", "MSFT"], args)
    assert cache_path is not None
    _fake_history(("AAPL", "2020-01-02", 105.0)).to_csv(cache_path, index=False)

    fake = _FakeYahoo(
        [
            _fake_history(
                ("AAPL", "2020-01-02", 105.0),
                ("MSFT", "2020-01-02", 205.0),
            )
        ]
    )
    frame = yahoo.fetch_history_frame(["AAPL", "MSFT"], args, cache_dir, yf=fake)
    assert set(frame["Ticker"]) == {"AAPL", "MSFT"}
    assert fake.calls == [("AAPL", "MSFT")]

    fake.responses = [_fake_history(("SHOULD_NOT", "2020-01-02", 1.0))]
    reused = yahoo.fetch_history_frame(["AAPL", "MSFT"], args, cache_dir, yf=fake)
    assert set(reused["Ticker"]) == {"AAPL", "MSFT"}
    assert fake.calls == [("AAPL", "MSFT")]


def test_end_exclusive_filter_keeps_valid_source_calendar_rows(tmp_path: Path) -> None:
    fake = _FakeYahoo(
        [
            _fake_history(
                ("AAPL", "2020-01-02", 105.0),
                ("AAPL", "2020-01-03", 106.0),
            )
        ]
    )
    args = _args()
    frame = yahoo.fetch_history_frame(["AAPL"], args, tmp_path / "cache", yf=fake)

    assert list(frame["Date"].dt.strftime("%Y-%m-%d")) == ["2020-01-02"]


def test_history_adjusted_rejects_invalid_consumed_adjusted_price(
    tmp_path: Path,
) -> None:
    fake = _FakeYahoo(
        [_fake_history(("AAPL", "2020-01-02", 105.0)).assign(**{"Adj Close": [None]})]
    )
    args = _args("history_adjusted")

    with pytest.raises(ValueError, match="invalid in-window price"):
        yahoo.fetch_history_frame(["AAPL"], args, tmp_path / "cache", yf=fake)


def test_invalid_adjusted_cache_is_refetched_and_valid_adjusted_cache_reused(
    tmp_path: Path,
) -> None:
    args = _args("history_adjusted")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_path = yahoo.history_cache_path(cache_dir, ["AAPL"], args)
    assert cache_path is not None
    invalid = _fake_history(("AAPL", "2020-01-02", 105.0)).assign(
        **{"Adj Close": [None]}
    )
    invalid.to_csv(cache_path, index=False)

    valid = _fake_history(("AAPL", "2020-01-02", 105.0))
    fake = _FakeYahoo([valid])
    frame = yahoo.fetch_history_frame(["AAPL"], args, cache_dir, yf=fake)

    assert frame.loc[0, "Adj Close"] == pytest.approx(105.0)
    assert fake.calls == [("AAPL",)]

    fake.responses = [_fake_history(("SHOULD_NOT", "2020-01-02", 1.0))]
    yahoo.fetch_history_frame(["AAPL"], args, cache_dir, yf=fake)
    assert fake.calls == [("AAPL",)]


def test_history_fetch_rejects_missing_cache_dir_before_provider_call() -> None:
    fake = _FakeYahoo([])
    args = _args()

    with pytest.raises(ValueError, match="--cache-dir"):
        yahoo.fetch_history_frame(["AAPL"], args, None, yf=fake)
    assert fake.calls == []


def test_history_fetch_rejects_unparseable_required_date(tmp_path: Path) -> None:
    fake = _FakeYahoo(
        [_fake_history(("AAPL", "2020-01-02", 105.0)).assign(Date=["bad"])]
    )
    args = _args()

    with pytest.raises(ValueError, match="invalid required date"):
        yahoo.fetch_history_frame(["AAPL"], args, tmp_path / "cache", yf=fake)


def test_history_manifest_evidence_retains_raw_rows_and_exclusions(
    tmp_path: Path,
) -> None:
    fake = _FakeYahoo(
        [
            _fake_history(
                ("AAPL", "2019-12-31", 104.0),
                ("AAPL", "2020-01-02", 105.0),
                ("AAPL", "2020-01-03", 106.0),
            )
        ]
    )
    args = _args()
    frame = yahoo.fetch_history_frame(["AAPL"], args, tmp_path / "cache", yf=fake)
    coverage = yahoo.build_coverage(frame, ["AAPL"])

    evidence = coverage["source_raw_artifact"]
    assert evidence["row_count"] == 3
    assert evidence["sha256"]
    assert [item["reason"] for item in coverage["exclusions"]] == [
        "out_of_window_before_start",
        "out_of_window_end_exclusive",
    ]


def test_cached_history_retains_fresh_raw_exclusions_without_provider_calls(
    tmp_path: Path,
) -> None:
    fake = _FakeYahoo(
        [
            _fake_history(
                ("AAPL", "2019-12-31", 104.0),
                ("AAPL", "2020-01-02", 105.0),
                ("AAPL", "2020-01-03", 106.0),
            )
        ]
    )
    args = _args()
    cache_dir = tmp_path / "cache"
    fresh = yahoo.fetch_history_frame(["AAPL"], args, cache_dir, yf=fake)
    fresh_coverage = yahoo.build_coverage(fresh, ["AAPL"])

    fake.download = lambda **kwargs: pytest.fail("provider called on cache hit")
    cached = yahoo.fetch_history_frame(["AAPL"], args, cache_dir, yf=fake)
    cached_coverage = yahoo.build_coverage(cached, ["AAPL"])

    assert (
        cached_coverage["source_raw_artifact"] == fresh_coverage["source_raw_artifact"]
    )
    assert cached_coverage["exclusions"] == fresh_coverage["exclusions"]


def test_distinct_failed_attempts_retain_content_addressed_receipts(
    tmp_path: Path,
) -> None:
    args = _args("history_adjusted")
    cache_dir = tmp_path / "cache"

    first = _FakeYahoo(
        [_fake_history(("AAPL", "2020-01-02", 105.0)).assign(Date=["bad"])]
    )
    with pytest.raises(ValueError, match="invalid required date"):
        yahoo.fetch_history_frame(["AAPL"], args, cache_dir, yf=first)

    second = _FakeYahoo(
        [_fake_history(("AAPL", "2020-01-02", 105.0)).assign(**{"Adj Close": [None]})]
    )
    with pytest.raises(ValueError, match="invalid in-window price"):
        yahoo.fetch_history_frame(["AAPL"], args, cache_dir, yf=second)

    receipts = sorted(cache_dir.glob("history_failure_*.json"))
    assert len(receipts) == 2
    reasons = {
        json.loads(path.read_text(encoding="utf-8"))["reason"] for path in receipts
    }
    assert reasons == {"invalid required date: AAPL", "invalid in-window price: AAPL"}
