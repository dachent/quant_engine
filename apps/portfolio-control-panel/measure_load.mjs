// #540 browser-load measurement for a control-panel generation, unattended.
//
// usage: node measure_load.mjs --html <gen>/index.html [--control <control>/index.html]
//          [--runs 3] [--idle-cpu 15] [--idle-wait 120] [--max-ratio 1.25]
//          [--memory] [--lens-and-download] [--out result.json]
// env:   PLAYWRIGHT_MODULE (index.mjs), PLAYWRIGHT_EXECUTABLE_PATH (chrome binary)
//
// Usable load = page.goto(file://...index.html#command, waitUntil "commit") until
// #app[data-app-state="ready"] exists. No DOMContentLoaded, no paint. Clicks (lens mode,
// download) are never part of it.
//
// Protocol (why the numbers can be trusted):
//   1. Timing runs never run a sampler: a process-tree scan next to the load inflated
//      R2's first figures from ~2 s to 88-150 s. Timing: one cold run (first load after
//      the file was written), one warm-up, then --runs timed runs.
//   2. Memory (--memory, Windows) is a separate phase on its own browser: the Chrome
//      process set is sampled once a second, and its numbers are never timing evidence.
//   3. Machine state is recorded, not assumed: total CPU is read before the timed runs
//      (waits up to --idle-wait seconds for it to fall under --idle-cpu percent) and
//      around every run. A run set that was not idle says so in `idle`.
//   4. A control generation (--control, e.g. the committed R1 panel) is measured with
//      the identical protocol. The verdict is the candidate/control ratio of the timed
//      means, so a busy machine moves both.
import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

export function mean(values) {
  return values.reduce((a, b) => a + b, 0) / values.length;
}

export function summarize(runs) {
  const ms = runs.map((r) => r.usable_load_ms);
  return {
    runs: ms,
    mean_ms: Math.round(mean(ms)),
    min_ms: Math.min(...ms),
    max_ms: Math.max(...ms),
  };
}

// The verdict compares like with like: candidate mean over control mean.
export function verdict(candidate, control, maxRatio) {
  if (!control) return { ratio: null, pass: null };
  const ratio = candidate.mean_ms / control.mean_ms;
  return { ratio: Number(ratio.toFixed(3)), max_ratio: maxRatio, pass: ratio <= maxRatio };
}

function cpuSnapshot() {
  return os.cpus().reduce(
    (acc, c) => {
      const t = Object.values(c.times).reduce((a, b) => a + b, 0);
      return { idle: acc.idle + c.times.idle, total: acc.total + t };
    },
    { idle: 0, total: 0 },
  );
}

function cpuBusyPercent(a, b) {
  const total = b.total - a.total;
  return total > 0 ? Math.round((100 * (total - (b.idle - a.idle))) / total) : 0;
}

async function sleep(ms) {
  await new Promise((r) => setTimeout(r, ms));
}

async function waitIdle(thresholdPercent, maxSeconds) {
  const deadline = Date.now() + maxSeconds * 1000;
  for (;;) {
    const a = cpuSnapshot();
    await sleep(3000);
    const busy = cpuBusyPercent(a, cpuSnapshot());
    if (busy <= thresholdPercent || Date.now() >= deadline) return busy;
  }
}

async function launch(pw, executable, port) {
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), "panel-load-"));
  const child = spawn(
    executable,
    [
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${userDir}`,
      "--headless=new",
      "--enable-precise-memory-info",
      "--no-first-run",
      "--allow-file-access-from-files",
      "about:blank",
    ],
    { stdio: "ignore" },
  );
  let browser;
  for (let attempt = 0; attempt < 40 && !browser; attempt += 1) {
    await sleep(500);
    try {
      browser = await pw.chromium.connectOverCDP(`http://127.0.0.1:${port}`);
    } catch {
      /* browser still starting */
    }
  }
  if (!browser) throw new Error("chrome did not accept a CDP connection");
  return {
    browser,
    async close() {
      const exited = new Promise((r) => child.once("exit", r));
      child.kill();
      await Promise.race([exited, sleep(5000)]);
      removeTree(userDir);
    },
  };
}

// Windows can hold a just-killed browser's profile files for a moment (EPERM/EBUSY). The
// measurement is already complete when this runs, so retry generously and then tolerate a
// cleanup failure instead of failing the harness; a leftover temp dir is not a result.
export function removeTree(target, remove = fs.rmSync) {
  try {
    remove(target, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
    return true;
  } catch (error) {
    if (["EPERM", "EBUSY", "ENOTEMPTY"].includes(error && error.code)) {
      return false;
    }
    throw error;
  }
}

async function loadOnce(browser, html, extra) {
  const page = await browser.contexts()[0].newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e).slice(0, 200)));
  const cpuBefore = cpuSnapshot();
  const t0 = Date.now();
  await page.goto(`${pathToFileURL(html).href}#command`, { waitUntil: "commit", timeout: 600000 });
  await page.waitForSelector('#app[data-app-state="ready"]', { state: "attached", timeout: 900000 });
  const usable = Date.now() - t0;
  const run = { usable_load_ms: usable, cpu_busy_percent: cpuBusyPercent(cpuBefore, cpuSnapshot()), errors };
  run.js_heap_mb = await page.evaluate(() => Math.round(performance.memory.usedJSHeapSize / 1048576));
  if (extra) Object.assign(run, await extra(page));
  await page.close();
  return run;
}

