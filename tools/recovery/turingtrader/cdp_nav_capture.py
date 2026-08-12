#!/usr/bin/env python3
"""
Simplified CDP capture for TuringTrader — creates a new tab per page,
navigates, captures all traffic with response bodies, saves one combined HAR.
Uses the proven 3-thread architecture from cdp_har_capture.py.
"""

import json
import os
import queue
import sys
import threading
import time
import base64
from collections import OrderedDict, Counter
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import requests
import websocket

OUTPUT_DIR = r"C:\Users\BorisVaisman\OneDrive - Fertility Specialists Network\Documents\Playground\har_turingtrader\har\raw"
TARGET_PAGES_FILE = os.path.join(OUTPUT_DIR, "target_pages.txt")
CDP_PORT = 9222


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
        self.pages.append({"startedDateTime": started_dt, "id": page_id, "title": title, "pageTimings": {"onContentLoad": -1, "onLoad": -1}})
        return page_id

    def record_request(self, request_id, params):
        req = params.get("request", {})
        with self._lock:
            self._requests[request_id] = {
                "method": req.get("method", "GET"), "url": req.get("url", ""),
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
                "status": resp.get("status", 0), "statusText": resp.get("statusText", ""),
                "httpVersion": resp.get("protocol", "http/1.1"),
                "headers": dict(resp.get("headers", {})), "cookies": resp.get("cookies", []),
                "content": {"size": resp.get("encodedDataLength", -1), "mimeType": resp.get("mimeType", ""), "text": "", "encoding": None},
                "redirectURL": "", "headersSize": -1, "bodySize": resp.get("encodedDataLength", -1),
                "remoteIPAddress": resp.get("remoteIPAddress", ""), "remotePort": resp.get("remotePort", -1),
            }

    def record_response_body(self, request_id, body, base64_encoded):
        with self._lock:
            if request_id in self._responses:
                content = self._responses[request_id]["content"]
                content["text"] = body
                content["encoding"] = "base64" if base64_encoded else None
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
                self._responses[request_id]["bodySize"] = params.get("encodedDataLength", self._responses[request_id].get("bodySize", -1))

    def record_cookies(self, cookies):
        with self._lock:
            for c in cookies:
                self._cookies.append({"name": c.get("name", ""), "value": c.get("value", ""), "domain": c.get("domain", ""),
                    "path": c.get("path", ""), "expires": c.get("expires", -1), "httpOnly": c.get("httpOnly", False),
                    "secure": c.get("secure", False), "sameSite": c.get("sameSite", ""), "size": c.get("size", 0)})

    def _parse_qs(self, url):
        parsed = urlparse(url)
        return [{"name": k, "value": v} for k, vs in parse_qs(parsed.query, keep_blank_values=True).items() for v in vs] if parsed.query else []

    def _build_post_data(self, req):
        if req.get("postData"):
            return {"mimeType": req.get("headers", {}).get("Content-Type", ""), "text": req["postData"]}
        if req.get("hasPostData") and req.get("postDataEntries"):
            try:
                return {"mimeType": req.get("headers", {}).get("Content-Type", ""), "text": base64.b64decode(req["postDataEntries"][0]["bytes"]).decode("utf-8", errors="replace")}
            except: pass
        return None

    def _fmt_headers(self, d):
        return [{"name": k, "value": v} for k, v in d.items()]

    def build_har(self):
        with self._lock:
            entries = []
            for req_id, req_data in self._requests.items():
                resp_data = self._responses.get(req_id, {"status": 0, "statusText": "", "httpVersion": "http/1.1", "headers": {}, "cookies": [], "content": {"size": 0, "mimeType": "", "text": ""}, "redirectURL": "", "headersSize": -1, "bodySize": -1, "remoteIPAddress": "", "remotePort": -1})
                req_headers = dict(req_data.get("headers", {}))
                if req_id in self._extra_req_headers: req_headers.update(self._extra_req_headers[req_id])
                resp_headers = dict(resp_data.get("headers", {}))
                if req_id in self._extra_resp_headers: resp_headers.update(self._extra_resp_headers[req_id])
                started_dt = datetime.fromtimestamp(req_data.get("wallTime", time.time()), tz=timezone.utc).isoformat()
                entries.append({"pageref": f"page_{req_data.get('page_id', 1)}", "startedDateTime": started_dt, "time": 0,
                    "request": {"method": req_data["method"], "url": req_data["url"], "httpVersion": req_data["httpVersion"],
                        "headers": self._fmt_headers(req_headers), "queryString": req_data["queryString"], "cookies": req_data["cookies"],
                        "headersSize": -1, "bodySize": req_data["bodySize"], "postData": req_data.get("postData")},
                    "response": {"status": resp_data["status"], "statusText": resp_data["statusText"], "httpVersion": resp_data["httpVersion"],
                        "headers": self._fmt_headers(resp_headers), "cookies": resp_data["cookies"], "content": resp_data["content"],
                        "redirectURL": resp_data.get("redirectURL", ""), "headersSize": resp_data["headersSize"], "bodySize": resp_data["bodySize"]},
                    "cache": {}, "timings": {"blocked": -1, "dns": -1, "connect": -1, "send": 0, "wait": 0, "receive": 0, "ssl": -1},
                    "serverIPAddress": resp_data.get("remoteIPAddress", ""), "_initiator": req_data.get("initiator", {})})
            return {"log": {"version": "1.2", "creator": {"name": "CDP TT Nav Capture", "version": "2.0"}, "pages": self.pages, "entries": entries, "_cookies": self._cookies}}


