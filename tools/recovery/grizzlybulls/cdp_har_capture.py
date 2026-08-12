#!/usr/bin/env python3
"""CDP HAR Capture — Generic Reusable Tool (copied from browser-har-capture skill template)"""

import argparse
import base64
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
                    "creator": {"name": "CDP HAR Capture", "version": "3.1"},
                    "pages": self.pages,
                    "entries": entries,
                    "_cookies": self._cookies,
                }
            }


class CDPCapture:
    def __init__(self, ws_url, page_load_wait=10, settle_wait=5, screenshot=False, tab_selectors=None):
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
        self.page_load_wait = page_load_wait
        self.settle_wait = settle_wait
        self.screenshot = screenshot
        self.tab_selectors = tab_selectors or []
        self.screenshots = []

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
            except Exception as e:
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
        print(f"  Captured {len(cookies)} cookies")
        return cookies

    def navigate_and_capture(self, url, page_title, index, output_dir, site_name):
        print(f"\n{'='*60}")
        print(f"[{index}] {url}")
        print(f"{'='*60}")
        started_dt = datetime.now(tz=timezone.utc).isoformat()
        self.har.add_page(url, page_title, started_dt)
        self._body_ok = 0
        self._body_fail = 0
        result = self.send("Page.navigate", {"url": url}, timeout=30)
        if "error" in result:
            print(f"  Navigation error: {result['error']}")
            return
        print(f"  Waiting {self.page_load_wait}s...")
        time.sleep(self.page_load_wait)
        time.sleep(self.settle_wait)
        time.sleep(2)
        print(f"  Bodies: {self._body_ok} OK, {self._body_fail} failed")

    def close(self):
        self._running = False
        self.ws.close()


def get_websocket_url(cdp_host, cdp_port):
    resp = requests.get(f"http://{cdp_host}:{cdp_port}/json/list")
    tabs = resp.json()
    for tab in tabs:
        if tab.get("type") == "page":
            return tab["webSocketDebuggerUrl"]
    resp = requests.get(f"http://{cdp_host}:{cdp_port}/json/new")
    tab = resp.json()
    return tab["webSocketDebuggerUrl"]


def main():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gb_capture_config.json")
    with open(config_path) as f:
        cfg = json.load(f)
    
    output_dir = cfg["output_dir"]
    site_name = cfg["site_name"]
    target_pages = cfg["target_pages"]
    cookie_urls = cfg.get("cookie_urls", ["https://grizzlybulls.com"])
    page_load_wait = cfg.get("page_load_wait", 5)
    settle_wait = cfg.get("settle_wait", 3)
    cdp_port = cfg.get("cdp_port", 9222)
    
    cdp_host = "localhost"
    os.makedirs(output_dir, exist_ok=True)
    
    print(f"CDP HAR Capture — {site_name}")
    print(f"  Output: {output_dir}")
    print(f"  Pages: {len(target_pages)}")
    
    ws_url = get_websocket_url(cdp_host, cdp_port)
    print(f"  Connected: {ws_url[:80]}...")
    
    capture = CDPCapture(ws_url, page_load_wait=page_load_wait, settle_wait=settle_wait)
    
    try:
        capture.send("Network.enable", {
            "maxTotalBufferSize": 100 * 1024 * 1024,
            "maxResourceBufferSize": 10 * 1024 * 1024,
            "maxPostDataSize": 10 * 1024 * 1024,
        })
        capture.send("Page.enable")
        capture.send("Runtime.enable")
        
        for i, url in enumerate(target_pages, 1):
            title = urlparse(url).path.strip("/") or "root"
            capture.navigate_and_capture(url, title, i, output_dir, site_name)
        
        capture.fetch_cookies(cookie_urls)
        
        har = capture.har.build_har()
        entries = har["log"]["entries"]
        has_body = sum(1 for e in entries if e["response"]["content"].get("text"))
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        har_path = os.path.join(output_dir, f"{site_name}_{timestamp}.har")
        api_path = os.path.join(output_dir, f"{site_name}_api_summary_{timestamp}.json")
        
        with open(har_path, "w", encoding="utf-8") as f:
            json.dump(har, f, indent=2, ensure_ascii=False)
        
        # API summary
        api_calls = []
        for entry in entries:
            url = entry["request"]["url"]
            mime = entry["response"]["content"].get("mimeType", "")
            if "json" in mime or "api" in url or "/_next/data/" in url:
                api_calls.append({
                    "url": url,
                    "method": entry["request"]["method"],
                    "status": entry["response"]["status"],
                    "mimeType": mime,
                    "hasBody": bool(entry["response"]["content"].get("text")),
                })
        
        with open(api_path, "w", encoding="utf-8") as f:
            json.dump(api_calls, f, indent=2, ensure_ascii=False)
        
        print(f"\n{'='*60}")
        print(f"SUMMARY")
        print(f"  Pages: {len(har['log']['pages'])}")
        print(f"  Entries: {len(entries)}")
        print(f"  Bodies: {has_body}/{len(entries)}")
        print(f"  API calls: {len(api_calls)}")
        print(f"  HAR: {har_path}")
        print(f"  API summary: {api_path}")
        print(f"{'='*60}")
    finally:
        capture.close()
        print("\nDone.")


if __name__ == "__main__":
    main()
