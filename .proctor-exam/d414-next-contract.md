# D414 attended implementation and acceptance boundary

Read-only review of repo HEAD `fc52383d6f12d38a78100cb1641afb96f5a5e148`, FINAL-PLAN v8 section D and IMPLEMENTATION-PACKETS section D. This refines the earlier attended proposals; neither `verify_attended.py` nor its deliberate terminal assertion is an acceptance authority. No new financial profile is proposed.

## Smallest deliverable

Add `attended` to the existing `tools/portfolio_pipeline_materialize.py::main` with the approved options:

```text
attended --repo-root REPO --python PYTHON --workspace-root WORKSPACE
  --output-root FRESH_RUN --retained-cache-root CACHE
  --requested-as-of YYYY-MM-DD --source-run CAPTURED_SELECTION
  --workbook-runtime DESCRIPTOR [--resume] [--execute]
```

Without execute, validate available selected inputs and emit the planned graph/view; no fitting, replay, Office, browser, or publication. With execute, invoke the existing stages in dependency order. A required missing source or financial prerequisite is a diagnosed dependent stop, not an accepted completion. Resume validates the same request and completed artifacts before skipping work. No execute/resume path calls publication/commit. `workspace-root/dataPortfolio` is the only portfolio root; no inference from output/cache roots.

No provider acquisition is implied by this command contract. Its initial scope consumes already captured, genuinely admitted sources. It preserves requested date separately from each source cutoff and model publication boundary. September19 acquisition, September18 requested financial date, currently older provider finality, and July31 French factor boundary are different facts. Do not advance any native row or finality timestamp to satisfy a CLI request.

## Concrete selection interface

Retain the earlier optional `source-run.attended` extension version1 and the existing FILE/DIRECTORY definition shapes (`_file_definition`, `_directory_definition`, lines631/636). Every selected artifact is hash/inventory bound; no expected financial outputs or caller-supplied PASS/readiness allowed.

| Extension member | Meaning and authoritative validation |
|---|---|
| `schema_version: 1` | Version of this selection extension. |
| `release_recipe` | FILE definition of an existing schema10 **release-builder input recipe**. Actual `portfolio_release_builder._recipe` must accept it. Both mode selections and its existing verified epoch generation remain bound there. |
| `history_generation`, `allocation_generation` | Existing DIRECTORY definitions, consistent with the actual selected dependence source. Use the accepted history/dependence readers after H3 integration; no second independent selectable identity when the recipe already names one. |
| `financial_input_contract` | FILE definition of the exact contract used for provider admission; actual materializer `_provider_admission_authority` must reconstruct PASS/READY_FOR_COMPOSITION. It is not automatically the financial product contract. |
| `dependence_authority` | The explicit existing-authority routing described in attended-schema10-authority-routing-gap.md: FILE definitions for financial_product_contract, strategy_registry, financial_product_schema, dependence_policy. Bind the policy-relative schema too. The epoch root/id comes only from the recipe's existing epoch selection. Defaults remain for legacy calls; an explicitly selected authority must never silently fall back. Reconcile with H3's delivered interface rather than implementing duplicate loaders. |
| `required_stages` | Fixed sequence ledger, baseline, attribution, analytics, report, workbook, panel-candidate, panel. This is a consistency assertion, not permission to omit panel. |
| `predecessor_spec` | FILE definition of predecessor PipelineSpec or null only when there is no committed predecessor. Read and verify its committed release with its own recorded roots/code. Null cannot hide an existing predecessor. |

Existing governed HWM/capital selections, when required by the real recipe, must be captured as FILE definitions and forwarded using the existing argument families, without creating a new policy: hwm_governed_result, hwm_governed_input_generation, hwm_governed_calendar_authority, hwm_governed_admission_receipt, invested_capital_result, invested_capital_input_generation. Families stay all-or-none under current validators. Do not invent expected results to fill them.

The workbook descriptor remains the explicit command argument, validated by accepted `_resolve_workbook_runtime`; bind its resolved executable/helper/dependency identity in execution/resume evidence only. It must not enter financial or report content identity.

## Remove the circular dependency, preserve both receipt types

The existing `portfolio_release_builder.create_recipe` at1085 creates schema10 from retained inputs. It does **not** require a completed new report/workbook/panel. Existing `build_all` at6594 runs ledger through panel-candidate and returns `PANEL_CANDIDATE_REQUIRES_BROWSER_QA` at6740. Existing `build_stage(..., role='panel', qa_dir=...)` is the later actual QA-bound completion seam.

In contrast, materializer `create_recipe` at2869 reads every proof stage result, expected generation inventory, panel launcher and QA directory. It is necessarily **post-build**. Its `build_spec` at3705 accepts that proof-bound schema3 recipe and requires a clean repository at its commit. Its `acquire`/`authority` receipt pair (3035/3041) derives from that recipe and enforces exact equality.

Therefore initial attended execution must use the schema10 input recipe first. Parent can create it with existing producer CLIs once native prerequisite selections exist; D is not required to create those inputs. D then executes the existing release stages; materializer post-proof recipe/spec generation happens only after actual stages and QA are complete. A first run with pending desktop/browser/source prerequisites must retain a truthful pending state and an incomplete completed-node list, not fail because future proof files were demanded as initial inputs and not label the run complete.

