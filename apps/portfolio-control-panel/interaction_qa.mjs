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
const includeCandidates = options["exclude-candidates"] !== "true";
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
await page.evaluate(() => {
  if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
});
await page.keyboard.press("Tab");
const firstKeyboardTarget = await page.evaluate(() => ({
  className: document.activeElement?.className,
  text: document.activeElement?.textContent?.trim(),
}));
await page.keyboard.press("Enter");
const skipTarget = await page.evaluate(() => document.activeElement?.id);
checks.push(check(
  "Keyboard skip link reaches the dashboard main region",
  String(firstKeyboardTarget.className).includes("skip-link")
    && firstKeyboardTarget.text === "Skip to dashboard"
    && skipTarget === "main-content",
  { first_keyboard_target: firstKeyboardTarget, skip_target: skipTarget },
));
const views = ["command", "capital", "risk", "attribution", "dependence", "weekly", "data"];
const requiredFactorLenses = [
  "ECONOMIC_FACTOR",
  "ETF_STYLE",
  "RETURN_INDUSTRY",
  "RETURN_SECTOR",
  "DYNAMIC_STYLE",
];
const requiredFactorEvidenceModes = ["LIVE_ONLY", "LIVE_PLUS_BACKTEST"];
const requiredAttributedStrategies = [
  "AI Factor [P123]",
  "GrizzlyBears VTMA",
  "QSI",
  "TT Portfolio",
  "Volatis Strategy",
];
const requiredCorrelationWindows = ["5", "21", "22", "63", "126", "130", "252", "260"];
const requiredDrawdownRegimes = ["either_drawdown", "both_drawdown"];
const requiredWeeklyGroups = [
  "ALL",
  "STRATEGY",
  "ECONOMIC_FACTOR_US",
  "ECONOMIC_FACTOR_EUROPE",
  "STYLE_US_EQUITY",
  "STYLE_MULTI_ASSET",
  "SECTOR",
  "INDUSTRY",
  "CORE_BENCHMARK",
];
const visited = [];
for (const view of views) {
  await page.locator(`[data-nav="${view}"]`).click();
  const active = await page.locator(`[data-view="${view}"]`).evaluate((node) => !node.hidden && node.classList.contains("is-active"));
  if (active) visited.push(view);
}
checks.push(check("All seven operator surfaces activate", visited.length === views.length, visited));
await page.locator('[data-nav="command"]').click();
await page.locator('[data-open-view="signals"]').first().click();
const signalsActive = await page.locator('[data-view="signals"]').evaluate((node) => !node.hidden && node.classList.contains("is-active"));
checks.push(check("Signals appendix activates from the governed operator route", signalsActive, { active: signalsActive }));

