"""Pin the identity the holdout walk's carried prior depends on.

The walk fits twice per step: a posterior on the history extended by the
observation just scored, then -- on the next iteration -- a prior on exactly
those same arrays. Carrying the posterior forward is only sound because
`_dynamic_fit` is pure and nothing mutates the arrays in between. That is an
assumption about the code, so it is pinned rather than trusted.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
TEST = REPO / 'tests/portfolio_analytics/test_portfolio_decomposition.py'

ADDITION = '''

def test_dynamic_fit_repeats_exactly_so_the_holdout_prior_can_be_carried():
    """The next step's prior is the step's posterior, bit for bit.

    The holdout walk used to solve for both. They are the same call on the same
    arrays with the same smoothness, ridge and fixed intercept, so the walk now
    carries the posterior forward and solves once per step instead of twice.
    If `_dynamic_fit` ever stops being pure -- an internal cache, a random
    initialisation, an in-place write into the arrays it is handed -- that
    optimisation would silently change published exposures, and this fails.
    """

    import numpy as np

    rng = np.random.default_rng(11)
    observations, factors = 60, 3
    matrix = rng.normal(size=(observations, factors))
    target = matrix @ rng.normal(size=factors) + rng.normal(
        scale=0.05, size=observations
    )
    intercept = 0.004

    posterior = decomposition._dynamic_fit(
        matrix, target, smoothness=0.3, ridge=1e-6, fixed_intercept=intercept
    )
    matrix_after = matrix.copy()
    target_after = target.copy()
    next_prior = decomposition._dynamic_fit(
        matrix, target, smoothness=0.3, ridge=1e-6, fixed_intercept=intercept
    )

    # The inputs survive the call, so the second caller really does hand over
    # the same arrays the first one did.
    assert np.array_equal(matrix, matrix_after)
    assert np.array_equal(target, target_after)
    # And the result is identical, not merely close.
    assert next_prior.intercept == posterior.intercept
    assert next_prior.rank_status == posterior.rank_status
    assert np.array_equal(next_prior.exposures, posterior.exposures)
    assert np.array_equal(next_prior.fitted, posterior.fitted)
'''


def main():
    text = TEST.read_text(encoding='utf-8')
    if 'test_dynamic_fit_repeats_exactly_so_the_holdout_prior_can_be_carried' in text:
        print('already present')
        return 0
    TEST.write_bytes((text.rstrip('\n') + '\n' + ADDITION).encode('utf-8'))
    print('added the carried-prior identity test')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
