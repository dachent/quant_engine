# Regression corpus (#911)

One named case per defect, in `registry.json`. Add a case when you close a defect ticket:

1. Write `tests/regression/test_<case_id>.py` with the function named in the registry. Build a
   small synthetic fixture from the *shape* of the failure; no real account values, ids or
   balances. The test must fail on the pre-fix code (state how in `kill`).
2. Set the case `status` to `covered` with `test` = `path::function`, and cite the case under the
   ticket in `tickets`.
3. `tools/check_regression_corpus.py` (run in CI) checks the registry is consistent.
   The release gate runs `tools/check_regression_corpus.py --release --closed <tickets...>`: it
   fails on any open case and on any closed ticket that cites no covered case.

Tickets that are not defects (process or run-time tickets) may be exempt with a written reason.
