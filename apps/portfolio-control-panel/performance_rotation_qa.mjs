#!/usr/bin/env node
// Rotation QA for the Performance charts tab: every viewport x evidence mode x zoom x axis x Max basis, then the value
// chart zooms, series toggles and tooltips, asserting the properties the visual audit found broken (overlapping or
// repeated tick labels, absurd or crowded y ticks, illegible tick text, chips without a window, a silent zoom cap).
//
//   node performance_rotation_qa.mjs --page <path to index.html> --out <result.json> [--shots <dir>] [--y-bound 50] [--viewports desktop,mobile] [--expect <expected.json>]
//
// The page must show the Performance view (the script opens `#performance`).  Exit code 1 lists violations in the JSON.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

function args(argv) {
  const parsed = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    if (!key?.startsWith("--") || argv[index + 1] == null) throw new Error("Arguments must be --name value pairs");
    parsed[key.slice(2)] = argv[index + 1];
  }
  return parsed;
}
const options = args(process.argv.slice(2));
if (!options.page || !options.out) throw new Error("--page and --out are required");
const yBound = Number(options["y-bound"] || 50);
// Dates derived from the source data by the caller: { returns: { <zoom>: { start, end } | null }, value: { <zoom>: { start, end } } }.
const expected = options.expect ? JSON.parse(fs.readFileSync(options.expect, "utf8")) : null;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const edgeIso = (label) => { const m = /^([A-Z][a-z]{2}) ([0-9]{1,2}), ([0-9]{4})$/.exec(label || ""); return m ? `${m[3]}-${String(MONTHS.indexOf(m[1]) + 1).padStart(2, "0")}-${m[2].padStart(2, "0")}` : null; };
const here = path.dirname(fileURLToPath(import.meta.url));
const modulePath = process.env.PLAYWRIGHT_MODULE || path.join(here, "node_modules", "playwright", "index.mjs");
const { chromium } = await import(pathToFileURL(modulePath).href);

const ALL_VIEWPORTS = [["desktop", 1440, 900], ["tablet", 820, 1000], ["mobile", 390, 844]];
const wanted = options.viewports ? options.viewports.split(",") : ALL_VIEWPORTS.map(([name]) => name);
const VIEWPORTS = ALL_VIEWPORTS.filter(([name]) => wanted.includes(name));
const EDGE_DATE = /^[A-Z][a-z]{2} \d{1,2}, \d{4}$/;
const violations = [];
let states = 0;

// Runs in the page: measurements of one chart svg.
const probe = (selector) => {
  const host = document.querySelector(selector);
  const svg = host && host.querySelector("svg");
  if (!svg) return { missing: true };
  const box = svg.getBoundingClientRect();
  const vb = svg.viewBox.baseVal;
  const scale = box.width / vb.width;
  const labels = [...svg.querySelectorAll("text.axis-label")].map((node) => ({ text: node.textContent, anchor: node.getAttribute("text-anchor"), rect: node.getBoundingClientRect(), size: parseFloat(getComputedStyle(node).fontSize) * scale, y: Number(node.getAttribute("y")) }));
  const xs = labels.filter((item) => item.anchor);
  const ys = labels.filter((item) => !item.anchor);
  let overlaps = 0;
  for (let i = 0; i < xs.length; i += 1) for (let j = i + 1; j < xs.length; j += 1) {
    const a = xs[i].rect, b = xs[j].rect;
    if (!(a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top)) overlaps += 1;
  }
  const clippedLeft = xs.filter((item) => item.rect.left < box.left - 1 || item.rect.right > box.right + 1).length;
  const baseline = svg.querySelector('line[stroke-dasharray="4 3"]');
  const starts = [...svg.querySelectorAll("polyline:not(.perf-cursor)")].map((line) => line.getAttribute("points").trim().split(/\s+/)[0].split(",").map(Number));
  const yPoints = ys.map((item) => [item.text, item.y]);
  return { yPoints, baselineY: baseline ? Number(baseline.getAttribute("y1")) : null, starts, x: xs.map((item) => item.text), y: ys.map((item) => item.text), overlaps, clippedLeft, minFont: Math.min(...labels.map((item) => item.size)), viewBoxWidth: vb.width, boxWidth: box.width, overflowX: document.documentElement.scrollWidth > innerWidth };
};
const yNumber = (text) => { const match = /^([\d,.]+)([KMBT]?)$/.exec(text); return match ? parseFloat(match[1].replace(/,/g, "")) * ({ "": 1, K: 1e3, M: 1e6, B: 1e9, T: 1e12 })[match[2]] : NaN; };

