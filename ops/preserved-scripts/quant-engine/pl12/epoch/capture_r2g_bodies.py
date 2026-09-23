"""Capture the designer-model responses the page itself fetches.

Re-issuing the request by hand returns 400: the endpoint expects the headers
the app sends, and guessing them is how a capture ends up recording something
subtly different from what the page saw. So the page is reloaded and its own
responses are read out of the network layer verbatim, which is also what the
existing HAR harness does.

Saves each matching response body plus the request headers that produced it, so
a later re-fetch can reproduce the call exactly.
"""
import json
import sys
import time
from pathlib import Path

import requests
import websocket

PORT = 9224
MATCH = '/spr/strategy/model/'


def main():
    model_id = sys.argv[1] if len(sys.argv) > 1 else '1764562'
    out = Path(
        r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
        r'/dataPortfolio123/source-runs/capture-20260912-mvp2/raw'
    )
    out.mkdir(parents=True, exist_ok=True)
    target = 'https://www.portfolio123.com/app/r2g/summary?id=%s' % model_id

    tabs = requests.get('http://localhost:%d/json/list' % PORT, timeout=10).json()
    page = next(t for t in tabs if t.get('type') == 'page' and 'portfolio123.com' in (t.get('url') or ''))
    ws = websocket.create_connection(
        page['webSocketDebuggerUrl'], timeout=120, suppress_origin=True
    )
    counter = {'n': 0}

    def send(method, params=None):
        counter['n'] += 1
        ws.send(json.dumps({'id': counter['n'], 'method': method, 'params': params or {}}))
        return counter['n']

    send('Network.enable')
    send('Page.enable')
    send('Page.navigate', {'url': target})

    wanted = {}
    pending = {}
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            ws.settimeout(max(1, deadline - time.time()))
            message = json.loads(ws.recv())
        except Exception:
            break
        method = message.get('method')
        if method == 'Network.requestWillBeSent':
            params = message['params']
            url = params['request']['url']
            if MATCH in url:
                wanted[params['requestId']] = {
                    'url': url,
                    'headers': params['request'].get('headers', {}),
                }
        elif method == 'Network.loadingFinished' and message['params']['requestId'] in wanted:
            pending[message['params']['requestId']] = True

    saved = 0
    for request_id, info in wanted.items():
        body_id = send('Network.getResponseBody', {'requestId': request_id})
        got = None
        stop = time.time() + 20
        while time.time() < stop:
            try:
                ws.settimeout(max(1, stop - time.time()))
                message = json.loads(ws.recv())
            except Exception:
                break
            if message.get('id') == body_id:
                got = message.get('result')
                break
        if not got or 'body' not in got:
            print('no body for %s' % info['url'][:90])
            continue
        label = ''.join(c if c.isalnum() else '_' for c in info['url'].split('/model/')[-1])
        destination = out / ('r2g_%s.json' % label[:70])
        destination.write_text(got['body'], encoding='utf-8')
        (out / (destination.stem + '.request.json')).write_text(
            json.dumps(info, indent=1, sort_keys=True), encoding='utf-8'
        )
        saved += 1
        print('saved %-58s %8d bytes' % (destination.name, len(got['body'])))
        try:
            parsed = json.loads(got['body'])
            if isinstance(parsed, dict):
                print('      keys: %s' % sorted(parsed)[:16])
                for field in ('launchDt', 'inceptionDt', 'date', 'dateLast', 'name', 'startDt'):
                    if field in parsed:
                        print('      %-12s %s' % (field, parsed[field]))
                for field in ('data', 'perf', 'chart', 'series'):
                    value = parsed.get(field)
                    if isinstance(value, list):
                        print('      %-12s %d rows, first=%s' % (field, len(value), str(value[0])[:90]))
        except json.JSONDecodeError:
            pass
    ws.close()
    print('responses captured:', saved)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