class CDPCapture:
    """Proven 3-thread CDP capture for a single tab."""
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
        self._page_entries = []

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
            except: break

    def _body_fetch_loop(self):
        while self._running:
            try:
                rid = self._body_queue.get(timeout=1)
            except queue.Empty: continue
            try:
                result = self.send("Network.getResponseBody", {"requestId": rid}, timeout=10)
                if "body" in result:
                    self.har.record_response_body(rid, result["body"], result.get("base64Encoded", False))
                    self._body_ok += 1
                else:
                    self._body_fail += 1
            except: self._body_fail += 1
            finally: self._body_queue.task_done()

    def send(self, method, params=None, timeout=30):
        with self._send_lock:
            self._msg_id += 1
            mid = self._msg_id
        msg = {"id": mid, "method": method}
        if params: msg["params"] = params
        self.ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        with self._cv:
            while mid not in self._results:
                if time.time() > deadline: return {"error": {"message": "timeout"}}
                self._cv.wait(timeout=deadline - time.time())
            result = self._results.pop(mid, {})
        return result.get("result", {}) if "error" not in result else {"error": result["error"]}

    def _dispatch_event(self, method, params):
        rid = params.get("requestId", "")
        if method == "Network.requestWillBeSent":
            self.har.record_request(rid, params)
            self._page_entries.append(rid)
        elif method == "Network.requestWillBeSentExtraInfo":
            self.har.record_extra_req_headers(rid, params.get("headers", {}))
        elif method == "Network.responseReceived":
            self.har.record_response(rid, params)
        elif method == "Network.responseReceivedExtraInfo":
            self.har.record_extra_resp_headers(rid, params.get("headers", {}))
        elif method == "Network.loadingFinished":
            self.har.record_loading_finished(rid, params)
            self._body_queue.put(rid)

    def fetch_cookies(self, urls):
        result = self.send("Network.getCookies", {"urls": urls})
        cookies = result.get("cookies", [])
        self.har.record_cookies(cookies)
        return cookies

    def close(self):
        self._running = False
        self.ws.close()


