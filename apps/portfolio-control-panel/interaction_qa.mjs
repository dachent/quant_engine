#!/usr/bin/env node
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
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

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function isCalendarDate(value) {
  const parsed = ISO_DATE.test(value || "") ? new Date(`${value}T00:00:00Z`) : null;
  return Boolean(parsed && !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value);
}

function acceptedCutoff(value) {
  if (!isCalendarDate(value)) {
    throw new Error("--expected-calculation-cutoff must be an ISO calendar date");
  }
  return value;
}

function calculationDateFacts(manifest) {
  const facts = [];
  for (const row of manifest?.rows || []) {
    for (const field of ["start", "end"]) {
      if (typeof row?.[field] === "string") facts.push({ account_id: row.account_id, field, value: row[field] });
    }
    for (const link of row?.observation_trace?.links || []) {
      for (const field of ["start_date", "end_date"]) {
        if (typeof link?.[field] === "string") facts.push({ account_id: row.account_id, field: `observation_trace.${field}`, value: link[field] });
      }
    }
    for (const observation of row?.nav_observation_trace || []) {
      if (typeof observation?.date === "string") facts.push({ account_id: row.account_id, field: "nav_observation_trace.date", value: observation.date });
    }
    for (const flow of row?.capital_flow_trace || []) {
      for (const field of ["date", "event_date", "nav_recognition_date"]) {
        if (typeof flow?.[field] === "string") facts.push({ account_id: row.account_id, field: `capital_flow_trace.${field}`, value: flow[field] });
      }
    }
  }
  return facts;
}

const EXPECTED_AUTHORITY_LABELS = Object.freeze({
  INDEPENDENT_EXACT_REBUILD: "Independent exact rebuild",
  VERIFIED_GENERATION_BUNDLE_EXACT_BYTES: "Report-attested static publication — verified generation bundle exact bytes",
});
const VERIFIED_BUNDLE_PROFILE = "VERIFIED_GENERATION_BUNDLE_EXACT_BYTES";
const VERIFIED_BUNDLE_REPORT_STATUS = "REPORT_ATTESTED_TO_EXACT_BUNDLE_BYTES";
const VERIFIED_BUNDLE_REPORT_METHOD = "READ_GENERATION_BUNDLE_EXACT_BYTES_AND_FINANCIAL_MVP_BINDER";
const VERIFIED_BUNDLE_PRODUCER_VERIFICATION = "NOT_PERFORMED";
const VERIFIED_BUNDLE_PRODUCER_PUBLICATION = "NOT_READY";

const options = args(process.argv.slice(2));
if (!options.html || !options.output || !process.env.PLAYWRIGHT_MODULE) {
  throw new Error("--html, --output, and PLAYWRIGHT_MODULE are required");
}
const expectedCalculationCutoff = acceptedCutoff(options["expected-calculation-cutoff"]);
const moduleUrl = pathToFileURL(path.resolve(process.env.PLAYWRIGHT_MODULE)).href;
const { chromium } = await import(moduleUrl);
const html = path.resolve(options.html);
const output = path.resolve(options.output);
const panelId = path.basename(path.dirname(html));
const externalManifestPath = options["epoch-manifest"] ? path.resolve(options["epoch-manifest"]) : null;
const externalManifestBytes = externalManifestPath ? await readFile(externalManifestPath) : null;
const externalManifest = externalManifestBytes ? JSON.parse(externalManifestBytes.toString("utf8")) : null;
const externalManifestSha256 = externalManifestBytes
  ? createHash("sha256").update(externalManifestBytes).digest("hex")
  : null;
