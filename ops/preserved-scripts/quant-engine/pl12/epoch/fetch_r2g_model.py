"""Fetch a Portfolio123 designer model's metadata and performance series.

The designer-model page serves its data from `spr/strategy/model/<id>`, which no
prior capture reached -- the harness only ever called the investment endpoints
for the subscribed copy, whose series necessarily begins when the copy was
created. This reads the model's own series, which is one continuous path:
simulated before the model's launch date, live after it.

Fetches run inside the authenticated page context so the session cookie
applies; nothing is written to the browser profile. Raw responses are saved
verbatim for hashing, with no reshaping here.
"""
import json
import sys
import time
from pathlib import Path

import requests
import websocket

PORT = 9224
ORIGIN = 'https://www.portfolio123.com'


def connect():
    tabs = requests.get('http://localhost:%d/json/list' % PORT, timeout=10).json()
    page = next(
        (t for t in tabs if t.get('type') == 'page' and 'portfolio123.com' in (t.get('url') or '')),
        None,
    )
    if page is None:
        raise SystemExit('no portfolio123 tab open in the capture browser')
    return websocket.create_connection(
        page['webSocketDebuggerUrl'], timeout=120, suppress_origin=True
    )


def evaluate(ws, counter, expression):
    counter['n'] += 1
    ws.send(json.dumps({
        'id': counter['n'],
        'method': 'Runtime.evaluate',
        'params': {'expression': expression, 'awaitPromise': True, 'returnByValue': True},
    }))
    deadline = time.time() + 120
    while time.time() < deadline:
        ws.settimeout(max(1, deadline - time.time()))
        message = json.loads(ws.recv())
        if message.get('id') == counter['n']:
            result = message.get('result', {}).get('result', {})
            if 'value' not in result:
                raise SystemExit('fetch failed: %s' % json.dumps(message)[:400])
            return result['value']
    raise SystemExit('fetch timed out')


def fetch(ws, counter, path):
    expression = (
        "fetch(%s, {credentials:'include'})"
        ".then(r => r.text().then(t => JSON.stringify({status:r.status, body:t})))"
        % json.dumps(ORIGIN + path)
    )
    return json.loads(evaluate(ws, counter, expression))


def main():
    model_id = sys.argv[1] if len(sys.argv) > 1 else '1764562'
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(
        r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
        r'/dataPortfolio123/source-runs/capture-20260912-mvp2/raw'
    )
    out.mkdir(parents=True, exist_ok=True)
    ws = connect()
    counter = {'n': 0}
    targets = [
        ('model', '/spr/strategy/model/%s?dm=true' % model_id),
        # `years` is the page's own range control; ask for more than the page
        # requests so the series is not truncated by the default view.
        ('perf-15y', '/spr/strategy/model/%s/perf?dm=true&init=true&years=15' % model_id),
        ('perf-50y', '/spr/strategy/model/%s/perf?dm=true&init=true&years=50' % model_id),
    ]
    for label, path in targets:
        response = fetch(ws, counter, path)
        body = response['body']
        destination = out / ('model_%s_%s.json' % (model_id, label))
        destination.write_text(body, encoding='utf-8')
        print('%-9s status=%s bytes=%-8d -> %s' % (label, response['status'], len(body), destination.name))
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                keys = sorted(parsed)
                print('           keys: %s' % keys[:14])
                for field in ('launchDt', 'inceptionDt', 'date', 'dateLast', 'name'):
                    if field in parsed:
                        print('           %-11s %s' % (field, parsed[field]))
                data = parsed.get('data')
                if isinstance(data, list):
                    print('           data rows: %d  first=%s' % (len(data), data[0] if data else None))
        except json.JSONDecodeError:
            print('           (not JSON)')
    ws.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
