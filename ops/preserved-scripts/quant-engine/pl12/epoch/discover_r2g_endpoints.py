"""List the network calls a Portfolio123 designer-model page makes.

The designer-model page is a single-page app: its data arrives over XHR, so the
URL alone does not say where the performance series comes from. This attaches
to the already-open tab over CDP, reloads it, and reports every request the
page issued, so the real endpoints are read from the browser rather than
guessed.

Read-only: it navigates and observes, and writes nothing.
"""
import json
import sys
import time

import requests
import websocket

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9224
TARGET = sys.argv[2] if len(sys.argv) > 2 else 'https://www.portfolio123.com/app/r2g/summary?id=1764562'


def main():
    tabs = requests.get('http://localhost:%d/json/list' % PORT, timeout=10).json()
    page = next(
        (t for t in tabs if t.get('type') == 'page' and 'portfolio123.com' in (t.get('url') or '')),
        None,
    )
    if page is None:
        page = next(t for t in tabs if t.get('type') == 'page')
    ws = websocket.create_connection(
        page['webSocketDebuggerUrl'], timeout=60, suppress_origin=True
    )
    counter = {'n': 0}

    def send(method, params=None):
        counter['n'] += 1
        ws.send(json.dumps({'id': counter['n'], 'method': method, 'params': params or {}}))
        return counter['n']

    send('Network.enable')
    send('Page.enable')
    send('Page.navigate', {'url': TARGET})

    seen = []
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            ws.settimeout(max(1, deadline - time.time()))
            message = json.loads(ws.recv())
        except Exception:
            break
        if message.get('method') == 'Network.requestWillBeSent':
            request = message['params']['request']
            seen.append((request['method'], request['url']))
    ws.close()

    interesting = [
        (m, u) for m, u in seen
        if 'portfolio123.com' in u
        and not any(u.endswith(x) for x in ('.js', '.css', '.png', '.woff', '.woff2', '.svg', '.ico'))
    ]
    print('total requests observed: %d | portfolio123 non-asset: %d' % (len(seen), len(interesting)))
    for method, url in dict.fromkeys(interesting):
        print('  %-5s %s' % (method, url))


if __name__ == '__main__':
    raise SystemExit(main())
