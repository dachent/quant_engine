# How each financial outcome is verified, and why they are not verified the same way

`verify_outcome_inventory` (`tools/portfolio_financial_outcomes.py`) requires every REQUIRED
financial outcome's artifact to resolve to a file **inside one of its `depends_on` generations**,
matching sha256 and size, with exactly one match. Two outcomes are exempt from that containment
check. This document records what each outcome actually gets, why the exemptions exist, and — the
part that is easy to get wrong — why the two exempt outcomes are compensated *differently*.

Written because the exemption for `INVESTED_CAPITAL` was first justified as "the same as the HWM
precedent", and that justification was wrong. The fix survived; the reasoning did not.

## The four outcomes

| Outcome | Contained in a generation? | What verifies it instead |
|---|---|---|
| `DECOMPOSITION` | yes — lens `decomposition.json` | containment + consumer parity |
| `DEPENDENCE` | yes — `correlation.json` | containment + consumer parity |
| `AS_HELD_COMPOSITE_HWM` | **no** | cross-declaration binding to the HWM authority gate |
| `INVESTED_CAPITAL` | **no** | re-derivation from a pinned, ledger-bound input |

`DECOMPOSITION` and `DEPENDENCE` are ordinary: their artifacts are published inside release
generations, so containment applies and nothing special is needed.

## AS_HELD_COMPOSITE_HWM — compensated by a cross-declaration binding

HWM's governed result is produced by a separate governed pipeline and is declared **twice**, by
two different producers:

* the production inventory names a `financial-outcome-hwm` artifact node;
* `_hwm_input_authority_assessment` independently records, in the
  `financial-mvp-hwm-input-authority` gate, which governed result the HWM assessment verified.

Because there are two independent declarations, they can disagree, and a disagreement is a real
defect: the release would ship a result the assessment never examined. So the verifier requires
them to agree, on both digest and path:

```python
hwm_node = nodes["financial-outcome-hwm"]
if (
    hwm_node.sha256 != bindings["governed_result"]["sha256"]
    or hwm_node.path.resolve() != Path(bindings["governed_result"]["path"]).resolve()
):
    raise FinancialOutcomeError("HWM outcome and assessment refer to different results")
```

That binding is worth having **because the two sides are produced independently.** It is a
consistency check between producers, not a check of the value itself.

## INVESTED_CAPITAL — compensated by re-derivation, not by a second declaration

### Why containment is impossible, not merely inconvenient

`invested-capital.json` reached the published report generation through exactly one gate:

```
tools/portfolio_report.py:11002   if "invested_capital_result" in evidence_snapshots:   # registers it
tools/portfolio_report.py:11201   same guard                                            # writes the file
```

and that key is populated in only two places, `:9547` (`bind_workbook_evidence`) and `:9693`
(`_validated_workbook_evidence_snapshots`) — both on the **workbook route**. Commit `47120f1`
("gate source phase and remove workbook runtime") removed the workbook runtime under the owner's
permanent hold and severed both feeders. On the current path `evidence_snapshots` is built from
`report_data` plus epoch-manifest snapshots only, under an explicit comment:

> *"Current authority is canonical report data. Do not create or infer an XLSX/evidence
> substitute when the held workbook route is absent."*

Measured: `invested-capital.json` is absent from `stages/report/generations/*/` in every completed
run on this workstation, and present under `stages/report/candidates/` — written to the candidate,
never promoted. So the containment requirement was unsatisfiable by construction, and the failure
it produced was not even the containment error: `build_financial_artifacts.py` hashes each row's
file at assembly, so the sequence died earlier on a raw `FileNotFoundError`.

### Why a HWM-style gate would be ceremony

The obvious symmetry — "give capital a gate too, then bind the node to it" — does not work, and
the reason is worth stating because it is the difference between a check and a ritual.

HWM's binding has value because the gate is written by an **independent** producer. Capital has no
such producer. The capital result path reaches the pipeline as a CLI argument threaded through
`_governed_stage_paths` (`tools/portfolio_pipeline_materialize.py`), by the same step that builds
the inventory row. A capital gate would therefore record the value it was handed, and comparing
the inventory row against it would compare a value with its own source. It cannot fail for any
reason the existing checks do not already catch. That is added plumbing with no detection power.

More importantly, capital already has something **stronger than a second declaration**: the
verifier does not consult an assessment, it **recomputes the result**.

```python
capital_node = nodes["financial-outcome-capital"]      # the outcome's own bytes
capital = json.loads(capital_node.path.read_bytes())
capital_input = capital["input_generation"]            # identity declared inside the payload
capital_input_node = _artifact_for_digest(spec, capital_input["sha256"], capital_input["size_bytes"])
verify_selected_capital_ledger(capital_input_node.path, selected_sources["provider_ledger_generation_path"])
capital = verify_governed_capital_result(capital, capital_input_node.path, contract_path)
```

The outcome's own bytes must re-derive from an input that is resolved **by digest** out of the
specification and **bound to the provider ledger**. Agreement between two declarations proves they
match each other; re-derivation proves the number is correct. The latter subsumes the former.

### What containment did provide, and what replaced it

Containment tied the result **artifact** to a release **generation** — provenance, not
correctness. Dropping it loses that tie, and honesty requires saying so rather than claiming
equivalence with HWM.

It is replaced by a pin that the code did not previously have. `_artifact_for_digest` matches on
`sha256 + size` across every `ArtifactNode` and never checks which node it found, so the capital
result's declared input could be satisfied by *any* artifact carrying those bytes. The verifier now
requires it to be the declared capital input row:

```python
if capital_input_node.node_id != "financial-capital-input":
    raise FinancialOutcomeError("selected capital input is not the declared capital input artifact")
```

Net: `artifact → generation` containment is traded for `result → declared capital input` identity
plus the re-derivation chain. Not identical to what was removed. Not nothing. Stated plainly so the
next reader does not have to reconstruct it.

## The rule this encodes

A verification mechanism is worth adding when the two things it compares are produced
**independently**. When one side is derived from the other, the comparison is ceremony. When the
value can be **recomputed from pinned inputs**, recomputation is stronger than any number of
agreeing declarations, and the remaining question is only provenance — which should be secured by
the cheapest binding that is not circular.

## Related

* `docs/production-replay-seam.md` — why the production replay must call a producer the way the producer was called, and the four blockers that hid behind one omission.
* `docs/controller-graph.md` — why edge connectivity does not establish value semantics.
* `AGENTS.md` A1.3 — a changed output contract must enumerate and update its in-repo consumers in
  the same change. The workbook removal did not, which is how this defect was created.
