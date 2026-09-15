"""Pin the dynamic leave-one-out identities the linear-time scorer relies on."""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tests/portfolio_analytics/test_portfolio_decomposition.py'
raw = path.read_bytes()
nl = (chr(13) + chr(10)).encode() if (chr(13) + chr(10)).encode() in raw else chr(10).encode()

test = '''

def _loo_reference(module, matrix, target, *, smoothness, ridge):
    """Recompute leave-one-out the way the quadratic implementation did.

    One right-hand side per observation, materializing the whole smoother. It
    is the definition the linear-time scorer has to reproduce, and it is only
    tractable at these sizes.
    """

    import numpy as np

    fit = module._dynamic_fit(matrix, target, smoothness=smoothness, ridge=ridge)
    observations, factors = matrix.shape
    identity = np.eye(factors, dtype=np.float64)
    diagonal = [
        np.outer(matrix[i], matrix[i])
        + (ridge + smoothness * (1.0 if i in {0, observations - 1} else 2.0)) * identity
        for i in range(observations)
    ]
    off_diagonal = [(-smoothness * identity).copy() for _ in range(observations - 1)]
    right = np.zeros((observations, factors, observations + 1), dtype=np.float64)
    for i in range(observations):
        right[i, :, i] = matrix[i]
        right[i, :, -1] = matrix[i]
    solved, _status = module._block_tridiagonal_solve(diagonal, off_diagonal, right)
    influence = solved[:, :, :-1]
    intercept_solution = solved[:, :, -1]
    denominator = float(observations) - float(np.sum(matrix * intercept_solution))
    projected = np.einsum("tk,tkn->n", matrix, influence)
    intercept_influence = (1.0 - projected) / denominator
    local = np.einsum("ik,iki->i", matrix, influence, optimize=False)
    local_projection = np.sum(matrix * intercept_solution, axis=1)
    leverage = local + intercept_influence * (1.0 - local_projection)
    predictions = (fit.fitted - leverage * target) / (1.0 - leverage)
    return module._r_squared(target, predictions, float(np.mean(target))), predictions


@pytest.mark.parametrize("smoothness", [0.05, 0.5])
@pytest.mark.parametrize("shape", [(40, 3), (120, 5)])
def test_dynamic_loo_matches_the_explicit_smoother(shape, smoothness):
    """The linear-time scorer reproduces the per-observation refit exactly."""

    import numpy as np

    observations, factors = shape
    rng = np.random.default_rng(20260912)
    matrix = rng.normal(size=(observations, factors))
    target = matrix @ rng.normal(size=factors) + rng.normal(scale=0.1, size=observations)

    score, predictions = decomposition._dynamic_loo_score(
        matrix, target, smoothness=smoothness, ridge=1e-6
    )
    expected_score, expected_predictions = _loo_reference(
        decomposition, matrix, target, smoothness=smoothness, ridge=1e-6
    )

    assert score == pytest.approx(expected_score, abs=1e-9)
    assert predictions == pytest.approx(expected_predictions, abs=1e-8)


def test_inverse_diagonal_matches_a_dense_inverse():
    """The Takahashi recursion returns the true diagonal blocks."""

    import numpy as np

    rng = np.random.default_rng(11)
    count, size = 7, 3
    identity = np.eye(size)
    diagonal = []
    for _ in range(count):
        block = rng.normal(size=(size, size))
        diagonal.append(block @ block.T + 4.0 * identity)
    off_diagonal = [(-0.3 * identity).copy() for _ in range(count - 1)]

    dense = np.zeros((count * size, count * size))
    for i, block in enumerate(diagonal):
        dense[i * size:(i + 1) * size, i * size:(i + 1) * size] = block
    for i, block in enumerate(off_diagonal):
        dense[i * size:(i + 1) * size, (i + 1) * size:(i + 2) * size] = block
        dense[(i + 1) * size:(i + 2) * size, i * size:(i + 1) * size] = block.T
    inverse = np.linalg.inv(dense)

    blocks, status = decomposition._block_tridiagonal_inverse_diagonal(
        diagonal, off_diagonal
    )

    assert status == "BLOCK_TRIDIAGONAL_FULL_RANK"
    for i, block in enumerate(blocks):
        expected = inverse[i * size:(i + 1) * size, i * size:(i + 1) * size]
        assert block == pytest.approx(expected, abs=1e-10)


def test_dynamic_loo_stays_linear_in_the_series_length():
    """A spliced series must not cost the square of its length.

    The previous implementation allocated one smoother column per observation;
    at this length that array alone is 18 GB, so the guard is that the call
    completes at all rather than any particular duration.
    """

    import numpy as np

    rng = np.random.default_rng(3)
    observations, factors = 6000, 8
    matrix = rng.normal(size=(observations, factors))
    target = matrix @ rng.normal(size=factors) + rng.normal(scale=0.1, size=observations)

    score, predictions = decomposition._dynamic_loo_score(
        matrix, target, smoothness=0.2, ridge=1e-6
    )

    assert score is not None
    assert len(predictions) == observations
'''

assert b'def test_dynamic_loo_matches_the_explicit_smoother' not in raw
path.write_bytes(raw.rstrip() + nl + test.replace('\n', nl.decode()).encode('utf-8'))
print('ok')