await page.locator('[data-nav="attribution"]').click();
const factorModeEvidence = [];
for (const mode of requiredFactorEvidenceModes) {
  const button = page.locator(`[data-factor-evidence-mode="${mode}"]`);
  const disabled = await button.isDisabled();
  if (disabled) {
    factorModeEvidence.push({ active: "false", disabled, mode, strategy_rows: [], table: null });
    continue;
  }
  await button.click();
  const strategyRows = await page.locator("#factor-model-table tbody tr").evaluateAll(
    (nodes, strategies) => strategies.map((strategy) => ({
      beta_visible: nodes.find((node) => node.textContent?.includes(strategy))?.textContent?.includes("β") ?? false,
      evidence: nodes.find((node) => node.textContent?.includes(strategy))?.textContent?.trim() ?? null,
      strategy,
    })),
    requiredAttributedStrategies,
  );
  factorModeEvidence.push({
    active: await button.getAttribute("aria-pressed"),
    boundary: (await page.locator("#factor-lens-boundary").textContent())?.trim(),
    disabled,
    mode,
    strategy_rows: strategyRows,
    table: (await page.locator("#factor-model-table").textContent())?.trim(),
  });
}
const hybridFactorEvidence = factorModeEvidence.find((item) => item.mode === "LIVE_PLUS_BACKTEST");
checks.push(check(
  "Factor analyses expose live-only and compatible-history modes without inventing prehistory",
  factorModeEvidence.length === requiredFactorEvidenceModes.length
    && factorModeEvidence.every((item) => item.active === "true"
      && item.disabled === false
      && item.boundary?.includes(item.mode)
      && item.table?.includes("Beta / loading")
      && item.table.includes("Evidence composition")
      && item.strategy_rows.every((row) => row.evidence))
    && hybridFactorEvidence?.strategy_rows.every((row) => /[1-9][0-9,]* backtest/.test(row.evidence ?? "")
      && !row.evidence?.includes("no version-matched backtest prehistory")),
  factorModeEvidence,
));
await page.locator('[data-factor-evidence-mode="LIVE_ONLY"]').click();
const factorLensButtons = await page.locator("[data-factor-lens]").count();
const factorLensEvidence = [];
for (const lens of requiredFactorLenses) {
  const button = page.locator(`[data-factor-lens="${lens}"]`);
  await button.click();
  const requestedBetaRows = await page.locator("#factor-model-table tbody tr").evaluateAll(
    (nodes, strategies) => strategies.map((strategy) => ({
      beta_visible: nodes.find((node) => node.textContent?.includes(strategy))?.textContent?.includes("β") ?? false,
      model_unavailable: nodes.find((node) => node.textContent?.includes(strategy))?.textContent?.includes("NO ACCEPTED MODEL") ?? false,
      strategy,
    })),
    requiredAttributedStrategies,
  );
  factorLensEvidence.push({
    active: await page.locator(`[data-factor-lens="${lens}"]`).getAttribute("aria-pressed"),
    boundary: (await page.locator("#factor-lens-boundary").textContent())?.trim(),
    chips: await page.locator("[data-factor-strategy]").count(),
    lens,
    requested_beta_rows: requestedBetaRows,
    rows: await page.locator("#factor-model-table tbody tr").count(),
  });
}
checks.push(check(
  "All five production decomposition lenses are rendered and exercised",
  factorLensButtons === requiredFactorLenses.length
    && factorLensEvidence.length === requiredFactorLenses.length
    && factorLensEvidence.every((item) => item.active === "true"
      && item.boundary?.includes("Beta:")
      && item.boundary.includes("never concatenated")
      && item.chips === 9
      && item.rows === 9
      && item.requested_beta_rows.every((row) => row.beta_visible || row.model_unavailable)),
  factorLensEvidence,
));
await page.locator('[data-factor-lens="DYNAMIC_STYLE"]').click();
const factorRows = await page.locator("#factor-model-table tbody tr").count();
const factorChips = await page.locator("[data-factor-strategy]").count();
await page.locator('[data-factor-strategy]:not(.is-unavailable):not(.is-diagnostic)').first().click();
const dynamicChart = await page.locator(".dynamic-chart").count();
const dynamicValidation = await page.locator("#factor-detail").textContent();
checks.push(check(
  "Clean-room dynamic FLS path and same-factor validation are rendered",
  dynamicChart === 1
    && dynamicValidation?.includes("MPI-informed and non-equivalent")
    && dynamicValidation?.includes("Same-factor static benchmark"),
  {
    chart_count: dynamicChart,
    methodology_boundary: dynamicValidation?.includes("MPI-informed and non-equivalent"),
    static_benchmark: dynamicValidation?.includes("Same-factor static benchmark"),
  },
));
await page.screenshot({
  fullPage: true,
  path: path.join(path.dirname(output), "dynamic-style.png"),
});
await page.locator("[data-factor-strategy]").last().click();
const unavailableFactorDetail = await page.locator("#factor-detail").textContent();
checks.push(check("Factor source and model states remain separate", factorRows === 9 && factorChips === 9 && unavailableFactorDetail?.includes("Source state") && unavailableFactorDetail.includes("Model state"), { factor_chips: factorChips, factor_rows: factorRows, state_separation_visible: unavailableFactorDetail?.includes("Source state") && unavailableFactorDetail.includes("Model state") }));
await page.locator('[data-factor-lens="ECONOMIC_FACTOR"]').click();
const economicBoundary = await page.locator("#factor-lens-boundary").textContent();
checks.push(check(
  "Economic, style, sector, and industry lenses remain separate",
  factorLensButtons === requiredFactorLenses.length && economicBoundary?.includes("EXCESS_AND_LONG_SHORT_FACTOR_RETURN") && economicBoundary?.includes("never concatenated"),
  { exercised_lenses: factorLensEvidence.map((item) => item.lens), factor_lenses: factorLensButtons, economic_boundary: economicBoundary },
));

