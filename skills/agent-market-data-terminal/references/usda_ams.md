# USDA AMS Market News

The USDA Agricultural Marketing Service (AMS) Market News program publishes daily and weekly cash quotes for grains, livestock, dairy, fruit/vegetable, cotton, and tobacco. The program exposes two distribution channels:

1. **Public bulk archive** at `https://mymarketnews.ams.usda.gov/public_data` — a directory of ZIP archives organized by commodity area and report. Free, no authentication, recommended for backfill.
2. **REST API** at `https://mymarketnews.ams.usda.gov/mymarketnews-api/v1/{report_endpoint}` — JSON delivery of recent report runs. May require an API key for some endpoints; aMDT does not pass authentication.

For the highest-frequency live quotes, AMS also runs an authenticated MARS API at `marsapi.ams.usda.gov`, which is out of scope for this no-auth provider.

## Curated commodity report slugs

These are the slugs called out by the local `amdt_extensions.md` and are pre-baked in the helper so users can request them by short name. The slug column is the report identifier as used in URLs and report metadata; the helper preserves provider-native column names in output.

| Commodity | Slug | Coverage |
|---|---|---|
| Corn | `CN_GR210` | Daily corn cash bids (US grains) |
| Wheat | `WH_GR110` | Daily wheat cash bids |
| Soybeans | `SB_GR110` | Daily soybean cash bids |
| Soybean meal | `SB_GR210` | Daily soybean meal cash |
| Soybean oil | `SB_GR310` | Daily soybean oil cash |
| Live cattle | `LM_CT155` | Daily direct slaughter cattle |
| Lean hogs | `LM_HG200` | Daily direct slaughter hogs |
| Cotton | `CN_CT100` | Daily cotton cash quotes |

The `--list-slugs` mode returns the full curated list; the `--slug` option accepts any AMS slug string for ad-hoc lookup. Verify slug correctness against `https://mymarketnews.ams.usda.gov/public_data` when an unknown slug 404s — AMS occasionally renames or retires reports.

## Endpoints

- Bulk archive directory:
  - `https://mymarketnews.ams.usda.gov/public_data`
  - Sub-directories per commodity area; each contains ZIP archives of historical CSV reports.
- REST API base:
  - `https://mymarketnews.ams.usda.gov/mymarketnews-api/v1/`
- Documentation:
  - `https://mymarketnews.ams.usda.gov/help/api`

## Helper behavior

- `--slug` builds a REST URL using the documented pattern and fetches JSON. Output preserves the AMS-native field names (report_name, report_date, commodity, class, etc.).
- `--bulk` fetches the bulk-archive directory listing (HTML index) and writes a JSON catalog of the linked ZIP files.
- `--list-slugs` prints the curated commodity-to-slug mapping without making any HTTP calls.
- `--url` overrides URL construction for users who have a stable direct file URL.

## Guidance

- For backfill of more than one year, prefer the `/public_data` ZIP archives over the REST API to avoid rate-limit issues.
- Do not pass API keys through this helper. If a slug returns 401/403, the user is on the wrong distribution channel — fall back to the bulk archive or document the gap.
- Preserve AMS-native column names; the field set varies between commodity reports.
