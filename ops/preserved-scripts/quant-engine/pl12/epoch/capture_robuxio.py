"""Retain everything robuxio.com publishes, as raw bytes with identity.

The repository's provider tools perform no network operation: they read a
retained capture and stage a candidate generation from it. This is the capture
half. robuxio.com serves its public content without authentication and ships
its own machine map at /llms.txt, which states: fetch /llms.txt to discover
links, then the .md URL; a 404 means unavailable, consult the sitemap rather
than guessing further paths. This follows that instruction rather than probing.

Three classes are retained:

* the `/api/data` index and every `downloadUrl` it names (the portfolio NAV and
  GAV series and their factsheets);
* the machine map, sitemap and robots;
* the markdown rendering of every page in the sitemap, which is where the
  benchmarks, monthly reports and update log actually live.

Nothing is parsed or normalized here. The adapter re-verifies these bytes.
"""
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = 'https://robuxio.com'
USER_AGENT = 'quant-engine-robuxio-capture/1'
TIMEOUT = 120
PAUSE = 0.3


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        body = response.read()
        return {
            'url': url,
            'status': response.status,
            'declared_length': response.headers.get('Content-Length'),
            'content_type': (response.headers.get('Content-Type') or '').split(';')[0],
            'last_modified': response.headers.get('Last-Modified'),
            'observed_length': len(body),
            'sha256': hashlib.sha256(body).hexdigest(),
        }, body


def retain(out, relative, url, records, optional=False):
    try:
        identity, body = fetch(url)
    except urllib.error.HTTPError as exc:
        if optional:
            return None
        raise SystemExit('%s -> HTTP %s' % (url, exc.code))
    path = out / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    records.append({**identity, 'relative_path': relative})
    time.sleep(PAUSE)
    return body


def main(argv):
    out = Path(argv[0]) if argv else Path(
        r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
        r'/dataRobuxio/source-runs/capture-20260911'
    )
    out.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).isoformat()
    records = []

    for relative, path in (
        ('llms.txt', '/llms.txt'),
        ('robots.txt', '/robots.txt'),
        ('sitemap.xml', '/sitemap.xml'),
    ):
        retain(out, relative, BASE + path, records)

    index_body = retain(out, 'api-data-index.json', BASE + '/api/data', records)
    index = json.loads(index_body)
    print('api index lastUpdated', index.get('lastUpdated'),
          '| files', len(index.get('files', [])))

    for entry in index.get('files', []):
        url = entry['downloadUrl']
        if not url.startswith('http'):
            url = BASE + url
        slug = entry.get('slug') or url.rsplit('/', 1)[-1]
        suffix = '' if slug.endswith(('.csv', '.pdf')) else {
            'csv': '.csv', 'pdf': '.pdf'
        }.get(str(entry.get('format', '')).lower(), '')
        body = retain(out, 'files/%s%s' % (slug, suffix), url, records)
        records[-1].update(
            slug=entry.get('slug'), name=entry.get('name'),
            portfolio=entry.get('portfolio'), metric=entry.get('metric'),
            format=entry.get('format'), index_modified_time=entry.get('modifiedTime'),
            index_size=entry.get('size'),
        )
        head = body[:120].decode('utf-8', 'replace').splitlines()[:1]
        print('  %-44s %9d  %s' % (slug, len(body), head[0] if head else ''))

    sitemap = (out / 'sitemap.xml').read_text(encoding='utf-8', errors='replace')
    pages = sorted(set(re.findall(r'<loc>([^<]+)</loc>', sitemap)))
    print('sitemap pages', len(pages))
    kept = 0
    for url in pages:
        slug = url[len(BASE):].strip('/') or 'index'
        body = retain(
            out, 'pages/%s.md' % slug, '%s/%s.md' % (BASE, slug), records,
            optional=True,
        )
        if body is not None:
            kept += 1
    print('markdown pages retained', kept, 'of', len(pages))

    manifest = {
        'schema_version': 1,
        'capture': 'robuxio-public-site',
        'source': BASE,
        'fetched_at': fetched_at,
        'api_index_last_updated': index.get('lastUpdated'),
        'artifacts': sorted(records, key=lambda row: row['relative_path']),
    }
    (out / 'capture-manifest.json').write_bytes(
        json.dumps(manifest, indent=1, sort_keys=True).encode('utf-8')
    )
    print('artifacts', len(records))
    print('written', out / 'capture-manifest.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
