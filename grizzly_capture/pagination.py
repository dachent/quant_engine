"""DOM walking, view switching, and the trades/positions extraction routes.

All page-side work is done by the fixed, read-only scripts embedded in this
module; the driver never evaluates arbitrary JavaScript. The state route only
reads React state and serializes primitive values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from grizzly_capture import RouteFailure
from grizzly_capture.persistence import (
    DOWNLOADS_DIRNAME,
    POSITIONS_FILENAME,
    TRADES_CSV_HEADER,
    TRADES_FILENAME,
    ArtifactRecord,
    csv_header,
    csv_row_count,
    rows_to_csv,
    wait_for_download,
    write_artifact,
)
from grizzly_capture.session import set_download_behavior

CLICKABLE_SELECTOR = (
    "button, a, [role='button'], [role='tab'], "
    "input[type='button'], input[type='submit']"
)

_LIST_CLICKABLES_SCRIPT = """
() => {
  const els = Array.from(document.querySelectorAll(__SELECTOR__));
  return els.map((el, index) => {
    const near = el.closest("div, section, main, form");
    return {
      index,
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || el.textContent || "").trim().slice(0, 120),
      aria: (el.getAttribute("aria-label") || "").slice(0, 120),
      title: (el.getAttribute("title") || "").slice(0, 120),
      svg_count: el.querySelectorAll("svg").length,
      near_table: !!(near && near.querySelector("table")),
      visible: !!(
        el.offsetWidth || el.offsetHeight || el.getClientRects().length
      ),
      disabled: !!el.disabled,
    };
  });
}
""".replace("__SELECTOR__", json.dumps(CLICKABLE_SELECTOR))

_CLICK_CLICKABLE_SCRIPT = """
(index) => {
  const el = document.querySelectorAll(__SELECTOR__)[index];
  if (!el) return false;
  el.click();
  return true;
}
""".replace("__SELECTOR__", json.dumps(CLICKABLE_SELECTOR))

_SWITCH_TO_TABLE_VIEW_SCRIPT = """
() => {
  const els = Array.from(document.querySelectorAll(__SELECTOR__));
  const norm = (s) => (s || "").trim().toLowerCase();
  for (const el of els) {
    const label = norm(el.innerText || el.textContent);
    const aria = norm(el.getAttribute("aria-label"));
    const visible = !!(
      el.offsetWidth || el.offsetHeight || el.getClientRects().length
    );
    if (!visible) continue;
    if (["table", "table view"].includes(label) ||
        ["table", "table view"].includes(aria)) {
      if (el.getAttribute("aria-selected") === "true") return "already";
      el.click();
      return "clicked";
    }
  }
  return "not-found";
}
""".replace("__SELECTOR__", json.dumps(CLICKABLE_SELECTOR))

_CLICK_POSITIONS_TAB_SCRIPT = """
() => {
  const els = Array.from(document.querySelectorAll(__SELECTOR__));
  const norm = (s) => (s || "").trim().toLowerCase();
  const labels = ["positions", "current positions", "holdings"];
  for (const el of els) {
    const label = norm(el.innerText || el.textContent);
    const aria = norm(el.getAttribute("aria-label"));
    const visible = !!(
      el.offsetWidth || el.offsetHeight || el.getClientRects().length
    );
    if (!visible) continue;
    if (labels.includes(label) || labels.includes(aria)) {
      if (el.getAttribute("aria-selected") === "true") return "already";
      el.click();
      return "clicked";
    }
  }
  return "not-found";
}
""".replace("__SELECTOR__", json.dumps(CLICKABLE_SELECTOR))

# Read-only React state walk: anchor on the rendered trades table, climb the
# fiber return chain, and scan memoized state/props for the first array of
# objects carrying date/action/price-like keys. Serializes primitives only.
_STATE_EXTRACTION_SCRIPT = """
() => {
  const MAX_FIBER_HOPS = 400;
  const MAX_DEPTH = 6;
  const looksLikeTrades = (arr) => {
    if (!Array.isArray(arr) || arr.length === 0) return false;
    let objectRows = 0;
    let tradeRows = 0;
    for (const item of arr.slice(0, 50)) {
      if (!item || typeof item !== "object" || Array.isArray(item)) continue;
      objectRows += 1;
      const keys = Object.keys(item).map((k) => k.toLowerCase());
      const hasDate = keys.some((k) => k.includes("date"));
      const hasAction = keys.some(
        (k) => k.includes("action") || k.includes("side") ||
               k.includes("signal"),
      );
      const hasPrice = keys.some(
        (k) => k.includes("price") || k.includes("spx") ||
               k.includes("pnl") || k.includes("p/l"),
      );
      if (hasDate && (hasAction || hasPrice)) tradeRows += 1;
    }
    return objectRows > 0 && tradeRows * 2 >= objectRows;
  };
  const fiberKeyOf = (node) =>
    Object.keys(node).find(
      (k) => k.startsWith("__reactFiber$") ||
             k.startsWith("__reactInternalInstance$"),
    );
  let anchor = null;
  for (const cell of Array.from(document.querySelectorAll("th, td"))) {
    const t = (cell.textContent || "").trim().toLowerCase();
    if (t === "date (et)" || t === "date" || t === "action") {
      anchor = cell;
      break;
    }
  }
  if (!anchor) anchor = document.querySelector("table") || document.body;
  const seen = new Set();
  const scanState = (value, depth) => {
    if (!value || typeof value !== "object" || depth > MAX_DEPTH) return null;
    if (seen.has(value)) return null;
    seen.add(value);
    if (Array.isArray(value)) return looksLikeTrades(value) ? value : null;
    for (const key of Object.keys(value)) {
      if (key.startsWith("__react")) continue;
      const found = scanState(value[key], depth + 1);
      if (found) return found;
    }
    return null;
  };
  const key = fiberKeyOf(anchor);
  let fiber = key ? anchor[key] : null;
  for (let hop = 0; fiber && hop < MAX_FIBER_HOPS; hop += 1) {
    const found =
      scanState(fiber.memoizedState, 0) || scanState(fiber.memoizedProps, 0);
    if (found) {
      return found.map((row) => {
        const out = {};
        for (const k of Object.keys(row)) {
          const v = row[k];
          out[k] =
            v === null || v === undefined
              ? ""
              : typeof v === "object"
                ? JSON.stringify(v)
                : String(v);
        }
        return out;
      });
    }
    fiber = fiber.return;
  }
  return [];
}
"""

_TABLES_SNAPSHOT_SCRIPT = """
() => {
  const grab = (row) =>
    Array.from(row.querySelectorAll("th, td")).map(
      (c) => (c.textContent || "").trim(),
    );
  return Array.from(document.querySelectorAll("table"))
    .map((table) => {
      const headRows = Array.from(table.querySelectorAll("thead tr"));
      let header = headRows.length ? grab(headRows[0]) : [];
      let rows = Array.from(table.querySelectorAll("tbody tr")).map(grab);
      if (!header.length && !rows.length) {
        const all = Array.from(table.querySelectorAll("tr")).map(grab);
        header = all.length ? all[0] : [];
        rows = all.slice(1);
      }
      return { header, rows };
    })
    .filter((t) => t.header.length || t.rows.length);
}
"""


@dataclass(frozen=True)
class ClickableDescriptor:
    """One rendered clickable element, as reported by the DOM walker."""

    index: int
    tag: str
    text: str
    aria: str
    title: str
    svg_count: int
    near_table: bool
    visible: bool
    disabled: bool


@dataclass(frozen=True)
class DomTable:
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


def switch_to_table_view(page: Any) -> str:
    """Switch Chart -> Table view; returns 'clicked'/'already'/'not-found'."""
    return str(page.evaluate(_SWITCH_TO_TABLE_VIEW_SCRIPT))


def click_positions_tab(page: Any) -> str:
    """Open the positions tab when present; same return contract as above."""
    return str(page.evaluate(_CLICK_POSITIONS_TAB_SCRIPT))


def list_clickables(page: Any) -> list[ClickableDescriptor]:
    raw = page.evaluate(_LIST_CLICKABLES_SCRIPT)
    return [ClickableDescriptor(**item) for item in raw or []]


def score_csv_control(descriptor: ClickableDescriptor) -> int:
    """Heuristic score for the client-side Download-CSV control."""
    if not descriptor.visible or descriptor.disabled:
        return -1
    haystack = " ".join(
        [descriptor.text, descriptor.aria, descriptor.title],
    ).lower()
    score = 0
    if "csv" in haystack:
        score += 4
    if "download" in haystack or "export" in haystack:
        score += 3
    if descriptor.svg_count:
        score += 1
    if descriptor.near_table:
        score += 2
    return score


def find_csv_control(page: Any) -> ClickableDescriptor | None:
    """Walk the rendered DOM for the CSV control (view-state dependent)."""
    candidates = [
        (score_csv_control(descriptor), descriptor)
        for descriptor in list_clickables(page)
    ]
    candidates = [pair for pair in candidates if pair[0] >= 2]
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def click_clickable(page: Any, descriptor: ClickableDescriptor) -> None:
    if not page.evaluate(_CLICK_CLICKABLE_SCRIPT, descriptor.index):
        raise RouteFailure("csv control disappeared before click")


def extract_trades_state(page: Any) -> list[dict[str, Any]]:
    """Read the trades array from React state via the fixed walk script."""
    raw = page.evaluate(_STATE_EXTRACTION_SCRIPT)
    if not isinstance(raw, list):
        return []
    return [dict(row) for row in raw if isinstance(row, dict)]


def read_dom_tables(page: Any) -> list[DomTable]:
    raw = page.evaluate(_TABLES_SNAPSHOT_SCRIPT)
    tables = []
    for item in raw or []:
        header = tuple(str(cell) for cell in item.get("header", []))
        rows = tuple(tuple(str(cell) for cell in row) for row in item.get("rows", []))
        tables.append(DomTable(header=header, rows=rows))
    return tables


def _normalized_key(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch.isalnum())


_STATE_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "Date (ET)": ("date", "dateet", "tradedate", "entrydate"),
    "Action": ("action", "side", "signal", "tradetype"),
    "SPX": ("spx", "spxprice", "spxclose"),
    "Prev Price": ("prevprice", "previousprice", "prev", "priorprice"),
    "P/L": ("pl", "pnl", "profitloss", "profit"),
    "PnL %": ("pnlpct", "pnlpercent", "returnpct", "pct", "percent"),
    "Model": ("model", "modelname"),
    "Buy & Hold": ("buyhold", "buyandhold", "bh", "spxreturn"),
}


def map_state_trade(trade: Mapping[str, Any]) -> list[str]:
    """Map one state-trade object onto the canonical CSV column shape."""
    normalized = {_normalized_key(key): value for key, value in trade.items()}
    row: list[str] = []
    for column in TRADES_CSV_HEADER:
        value: Any = ""
        for alias in _STATE_KEY_ALIASES[column]:
            if alias in normalized:
                value = normalized[alias]
                break
        row.append(str(value))
    return row


def run_download_route(
    page: Any,
    run_dir: Path,
    *,
    timeout_s: float,
) -> ArtifactRecord:
    """Route (a): CDP download behavior + DOM-walked CSV control click."""
    downloads_dir = run_dir / DOWNLOADS_DIRNAME
    set_download_behavior(page, downloads_dir)
    switch_to_table_view(page)
    control = find_csv_control(page)
    if control is None:
        raise RouteFailure("csv control not found in the rendered DOM")
    known = frozenset(entry.name for entry in downloads_dir.iterdir())
    click_clickable(page, control)
    landed = wait_for_download(downloads_dir, timeout_s=timeout_s, known=known)
    data = landed.read_bytes()
    if csv_header(data) != list(TRADES_CSV_HEADER):
        raise RouteFailure("downloaded artifact header mismatch")
    record = write_artifact(
        run_dir,
        TRADES_FILENAME,
        data,
        rows=csv_row_count(data),
        source="download",
    )
    landed.unlink()  # trades.csv is the canonical byte copy of the download
    return record


def run_state_route(
    page: Any,
    run_dir: Path,
    *,
    timeout_s: float,
) -> ArtifactRecord:
    """Route (b): read the trades array from React state, serialize to CSV."""
    del timeout_s  # no download to wait for on this route
    switch_to_table_view(page)
    trades = extract_trades_state(page)
    if not trades:
        raise RouteFailure("no trades array found in page state")
    rows = [map_state_trade(trade) for trade in trades]
    data = rows_to_csv(TRADES_CSV_HEADER, rows)
    return write_artifact(
        run_dir,
        TRADES_FILENAME,
        data,
        rows=len(rows),
        source="state",
    )


_ROUTE_RUNNERS = {
    "download": run_download_route,
    "state": run_state_route,
}


def run_route(
    route: str,
    page: Any,
    run_dir: Path,
    *,
    timeout_s: float,
) -> ArtifactRecord:
    if route not in _ROUTE_RUNNERS:
        raise RouteFailure("unknown route")
    return _ROUTE_RUNNERS[route](page, run_dir, timeout_s=timeout_s)


def pick_positions_table(tables: Sequence[DomTable]) -> DomTable | None:
    """Pick the table whose header looks like the current-positions surface."""
    symbol_tokens = {"symbol", "ticker", "position", "holding", "asset"}
    sizing_tokens = {
        "qty",
        "quantity",
        "shares",
        "price",
        "value",
        "weight",
        "allocation",
    }
    for table in tables:
        header = {_normalized_key(cell) for cell in table.header}
        if header & symbol_tokens and header & sizing_tokens:
            return table
    return None


def capture_positions(page: Any, run_dir: Path) -> ArtifactRecord | None:
    """Persist the current-positions DOM table to CSV; None when absent."""
    click_positions_tab(page)
    table = pick_positions_table(read_dom_tables(page))
    if table is None or not table.rows:
        return None
    data = rows_to_csv(table.header, table.rows)
    return write_artifact(
        run_dir,
        POSITIONS_FILENAME,
        data,
        rows=len(table.rows),
        source="dom-table",
    )
