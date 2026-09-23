# Preserved capture outputs — relocated out of this repository

The Portfolio123 capture outputs that briefly lived at `ops/preserved-captures/` are **data**,
not code, and the repository's own boundary policy rejects them:
`tools/check_repository_boundary.py` flagged eight `spr_user` profile responses as
`BOUNDARY_CONTENT_CREDENTIAL`, and `.gitignore` already excludes `*.csv` and `**/downloads/**`,
which silently dropped 237 of the 404 files at `git add` time.

They now live in the store, where provider data belongs:

    dataPortfolio123/preserved-captures/
        p123-union/              2026-09-02 capture workspace
        p123-refresh-20260907/   2026-09-07 refresh

167 files, 3.9 MB. Of those, 125 were verified unique — no byte-identical copy existed in any
store — so they were copied to the store *before* being removed from the branch, not after.

The capture and extraction **scripts** are code and remain here, under
`ops/preserved-scripts/`. Issue [#422](https://github.com/dachent/quant_engine/issues/422)
covers folding them into `packages/web-contracts/` with `full` and `targets` capture modes;
[#412](https://github.com/dachent/quant_engine/issues/412) is the adjudication these captures
are evidence for.

Recorded because putting data in this repository was the error, and a deleted directory with no
explanation is how the next person repeats it. See
[#421](https://github.com/dachent/quant_engine/issues/421).
