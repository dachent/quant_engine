#!/usr/bin/env python3
"""
CDP HAR Capture Script — Portfolio123
Captures all network traffic from Portfolio123 strategy pages via Edge CDP.
Session-cookie authenticated (login session), zero API quota cost.

Supports:
- Config file (JSON) listing strategy IDs and types
- Auto-discovery of transaction pagination counts
- Per-strategy HAR files (not one monolithic file)
- Resume capability (skips strategies with existing HAR files)
- CLI arguments for overriding config

Architecture: three threads (reader, body-fetcher, command sender) to avoid
deadlocks and ensure response bodies are captured before eviction.
Adapted from website_har_contract/har_capture.py.
"""

import json
import time
import base64
import os
import sys
import queue
import requests
import websocket
import threading
import argparse
import re
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from collections import OrderedDict

# ─── Configuration ───────────────────────────────────────────────────────────

CDP_HOST = "localhost"
CDP_PORT_DEFAULT = 9222
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR_DEFAULT = os.path.join(SCRIPT_DIR, "json")
SCREENSHOT_DIR_DEFAULT = os.path.join(SCRIPT_DIR, "screenshots")

PAGE_LOAD_WAIT = 12
SETTLE_WAIT = 5
TRANSACTION_PAGE_LOAD_WAIT = 5
TRANSACTION_SETTLE_WAIT = 2

# Default config — overridden by --config file or CLI args
DEFAULT_CONFIG = {
    "cdp_port": CDP_PORT_DEFAULT,
    "output_dir": OUTPUT_DIR_DEFAULT,
    "screenshot_dir": SCREENSHOT_DIR_DEFAULT,
    "page_load_wait": PAGE_LOAD_WAIT,
    "settle_wait": SETTLE_WAIT,
    "take_screenshots": True,
    "shared_strategies": [],  # list of portid integers
    "managed_strategies": [],  # list of id integers
    "account_ids": [],  # list of account id integers
    "ranking_system_ids": {},  # {portid: ranking_id} — auto-discovered if empty
}

# JSP pages to capture for each shared strategy (portid)
# Uses download endpoints for bulk data (transactions, holdings, performance)
# instead of paginating through JSP pages one by one
SHARED_STRATEGY_PAGES = [
    ("port_summary.jsp?portid={id}", "summary"),  # Summary + quick stats + top holdings + recent trades
    ("port_wiz.jsp?editUid={id}", "trading_system"),  # Buy/sell rules, universe, ranking, rebalance
    # Bulk download endpoints — return ALL data in one request:
    ("p123/DownloadPortTrans?portid={id}", "all_transactions"),
    ("p123/DownloadPortHoldings?portid={id}", "all_holdings"),
    ("p123/DownloadPortPerf?portid={id}", "all_performance"),
    ("p123/DownloadPortPeriodPerformance?portid={id}&bench=24262&period=1", "period_performance"),
    ("p123/DownloadPortPeriodPerformanceDist?portid={id}&bench=24262&period=1", "period_perf_dist"),
]

# SPA tabs to capture for each managed strategy (id)
MANAGED_STRATEGY_TABS = [
    ("", "summary"),
    ("&t=performance", "performance"),
    ("&t=holdings", "holdings"),
    ("&t=transactions", "transactions"),
    ("&t=stats", "stats"),
]

# SPA tabs to capture for each account (id) — same structure as managed
ACCOUNT_TABS = [
    ("", "summary"),
    ("&t=performance", "performance"),
    ("&t=holdings", "holdings"),
    ("&t=transactions", "transactions"),
    ("&t=stats", "stats"),
]

# ─── HAR Builder ─────────────────────────────────────────────────────────────

