"""Identify which retained vendor workbook each strategy-history input wants.

The builder names its inputs by role, not by filename, and the retained folder
carries several near-identical workbooks per vendor. This reports the sheet
names so the right file is chosen from evidence rather than by guessing at a
filename.
"""
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

STRAT = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/Strategies'
)
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


def sheets(path):
    try:
        with zipfile.ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read('xl/workbook.xml'))
    except Exception as exc:  # noqa: BLE001 - report and continue
        return ['<unreadable: %s>' % exc]
    return [node.get('name') for node in root.iter(NS + 'sheet')]


def main():
    wanted = {
        'tt': ('Index', 'raw_ttdata'),
        'qsi-appendix': (),
        'grizzly': (),
    }
    print('== TuringTrader candidates')
    for path in sorted((STRAT / 'TuringTrader').rglob('*.xlsx')):
        names = sheets(path)
        hit = all(name in names for name in wanted['tt'])
        print('%-5s %-60s %s' % ('HIT' if hit else '', path.name, names[:8]))
    print()
    print('== GrizzlyBulls candidates')
    for path in sorted((STRAT / 'GrizzlyBulls').glob('*.xlsx')):
        print('%-60s %s' % (path.name, sheets(path)[:8]))
    print()
    print('== QSI appendix candidates')
    for path in sorted((STRAT / 'Quanta72 Stratum Index').rglob('*.xlsx')):
        print('%-60s %s' % (path.name, sheets(path)[:8]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
