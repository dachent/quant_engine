// Test harness: run apps/portfolio-control-panel/app.js against a stub DOM.
//
//   node shell_harness.mjs <app.js> <payload.json> <function,function,...>
//
// The shell is one closed IIFE that renders every view from `window.PORTFOLIO_DATA`
// when the document loads.  To test one view without a browser the harness
//   1. instruments a copy of the source (nothing is written back) so the named render
//      functions are reachable as `globalThis.__panel.<name>`,
//   2. evaluates it in a vm context whose `document` is a stub that records every
//      `innerHTML` / `textContent` / attribute write per selector, and whose
//      `PORTFOLIO_DATA` is the given payload (a key the payload omits reads as a
//      permissive stub, so only the data a function under test needs must be real),
//   3. calls each named function in isolation (a throw is recorded, not fatal), and
//   4. prints the recorded DOM text and the errors as one JSON document.
import fs from "node:fs";
import vm from "node:vm";

const [appPath, payloadPath, calls = ""] = process.argv.slice(2);
const names = calls.split(",").map((name) => name.trim()).filter(Boolean);
const source = fs.readFileSync(appPath, "utf8");
const marker = 'document.addEventListener("DOMContentLoaded", init);';
if (source.split(marker).length !== 2) {
  throw new Error("shell entry marker `DOMContentLoaded` is not present exactly once");
}
const instrumented = source.replace(
  marker,
  `globalThis.__panel = { ${names.join(", ")} };\n  ${marker}`,
);

const permissive = () => new Proxy(function permissiveStub() {}, {
  get: (_target, prop) => {
    if (prop === Symbol.toPrimitive) return () => "";
    if (prop === Symbol.iterator) return function* empty() {};
    if (prop === "length") return 0;
    if (prop === "then") return undefined;
    return permissive();
  },
  apply: () => permissive(),
  has: () => true,
});

const payload = JSON.parse(fs.readFileSync(payloadPath, "utf8"));
const data = new Proxy(payload, {
  get: (target, prop) => (prop in target ? target[prop] : permissive()),
});

const elements = new Map();
const makeElement = () => {
  const state = {
    innerHTML: "",
    textContent: "",
    hidden: false,
    dataset: {},
    attributes: {},
    value: "",
    disabled: false,
  };
  return new Proxy({}, {
    get: (_target, prop) => {
      if (prop === "__state") return state;
      if (prop in state) return state[prop];
      if (prop === "classList") {
        return { add() {}, remove() {}, toggle() {}, contains: () => false };
      }
      if (prop === "style") return {};
      if (prop === "getAttribute") return (name) => state.attributes[name] ?? null;
      if (prop === "setAttribute") return (name, value) => { state.attributes[name] = String(value); };
      if (prop === "querySelector") return () => makeElement();
      if (prop === "querySelectorAll") return () => [];
      if (prop === "closest") return () => null;
      if (typeof prop === "symbol") return undefined;
      return () => undefined;
    },
    set: (_target, prop, value) => { state[prop] = value; return true; },
  });
};
const document = {
  querySelector: (selector) => {
    if (!elements.has(selector)) elements.set(selector, makeElement());
    return elements.get(selector);
  },
  querySelectorAll: () => [],
  getElementById: (id) => document.querySelector(`#${id}`),
  createElement: () => makeElement(),
  addEventListener: () => {},
  body: makeElement(),
};
const sandbox = {
  document,
  location: { hash: "" },
  console,
  CSS: { escape: (value) => String(value) },
};
sandbox.window = {
  PORTFOLIO_DATA: data,
  requestAnimationFrame: (callback) => callback(),
  setTimeout: () => 0,
  addEventListener: () => {},
  atob: (value) => Buffer.from(String(value), "base64").toString("binary"),
};
vm.createContext(sandbox);
vm.runInContext(instrumented, sandbox, { filename: "app.js" });

const errors = [];
for (const name of names) {
  try {
    sandbox.__panel[name]();
  } catch (error) {
    errors.push({ name, error: String((error && error.stack) || error) });
  }
}
const dom = {};
for (const [selector, element] of elements) {
  const state = element.__state;
  if (state.innerHTML || state.textContent || Object.keys(state.dataset).length) {
    dom[selector] = {
      innerHTML: state.innerHTML,
      textContent: state.textContent,
      dataset: state.dataset,
    };
  }
}
process.stdout.write(JSON.stringify({ errors, dom }));
