"""Map every Robuxio endpoint that returns data, by probing, not by guessing.

`/api/data` indexes only the 15 portfolio factsheet files. The benchmarks
(BTC, Top50, All Futures), the monthly reports and the updates log are served
elsewhere. This tries a candidate set and reports what each actually returns:
status, content type, byte length and the first line, so the real data
endpoints can be read off evidence.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = 'https://robuxio.com'
USER_AGENT = 'quant-engine-robuxio-capture/1'
TIMEOUT = 60

CANDIDATES = [
    '/api/data',
    '/api/benchmarks',
    '/api/benchmark',
    '/api/benchmarks/data',
    '/api/benchmarks/files',
    '/api/data/benchmarks',
    '/api/indices',
    '/api/index',
    '/api/market-data',
    '/api/updates',
    '/api/reports',
    '/api/crypto/reports',
    '/api/crypto/reports/2025',
    '/api/crypto/reports/2026',
    '/api/equities/reports',
    '/benchmarks.md',
    '/data.md',
    '/updates.md',
    '/crypto/reports.md',
    '/crypto/reports/2025.md',
    '/crypto/reports/2026.md',
    '/equities/reports.md',
    '/sitemap.xml',
    '/sitemap-0.xml',
    '/robots.txt',
    '/llms.txt',
]


def probe(path):
    url = path if path.startswith('http') else BASE + path
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
            head = body[:200].decode('utf-8', 'replace').splitlines()
            return {
                'path': path,
                'status': response.status,
                'type': (response.headers.get('Content-Type') or '').split(';')[0],
                'bytes': len(body),
                'head': head[0][:110] if head else '',
                'body': body,
            }
    except urllib.error.HTTPError as exc:
        return {'path': path, 'status': exc.code, 'type': '-', 'bytes': 0,
                'head': '', 'body': b''}
    except Exception as exc:  # noqa: BLE001 - report and continue
        return {'path': path, 'status': 'ERR', 'type': type(exc).__name__,
                'bytes': 0, 'head': '', 'body': b''}


def main(argv):
    found = []
    for path in CANDIDATES:
        row = probe(path)
        marker = 'OK ' if row['status'] == 200 else '   '
        print('%s%-30s %-4s %-26s %8s  %s' % (
            marker, row['path'], row['status'], row['type'], row['bytes'],
            row['head']))
        if row['status'] == 200:
            found.append(row)

    # Any JSON payload may itself name more endpoints.
    for row in found:
        if 'json' not in row['type']:
            continue
        try:
            payload = json.loads(row['body'])
        except ValueError:
            continue
        text = json.dumps(payload)
        print('\n-- %s keys: %s' % (
            row['path'],
            sorted(payload)[:20] if isinstance(payload, dict) else '(array)'))
        for token in sorted({
            part for part in text.replace('"', ' ').replace(',', ' ').split()
            if part.startswith('/api/') or part.startswith('/data')
        }):
            print('   endpoint', token)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