class HarBuilder:
    def __init__(self):
        self.pages = []
        self._requests = OrderedDict()
        self._responses = {}
        self._extra_req_headers = {}
        self._extra_resp_headers = {}
        self._cookies = []
        self._current_page_num = 0
        self._lock = threading.Lock()

    def add_page(self, url, title, started_dt):
        self._current_page_num += 1
        page_id = f"page_{self._current_page_num}"
        self.pages.append({
            "startedDateTime": started_dt,
            "id": page_id,
            "title": title,
            "pageTimings": {"onContentLoad": -1, "onLoad": -1},
        })
        return page_id

    def reset_page_counter(self):
        """Reset page counter for per-strategy HAR files. Thread-safe."""
        with self._lock:
            self._current_page_num = 0
            self.pages = []
            self._requests = OrderedDict()
            self._responses = {}
            self._extra_req_headers = {}
            self._extra_resp_headers = {}

    def record_request(self, request_id, params):
        req = params.get("request", {})
        with self._lock:
            self._requests[request_id] = {
                "method": req.get("method", "GET"),
                "url": req.get("url", ""),
                "httpVersion": req.get("protocol", "http/1.1"),
                "headers": dict(req.get("headers", {})),
                "queryString": self._parse_qs(req.get("url", "")),
                "cookies": req.get("cookies", []),
                "bodySize": len(req.get("postData", "")) if req.get("postData") else 0,
                "postData": self._build_post_data(req),
                "wallTime": params.get("wallTime", time.time()),
                "initiator": params.get("initiator", {}),
                "page_id": self._current_page_num,
            }

    def record_response(self, request_id, params):
        resp = params.get("response", {})
        with self._lock:
            self._responses[request_id] = {
                "status": resp.get("status", 0),
                "statusText": resp.get("statusText", ""),
                "httpVersion": resp.get("protocol", "http/1.1"),
                "headers": dict(resp.get("headers", {})),
                "cookies": resp.get("cookies", []),
                "content": {
                    "size": resp.get("encodedDataLength", -1),
                    "mimeType": resp.get("mimeType", ""),
                    "text": "",
                    "encoding": None,
                },
                "redirectURL": "",
                "headersSize": -1,
                "bodySize": resp.get("encodedDataLength", -1),
                "remoteIPAddress": resp.get("remoteIPAddress", ""),
                "remotePort": resp.get("remotePort", -1),
            }

    def record_response_body(self, request_id, body, base64_encoded):
        with self._lock:
            if request_id in self._responses:
                content = self._responses[request_id]["content"]
                if base64_encoded:
                    content["text"] = body
                    content["encoding"] = "base64"
                else:
                    content["text"] = body
                    content["encoding"] = None
                if content["size"] < 0:
                    content["size"] = len(body) if body else 0

    def record_extra_req_headers(self, request_id, headers):
        with self._lock:
            self._extra_req_headers[request_id] = headers

    def record_extra_resp_headers(self, request_id, headers):
        with self._lock:
            self._extra_resp_headers[request_id] = headers

    def record_loading_finished(self, request_id, params):
        with self._lock:
            if request_id in self._responses:
                self._responses[request_id]["bodySize"] = params.get(
                    "encodedDataLength",
                    self._responses[request_id].get("bodySize", -1),
                )

    def record_cookies(self, cookies):
        with self._lock:
            for c in cookies:
                self._cookies.append({
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "domain": c.get("domain", ""),
                    "path": c.get("path", ""),
                    "expires": c.get("expires", -1),
                    "httpOnly": c.get("httpOnly", False),
                    "secure": c.get("secure", False),
                    "sameSite": c.get("sameSite", ""),
                    "size": c.get("size", 0),
                })

    def _parse_qs(self, url):
        parsed = urlparse(url)
        qs = []
        if parsed.query:
            for k, v in parse_qs(parsed.query, keep_blank_values=True).items():
                for val in v:
                    qs.append({"name": k, "value": val})
        return qs

    def _build_post_data(self, req):
        if req.get("postData"):
            return {
                "mimeType": req.get("headers", {}).get("Content-Type", ""),
                "text": req["postData"],
            }
        if req.get("hasPostData") and req.get("postDataEntries"):
            try:
                decoded = base64.b64decode(req["postDataEntries"][0]["bytes"]).decode("utf-8", errors="replace")
                return {"mimeType": req.get("headers", {}).get("Content-Type", ""), "text": decoded}
            except Exception:
                pass
        return None

    def _fmt_headers(self, d):
        return [{"name": k, "value": v} for k, v in d.items()]

    def build_har(self):
        with self._lock:
            entries = []
            for req_id, req_data in self._requests.items():
                resp_data = self._responses.get(req_id, {
                    "status": 0, "statusText": "", "httpVersion": "http/1.1",
                    "headers": {}, "cookies": [],
                    "content": {"size": 0, "mimeType": "", "text": ""},
                    "redirectURL": "", "headersSize": -1, "bodySize": -1,
                    "remoteIPAddress": "", "remotePort": -1,
                })
                req_headers = dict(req_data.get("headers", {}))
                if req_id in self._extra_req_headers:
                    req_headers.update(self._extra_req_headers[req_id])
                resp_headers = dict(resp_data.get("headers", {}))
                if req_id in self._extra_resp_headers:
                    resp_headers.update(self._extra_resp_headers[req_id])
                wall_time = req_data.get("wallTime", time.time())
                started_dt = datetime.fromtimestamp(wall_time, tz=timezone.utc).isoformat()
                entry = {
                    "pageref": f"page_{req_data.get('page_id', 1)}",
                    "startedDateTime": started_dt,
                    "time": 0,
                    "request": {
                        "method": req_data["method"],
                        "url": req_data["url"],
                        "httpVersion": req_data["httpVersion"],
                        "headers": self._fmt_headers(req_headers),
                        "queryString": req_data["queryString"],
                        "cookies": req_data["cookies"],
                        "headersSize": -1,
                        "bodySize": req_data["bodySize"],
                        "postData": req_data.get("postData"),
                    },
                    "response": {
                        "status": resp_data["status"],
                        "statusText": resp_data["statusText"],
                        "httpVersion": resp_data["httpVersion"],
                        "headers": self._fmt_headers(resp_headers),
                        "cookies": resp_data["cookies"],
                        "content": resp_data["content"],
                        "redirectURL": resp_data.get("redirectURL", ""),
                        "headersSize": resp_data["headersSize"],
                        "bodySize": resp_data["bodySize"],
                    },
                    "cache": {},
                    "timings": {"blocked": -1, "dns": -1, "connect": -1, "send": 0, "wait": 0, "receive": 0, "ssl": -1},
                    "serverIPAddress": resp_data.get("remoteIPAddress", ""),
                    "_initiator": req_data.get("initiator", {}),
                }
                entries.append(entry)
            return {
                "log": {
                    "version": "1.2",
                    "creator": {"name": "P123 CDP HAR Capture", "version": "2.0"},
                    "pages": self.pages,
                    "entries": entries,
                    "_cookies": self._cookies,
                }
            }