async function lensAndDownload(page) {
  const out = {};
  await page.click('[data-nav="attribution"]');
  const t1 = Date.now();
  await page.click('[data-factor-evidence-mode="LIVE_PLUS_BACKTEST"]');
  await page.waitForFunction(() => document.documentElement.dataset.lazyData !== "loading", null, {
    timeout: 600000,
  });
  out.lens_open_ms = Date.now() - t1;
  const client = await page.context().newCDPSession(page);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "panel-dl-"));
  await client.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: dir });
  const t2 = Date.now();
  await page.click('a[data-download-filename="portfolio-report-data.json"]');
  let file;
  while (!file && Date.now() - t2 < 600000) {
    await sleep(250);
    file = fs.readdirSync(dir).find((n) => !n.endsWith(".crdownload"));
  }
  if (file) {
    const bytes = fs.readFileSync(path.join(dir, file));
    out.download_ms = Date.now() - t2;
    out.download_bytes = bytes.length;
    out.download_sha256 = crypto.createHash("sha256").update(bytes).digest("hex");
  }
  removeTree(dir);
  return out;
}

// One long-lived PowerShell sums WorkingSet64 of the processes running exactly this browser
// binary (a user's own Chrome is a different path and is not counted).
function startRssSampler(executable) {
  if (process.platform !== "win32") return null;
  const exePath = path.resolve(executable).replaceAll("'", "''");
  const script =
    `while ($true) { $s = (Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq '${exePath}' } | ` +
    `Measure-Object WorkingSet64 -Sum).Sum; if ($s) { [Console]::Out.WriteLine([int]($s / 1MB)) }; ` +
    "Start-Sleep -Seconds 1 }";
  const child = spawn("powershell", ["-NoProfile", "-Command", script], {
    stdio: ["ignore", "pipe", "ignore"],
  });
  const samples = [];
  child.stdout.on("data", (d) => {
    for (const line of String(d).split(/\r?\n/)) if (line.trim()) samples.push(Number(line));
  });
  return { samples, stop: () => child.kill() };
}

async function measure(pw, executable, html, options, label) {
  const result = { label, html };
  const session = await launch(pw, executable, options.port);
  try {
    result.cold = await loadOnce(session.browser, html);
    result.warm_up = await loadOnce(session.browser, html);
    const timed = [];
    for (let i = 0; i < options.runs; i += 1) timed.push(await loadOnce(session.browser, html));
    result.timed = timed;
    result.summary = summarize(timed);
    result.errors = [...new Set(timed.flatMap((r) => r.errors))];
  } finally {
    await session.close();
  }
  return result;
}

async function measureMemory(pw, executable, html, options) {
  const sampler = startRssSampler(executable);
  if (!sampler) return { skipped: "RSS sampling is implemented for Windows only" };
  const session = await launch(pw, executable, options.port);
  let run;
  try {
    run = await loadOnce(session.browser, html, options.lensAndDownload ? lensAndDownload : null);
    await sleep(1500);
  } finally {
    await session.close();
    sampler.stop();
  }
  return { ...run, peak_rss_mb: sampler.samples.length ? Math.max(...sampler.samples) : null };
}

function parseArgs(argv) {
  const get = (name, fallback) => {
    const at = argv.indexOf(name);
    return at >= 0 ? argv[at + 1] : fallback;
  };
  return {
    html: get("--html"),
    control: get("--control"),
    runs: Number(get("--runs", "3")),
    idleCpu: Number(get("--idle-cpu", "15")),
    idleWait: Number(get("--idle-wait", "120")),
    maxRatio: Number(get("--max-ratio", "1.25")),
    memory: argv.includes("--memory"),
    lensAndDownload: argv.includes("--lens-and-download"),
    out: get("--out"),
    port: Number(get("--port", "9333")),
  };
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (!options.html) throw new Error("--html is required");
  const watchdog = setTimeout(() => process.exit(3), 3 * 3600 * 1000);
  const pw = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
  const executable = process.env.PLAYWRIGHT_EXECUTABLE_PATH || pw.chromium.executablePath();
  const idleBusy = await waitIdle(options.idleCpu, options.idleWait);
  const report = {
    protocol: "usable load = goto commit -> #app[data-app-state=ready]; timing runs sampler-free",
    chromium: path.basename(path.dirname(executable)),
    node: process.version,
    machine: { logical_cpus: os.cpus().length, cpu_busy_percent_before: idleBusy },
  };
  const candidate = await measure(pw, executable, options.html, options, "candidate");
  const control = options.control ? await measure(pw, executable, options.control, options, "control") : null;
  report.candidate = candidate;
  if (control) report.control = control;
  const busy = [candidate, control].flatMap((m) => (m ? m.timed.map((r) => r.cpu_busy_percent) : []));
  report.idle = {
    threshold_percent: options.idleCpu,
    mean_cpu_busy_percent_during_timed_runs: Math.round(mean(busy)),
    idle: idleBusy <= options.idleCpu && mean(busy) <= options.idleCpu + 10,
  };
  report.verdict = verdict(candidate.summary, control && control.summary, options.maxRatio);
  if (options.memory) report.memory = await measureMemory(pw, executable, options.html, options);
  clearTimeout(watchdog);
  const text = `${JSON.stringify(report, null, 2)}\n`;
  if (options.out) fs.writeFileSync(options.out, text);
  else process.stdout.write(text);
  process.exit(report.verdict.pass === false ? 1 : 0);
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  main().catch((error) => {
    console.error(error);
    process.exit(2);
  });
}
