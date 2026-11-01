"""#540 browser-load harness: verdict arithmetic and an end-to-end run on a stub page."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

from browser_runtime import resolve_browser_runtime
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPOSITORY_ROOT / "apps" / "portfolio-control-panel" / "measure_load.mjs"

_UNIT = """
import { summarize, verdict } from %(harness)s;
const run = (ms) => ({ usable_load_ms: ms });
const cand = summarize([run(2000), run(2200), run(2400)]);
const ctl = summarize([run(2000), run(2000), run(2000)]);
process.stdout.write(JSON.stringify({
  cand,
  ok: verdict(cand, ctl, 1.25),
  bad: verdict(cand, ctl, 1.05),
  none: verdict(cand, null, 1.25),
}));
"""

_STUB = '<!doctype html><div id="app" data-app-state="ready"></div>'


def test_summary_and_verdict_compare_candidate_to_control() -> None:
    runtime = resolve_browser_runtime(REPOSITORY_ROOT)
    if runtime is None:
        pytest.skip("node is unavailable")
    source = _UNIT % {"harness": json.dumps(HARNESS.as_uri())}
    done = subprocess.run(
        [runtime.node, "--input-type=module", "-e", source],
        check=True,
        capture_output=True,
        text=True,
    )
    out = json.loads(done.stdout)
    assert out["cand"]["mean_ms"] == 2200
    assert out["ok"] == {"ratio": 1.1, "max_ratio": 1.25, "pass": True}
    assert out["bad"]["pass"] is False
    assert out["none"] == {"ratio": None, "pass": None}


def test_harness_measures_a_stub_panel_with_a_control(tmp_path: Path) -> None:
    runtime = resolve_browser_runtime(REPOSITORY_ROOT)
    if runtime is None:
        pytest.skip("browser runtime is unavailable")
    page = tmp_path / "index.html"
    page.write_bytes(_STUB.encode("utf-8"))
    out = tmp_path / "result.json"
    done = subprocess.run(
        [
            runtime.node,
            str(HARNESS),
            "--html",
            str(page),
            "--control",
            str(page),
            "--runs",
            "2",
            "--idle-wait",
            "0",
            "--max-ratio",
            "100",
            "--port",
            "9341",
            "--out",
            str(out),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PLAYWRIGHT_MODULE": runtime.playwright_module,
            "PLAYWRIGHT_EXECUTABLE_PATH": runtime.executable,
        },
        timeout=240,
    )
    assert done.returncode == 0, done.stderr
    report = json.loads(out.read_bytes())
    assert len(report["candidate"]["timed"]) == 2
    assert report["verdict"]["pass"] is True
    assert report["candidate"]["errors"] == []


_CLEANUP = """
import { removeTree } from %(harness)s;
const seen = [];
const eperm = () => { const e = new Error("held"); e.code = "EPERM"; throw e; };
const denied = () => { const e = new Error("no"); e.code = "EACCES"; throw e; };
const tolerated = removeTree("x", (p, o) => { seen.push(o); eperm(); });
let refused = false;
try { removeTree("x", denied); } catch (e) { refused = e.code === "EACCES"; }
process.stdout.write(JSON.stringify({ tolerated, refused, opts: seen[0], ok: removeTree("x", () => {}) }));
"""


def test_cleanup_tolerates_a_held_profile_but_not_other_errors() -> None:
    runtime = resolve_browser_runtime(REPOSITORY_ROOT)
    if runtime is None:
        pytest.skip("node is unavailable")
    source = _CLEANUP % {"harness": json.dumps(HARNESS.as_uri())}
    done = subprocess.run(
        [runtime.node, "--input-type=module", "-e", source],
        check=True,
        capture_output=True,
        text=True,
    )
    out = json.loads(done.stdout)
    assert out["tolerated"] is False and out["refused"] is True and out["ok"] is True
    assert out["opts"]["maxRetries"] >= 10 and out["opts"]["retryDelay"] >= 200


# #690: the lens/download step against a panel model whose report link sits in a hidden
# view. The fake page refuses a visibility-gated click (like Playwright's `page.click` on
# a nav item outside the viewport) and only honours attached-element DOM clicks.
_LENS = """
import { lensAndDownload, PAGE, STEP_ATTEMPTS } from %(harness)s;
import crypto from "node:crypto";
const BODY = Buffer.from("report-bytes");
function build({ linkInHiddenView = true, brokenNav = null, lensNeverReady = false, noDownload = false }) {
  const log = [];
  const state = { view: "command", lazy: "idle", files: new Map(), navFailures: 0 };
  const exists = (sel) => {
    if (sel.startsWith("[data-nav=")) return true;
    if (sel.includes("evidence-mode")) return state.view === "attribution" || !linkInHiddenView;
    if (sel.startsWith("a[data-download-filename")) return true; // attached, hidden unless view=data
    return false;
  };
  const page = {
    async click() { throw new Error("page.click must not be used: element outside of the viewport"); },
    async waitForSelector(sel, opts) {
      log.push(["wait", sel, opts.state]);
      if (opts.state !== "attached") throw new Error("visibility wait on a hidden element");
      if (!exists(sel)) throw new Error("timeout " + sel);
    },
    async evaluate(fn, arg) {
      if (fn === PAGE.click) {
        log.push(["click", arg]);
        if (arg === brokenNav && state.navFailures++ < %(nav_failures)d) return false;
        const nav = /data-nav="([a-z]+)"/.exec(arg);
        if (nav) state.view = nav[1];
        else if (arg.includes("evidence-mode")) {
          if (state.view !== "attribution") return false;
          state.lazy = lensNeverReady ? "loading" : "ready";
        } else if (arg.startsWith("a[data-download")) {
          if (state.view !== "data") return false;
          if (!noDownload) state.files.set("portfolio-report-data.json", BODY);
        }
        return true;
      }
      throw new Error("unexpected evaluate");
    },
    async waitForFunction(fn, arg) {
      if (fn === PAGE.viewShown) {
        if (state.view !== arg) throw new Error("view " + arg + " not shown");
        return;
      }
      if (lensNeverReady) throw new Error("timeout waiting for lazyData");
    },
  };
  let clock = 0;
  const io = {
    sleep: async (ms) => { clock += ms; },
    now: () => clock,
    makeDownloadDir: () => "dl",
    allowDownloads: async () => {},
    listDir: () => [...state.files.keys()],
    sizeOf: () => BODY.length,
    readFile: () => BODY,
    removeTree: () => { log.push(["cleanup"]); },
  };
  return { page, io, state, log };
}
const out = {};
{
  const r = build({});
  const res = await lensAndDownload(r.page, r.io);
  out.ok = { res, view: r.state.view, cleaned: r.log.some((l) => l[0] === "cleanup"),
    sha: crypto.createHash("sha256").update(BODY).digest("hex") };
  out.nav_order = r.log.filter((l) => l[0] === "click").map((l) => l[1]);
}
{
  const r = build({ brokenNav: '[data-nav="data"]' });
  out.retry = await lensAndDownload(r.page, r.io);
}
{
  const r = build({ lensNeverReady: true });
  out.lens = await lensAndDownload(r.page, r.io);
}
{
  const r = build({ noDownload: true });
  out.nodl = await lensAndDownload(r.page, r.io);
}
out.attempts = STEP_ATTEMPTS;
process.stdout.write(JSON.stringify(out));
"""


def _run_lens(nav_failures: int) -> dict[str, object]:
    runtime = resolve_browser_runtime(REPOSITORY_ROOT)
    node = runtime.node if runtime else shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    source = _LENS % {"harness": json.dumps(HARNESS.as_uri()), "nav_failures": nav_failures}
    done = subprocess.run(
        [node, "--input-type=module", "-e", source],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return json.loads(done.stdout)  # type: ignore[no-any-return]


def test_lens_and_download_reaches_the_report_link_in_the_hidden_data_view() -> None:
    out = _run_lens(nav_failures=1)
    ok = out["ok"]
    assert "failure" not in ok["res"]
    assert ok["view"] == "data" and ok["cleaned"] is True
    assert ok["res"]["download_bytes"] == len(b"report-bytes")
    assert ok["res"]["download_sha256"] == ok["sha"]
    assert ok["res"]["lens_open_ms"] >= 0 and ok["res"]["download_ms"] >= 0
    # The data view is reached by its documented nav selector before the download click.
    clicks = out["nav_order"]
    assert clicks.index('[data-nav="data"]') < clicks.index(
        'a[data-download-filename="portfolio-report-data.json"]'
    )


def test_lens_and_download_retries_a_flaky_nav_click_then_succeeds() -> None:
    out = _run_lens(nav_failures=1)
    assert "failure" not in out["retry"]
    assert out["retry"]["download_bytes"] == len(b"report-bytes")


def test_lens_and_download_fails_closed_with_a_named_step() -> None:
    out = _run_lens(nav_failures=99)
    assert out["retry"]["failure"]["step"] == "open data view"
    assert "failed" in out["retry"]["failure"]["message"]
    assert "download_sha256" not in out["retry"]
    assert out["lens"]["failure"]["step"] == "wait for lens data"
    assert out["nodl"]["failure"]["step"] == "wait for download"
