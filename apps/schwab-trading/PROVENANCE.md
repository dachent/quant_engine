# Schwab application provenance

This application was absorbed from
[`dachent/schwab_trading_engine`](https://github.com/dachent/schwab_trading_engine)
at source commit `4864bc1428563b79cdeda35d55dd5f7c73b4ee3c`.

The source repository's 11-commit history was imported without squashing by Git
subtree commit `aaa3bc237c845a484669f9930c59686a1af93f94`. The subtree merge retains the
original source commit as a parent, so the upstream history remains reachable
from this monorepo.

Project-local adaptations are intentionally narrow:

- Schwab order submission and cancellation are unconditionally disabled.
- The Tkinter `Place Orders` control is disabled and the task runner fails
  before client construction, network access, or state mutation.
- Runtime jobs, results, logs, imports, exports, SQLite state, and DPAPI
  credentials live under the governed local `dataSchwab` runtime root, never
  in Git.
- The existing UI and `runner.py --task ... --request-file ... --result-file
  ...` seam remain the operator and callable entry points.

The upstream repository remains a provenance source. Archiving or deleting it
is outside issue `SCH-APP-001`.
