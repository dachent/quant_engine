#!/usr/bin/env python3
"""
CDP HAR Capture for TuringTrader — connects to a specific tab, monitors
all network traffic, and saves a HAR file when signaled (via 'done.txt' file
or timeout). Three-thread architecture from the proven cdp_har_capture.py.
"""

import json
import os
import queue
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import requests
import websocket

# ─── HAR Builder (same proven code from cdp_har_capture.py) ──────────────────

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
            import base64
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
                    "creator": {"name": "CDP HAR Capture (TT)", "version": "1.0"},
                    "pages": self.pages,
                    "entries": entries,
                    "_cookies": self._cookies,
                }
            }


# ─── CDP Client (same proven three-thread architecture) ──────────────────────

class CDPCapture:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, enable_multithread=True)
        self._msg_id = 0
        self._send_lock = threading.Lock()
        self._cv = threading.Condition()
        self._results = {}
        self.har = HarBuilder()
        self._body_queue = queue.Queue()
        self._body_ok = 0
        self._body_fail = 0
        self._running = True
        self._api_count = 0
        self._last_api_time = time.time()

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
            except Exception:
                self._body_fail += 1
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
            url = params.get("response", {}).get("url", "")
            mime = params.get("response", {}).get("mimeType", "")
            if "json" in mime or "api" in url or "/v1/" in url or "/v2/" in url:
                self._api_count += 1
                self._last_api_time = time.time()
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
        return cookies

    def close(self):
        self._running = False
        self.ws.close()

    def get_stats(self):
        with self.har._lock:
            total = len(self.har._requests)
        return {
            "total_requests": total,
            "bodies_ok": self._body_ok,
            "bodies_fail": self._body_fail,
            "api_count": self._api_count,
            "last_api_ago": int(time.time() - self._last_api_time),
        }


# ─── Main ────────────────────────────────────────────────────────────────────

OUTPUT_DIR = r"C:\Users\BorisVaisman\OneDrive - Fertility Specialists Network\Documents\Playground\har_turingtrader\har\raw"
SIGNAL_FILE = os.path.join(OUTPUT_DIR, "done.txt")
TIMEOUT_SECONDS = 900  # 10 minutes max
IDLE_TIMEOUT = 300     # save after 2 min of no API activity

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    # Remove stale signal file
    if os.path.exists(SIGNAL_FILE):
        os.remove(SIGNAL_FILE)

    # Find the TuringTrader tab
    resp = requests.get("http://127.0.0.1:9222/json/list")
    tabs = resp.json()
    ws_url = None
    for tab in tabs:
        if tab.get("type") == "page" and "turingtrader" in tab.get("url", "").lower():
            ws_url = tab["webSocketDebuggerUrl"]
            print(f"Found TuringTrader tab: {tab['url'][:80]}", flush=True)
            break
    if not ws_url:
        print("ERROR: No TuringTrader tab found!", flush=True)
        sys.exit(1)

    print(f"Connecting to: {ws_url}", flush=True)
    capture = CDPCapture(ws_url)

    try:
        print("Enabling CDP domains...", flush=True)
        capture.send("Network.enable", {
            "maxTotalBufferSize": 100 * 1024 * 1024,
            "maxResourceBufferSize": 10 * 1024 * 1024,
            "maxPostDataSize": 10 * 1024 * 1024,
        })
        capture.send("Page.enable")
        capture.send("Runtime.enable")

        # Add initial page
        capture.har.add_page(
            "https://www.turingtrader.com/",
            "TuringTrader Session",
            datetime.now(tz=timezone.utc).isoformat()
        )

        print(f"\nCapture active! Browse TuringTrader in the managed Edge browser.", flush=True)
        print(f"  - Signal file to stop: {SIGNAL_FILE}", flush=True)
        print(f"  - Auto-save after {IDLE_TIMEOUT}s of no API activity", flush=True)
        print(f"  - Hard timeout: {TIMEOUT_SECONDS}s", flush=True)
        print(f"  - Stats will print every 15 seconds\n", flush=True)

        start_time = time.time()
        last_stats = 0

        while True:
            time.sleep(1)
            elapsed = int(time.time() - start_time)

            # Print stats every 15 seconds
            if elapsed - last_stats >= 15:
                stats = capture.get_stats()
                print(f"  [{elapsed}s] requests={stats['total_requests']} "
                      f"api={stats['api_count']} bodies_ok={stats['bodies_ok']} "
                      f"bodies_fail={stats['bodies_fail']} "
                      f"last_api={stats['last_api_ago']}s ago", flush=True)
                last_stats = elapsed

            # Check for signal file
            if os.path.exists(SIGNAL_FILE):
                print("\nSignal file detected — saving HAR...", flush=True)
                break

            # Check idle timeout (only after we've seen some API calls)
            stats = capture.get_stats()
            if stats["api_count"] > 0 and stats["last_api_ago"] > IDLE_TIMEOUT:
                print(f"\n{IDLE_TIMEOUT}s idle after {stats['api_count']} API calls — saving HAR...", flush=True)
                break

            # Check hard timeout
            if elapsed >= TIMEOUT_SECONDS:
                print(f"\nHard timeout ({TIMEOUT_SECONDS}s) — saving HAR...", flush=True)
                break

        # Fetch cookies for TuringTrader domain
        print("Fetching cookies...", flush=True)
        capture.fetch_cookies(["https://www.turingtrader.com/"])

        # Build and save HAR
        print("Building HAR...", flush=True)
        har = capture.har.build_har()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        har_path = os.path.join(OUTPUT_DIR, f"turingtrader_{timestamp}.har")

        with open(har_path, "w", encoding="utf-8") as f:
            json.dump(har, f, indent=2, ensure_ascii=False)

        entries = har["log"]["entries"]
        has_body = sum(1 for e in entries if e["response"]["content"].get("text"))
        api_entries = [e for e in entries if "json" in e["response"]["content"].get("mimeType", "")]

        print(f"\n{'='*60}", flush=True)
        print(f"CAPTURE COMPLETE", flush=True)
        print(f"{'='*60}", flush=True)
        print(f"  Total entries:      {len(entries)}", flush=True)
        print(f"  With response body: {has_body}/{len(entries)}", flush=True)
        print(f"  API/JSON responses: {len(api_entries)}", flush=True)
        print(f"  Cookies:            {len(har['log']['_cookies'])}", flush=True)
        print(f"  HAR file:           {har_path}", flush=True)

        # Print API endpoint summary
        if api_entries:
            print(f"\n  API endpoints found:", flush=True)
            seen = set()
            for e in api_entries:
                url = e["request"]["url"]
                method = e["request"]["method"]
                status = e["response"]["status"]
                key = f"{method} {url.split('?')[0]}"
                if key not in seen:
                    seen.add(key)
                    print(f"    {status} {method:6s} {url.split('?')[0]}", flush=True)

        print(f"{'='*60}", flush=True)

    finally:
        capture.close()
        print("Done.", flush=True)


if __name__ == "__main__":
    main()