await page.locator('[data-nav="dependence"]').click();
const liveModePressed = await page.locator('[data-evidence-mode="LIVE_ONLY"]').getAttribute("aria-pressed");
const liveModeBadge = await page.locator("#evidence-mode-badge").textContent();
const liveModeBoundary = await page.locator("#evidence-mode-boundary").textContent();
const hybridMode = page.locator('[data-evidence-mode="LIVE_PLUS_BACKTEST"]');
const hybridDisabled = await hybridMode.isDisabled();
let hybridBadge = null;
let hybridBoundary = null;
if (!hybridDisabled) {
  await hybridMode.click();
  hybridBadge = await page.locator("#evidence-mode-badge").textContent();
  hybridBoundary = await page.locator("#evidence-mode-boundary").textContent();
  await page.locator('[data-evidence-mode="LIVE_ONLY"]').click();
}
checks.push(check(
  "Dependence live and hybrid evidence disclosures are exercised",
  liveModePressed === "true"
    && liveModeBadge?.includes("LIVE ONLY")
    && liveModeBoundary?.includes("authoritative default")
    && (hybridDisabled
      || (hybridBadge?.includes("HYBRID BACKTEST + LIVE")
        && hybridBoundary?.includes("Exploratory hybrid view")
        && hybridBoundary?.includes("cannot change readiness"))),
  {
    hybrid_available: !hybridDisabled,
    hybrid_badge: hybridBadge,
    hybrid_boundary: hybridBoundary,
    live_badge: liveModeBadge,
    live_boundary: liveModeBoundary,
    live_default: liveModePressed,
  },
));

const matrixCells = await page.locator("[data-correlation-left]").count();
await page.locator('[data-matrix="stress"]').click();
const matrixTitle = await page.locator("#matrix-title").textContent();
await page.locator('[data-correlation-left]:not([disabled])').first().click();
const pairDetail = await page.locator("#correlation-detail").textContent();
checks.push(check("Dependence matrices and cell evidence are interactive", matrixCells === 81 && matrixTitle?.includes("Stress") && pairDetail?.includes("Observed status"), { cell_buttons: matrixCells, matrix_title: matrixTitle, pair_detail: Boolean(pairDetail?.includes("Observed status")) }));
const horizonEvidence = [];
for (const window of requiredCorrelationWindows) {
  await page.locator(`[data-correlation-window="${window}"]`).click();
  const detail = await page.locator("#correlation-detail").textContent();
  horizonEvidence.push({
    active: await page.locator(`[data-correlation-window="${window}"]`).getAttribute("aria-pressed"),
    detail_has_selected_period: detail?.includes("Selected period") && detail.includes("Selected sample"),
    title: (await page.locator("#matrix-title").textContent())?.trim(),
    window,
  });
}
const drawdownEvidence = [];
for (const regime of requiredDrawdownRegimes) {
  await page.locator(`[data-correlation-window="${regime}"]`).click();
  const detail = await page.locator("#correlation-detail").textContent();
  drawdownEvidence.push({
    active: await page.locator(`[data-correlation-window="${regime}"]`).getAttribute("aria-pressed"),
    detail_has_selected_period: detail?.includes("Selected period") && detail.includes("Selected sample"),
    regime,
    title: (await page.locator("#matrix-title").textContent())?.trim(),
  });
}
checks.push(check(
  "All observed windows and both true drawdown regimes are exercised",
  horizonEvidence.length === requiredCorrelationWindows.length
    && horizonEvidence.every((item) => item.active === "true" && item.title?.includes(`${item.window} sessions`) && item.detail_has_selected_period)
    && drawdownEvidence.length === requiredDrawdownRegimes.length
    && drawdownEvidence.every((item) => item.active === "true"
      && item.title?.includes(item.regime === "either_drawdown" ? "either strategy in drawdown" : "both strategies in drawdown")
      && item.detail_has_selected_period),
  { drawdown_regimes: drawdownEvidence, observed_windows: horizonEvidence },
));

