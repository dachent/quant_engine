"""Let the dependence source carry the prehistory it already admits.

`portfolio_current_dependence` selects one `INCLUDE_SELECTED_LIVE` segment per
required strategy and projects only those rows. The layered history's
`INCLUDE_VERSION_COMPATIBLE_PREHISTORY` segments -- 19,711 admitted backtest
observations across six strategies -- are never selected, so they never reach
`portfolio_correlation`, which then finds no BACKTEST rows and records
`AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY` for LIVE_PLUS_BACKTEST.
The hybrid dependence outcome is published as hybrid while being live-only.

Both the build path and the packaged verifier select segments, and they have to
agree row for row or the source cannot re-verify, so both move together here.

The live selection is unchanged: `complete_through` is still the intersection
of the live dates, prehistory is admitted only where the layered history
already marked it version-compatible, and a strategy with no such segment
(volatis-strategy) simply contributes none -- `portfolio_correlation` already
falls back per series rather than globally.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
TARGET = REPO / 'tools' / 'portfolio_current_dependence.py'

PREHISTORY_SELECTOR = '''

def _selected_prehistory_identities(
    contract: Mapping[str, Any], required_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """The version-compatible prehistory segment per strategy, where one exists.

    A strategy whose backtest was never admitted contributes nothing, which is
    not an error: the layered history records that decision per strategy, and
    the evidence-mode split downstream degrades that series alone rather than
    the whole mode.
    """

    identities = _objects(
        _object(contract.get("history_corrections"), "history corrections").get(
            "materialized_segment_identities"
        ),
        "materialized segment identities",
    )
    selected: list[dict[str, Any]] = []
    for strategy_id in required_ids:
        matches = [
            row
            for row in identities
            if row.get("strategy_id") == strategy_id
            and row.get("mode_admission") == "INCLUDE_VERSION_COMPATIBLE_PREHISTORY"
        ]
        if len(matches) > 1:
            raise CurrentDependenceError(
                "required dependence strategy has more than one admitted "
                f"prehistory segment: {strategy_id}"
            )
        selected.extend(matches)
    return selected
'''

EDITS = (
    (
        'prehistory selector beside the live one',
        '\n\ndef _packaged_projection(\n',
        PREHISTORY_SELECTOR + '\n\ndef _packaged_projection(\n',
    ),
    (
        'build path: admit the prehistory segments too',
        '''    expected_by_id = {str(row["segment_id"]): row for row in expected_segments}
    for strategy in required_strategies:
''',
        '''    expected_by_id = {str(row["segment_id"]): row for row in expected_segments}
    prehistory_identities = _selected_prehistory_identities(contract, required_ids)
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
    for strategy in required_strategies:
''',
    ),
    (
        'build path: project the prehistory rows',
        '''        for row in live_by_strategy[strategy_id]:
            if _day(row["date"], "history observation date") <= complete_through:
                projected.append({field: row[field] for field in RETURN_FIELDS})
''',
        '''        for row in (
            *prehistory_by_strategy.get(strategy_id, ()),
            *live_by_strategy[strategy_id],
        ):
            if _day(row["date"], "history observation date") <= complete_through:
                projected.append({field: row[field] for field in RETURN_FIELDS})
''',
    ),
)


def main():
    text = TARGET.read_text(encoding='utf-8')
    if '_selected_prehistory_identities' in text:
        print('already admits prehistory')
        return 0
    if '\r\n' in text:
        raise SystemExit('refusing to edit a file that already carries CRLF')
    for label, anchor, replacement in EDITS:
        if text.count(anchor) != 1:
            raise SystemExit('anchor is not unique (%d): %s' % (text.count(anchor), label))
        text = text.replace(anchor, replacement, 1)
        print('applied:', label)
    TARGET.write_bytes(text.encode('utf-8'))
    print('wrote', TARGET.name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
