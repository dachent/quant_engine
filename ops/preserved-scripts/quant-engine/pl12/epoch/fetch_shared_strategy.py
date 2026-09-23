"""Download a shared strategy's bulk exports through the authenticated session.

These are the endpoints the existing capture plan already uses for shared
strategies -- plain download servlets keyed by portid, not the Angular
`spr/strategy/model` family the designer model needs. They return CSV, so the
bytes are saved verbatim with no parsing here.
"""
import json
import sys
import time
from pathlib import Path

import requests
import websocket

PORT = 9224
ORIGIN = 'https://www.portfolio123.com'
OUT = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataPortfolio123/source-runs/capture-20260912-mvp2/raw'
)
TARGETS = (
    ('perf', '/p123/DownloadPortPerf?portid=%s'),
    ('transactions', '/p123/DownloadPortTrans?portid=%s'),
    ('holdings', '/p123/DownloadPortHoldings?portid=%s'),
)


def connect():
    tabs = requests.get('http://localhost:%d/json/list' % PORT, timeout=10).json()
    page = next(t for t in tabs if t.get('type') == 'page' and 'portfolio123.com' in (t.get('url') or ''))
    return websocket.create_connection(
        page['webSocketDebuggerUrl'], timeout=300, suppress_origin=True
    )


def fetch(ws, counter, path):
    expression = (
        "fetch(%s, {credentials:'include'})"
        ".then(r => r.text().then(t => JSON.stringify({status:r.status, body:t})))"
        % json.dumps(ORIGIN + path)
    )
    counter['n'] += 1
    ws.send(json.dumps({
        'id': counter['n'],
        'method': 'Runtime.evaluate',
        'params': {'expression': expression, 'awaitPromise': True, 'returnByValue': True},
    }))
    deadline = time.time() + 300
    while time.time() < deadline:
        ws.settimeout(max(1, deadline - time.time()))
        message = json.loads(ws.recv())
        if message.get('id') == counter['n']:
            value = message.get('result', {}).get('result', {}).get('value')
            if value is None:
                return {'status': 'ERROR', 'body': json.dumps(message)[:300]}
            return json.loads(value)
    return {'status': 'TIMEOUT', 'body': ''}


def main():
    portid = sys.argv[1] if len(sys.argv) > 1 else '1853549'
    OUT.mkdir(parents=True, exist_ok=True)
    ws = connect()
    counter = {'n': 0}
    for label, template in TARGETS:
        response = fetch(ws, counter, template % portid)
        status, body = response.get('status'), response.get('body') or ''
        if status != 200 or not body.strip():
            print('%-13s status=%-8s (empty or unavailable)' % (label, status))
            continue
        destination = OUT / ('strategy_%s_%s.csv' % (portid, label))
        destination.write_text(body, encoding='utf-8', newline='')
        lines = body.splitlines()
        first = lines[1].split(',')[0] if len(lines) > 1 else ''
        last = lines[-1].split(',')[0] if len(lines) > 1 else ''
        print('%-13s status=%-4s rows=%-6d %s .. %s -> %s'
              % (label, status, max(0, len(lines) - 1), first, last, destination.name))
        if label == 'perf':
            print('              header: %s' % lines[0][:120])
    ws.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
