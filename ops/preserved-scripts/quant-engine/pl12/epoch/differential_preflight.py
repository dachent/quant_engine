"""Compare this cycle's recipe against the last cycle that reached a green panel.

Every failure this session cost hours because a downstream role asserted
something the recipe did not carry, and the assertion only ran after the
producers. The previous cycle got through report, workbook, panel and QA, so
its recipe is a worked example of what those roles accept. Differences in
*shape* -- a key present there and missing here, or a mode that differs -- are
the candidates worth checking before paying for another analytics stage.

This reports shape, never values: generation ids and digests are supposed to
differ between cycles.
"""
import json
from pathlib import Path

PRIOR = Path(r'C:/Dev/scratch/quant-engine/pl12/cycle/vertical-recipe.json')
CURRENT = Path(r'C:/Dev/scratch/quant-engine/pl12/cycle2/vertical-recipe-two-mode.json')


def shape(node, prefix=''):
    """Every key path in the document, with the leaf's type."""
    out = {}
    if isinstance(node, dict):
        for key, value in node.items():
            out.update(shape(value, prefix + '.' + key))
    elif isinstance(node, list):
        out[prefix] = 'list[%d]' % len(node)
        if node:
            out.update(shape(node[0], prefix + '[]'))
    else:
        out[prefix] = type(node).__name__
    return out


def main():
    if not PRIOR.is_file():
        print('no prior recipe at %s' % PRIOR)
        return 1
    if not CURRENT.is_file():
        print('no current recipe at %s' % CURRENT)
        return 1
    prior = json.loads(PRIOR.read_text(encoding='utf-8'))
    current = json.loads(CURRENT.read_text(encoding='utf-8'))

    print('prior   schema_version %s  pipeline %s'
          % (prior.get('schema_version'), prior.get('pipeline_id')))
    print('current schema_version %s  pipeline %s'
          % (current.get('schema_version'), current.get('pipeline_id')))

    prior_shape, current_shape = shape(prior), shape(current)
    missing = sorted(set(prior_shape) - set(current_shape))
    added = sorted(set(current_shape) - set(prior_shape))
    changed = sorted(
        key for key in set(prior_shape) & set(current_shape)
        if prior_shape[key] != current_shape[key]
    )

    print('\nin the known-good recipe but NOT in this one  (%d)' % len(missing))
    for key in missing:
        print('   %-56s %s' % (key, prior_shape[key]))
    print('\nnew in this recipe  (%d)' % len(added))
    for key in added:
        print('   %-56s %s' % (key, current_shape[key]))
    print('\ntype changed  (%d)' % len(changed))
    for key in changed:
        print('   %-56s %s -> %s' % (key, prior_shape[key], current_shape[key]))

    print('\ntop-level keys')
    for key in sorted(set(prior) | set(current)):
        mark = '  ' if key in prior and key in current else ('- ' if key in prior else '+ ')
        print('  %s%s' % (mark, key))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
