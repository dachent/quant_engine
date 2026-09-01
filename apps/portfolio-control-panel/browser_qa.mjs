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

const options = args(process.argv.slice(2));
if (!options.html || !options.output || !process.env.PLAYWRIGHT_MODULE) {
  throw new Error("--html, --output, and PLAYWRIGHT_MODULE are required");
}
const expectedCalculationCutoff = acceptedCutoff(options["expected-calculation-cutoff"]);
const moduleUrl = pathToFileURL(path.resolve(process.env.PLAYWRIGHT_MODULE)).href;
const { chromium } = await import(moduleUrl);
const html = path.resolve(options.html);
const output = path.resolve(options.output);
const baseUrl = pathToFileURL(html).href;
const candidateRecordBytes = await readFile(path.join(path.dirname(html), "candidate.json"));
const candidateRecord = JSON.parse(candidateRecordBytes.toString("utf8"));
const candidateRecordSha256 = createHash("sha256").update(candidateRecordBytes).digest("hex");
const dashboardData = JSON.parse(await readFile(path.join(path.dirname(html), "data", "dashboard_data.json"), "utf8"));
const launchOptions = {
  headless: true,
  ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH
    ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH }
    : {}),
};
const excludeCandidates = options["exclude-candidates"] === "true";
const views = ["command", "capital", "risk", "signals", "attribution", "dependence", "weekly", "candidates", "data"]
  .filter((view) => !excludeCandidates || view !== "candidates");
const viewports = [
  { name: "desktop", viewport: { width: 1440, height: 900 } },
  { name: "tablet", viewport: { width: 1024, height: 768 } },
  { name: "mobile", viewport: { width: 390, height: 844 } },
];
const captures = [
  ...viewports.flatMap(({ name, viewport }) => views.map((view) => ({
    name: `${name}-${view}`,
    view,
    viewport,
    lint: true,
    pdf: name === "desktop" && view === "command",
  }))),
  { name: "direct-file", view: "", viewport: { width: 1280, height: 800 } },
];

async function visualLint(page) {
  return page.evaluate(() => {
    function selectorFor(element) {
      if (element.id) return `#${element.id}`;
      const parts = [];
      let current = element;
      while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 4) {
        let part = current.tagName.toLowerCase();
        if (current.classList.length > 0) part += `.${Array.from(current.classList).slice(0, 2).join(".")}`;
        parts.unshift(part);
        current = current.parentElement;
      }
      return parts.join(" > ");
    }
    function parseRgb(value) {
      const match = String(value).match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
      return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null;
    }
    function luminance(rgb) {
      const parts = rgb.map((part) => {
        const channel = part / 255;
        return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2];
    }
    function contrastRatio(foreground, background) {
      const first = luminance(foreground);
      const second = luminance(background);
      return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
    }
    function resolvedBackground(element) {
      let current = element;
      while (current) {
        const color = getComputedStyle(current).backgroundColor;
        if (color && color !== "transparent" && !color.endsWith(", 0)")) return color;
        current = current.parentElement;
      }
      return "rgb(255, 255, 255)";
    }
    function isVisible(element) {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) > 0 && rect.width > 0 && rect.height > 0;
    }
    const findings = [];
    for (const element of Array.from(document.querySelectorAll("body *"))) {
      if (!isVisible(element)) continue;
      if (element.matches("caption, .sr-only")) continue;
      const text = (element.innerText || "").trim().replace(/\s+/g, " ");
      const selector = selectorFor(element);
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      const intentionalScroll = element.matches("#app, .workspace, main, .view, .grid, .table-wrap, .matrix-shell, .weekly-table-shell, .scenario-row");
      if (text && !intentionalScroll && (element.scrollWidth > element.clientWidth + 3 || element.scrollHeight > element.clientHeight + 3)) {
        findings.push({ rule: "text_overflow", severity: "major", selector, message: "Element text appears clipped or scrollable.", sample: text.slice(0, 120), bounds: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } });
      }
      const fontSize = Number.parseFloat(style.fontSize);
      if (text && fontSize > 0 && fontSize < 11) {
        findings.push({ rule: "tiny_text", severity: "minor", selector, message: `Text is ${fontSize}px, which is likely too small for general UI reading.`, sample: text.slice(0, 120) });
      }
      const foreground = parseRgb(style.color);
      const background = parseRgb(resolvedBackground(element));
      if (text && foreground && background) {
        const ratio = contrastRatio(foreground, background);
        const threshold = fontSize >= 18 ? 3 : 4.5;
        if (ratio < threshold) findings.push({ rule: "low_contrast", severity: "major", selector, message: `Contrast ratio ${ratio.toFixed(2)} is below ${threshold}.`, sample: text.slice(0, 120) });
      }
      if (element.tagName.toLowerCase() === "img") {
        if (!element.getAttribute("alt")) findings.push({ rule: "missing_image_alt", severity: "minor", selector, message: "Image is missing alt text." });
        if (element.naturalWidth === 0 || element.naturalHeight === 0) findings.push({ rule: "broken_image", severity: "major", selector, message: "Image did not load." });
      }
    }
    return findings;
  });
}

