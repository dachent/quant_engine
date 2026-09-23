---
display_name: "Agent Market Data Terminal (aMDT)"
short_description: "Lightweight no-auth market and macro data for Claude Code agents"
---

# Agent Market Data Terminal (aMDT)

## Claude Code metadata

- **Invocation**: `/agent-market-data-terminal`
- **Skill file**: `SKILL.md` (shared with Codex)
- **User-global install**: `~/.claude/skills/agent-market-data-terminal/`
- **Project-local install**: `.claude/skills/agent-market-data-terminal/`

## Default prompt

Use /agent-market-data-terminal to fetch BEA, Treasury FiscalData, Treasury rates, BLS, World Bank, OECD, Kenneth French Data Library, FRED, VIXCentral, CBOE, vix_utils, Macrotrends, direct Yahoo Finance via yfinance, Westmetall, EIA DNAV, Multpl, Federal Reserve DDP (H.10 / H.15), CFTC Commitments of Traders, foreign central-bank yield archives (Bundesbank / BoE / RBA / BoC / BoJ), USDA AMS, ICCO, ICO, ICE settlement archives, World Bank Pink Sheet, IMF Primary Commodity Prices, or iShares ETF holdings into this workspace.

## Notes

- The `description` field in `SKILL.md` frontmatter controls when Claude Code auto-triggers the skill. Do not modify the `SKILL.md` frontmatter.
- This file is Claude Code metadata only. Codex metadata lives in `agents/openai.yaml`.