await page.locator('[data-nav="weekly"]').click();
const currentWeekIncluded = await page.locator("#weekly-current-toggle").isChecked();
const weeklyGroupEvidence = [];
for (const group of requiredWeeklyGroups) {
  await page.locator(`[data-weekly-group="${group}"]`).click();
  const cell = page.locator("[data-weekly-series]").first();
  await cell.click();
  weeklyGroupEvidence.push({
    active: await page.locator(`[data-weekly-group="${group}"]`).getAttribute("aria-pressed"),
    detail: (await page.locator("#weekly-detail").textContent())?.trim(),
    group,
    rows: await page.locator("#weekly-table tbody tr").count(),
  });
}
const weeklyRows = weeklyGroupEvidence.find((item) => item.group === "STRATEGY")?.rows;
const benchmarkRows = weeklyGroupEvidence.find((item) => item.group === "CORE_BENCHMARK")?.rows;
checks.push(check(
  "Every weekly display group is rendered and exercised",
  weeklyGroupEvidence.length === requiredWeeklyGroups.length
    && weeklyGroupEvidence.every((item) => item.active === "true" && item.rows > 0 && item.detail?.includes("Return basis") && item.detail.includes("Calendar week")),
  weeklyGroupEvidence,
));
await page.locator('[data-weekly-group="STRATEGY"]').click();
await page.locator('[data-weekly-mode="LIVE_ONLY"]').click();
await page.locator("[data-weekly-series]").first().click();
const weeklyLiveDetail = await page.locator("#weekly-detail").textContent();
const weeklyLivePressed = await page.locator('[data-weekly-mode="LIVE_ONLY"]').getAttribute("aria-pressed");
const weeklyHybridMode = page.locator('[data-weekly-mode="LIVE_PLUS_BACKTEST"]');
const weeklyHybridDisabled = await weeklyHybridMode.isDisabled();
const weeklyHybridTitle = await weeklyHybridMode.getAttribute("title");
let weeklyModePressed = await weeklyHybridMode.getAttribute("aria-pressed");
let weeklyHybridDetail = null;
if (!weeklyHybridDisabled) {
  await weeklyHybridMode.click();
  weeklyModePressed = await weeklyHybridMode.getAttribute("aria-pressed");
  await page.locator("[data-weekly-series]").first().click();
  weeklyHybridDetail = await page.locator("#weekly-detail").textContent();
}
checks.push(check(
  "Weekly returns disclose live and hybrid strategy evidence without enabling the partial week",
  weeklyRows === 9
    && benchmarkRows === 9
    && currentWeekIncluded === false
    && weeklyLivePressed === "true"
    && weeklyLiveDetail?.includes("Live only")
    && weeklyLiveDetail.includes("Accepted observations")
    && (weeklyHybridDisabled
      ? weeklyModePressed === "false" && weeklyHybridTitle?.includes("not PROVEN_COMPATIBLE")
      : weeklyModePressed === "true"
        && weeklyHybridDetail?.includes("Live plus backtest")
        && weeklyHybridDetail.includes("Accepted observations")),
  {
    benchmark_rows: benchmarkRows,
    hybrid_available: !weeklyHybridDisabled,
    hybrid_detail_visible: weeklyHybridDetail?.includes("Live plus backtest"),
    hybrid_pressed: weeklyModePressed,
    hybrid_title: weeklyHybridTitle,
    live_detail_visible: weeklyLiveDetail?.includes("Live only"),
    live_pressed: weeklyLivePressed,
    partial_week_enabled: currentWeekIncluded,
    strategy_rows: weeklyRows,
  },
));

