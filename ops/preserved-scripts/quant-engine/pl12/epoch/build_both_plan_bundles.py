"""Teach the vertical driver to build both decomposition plan bundles.

The product contract requires DECOMPOSITION in LIVE_ONLY and in
LIVE_PLUS_BACKTEST, and the report resolves one lens layout per required mode.
The driver built a single bundle from `EVIDENCE_MODE`, so a hybrid run produced
no live-only layout and the report stopped with `current plan requires a
LIVE_ONLY lens layout`.

The release builder takes the live-only bundle as primary and refuses any other
primary mode, then takes the hybrid one alongside it, checking that both were
built from the same contract bytes. So both are built here from one contract
state and bound together.
"""
from pathlib import Path

DRIVER = Path(r'C:/Dev/scratch/quant-engine/pl12/epoch/build_vertical.sh')
BACKSLASH = chr(92)


def main():
    text = DRIVER.read_text(encoding='utf-8')
    if 'plan_bundle LIVE_ONLY' in text:
        print('already builds both bundles')
        return 0

    line_end = ' ' + BACKSLASH + '\n'
    body = [
        '# The contract requires DECOMPOSITION in both evidence modes and the',
        '# report resolves one lens layout per mode. The release builder refuses',
        '# any primary mode other than LIVE_ONLY, so both bundles are built from',
        '# one contract state; it verifies they carry the same contract bytes.',
        'plan_bundle() {',
        '  "$PY" tools/portfolio_current_decomposition_plan.py' + line_end.rstrip('\n'),
    ]
    options = [
        '--plan docs/analytics/current-strategy-decomposition-plan.json',
        '--catalog docs/analytics/decomposition-source-catalog.json',
        '--financial-mvp-contract contracts/portfolio-financial-mvp-source-v1.json',
        '--layered-history-generation "$LH_ROOT/$HISTORY_ID"',
        '--base-spec "$W/base-spec/base-decomposition-spec.json"',
        '--base-source-root "$E/base-source"',
        '--us-factors "$PRIOR_BUNDLE/inputs/market/us_factors.csv"',
        '--europe-factors "$PRIOR_BUNDLE/inputs/market/europe_factors.csv"',
        '--style-returns "$PRIOR_BUNDLE/inputs/market/style_returns.csv"',
        '--sector-returns "$PRIOR_BUNDLE/inputs/market/sector_returns.csv"',
        '--us-industries "$PRIOR_BUNDLE/inputs/market/us_industries.csv"',
        '--public-factor-manifest "$PRIOR_BUNDLE/inputs/governance/public-factor-source-manifest.json"',
        '--style-manifest "$PRIOR_BUNDLE/inputs/governance/style-source-manifest.json"',
        '--sector-manifest "$PRIOR_BUNDLE/inputs/governance/sector-source-manifest.json"',
        '--evidence-mode "$1"',
        '--evaluation-date 2026-09-04',
        '--assurance-profile RELEASE_QC',
    ]
    helper = '\n'.join(body) + '\n'
    for option in options:
        helper += '    ' + option + line_end
    helper += '    --output-root "$PLAN_ROOT" > "$2"\n}\n\n'

    header = 'echo "== rebuild the decomposition plan bundle =="\n'
    if text.count(header) != 1:
        raise SystemExit('header anchor is not unique: %d' % text.count(header))
    text = text.replace(header, helper + header, 1)

    # Replace the original single invocation with two calls to the helper.
    marker = '"$PY" tools/portfolio_current_decomposition_plan.py ' + BACKSLASH
    start = text.index(marker, text.index(header))
    tail = '> "$CYCLE/plan-bundle.json"'
    end = text.index(tail, start) + len(tail)
    text = (
        text[:start]
        + 'plan_bundle LIVE_ONLY "$CYCLE/plan-bundle.json"\n'
        + 'plan_bundle LIVE_PLUS_BACKTEST "$CYCLE/plan-bundle-hybrid.json"'
        + text[end:]
    )

    old_ids = (
        'PLAN_ID="$(json_field "$CYCLE/plan-bundle.json" bundle_id)"\n'
        'echo "plan bundle $PLAN_ID"\n'
    )
    new_ids = (
        'PLAN_ID="$(json_field "$CYCLE/plan-bundle.json" bundle_id)"\n'
        'HYBRID_PLAN_ID="$(json_field "$CYCLE/plan-bundle-hybrid.json" bundle_id)"\n'
        'echo "plan bundle live-only $PLAN_ID"\n'
        'echo "plan bundle hybrid    $HYBRID_PLAN_ID"\n'
    )
    if text.count(old_ids) != 1:
        raise SystemExit('id anchor is not unique: %d' % text.count(old_ids))
    text = text.replace(old_ids, new_ids, 1)

    old_recipe = '  --decomposition-plan-bundle "$PLAN_ROOT/$PLAN_ID" ' + BACKSLASH + '\n'
    new_recipe = (
        old_recipe
        + '  --hybrid-decomposition-plan-bundle "$PLAN_ROOT/$HYBRID_PLAN_ID" '
        + BACKSLASH + '\n'
    )
    if text.count(old_recipe) != 1:
        raise SystemExit('recipe anchor is not unique: %d' % text.count(old_recipe))
    text = text.replace(old_recipe, new_recipe, 1)

    DRIVER.write_bytes(text.encode('utf-8'))
    print('build_vertical.sh builds LIVE_ONLY and LIVE_PLUS_BACKTEST and binds both')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
