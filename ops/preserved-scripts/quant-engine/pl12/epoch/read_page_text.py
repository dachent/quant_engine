"""Print the visible text of a page already open in the capture browser.

Used to read what the provider states about a strategy -- period covered, last
rebalance, update date -- rather than inferring it from whatever an export
happened to return.
"""
import json
import sys
import time

import requests
import websocket

PORT = 9224


def main():
    match = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    tabs = requests.get('http://localhost:%d/json/list' % PORT, timeout=10).json()
    page = next(t for t in tabs if t.get('type') == 'page' and match in (t.get('url') or ''))
    ws = websocket.create_connection(
        page['webSocketDebuggerUrl'], timeout=60, suppress_origin=True
    )
    ws.send(json.dumps({
        'id': 1,
        'method': 'Runtime.evaluate',
        'params': {
            'expression': "document.body.innerText.replace(/\\n{2,}/g,'\\n')",
            'returnByValue': True,
        },
    }))
    deadline = time.time() + 60
    while time.time() < deadline:
        ws.settimeout(max(1, deadline - time.time()))
        message = json.loads(ws.recv())
        if message.get('id') == 1:
            print((message['result']['result'].get('value') or '')[:limit])
            break
    ws.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