await page.locator('[data-nav="capital"]').click();
const targetStateText = await page.locator("#allocation-table").textContent();
checks.push(check(
  "Allocation target and drift fail closed when no target is admitted",
  targetStateText?.includes("No accepted target decision") && !targetStateText.includes("Target inferred"),
  { no_accepted_target_visible: targetStateText?.includes("No accepted target decision") },
));
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
checks.push(check("Current positions retain mapped strategy attribution", positionRows > 0 && !positionText?.includes("AVAILABLE UNMAPPED") && !positionText?.includes("No accepted strategy"), { mapped_rows: positionRows, unmapped_visible: Boolean(positionText?.includes("UNMAPPED")) }));
const accountOptions = await page.locator("#context-account option").evaluateAll((nodes) => nodes.map((node) => ({ label: node.textContent?.trim(), value: node.value })));
const firstPositionAccount = await page.evaluate(() => window.PORTFOLIO_DATA.positions[0]?.account_id);
await page.locator("#context-account").selectOption(firstPositionAccount);
const selectedAccountLabel = await page.locator("#context-account option:checked").textContent();
const accountContextSummary = await page.locator("#context-summary").textContent();
const filteredPositionRows = Number(await page.locator("#position-table").getAttribute("data-visible-rows"));
const lifecycleAccountRows = await page.locator("#lifecycle-table tbody tr[data-account-id]").evaluateAll((nodes) => nodes.map((node) => ({ account: node.dataset.accountId, label: node.cells[0]?.textContent?.trim() })));
const positionAccountRows = await page.locator("#position-table tbody tr[data-account-id]").evaluateAll((nodes) => nodes.map((node) => ({ account: node.dataset.accountId, label: node.cells[2]?.textContent?.trim() })));
await page.locator('[data-nav="risk"]').click();
const persistentAccount = await page.locator("#context-account").inputValue();
const riskAccountRows = await page.locator("#performance-table tbody tr[data-account-id], #historical-gap-table tbody tr[data-account-id]").evaluateAll((nodes) => nodes.map((node) => ({ account: node.dataset.accountId, label: node.cells[0]?.textContent?.trim() })));
checks.push(check(
  "Privacy-safe account context filters lifecycle, positions, and performance",
  accountOptions.length === 20
    && new Set(accountOptions.map((item) => item.label)).size === 20
    && firstPositionAccount
    && persistentAccount === firstPositionAccount
    && selectedAccountLabel
    && selectedAccountLabel !== "Provider account"
    && accountContextSummary?.includes(selectedAccountLabel)
    && filteredPositionRows > 0
    && lifecycleAccountRows.length > 0
    && positionAccountRows.length > 0
    && riskAccountRows.length > 0
    && [...lifecycleAccountRows, ...positionAccountRows, ...riskAccountRows].every((row) => row.account === firstPositionAccount && row.label === selectedAccountLabel),
  {
    account_options: accountOptions.length,
    filtered_positions: filteredPositionRows,
    lifecycle_rows: lifecycleAccountRows.length,
    performance_rows: riskAccountRows.length,
    persisted_account: persistentAccount,
    selected_account: firstPositionAccount,
    selected_label: selectedAccountLabel,
  },
));
await page.locator("#context-reset").click();
await page.locator('[data-nav="capital"]').click();
const firstStrategy = await page.locator("#context-strategy option").nth(1).getAttribute("value");
await page.locator("#context-strategy").selectOption(firstStrategy);
const contextualSummary = await page.locator("#context-summary").textContent();
await page.locator('[data-nav="risk"]').click();
const persistentStrategy = await page.locator("#context-strategy").inputValue();
checks.push(check(
  "Account and strategy context persists across surfaces",
  firstStrategy && persistentStrategy === firstStrategy && contextualSummary?.includes(firstStrategy),
  { selected_strategy: firstStrategy, persisted_strategy: persistentStrategy, summary: contextualSummary },
));
await page.locator("#context-reset").click();
await page.locator('[data-nav="risk"]').click();
const historicalGapRows = await page.locator("#historical-gap-table tbody tr").count();
const historicalGapHeading = await page.locator("#historical-gap-badge").textContent();
const volatisPerformance = await page.locator("#performance-table tbody tr", { hasText: "Volatis" }).textContent();
const robuxioPerformance = await page.locator("#performance-table tbody tr", { hasText: "Robuxio" }).textContent();
checks.push(check(
  "Volatis and Robuxio expose their accepted account-live return paths",
  volatisPerformance?.includes("canonical account-live returns")
    && volatisPerformance.includes("Available account-live return path")
    && robuxioPerformance?.includes("canonical account-live returns")
    && robuxioPerformance.includes("Available account-live return path"),
  { robuxio: robuxioPerformance, volatis: volatisPerformance },
));
checks.push(check(
  "Reserve assignments are excluded from historical systematic mapping gaps",
  historicalGapRows === 7 && historicalGapHeading?.includes("7 unresolved"),
  { historical_gap_rows: historicalGapRows, label: historicalGapHeading, mapped_current_positions: positionRows },
));
await page.locator('[data-nav="capital"]').click();
const reserveLifecycleRows = await page.locator("#lifecycle-table tbody tr", { hasText: "Reserve — strategy not applicable" }).count();
checks.push(check(
  "Reserve lifecycle intervals are labeled not applicable rather than unmapped strategy gaps",
  reserveLifecycleRows === 4,
  { reserve_rows: reserveLifecycleRows },
));

