# Strategy version authority

Display names and provider labels are not strategy-version identity. Every
retained strategy registry record receives an explicit version disposition.
Seven records are REQUIRED active current strategies; Echo is retained as
`DEPRECATED_HISTORY_ONLY`, and Composer is retained as `IMMATERIAL_EXCLUDED`:

- `EVIDENCED` binds an effective interval, design artifact, provider entity and
  configuration, assumption version, cost version, and applicable backtest;
- `REFERENCE_ONLY` preserves a known provider or research version reference but
  records the missing proof; and
- `UNRESOLVED` keeps the version reference null and requires explicit gaps.

The private provenance builder now emits a non-null disposition for every one
of the nine retained strategy registry records. Existing null references
migrate to `UNRESOLVED`; they are never converted into invented version
identifiers.
`StrategyVersion` is the effective-dated canonical contract for later evidence.

`StrategyEvidenceVersion` binds each account-live, provider-live, official
vehicle, predecessor, shadow/OOS, or backtest series to one strategy and one
version reference. `compare_strategy_evidence` permits a model-versus-actual
comparison only when the strategy and version match and the layers form one
model/one live pair. It permits concatenation only within the same layer and
same version. Version mismatch or unresolved version is a visible, fail-closed
classification.

Real design, configuration, backtest, assumption, cost, and provider identity
artifacts remain in the governed local data boundary. Only generic contracts,
schemas, and synthetic tests belong in Git.