# ─── CDP Client (thread-safe, no deadlock) ───────────────────────────────────

class CDPCapture:
    def __init__(self, ws_url, cdp_port=CDP_PORT_DEFAULT):
        self.ws = websocket.create_connection(ws_url, enable_multithread=True)
        self._msg_id = 0
        self._send_lock = threading.Lock()
        self._cv = threading.Condition()
        self._results = {}
        self.har = HarBuilder()
        self._body_queue = queue.Queue()
        self._body_ok = 0
        self._body_fail = 0
        self._body_errors = []
        self._running = True
        self.cdp_port = cdp_port

        t = threading.Thread(target=self._read_loop, daemon=True)
        t.start()

        t2 = threading.Thread(target=self._body_fetch_loop, daemon=True)
        t2.start()

    def _read_loop(self):
        while self._running:
            try:
                raw = self.ws.recv()
                data = json.loads(raw)
                if "id" in data:
                    with self._cv:
                        self._results[data["id"]] = data
                        self._cv.notify_all()
                elif "method" in data:
                    self._dispatch_event(data["method"], data.get("params", {}))
            except websocket.WebSocketException:
                break
            except Exception as e:
                if self._running:
                    print(f"  [reader] error: {e}", flush=True)
                break

    def _body_fetch_loop(self):
        while self._running:
            try:
                request_id = self._body_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                result = self.send("Network.getResponseBody", {"requestId": request_id}, timeout=10)
                if "body" in result:
                    self.har.record_response_body(request_id, result["body"], result.get("base64Encoded", False))
                    self._body_ok += 1
                elif "error" in result:
                    self._body_fail += 1
                    if len(self._body_errors) < 10:
                        self._body_errors.append(f"  {request_id[:16]}... -> {result['error'].get('message','?')}")
            except Exception as e:
                self._body_fail += 1
                if len(self._body_errors) < 10:
                    self._body_errors.append(f"  {request_id[:16]}... -> exc: {e}")
            finally:
                self._body_queue.task_done()

    def send(self, method, params=None, timeout=30):
        with self._send_lock:
            self._msg_id += 1
            msg_id = self._msg_id
            msg = {"id": msg_id, "method": method}
            if params:
                msg["params"] = params
            self.ws.send(json.dumps(msg))

        deadline = time.time() + timeout
        with self._cv:
            while msg_id not in self._results:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return {"error": {"message": "timeout"}}
                self._cv.wait(timeout=remaining)
            result = self._results.pop(msg_id, {})

        if "error" in result:
            return {"error": result["error"]}
        return result.get("result", {})

    def _dispatch_event(self, method, params):
        request_id = params.get("requestId", "")
        if method == "Network.requestWillBeSent":
            self.har.record_request(request_id, params)
        elif method == "Network.requestWillBeSentExtraInfo":
            self.har.record_extra_req_headers(request_id, params.get("headers", {}))
        elif method == "Network.responseReceived":
            self.har.record_response(request_id, params)
        elif method == "Network.responseReceivedExtraInfo":
            self.har.record_extra_resp_headers(request_id, params.get("headers", {}))
        elif method == "Network.loadingFinished":
            self.har.record_loading_finished(request_id, params)
            self._body_queue.put(request_id)

    def fetch_cookies(self, urls=None):
        params = {}
        if urls:
            params["urls"] = urls
        result = self.send("Network.getCookies", params)
        cookies = result.get("cookies", [])
        self.har.record_cookies(cookies)
        print(f"  Captured {len(cookies)} cookies (including httpOnly)")
        return cookies

    def take_screenshot(self, filepath):
        try:
            result = self.send("Page.captureScreenshot", {"format": "png"}, timeout=15)
            if "data" in result:
                img_data = base64.b64decode(result["data"])
                with open(filepath, "wb") as f:
                    f.write(img_data)
                print(f"  📸 Screenshot saved: {os.path.basename(filepath)}")
                return True
            elif "error" in result:
                print(f"  Screenshot error: {result['error']}")
                return False
        except Exception as e:
            print(f"  Screenshot exception: {e}")
            return False

    def eval_js(self, expression, timeout=15):
        """Evaluate JavaScript in the page and return the result value."""
        result = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=timeout)
        return result.get("result", {}).get("value", None)

    def navigate_and_capture(self, url, page_title, take_shot=True, screenshot_dir=None,
                              load_wait=None, settle_wait=None):
        is_transaction_page = "transact_list.jsp" in url and "offset=" in url
        is_download_page = "DownloadPort" in url
        if load_wait is None:
            if is_download_page:
                load_wait = 5   # Downloads are simple responses
            elif is_transaction_page:
                load_wait = TRANSACTION_PAGE_LOAD_WAIT
            else:
                load_wait = PAGE_LOAD_WAIT
        if settle_wait is None:
            if is_download_page or is_transaction_page:
                settle_wait = 2
            else:
                settle_wait = SETTLE_WAIT
        take_shot = take_shot and not is_transaction_page and not is_download_page

        print(f"\n  Navigating to: {url[:100]}")

        started_dt = datetime.now(tz=timezone.utc).isoformat()
        page_id = self.har.add_page(url, page_title, started_dt)

        self._body_ok = 0
        self._body_fail = 0
        self._body_errors = []

        result = self.send("Page.navigate", {"url": url}, timeout=30)
        if "error" in result:
            print(f"  Navigation error: {result['error']}")
            return

        time.sleep(load_wait)
        time.sleep(settle_wait)

        # Wait for body queue to drain
        try:
            self._body_queue.join()
        except Exception:
            pass
        time.sleep(1)

        print(f"  Bodies: {self._body_ok} OK, {self._body_fail} failed")

        # Take screenshot
        if take_shot and screenshot_dir:
            parsed = urlparse(url)
            qs = parse_qs(parsed.query or "")
            sid = qs.get("portid", qs.get("id", [""]))[0]
            tab = qs.get("t", [""])[0]
            name_parts = [f"strategy_{sid}"] if sid else ["page"]
            if tab:
                name_parts.append(tab)
            elif "port_wiz" in url or "port_simul6" in url:
                name_parts.append("trading_system")
            elif "performance_stats" in url:
                name_parts.append("stats")
            elif "performance.jsp" in url:
                name_parts.append("chart")
            elif "holdings.jsp" in url:
                name_parts.append("holdings")
            screenshot_name = "_".join(name_parts) + ".png"
            self.take_screenshot(os.path.join(screenshot_dir, screenshot_name))

        self.fetch_cookies([url])

    def discover_transaction_count(self, portid):
        """Navigate to first transaction page and parse '1 - 50 of N' to get total count."""
        url = f"https://www.portfolio123.com/transact_list.jsp?portid={portid}"
        print(f"\n  Discovering transaction count for {portid}...")
        self.send("Page.navigate", {"url": url}, timeout=30)
        time.sleep(8)

        count = self.eval_js("""
            (function() {
                var el = document.getElementById('resulttotalcnt');
                if (el) return parseInt(el.getAttribute('rel') || el.textContent);
                var text = document.body.innerText;
                var m = text.match(/of\\s+(\\d+)/);
                return m ? parseInt(m[1]) : null;
            })()
        """)
        if count and isinstance(count, int) and count > 0:
            print(f"  Found {count} total transactions")
            return count
        print(f"  WARNING: Could not discover transaction count, using default 5000")
        return 5000

    def discover_ranking_system_id(self, portid):
        """Navigate to port_summary.jsp and find the ranking system link to get its ID."""
        url = f"https://www.portfolio123.com/port_summary.jsp?portid={portid}"
        print(f"\n  Discovering ranking system ID for {portid}...")
        self.send("Page.navigate", {"url": url}, timeout=30)
        time.sleep(8)

        ranking_id = self.eval_js("""
            (function() {
                var links = document.querySelectorAll('a[href*="ranking-system"]');
                for (var i = 0; i < links.length; i++) {
                    var href = links[i].getAttribute('href');
                    var m = href.match(/ranking-system\\/(\d+)/);
                    if (m) return parseInt(m[1]);
                }
                return null;
            })()
        """)
        if ranking_id and isinstance(ranking_id, int):
            print(f"  Found ranking system ID: {ranking_id}")
            return ranking_id
        print(f"  No ranking system link found (may be a Book without one)")
        return None

    def discover_strategy_info(self, portid, config=None):
        """Navigate to port_summary.jsp and extract strategy name, type, and ranking system ID.

        Returns dict with keys: name, type, ranking_id.
        name is the strategy display name (e.g. "AI Factor Small and Micro Cap Index 125 Stocks").
        type is one of: "Live Strategy", "Sim Strategy", "Live Book", "Sim Book", or None.
        ranking_id is an int or None.
        """
        url = f"https://www.portfolio123.com/port_summary.jsp?portid={portid}"
        print(f"\n  Discovering strategy info for {portid}...")
        self.send("Page.navigate", {"url": url}, timeout=30)
        time.sleep(config.get("page_load_wait", PAGE_LOAD_WAIT) if config else PAGE_LOAD_WAIT)
        time.sleep(config.get("settle_wait", SETTLE_WAIT) if config else SETTLE_WAIT)

        info = self.eval_js("""
            (function() {
                // Strategy name: P123 port_summary pages have it in #item-toolbar-name
                var title = document.title || '';
                var name = null;

                // 1. Primary: #item-toolbar-name element (verified on all P123 pages)
                var nameEl = document.getElementById('item-toolbar-name');
                if (nameEl) {
                    name = nameEl.textContent.trim();
                }

                // 2. Fallback: h1/h2 heading
                if (!name || name.length < 3) {
                    var headings = document.querySelectorAll('h1, h2, h3');
                    for (var i = 0; i < headings.length; i++) {
                        var t = headings[i].textContent.trim();
                        if (t.length > 3 && t.indexOf('Portfolio123') < 0 && t.indexOf('Summary') < 0) {
                            name = t;
                            break;
                        }
                    }
                }

                // Determine type from title
                var type = null;
                if (title.indexOf('Live Strategy') >= 0) type = 'Live Strategy';
                else if (title.indexOf('Simulated Book') >= 0) type = 'Sim Book';
                else if (title.indexOf('Live Book') >= 0) type = 'Live Book';
                else if (title.indexOf('Simulated Strategy') >= 0) type = 'Sim Strategy';

                // 3. Ranking system ID
                var rankingId = null;
                var links = document.querySelectorAll('a[href*="ranking-system"]');
                for (var i = 0; i < links.length; i++) {
                    var href = links[i].getAttribute('href');
                    var m = href.match(/ranking-system\\/(\d+)/);
                    if (m) { rankingId = parseInt(m[1]); break; }
                }

                return { name: name, type: type, ranking_id: rankingId };
            })()
        """)

        if info and isinstance(info, dict):
            name = info.get("name")
            stype = info.get("type")
            ranking_id = info.get("ranking_id")
            if name:
                print(f"  Strategy name: {name}")
            if stype:
                print(f"  Strategy type: {stype}")
            if ranking_id:
                print(f"  Ranking system ID: {ranking_id}")
            else:
                print(f"  No ranking system link found (may be a Book without one)")
            return {"name": name, "type": stype, "ranking_id": ranking_id}

        print(f"  Could not extract strategy info")
        return {"name": None, "type": None, "ranking_id": None}

    def save_strategy_har(self, strategy_id, output_dir, prefix="strategy"):
        """Build and save a HAR file for the current strategy."""
        har = self.har.build_har()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        har_path = os.path.join(output_dir, f"{prefix}_{strategy_id}_{timestamp}.har")
        with open(har_path, "w", encoding="utf-8") as f:
            json.dump(har, f, indent=2, ensure_ascii=False)
        entries = har["log"]["entries"]
        has_body = sum(1 for e in entries if e["response"]["content"].get("text"))
        print(f"\n  ✅ HAR saved: {os.path.basename(har_path)}")
        print(f"     Entries: {len(entries)}, With body: {has_body}, Pages: {len(har['log']['pages'])}")
        # Reset for next strategy
        self.har.reset_page_counter()
        return har_path

    def close(self):
        self._running = False
        self.ws.close()


