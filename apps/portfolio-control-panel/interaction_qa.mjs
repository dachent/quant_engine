#!/usr/bin/env node
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

function args(argv) {
  const parsed = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || !value) throw new Error("Arguments must be --name value pairs");
    parsed[key.slice(2)] = value;
  }
  return parsed;
}

function check(name, condition, evidence) {
  return { check: name, evidence, status: condition ? "PASS" : "FAIL" };
}

const options = args(process.argv.slice(2));
if (!options.html || !options.output || !process.env.PLAYWRIGHT_MODULE) {
  throw new Error("--html, --output, and PLAYWRIGHT_MODULE are required");
}
const moduleUrl = pathToFileURL(path.resolve(process.env.PLAYWRIGHT_MODULE)).href;
const { chromium } = await import(moduleUrl);
const html = path.resolve(options.html);
const output = path.resolve(options.output);
const panelId = path.basename(path.dirname(html));
const browser = await chromium.launch({
  headless: true,
  ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH
    ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH }
    : {}),
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
await page.goto(pathToFileURL(html).href, { waitUntil: "load" });
await page.waitForFunction(() => document.querySelector("#app")?.dataset.appState === "ready");

const checks = [];
const views = ["command", "capital", "risk", "signals", "attribution", "dependence", "candidates", "data"];
const visited = [];
for (const view of views) {
  await page.locator(`[data-nav="${view}"]`).click();
  const active = await page.locator(`[data-view="${view}"]`).evaluate((node) => !node.hidden && node.classList.contains("is-active"));
  if (active) visited.push(view);
}
checks.push(check("All eight navigation views activate", visited.length === views.length, visited));

await page.locator('[data-nav="attribution"]').click();
const factorLensButtons = await page.locator("[data-factor-lens]").count();
await page.locator('[data-factor-lens="HISTORICAL_MINIMUM_PROOF"]').click();
const factorRows = await page.locator("#factor-model-table tbody tr").count();
const factorChips = await page.locator("[data-factor-strategy]").count();
await page.locator("[data-factor-strategy]").first().click();
const dynamicChart = await page.locator(".dynamic-chart").count();
const dynamicValidation = await page.locator("#factor-detail").textContent();
checks.push(check(
  "Dynamic factor path and static benchmark are visible",
  dynamicChart === 1 && dynamicValidation?.includes("Dynamic minus static") && dynamicValidation?.includes("Train LOO predicted R²"),
  { chart_count: dynamicChart, static_benchmark: dynamicValidation?.includes("Dynamic minus static"), training_validation: dynamicValidation?.includes("Train LOO predicted R²") },
));
await page.locator("[data-factor-strategy]").last().click();
const unavailableFactorDetail = await page.locator("#factor-detail").textContent();
checks.push(check("Factor and alpha coverage is interactive", factorRows === 9 && factorChips === 9 && unavailableFactorDetail?.includes("UNAVAILABLE"), { factor_chips: factorChips, factor_rows: factorRows, unavailable_visible: unavailableFactorDetail?.includes("UNAVAILABLE") }));
await page.locator('[data-factor-lens="ECONOMIC_FACTOR"]').click();
const economicBoundary = await page.locator("#factor-lens-boundary").textContent();
checks.push(check(
  "Economic, style, sector, industry, and historical lenses remain separate",
  factorLensButtons === 5 && economicBoundary?.includes("EXCESS_AND_LONG_SHORT_FACTOR_RETURN") && economicBoundary?.includes("never concatenated"),
  { factor_lenses: factorLensButtons, economic_boundary: economicBoundary },
));

await page.locator('[data-nav="dependence"]').click();
const liveModePressed = await page.locator('[data-evidence-mode="LIVE_ONLY"]').getAttribute("aria-pressed");
const hybridMode = page.locator('[data-evidence-mode="LIVE_PLUS_BACKTEST"]');
const hybridDisabled = await hybridMode.isDisabled();
let hybridBadge = null;
if (!hybridDisabled) {
  await hybridMode.click();
  hybridBadge = await page.locator("#evidence-mode-badge").textContent();
  await page.locator('[data-evidence-mode="LIVE_ONLY"]').click();
}
checks.push(check(
  "Dependence evidence mode defaults to live-only and fails closed without backtest",
  liveModePressed === "true" && (hybridDisabled || hybridBadge?.includes("HYBRID BACKTEST + LIVE")),
  { hybrid_available: !hybridDisabled, hybrid_badge: hybridBadge, live_default: liveModePressed },
));
const matrixCells = await page.locator("[data-correlation-left]").count();
await page.locator('[data-matrix="stress"]').click();
const matrixTitle = await page.locator("#matrix-title").textContent();
await page.locator('[data-correlation-left]:not([disabled])').first().click();
const pairDetail = await page.locator("#correlation-detail").textContent();
checks.push(check("Dependence matrices and cell evidence are interactive", matrixCells === 81 && matrixTitle?.includes("Stress") && pairDetail?.includes("Observed status"), { cell_buttons: matrixCells, matrix_title: matrixTitle, pair_detail: Boolean(pairDetail?.includes("Observed status")) }));
await page.locator('[data-correlation-window="21"]').click();
const horizonTitle = await page.locator("#matrix-title").textContent();
const horizonPressed = await page.locator('[data-correlation-window="21"]').getAttribute("aria-pressed");
await page.locator('[data-correlation-window="either_drawdown"]').click();
const drawdownTitle = await page.locator("#matrix-title").textContent();
checks.push(check(
  "Observed windows and true drawdown regimes are selectable",
  horizonTitle?.includes("21 sessions") && horizonPressed === "true" && drawdownTitle?.includes("either strategy in drawdown"),
  { drawdown_title: drawdownTitle, horizon_pressed: horizonPressed, horizon_title: horizonTitle },
));