await page.locator('[data-nav="data"]').click();
const remediationButton = page.locator("#remediation-table [data-focus-coverage]").first();
const remediationItem = await remediationButton.getAttribute("data-focus-coverage");
await remediationButton.click();
await page.waitForFunction((item) => (
  document.activeElement?.getAttribute("data-coverage-item") === item
), remediationItem);
const remediationFocus = await page.evaluate(() => ({
  active_item: document.activeElement?.getAttribute("data-coverage-item"),
  active_view: document.querySelector('[data-view="data"]')?.classList.contains("is-active"),
}));
checks.push(check(
  "Data-health remediation routes focus the exact producing exception",
  Boolean(remediationItem)
    && remediationFocus.active_view === true
    && remediationFocus.active_item === remediationItem,
  { requested_item: remediationItem, ...remediationFocus },
));
if (includeCandidates) {
  await page.locator("#candidate-appendix-button").click();
  const candidateActive = await page.locator('[data-view="candidates"]').evaluate((node) => !node.hidden && node.classList.contains("is-active"));
  await page.locator('[data-tier-filter="Tier 1"]').click();
  const tier1Rows = Number(await page.locator("#candidate-table").getAttribute("data-visible-rows"));
  await page.locator("#candidate-search").fill("CAOS");
  const caosRows = Number(await page.locator("#candidate-table").getAttribute("data-visible-rows"));
  await page.locator("#candidate-search").fill("");
  await page.locator('[data-tier-filter="all"]').click();
  const allCandidateRows = Number(await page.locator("#candidate-table").getAttribute("data-visible-rows"));
  checks.push(check("Frozen candidate tier filter is stable", tier1Rows === 5, { tier1: tier1Rows }));
  checks.push(check("Frozen candidate search is stable", caosRows === 1, { query: "CAOS", rows: caosRows }));
  checks.push(check("Frozen candidate appendix remains separate from primary navigation", candidateActive && allCandidateRows === 33 && await page.locator('[data-nav="candidates"]').count() === 0, { active_from_appendix_link: candidateActive, primary_nav_buttons: await page.locator('[data-nav="candidates"]').count(), rows: allCandidateRows }));
}

await page.locator("#snapshot-button").click();
const toast = await page.locator("#toast").textContent();
checks.push(check("Snapshot provenance control responds", Boolean(toast?.includes("Report") && toast.includes("read-only")), toast));
const executionControls = await page.locator('button, a').evaluateAll((nodes) => nodes.filter((node) => /place order|submit order|execute trade|connect broker/i.test(node.textContent || "")).length);
checks.push(check("No execution control is present", executionControls === 0, { execution_controls: executionControls }));
const unnamedControls = await page.locator("button, input, select, textarea, a[href]").evaluateAll((nodes) => nodes.map((node) => {
  const labels = "labels" in node && node.labels
    ? Array.from(node.labels).map((label) => label.textContent?.trim()).filter(Boolean)
    : [];
  const name = node.getAttribute("aria-label")
    || node.getAttribute("title")
    || labels.join(" ")
    || node.textContent?.trim()
    || node.getAttribute("placeholder")
    || "";
  return { id: node.id, name, tag: node.tagName };
}).filter((item) => !item.name));
const mainRegions = await page.locator("main").count();
checks.push(check(
  "Interactive controls have accessible names and one main region",
  unnamedControls.length === 0 && mainRegions === 1,
  { main_regions: mainRegions, unnamed_controls: unnamedControls },
));