# ─── URL Generation ──────────────────────────────────────────────────────────

def gen_shared_strategy_urls(portid, transact_count=None, ranking_id=None):
    """Generate all target URLs for a shared strategy.
    
    Uses bulk download endpoints instead of pagination — each download returns ALL data in one request.
    transact_count is ignored (kept for backward compatibility) — downloads get everything.
    """
    base = "https://www.portfolio123.com"
    urls = []

    # Main pages + bulk download endpoints
    for path_template, label in SHARED_STRATEGY_PAGES:
        url = f"{base}/{path_template.format(id=portid)}"
        urls.append((url, f"strategy_{portid}_{label}"))

    # Ranking system page (if ID known)
    if ranking_id:
        url = f"{base}/app/ranking-system/{ranking_id}"
        urls.append((url, f"strategy_{portid}_ranking_system"))

    return urls


def gen_managed_strategy_urls(strategy_id):
    """Generate all target URLs for a managed strategy (SPA tabs)."""
    base = "https://www.portfolio123.com/app/investment/details"
    urls = []
    for tab_param, label in MANAGED_STRATEGY_TABS:
        url = f"{base}?id={strategy_id}{tab_param}"
        urls.append((url, f"strategy_{strategy_id}_{label}"))
    return urls


def gen_account_urls(account_id):
    """Generate all target URLs for an account (SPA tabs, same structure as managed)."""
    base = "https://www.portfolio123.com/app/account/details"
    urls = []
    for tab_param, label in ACCOUNT_TABS:
        url = f"{base}?id={account_id}{tab_param}"
        urls.append((url, f"account_{account_id}_{label}"))
    return urls


