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
preparation entrypoints (including reachable same-module helpers), materializer
stage command to the release builder, and the release builder's exact Python and
CLI calls. The preparation route must reach the layered-history, governed-HWM,
dependence, decomposition-plan, epoch-manifest, and governed-capital producer
or verifier seams. The core coordinator's `execute_pipeline` must call both
`_validate_commit_authorization` and `commit_release`; this is the coordinated
publication sink protected by the existing owner-publication gate, not the
release builder's private-stage `CURRENT` work. For report and panel routing,
`build_report` must call `build_report_candidate`; that candidate must call
`_tool("portfolio_report.py", "build", ...)`; `build_report` must call report
`verify` and `publish`; and `build_panel_candidate` must call
`_tool("portfolio_control_panel.py", "build", ...)`. A matching string literal
or a call to `str` is not an edge. Every runtime guarantee points to a real
integration-test path in the graph. Missing source bytes, changed declarations,
missing static symbols/calls, an omitted bound file, or a stale fingerprint are
validation failures.

The same source-bound projection carries maintained `artifact_flows`, not a
separate mapper or diagram. Each flow has one native producer, its output
inventory, bound contracts and source hashes, consumers, native validators,
writers, linked tests, downstream stages and terminal sink(s). Layered history,
dependence, plan, ledger, ledger freshness, baseline, epoch manifest, HWM,
capital, attribution, decomposition, report and panel artifacts remain separate
records. The ledger freshness flow binds `build_ledger`'s literal
`portfolio_freshness.py` command, `write_health`/`build_health`, its
`freshness-health.json` output, and the report/panel freshness consumers. Its
provider-ledger input contract is bound; the existing core freshness schema is
not listed as the output contract because it declares schema version 2 while
the native producer and consumers use version 3. Report and panel candidates
are distinct producer records, and `publish_catalog`,
`publish_report`, and `publish_panel` are writers rather than relabelled
validators. The actual delivery change range is resolved through these records,
so an unlisted relevant producer, contract or consumer fails reconciliation
rather than disappearing. The publication-pointer record documents the existing
owner-publication gate; it does not create a new publication requirement.
Workbook is an explicit excluded sink and cannot become a required edge.

The graph is deliberately not an execution authority.  AST cannot prove dynamic
runtime ordering, source admission/finality, recovery/reuse behavior, or
financial numerics.  Those guarantees remain with the supported attended
runner and its integration tests.  The gate neither replaces nor invokes them.

The graph never probes an ancestor for task-specific mapper snapshots. An
optional mapper artifact is accepted only through `--mapper-evidence <path>`.
It must name this exact repository root and package, have no scanner errors,
and carry the current installed-mapper Python fingerprint: the SHA-256 of
sorted `relative_path:raw_sha256` lines for every scanned Python file in its
declared package (currently `tools`). The mapper's internal cache `files`
dictionary is not required or consumed. Accepted mapper
evidence is contextual only; the standard-library executable-contract gate does
not need it in CI.

Run the gate locally without executing financial code:

```powershell
python tools/portfolio_controller_graph.py --repo-root .
python tools/portfolio_controller_graph.py --repo-root . --write .pt\tmp\controller-graph.json --changed-path tools/portfolio_report.py
python tools/portfolio_controller_graph.py --repo-root . --changed-from <base-sha> --changed-to <candidate-sha>
```

The second form writes the supplied task-local graph output and reports the
declared downstream impact. The third reads the actual Git range; Quality uses
the pull request's base SHA, the push's `before` SHA, or `EMPTY_TREE` for a
root commit/workflow dispatch. A report change affects report and panel; a
panel-only change affects panel. Preparation, controller, the financial-source
contract, and its JSON schema affect their downstream stages. An unmapped
relevant production or portfolio-schema path raises a reconciliation error;
unrelated documentation/test paths return no execution-stage impact. The
Quality workflow invokes the gate with its installed CI environment, without a
workstation path or an optional mapper artifact.