def main():
    with open(TARGET_PAGES_FILE, 'r') as f:
        all_pages = [line.strip() for line in f if line.strip()]

    # Capture first 25 priority pages
    pages_to_capture = all_pages[:25]
    print(f"CDP Navigation Capture for TuringTrader", flush=True)
    print(f"  Pages: {len(pages_to_capture)}", flush=True)
    print(f"  CDP port: {CDP_PORT}", flush=True)

    combined_har = HarBuilder()
    all_api_endpoints = set()

    for idx, url in enumerate(pages_to_capture, 1):
        print(f"\n[{idx}/{len(pages_to_capture)}] {url}", flush=True)

        # Create new tab
        try:
            resp = requests.put(f"http://127.0.0.1:{CDP_PORT}/json/new?{url}", timeout=10)
            tab = resp.json()
            ws_url = tab["webSocketDebuggerUrl"]
            tab_id = tab["id"]
        except Exception as e:
            print(f"  ERROR creating tab: {e}", flush=True)
            continue

        # Connect and capture
        try:
            capture = CDPCapture(ws_url)
        except Exception as e:
            print(f"  ERROR connecting: {e}", flush=True)
            continue

        try:
            capture.send("Network.enable", {"maxTotalBufferSize": 100*1024*1024, "maxResourceBufferSize": 10*1024*1024, "maxPostDataSize": 10*1024*1024})
            capture.send("Page.enable")
            capture.send("Runtime.enable")

            title = urlparse(url).path.strip("/") or "root"
            combined_har.add_page(url, title, datetime.now(tz=timezone.utc).isoformat())

            # Merge capture's har into combined
            # Actually, let's just use the capture's own har and merge at the end
            capture.har.add_page(url, title, datetime.now(tz=timezone.utc).isoformat())

            # The page is already navigating (we used /json/new with URL)
            print(f"  Waiting for load...", flush=True)
            time.sleep(8)
            time.sleep(3)  # settle

            # Wait for bodies
            try: capture._body_queue.join()
            except: pass
            time.sleep(2)

            print(f"  Requests: {len(capture._page_entries)} bodies_ok={capture._body_ok} bodies_fail={capture._body_fail}", flush=True)

            # Merge into combined HAR
            with capture.har._lock:
                for rid, req_data in capture.har._requests.items():
                    combined_har._requests[f"{tab_id}_{rid}"] = req_data
                for rid, resp_data in capture.har._responses.items():
                    combined_har._responses[f"{tab_id}_{rid}"] = resp_data
                for rid, headers in capture.har._extra_req_headers.items():
                    combined_har._extra_req_headers[f"{tab_id}_{rid}"] = headers
                for rid, headers in capture.har._extra_resp_headers.items():
                    combined_har._extra_resp_headers[f"{tab_id}_{rid}"] = headers

            # Track API endpoints
            for rid in capture._page_entries:
                resp = capture.har._responses.get(rid, {})
                rurl = ""
                for e in capture.har._requests.values():
                    if e.get("url"):
                        rurl = e["url"]
                mime = resp.get("content", {}).get("mimeType", "")
                if "json" in mime or "firebaseio" in rurl or "/api/" in rurl:
                    status = resp.get("status", 0)
                    all_api_endpoints.add(f"{status} {rurl.split('?')[0]}")

        finally:
            capture.close()
            try: requests.get(f"http://127.0.0.1:{CDP_PORT}/json/close/{tab_id}", timeout=5)
            except: pass

    # Fetch cookies from any remaining TT tab
    try:
        resp = requests.get(f"http://127.0.0.1:{CDP_PORT}/json/list")
        tabs = resp.json()
        for tab in tabs:
            if tab.get("type") == "page" and "turingtrader" in tab.get("url", "").lower():
                ws = websocket.create_connection(tab["webSocketDebuggerUrl"])
                ws.send(json.dumps({"id":1,"method":"Network.getCookies","params":{"urls":["https://www.turingtrader.com/"]}}))
                r = json.loads(ws.recv())
                cookies = r.get("result", {}).get("cookies", [])
                combined_har.record_cookies(cookies)
                print(f"\nCookies: {len(cookies)}", flush=True)
                ws.close()
                break
    except: pass

    # Build HAR
    print("\nBuilding HAR...", flush=True)
    har_doc = combined_har.build_har()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    har_path = os.path.join(OUTPUT_DIR, f"turingtrader_nav_{ts}.har")
    with open(har_path, "w", encoding="utf-8") as f:
        json.dump(har_doc, f, indent=2, ensure_ascii=False)

    entries = har_doc["log"]["entries"]
    has_body = sum(1 for e in entries if e["response"]["content"].get("text"))
    origins = Counter()
    mimes = Counter()
    for e in entries:
        p = urlparse(e["request"]["url"])
        origins[f"{p.scheme}://{p.netloc}"] += 1
        mimes[e["response"]["content"].get("mimeType", "").split(";")[0]] += 1

    print(f"\n{'='*60}", flush=True)
    print(f"CAPTURE COMPLETE", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"  Pages: {len(har_doc['log']['pages'])}", flush=True)
    print(f"  Total entries: {len(entries)}", flush=True)
    print(f"  With body: {has_body}/{len(entries)}", flush=True)
    print(f"  Cookies: {len(har_doc['log'].get('_cookies', []))}", flush=True)
    print(f"  HAR: {har_path}", flush=True)
    print(f"\n  Origins:", flush=True)
    for o, c in origins.most_common(15): print(f"    {c:4d}  {o}", flush=True)
    print(f"\n  Content types:", flush=True)
    for m, c in mimes.most_common(15): print(f"    {c:4d}  {m}", flush=True)
    if all_api_endpoints:
        print(f"\n  API/JSON endpoints:", flush=True)
        for ep in sorted(all_api_endpoints): print(f"    {ep}", flush=True)
    print(f"{'='*60}", flush=True)


if __name__ == "__main__":
    main()
