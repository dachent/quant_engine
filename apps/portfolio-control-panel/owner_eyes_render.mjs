#!/usr/bin/env node
// Owner-eyes rendered-panel checks with numeric assertions (#912, review Q1 control 2).
//
// The existing browser checks assert that the page renders; the owner's finds were things a
// rendered page showed and no check measured: charts with a thousand-fold axis, repeated tick
// labels, a "PARTIAL" week on the last trading day, benchmarks hidden by default, headline
// numbers that differ from the payload.  This script measures the rendered DOM of every tab
// (and every performance zoom) and asserts numbers.
//
//   live:  node owner_eyes_render.mjs --html <dir>/index.html --output <dir>
//          (PLAYWRIGHT_MODULE names playwright's index.mjs; PLAYWRIGHT_EXECUTABLE_PATH optional)
//   dry:   node owner_eyes_render.mjs --snapshot <snapshot.json> --payload <dashboard_data.json>
//          --output <dir>          (no browser: evaluates a captured snapshot, used by the tests)
//
// Writes owner-eyes-render.json (check ids, codes, counts; never a DOM or payload value) and,
// live, one screenshot per tab.  Exit 0 all pass, 1 a check failed, 2 usage or load error.
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

export const BOUNDS = {
  minTicks: 2,
  maxTicks: 14,
  axisRatioMax: 1000,
  percentChipMax: 10000,
  numberTolerance: 0.06,
};

export const TABS = ["command", "capital", "risk", "signals", "attribution", "dependence", "weekly", "performance", "modelvsactual", "candidates", "data"];

const squash = (t) => String(t ?? "").replace(/\s+/g, " ").trim();
const PCT = /(-?\d[\d,]*\.?\d*)\s?%/g;

export function parseTick(text) {
  const m = String(text).replace(/[,$\s]/g, "").match(/^(-?\d*\.?\d+)(?:[%x×kKmMbB])?$/);
  return m ? Number(m[1]) : null;
}

export function numbersIn(text) {
  return [...String(text).matchAll(/-?\d[\d,]*\.?\d*/g)].map((m) => Number(m[0].replaceAll(",", ""))).filter(Number.isFinite);
}

function get(doc, ...keys) {
  let node = doc;
  for (const k of keys) {
    if (node == null || typeof node !== "object") return undefined;
    node = node[k];
  }
  return node;
}

const isoDate = (s) => (typeof s === "string" && /^\d{4}-\d{2}-\d{2}$/.test(s) ? new Date(`${s}T00:00:00Z`) : null);

