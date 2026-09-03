"""Offline tests for grizzly_capture.

All page/DOM objects are fakes; no browser, no network, no playwright import,
and only synthetic data. The tests are pytest-compatible but use no pytest
fixtures so they can also run under a plain stdlib harness.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from grizzly_capture import (
    CredentialScanError,
    LoginRequiredError,
    RunDirExistsError,
)
from grizzly_capture.cli import (
    EXIT_OK,
    EXIT_ROUTES_FAILED,
    run_capture,
)
from grizzly_capture.manifest import (
    STATUS_NON_TERMINAL,
    STATUS_TERMINAL,
    CaptureManifest,
)
from grizzly_capture.pagination import (
    _CLICK_CLICKABLE_SCRIPT,
    _CLICK_POSITIONS_TAB_SCRIPT,
    _LIST_CLICKABLES_SCRIPT,
    _STATE_EXTRACTION_SCRIPT,
    _SWITCH_TO_TABLE_VIEW_SCRIPT,
    _TABLES_SNAPSHOT_SCRIPT,
    ClickableDescriptor,
    map_state_trade,
)
from grizzly_capture.persistence import (
    TRADES_CSV_HEADER,
    create_run_dir,
)
from grizzly_capture.session import AUTHENTICATED_MARKER_SELECTORS

BASE_URL = "https://grizzlybulls.test"
MODEL_URL = f"{BASE_URL}/models/vix-ta-macro-advanced"

SYNTHETIC_CSV = (
    "Date (ET),Action,SPX,Prev Price,P/L,PnL %,Model,Buy & Hold\r\n"
    "2026-04-06,SELL,5001.0,4990.0,11.0,0.22,synth-model,0.10\r\n"
    "2026-04-08,BUY,5010.0,5001.0,9.0,0.18,synth-model,0.12\r\n"
).encode("utf-8")

SYNTHETIC_STATE_TRADES = [
    {
        "date": "2026-04-06",
        "action": "SELL",
        "spx": "5001.0",
        "prev_price": "4990.0",
        "pnl": "11.0",
        "pnl_pct": "0.22",
        "model": "synth-model",
        "buy_and_hold": "0.10",
    },
    {
        "date": "2026-04-08",
        "action": "BUY",
        "spx": "5010.0",
        "prev_price": "5001.0",
        "pnl": "9.0",
        "pnl_pct": "0.18",
        "model": "synth-model",
        "buy_and_hold": "0.12",
    },
]

SYNTHETIC_POSITIONS_TABLE = {
    "header": ["Symbol", "Qty", "Price"],
    "rows": [["SPX synth", "1", "5001.0"]],
}


class FakeCDPSession:
    def __init__(self, page: "FakePage") -> None:
        self._page = page

    def send(self, method: str, params: dict) -> None:
        self._page.cdp_calls.append((method, params))
        if method == "Browser.setDownloadBehavior":
            self._page.download_dir = Path(params["downloadPath"])


class FakeContext:
    def __init__(self, page: "FakePage") -> None:
        self._page = page

    def new_cdp_session(self, page: "FakePage") -> FakeCDPSession:
        return FakeCDPSession(page)


class FakePage:
    """Duck-typed stand-in for a playwright Page; pure-python, offline."""

    def __init__(
        self,
        *,
        authenticated: bool = True,
        clickables: list[ClickableDescriptor] | None = None,
        state_trades: list[dict] | None = None,
        tables: list[dict] | None = None,
        download_bytes: bytes | None = None,
    ) -> None:
        self.authenticated = authenticated
        self.url = MODEL_URL
        self.clickables = clickables or []
        self.state_trades = state_trades or []
        self.tables = tables or []
        self.download_bytes = download_bytes
        self.cdp_calls: list[tuple[str, dict]] = []
        self.download_dir: Path | None = None
        self.context = FakeContext(self)
        self.navigated: list[str] = []

    def goto(self, url: str, **kwargs: object) -> None:
        self.navigated.append(url)

    def wait_for_load_state(self, state: str, **kwargs: object) -> None:
        return None

    def query_selector(self, selector: str) -> object | None:
        if self.authenticated and selector in AUTHENTICATED_MARKER_SELECTORS:
            return object()
        return None

    def title(self) -> str:
        return "Synthetic Model Page"

    def evaluate(self, script: str, arg: object = None) -> object:
        if script == _LIST_CLICKABLES_SCRIPT:
            return [asdict(item) for item in self.clickables]
        if script == _CLICK_CLICKABLE_SCRIPT:
            self._click(int(arg))
            return True
        if script == _SWITCH_TO_TABLE_VIEW_SCRIPT:
            return "clicked"
        if script == _CLICK_POSITIONS_TAB_SCRIPT:
            return "clicked"
        if script == _STATE_EXTRACTION_SCRIPT:
            return self.state_trades
        if script == _TABLES_SNAPSHOT_SCRIPT:
            return self.tables
        raise AssertionError("unexpected page-side script")

    def _click(self, index: int) -> None:
        if self.download_bytes is not None and self.download_dir is not None:
            target = self.download_dir / "synthetic-trades.csv"
            target.write_bytes(self.download_bytes)


def assert_raises(error_type: type[BaseException], func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except error_type as exc:
        return exc
    raise AssertionError(f"expected {error_type.__name__}")


def csv_control() -> ClickableDescriptor:
    return ClickableDescriptor(
        index=0,
        tag="button",
        text="Download CSV",
        aria="",
        title="",
        svg_count=1,
        near_table=True,
        visible=True,
        disabled=False,
    )


def read_manifest(run_dir: Path) -> dict:
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def test_playwright_never_imported() -> None:
    assert "playwright" not in sys.modules
    assert "playwright.sync_api" not in sys.modules


def test_login_required_fails_closed_and_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "run"
        page = FakePage(authenticated=False)
        assert_raises(
            LoginRequiredError,
            run_capture,
            page,
            run_dir=run_dir,
            model_slug="vix-ta-macro-advanced",
            base_url=BASE_URL,
        )
        assert not run_dir.exists()
        assert page.navigated == [MODEL_URL]


def test_download_route_happy_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "run"
        page = FakePage(
            clickables=[csv_control()],
            download_bytes=SYNTHETIC_CSV,
            tables=[SYNTHETIC_POSITIONS_TABLE],
        )
        code = run_capture(
            page,
            run_dir=run_dir,
            model_slug="vix-ta-macro-advanced",
            base_url=BASE_URL,
            download_timeout_s=5.0,
        )
        assert code == EXIT_OK
        trades = run_dir / "trades.csv"
        assert trades.read_bytes() == SYNTHETIC_CSV
        positions = run_dir / "positions.csv"
        assert positions.exists()
        manifest = read_manifest(run_dir)
        assert manifest["status"] == STATUS_TERMINAL
        assert manifest["route_used"] == "download"
        assert manifest["counts"]["trades_rows"] == 2
        assert manifest["counts"]["positions_rows"] == 1
        by_path = {item["path"]: item for item in manifest["artifacts"]}
        trades_entry = by_path["trades.csv"]
        assert trades_entry["source"] == "download"
        assert trades_entry["sha256"] == hashlib.sha256(SYNTHETIC_CSV).hexdigest()
        assert trades_entry["size_bytes"] == len(SYNTHETIC_CSV)
        assert by_path["positions.csv"]["source"] == "dom-table"
        # The raw download is folded into the canonical artifact.
        assert list((run_dir / "downloads").iterdir()) == []
        # CDP download behavior targeted the run dir downloads folder.
        methods = [method for method, _ in page.cdp_calls]
        assert "Browser.setDownloadBehavior" in methods


def test_state_route_serialization_matches_header() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "run"
        page = FakePage(
            state_trades=SYNTHETIC_STATE_TRADES,
            tables=[SYNTHETIC_POSITIONS_TABLE],
        )
        code = run_capture(
            page,
            run_dir=run_dir,
            model_slug="vix-ta-macro-advanced",
            base_url=BASE_URL,
            route="state",
        )
        assert code == EXIT_OK
        text = (run_dir / "trades.csv").read_text(encoding="utf-8")
        header_line = text.splitlines()[0]
        assert header_line == ",".join(TRADES_CSV_HEADER)
        rows = text.splitlines()[1:]
        assert len(rows) == 2
        assert rows[0].startswith("2026-04-06,SELL,5001.0,4990.0,11.0,0.22")
        manifest = read_manifest(run_dir)
        assert manifest["route_used"] == "state"
        by_path = {item["path"]: item for item in manifest["artifacts"]}
        assert by_path["trades.csv"]["source"] == "state"
        assert any("state-derived" in note for note in manifest["notes"])


def test_state_key_alias_normalization() -> None:
    row = map_state_trade(
        {
            "Date (ET)": "2026-04-06",
            "ACTION": "SELL",
            "spxPrice": "5001.0",
            "previousPrice": "4990.0",
            "P/L": "11.0",
            "pnlPercent": "0.22",
            "modelName": "synth-model",
            "buy&hold": "0.10",
        },
    )
    assert row == [
        "2026-04-06",
        "SELL",
        "5001.0",
        "4990.0",
        "11.0",
        "0.22",
        "synth-model",
        "0.10",
    ]


def test_manifest_credential_scan_rejects_bearer() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = create_run_dir(Path(tmp) / "run")
        manifest = CaptureManifest(
            model_slug="synth",
            base_url="https://grizzlybulls.test",
            cdp_port=9223,
            route_requested="auto",
        )
        # Synthetic bearer-shaped string built at runtime, matching the
        # repo's existing convention for credential-scanner fixtures.
        leaked = "".join(("Authorization: Bearer ", "A" * 24))
        manifest.notes.append(f"leaked {leaked}")
        assert_raises(CredentialScanError, manifest.finalize, run_dir)
        assert not (run_dir / "manifest.json").exists()


def test_manifest_credential_scan_ignores_hashes() -> None:
    payload = CaptureManifest(
        model_slug="synth",
        base_url="https://grizzlybulls.test",
        cdp_port=9223,
        route_requested="auto",
    ).to_dict()
    payload["artifacts"] = [
        {
            "path": "trades.csv",
            "sha256": hashlib.sha256(b"synthetic").hexdigest(),
            "size_bytes": 9,
            "rows": 1,
            "source": "download",
        },
    ]
    from grizzly_capture.manifest import scan_manifest_fields

    assert scan_manifest_fields(payload) == []


def test_create_only_run_dir_enforced() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "run"
        create_run_dir(run_dir)
        assert (run_dir / "downloads").is_dir()
        assert_raises(RunDirExistsError, create_run_dir, run_dir)


def test_double_route_failure_is_non_terminal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "run"
        page = FakePage()  # no csv control, no state trades
        code = run_capture(
            page,
            run_dir=run_dir,
            model_slug="vix-ta-macro-advanced",
            base_url=BASE_URL,
            download_timeout_s=5.0,
        )
        assert code == EXIT_ROUTES_FAILED
        assert not (run_dir / "trades.csv").exists()
        assert not (run_dir / "positions.csv").exists()
        evidence = run_dir / "session-evidence.json"
        assert evidence.exists()
        payload = json.loads(evidence.read_text(encoding="utf-8"))
        assert payload["authenticated"] is True
        assert payload["page_url"] == MODEL_URL
        manifest = read_manifest(run_dir)
        assert manifest["status"] == STATUS_NON_TERMINAL
        assert manifest["route_used"] is None
        assert [item["outcome"] for item in manifest["attempts"]] == [
            "failed",
            "failed",
        ]
        assert len(manifest["artifacts"]) == 1
        assert manifest["artifacts"][0]["source"] == "session-evidence"
