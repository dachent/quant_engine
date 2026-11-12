#!/usr/bin/env node
// Rendered-UI check of a PUBLISHED panel (#912). Opens index.html in headless Chromium, reads
// what a viewer sees from the DOM, and compares it with the panel's own payload
// (data/dashboard_data.json) as declared by a contract file. It writes a value-free result
// (check ids, kinds and PASS/FAIL codes; never a DOM or payload value) and a screenshot.
//
//   node published_render_check.mjs --html <dir>/index.html --contract <contract.json>
//        --output <dir>        (PLAYWRIGHT_MODULE names the playwright index.mjs)
//
// Check kinds (all read the rendered DOM after #app reports data-app-state=ready):
//   text_equals      selector text equals a payload string (transform: underscore_to_space)
//   number_in_text   selector text holds a number equal to payload value * scale (+- tolerance)
//   count_equals     selector matches as many nodes as the payload array has items
//   svg_end_label    last text.chart-label of the svg at selector equals a payload string
//   forbidden_text   none of the phrases may appear in the page text, unless a payload value
//                    equals `unless.equals` (a banner that contradicts the release)
// Exit 0 when every check passes, 1 when any fails, 2 on a usage or load error.
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

function parseArgs(argv) {
  const parsed = {};
  for (let i = 0; i < argv.length; i += 2) {
    if (!argv[i]?.startsWith("--") || !argv[i + 1]) throw new Error("Arguments must be --name value pairs");
    parsed[argv[i].slice(2)] = argv[i + 1];
  }
  return parsed;
}

export function pointer(document, ptr) {
  if (ptr === "" || ptr == null) return document;
  let node = document;
  for (const raw of ptr.split("/").slice(1)) {
    const key = raw.replaceAll("~1", "/").replaceAll("~0", "~");
    if (node == null || typeof node !== "object" || !(key in node)) return undefined;
    node = node[key];
  }
  return node;
}

const squash = (text) => String(text ?? "").replace(/\s+/g, " ").trim();

export function numbersIn(text) {
  return [...String(text).matchAll(/-?\d[\d,]*\.?\d*/g)]
    .map((m) => Number(m[0].replaceAll(",", "")))
    .filter(Number.isFinite);
}

// dom: { text(selector) -> string|null, count(selector) -> number, endLabel(selector) -> string|null, bodyText }
export function evaluate(contract, payload, dom) {
  return contract.checks.map((check) => {
    const base = { id: check.id, kind: check.kind };
    const expected = check.pointer === undefined ? undefined : pointer(payload, check.pointer);
    if (check.kind === "forbidden_text") {
      const body = squash(dom.bodyText).toLowerCase();
      const hit = check.phrases.some((phrase) => body.includes(phrase.toLowerCase()));
      const released = check.unless && pointer(payload, check.unless.pointer) === check.unless.equals;
      return { ...base, status: hit && !released ? "FAIL" : "PASS", code: hit && !released ? "FORBIDDEN_TEXT_PRESENT" : "OK" };
    }
    if (expected === undefined) return { ...base, status: "FAIL", code: "PAYLOAD_VALUE_MISSING" };
    if (check.kind === "count_equals") {
      const found = dom.count(check.selector);
      const ok = Array.isArray(expected) && found === expected.length;
      return { ...base, status: ok ? "PASS" : "FAIL", code: ok ? "OK" : "COUNT_DIFFERS" };
    }
    const text = check.kind === "svg_end_label" ? dom.endLabel(check.selector) : dom.text(check.selector);
    if (text == null) return { ...base, status: "FAIL", code: "DOM_ELEMENT_MISSING" };
    if (check.kind === "text_equals" || check.kind === "svg_end_label") {
      let want = String(expected);
      if (check.transform === "underscore_to_space") want = want.replaceAll("_", " ");
      const ok = squash(text).toLowerCase() === squash(want).toLowerCase();
      return { ...base, status: ok ? "PASS" : "FAIL", code: ok ? "OK" : "TEXT_DIFFERS" };
    }
    if (check.kind === "number_in_text") {
      const want = Number(expected) * (check.scale ?? 1);
      const tolerance = check.tolerance ?? 0.06;
      const ok = Number.isFinite(want) && numbersIn(text).some((n) => Math.abs(n - want) <= tolerance);
      return { ...base, status: ok ? "PASS" : "FAIL", code: ok ? "OK" : "NUMBER_DIFFERS" };
    }
    return { ...base, status: "FAIL", code: "UNKNOWN_KIND" };
  });
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (!options.html || !options.contract || !options.output || !process.env.PLAYWRIGHT_MODULE) {
    throw new Error("--html, --contract, --output and PLAYWRIGHT_MODULE are required");
  }
  const html = path.resolve(options.html);
  const output = path.resolve(options.output);
  const contract = JSON.parse(await readFile(options.contract, "utf8"));
  const payload = JSON.parse(await readFile(path.join(path.dirname(html), "data", "dashboard_data.json"), "utf8"));
  const { chromium } = await import(pathToFileURL(path.resolve(process.env.PLAYWRIGHT_MODULE)).href);
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {}),
  });
  await mkdir(output, { recursive: true });
  let results = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.setDefaultTimeout(300000);
    await page.goto(`${pathToFileURL(html).href}#command`, { waitUntil: "load", timeout: 300000 });
    await page.waitForFunction(() => document.querySelector("#app")?.dataset.appState === "ready");
    // Gather every DOM read the contract needs in one page round trip.
    const reads = await page.evaluate((checks) => {
      const out = { bodyText: document.body.innerText, text: {}, count: {}, end: {} };
      for (const check of checks) {
        if (!check.selector) continue;
        const nodes = [...document.querySelectorAll(check.selector)];
        out.count[check.selector] = nodes.length;
        out.text[check.selector] = nodes.length ? nodes.map((n) => n.textContent).join(" ") : null;
        const labels = nodes.length ? [...nodes[0].querySelectorAll("text.chart-label")] : [];
        out.end[check.selector] = labels.length ? labels[labels.length - 1].textContent : null;
      }
      return out;
    }, contract.checks);
    results = evaluate(contract, payload, {
      bodyText: reads.bodyText,
      text: (s) => reads.text[s] ?? null,
      count: (s) => reads.count[s] ?? 0,
      endLabel: (s) => reads.end[s] ?? null,
    });
    await page.screenshot({ path: path.join(output, "command-desktop.png"), fullPage: true });
  } finally {
    await browser.close();
  }
  const status = results.every((r) => r.status === "PASS") ? "PASS" : "FAIL";
  await writeFile(
    path.join(output, "render-check.json"),
    `${JSON.stringify({ schema: "qe-published-render-check/1", contract_id: contract.contract_id, status, checks: results }, null, 2)}\n`,
    "utf8",
  );
  for (const r of results.filter((x) => x.status !== "PASS")) console.error(`RENDER_CHECK_FAIL ${r.id} ${r.code}`);
  process.exitCode = status === "PASS" ? 0 : 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((error) => {
    console.error(`RENDER_CHECK_ERROR ${error.message}`);
    process.exitCode = 2;
  });
}