const candidateRecordBytes = await readFile(path.join(path.dirname(html), "candidate.json"));
const candidateRecord = JSON.parse(candidateRecordBytes.toString("utf8"));
const candidateRecordSha256 = createHash("sha256").update(candidateRecordBytes).digest("hex");
const dashboardData = JSON.parse(await readFile(path.join(path.dirname(html), "data", "dashboard_data.json"), "utf8"));
const expectedFactorSeries = dashboardData.dependence_analysis.series_ids.length;
const expectedCurrentStrategies = dashboardData.strategies.length;
// The account selector lists every account the panel scopes to, plus the
// "all accounts" entry.  Bind that to the census the panel actually carries
// rather than to a literal: the current-product perimeter decides how many
// accounts appear, and a fixed count silently encodes one perimeter vintage.
const expectedAccountOptions = dashboardData.account_summary.length + 1;
const expectedReserveAssignments = dashboardData.performance.filter((row) => row.strategy_mapping_disposition === "RESERVE_NOT_APPLICABLE").length;
const performanceLineageMismatch = dashboardData.meta?.performance_lineage?.status === "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH";
const includeCandidates = options["exclude-candidates"] !== "true";
const epochOnly = options["epoch-only"] === "true";
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
if (!epochOnly) {
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
  await page.locator('[data-nav="data"]').click();
  const downloads = [
    "downloads/portfolio-report-data.json",
    ...(dashboardData.invested_capital ? ["downloads/portfolio-report.xlsx"] : []),
  ];
  const downloadDirectory = path.join(path.dirname(output), "downloads");
  await mkdir(downloadDirectory, { recursive: true });
  for (const relative of downloads) {
    const link = page.locator(`a[download][href="${relative}"]`);
    const pendingDownload = page.waitForEvent("download");
    await link.click();
    const download = await pendingDownload;
    const destination = path.join(downloadDirectory, path.basename(relative));
    await download.saveAs(destination);
    const downloaded = await readFile(destination);
    const actualSha256 = createHash("sha256").update(downloaded).digest("hex");
    // A download that is written beside the page is checked against the
    // candidate's file inventory. The workbook is not: it is carried only as
    // base64 payload elements inside the page, because an .xlsx sitting in a
    // published generation gets rewritten in place by the SharePoint sync
    // client, which silently changes the bytes of a content-addressed
    // artifact. Its expected digest therefore comes from the payload the page
    // actually carries. That the payload IS the selected workbook is already
    // established upstream by the panel's `report_workbook` binding and the
    // workbook parity receipt; what remains to prove here is that the download
    // path reconstructs it faithfully, which is what this check is named for.
    const expectedSha256 = Object.prototype.hasOwnProperty.call(
      candidateRecord.artifact_sha256, relative,
    )
      ? candidateRecord.artifact_sha256[relative]
      : await page.evaluate(async (href) => {
        const link = document.querySelector(`a[download][href="${href}"]`);
        const prefix = link?.getAttribute("data-download-payload-prefix");
        const count = Number(link?.getAttribute("data-download-chunk-count"));
        if (!prefix || !Number.isInteger(count) || count < 1) return null;
        let base64 = "";
        for (let index = 0; index < count; index += 1) {
          const element = document.getElementById(
            `${prefix}-${String(index).padStart(5, "0")}`,
          );
          if (!element) return null;
          base64 += element.textContent;
        }
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index += 1) {
          bytes[index] = binary.charCodeAt(index);
        }
        const digest = await crypto.subtle.digest("SHA-256", bytes);
        return Array.from(new Uint8Array(digest))
          .map((byte) => byte.toString(16).padStart(2, "0"))
          .join("");
      }, relative);
    checks.push(check(
      `Download delivers the selected ${path.basename(relative)}`,
      expectedSha256 !== null && actualSha256 === expectedSha256,
      {
        relative_path: relative,
        sha256: actualSha256,
        size_bytes: downloaded.length,
        expected_source: Object.prototype.hasOwnProperty.call(
          candidateRecord.artifact_sha256, relative,
        ) ? "candidate_artifact_inventory" : "embedded_payload",
      },
    ));
  }
  await page.locator('[data-nav="command"]').click();
}
if (externalManifestPath) {
  const payloadMatchesExternalManifest = JSON.stringify(dashboardData.account_manifest) === JSON.stringify(externalManifest);
  checks.push(check(
    "Panel payload equals the independently supplied epoch manifest",
    payloadMatchesExternalManifest,
    {
      external_manifest_path: externalManifestPath,
      external_manifest_sha256: externalManifestSha256,
      external_manifest_size: externalManifestBytes.byteLength,
      payload_equal: payloadMatchesExternalManifest,
    },
  ));
}
function exactDecimal(value) {
  const source = typeof value === "string" && /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value) ? value : null;
  if (source == null) return null;
  const negative = source.startsWith("-");
  const unsigned = negative ? source.slice(1) : source;
  const [whole, fraction = ""] = unsigned.split(".");
  return { magnitude: BigInt(`${whole}${fraction}`), negative, scale: fraction.length };
}
function exactFixed(value, places = 2, scaleAdjustment = 0) {
  const parsed = exactDecimal(value);
  if (parsed == null) return "—";
  const scale = parsed.scale + scaleAdjustment;
  let rounded;
  if (scale <= places) {
    rounded = parsed.magnitude * (10n ** BigInt(places - scale));
  } else {
    const divisor = 10n ** BigInt(scale - places);
    rounded = parsed.magnitude / divisor;
    const remainder = parsed.magnitude % divisor;
    const doubled = remainder * 2n;
    if (doubled > divisor || (doubled === divisor && rounded % 2n !== 0n)) rounded += 1n;
  }
  let text = rounded.toString().padStart(places + 1, "0");
  const decimals = places ? text.slice(-places) : "";
  const integer = places ? text.slice(0, -places) : text;
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const signed = parsed.negative && rounded !== 0n ? `-${grouped}` : grouped;
  return places ? `${signed}.${decimals}` : signed;
}
const exactMoney = (value) => exactFixed(value, 2);
const exactPct = (value) => {
  const fixed = exactFixed(value, 2, -2);
  return fixed === "—" ? fixed : `${fixed}%`;
};
const epochManifest = externalManifest ?? dashboardData.account_manifest;
const epochDom = await page.locator("#account-manifest-table").evaluate((table) => ({
  caption: table.querySelector("caption")?.textContent?.trim() ?? null,
  headers: Array.from(table.querySelectorAll("thead th"), (node) => node.textContent?.trim()),
  rows: Array.from(table.querySelectorAll("tbody tr"), (row) => ({
    accountId: row.getAttribute("data-account-id"),
    cells: Array.from(row.querySelectorAll("td"), (cell) => ({
      primary: cell.childNodes[0]?.textContent?.trim() ?? "",
      secondary: cell.querySelector(".cell-secondary")?.textContent?.trim() ?? null,
      text: cell.textContent?.trim() ?? "",
    })),
  })),
}));
const epochAuthority = await page.locator("#account-manifest-authority").textContent();
const epochLegend = await page.locator("#account-manifest-legend").textContent();
const epochDisclosure = await page.locator("#account-manifest-disclosure-list").textContent();
const epochHeaders = [
  "Account", "Strategy", "Observed interval", "Invested", "Withdrawn", "Net capital",
  "Current", "Gain", "RONC", "Ann. RONC", "Observed return", "Observed HWM", "Observed Max DD",
];
const authorityProfile = dashboardData.meta.account_manifest_authority_profile;
const authorityProfileKnown = Object.prototype.hasOwnProperty.call(EXPECTED_AUTHORITY_LABELS, authorityProfile);
const expectedAuthority = authorityProfileKnown ? EXPECTED_AUTHORITY_LABELS[authorityProfile] : null;
let epochValuesMatch = false;
const epochEvidence = { authority: epochAuthority, caption: epochDom.caption, headers: epochDom.headers, rows: epochDom.rows };
if (!epochManifest || !Array.isArray(epochManifest.rows)) {
  epochValuesMatch = epochAuthority?.trim() === "Manifest not supplied — non-releaseable"
    && epochDom.headers.join("|") === "Epoch manifest unavailable"
    && epochDom.caption === "Epoch manifest unavailable"
    && epochDom.rows.length === 1
    && epochDom.rows[0].cells[0].text === "No account-manifest evidence was supplied to this build";
} else {
  const rowMatches = epochManifest.rows.length === epochDom.rows.length && epochManifest.rows.every((row, index) => {
    const rendered = epochDom.rows[index];
    if (!rendered || rendered.accountId !== row.account_id || rendered.cells.length !== 13) return false;
    const expectedLabel = Number(epochManifest.schema_version) >= 9
      ? row.account_label
      : (row.account_label || rendered.cells[0].text);
    const lastFourVisible = Number(epochManifest.schema_version) < 9
      || row.last_four == null
      || rendered.cells[0].text.includes(String(row.last_four));
    if (!expectedLabel || rendered.cells[0].text !== expectedLabel || !lastFourVisible || rendered.cells[1].text !== row.strategy_id) return false;
    if (row.status !== "OK") {
      const blocker = row.status === "FLOWS_AFTER_CLOSING_NAV"
        ? row.status
        : `${row.status}: ${row.blocking_reason || "blocking reason unavailable"}`;
      return rendered.cells[2].primary === row.effective_start
        && rendered.cells[2].secondary === `${row.effective_end || "open"} · ${blocker}`
        && rendered.cells.slice(3).every((cell) => cell.text === "—");
    }
    const expectedNumbers = [
      exactMoney(row.invested), exactMoney(row.withdrawn), exactMoney(row.net_capital),
      exactMoney(row.current_value), exactMoney(row.gain), exactPct(row.return_on_net_capital),
      exactPct(row.annualized_return_on_net_capital), exactPct(row.observation_linked_return),
      exactFixed(row.observed_hwm_index, 2), exactPct(row.observed_max_drawdown),
    ];
    return rendered.cells[2].primary === row.start
      && rendered.cells[2].secondary === row.end
      && rendered.cells.slice(3).every((cell, cellIndex) => cell.text === expectedNumbers[cellIndex]);
  });
  epochValuesMatch = authorityProfileKnown
    && epochAuthority?.trim() === expectedAuthority
    && epochDom.caption === `Epoch manifest by account and strategy — monetary values ${epochManifest.accounting_currency}`
    && epochDom.headers.join("|") === epochHeaders.join("|")
    && rowMatches;
}
const manifestStrategyIds = new Set((epochManifest?.rows || []).map((row) => row.strategy_id));
const currentManifestShapeMatch = !epochManifest || !Array.isArray(epochManifest.rows)
  ? false
  : epochManifest.rows.length === 13
    && manifestStrategyIds.size === 7
    && epochManifest.rows.every((row) => !/echo|composer/i.test(`${row.strategy_id || ""} ${row.account_label || ""}`));
