"""Score dynamic leave-one-out in linear time instead of quadratic.

The PRESS identity needs two things from the smoother: the diagonal of the hat
matrix, and each column's sum. The scorer obtained both by solving the block
system against one right-hand side per observation, which materializes a
``T x K x (T+1)`` array and costs O(T^2 K^3). At the live-only lengths this code
was written for that is a few hundred observations and it is free; with the
backtest prehistory spliced in the industry lens reaches fourteen thousand, and
the stage exceeded its two-hour budget having allocated gigabytes.

Neither quantity actually needs those columns.

The column sums are already computed. With ``b`` the stacked regressor vector,
the n-th right-hand side is ``e_n x_n``, so the n-th column sum is
``b^T A^-1 e_n x_n = (A^-1 b)_n . x_n`` -- and ``A^-1 b`` is the single extra
column the scorer already solves for as ``intercept_solution``. That makes
``projected_influence`` algebraically identical to ``local_intercept_projection``
on the next line; one of the two was always redundant.

The hat diagonal needs only the diagonal blocks of the inverse, which follow
from the factorization the forward pass already performs:
``[A^-1]_ii = S_i^-1 + U_i [A^-1]_i+1,i+1 U_i^T``, where ``S_i`` is the Schur
complement and ``U_i = S_i^-1 E_i``. That is the Takahashi recursion, O(T K^3)
in time and O(T K^2) in memory.

The result is the same number to floating point, which the accompanying test
pins against the previous implementation.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tools/portfolio_decomposition.py'
raw = path.read_bytes()

helper = b'''def _block_tridiagonal_inverse_diagonal(
    diagonal: Sequence[FloatArray],
    off_diagonal: Sequence[FloatArray],
) -> tuple[list[FloatArray], str]:
    """Return the diagonal blocks of a symmetric block-tridiagonal inverse.

    The forward pass is the same block Thomas elimination the solver performs,
    so ``schur[i]`` is that block's Schur complement and ``upper[i]`` is
    ``schur[i]^-1 @ off_diagonal[i]``. The backward pass is the Takahashi
    recursion, which reconstructs each diagonal inverse block from the one
    after it without ever forming the whole inverse.
    """

    count = len(diagonal)
    if count == 0 or len(off_diagonal) != count - 1:
        raise DecompositionError("dynamic system dimensions are invalid")
    used_pseudoinverse = False
    schur: list[FloatArray] = []
    upper: list[FloatArray] = []
    for index in range(count):
        effective = diagonal[index].copy()
        if index:
            effective -= off_diagonal[index - 1].T @ upper[index - 1]
        schur.append(cast(FloatArray, effective))
        if index < count - 1:
            solved_upper, fallback = _linear_solve(effective, off_diagonal[index])
            used_pseudoinverse = used_pseudoinverse or fallback
            upper.append(solved_upper)
    identity = np.eye(diagonal[0].shape[0], dtype=np.float64)
    inverse_diagonal: list[FloatArray] = [
        cast(FloatArray, np.empty_like(identity)) for _ in range(count)
    ]
    last, fallback = _linear_solve(schur[-1], identity)
    used_pseudoinverse = used_pseudoinverse or fallback
    inverse_diagonal[-1] = last
    for index in range(count - 2, -1, -1):
        schur_inverse, fallback = _linear_solve(schur[index], identity)
        used_pseudoinverse = used_pseudoinverse or fallback
        inverse_diagonal[index] = cast(
            FloatArray,
            schur_inverse
            + upper[index] @ inverse_diagonal[index + 1] @ upper[index].T,
        )
    status = (
        "BLOCK_TRIDIAGONAL_PSEUDOINVERSE"
        if used_pseudoinverse
        else "BLOCK_TRIDIAGONAL_FULL_RANK"
    )
    return inverse_diagonal, status


def _dynamic_fit(
'''
anchor = b'def _dynamic_fit(\n'
assert raw.count(anchor) == 1, raw.count(anchor)
raw = raw.replace(anchor, helper, 1)

old = b'''    right = np.zeros(
        (observation_count, factor_count, observation_count + 1),
        dtype=np.float64,
    )
    for index in range(observation_count):
        right[index, :, index] = matrix[index]
        right[index, :, -1] = matrix[index]
    solved, _rank_status = _block_tridiagonal_solve(
        diagonal,
        off_diagonal,
        cast(FloatArray, right),
    )
    influence = solved[:, :, :-1]
    intercept_solution = solved[:, :, -1]
    denominator = float(observation_count) - float(np.sum(matrix * intercept_solution))
    if abs(denominator) <= 1e-12:
        raise DecompositionError("dynamic intercept is not identifiable")
    projected_influence = np.einsum("tk,tkn->n", matrix, influence)
    intercept_influence = (1.0 - projected_influence) / denominator
    local_influence = np.einsum("ik,iki->i", matrix, influence, optimize=False)
    local_intercept_projection = np.sum(matrix * intercept_solution, axis=1)
'''
new = b'''    right = np.zeros((observation_count, factor_count, 1), dtype=np.float64)
    right[:, :, 0] = matrix
    solved, _rank_status = _block_tridiagonal_solve(
        diagonal,
        off_diagonal,
        cast(FloatArray, right),
    )
    intercept_solution = solved[:, :, 0]
    denominator = float(observation_count) - float(np.sum(matrix * intercept_solution))
    if abs(denominator) <= 1e-12:
        raise DecompositionError("dynamic intercept is not identifiable")
    inverse_diagonal, _inverse_status = _block_tridiagonal_inverse_diagonal(
        diagonal, off_diagonal
    )
    local_influence = np.einsum(
        "ik,ikl,il->i", matrix, np.stack(inverse_diagonal), matrix, optimize=True
    )
    # The n-th right-hand side is e_n x_n, so the n-th column sum of the hat
    # matrix is b^T A^-1 e_n x_n = (A^-1 b)_n . x_n -- the same quantity as the
    # local intercept projection below, computed from the one solve above.
    local_intercept_projection = np.sum(matrix * intercept_solution, axis=1)
    intercept_influence = (1.0 - local_intercept_projection) / denominator
'''
assert raw.count(old) == 1, raw.count(old)
raw = raw.replace(old, new)
path.write_bytes(raw)
print('ok')