function check(state, measured, { own = false, scale = null, expectEdges = null } = {}) {
  states += 1;
  const fail = (message) => violations.push({ state, message });
  // The y labels sit where the active axis says: linear in the value, or in its logarithm.
  if (scale && measured.yPoints) {
    const points = measured.yPoints.map(([text, y]) => [yNumber(text), y]).filter(([value]) => Number.isFinite(value) && value > 0);
    if (points.length >= 3) {
      const map = scale === "log" ? Math.log : (value) => value;
      const slopes = points.slice(1).map(([value, y]) => (y - points[0][1]) / (map(value) - map(points[0][0])));
      const mean = slopes.reduce((total, item) => total + item, 0) / slopes.length;
      if (slopes.some((slope) => Math.abs(slope - mean) > Math.abs(mean) * 0.03)) fail(`y labels do not follow a ${scale} axis: ${measured.yPoints.map((item) => item.join("@")).join(" ")}`);
    }
  }
  // The first and last axis dates are the source-derived window, not whatever the app printed.
  if (expectEdges && !measured.missing) {
    if (edgeIso(measured.x[0]) !== expectEdges.start) fail(`axis starts ${edgeIso(measured.x[0])}, source says ${expectEdges.start}`);
    if (edgeIso(measured.x.at(-1)) !== expectEdges.end) fail(`axis ends ${edgeIso(measured.x.at(-1))}, source says ${expectEdges.end}`);
  }
  if (measured.missing) return fail("chart svg missing");
  if (measured.overlaps > 0) fail(`${measured.overlaps} overlapping x tick labels`);
  if (new Set(measured.x).size !== measured.x.length) fail(`repeated x tick labels: ${measured.x.join(" | ")}`);
  if (measured.x.length < 2 || !EDGE_DATE.test(measured.x[0]) || !EDGE_DATE.test(measured.x.at(-1))) fail(`x axis does not show exact start and end dates: ${measured.x.join(" | ")}`);
  if (measured.y.length > 10 || measured.y.length < 4) fail(`${measured.y.length} y tick labels (need 4 to 10)`);
  // Comparable views rebase every line to 1.00 at the left edge: the dashed baseline exists and each line starting there starts on it.
  if (!own && check.rebased) {
    if (measured.baselineY == null) fail("no 1.00 baseline");
    else {
      const left = Math.min(...measured.starts.map((point) => point[0]));
      const off = measured.starts.filter((point) => Math.abs(point[0] - left) < 0.6 && Math.abs(point[1] - measured.baselineY) > 0.8);
      if (off.length) fail(`${off.length} lines do not start on the 1.00 baseline`);
    }
  }
  if (!own && Math.max(...measured.y.map(yNumber).filter(Number.isFinite)) > yBound) fail(`y tick above ${yBound}: ${measured.y.join(" | ")}`);
  if (measured.minFont < 9) fail(`axis text renders at ${measured.minFont.toFixed(1)} px`);
  if (measured.clippedLeft > 0) fail("x tick label outside the chart box");
  if (measured.overflowX) fail("page overflows horizontally");
}

