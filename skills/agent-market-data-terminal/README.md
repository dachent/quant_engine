# Agent Market Data Terminal (aMDT)

Agent Market Data Terminal (aMDT) is a lightweight LLM skill built to give both autonomous agents and cloud chatbots direct access to market and macro data for research and backtesting rule-based trading theories. The repo is intentionally designed around public endpoints that do not require API keys or authentication, so the skill stays easy to run and easy to distribute.

## Catalog index

A flattened single-file index of this catalog is maintained as a public gist:
[gist.github.com/dachent/a80848d89966daa6730cd553be691b1a](https://gist.github.com/dachent/a80848d89966daa6730cd553be691b1a). Use it when you want every provider, endpoint, and parameter in one document; use this repository when you want per-provider reference docs and runnable fetch helpers.

`agent-market-data-terminal` is the skill packaged by this repository, and it pulls market and macro data from:

- BEA public files and `Data.json`
- Treasury FiscalData
- Treasury daily rates feeds
- BLS public v1
- World Bank
- OECD
- Kenneth French Data Library via direct Fama/French CSV zips
- FRED
- VIXCentral
- CBOE
- `dougransom/vix_utils`
- Macrotrends
- Westmetall
- EIA DNAV
- Multpl
- Yahoo Finance via direct `yfinance`
- Federal Reserve Data Download Program (H.10 FX, H.15 rates, and other DDP releases)
- CFTC Commitments of Traders (Legacy / Disaggregated / TFF / Combined / Supplemental families)
- Foreign central-bank yield archives (Bundesbank, Bank of England, Reserve Bank of Australia, Bank of Canada, Bank of Japan)
- USDA Agricultural Marketing Service (Market News REST + `/public_data` bulk archives)
- ICCO daily cocoa indicator
- ICO coffee composite indicator
- ICE marketdata report scraper (sugar / cotton settlements)
- World Bank Pink Sheet (Commodity Markets Outlook XLSX)
- IMF Primary Commodity Prices (External_Data.xls)
- iShares ETF universe and holdings (current and as-of-date history)

The skill is native-first. It prefers direct machine-readable provider sources such as BEA public files, Treasury FiscalData JSON, Treasury rate feeds, BLS v1 JSON, World Bank indicator JSON, OECD SDMX-JSON, Kenneth French `_CSV.zip` feeds, FRED CSV, Macrotrends JSON, Westmetall chart XML, linked EIA XLS workbooks for history pages, VIXCentral XHR-style routes, CBOE CSV endpoints, `vix_utils`' CBOE-backed history loaders, and Multpl's inline chart payload.

## Install

### Codex

Clone or copy this repository into your Codex skills directory as:

```text
$CODEX_HOME/skills/agent-market-data-terminal
```

After installation, Codex can use the skill as `$agent-market-data-terminal`.

### Claude Code

Clone or copy this repository into your Claude Code skills directory.

For a user-global install (available in all projects):

```text
~/.claude/skills/agent-market-data-terminal/
```

For a project-local install (available only in a specific project):

```text
.claude/skills/agent-market-data-terminal/
```

After installation, Claude Code can use the skill as `/agent-market-data-terminal`.

Claude Code also reads `CLAUDE.md` automatically when you open this repository as a project directory, providing layout and routing context without an explicit invocation.

## Layout

- [SKILL.md](./SKILL.md): trigger metadata and workflow (shared by Codex and Claude Code)
- [agents/openai.yaml](./agents/openai.yaml): Codex UI metadata
- [agents/claude.md](./agents/claude.md): Claude Code metadata
- [CLAUDE.md](./CLAUDE.md): Claude Code auto-loaded project context
- [references/](./references/): provider-specific guidance and source contracts
- [scripts/](./scripts/): fetch helpers for each provider

## Notes

- EIA history parsing prefers linked `.xls` workbooks and uses Python-native `xlrd` for structured history extraction instead of Windows-only COM automation.
- VIXCentral no longer depends on Playwright. The helper uses direct requests first, then a homepage-primed `requests.Session()`, then optional `curl_cffi` impersonation if needed.
- BEA is implemented as a no-auth file/catalog source. It uses direct public files, BEA release pages, and `https://apps.bea.gov/Data.json` instead of the key-based API.
- Treasury yield-curve and bill-rate data come from Treasury's official TextView-linked CSV/XML feeds, not FiscalData.
- Fama/French support is structured-feed only. The helper accepts direct Kenneth French `_CSV.zip` feeds and direct archive zip URLs, and rejects HTML page scraping.
- Westmetall chart parsing is XML-first. Raw chart fetches remain XML-only, while parsed chart mode can fall back to the daily HTML table if the XML endpoint is temporarily unavailable.
- `vix_utils` is exposed as a separate historical provider and keeps its cache inside a workspace-local directory when used through the bundled helper.
- The Yahoo path uses direct `yfinance` inside this repo and keeps only the lightweight `history` and `current_snapshot` datasets.
- Federal Reserve DDP fetches the all-release SDMX-XML ZIP for full history; the current-release HTML page is for the latest weekly print only.
- CFTC pulls compressed annual ZIP archives from `/files/dea/history/` per report family rather than the per-week pages.
- The `central_banks` provider wraps five foreign central-bank archives behind one CLI (`--source bundesbank|boe|rba|boc|boj`). BoJ is intentionally limited — its Time-Series Data Search is fragile, so for 10Y JGB FRED `IRLTLT01JPM156N` is the recommended fallback.
- USDA AMS ships with a curated commodity-to-slug map (corn, wheat, soybeans, soymeal, soyoil, live cattle, lean hogs, cotton) and a `--bulk` mode that indexes the public `/public_data` ZIP archive directory for backfill.
- ICCO and ICO have moved most historical bulk data behind paid subscriptions. The bundled helpers parse the public homepage and statistics-page tables for current values and document the paywalled channels; for monthly bulk history, use World Bank Pink Sheet or IMF Primary Commodity Prices.
- `worldbank_pinksheet` is intentionally a separate provider from `worldbank` — Pink Sheet is a multi-sheet XLSX workbook (CMO release), while `worldbank` targets the WDI indicator JSON API.
- iShares uses the public AJAX holdings endpoint and follows polite request rules (single user-agent, no concurrency, conservative throttle). The provider was added in response to issue #5; endpoint discovery was informed by the public `talsan/ishares` repository (no license declared, used for reference only — no code vendored).

## Optional Python dependencies

Some helpers depend on lightweight optional packages:

- `openpyxl` for parsed BEA, RBA F2, and World Bank Pink Sheet `.xlsx` workbooks
- `pandas` for the direct Yahoo helper and some dataframe-backed providers
- `yfinance` for direct Yahoo Finance access
- `xlrd` for EIA and IMF Primary Commodity Prices `.xls` history workbook parsing
- `curl_cffi` for the optional VIXCentral TLS-impersonation fallback
- `vix_utils` for the standalone historical VIX term-structure provider
- `lxml` (optional) for faster SDMX-XML parsing of Federal Reserve DDP archives

The repo keeps imports lazy or guarded so syntax validation still passes even when these packages are not installed.

## Validation

This repository includes a lightweight GitHub Actions workflow that runs Python syntax checks over the bundled scripts.
