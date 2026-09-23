"""Prove the carried prior reproduces the double-solve walk exactly.

The EU hybrid model came back DIAGNOSTIC_ONLY with a strongly negative holdout
R-squared, and that is the same code path the carry-forward optimisation
touched today. A wrong prior would produce exactly that symptom, so the
optimisation has to be cleared before the result can be read as a finding.

This replays the holdout walk on real-shaped data both ways -- the old loop
that solved for the prior every step, and the new one that carries the previous
posterior -- and compares every forecast the walk produces.
"""
import sys
from pathlib import Path

import numpy as np

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
sys.path[:0] = [str(REPO / 'tools')] + [str(path) for path in REPO.glob('packages/*/src')]

import portfolio_decomposition as decomposition  # noqa: E402


def walk(matrix, target, split, *, smoothness, ridge, carry):
    """Reproduce the holdout walk; `carry` selects the new or old prior."""

    train_matrix, train_target = matrix[:split], target[:split]
    test_matrix, test_target = matrix[split:], target[split:]
    train_fit = decomposition._dynamic_fit(
        train_matrix, train_target, smoothness=smoothness, ridge=ridge
    )
    history_matrix = train_matrix.copy()
    history_target = train_target.copy()
    carried = None
    forecasts, predictions = [], []
    for index in range(len(test_target)):
        if carry and carried is not None:
            prior_fit = carried
        else:
            prior_fit = decomposition._dynamic_fit(
                history_matrix,
                history_target,
                smoothness=smoothness,
                ridge=ridge,
                fixed_intercept=train_fit.intercept,
            )
        forecast_exposure = prior_fit.exposures[-1].copy()
        predictions.append(
            float(train_fit.intercept + test_matrix[index] @ forecast_exposure)
        )
        forecasts.append(forecast_exposure)
        history_matrix = np.vstack((history_matrix, test_matrix[index]))
        history_target = np.append(history_target, test_target[index])
        posterior_fit = decomposition._dynamic_fit(
            history_matrix,
            history_target,
            smoothness=smoothness,
            ridge=ridge,
            fixed_intercept=train_fit.intercept,
        )
        carried = posterior_fit
    return np.array(predictions), np.array(forecasts)


def main():
    # Shaped like the EU europe-economic model: six factors, a long history and
    # a holdout of a few hundred days.
    rng = np.random.default_rng(7)
    observations, factors = 1200, 6
    matrix = rng.normal(size=(observations, factors))
    drift = np.cumsum(rng.normal(scale=0.01, size=(observations, factors)), axis=0)
    target = np.einsum('ij,ij->i', matrix, 0.5 + drift) + rng.normal(
        scale=0.05, size=observations
    )
    split = 900

    old_predictions, old_forecasts = walk(
        matrix, target, split, smoothness=1e-5, ridge=1e-6, carry=False
    )
    new_predictions, new_forecasts = walk(
        matrix, target, split, smoothness=1e-5, ridge=1e-6, carry=True
    )

    print('holdout steps        : %d' % len(old_predictions))
    print('predictions identical: %s' % np.array_equal(old_predictions, new_predictions))
    print('forecasts identical  : %s' % np.array_equal(old_forecasts, new_forecasts))
    print('max |prediction gap| : %.3e'
          % float(np.max(np.abs(old_predictions - new_predictions))))
    print('max |exposure gap|   : %.3e'
          % float(np.max(np.abs(old_forecasts - new_forecasts))))

    # And the number the quality gate actually reads.
    actual = target[split:]
    def predicted_r2(predictions):
        residual = float(np.sum((actual - predictions) ** 2))
        total = float(np.sum((actual - np.mean(actual)) ** 2))
        return 1 - residual / total
    print('holdout R2 old       : %.12f' % predicted_r2(old_predictions))
    print('holdout R2 new       : %.12f' % predicted_r2(new_predictions))
    return 0 if np.array_equal(old_predictions, new_predictions) else 1


if __name__ == '__main__':
    raise SystemExit(main())