await page.setViewportSize({ width: 390, height: 844 });
await page.locator('[data-nav="command"]').click();
const mobileFacts = await page.locator("#portfolio-as-of, #freshness-as-of, #coverage-status").evaluateAll((nodes) => nodes.map((node) => ({
  id: node.id,
  text: node.textContent?.trim(),
  visible: Boolean(node.getClientRects().length) && getComputedStyle(node).visibility !== "hidden",
})));
checks.push(check(
  "Mobile header retains portfolio date, freshness date, and coverage",
  mobileFacts.length === 3 && mobileFacts.every((item) => item.visible && item.text && item.text !== "Loading…"),
  mobileFacts,
));
const touchSelector = '.nav-item, .segment, .ghost-button, .strategy-chip, .text-action, .weekly-cell, .weekly-toggle, .disclosure-panel summary, .search-control input, .context-bar select';
const mobileTouchEvidence = [];
for (const view of views) {
  await page.locator(`[data-nav="${view}"]`).click();
  const undersized = await page.locator(touchSelector).evaluateAll((nodes) => nodes.filter((node) => {
    const style = getComputedStyle(node);
    const bounds = node.getBoundingClientRect();
    const visible = style.display !== "none" && style.visibility !== "hidden" && bounds.width > 0 && bounds.height > 0;
    return visible && (bounds.height < 44 || bounds.width < 44);
  }).map((node) => ({
    class_name: node.className,
    height: node.getBoundingClientRect().height,
    text: node.textContent?.trim(),
    width: node.getBoundingClientRect().width,
  })));
  mobileTouchEvidence.push({ undersized, view });
}
await page.locator('[data-nav="command"]').click();
await page.locator('[data-open-view="signals"]').first().click();
const undersizedSignals = await page.locator(touchSelector).evaluateAll((nodes) => nodes.filter((node) => {
  const style = getComputedStyle(node);
  const bounds = node.getBoundingClientRect();
  const visible = style.display !== "none" && style.visibility !== "hidden" && bounds.width > 0 && bounds.height > 0;
  return visible && (bounds.height < 44 || bounds.width < 44);
}).map((node) => ({
  class_name: node.className,
  height: node.getBoundingClientRect().height,
  text: node.textContent?.trim(),
  width: node.getBoundingClientRect().width,
})));
mobileTouchEvidence.push({ undersized: undersizedSignals, view: "signals" });
checks.push(check(
  "All eight non-Candidate mobile surfaces meet the 44px target",
  mobileTouchEvidence.every((item) => item.undersized.length === 0),
  mobileTouchEvidence,
));
await page.locator('[data-nav="data"]').click();
await page.locator("#operator-glossary summary").click();
const glossaryText = await page.locator("#operator-glossary").textContent();
checks.push(check(
  "Operator glossary defines the governed control and methodology terms",
  ["HWM", "TWR / MWR", "Evidence state", "Model state", "Gate state", "MPI-informed, non-equivalent"].every((term) => glossaryText?.includes(term)),
  glossaryText,
));
await page.locator('[data-nav="data"]').click();
const mobileRemediation = page.locator("#remediation-table [data-focus-coverage]").first();
const mobileRemediationItem = await mobileRemediation.getAttribute("data-focus-coverage");
await mobileRemediation.click();
await page.waitForFunction((item) => {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement)
      || active.getAttribute("data-coverage-item") !== item) return false;
  const bounds = active.getBoundingClientRect();
  return bounds.top >= 0 && bounds.bottom <= window.innerHeight;
}, mobileRemediationItem);
const mobileRemediationEvidence = await page.evaluate(() => ({
  active_item: document.activeElement?.getAttribute("data-coverage-item"),
  in_view: document.activeElement instanceof HTMLElement
    && document.activeElement.getBoundingClientRect().top >= 0
    && document.activeElement.getBoundingClientRect().bottom <= window.innerHeight,
}));
checks.push(check(
  "Mobile remediation action remains operable and focused",
  mobileRemediationEvidence.active_item === mobileRemediationItem
    && mobileRemediationEvidence.in_view === true,
  { requested_item: mobileRemediationItem, ...mobileRemediationEvidence },
));
await page.locator('[data-nav="risk"]').click();
const mobileHwmColumns = await page.locator("#hwm-status .validation-card").first().evaluate((node) => getComputedStyle(node).gridTemplateColumns.split(" ").filter(Boolean).length);
checks.push(check("Mobile HWM governor uses one readable column", mobileHwmColumns === 1, { rendered_columns: mobileHwmColumns }));
await page.locator('[data-nav="command"]').click();
await page.locator('[data-open-view="signals"]').first().click();
const robuxioEvidence = await page.locator("#implementation-table tbody tr", { hasText: "Robuxio" }).textContent();
checks.push(check(
  "Provider-custodied Robuxio account NAV is labeled account-live",
  robuxioEvidence?.includes("Account-live available") && !robuxioEvidence?.includes("Account-live unavailable"),
  robuxioEvidence,
));
checks.push(check("No console errors occur", consoleErrors.length === 0, consoleErrors));

const result = {
  all_passed: checks.every((item) => item.status === "PASS"),
  browser_version: browser.version(),
  checks,
  panel_id: panelId,
  scope: includeCandidates ? "FULL" : "NON_CANDIDATE",
};
await browser.close();
await writeFile(output, `${JSON.stringify(result, null, 2)}\n`, "utf8");
const reread = JSON.parse(await readFile(output, "utf8"));
if (!reread.all_passed) process.exitCode = 1;