const browser = await chromium.launch(launchOptions);
try {
  for (const capture of captures) {
    const folder = path.join(output, capture.name);
    await mkdir(folder, { recursive: true });
    const page = await browser.newPage({ viewport: capture.viewport });
    const consoleEvents = [];
    const requestFailures = [];
    page.on("console", (message) => consoleEvents.push({ type: message.type(), text: message.text(), location: message.location() }));
    page.on("pageerror", (error) => consoleEvents.push({ type: "pageerror", text: error.message, location: {} }));
    page.on("requestfailed", (request) => requestFailures.push({ url: request.url(), method: request.method(), failure: request.failure()?.errorText ?? "unknown" }));
    const target = `${baseUrl}${capture.view ? `#${capture.view}` : ""}`;
    await page.goto(target, { waitUntil: "load", timeout: 30000 });
    await page.waitForFunction(() => document.querySelector("#app")?.dataset.appState === "ready");
    let epochDisplay = null;
    if (capture.view === "risk" && dashboardData.account_manifest?.schema_version >= 9) {
      const manifest = dashboardData.account_manifest;
      const rows = await page.locator("#account-manifest-table tbody tr").evaluateAll((nodes) => nodes.map((node) => ({
        account_id: node.getAttribute("data-account-id"),
        account_label: node.querySelector("td")?.textContent?.trim() ?? "",
        strategy_id: node.querySelector("td:nth-child(2)")?.textContent?.trim() ?? "",
      })));
      const legend = (await page.locator("#account-manifest-legend").textContent())?.trim() ?? "";
      const strategyIds = new Set(rows.map((row) => row.strategy_id));
      const boundLedgerIdentity = manifest.source_identities?.classified_flows?.ledger_identity;
      const boundLedgerArtifact = boundLedgerIdentity?.artifact;
      const captureDate = boundLedgerArtifact?.as_of_date;
      const calculationFactsAfterAcceptedCutoff = calculationDateFacts(manifest)
        .filter((fact) => !isCalendarDate(fact.value) || fact.value > expectedCalculationCutoff);
      if (rows.length !== 13 || strategyIds.size !== 7 || rows.some((row, index) => (
        row.account_id !== manifest.rows[index]?.account_id
        || row.account_label !== manifest.rows[index]?.account_label
        || row.strategy_id !== manifest.rows[index]?.strategy_id
      )) || rows.some((row) => /echo|composer/i.test(`${row.strategy_id} ${row.account_label}`))) {
        throw new Error("schema-9 epoch manifest rendered rows diverge from the bound current subject");
      }
      if (manifest.calculation_cutoff_date !== expectedCalculationCutoff
        || typeof boundLedgerIdentity !== "object"
        || typeof boundLedgerArtifact !== "object"
        || !isCalendarDate(captureDate)
        || expectedCalculationCutoff > captureDate
        || !legend.includes(`Calculations through ${manifest.calculation_cutoff_date}.`)
        || !legend.includes(`Artifact/provider capture date: ${captureDate}.`)
        || calculationFactsAfterAcceptedCutoff.length > 0) {
        throw new Error("schema-9 epoch manifest temporal acceptance boundary is missing, divergent, or exceeded");
      }
      epochDisplay = { rows, legend };
    }
    await page.screenshot({ path: path.join(folder, "screenshot.png"), fullPage: true });
    if (capture.pdf) await page.pdf({ path: path.join(folder, "page.pdf"), printBackground: true, preferCSSPageSize: true });
    const manifest = {
      artifact_sha256: candidateRecord.artifact_sha256,
      candidate_record_sha256: candidateRecordSha256,
      console_event_count: consoleEvents.length,
      console_events_path: path.join(folder, "console-events.json"),
      device_scale_factor: 1,
      generated_at: new Date().toISOString(),
      pdf_path: capture.pdf ? path.join(folder, "page.pdf") : null,
      request_failure_count: requestFailures.length,
      request_failures_path: path.join(folder, "request-failures.json"),
      screenshot_path: path.join(folder, "screenshot.png"),
      target,
      final_url: page.url(),
      epoch_display: epochDisplay,
      title: await page.title(),
      tool: "portfolio_control_panel_browser_qa",
      scope: excludeCandidates ? "NON_CANDIDATE" : "FULL",
      viewport: capture.viewport,
    };
    await writeFile(path.join(folder, "console-events.json"), `${JSON.stringify(consoleEvents, null, 2)}\n`, "utf8");
    await writeFile(path.join(folder, "request-failures.json"), `${JSON.stringify(requestFailures, null, 2)}\n`, "utf8");
    await writeFile(path.join(folder, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
    if (capture.lint) {
      const findings = await visualLint(page);
      const lint = { finding_count: findings.length, findings, generated_at: new Date().toISOString(), target, tool: "visual_lint", viewport: capture.viewport };
      await writeFile(path.join(folder, "visual-lint.json"), `${JSON.stringify(lint, null, 2)}\n`, "utf8");
    }
    await page.close();
  }
} finally {
  await browser.close();
}
