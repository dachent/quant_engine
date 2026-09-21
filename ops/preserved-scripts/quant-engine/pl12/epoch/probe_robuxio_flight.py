"""Decode the Next.js RSC flight payload on a Robuxio page and find series data.

The `.md` rendering strips embedded data. The HTML streams React Server
Component payloads through `self.__next_f.push([1, "<json-string>"])`; chart
series are carried there rather than in a separate endpoint. This reassembles
those string chunks and reports what numeric series they contain.
"""
import json
import re
import sys
import urllib.request

BASE = 'https://robuxio.com'
HEADERS = {'User-Agent': 'Mozilla/5.0 quant-engine-robuxio-capture/1',
           'Accept': 'text/html'}
PUSH = re.compile(r'self\.__next_f\.push\(\[1,')
DATE = re.compile(r'20[12][0-9]-[01][0-9]-[0-3][0-9]')


def get(path):
    request = urllib.request.Request(BASE + path, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read().decode('utf-8', 'replace')


def flight(html):
    """Reassemble the pushed JSON string chunks, honouring JS string escapes."""
    blob = []
    for match in PUSH.finditer(html):
        index = html.find('"', match.end())
        if index < 0:
            continue
        decoder = json.JSONDecoder()
        try:
            value, _end = decoder.raw_decode(html[index:])
        except ValueError:
            continue
        if isinstance(value, str):
            blob.append(value)
    return ''.join(blob)


def main(argv):
    path = argv[0].strip() if argv else '/benchmarks'
    html = get(path)
    blob = flight(html)
    print('page', path, '| html', len(html), '| flight', len(blob))
    dates = DATE.findall(blob)
    print('date tokens', len(dates), (min(dates), max(dates)) if dates else '')

    for key in ('btc', 'top50', 'allFutures', 'spy', 'nav', 'gav', 'benchmark',
                'chart', 'data', 'values', 'points', 'series'):
        print('  %-12s %d' % (key, blob.lower().count(key.lower())))

    # Any JSON array of {date, value}-shaped objects, or [date, number] pairs.
    objects = re.findall(
        r'\{"date":"20[12][0-9]-[01][0-9]-[0-3][0-9]"[^}]{0,200}\}', blob)
    pairs = re.findall(
        r'\["?20[12][0-9]-[01][0-9]-[0-3][0-9]"?\s*,\s*-?\d', blob)
    print('object-shaped rows', len(objects), '| pair-shaped rows', len(pairs))
    for sample in objects[:5]:
        print('   ', sample[:160])
    for sample in pairs[:5]:
        print('   ', sample[:160])

    # Long runs of bare numbers are how compact chart series are shipped.
    runs = sorted(
        (len(run), run[:90])
        for run in re.findall(r'\[(?:\s*-?\d+(?:\.\d+)?\s*,){40,}\s*-?\d+(?:\.\d+)?\s*\]', blob)
    )
    print('numeric runs >40', len(runs))
    for size, sample in runs[-3:]:
        print('    len=%d %s...' % (size, sample))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
