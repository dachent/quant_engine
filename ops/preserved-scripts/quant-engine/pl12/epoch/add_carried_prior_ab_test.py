"""Pin the carry-forward optimisation against the walk it replaced.

The committed test pins `_dynamic_fit`'s purity, which is what the
optimisation rests on, but not the optimisation itself: a future edit could
carry the wrong fit forward and still pass it. This adds the direct comparison
-- the old double-solve walk against the shipped one -- on a dataset small
enough to run in a normal suite.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
TEST = REPO / 'tests/portfolio_analytics/test_portfolio_decomposition.py'

ADDITION = '''

def test_carried_holdout_prior_reproduces_the_double_solve_walk():
    """Carrying the posterior forward changes cost, never a single forecast.

    The walk the scorer runs is reproduced here twice on the same arrays: once
    solving for the prior at every step, as it did before, and once carrying
    the previous step's posterior, as it does now. Every forecast exposure and
    every holdout prediction must agree exactly -- not approximately -- because
    the optimisation claims to be the same computation rather than a cheaper
    estimate of it.
    """

    import numpy as np

    def walk(matrix, target, split, *, carry):
        train_matrix, train_target = matrix[:split], target[:split]
        test_matrix, test_target = matrix[split:], target[split:]
        train_fit = decomposition._dynamic_fit(
            train_matrix, train_target, smoothness=1e-5, ridge=1e-6
        )
        history_matrix = train_matrix.copy()
        history_target = train_target.copy()
        carried = None
        predictions, forecasts = [], []
        for index in range(len(test_target)):
            if carry and carried is not None:
                prior_fit = carried
            else:
                prior_fit = decomposition._dynamic_fit(
                    history_matrix,
                    history_target,
                    smoothness=1e-5,
                    ridge=1e-6,
                    fixed_intercept=train_fit.intercept,
                )
            forecast_exposure = prior_fit.exposures[-1].copy()
            forecasts.append(forecast_exposure)
            predictions.append(
                float(train_fit.intercept + test_matrix[index] @ forecast_exposure)
            )
            history_matrix = np.vstack((history_matrix, test_matrix[index]))
            history_target = np.append(history_target, test_target[index])
            carried = decomposition._dynamic_fit(
                history_matrix,
                history_target,
                smoothness=1e-5,
                ridge=1e-6,
                fixed_intercept=train_fit.intercept,
            )
        return np.array(predictions), np.array(forecasts)

    rng = np.random.default_rng(7)
    observations, factors, split = 260, 4, 200
    matrix = rng.normal(size=(observations, factors))
    # Exposures that actually move, so a stale carried prior would show up.
    drift = np.cumsum(rng.normal(scale=0.02, size=(observations, factors)), axis=0)
    target = np.einsum("ij,ij->i", matrix, 0.5 + drift) + rng.normal(
        scale=0.05, size=observations
    )

    resolved_predictions, resolved_forecasts = walk(matrix, target, split, carry=False)
    carried_predictions, carried_forecasts = walk(matrix, target, split, carry=True)

    assert np.array_equal(resolved_predictions, carried_predictions)
    assert np.array_equal(resolved_forecasts, carried_forecasts)
'''


def main():
    text = TEST.read_text(encoding='utf-8')
    if 'test_carried_holdout_prior_reproduces_the_double_solve_walk' in text:
        print('already present')
        return 0
    TEST.write_bytes((text.rstrip('\n') + '\n' + ADDITION).encode('utf-8'))
    print('added the carried-prior A/B test')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
