"""Fetch the designer model's full series and probe for a positions endpoint.

The app marks its own XHRs with `ANGULAR_REQ: 1`; without it the endpoint
answers 400, which is why a hand-rolled fetch failed. Replaying the header the
page actually sent lets the series be requested over a longer window than the
page's default view, and lets sibling endpoints be probed without navigating.

Requests run in the page context so the session cookie applies. Probing is
read-only: every candidate is a GET, and anything that is not JSON is recorded
as unavailable rather than retried.
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


def connect():
    tabs = requests.get('http://localhost:%d/json/list' % PORT, timeout=10).json()
    page = next(t for t in tabs if t.get('type') == 'page' and 'portfolio123.com' in (t.get('url') or ''))
    return websocket.create_connection(
        page['webSocketDebuggerUrl'], timeout=180, suppress_origin=True
    )


def fetch(ws, counter, path):
    expression = (
        "fetch(%s, {credentials:'include', headers:{'ANGULAR_REQ':'1',"
        "'Accept':'application/json, text/plain, */*'}})"
        ".then(r => r.text().then(t => JSON.stringify({status:r.status, body:t})))"
        % json.dumps(ORIGIN + path)
    )
    counter['n'] += 1
    ws.send(json.dumps({
        'id': counter['n'],
        'method': 'Runtime.evaluate',
        'params': {'expression': expression, 'awaitPromise': True, 'returnByValue': True},
    }))
    deadline = time.time() + 180
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
    model_id = sys.argv[1] if len(sys.argv) > 1 else '1764562'
    OUT.mkdir(parents=True, exist_ok=True)
    ws = connect()
    counter = {'n': 0}

    targets = [
        ('perf-full', '/spr/strategy/model/%s/perf?dm=true&init=true&years=25' % model_id),
        ('holdings', '/spr/strategy/model/%s/holdings?dm=true' % model_id),
        ('positions', '/spr/strategy/model/%s/positions?dm=true' % model_id),
        ('trans', '/spr/strategy/model/%s/transactions?dm=true' % model_id),
        ('trades', '/spr/strategy/model/%s/trades?dm=true' % model_id),
        ('stats', '/spr/strategy/model/%s/stats?dm=true' % model_id),
    ]
    for label, path in targets:
        response = fetch(ws, counter, path)
        status, body = response.get('status'), response.get('body') or ''
        note = ''
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if status == 200 and parsed is not None:
            destination = OUT / ('r2g_%s_%s.json' % (model_id, label))
            destination.write_text(body, encoding='utf-8')
            if isinstance(parsed, dict):
                note = 'keys=%s' % (sorted(parsed)[:10])
                perf = parsed.get('perfData')
                if isinstance(perf, dict):
                    note += ' | %s..%s rows=%d' % (
                        perf.get('date'), perf.get('dateLast'),
                        len(perf.get('data') or []),
                    )
            elif isinstance(parsed, list):
                note = 'list rows=%d first=%s' % (len(parsed), json.dumps(parsed[0])[:110] if parsed else None)
            note += ' -> %s' % destination.name
        print('%-10s status=%-7s %s' % (label, status, note or '(unavailable)'))
    ws.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
