"""Refresh the account-side Portfolio123 surfaces while the session is live.

The retained capture's investment endpoints are from 2026-09-02 and stop at the
provider's then-current date. Everything reachable for the owner's own
investments is pulled again here -- the resource listing, each account's
details and trade activity, and each investment's performance series -- so the
account side is as current as the model side.

`ANGULAR_REQ: 1` is the header the app tags its own XHRs with; without it these
endpoints answer 400. Responses are saved verbatim.
"""
import json
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
ACCOUNTS = ('10402', '10560')


def connect():
    tabs = requests.get('http://localhost:%d/json/list' % PORT, timeout=10).json()
    page = next(t for t in tabs if t.get('type') == 'page' and 'portfolio123.com' in (t.get('url') or ''))
    return websocket.create_connection(
        page['webSocketDebuggerUrl'], timeout=300, suppress_origin=True
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
    deadline = time.time() + 300
    while time.time() < deadline:
        ws.settimeout(max(1, deadline - time.time()))
        message = json.loads(ws.recv())
        if message.get('id') == counter['n']:
            value = message.get('result', {}).get('result', {}).get('value')
            if value is None:
                return {'status': 'ERROR', 'body': ''}
            return json.loads(value)
    return {'status': 'TIMEOUT', 'body': ''}


def save(label, response):
    status, body = response.get('status'), response.get('body') or ''
    if status != 200 or not body.strip():
        print('%-46s status=%s (unavailable)' % (label, status))
        return None
    destination = OUT / ('p123_%s.json' % label)
    destination.write_text(body, encoding='utf-8')
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        print('%-46s %d bytes (non-JSON)' % (label, len(body)))
        return None
    note = ''
    if isinstance(parsed, dict):
        for field in ('date', 'dateLast'):
            if field in parsed:
                note += ' %s=%s' % (field, parsed[field])
        if isinstance(parsed.get('data'), list):
            note += ' rows=%d' % len(parsed['data'])
    elif isinstance(parsed, list):
        note = ' list rows=%d' % len(parsed)
    print('%-46s %7d bytes%s' % (label, len(body), note))
    return parsed


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    ws = connect()
    counter = {'n': 0}
    stamp = str(int(time.time() * 1000))

    resource = save('investment_resource', fetch(ws, counter, '/app/investment/resource?f=1&s=' + stamp))
    investments = []
    if isinstance(resource, list):
        rows = resource
    elif isinstance(resource, dict):
        rows = resource.get('investments') or resource.get('data') or []
    else:
        rows = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            uid = str(row.get('portfolioUid') or '')
            if uid:
                investments.append((uid, str(row.get('assetName') or row.get('name') or '')[:46]))

    for account in ACCOUNTS:
        save('account_%s_details' % account, fetch(ws, counter, '/app/investment/details?id=' + account))
    save('tradeActivity', fetch(ws, counter, '/app/tradeActivity?s=' + stamp))

    print('\ninvestments discovered: %d' % len(investments))
    for uid, name in investments:
        print('  %-9s %s' % (uid, name))
        save('investment_%s_perfdata' % uid, fetch(ws, counter, '/app/investment/perfdata?id=' + uid))
        save('investment_%s_holdings' % uid, fetch(ws, counter, '/app/invest/holdings?id=' + uid))
    ws.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