// snapshot: { views: [ { tab, zoom|null, bodyText, charts: [ { id, ticks: [text], legend: [text], log: bool } ], chips: [text] } ] }
export function evaluateSnapshot(snapshot, payload) {
  const out = [];
  const add = (id, ok, code, count = 0) => out.push({ id, status: ok ? "PASS" : "FAIL", code: ok ? "OK" : code, count });
  const views = Array.isArray(snapshot?.views) ? snapshot.views : [];
  add("views_captured", views.length >= 1, "NO_VIEWS_CAPTURED", views.length);

  let tickBad = 0, labelRepeat = 0, ratioBad = 0, chartCount = 0;
  for (const v of views) {
    for (const c of v.charts ?? []) {
      chartCount += 1;
      const ticks = (c.ticks ?? []).map(squash).filter(Boolean);
      if (ticks.length && (ticks.length < BOUNDS.minTicks || ticks.length > BOUNDS.maxTicks)) tickBad += 1;
      if (new Set(ticks).size !== ticks.length) labelRepeat += 1;
      const nums = ticks.map(parseTick).filter((n) => n !== null);
      if (!c.log && nums.length >= 2 && Math.min(...nums) > 0 && Math.max(...nums) / Math.min(...nums) > BOUNDS.axisRatioMax) ratioBad += 1;
    }
  }
  add("tick_count_bounded", tickBad === 0, "TICK_COUNT_OUT_OF_BOUNDS", tickBad);
  add("tick_labels_unique", labelRepeat === 0, "REPEATED_AXIS_LABELS", labelRepeat);
  add("axis_ratio_bounded_or_log", ratioBad === 0, "AXIS_RANGE_RATIO_TOO_LARGE", ratioBad);

  let chipBad = 0;
  for (const v of views) {
    for (const chip of v.chips ?? []) {
      for (const m of String(chip).matchAll(PCT)) {
        if (Math.abs(Number(m[1].replaceAll(",", ""))) >= BOUNDS.percentChipMax && !/\b(x|scale|log)\b/i.test(chip)) chipBad += 1;
      }
    }
  }
  add("percent_chip_below_10000", chipBad === 0, "PERCENT_CHIP_UNBOUNDED", chipBad);

  // calendar truth: a week whose data reach its last trading day is never labelled partial
  const cw = get(payload, "weekly_returns", "current_week") ?? {};
  const through = isoDate(cw.data_through_date);
  const fridayComplete = through !== null && through.getUTCDay() === 5;
  const bodies = views.map((v) => squash(v.bodyText).toLowerCase());
  const partialShown = bodies.some((b) => /\bpartial week\b|\(partial\)|\bpartial\b.{0,20}\bweek\b/.test(b));
  add("no_partial_label_on_complete_week", !(fridayComplete && partialShown), "PARTIAL_LABEL_ON_COMPLETE_WEEK", partialShown ? 1 : 0);

  // default view shows the benchmarks the payload carries
  const perf = views.filter((v) => v.tab === "performance");
  const lines = get(payload, "performance_tab", "modes", "LIVE_ONLY", "lines") ?? [];
  const benchmarks = lines.filter((l) => l.kind === "BENCHMARK").map((l) => squash(l.display_name).toLowerCase());
  if (benchmarks.length && perf.length) {
    const first = perf[0];
    const legend = (first.charts ?? []).flatMap((c) => c.legend ?? []).map((t) => squash(t).toLowerCase()).join(" ") + " " + squash(first.bodyText).toLowerCase();
    const missing = benchmarks.filter((b) => !legend.includes(b));
    add("benchmarks_visible_by_default", missing.length === 0, "BENCHMARK_HIDDEN_IN_DEFAULT_VIEW", missing.length);
  } else {
    add("benchmarks_visible_by_default", perf.length === 0 || benchmarks.length === 0, "NO_PERFORMANCE_VIEW_CAPTURED", 0);
  }

  // headline numbers on the page equal the payload (numbers, not tokens)
  const heads = [
    ["correlation_one_stress", get(payload, "advisory", "correlation_one_stress")],
    ["central_case_loss", get(payload, "advisory", "central_case_loss")],
    ["hwm_current_drawdown", get(payload, "advisory", "hwm_candidate", "current_drawdown")],
  ];
  const everyNumber = views.flatMap((v) => numbersIn(v.bodyText));
  let headBad = 0;
  for (const [, raw] of heads) {
    const want = Math.abs(Number(raw)) * 100;
    if (Number.isFinite(want) && !everyNumber.some((n) => Math.abs(Math.abs(n) - want) <= BOUNDS.numberTolerance)) headBad += 1;
  }
  add("headline_numbers_match_payload", headBad === 0, "HEADLINE_NUMBER_NOT_ON_PAGE", headBad);

  // each tab of the contract was visited
  const seen = new Set(views.map((v) => v.tab));
  const absent = TABS.filter((t) => !seen.has(t));
  add("every_tab_visited", absent.length === 0, "TAB_NOT_VISITED", absent.length);
  add("charts_measured", chartCount > 0, "NO_CHART_MEASURED", chartCount);
  return out;
}

