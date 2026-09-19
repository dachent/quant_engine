# Controller graph gate

`tools/portfolio_controller_graph.py` is a standard-library development gate
for the supported source-to-panel controller.  It reads Python source with
`ast`; it does not import a financial module, start a producer, execute a
calculation, or write a release artifact.

The graph is a deterministic schema-1 JSON projection.  Its `source_files`
maps every bound repository-relative implementation, contract, and linked test
file to its raw SHA-256.  `source_fingerprint` is the canonical SHA-256 of
that mapping.  Its stage view comes directly from both of these executable
declarations:

- `tools/portfolio_pipeline_materialize.py`: `STAGE_DEPENDENCIES` and
  `ATTENDED_STAGES`.
- `packages/core/src/quant_engine/core/pipeline.py`:
  `_PORTFOLIO_STAGE_DEPENDENCIES` and `_PORTFOLIO_STAGE_ORDER`.

The product stages remain `ledger`, `baseline`, `attribution`, `analytics`,
`report`, and `panel`.  `panel-candidate` is an attended substep, not a new
core product stage.  The gate requires the report-to-panel dependency, so the
MVP path cannot be silently redirected through a workbook.

It also binds the actual static seams: attended preflight to the selected-source
preparation entrypoints, materializer stage command to the release builder,
release builder to report and panel producers, and those producers to their
native report and panel consumers.  Every runtime guarantee points to a real
integration-test path in the graph.  Missing source bytes, changed declarations,
missing static symbols/calls, an omitted bound file, or a stale fingerprint are
validation failures.

The graph is deliberately not an execution authority.  AST cannot prove dynamic
runtime ordering, source admission/finality, recovery/reuse behavior, or
financial numerics.  Those guarantees remain with the supported attended
runner and its integration tests.  The gate neither replaces nor invokes them.

The retained code-mapper snapshots are recorded only as contextual evidence.
Their root, fingerprint, and scanner-error state are inspected and labeled
non-current/non-consumed when they do not match this checkout.  They therefore
cannot certify the graph.  A future accepted mapper must match the actual
scanned root and fingerprint and be free of errors affecting the bound paths
before it may be used as evidence.

Run the gate locally without executing financial code:

```powershell
python tools/portfolio_controller_graph.py --repo-root .
python tools/portfolio_controller_graph.py --repo-root . --write .pt\tmp\controller-graph.json --changed-path tools/portfolio_report.py
```

The second form writes the supplied task-local graph output and reports the
declared downstream impact.  A report change affects report and panel; a
panel-only change affects panel.  Preparation and controller changes affect
their downstream stages.  An unmapped production or contract change raises a
reconciliation error; unrelated documentation/test paths return no execution
stage impact.  The Quality workflow invokes the gate with its installed CI
environment, without a workstation path or an optional visualization tool.
