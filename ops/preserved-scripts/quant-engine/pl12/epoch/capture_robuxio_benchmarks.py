"""Retain the Robuxio benchmark API in full, driven by its own OpenAPI spec.

The benchmark series are not on robuxio.com. The page loads them at runtime from
a separate host, which is why every robuxio.com/api/benchmarks* path returns 404
and why the markdown rendering shows only summary statistics. That host
publishes an OpenAPI document, so the endpoint set is read from the service
rather than guessed.

Only public read endpoints are captured. The `/api/admin/*` operations take an
admin token, mutate server state (flush cache, trigger pipeline) and are
deliberately skipped.

Every response is written verbatim with its URL, status, byte length and
sha256, and each date-bearing series inside a payload is catalogued with its
columns, span and observation count.
"""
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = 'https://robuxio-benchmark-api-production.up.railway.app'
HEADERS = {'User-Agent': 'quant-engine-robuxio-capture/1',
           'Accept': 'application/json'}
TIMEOUT = 180
PAUSE = 0.3
PERIODS = ('all', '3y', '1y', 'ytd')
WINDOWS = ('30d', '90d', '365d')


def fetch(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        body = response.read()
        return {
            'url': url,
            'status': response.status,
            'content_type': (response.headers.get('Content-Type') or '').split(';')[0],
            'observed_length': len(body),
            'sha256': hashlib.sha256(body).hexdigest(),
        }, body


def series_in(payload, trail=''):
    """Report every list of date-keyed objects anywhere in a payload."""
    found = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.extend(series_in(value, '%s.%s' % (trail, key) if trail else key))
    elif isinstance(payload, list) and payload:
        if isinstance(payload[0], dict) and 'date' in payload[0]:
            dates = [row['date'] for row in payload if isinstance(row, dict)
                     and row.get('date')]
            found.append({
                'path': trail,
                'rows': len(payload),
                'columns': sorted({k for row in payload
                                   if isinstance(row, dict) for k in row}),
                'observed_start': min(dates) if dates else None,
                'observed_end': max(dates) if dates else None,
            })
        else:
            for index, item in enumerate(payload[:1]):
                found.extend(series_in(item, '%s[%d]' % (trail, index)))
    return found


def main(argv):
    out = Path(argv[0]) if argv else Path(
        r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
        r'/dataRobuxio/source-runs/capture-20260911/benchmark-api'
    )
    out.mkdir(parents=True, exist_ok=True)
    records, catalog = [], []

    spec_identity, spec_body = fetch(API + '/openapi.json')
    (out / 'openapi.json').write_bytes(spec_body)
    records.append({**spec_identity, 'relative_path': 'openapi.json'})
    spec = json.loads(spec_body)

    targets = []
    for path, operations in sorted(spec['paths'].items()):
        if 'get' not in operations or path.startswith('/api/admin'):
            continue
        names = {p['name'] for p in operations['get'].get('parameters', [])}
        if 'period' in names and 'window' in names:
            targets.extend(
                (path, {'period': p, 'window': w}) for p in PERIODS for w in WINDOWS
            )
        elif 'period' in names:
            targets.extend((path, {'period': p}) for p in PERIODS)
        else:
            targets.append((path, {}))

    for path, params in targets:
        url = API + path + ('?' + urllib.parse.urlencode(params) if params else '')
        try:
            identity, body = fetch(url)
        except urllib.error.HTTPError as exc:
            print('  MISS %-58s HTTP %s' % (path + str(params), exc.code))
            continue
        slug = path.strip('/').replace('/', '_') or 'root'
        if params:
            slug += '__' + '_'.join('%s-%s' % kv for kv in sorted(params.items()))
        relative = '%s.json' % slug
        (out / relative).write_bytes(body)
        records.append({**identity, 'relative_path': relative,
                        'endpoint': path, 'params': params})
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        found = series_in(payload) if payload is not None else []
        catalog.append({'relative_path': relative, 'endpoint': path,
                        'params': params, 'sha256': identity['sha256'],
                        'observed_length': identity['observed_length'],
                        'series': found})
        print('  %-54s %9d  series=%d' % (
            path + (str(params) if params else ''), len(body), len(found)))
        time.sleep(PAUSE)

    manifest = {
        'schema_version': 1,
        'capture': 'robuxio-benchmark-api',
        'service': API,
        'service_title': spec['info']['title'],
        'service_version': spec['info']['version'],
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'admin_endpoints_skipped': sorted(
            p for p in spec['paths'] if p.startswith('/api/admin')),
        'artifacts': records,
        'datasets': catalog,
    }
    (out / 'benchmark-api-manifest.json').write_bytes(
        json.dumps(manifest, indent=1, sort_keys=True).encode('utf-8'))
    print('\nartifacts', len(records), '| bytes',
          sum(r['observed_length'] for r in records))
    print('written', out / 'benchmark-api-manifest.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
