# FRED

Prefer the clean CSV endpoint:

```text
https://fred.stlouisfed.org/graph/fredgraph.csv?id={SERIES_ID}&cosd={START_DATE}&coed={END_DATE}
```

Required parameters:

- `id`: FRED series id such as `GS10`, `VIXCLS`, `WTISPLC`
- `cosd`: start date in `YYYY-MM-DD`
- `coed`: end date in `YYYY-MM-DD`

Optional data-shaping parameters:

- `transformation`: `lin`, `pch`, `pc1`, `log`
- `fq`: `Monthly`, `Quarterly`, `Annual`
- `fam`: `avg`, `sum`, `eop`
- `vintage_date`: `YYYY-MM-DD`

UI-only parameters should be stripped and never drive automation, including:

- `bgcolor`
- `chart_type`
- `drp`
- `fo`
- `graph_bgcolor`
- `height`
- `link_values`
- `line_color`
- `line_index`
- `line_style`
- `lw`
- `mark_type`
- `mode`
- `mma`
- `mw`
- `nd`
- `nt`
- `oet`
- `ost`
- `revision_date`
- `scale`
- `show_axis_titles`
- `show_legend`
- `show_tooltip`
- `thu`
- `trc`
- `ts`
- `tts`
- `txtcolor`
- `width`

Common examples:

```text
https://fred.stlouisfed.org/graph/fredgraph.csv?id=GS10&cosd=1953-04-01&coed=2026-04-01
https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS&cosd=1990-01-02&coed=2026-04-01
```

Native output is CSV with an `observation_date` column and one series column.
