"""Flip the leverage requirement into a deferral guard while #396 is open.

The validator required every account segment to declare the three leverage
classes. They are now out of MVP-2's scope by owner ruling, so the same place
must refuse the opposite mistake: silently re-declaring a class whose producer
still does not exist would put the product straight back into
REQUIRED_DATA_CLASS_NOT_MATERIALIZED, which is what blocked execution.

Re-adding them is therefore a deliberate edit here, named to the issue.
"""
from pathlib import Path

path = Path('tools/portfolio_financial_mvp_contract.py')
text = path.read_text(encoding='utf-8')

old_check = '''        if source_id in REQUIRED_ACCOUNT_PROVIDER_SOURCE_IDS:
            missing_leverage_classes = {
                "FINANCING_COST",
                "FINANCING_LIABILITY",
                "LEVERAGE_EXPOSURE",
            } - set(classes)
            if missing_leverage_classes:
                raise FinancialMvpContractError(
                    f"account source lacks leverage facts: {source_id}: "
                    f"{sorted(missing_leverage_classes)}"
                )
'''
new_check = '''        # Issue #396 defers the leverage facts by owner ruling: no provider has
        # a materialization path for them, so declaring one would only restore
        # REQUIRED_DATA_CLASS_NOT_MATERIALIZED and block execution again.
        # Re-adding a class belongs with its producer, in the same change.
        deferred_leverage_classes = DEFERRED_LEVERAGE_CLASSES & set(classes)
        if deferred_leverage_classes:
            raise FinancialMvpContractError(
                f"source declares leverage facts deferred by issue 396: "
                f"{source_id}: {sorted(deferred_leverage_classes)}"
            )
'''
assert text.count(old_check) == 1
text = text.replace(old_check, new_check)

# Drop the three from the required set, and name them as deferred beside it.
old_required = '''        "EXTERNAL_FLOW",
        "FINANCING_COST",
        "FINANCING_LIABILITY",
        "FX_RATE",
'''
new_required = '''        "EXTERNAL_FLOW",
        "FX_RATE",
'''
assert text.count(old_required) == 1
text = text.replace(old_required, new_required)

old_leverage = '''        "INDUSTRY_RETURN",
        "LEVERAGE_EXPOSURE",
        "MARKET_PRICE",
'''
new_leverage = '''        "INDUSTRY_RETURN",
        "MARKET_PRICE",
'''
assert text.count(old_leverage) == 1
text = text.replace(old_leverage, new_leverage)

anchor = 'REQUIRED_ACCOUNT_PROVIDER_SOURCE_IDS: Final = frozenset('
addition = '''# Deferred by owner ruling under issue #396: declared by no segment until a
# provider can materialize them. `_DATA_CLASS_ADMISSION_FACT` still maps them,
# so re-adding a class needs only its producer and this set.
DEFERRED_LEVERAGE_CLASSES: Final = frozenset(
    {
        "FINANCING_COST",
        "FINANCING_LIABILITY",
        "LEVERAGE_EXPOSURE",
    }
)
'''
assert text.count(anchor) == 1
text = text.replace(anchor, addition + anchor)

path.write_bytes(text.encode('utf-8'))
print('leverage requirement inverted into a deferral guard')
