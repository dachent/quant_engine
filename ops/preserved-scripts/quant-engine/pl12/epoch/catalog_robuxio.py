"""Capture every asset the retained Robuxio pages reference, then catalog it all.

`capture_robuxio.py` retains the published files and every sitemap page. This
completes the capture by fetching each asset those pages reference (charts,
logos, and any file endpoints named in prose rather than in `/api/data`), and
then emits a dataset catalog describing what each retained artifact actually
is.

The catalog is the deliverable. For a daily return series it records the
columns, the observed span, the observation count and whether `balance`
reconciles to `daily_change_percent` -- the check that distinguishes a gross
series from a net one. For a page it records whether the markdown carries
numeric statistics. Nothing is normalized into returns here; that belongs to
the adapter, which re-verifies these bytes.
"""
import csv
import hashlib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

BASE = 'https://robuxio.com'
USER_AGENT = 'quant-engine-robuxio-capture/1'
TIMEOUT = 120
PAUSE = 0.25
ASSET = re.compile(
    r'https://robuxio\.com(/[A-Za-z0-9_./%-]+\.'
    r'(?:png|jpg|jpeg|svg|webp|csv|pdf|json|xlsx))'
)
# A statistics row on the benchmarks page, e.g. "BTC +963.4% +42.3% 0.70 ...".
STAT_ROW = re.compile(r'^\s*([A-Za-z][A-Za-z0-9 &/-]{1,24})\s+([+-][\d.]+%.*)$')


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        body = response.read()
        return {
            'url': url,
            'status': response.status,
            'content_type': (response.headers.get('Content-Type') or '').split(';')[0],
            'last_modified': response.headers.get('Last-Modified'),
            'observed_length': len(body),
            'sha256': hashlib.sha256(body).hexdigest(),
        }, body


def describe_csv(body):
    text = body.decode('utf-8-sig', 'replace')
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return {'kind': 'csv', 'rows': 0}
    columns = list(rows[0])
    dates = [row['date'] for row in rows if row.get('date')]
    described = {
        'kind': 'daily-series' if 'date' in columns else 'csv',
        'columns': columns,
        'rows': len(rows),
        'observed_start': min(dates) if dates else None,
        'observed_end': max(dates) if dates else None,
    }
    if {'balance', 'daily_change_percent'} <= set(columns):
        mismatches = 0
        for previous, current in zip(rows, rows[1:]):
            try:
                before = Decimal(previous['balance'])
                after = Decimal(current['balance'])
                stated = Decimal(current['daily_change_percent'])
            except (InvalidOperation, TypeError):
                continue
            if before == 0:
                continue
            if abs((after / before - 1) * 100 - stated) > Decimal('0.000001'):
                mismatches += 1
        described['balance_reconciles_to_percent'] = mismatches == 0
        described['balance_percent_mismatches'] = mismatches
        described['return_basis'] = 'GROSS_PERCENT_COLUMN' if mismatches == 0 else (
            'PERCENT_COLUMN_IS_GROSS_BALANCE_IS_NET'
        )
    return described


def describe_markdown(body):
    text = body.decode('utf-8', 'replace')
    stats = [
        match.group(0).strip()
        for match in (STAT_ROW.match(line) for line in text.splitlines())
        if match
    ]
    return {
        'kind': 'page',
        'bytes': len(body),
        'title': next(
            (line[2:].strip() for line in text.splitlines() if line.startswith('# ')),
            None,
        ),
        'statistic_rows': stats[:40],
        'statistic_row_count': len(stats),
    }


def main(argv):
    out = Path(argv[0]) if argv else Path(
        r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
        r'/dataRobuxio/source-runs/capture-20260911'
    )
    manifest = json.loads((out / 'capture-manifest.json').read_bytes())
    records = {row['relative_path']: row for row in manifest['artifacts']}

    referenced = set()
    for path in sorted((out / 'pages').rglob('*.md')):
        referenced.update(
            ASSET.findall(path.read_text(encoding='utf-8', errors='replace'))
        )
    print('referenced assets', len(referenced))

    added = 0
    for asset in sorted(referenced):
        relative = 'assets' + asset
        if relative in records:
            continue
        try:
            identity, body = fetch(BASE + asset)
        except urllib.error.HTTPError as exc:
            print('  MISS %-58s HTTP %s' % (asset[:56], exc.code))
            continue
        destination = out / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        records[relative] = {**identity, 'relative_path': relative}
        added += 1
        time.sleep(PAUSE)
    print('assets retained', added)

    catalog = []
    for relative, record in sorted(records.items()):
        body = (out / relative).read_bytes()
        if relative.endswith('.csv'):
            described = describe_csv(body)
        elif relative.endswith('.md'):
            described = describe_markdown(body)
        elif relative.endswith('.pdf'):
            described = {'kind': 'factsheet-pdf', 'bytes': len(body)}
        elif relative.endswith('.json'):
            described = {'kind': 'api-index', 'bytes': len(body)}
        else:
            described = {'kind': 'asset', 'bytes': len(body)}
        catalog.append({
            'relative_path': relative,
            'url': record['url'],
            'sha256': record['sha256'],
            'observed_length': record['observed_length'],
            'content_type': record.get('content_type'),
            'last_modified': record.get('last_modified'),
            **{k: v for k, v in record.items() if k in (
                'slug', 'name', 'portfolio', 'metric', 'format',
                'index_modified_time', 'index_size')},
            **described,
        })

    manifest['artifacts'] = sorted(records.values(), key=lambda r: r['relative_path'])
    manifest['catalogued_at'] = datetime.now(timezone.utc).isoformat()
    (out / 'capture-manifest.json').write_bytes(
        json.dumps(manifest, indent=1, sort_keys=True).encode('utf-8')
    )
    (out / 'dataset-catalog.json').write_bytes(
        json.dumps({
            'schema_version': 1,
            'catalog': 'robuxio-public-datasets',
            'source': BASE,
            'catalogued_at': manifest['catalogued_at'],
            'api_index_last_updated': manifest.get('api_index_last_updated'),
            'artifact_count': len(catalog),
            'datasets': catalog,
        }, indent=1, sort_keys=True).encode('utf-8')
    )

    series = [row for row in catalog if row.get('kind') == 'daily-series']
    print('\n== daily series')
    for row in series:
        print('  %-42s %s .. %s  n=%-6s %s' % (
            row['relative_path'].rsplit('/', 1)[-1][:40],
            row['observed_start'], row['observed_end'], row['rows'],
            row.get('return_basis', '')))
    print('\ncatalog entries', len(catalog))
    print('written', out / 'dataset-catalog.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