const browser = await chromium.launch(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {});
const urlBase = pathToFileURL(path.resolve(options.page)).href;
const notes = {};
for (const [viewport, width, height] of VIEWPORTS) {
  const context = await browser.newContext({ viewport: { width, height } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("requestfailed", (request) => errors.push(`request failed: ${request.url().slice(0, 100)}`));
  await page.goto(`${urlBase}#performance`, { waitUntil: "load", timeout: 120000 });
  await page.waitForSelector("#perf-returns-chart svg", { timeout: 30000 });
  const modes = await page.$$eval("[data-perf-mode]", (buttons) => buttons.map((button) => button.dataset.perfMode));
  const zooms = await page.$$eval("[data-perf-zoom]", (buttons) => buttons.map((button) => button.dataset.perfZoom));
  if (!zooms.includes("YTD")) violations.push({ state: `${viewport}/controls`, message: "no YTD zoom" });
  const vzooms = await page.$$eval("[data-perf-value-zoom]", (buttons) => buttons.map((button) => button.dataset.perfValueZoom));
  const shot = async (name) => { if (options.shots) { fs.mkdirSync(options.shots, { recursive: true }); await page.locator("#perf-returns-chart").locator("xpath=ancestor::article[1]").screenshot({ path: path.join(options.shots, `${name}.png`) }); } };
  for (const mode of modes) {
    await page.click(`[data-perf-mode="${mode}"]`);
    notes[`${viewport}/${mode}`] = {};
    for (const zoom of zooms) {
      await page.click(`[data-perf-zoom="${zoom}"]`);
      const bases = zoom === "Max" ? ["common", "own"] : ["common"];
      for (const basis of bases) {
        if (zoom === "Max") await page.click(`[data-perf-basis="${basis}"]`);
        for (const axis of ["linear", "log"]) {
          const button = page.locator(`[data-perf-axis="${axis}"]`);
          if (await button.isEnabled()) await button.click();
          const state = `${viewport}/${mode}/${zoom}/${basis}/${axis}`;
          check.rebased = true;
          const measured = await page.evaluate(probe, "#perf-returns-chart");
          const activeAxis = await page.$eval("[data-perf-axis].is-active", (node) => node.dataset.perfAxis);
          const sourceWindow = expected?.returns?.[zoom];
          check(state, measured, { own: basis === "own", scale: activeAxis, expectEdges: basis === "common" && sourceWindow ? sourceWindow : null });
          if (basis === "common" && expected && sourceWindow === null && !(await page.$eval("#perf-returns-chart", (node) => node.textContent)).includes("no full week yet")) violations.push({ state, message: "an unavailable zoom does not say so" });
          check.rebased = false;
          // The window line and the chart's first date label agree on the start.
          if (basis === "common") {
            const note = await page.$eval("#perf-window-note", (node) => node.textContent);
            const noteStart = /(?:starts |: )([0-9]{4}-[0-9]{2}-[0-9]{2})/.exec(note)?.[1];
            const edge = /^([A-Z][a-z]{2}) ([0-9]{1,2}), ([0-9]{4})$/.exec(measured.x[0]);
            const edgeIso = edge ? `${edge[3]}-${String(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"].indexOf(edge[1]) + 1).padStart(2, "0")}-${edge[2].padStart(2, "0")}` : null;
            if (!noteStart || noteStart !== edgeIso) violations.push({ state, message: `window line start ${noteStart} differs from the first axis date ${edgeIso}` });
          }
          const chips = await page.$$eval("#perf-returns-legend button.perf-legend-item", (nodes) => nodes.map((node) => node.textContent.replace(/\s+/g, " ").trim()));
          if (!chips.length || chips.some((chip) => !chip.includes("→"))) violations.push({ state, message: `legend chip without a window: ${chips.filter((chip) => !chip.includes("→")).join(" ; ") || "no chips"}` });
          if (axis === "linear" && basis === "common") notes[`${viewport}/${mode}`][zoom] = await page.$eval("#perf-window-note", (node) => node.textContent.trim());
          await shot(`${state.replaceAll("/", "__")}`);
        }
      }
    }
    // 1y and 3y must differ when the data allows; when they match, the 3y window must say it is limited.
    const one = notes[`${viewport}/${mode}`]["1y"], three = notes[`${viewport}/${mode}`]["3y"];
    if (one && three && one.replace(/^\w+:/, "") === three.replace(/^\w+:/, "") && !three.includes("limited by the group anchor")) violations.push({ state: `${viewport}/${mode}`, message: "1y and 3y windows are identical and 3y does not say it is limited" });
    if (three && three.includes("limited") && !/starts \d{4}-\d{2}-\d{2}/.test(three)) violations.push({ state: `${viewport}/${mode}`, message: `limited state does not name the start: ${three}` });
  }
  // Series toggle and tooltip.
  await page.click(`[data-perf-zoom="Max"]`);
  const first = page.locator("[data-perf-series]").first();
  await first.click();
  if ((await page.locator("[data-perf-series]").first().getAttribute("aria-pressed")) !== "false") violations.push({ state: `${viewport}/toggle`, message: "series chip did not switch off" });
  await page.locator("[data-perf-series]").first().click();
  const svgBox = await page.locator("#perf-returns-chart svg").boundingBox();
  await page.mouse.move(svgBox.x + svgBox.width * 0.7, svgBox.y + svgBox.height * 0.5);
  const tip = await page.evaluate(() => { const node = document.querySelector("#perf-returns-chart .perf-tooltip"); return node && !node.hidden ? node.innerText : ""; });
  if (!tip.includes("%") || /\d{3,}\.\d{3}/.test(tip)) violations.push({ state: `${viewport}/tooltip`, message: `tooltip does not print percentages only: ${tip.slice(0, 120)}` });
  // Keyboard: the chart takes focus and the arrow keys move a visible tooltip.
  await page.locator("#perf-returns-chart svg").focus();
  await page.keyboard.press("ArrowLeft");
  if (!(await page.evaluate(() => { const node = document.querySelector("#perf-returns-chart .perf-tooltip"); return Boolean(node && !node.hidden && node.innerText.includes("%")); }))) violations.push({ state: `${viewport}/keyboard`, message: "ArrowLeft did not show the tooltip" });
  // A phone tooltip sits below the chart, not over it.
  if (width < 520) {
    const geometry = await page.evaluate(() => { const svg = document.querySelector("#perf-returns-chart svg").getBoundingClientRect(); const tip = document.querySelector("#perf-returns-chart .perf-tooltip").getBoundingClientRect(); return { svgBottom: svg.bottom, tipTop: tip.top }; });
    if (geometry.tipTop < geometry.svgBottom - 1) violations.push({ state: `${viewport}/tooltip-position`, message: "tooltip covers the chart on a phone" });
  }
  // Every series off: a message, not a blank axis; chips stay so the series can come back.
  const chipCount = await page.locator("[data-perf-series]").count();
  for (let index = 0; index < chipCount; index += 1) {
    const chip = page.locator("[data-perf-series]").nth(index);
    if ((await chip.getAttribute("aria-pressed")) === "true") await chip.click();
  }
  if (!(await page.$eval("#perf-returns-chart", (node) => node.textContent)).includes("switched off")) violations.push({ state: `${viewport}/all-off`, message: "no empty-state message when every series is off" });
  for (let index = 0; index < chipCount; index += 1) {
    const chip = page.locator("[data-perf-series]").nth(index);
    if ((await chip.getAttribute("aria-pressed")) === "false") await chip.click();
  }
  // Value chart zooms.
  for (const zoom of vzooms) {
    await page.click(`[data-perf-value-zoom="${zoom}"]`);
    const state = `${viewport}/value/${zoom}`;
    check(state, await page.evaluate(probe, "#perf-value-chart"), { own: true, scale: "linear", expectEdges: expected?.value?.[zoom] || null });
    const legend = await page.$eval("#perf-value-legend", (node) => node.innerText);
    if (/\$0\b(?!,)/.test(legend.replace(/\$0\.\d+/g, ""))) violations.push({ state, message: "value legend lists a $0 slice" });
    if (!(await page.$eval("#perf-value-window-note", (node) => node.textContent)).includes("flow-matched")) violations.push({ state, message: "value chart does not label its benchmark basis" });
  }
  if (errors.length) violations.push({ state: `${viewport}/console`, message: errors.slice(0, 3).join(" | ") });
  await context.close();
}
await browser.close();
fs.writeFileSync(options.out, JSON.stringify({ states, violations, notes }, null, 1));
console.log(JSON.stringify({ states, violations: violations.length }));
process.exit(violations.length ? 1 : 0);