# ─── Main ────────────────────────────────────────────────────────────────────

def get_websocket_url(cdp_port):
    resp = requests.get(f"http://{CDP_HOST}:{cdp_port}/json/list")
    tabs = resp.json()
    for tab in tabs:
        if tab.get("type") == "page":
            return tab["webSocketDebuggerUrl"]
    resp = requests.get(f"http://{CDP_HOST}:{cdp_port}/json/new")
    tab = resp.json()
    return tab["webSocketDebuggerUrl"]


def load_config(config_path):
    """Load JSON config file and merge with defaults."""
    config = dict(DEFAULT_CONFIG)
    if config_path:
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = json.load(f)
        config.update(user_config)
    return config


def har_exists_for_strategy(strategy_id, output_dir, strategy_type="strategy"):
    """Check if a HAR file already exists for this strategy (resume support)."""
    pattern = os.path.join(output_dir, f"{strategy_type}_{strategy_id}_*.har")
    import glob
    existing = glob.glob(pattern)
    return len(existing) > 0


# ─── Download Organization ───────────────────────────────────────────────────

# Map DownloadPort URL path segments to our file type names
DOWNLOAD_TYPE_MAP = {
    "DownloadPortTrans": "transactions",
    "DownloadPortHoldings": "holdings",
    "DownloadPortPerf": "perf",
    "DownloadPortPeriodPerformanceDist": "period_perf_dist",
    "DownloadPortPeriodPerformance": "period_perf",
}

