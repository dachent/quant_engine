#!/usr/bin/env node
// Rotation QA for the Performance charts tab: every viewport x evidence mode x zoom x axis x Max basis, then the value
// chart zooms, series toggles and tooltips, asserting the properties the visual audit found broken (overlapping or
// repeated tick labels, absurd or crowded y ticks, illegible tick text, chips without a window, a silent zoom cap).
//
//   node performance_rotation_qa.mjs --page <path to index.html> --out <result.json> [--shots <dir>] [--y-bound 50] [--viewports desktop,mobile]
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
  const labels = [...svg.querySelectorAll("text.axis-label")].map((node) => ({ text: node.textContent, anchor: node.getAttribute("text-anchor"), rect: node.getBoundingClientRect(), size: parseFloat(getComputedStyle(node).fontSize) * scale }));
  const xs = labels.filter((item) => item.anchor);
  const ys = labels.filter((item) => !item.anchor);
  let overlaps = 0;
  for (let i = 0; i < xs.length; i += 1) for (let j = i + 1; j < xs.length; j += 1) {
    const a = xs[i].rect, b = xs[j].rect;
    if (!(a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top)) overlaps += 1;
  }
  const clippedLeft = xs.filter((item) => item.rect.left < box.left - 1 || item.rect.right > box.right + 1).length;
  return { x: xs.map((item) => item.text), y: ys.map((item) => item.text), overlaps, clippedLeft, minFont: Math.min(...labels.map((item) => item.size)), viewBoxWidth: vb.width, boxWidth: box.width, overflowX: document.documentElement.scrollWidth > innerWidth };
};
const yNumber = (text) => { const match = /^([\d,.]+)([KMBT]?)$/.exec(text); return match ? parseFloat(match[1].replace(/,/g, "")) * ({ "": 1, K: 1e3, M: 1e6, B: 1e9, T: 1e12 })[match[2]] : NaN; };

function check(state, measured, { own = false } = {}) {
  states += 1;
  const fail = (message) => violations.push({ state, message });
  if (measured.missing) return fail("chart svg missing");
  if (measured.overlaps > 0) fail(`${measured.overlaps} overlapping x tick labels`);
  if (new Set(measured.x).size !== measured.x.length) fail(`repeated x tick labels: ${measured.x.join(" | ")}`);
  if (measured.x.length < 2 || !EDGE_DATE.test(measured.x[0]) || !EDGE_DATE.test(measured.x.at(-1))) fail(`x axis does not show exact start and end dates: ${measured.x.join(" | ")}`);
  if (measured.y.length > 10) fail(`${measured.y.length} y tick labels`);
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
  await page.goto(`${urlBase}#performance`, { waitUntil: "load", timeout: 120000 });
  await page.waitForSelector("#perf-returns-chart svg", { timeout: 30000 });
  const modes = await page.$$eval("[data-perf-mode]", (buttons) => buttons.map((button) => button.dataset.perfMode));
  const zooms = await page.$$eval("[data-perf-zoom]", (buttons) => buttons.map((button) => button.dataset.perfZoom));
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
          check(state, await page.evaluate(probe, "#perf-returns-chart"), { own: basis === "own" });
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
  // Value chart zooms.
  for (const zoom of vzooms) {
    await page.click(`[data-perf-value-zoom="${zoom}"]`);
    const state = `${viewport}/value/${zoom}`;
    check(state, await page.evaluate(probe, "#perf-value-chart"), { own: true });
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
