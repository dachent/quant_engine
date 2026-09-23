"""Probe the investment performance endpoint shape."""
import json, time, sys
import requests, websocket
PORT, ORIGIN = 9224, 'https://www.portfolio123.com'
tabs = requests.get('http://localhost:%d/json/list' % PORT, timeout=10).json()
page = next(t for t in tabs if t.get('type') == 'page' and 'portfolio123.com' in (t.get('url') or ''))
ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=120, suppress_origin=True)
n = {'i': 0}
def fetch(path):
    e = ("fetch(%s,{credentials:'include',headers:{'ANGULAR_REQ':'1','Accept':'application/json, text/plain, */*'}})"
         ".then(r=>r.text().then(t=>JSON.stringify({status:r.status,body:t.slice(0,400),len:t.length})))" % json.dumps(ORIGIN+path))
    n['i'] += 1
    ws.send(json.dumps({'id': n['i'], 'method': 'Runtime.evaluate',
                        'params': {'expression': e, 'awaitPromise': True, 'returnByValue': True}}))
    d = time.time() + 90
    while time.time() < d:
        ws.settimeout(max(1, d - time.time()))
        m = json.loads(ws.recv())
        if m.get('id') == n['i']:
            v = m.get('result', {}).get('result', {}).get('value')
            return json.loads(v) if v else {'status': 'ERR', 'body': '', 'len': 0}
    return {'status': 'TIMEOUT', 'body': '', 'len': 0}
uid = sys.argv[1] if len(sys.argv) > 1 else '1897754'
for path in ('/app/investment/perfdata/%s' % uid,
             '/app/investment/perfdata?id=%s&dm=false' % uid,
             '/app/invest/perfdata/%s' % uid,
             '/app/investment/%s/perfdata' % uid,
             '/spr/strategy/model/%s/perf' % uid,
             '/app/investment/perf?id=%s' % uid):
    r = fetch(path)
    print('%-46s status=%-5s len=%-7s %s' % (path, r['status'], r['len'], r['body'][:90].replace('\n', ' ')))
ws.close()