await page.locator('[data-nav="capital"]').click();
await page.locator('[data-allocation-filter="RESERVE"]').click();
const reservePressed = await page.locator('[data-allocation-filter="RESERVE"]').getAttribute("aria-pressed");
const reserveRows = Number(await page.locator("#allocation-table").getAttribute("data-visible-rows"));
await page.locator('[data-allocation-filter="SYSTEMATIC"]').click();
const systematicRows = Number(await page.locator("#allocation-table").getAttribute("data-visible-rows"));
await page.locator('[data-allocation-filter="all"]').click();
const allAllocationRows = Number(await page.locator("#allocation-table").getAttribute("data-visible-rows"));
checks.push(check("Allocation filters preserve 9 plus reserve", reserveRows === 1 && systematicRows === 9 && allAllocationRows === 10 && reservePressed === "true", { all: allAllocationRows, reserve: reserveRows, reserve_pressed: reservePressed, systematic: systematicRows }));
const positionRows = Number(await page.locator("#position-table").getAttribute("data-visible-rows"));
const positionText = await page.locator("#position-table").textContent();
checks.push(check("Current positions retain mapped strategy attribution", positionRows === 29 && !positionText?.includes("AVAILABLE UNMAPPED") && !positionText?.includes("No accepted strategy"), { mapped_rows: positionRows, unmapped_visible: Boolean(positionText?.includes("UNMAPPED")) }));
await page.locator('[data-nav="risk"]').click();
const historicalGapRows = await page.locator("#historical-gap-table tbody tr").count();
const historicalGapHeading = await page.locator("#historical-gap-badge").textContent();
checks.push(check(
  "Current mapping is separated from historical General intervals",
  historicalGapRows === 11 && historicalGapHeading?.includes("11 unresolved"),
  { historical_gap_rows: historicalGapRows, label: historicalGapHeading, mapped_current_positions: positionRows },
));

await page.locator('[data-nav="candidates"]').click();
await page.locator('[data-tier-filter="Tier 1"]').click();
const tier1Rows = Number(await page.locator("#candidate-table").getAttribute("data-visible-rows"));
await page.locator("#candidate-search").fill("CAOS");
const caosRows = Number(await page.locator("#candidate-table").getAttribute("data-visible-rows"));
await page.locator("#candidate-search").fill("");
await page.locator('[data-tier-filter="all"]').click();
const allCandidateRows = Number(await page.locator("#candidate-table").getAttribute("data-visible-rows"));
checks.push(check("Frozen candidate tier filter is stable", tier1Rows === 5, { tier1: tier1Rows }));
checks.push(check("Frozen candidate search is stable", caosRows === 1, { query: "CAOS", rows: caosRows }));
checks.push(check("Frozen candidate appendix contains 33 rows", allCandidateRows === 33, { rows: allCandidateRows }));

await page.locator("#snapshot-button").click();
const toast = await page.locator("#toast").textContent();
checks.push(check("Snapshot provenance control responds", Boolean(toast?.includes("Report") && toast.includes("read-only")), toast));
const executionControls = await page.locator('button, a').evaluateAll((nodes) => nodes.filter((node) => /place order|submit order|execute trade|connect broker/i.test(node.textContent || "")).length);
checks.push(check("No execution control is present", executionControls === 0, { execution_controls: executionControls }));
checks.push(check("No console errors occur", consoleErrors.length === 0, consoleErrors));

const result = {
  all_passed: checks.every((item) => item.status === "PASS"),
  browser_version: browser.version(),
  checks,
  panel_id: panelId,
};
await browser.close();
await writeFile(output, `${JSON.stringify(result, null, 2)}\n`, "utf8");
const reread = JSON.parse(await readFile(output, "utf8"));
if (!reread.all_passed) process.exitCode = 1;
