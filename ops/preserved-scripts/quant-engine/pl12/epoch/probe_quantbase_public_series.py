"""Find where the public Volatis series comes from and at what resolution.

The extracted diagnostic file carries one observation every five days. That is
either what the provider publishes or what the capture happened to keep, and
those lead to different conclusions -- the first blocks a daily decomposition,
the second is fixed by asking the provider for more. The captured responses
answer it.
"""
import json
from pathlib import Path

RESPONSES = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/dataQuantbase/source-runs/refresh-20260902/har_output/responses'
)


def walk(node, path=''):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, path + '.' + key)
    elif isinstance(node, list):
        yield path, node


def main():
    for response in sorted(RESPONSES.iterdir()):
        if not response.is_file():
            continue
        try:
            payload = json.loads(response.read_text(encoding='utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        for path, rows in walk(payload):
            if not rows or not isinstance(rows[0], dict):
                continue
            keys = set(rows[0])
            if not ({'value', 'time'} <= keys):
                continue
            titles = {str(row.get('fund_title') or '') for row in rows}
            if titles and not any('Volatis' in title for title in titles):
                continue
            volatis = [
                row for row in rows
                if 'Volatis' in str(row.get('fund_title') or '') or len(titles) <= 1
            ]
            if len(volatis) < 2:
                continue
            times = sorted(str(row['time']) for row in volatis)
            print('%s  %s' % (response.name, path))
            print('   rows=%d  keys=%s' % (len(volatis), sorted(keys)))
            print('   %s .. %s' % (times[0], times[-1]))
            print('   first three: %s' % times[:3])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
