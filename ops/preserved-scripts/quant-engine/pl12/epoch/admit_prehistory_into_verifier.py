"""Mirror the prehistory admission in the packaged verifier.

The verifier re-derives the projection from the packaged segments and compares
it to what the bundle carries, so it has to select exactly what the build path
selects. It also refuses any segment outside its expected set, which is where a
one-sided change would surface -- as `dependence segments contain an unaccepted
row` rather than as a number that differs.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
TARGET = REPO / 'tools' / 'portfolio_current_dependence.py'

EDITS = (
    (
        'verifier: expect the prehistory segments as well',
        '''    selected_identities = _selected_live_identities(contract, required_ids)
    expected_by_id = {
        _text(row.get("segment_id"), "materialized segment_id"): row
        for row in selected_identities
    }
''',
        '''    selected_identities = _selected_live_identities(contract, required_ids)
    prehistory_identities = _selected_prehistory_identities(contract, required_ids)
    expected_by_id = {
        _text(row.get("segment_id"), "materialized segment_id"): row
        for row in (*selected_identities, *prehistory_identities)
    }
''',
    ),
    (
        'verifier: collect the prehistory rows per strategy',
        '''        live_by_strategy[strategy_id] = rows

    common_dates = set.intersection(
        *({row["date"] for row in live_by_strategy[item]} for item in required_ids)
    )
''',
        '''        live_by_strategy[strategy_id] = rows

    prehistory_by_strategy: dict[str, list[dict[str, str]]] = {}
    for identity in prehistory_identities:
        strategy_id = _text(identity.get("strategy_id"), "history strategy_id")
        segment_id = _text(identity.get("segment_id"), "materialized segment_id")
        rows = actual_by_id.get(segment_id)
        if not rows:
            raise CurrentDependenceError(
                f"admitted prehistory segment is absent: {segment_id}"
            )
        prehistory_by_strategy[strategy_id] = rows

    common_dates = set.intersection(
        *({row["date"] for row in live_by_strategy[item]} for item in required_ids)
    )
''',
    ),
    (
        'verifier: project the prehistory rows',
        '''        for row in live_by_strategy[strategy_id]:
            if _day(row["date"], "dependence observation date") <= complete_through:
                projected.append({field: row[field] for field in RETURN_FIELDS})
''',
        '''        for row in (
            *prehistory_by_strategy.get(strategy_id, ()),
            *live_by_strategy[strategy_id],
        ):
            if _day(row["date"], "dependence observation date") <= complete_through:
                projected.append({field: row[field] for field in RETURN_FIELDS})
''',
    ),
)


def main():
    text = TARGET.read_text(encoding='utf-8')
    if 'prehistory_by_strategy' in text and 'dependence observation date' in text.split(
        'prehistory_by_strategy'
    )[-1]:
        pass
    if '\r\n' in text:
        raise SystemExit('refusing to edit a file that already carries CRLF')
    for label, anchor, replacement in EDITS:
        if replacement in text:
            print('already applied:', label)
            continue
        if text.count(anchor) != 1:
            raise SystemExit('anchor is not unique (%d): %s' % (text.count(anchor), label))
        text = text.replace(anchor, replacement, 1)
        print('applied:', label)
    TARGET.write_bytes(text.encode('utf-8'))
    print('wrote', TARGET.name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
