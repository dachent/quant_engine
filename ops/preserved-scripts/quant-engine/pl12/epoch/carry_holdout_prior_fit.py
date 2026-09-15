"""Carry each holdout step's posterior forward as the next step's prior.

The holdout walk fits twice per step: a prior on the history so far, then a
posterior on that history extended by the observation just scored. The next
iteration's prior is then computed on exactly those extended arrays, with the
same smoothness, ridge and fixed intercept -- which is the posterior that was
just computed. `_dynamic_fit` is pure and the arrays are not touched in
between, so the second call reproduces the first bit for bit.

Carrying it forward takes the walk from 2*N_test solves to N_test + 1. Each
solve is a full block-tridiagonal pass over a history that grows by one row per
step, so this loop dominates the per-model cost -- far more than the smoothness
sweep above it.

This is the same computation on the same bytes, not a cheaper approximation:
the published lens digest is unchanged. The accompanying test pins that.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tools/portfolio_decomposition.py'
raw = path.read_bytes()

old = b'''    for index, observation in enumerate(test):
        prior_fit = _dynamic_fit(
            history_matrix,
            history_target,
            smoothness=selected_smoothness,
            ridge=ridge,
            fixed_intercept=train_fit.intercept,
        )
        forecast_exposure = prior_fit.exposures[-1].copy()'''
new = b'''    # The posterior of one step is the prior of the next: same arrays, same
    # smoothness, ridge and fixed intercept, and `_dynamic_fit` is pure. Only
    # the first prior has to be solved for.
    carried_fit: DynamicFit | None = None
    for index, observation in enumerate(test):
        prior_fit = (
            carried_fit
            if carried_fit is not None
            else _dynamic_fit(
                history_matrix,
                history_target,
                smoothness=selected_smoothness,
                ridge=ridge,
                fixed_intercept=train_fit.intercept,
            )
        )
        forecast_exposure = prior_fit.exposures[-1].copy()'''
assert raw.count(old) == 1, raw.count(old)
raw = raw.replace(old, new)

old_tail = b'''        filtered_exposure = posterior_fit.exposures[-1].copy()'''
new_tail = b'''        carried_fit = posterior_fit
        filtered_exposure = posterior_fit.exposures[-1].copy()'''
assert raw.count(old_tail) == 1, raw.count(old_tail)
raw = raw.replace(old_tail, new_tail)
path.write_bytes(raw)
print('ok')