**Correction to the earlier v2 proposal:** do not weaken legacy `authority()` equality or treat the initial input-selection receipt as the later materializer proof receipt. The attended loader validates its versioned extension and input inventory itself using existing readers. The later legacy acquire/authority sequence creates its ordinary distinct receipt from actual completed evidence. Record both hashes in the run view. Never manufacture expected_generations, QA manifests, or the post-proof source-run merely to bootstrap D.

QA output is a planned run location until actual evidence exists; it becomes completed only after its existing validator passes. Immutable initial selection/resume identity must not change merely because an expected QA output has now been produced. Existing source bytes changing is a different run and requires a new selection.

## Actual seams and necessary limited changes

- materializer `_parser`4742 / `main`4832: public attended surface; early argument/root checks and execution switch.
- Existing file/directory validators at640/747 and `_provider_admission_authority`1089: input integrity and genuine admission, not invented acceptance.
- release-builder `_recipe`884, `create_recipe`1085, `build_stage`6297: retained input validation and stage calls; preserve explicit portfolio/runtime/cache/financial arguments.
- release-builder `_history_contract_path`811 still hardcodes repository contract/policy at review time. H3 is working on current-selection admission. D must route the accepted selected authority all the way through this consumer and later report/workbook/panel revalidation if that seam remains after H3; do not rebuild another verifier.
- materializer `build_spec`3741 selects its runtime from QE_PIPELINE_PYTHON/sys.executable. Prefer a backward-compatible explicit Python-path argument supplied by attended, forwarded into `_command`3641. Do not persist a global environment change to implement `--python`.
- `portfolio_qc._read_committed_release`639 / `_verify_committed_manifest`655: predecessor authority, including actual pointer/root/code binding. This check precedes expensive work, with existing publication race checks retained for any later separately authorized commit.
- Generated run view derives from selections, existing PipelineSpec when available, actual receipts and counters. Initially it may lack a post-proof PipelineSpec; state that explicitly. Planned stages and verified completed stages are distinct. No sync_contract_pins execution or copied pin authority.

## Executable acceptance requirements

1. **Isolated CLI/constructor component, executable now.** `d414-next-routing-skeleton.py CHECKOUT` invokes actual public main(attended --help) and the existing `_command` in two root layouts. It requires all approved flags including workbook runtime and confirms explicit Python/root argv construction. No mocked validator or synthetic authority is used. Baseline passes constructor controls then exits1 `RED_MISSING_ATTENDED_SUBCOMMAND`. A future PASS is only the CLI surface and constructor; it does not prove actual-main forwarding/preflight. No full suite was run.

2. **Actual-main forwarding/preflight once a real recipe is retained.** Freeze native schema10 recipe, current admitted provider generation/receipt, history/dependence/allocation, epoch, financial authorities, runtime descriptor and source-run extension under parent hashes. Independently run their existing readers first. Invoke main with all explicit args; instrument only the chosen existing expensive execution boundary after preflight. The valid case reaches it once, records the actual args/kwargs and returns a named nonzero child result. Assert exact requested date, selected recipe/source identities, explicit Python, workspace portfolio root, output/cache/runtime routing, and stop without downstream execution. Planning without execute reaches no expensive child. This is routing proof, not a successful financial run. Do not patch `_recipe`, `_history_contract_path`, load_contract, admission/readiness or source validators.

3. **One-mutation negatives of that same valid selection.** Missing/invalid schema; missing source/receipt; changed source/contract bytes; valid producerB bound asA; wrong epoch binding; occupied create-only output; missing/invalid runtime; invalid predecessor and hidden predecessor via null; omitted panel; different resume requested date/source hash/runtime identity; corrupt completed artifact; nonzero child. Reject before the affected expensive stage, preserve sentinels/pointers, and name the actual failed boundary. Earlier independently completed stages may remain retained. First-child failure must yield exactly one call. Do not claim every resumed graph error must erase unaffected work.

4. **Full native acceptance remains separate.** Clean committed candidate; actual all7/13 and both modes; independently verified native financial outcomes; current-source comparison; native workbook and actual panel QA; unchanged warm run and second-root reuse counters, controlled delta, resume and no publication. The existing synthetic source-admission fixture is only a component control; its narrowed authority, recovery schema2 fixture, and patched 172 fixture cannot substitute for this native profile. Desktop Active/browser authorization and provider/FX freshness still bound the final run.

These checks do not require the full native recipe to implement CLI/root validation, command construction, planned/completed state handling and existing boundary forwarding. They do require it before claiming complete attended source/preflight integration or financial acceptance. Missing native inputs are exact dependencies, not a reason to create a second financial fixture framework.

## Reproduction and stop

```powershell
& .codex-bootstrap/runtime/venv/Scripts/python.exe -I -B .pt/control/d414-next-routing-skeleton.py repo
```

Observed constructor PASS then missing-command RED is retained in `d414-next-routing-baseline.log`. Earlier attended drafts and native sources were not modified. This review creates only `.pt/control/d414-next-*` artifacts, with no production edits, Office/browser/network, heavy tests, new agents or global changes. Parent seals the real selection and final main-entrypoint exam after native dependency integration; this document is task decomposition, not acceptance.