function parseArgs(argv) {
  const parsed = {};
  for (let i = 0; i < argv.length; i += 2) {
    if (!argv[i]?.startsWith("--") || !argv[i + 1]) throw new Error("Arguments must be --name value pairs");
    parsed[argv[i].slice(2)] = argv[i + 1];
  }
  return parsed;
}

async function collect(page, tab) {
  return page.evaluate((name) => {
    const charts = [...document.querySelectorAll("svg")].map((svg, i) => ({
      id: svg.id || `svg-${i}`,
      ticks: [...svg.querySelectorAll("text")].map((t) => t.textContent).filter((t) => /^[\s$\-+0-9.,%×xkKmMbB]+$/.test(t)),
      legend: [...document.querySelectorAll(".legend-item, .chart-legend, [data-legend]")].map((n) => n.textContent),
      log: document.querySelector('[data-perf-axis="log"]')?.getAttribute("aria-pressed") === "true",
    }));
    const chips = [...document.querySelectorAll('[class*="chip"], [class*="badge"], [class*="kpi"], [class*="pill"]')].map((n) => n.textContent);
    return { tab: name, bodyText: document.body.innerText, charts, chips };
  }, tab);
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (!options.output) throw new Error("--output is required");
  const output = path.resolve(options.output);
  await mkdir(output, { recursive: true });
  let snapshot, payload;
  if (options.snapshot) {
    if (!options.payload) throw new Error("--payload is required with --snapshot");
    snapshot = JSON.parse(await readFile(options.snapshot, "utf8"));
    payload = JSON.parse(await readFile(options.payload, "utf8"));
  } else {
    if (!options.html || !process.env.PLAYWRIGHT_MODULE) throw new Error("--html and PLAYWRIGHT_MODULE are required");
    const html = path.resolve(options.html);
    payload = JSON.parse(await readFile(path.join(path.dirname(html), "data", "dashboard_data.json"), "utf8"));
    const { chromium } = await import(pathToFileURL(path.resolve(process.env.PLAYWRIGHT_MODULE)).href);
    const browser = await chromium.launch({
      headless: true,
      ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {}),
    });
    const views = [];
    try {
      for (const mode of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
        const page = await browser.newPage({ viewport: mode });
        page.setDefaultTimeout(300000);
        await page.goto(`${pathToFileURL(html).href}#command`, { waitUntil: "load", timeout: 300000 });
        await page.waitForFunction(() => document.querySelector("#app")?.dataset.appState === "ready");
        for (const tab of TABS) {
          const nav = page.locator(`[data-nav="${tab}"]`);
          if (await nav.count()) await nav.first().click();
          else await page.evaluate((h) => { window.location.hash = h; }, tab);
          await page.waitForTimeout(250);
          views.push({ ...(await collect(page, tab)), zoom: null, width: mode.width });
          if (tab === "performance") {
            for (const zoom of await page.locator("[data-perf-zoom]").evaluateAll((n) => n.map((x) => x.getAttribute("data-perf-zoom")))) {
              await page.locator(`[data-perf-zoom="${zoom}"]`).click();
              views.push({ ...(await collect(page, tab)), zoom, width: mode.width });
            }
          }
          await page.screenshot({ path: path.join(output, `${tab}-${mode.width}.png`), fullPage: true });
        }
        await page.close();
      }
    } finally {
      await browser.close();
    }
    snapshot = { views };
  }
  const results = evaluateSnapshot(snapshot, payload);
  const status = results.every((r) => r.status === "PASS") ? "PASS" : "FAIL";
  await writeFile(path.join(output, "owner-eyes-render.json"), `${JSON.stringify({ schema: "qe-owner-eyes-render/1", status, checks: results }, null, 2)}\n`, "utf8");
  for (const r of results.filter((x) => x.status !== "PASS")) console.error(`OWNER_EYES_FAIL ${r.id} ${r.code} ${r.count}`);
  process.exitCode = status === "PASS" ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((error) => {
    console.error(`OWNER_EYES_ERROR ${error.name}`);
    process.exitCode = 2;
  });
}
