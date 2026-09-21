"""Show whether the two dependence modes now differ, and by how much.

Before the change both modes were computed from the same live-only rows, so
every observed correlation cell was identical and the hybrid mode was hybrid in
name only. The cells are keyed by the pair's two series names and carry their
own observation count, so a real hybrid shows both a higher count and a
different correlation wherever a pair has prehistory on both sides.
"""
import json
from pathlib import Path

CORRELATION = Path(
    r'C:/Dev/scratch/quant-engine/pl12/epoch/corr-staged/candidates'
    r'/9410944eaa1aadf724cb76010b98f79738d3d0d825f1e1a81364c4b273514ee6'
    r'/correlation.json'
)


def cells(block):
    out = {}
    for row in block.get('blend_cells', []) or []:
        out[(str(row.get('left')), str(row.get('right')))] = row
    return out


def main():
    payload = json.loads(CORRELATION.read_text(encoding='utf-8'))
    modes = payload['dependence_modes']
    live = cells(modes['LIVE_ONLY'])
    hybrid = cells(modes['LIVE_PLUS_BACKTEST'])
    print('pairs: live-only %d, hybrid %d' % (len(live), len(hybrid)))

    same, different = 0, []
    for key in sorted(set(live) & set(hybrid)):
        a, b = live[key], hybrid[key]
        if a.get('observed_correlation') == b.get('observed_correlation'):
            same += 1
        else:
            different.append(key)

    print('identical observed correlations : %d' % same)
    print('differing observed correlations : %d' % len(different))
    print()
    header = '%-28s %-28s %10s %10s %8s %8s' % (
        'left', 'right', 'live r', 'hybrid r', 'live n', 'hyb n')
    print(header)
    for key in sorted(set(live) & set(hybrid)):
        a, b = live[key], hybrid[key]
        mark = ' ' if key in [k for k in different] else ''
        print('%-28s %-28s %10s %10s %8s %8s%s' % (
            key[0][:28], key[1][:28],
            str(a.get('observed_correlation'))[:10],
            str(b.get('observed_correlation'))[:10],
            a.get('observations'), b.get('observations'), mark))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
