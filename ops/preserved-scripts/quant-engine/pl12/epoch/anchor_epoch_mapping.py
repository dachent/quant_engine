"""Attribute IBKR capital anchors to the epoch they fund.

Two anchor populations came out unmapped and the account-manifest builder
refuses any unmapped anchor ("provider capital anchor mapping is unavailable",
`build_account_manifest_generation.py:1699`), because an anchor carries a
`strategy_id` by construction:

  * `account-2d0a996cf7ab4876` holds five anchors and never carries a strategy
    assignment at all. It is outside the product's account perimeter, so it has
    no epoch for an anchor to belong to.
  * `account-1b44c987969b9e47` is funded with 35,000 on 2026-01-05 and its only
    assignment, p123-ai-factor, starts 2026-01-22. HWM-003 makes capital
    continuous across epochs, so money already sitting in the account when its
    first strategy starts is that epoch's opening capital, not an orphan. The
    accepted r9 manifest agrees: it books 55,761.32 of net capital there, which
    only reconciles with the 35,000 included.

A day that falls after an assignment has begun but inside no window is a
genuine gap and still maps to nothing.
"""
import re
from pathlib import Path

SOURCE = Path('tools/provider_ledger.py')

HELPER = '''def _capital_anchor_epoch_strategy(
    account_id: str,
    day: date,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[str | None, str]:
    """Resolve the epoch a capital event funds.

    An anchor is a per-strategy fact, so the window that contains the event is
    the first answer.  Capital committed before an account's earliest epoch
    begins is that epoch's opening capital: owner rule HWM-003 makes capital
    continuous across epochs, and an account funded ahead of its strategy start
    opens holding what it was funded with.  A day after an epoch has begun but
    inside no window is a real gap and maps to nothing.
    """

    strategy_id, mapping_status = _mapped_strategy_for_account(
        account_id, day, accounts, strategies, assignments
    )
    if mapping_status == "MAPPED" or account_id not in accounts:
        return strategy_id, mapping_status
    starts: list[tuple[date, str]] = []
    for item in assignments.get(account_id, ()):
        strategy = item.get("strategy_id")
        raw_start = item.get("effective_start")
        if not isinstance(strategy, str) or strategy not in strategies:
            continue
        if raw_start in {None, ""}:
            continue
        starts.append((_day(raw_start, "assignment.effective_start"), strategy))
    if not starts:
        return None, "UNMAPPED"
    first_start = min(start for start, _strategy in starts)
    if day >= first_start:
        return None, "UNMAPPED"
    opening = {strategy for start, strategy in starts if start == first_start}
    if len(opening) != 1:
        return None, "UNMAPPED"
    return opening.pop(), "MAPPED"


'''

SKIP_OLD = """    anchors: list[dict[str, Any]] = []
    for account_id in sorted(by_day):
        if account_id in unquantified:
            continue
"""
SKIP_NEW = """    anchors: list[dict[str, Any]] = []
    for account_id in sorted(by_day):
        if account_id in unquantified:
            continue
        # An anchor names the strategy it funds.  An account the product never
        # assigns to a strategy has no epoch to name, so it yields no anchors
        # rather than anchors nothing can attribute.
        if not _assignable_strategies(account_id, accounts, strategies, assignments):
            continue
"""

ASSIGNABLE = '''def _assignable_strategies(
    account_id: str,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
) -> set[str]:
    """Return the in-perimeter strategies this account is ever assigned to."""

    if account_id not in accounts:
        return set()
    return {
        strategy
        for item in assignments.get(account_id, ())
        for strategy in (item.get("strategy_id"),)
        if isinstance(strategy, str)
        and strategy in strategies
        and item.get("effective_start") not in {None, ""}
    }


'''

CALL_OLD = """            strategy_id, mapping_status = _mapped_strategy_for_account(
                account_id, recognition_day, accounts, strategies, assignments
            )
            source_record_id = f"{account_id}:{recognition_day.isoformat()}\""""
CALL_NEW = """            strategy_id, mapping_status = _capital_anchor_epoch_strategy(
                account_id, recognition_day, accounts, strategies, assignments
            )
            source_record_id = f"{account_id}:{recognition_day.isoformat()}\""""


def main() -> int:
    text = SOURCE.read_text(encoding='utf-8')
    anchor_def = 'def _ibkr_capital_anchors(\n'
    if anchor_def not in text:
        raise SystemExit('the IBKR anchor builder is not where it was')
    for name, block in (('_assignable_strategies', ASSIGNABLE),
                        ('_capital_anchor_epoch_strategy', HELPER)):
        if f'def {name}(' in text:
            raise SystemExit(f'{name} already exists')
        text = text.replace(anchor_def, block + anchor_def, 1)
    for old, new, label in (
        (SKIP_OLD, SKIP_NEW, 'unassigned-account skip'),
        (CALL_OLD, CALL_NEW, 'epoch resolution call'),
    ):
        if text.count(old) != 1:
            raise SystemExit(f'{label} anchor is not unique')
        text = text.replace(old, new, 1)
    SOURCE.write_bytes(text.encode('utf-8').replace(b'\r\n', b'\n'))
    print('patched', SOURCE)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
