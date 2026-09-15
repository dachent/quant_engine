# Preserved capture outputs

Non-HAR outputs of the two Portfolio123 capture workspaces that existed only in
`C:\Dev\scratch\quant-engine`, recovered on 2026-09-15 before that tree was deleted.

* `p123-union/` — the 2026-09-02 capture: extracted series CSVs, the files the session
  downloaded directly, the discovery call list, the strategy name map, and the shared
  strategy id list.
* `p123-refresh-20260907/` — the 2026-09-07 refresh, same shape.

The HAR files themselves are **not** here. They are 98% JavaScript, CSS, fonts and
third-party tracker payload — see [#422](https://github.com/dachent/quant_engine/issues/422)
for the measurement and the two-mode fix. The one HAR that is evidence rather than bulk,
`strategy_1853549_20260726_230951.har` (38.7 MB), stays where it already is under
`dataPortfolio123/quarantine/plans/8f786fea…/objects/har-workspace/json/`, because
[#412](https://github.com/dachent/quant_engine/issues/412) needs it: 1,424 of 1,598
overlapping daily ratios for that entity disagree between the HAR-derived and API-derived
series, and nothing adjudicates which is true. The extracted side of that comparison is
`p123-union/csv/strategy_1853549_perf.csv` and
`p123-union/downloads/strategy_1853549_*.csv`, here.

Everything was preserved rather than selected. Choosing which of these mattered would have
been the same recall-based judgement that produced the sprawl in
[#421](https://github.com/dachent/quant_engine/issues/421); 17 MB is cheaper than being wrong
about it.