const manifestCutoff = epochManifest?.calculation_cutoff_date;
const boundLedgerIdentity = epochManifest?.source_identities?.classified_flows?.ledger_identity;
const boundLedgerArtifact = boundLedgerIdentity?.artifact;
const providerCaptureDate = boundLedgerArtifact?.as_of_date;
const manifestCutoffDisplayMatch = !epochManifest || !Array.isArray(epochManifest.rows)
  ? false
  : Number(epochManifest.schema_version) < 9
    || (manifestCutoff === expectedCalculationCutoff
      && typeof boundLedgerIdentity === "object"
      && typeof boundLedgerArtifact === "object"
      && isCalendarDate(providerCaptureDate)
      && expectedCalculationCutoff <= providerCaptureDate
      && epochLegend?.includes(`Calculations through ${manifestCutoff}.`)
      && epochLegend.includes(`Artifact/provider capture date: ${providerCaptureDate}.`));
const calculationFactsAfterAcceptedCutoff = calculationDateFacts(epochManifest)
  .filter((fact) => !isCalendarDate(fact.value) || fact.value > expectedCalculationCutoff);
checks.push(check(
  "Current epoch manifest keeps the exact 13-row / 7-strategy subject and exclusions",
  currentManifestShapeMatch,
  { rows: epochManifest?.rows?.length, strategies: manifestStrategyIds.size, excluded_echo_or_composer: !currentManifestShapeMatch },
));
checks.push(check(
  "Epoch manifest legend distinguishes calculation cutoff from provider capture",
  manifestCutoffDisplayMatch,
  {
    bound_ledger_artifact_id: boundLedgerArtifact?.artifact_id,
    cutoff: manifestCutoff,
    expected_cutoff: expectedCalculationCutoff,
    ledger_identity_generation_id: boundLedgerIdentity?.generation_id,
    legend: epochLegend,
    provider_capture_date: providerCaptureDate,
  },
));
checks.push(check(
  "No epoch calculation fact extends beyond the explicit accepted cutoff",
  calculationFactsAfterAcceptedCutoff.length === 0,
  { expected_cutoff: expectedCalculationCutoff, violations: calculationFactsAfterAcceptedCutoff },
));
checks.push(check(
  "Epoch manifest authority uses the closed known-profile label mapping",
  !epochManifest || !Array.isArray(epochManifest.rows)
    ? epochAuthority?.trim() === "Manifest not supplied — non-releaseable"
    : authorityProfileKnown && epochAuthority?.trim() === expectedAuthority,
  { known: authorityProfileKnown, profile: authorityProfile, expected: expectedAuthority, rendered: epochAuthority?.trim() },
));
const accountManifestVerification = dashboardData.meta.account_manifest_verification || {};
const producerReadiness = accountManifestVerification.producer_readiness || {};
const verifiedBundleVerificationMatch = authorityProfile !== VERIFIED_BUNDLE_PROFILE || (
  accountManifestVerification.status === VERIFIED_BUNDLE_REPORT_STATUS
    && accountManifestVerification.method === VERIFIED_BUNDLE_REPORT_METHOD
    && accountManifestVerification.publication_readiness === "NOT_READY"
    && producerReadiness.independent_authority_verification === VERIFIED_BUNDLE_PRODUCER_VERIFICATION
    && producerReadiness.publication_readiness === VERIFIED_BUNDLE_PRODUCER_PUBLICATION
    && epochDisclosure?.includes(VERIFIED_BUNDLE_REPORT_STATUS)
    && epochDisclosure.includes(VERIFIED_BUNDLE_REPORT_METHOD)
    && epochDisclosure.includes(VERIFIED_BUNDLE_PRODUCER_VERIFICATION)
    && epochDisclosure.includes(VERIFIED_BUNDLE_PRODUCER_PUBLICATION)
);
checks.push(check(
  "Verified bundle retains report status, method, and producer readiness disclosure",
  verifiedBundleVerificationMatch,
  {
    applicable: authorityProfile === VERIFIED_BUNDLE_PROFILE,
    report_status: accountManifestVerification.status,
    report_method: accountManifestVerification.method,
    publication_readiness: accountManifestVerification.publication_readiness,
    producer_independent_verification: producerReadiness.independent_authority_verification,
    producer_publication_readiness: producerReadiness.publication_readiness,
    disclosure: epochDisclosure,
  },
));
checks.push(check(
  "Epoch manifest DOM matches exact source values and blockers",
  epochValuesMatch,
  epochEvidence,
));
if (performanceLineageMismatch) {
  await page.locator('[data-nav="weekly"]').click();
  await page.locator("#weekly-current-toggle").check();
  await page.locator("[data-weekly-series]").first().click();
}
const lineageMismatchDom = await page.evaluate(() => {
  const performanceRows = Array.from(document.querySelectorAll("#performance-table tbody tr"), (row) => Array.from(row.cells, (cell) => ({
    primary: cell.childNodes[0]?.textContent?.trim() ?? "",
    text: cell.textContent?.trim() ?? "",
  })));
  const comparison = Array.from(document.querySelectorAll("#performance-comparison .metric-block"), (block) => ({
    label: block.querySelector(".metric-label")?.textContent?.trim() ?? "",
    value: block.querySelector(".metric-value")?.textContent?.trim() ?? "",
    note: block.querySelector(".metric-note")?.textContent?.trim() ?? "",
  }));
  return {
    command: {
      gates: document.querySelector("#gate-stack")?.textContent?.trim() ?? "",
      kpis: document.querySelector("#command-kpis")?.textContent?.trim() ?? "",
      operator: document.querySelector("#operator-queue")?.textContent?.trim() ?? "",
      reason: document.querySelector("#command-reason")?.textContent?.trim() ?? "",
    },
    hwm_text: document.querySelector("#hwm-status")?.textContent?.trim() ?? "",
    performance_headers: Array.from(document.querySelectorAll("#performance-table thead th"), (header) => header.textContent?.trim() ?? ""),
    performance_rows: performanceRows,
    performance_text: document.querySelector("#performance-table")?.textContent?.trim() ?? "",
    strategy_coverage: document.querySelector("#strategy-coverage-bars")?.textContent?.trim() ?? "",
    weekly: {
      current_week_enabled: document.querySelector("#weekly-current-toggle")?.checked ?? false,
      detail: document.querySelector("#weekly-detail")?.textContent?.trim() ?? "",
      rows: Array.from(document.querySelectorAll("#weekly-table tbody tr"), (row) => Array.from(row.querySelectorAll(".weekly-cell"), (cell) => cell.textContent?.trim() ?? "")),
      text: document.querySelector("#weekly-table")?.textContent?.trim() ?? "",
    },
    comparison,
  };
});
const lineageWithheldText = `${lineageMismatchDom.performance_text} ${lineageMismatchDom.hwm_text}`;
const lineageWeeklyText = `${lineageMismatchDom.weekly.text} ${lineageMismatchDom.weekly.detail}`;
const lineageCommandText = Object.values(lineageMismatchDom.command).join(" ");
const lineageStrategyReturn = lineageMismatchDom.comparison.find((item) => item.label === "13-week strategy return");
const lineageMismatchDomMatch = !performanceLineageMismatch || (
  lineageMismatchDom.performance_rows.length > 0
    && lineageMismatchDom.performance_headers.every((header) => !/\b(?:Accepted|Available)\b/i.test(header))
    && lineageMismatchDom.performance_rows.every((row) => (
      row[4]?.primary === "—"
        && row[6]?.primary === "—"
        && row[4]?.text.includes("UNAVAILABLE_SOURCE_LINEAGE_MISMATCH")
        && row[6]?.text.includes("UNAVAILABLE_SOURCE_LINEAGE_MISMATCH")
    ))
    && lineageWithheldText.includes("UNAVAILABLE_SOURCE_LINEAGE_MISMATCH")
    && !/\b(?:Accepted|canonical account-live returns|Available for HWM|HWM-qualified|AVAILABLE)\b/i.test(lineageWithheldText)
    && lineageStrategyReturn?.value === "—"
    && /withheld/i.test(lineageStrategyReturn.note)
    && lineageMismatchDom.strategy_coverage.includes("UNAVAILABLE_SOURCE_LINEAGE_MISMATCH")
    && !/\b(?:AVAILABLE|coverage-qualified|TWR segments)\b/i.test(lineageMismatchDom.strategy_coverage)
    && lineageCommandText.includes("UNAVAILABLE_SOURCE_LINEAGE_MISMATCH")
    && !/\b(?:HWM|accepted|canonical|echo|composer)\b/i.test(lineageCommandText)
    && lineageMismatchDom.weekly.current_week_enabled
    && lineageMismatchDom.weekly.rows.length === 7
    && lineageMismatchDom.weekly.rows.every((row) => row.every((cell) => cell === "—"))
    && lineageMismatchDom.weekly.detail.includes("UNAVAILABLE_SOURCE_LINEAGE_MISMATCH")
    && !/\b(?:Accepted observations|Compounded from accepted daily returns|AVAILABLE)\b/i.test(lineageWeeklyText)
    && currentManifestShapeMatch
);
checks.push(check(
  "Lineage mismatch withholds retained report performance in the DOM",
  lineageMismatchDomMatch,
  { applicable: performanceLineageMismatch, ...lineageMismatchDom },
));
if (epochOnly) {
  const result = {
    all_passed: checks.every((item) => item.status === "PASS"),
    artifact_sha256: candidateRecord.artifact_sha256,
    browser_version: browser.version(),
    candidate_record_sha256: candidateRecordSha256,
    checks,
    panel_id: panelId,
    scope: "EPOCH_ONLY",
  };
  await browser.close();
  await writeFile(output, `${JSON.stringify(result, null, 2)}\n`, "utf8");
  if (!result.all_passed) process.exitCode = 1;
  process.exit();
}
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
    factorModeEvidence.push({
      active: await button.getAttribute("aria-pressed"),
      disabled,
      mode,
      strategy_rows: [],
      table: null,
      title: await button.getAttribute("title"),
    });
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
const liveFactorEvidence = factorModeEvidence.find((item) => item.mode === "LIVE_ONLY");
const hybridFactorEvidence = factorModeEvidence.find((item) => item.mode === "LIVE_PLUS_BACKTEST");
checks.push(check(
  "Factor analyses expose live-only and compatible-history modes without inventing prehistory",
  factorModeEvidence.length === requiredFactorEvidenceModes.length
    && liveFactorEvidence?.active === "true"
    && liveFactorEvidence.disabled === false
    && liveFactorEvidence.boundary?.includes("LIVE_ONLY")
    && liveFactorEvidence.table?.includes("Beta / loading")
    && liveFactorEvidence.table.includes("Evidence composition")
    && liveFactorEvidence.strategy_rows.every((row) => row.evidence)
    && (hybridFactorEvidence?.disabled
      ? hybridFactorEvidence.active === "false"
        && /(?:unavailable|not accepted|no accepted.*backtest|not proven compatible)/i.test(hybridFactorEvidence.title ?? "")
      : hybridFactorEvidence?.active === "true"
        && hybridFactorEvidence.boundary?.includes("LIVE_PLUS_BACKTEST")
        && hybridFactorEvidence.table?.includes("Beta / loading")
        && hybridFactorEvidence.table.includes("Evidence composition")
        && hybridFactorEvidence.strategy_rows.every(
          (row) => !row.evidence?.includes("no version-matched backtest prehistory"),
        )
        && hybridFactorEvidence.strategy_rows.some((row) => /[1-9][0-9,]* backtest/.test(row.evidence ?? ""))),
  factorModeEvidence,
));
await page.locator('[data-factor-evidence-mode="LIVE_ONLY"]').click();
const factorLensButtons = await page.locator("[data-factor-lens]").count();
const factorLensEvidence = [];
for (const lens of requiredFactorLenses) {
  const button = page.locator(`[data-factor-lens="${lens}"]`);
  await button.click();
  const requestedBetaRows = await page.locator("#factor-model-table tbody tr").evaluateAll(
    (nodes, strategies) => strategies.map((strategy) => {
      const evidence = nodes.find((node) => node.textContent?.includes(strategy))?.textContent ?? "";
      return {
        beta_visible: evidence.includes("β"),
        model_diagnostic: /model computed.*pm use blocked/i.test(evidence),
        model_unavailable: evidence.includes("NO ACCEPTED MODEL"),
        strategy,
      };
    }),
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
      && item.chips === expectedFactorSeries
      && item.rows === expectedFactorSeries
      && item.requested_beta_rows.every((row) => row.beta_visible || row.model_diagnostic || row.model_unavailable)),
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
checks.push(check("Factor source and model states remain separate", factorRows === expectedFactorSeries && factorChips === expectedFactorSeries && unavailableFactorDetail?.includes("Source state") && unavailableFactorDetail.includes("Model state"), { factor_chips: factorChips, factor_rows: factorRows, state_separation_visible: unavailableFactorDetail?.includes("Source state") && unavailableFactorDetail.includes("Model state") }));
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
checks.push(check("Dependence matrices and cell evidence are interactive", matrixCells === expectedFactorSeries ** 2 && matrixTitle?.includes("Stress") && pairDetail?.includes("Observed status"), { cell_buttons: matrixCells, matrix_title: matrixTitle, pair_detail: Boolean(pairDetail?.includes("Observed status")) }));
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
  weeklyRows === expectedCurrentStrategies
    && benchmarkRows === 9
    && currentWeekIncluded === false
    && weeklyLivePressed === "true"
    && weeklyLiveDetail?.includes("Live only")
    && weeklyLiveDetail.includes("Accepted observations")
    && (weeklyHybridDisabled
      ? weeklyModePressed === "false"
        && /(?:unavailable|not accepted|not proven compatible)/i.test(weeklyHybridTitle ?? "")
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
checks.push(check("Allocation filters preserve current strategies plus reserve", reserveRows === 1 && systematicRows === expectedCurrentStrategies && allAllocationRows === expectedCurrentStrategies + 1 && reservePressed === "true", { all: allAllocationRows, reserve: reserveRows, reserve_pressed: reservePressed, systematic: systematicRows }));
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
  accountOptions.length === expectedAccountOptions
    && new Set(accountOptions.map((item) => item.label)).size === expectedAccountOptions
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
    account_options_expected: expectedAccountOptions,
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
const hwmStatusText = await page.locator("#hwm-status").textContent();
checks.push(check(
  "HWM evidence summary never renders non-finite numbers when optional observation detail is absent",
  !/(?:^|\W)(?:NaN|Infinity)(?:$|\W)/.test(hwmStatusText || ""),
  hwmStatusText,
));
const historicalGapRows = await page.locator("#historical-gap-table tbody tr").count();
const historicalGapHeading = await page.locator("#historical-gap-badge").textContent();
const volatisPerformance = await page.locator("#performance-table tbody tr", { hasText: "Volatis" }).textContent();
const robuxioPerformance = await page.locator("#performance-table tbody tr", { hasText: "Robuxio" }).textContent();
checks.push(check(
  "Volatis and Robuxio expose their accepted account-live return paths",
  // The return path must be exposed and available; the renderer pairs the
  // evidence and status labels by evidence kind, so the QA must assert the
  // pairing, not independent alternatives: real ledger evidence renders
  // "account NAV observations" with "Available with inferred flow timing";
  // the frozen series renders "canonical account-live returns" with
  // "Available account-live return path".
  [volatisPerformance, robuxioPerformance].every((row) =>
    row
    && (
      (row.includes("account NAV observations")
        && row.includes("Available with inferred flow timing"))
      || (row.includes("canonical account-live returns")
        && row.includes("Available account-live return path"))
    )
  ),
  { robuxio: robuxioPerformance, volatis: volatisPerformance },
));
checks.push(check(
  "Reserve assignments are excluded from historical systematic mapping gaps",
  historicalGapRows === 1 && historicalGapHeading?.includes("0 unresolved"),
  { historical_gap_rows: historicalGapRows, label: historicalGapHeading, mapped_current_positions: positionRows },
));
await page.locator('[data-nav="capital"]').click();
const reserveLifecycleRows = await page.locator("#lifecycle-table tbody tr", { hasText: "Reserve — strategy not applicable" }).count();
checks.push(check(
  "Reserve lifecycle intervals are labeled not applicable rather than unmapped strategy gaps",
  reserveLifecycleRows === expectedReserveAssignments,
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
  artifact_sha256: candidateRecord.artifact_sha256,
  browser_version: browser.version(),
  candidate_record_sha256: candidateRecordSha256,
  checks,
  panel_id: panelId,
  scope: includeCandidates ? "FULL" : "NON_CANDIDATE",
};
await browser.close();
await writeFile(output, `${JSON.stringify(result, null, 2)}\n`, "utf8");
const reread = JSON.parse(await readFile(output, "utf8"));
if (!reread.all_passed) process.exitCode = 1;
