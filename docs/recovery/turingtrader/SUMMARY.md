# TuringTrader HAR Contract — Summary

## Deliverable

A comprehensive `ProviderContract` for turingtrader.com in the
`quant_engine/packages/web-contracts` framework, proven by real authenticated
HAR captures.

## What was built

### Contract module: `turingtrader.py`

**9 read-only endpoints** across 2 origins:

| Capability | Origin | Endpoint | Auth |
|---|---|---|---|
| `strategy.performance-chart` | Firebase Storage | `{slug}%2Fperformance-chart.json?alt=media&token=*` | None (public token) |
| `strategy.performance-table` | Firebase Storage | `{slug}%2Fperformance-table.json?alt=media&token=*` | None |
| `strategy.holdings-history` | Firebase Storage | `{slug}%2Fholdings-history.json?alt=media&token=*` | None |
| `strategy.portfolio-holdings` | Firebase Storage | `{slug}%2Fportfolio-holdings.json?alt=media&token=*` | None |
| `strategy.last-rebalance` | Firebase Storage | `{slug}%2Flast-rebalance.txt?alt=media&token=*` | None |
| `strategy.last-update` | Firebase Storage | `{slug}%2Flast-update.txt?alt=media&token=*` | None |
| `strategy.market-vane-status` | Firebase Storage | `tt-market-vane%2Fstatus.json?alt=media&token=*` | None |
| `strategies.catalog` | Firebase Storage | `portfolio-comp.json?alt=media&token=*` | None |
| `account.profile` | Outseta | `turingtrader.outseta.com/api/v1/profile?fields=*` | Cookie |

### Custom extraction: `extract_turingtrader_har`

Filters out:
- Firebase metadata requests (no `alt=media`)
- Firebase image assets (`.jpg`)
- Google Analytics, Google Tag Manager
- WordPress static assets (JS, CSS, fonts, images)
- Outseta preflight (`OPTIONS`)
- Outseta profile write (`PUT`)
- Outseta config (`config.outseta.com`)
- Entries with empty response bodies

### Sensitive fields

Outseta PII: `email`, `firstname`, `lastname`, `phonemobile`, `phonework`,
`mailingaddress`, `ipaddress`, `referer`, `profileimages3url`

### Tests: 16 test cases (all passing)

- Contract structure (provider name, endpoints, read-only, no fallback, sensitive fields)
- Deterministic extraction + redaction
- Noise disposal (metadata, images, analytics, WordPress, preflight, PUT, config)
- Empty body filtering
- Fail-closed: expired session, unknown route, non-success, missing alt=media, missing auth
- Endpoint matching: catalog, market-vane status, text endpoints

## HAR capture details

- **Date:** 2026-08-02
- **Method:** CDP navigation capture via managed Edge browser (port 9222)
- **Pages captured:** 25 (portfolios catalog, dashboards, 19 strategy pages)
- **Total entries:** 2,048
- **With response body:** 1,751
- **JSON responses:** 737
- **Unique Firebase alt=media endpoints:** 386 (19 slugs × ~10 endpoint types + catalog)
- **Strategy slugs covered:** 19 of 121 (all 6 live + 13 non-live)
- **HAR file:** `har/raw/turingtrader_nav_20260802_133725.har`

## Architecture discovered

TuringTrader is a **server-rendered WordPress site** with a **Firebase Storage**
data backend. The WordPress plugin (`turingtrader-wp`) embeds JavaScript blocks
that fetch strategy data from Firebase Storage using public download tokens.
**Outseta** handles membership/auth (user profile, subscription management).

Key finding: the Firebase JSON endpoints mentioned in the user's Excel workbook
(`portfolio-comp`, `performance-chart`, `performance-table`, `holdings-history`,
`portfolio-holdings`) are the same endpoints the website frontend calls —
confirming the workbook's data pipeline matches the website's data source.

## Files

### In quant_engine worktree (`C:\Dev\gitdirs\quant_engine-turingtrader`)

| File | Status | Purpose |
|---|---|---|
| `packages/web-contracts/src/quant_engine/web_contracts/turingtrader.py` | New | Contract module |
| `packages/web-contracts/src/quant_engine/web_contracts/__init__.py` | Modified | Export new symbols |
| `packages/web-contracts/src/quant_engine/web_contracts/provenance.py` | Modified | Add source anchor |
| `tests/web_contracts/test_turingtrader_contract.py` | New | 16 tests |
| `tests/web_contracts/test_schwab_contract.py` | Modified | Update anchor count |
| `docs/providers/turingtrader.md` | New | Provider documentation |

### In har_turingtrader workspace

| File | Purpose |
|---|---|
| `har/raw/turingtrader_nav_20260802_133725.har` | Raw HAR capture |
| `har/raw/cdp_nav_capture_tt.py` | CDP capture script |
| `har/raw/cdp_capture_tt.py` | Passive CDP capture script |
| `har/raw/target_pages.txt` | 84 target URLs |
| `analysis/endpoint-catalog.md` | Endpoint discovery analysis |
| `RUNBOOK.md` | Capture runbook |
| `SUMMARY.md` | This file |

## Test results

```
41 passed in 0.17s
```

All 16 TuringTrader tests + 25 existing tests (Schwab, Quantbase, Portfolio123) pass.
Ruff lint: clean.

## Coverage assessment

### Covered
- 9 read-only data endpoints (7 Firebase + 1 catalog + 1 Outseta profile)
- 19 strategy slugs (all 6 live + 13 non-live)
- Full 121-strategy catalog (`portfolio-comp.json`)
- Market Vane signal status
- User profile (Outseta)
- Noise filtering for all known non-data traffic

### Not covered (future work)
- Remaining 102 strategy slugs (add URLs to `target_pages.txt` and re-capture)
- Private strategies (`priv-*`) — accessible if account has permission
- Dashboard rebalance page data (may have additional endpoints)
- WordPress REST API (`/wp-json/`) — not observed in capture, may exist
- Outseta subscription/billing endpoints — not captured (write operations excluded)