# Order matters: check longer prefixes first (PeriodPerformanceDist before PeriodPerformance)
DOWNLOAD_ENDPOINTS_ORDERED = [
    "DownloadPortPeriodPerformanceDist",
    "DownloadPortPeriodPerformance",
    "DownloadPortTrans",
    "DownloadPortHoldings",
    "DownloadPortPerf",
]


def save_name_map(name_map, output_dir):
    """Write/update strategy_name_map.json in the output directory."""
    map_path = os.path.join(output_dir, "strategy_name_map.json")
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump(name_map, f, indent=2, ensure_ascii=False)


def load_name_map(output_dir):
    """Load existing strategy_name_map.json if it exists (for resume)."""
    map_path = os.path.join(output_dir, "strategy_name_map.json")
    if os.path.exists(map_path):
        with open(map_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def extract_download_type_from_url(url):
    """Extract the download type key from a DownloadPort URL."""
    for endpoint in DOWNLOAD_ENDPOINTS_ORDERED:
        if endpoint in url:
            return DOWNLOAD_TYPE_MAP[endpoint]
    return None


def find_and_rename_downloads(strategy_id, download_dir, system_download_dir=None):
    """Find downloaded P123 CSV files for this strategy and rename them.

    Looks for recently-modified P123_*.csv files in the download directory,
    matches by filename prefix to download type, and renames to
    strategy_{id}_{type}.csv in the download_dir.

    Returns list of (old_path, new_path) tuples for renamed files.
    """
    if system_download_dir is None:
        system_download_dir = os.path.join(os.path.expanduser("~"), "Downloads")

    renamed = []
    now = time.time()

    # Find all P123_*.csv files modified in the last 5 minutes
    candidates = []
    for f in os.listdir(system_download_dir):
        if not f.startswith("P123_") or not f.endswith(".csv"):
            continue
        fpath = os.path.join(system_download_dir, f)
        mtime = os.path.getmtime(fpath)
        if now - mtime < 300:  # modified in last 5 minutes (generous window for slow strategies)
            candidates.append((fpath, f, mtime))

    if not candidates:
        return renamed

    # Sort by modification time (oldest first)
    candidates.sort(key=lambda x: x[2])

    # Match each candidate to a download type by filename prefix
    used_types = set()
    for fpath, fname, mtime in candidates:
        # Determine download type from filename prefix
        dtype = None
        for prefix in ["P123_PortPeriodPerfDist_", "P123_PortPeriodPerf_", "P123_PortHoldings_",
                        "P123_PortPerf_", "P123_SimTran_"]:
            if fname.startswith(prefix):
                # Map prefix to our type name
                if "PortPeriodPerfDist" in prefix:
                    dtype = "period_perf_dist"
                elif "PortPeriodPerf" in prefix:
                    dtype = "period_perf"
                elif "PortHoldings" in prefix:
                    dtype = "holdings"
                elif "PortPerf" in prefix:
                    dtype = "perf"
                elif "SimTran" in prefix:
                    dtype = "transactions"
                break

        if dtype is None or dtype in used_types:
            continue

        used_types.add(dtype)

        # Move and rename
        new_name = f"strategy_{strategy_id}_{dtype}.csv"
        new_path = os.path.join(download_dir, new_name)

        # If destination already exists (from a previous run), keep the larger file
        if os.path.exists(new_path):
            old_size = os.path.getsize(new_path)
            new_size = os.path.getsize(fpath)
            if new_size > old_size:
                os.remove(new_path)
            else:
                os.remove(fpath)
                print(f"  Deleted duplicate download (smaller than existing): {fname}")
                continue

        # Rename with retry for Windows file-in-use errors
        for attempt in range(3):
            try:
                os.rename(fpath, new_path)
                renamed.append((fpath, new_path))
                print(f"  Renamed download: {fname} → {new_name}")
                break
            except PermissionError:
                if attempt < 2:
                    time.sleep(1)
                else:
                    print(f"  WARNING: Could not rename {fname} (file in use after 3 retries)")
            except OSError as e:
                print(f"  WARNING: Could not rename {fname}: {e}")
                break

    return renamed


def capture_strategy(capture, strategy_id, strategy_type, config, output_dir, screenshot_dir,
                     name_map=None, download_dir=None):
    """Capture all pages for a single strategy and save its HAR.

    Also extracts strategy name, updates the name map, and renames downloaded CSVs.
    """
    print(f"\n{'='*60}")
    print(f"Capturing strategy {strategy_id} ({strategy_type})")
    print(f"{'='*60}")

    # Generate URLs based on strategy type
    if strategy_type == "shared":
        # Discover strategy name, type, and ranking system ID in one navigation
        info = capture.discover_strategy_info(strategy_id, config)
        strategy_name = info.get("name")
        strategy_p123_type = info.get("type")
        ranking_id = info.get("ranking_id")

        # Update name map
        if name_map is not None and strategy_name:
            name_map[strategy_id] = {
                "name": strategy_name,
                "type": strategy_p123_type,
            }
            save_name_map(name_map, SCRIPT_DIR)

        # Fall back to config ranking_system_ids if discovery failed
        if not ranking_id:
            ranking_ids = config.get("ranking_system_ids", {})
            ranking_id = ranking_ids.get(str(strategy_id))

        urls = gen_shared_strategy_urls(strategy_id, ranking_id=ranking_id)
        # Skip port_summary.jsp — discover_strategy_info already navigated to it
        # and it's the first URL in the list. This saves ~17 seconds per strategy.
        if urls and 'port_summary.jsp' in urls[0][0]:
            urls = urls[1:]  # skip the duplicate port_summary navigation
    elif strategy_type == "managed":
        urls = gen_managed_strategy_urls(strategy_id)
    elif strategy_type == "account":
        urls = gen_account_urls(strategy_id)
    else:
        print(f"  Unknown strategy type: {strategy_type}")
        return

    print(f"  Total pages to capture: {len(urls)}")

    # Navigate to each URL
    for url, title in urls:
        capture.navigate_and_capture(
            url, title,
            take_shot=config.get("take_screenshots", True),
            screenshot_dir=screenshot_dir,
            load_wait=config.get("page_load_wait", PAGE_LOAD_WAIT),
            settle_wait=config.get("settle_wait", SETTLE_WAIT),
        )

    # Save per-strategy HAR
    prefix = "account" if strategy_type == "account" else "strategy"
    har_path = capture.save_strategy_har(strategy_id, output_dir, prefix)

    # Rename downloaded CSV files (shared strategies only — they trigger DownloadPort downloads)
    if strategy_type == "shared" and download_dir:
        find_and_rename_downloads(strategy_id, download_dir)

    return har_path


def main():
    parser = argparse.ArgumentParser(
        description="CDP HAR Capture for Portfolio123 strategies",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Capture from config file
  python p123_har_capture.py --config p123_config.json

  # Capture specific strategies
  python p123_har_capture.py --shared 1853536,1735193 --managed 1913284

  # Capture with custom output dir and no screenshots
  python p123_har_capture.py --shared 1853536 --output-dir /tmp/p123_har --no-screenshots

  # Resume (skip strategies with existing HAR files)
  python p123_har_capture.py --config p123_config.json --resume
        """,
    )
    parser.add_argument("--config", help="JSON config file path")
    parser.add_argument("--shared", help="Comma-separated shared strategy portids")
    parser.add_argument("--managed", help="Comma-separated managed strategy ids")
    parser.add_argument("--output-dir", help="Output directory for HAR files")
    parser.add_argument("--screenshot-dir", help="Screenshot directory")
    parser.add_argument("--no-screenshots", action="store_true", help="Disable screenshots")
    parser.add_argument("--cdp-port", type=int, default=CDP_PORT_DEFAULT, help="CDP port (default 9222)")
    parser.add_argument("--resume", action="store_true", help="Skip strategies with existing HAR files")
    parser.add_argument("--page-load-wait", type=int, help="Page load wait in seconds")
    parser.add_argument("--settle-wait", type=int, help="Settle wait in seconds")
    args = parser.parse_args()

    # Load config
    config = load_config(args.config)

    # Override with CLI args
    if args.output_dir:
        config["output_dir"] = args.output_dir
    if args.screenshot_dir:
        config["screenshot_dir"] = args.screenshot_dir
    if args.no_screenshots:
        config["take_screenshots"] = False
    if args.cdp_port:
        config["cdp_port"] = args.cdp_port
    if args.page_load_wait:
        config["page_load_wait"] = args.page_load_wait
    if args.settle_wait:
        config["settle_wait"] = args.settle_wait
    if args.shared:
        config["shared_strategies"] = [int(x.strip()) for x in args.shared.split(",")]
    if args.managed:
        config["managed_strategies"] = [int(x.strip()) for x in args.managed.split(",")]

    output_dir = config["output_dir"]
    screenshot_dir = config["screenshot_dir"]
    download_dir = os.path.join(SCRIPT_DIR, "downloads")
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(screenshot_dir, exist_ok=True)
    os.makedirs(download_dir, exist_ok=True)

    # Load existing name map (for resume)
    name_map = load_name_map(SCRIPT_DIR)

    # Build strategy list
    strategies = []
    for sid in config.get("shared_strategies", []):
        strategies.append((str(sid), "shared"))
    for sid in config.get("managed_strategies", []):
        strategies.append((str(sid), "managed"))
    for sid in config.get("account_ids", []):
        strategies.append((str(sid), "account"))

    if not strategies:
        print("ERROR: No strategies configured. Use --shared, --managed, or --config.")
        sys.exit(1)

    print(f"P123 HAR Capture — {len(strategies)} strategies")
    print(f"Output: {output_dir}")
    print(f"Downloads: {download_dir}")
    print(f"Screenshots: {screenshot_dir}")
    print(f"CDP port: {config['cdp_port']}")

    # Filter out already-captured strategies if --resume
    if args.resume:
        before = len(strategies)
        filtered = []
        needs_download_rename = []
        for sid, st in strategies:
            prefix = "account" if st == "account" else "strategy"
            har_exists = har_exists_for_strategy(sid, output_dir, prefix)
            if not har_exists:
                filtered.append((sid, st))
            elif st == "shared":
                # HAR exists but check if downloads were renamed
                import glob as glb
                dl_pattern = os.path.join(download_dir, f"strategy_{sid}_*.csv")
                if not glb.glob(dl_pattern):
                    # HAR exists but downloads weren't renamed — need to rename only
                    needs_download_rename.append((sid, st))
                # else: fully captured, skip
        strategies = filtered
        skipped = before - len(strategies)
        if skipped:
            print(f"Resume: skipping {skipped} fully-captured strategies")
        if needs_download_rename:
            print(f"Resume: {len(needs_download_rename)} strategies need download rename only")
            for sid, st in needs_download_rename:
                find_and_rename_downloads(sid, download_dir)
        if not strategies:
            print("All strategies already captured. Nothing to do.")
            return

    # Connect to CDP
    try:
        ws_url = get_websocket_url(config["cdp_port"])
        print(f"Connected to CDP: {ws_url[:60]}...")
    except Exception as e:
        print(f"ERROR: Cannot connect to CDP at {CDP_HOST}:{config['cdp_port']}")
        print(f"  Error: {e}")
        print(f"\nMake sure Edge is running with:")
        print(f"  --remote-debugging-port={config['cdp_port']} --remote-allow-origins=*")
        sys.exit(1)

    capture = CDPCapture(ws_url, config["cdp_port"])

    try:
        print("\nEnabling CDP domains...")
        capture.send("Network.enable", {
            "maxTotalBufferSize": 100 * 1024 * 1024,
            "maxResourceBufferSize": 10 * 1024 * 1024,
            "maxPostDataSize": 10 * 1024 * 1024,
        })
        capture.send("Page.enable")
        capture.send("Runtime.enable")

        har_files = []
        for i, (strategy_id, strategy_type) in enumerate(strategies, 1):
            print(f"\n[{i}/{len(strategies)}] ", end="")
            har_path = capture_strategy(
                capture, strategy_id, strategy_type, config,
                output_dir, screenshot_dir,
                name_map=name_map, download_dir=download_dir,
            )
            if har_path:
                har_files.append(har_path)

        # Save final name map
        save_name_map(name_map, SCRIPT_DIR)
        print(f"\n✅ Strategy name map saved: strategy_name_map.json ({len(name_map)} entries)")

        # Save global cookies
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        cookies_path = os.path.join(output_dir, f"p123_cookies_{timestamp}.json")
        with open(cookies_path, "w", encoding="utf-8") as f:
            json.dump(capture.har._cookies, f, indent=2, ensure_ascii=False)
        print(f"\n✅ Global cookies saved: {os.path.basename(cookies_path)}")

        # Summary
        print(f"\n{'='*60}")
        print(f"Capture complete — {len(har_files)} HAR files created")
        print(f"{'='*60}")
        for hp in har_files:
            size = os.path.getsize(hp)
            print(f"  {os.path.basename(hp)} ({size / 1024 / 1024:.1f} MB)")

    finally:
        capture.close()
        print("\nDone!")


if __name__ == "__main__":
    main()
