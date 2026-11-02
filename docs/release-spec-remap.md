# Predecessor spec remap (RELEASE-DRIVER-001, block B1b)

`tools/release_ledger_spec.py build --predecessor-spec` carries `strategy_inputs`,
`strategy_provider_map` and `portfolio_fx_rates` of the predecessor release's ledger spec as data.
The raw spec names the predecessor's world (its scratch roots and worktrees), so it is remapped
first by `tools/release_spec_remap.py`, which replaces the `remap()` hand function of the R2
cycle. The output is passed to the builder unchanged.

```text
release_spec_remap.py remap --manifest cycle-manifest.json --root KEY=DIR ... [--roots-file F]
                            --mapping spec-remap.json --spec predecessor-ledger-spec.json
                            --out predecessor-remapped.json [--carried-only] [--plan]
```

The roots are bound exactly as for `release_cycle_manifest.py` and only the root keys the manifest
uses are declared. The mapping file says where the predecessor's paths came from:

```json
{"schema_version": "qe-release-spec-remap/1",
 "mappings": [{"old_prefix": "<predecessor directory>", "root_key": "data", "relative": "sub/dir"}]}
```

A path under `old_prefix` (either slash style, any drive-letter case, whole segments only) becomes
`<root>/<relative>/<rest>` with `/` separators. A path already under a declared root is kept.

The step refuses (exit 1, `CODE: detail`, nothing written) when any absolute path, also one embedded
in a longer string, remains outside the declared roots (`FOREIGN_PATH`, all offenders listed); when
two old prefixes are equal or nested, or an old prefix overlaps a declared root (`AMBIGUOUS_PREFIX`);
when a mapping names a root key the manifest does not declare (`UNDECLARED_ROOT`) or leaves its root;
and when a mapping target, or a remapped path under a carried key, does not exist (`MISSING_TARGET`).
`--carried-only` drops the blocks the builder regenerates (paths of the old repository checkout,
the old ledger output, the old provider roots) before the checks, so only the carried keys need
mapping. The output is create-only.
