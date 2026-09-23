"""Capture an investment's performance response by driving its tab.

The performance series is requested when the Performance tab is selected, not
on page load, and the request shape is not guessable -- six plausible spellings
all answered 404 or 500. So the page is loaded, the tab is clicked, and
whatever the app requests is recorded from the network layer.
"""
import json
import sys
import time
from pathlib import Path

import requests
import websocket

PORT = 9224
OUT = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio123/source-runs/capture-20260912-mvp2/raw'
)


def main():
    uid = sys.argv[1] if len(sys.argv) > 1 else '1897754'
    OUT.mkdir(parents=True, exist_ok=True)
    tabs = requests.get('http://localhost:%d/json/list' % PORT, timeout=10).json()
    page = next(t for t in tabs if t.get('type') == 'page' and 'portfolio123.com' in (t.get('url') or ''))
    ws = websocket.create_connection(
        page['webSocketDebuggerUrl'], timeout=180, suppress_origin=True
    )
    counter = {'n': 0}

    def send(method, params=None):
        counter['n'] += 1
        ws.send(json.dumps({'id': counter['n'], 'method': method, 'params': params or {}}))
        return counter['n']

    def drain(seconds, collect):
        stop = time.time() + seconds
        while time.time() < stop:
            try:
                ws.settimeout(max(0.5, stop - time.time()))
                collect(json.loads(ws.recv()))
            except Exception:
                return

    send('Network.enable')
    send('Page.enable')
    send('Page.navigate', {'url': 'https://www.portfolio123.com/app/investment?id=%s' % uid})

    wanted = {}

    def watch(message):
        if message.get('method') == 'Network.requestWillBeSent':
            url = message['params']['request']['url']
            if 'portfolio123.com' in url and any(
                k in url.lower() for k in ('perf', 'chart', 'history')
            ):
                wanted[message['params']['requestId']] = url

    drain(25, watch)

    # Click whichever control the app uses for the performance view.
    click = (
        "(() => {const t=[...document.querySelectorAll('a,button,li,span')]"
        ".find(e => /performance/i.test((e.textContent||'').trim()) &&"
        " (e.offsetParent !== null));"
        " if (t) { t.click(); return t.textContent.trim().slice(0,40); } return 'NOT_FOUND';})()"
    )
    click_id = send('Runtime.evaluate', {'expression': click, 'returnByValue': True})
    clicked = None

    def watch_click(message):
        nonlocal clicked
        watch(message)
        if message.get('id') == click_id:
            clicked = message.get('result', {}).get('result', {}).get('value')

    drain(30, watch_click)
    print('performance control clicked:', clicked)

    saved = 0
    for request_id, url in wanted.items():
        body_id = send('Network.getResponseBody', {'requestId': request_id})
        got = {}

        def watch_body(message):
            nonlocal got
            if message.get('id') == body_id:
                got = message.get('result') or {}

        drain(15, watch_body)
        body = got.get('body')
        if not body:
            continue
        label = ''.join(c if c.isalnum() else '_' for c in url.split('portfolio123.com/')[-1])[:70]
        destination = OUT / ('p123_%s.json' % label)
        destination.write_text(body, encoding='utf-8')
        saved += 1
        print('saved %-62s %7d bytes' % (destination.name, len(body)))
        print('      url: %s' % url[:120])
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                for field in ('date', 'dateLast'):
                    if field in parsed:
                        print('      %-9s %s' % (field, parsed[field]))
                if isinstance(parsed.get('data'), list):
                    print('      data rows: %d' % len(parsed['data']))
        except json.JSONDecodeError:
            pass
    ws.close()
    print('captured:', saved)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
