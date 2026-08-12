# TuringTrader HAR Capture Runbook

## Prerequisites

- Managed Edge browser with CDP on port 9222
- TuringTrader account (any membership tier)
- Python with `websocket-client` and `requests` installed
- CDP capture script: `har/raw/cdp_nav_capture_tt.py`
- Target pages file: `har/raw/target_pages.txt`

## Procedure

### Step 1: Ensure Edge CDP is running

```bash
# Check if CDP is already running from another Hermes session
curl -s http://127.0.0.1:9222/json/version
# If not, launch Edge with CDP:
# "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" \
#   --remote-debugging-port=9222 --remote-allow-origins=* \
#   --user-data-dir="C:\Users\<user>\AppData\Local\Microsoft\Edge\User Data" \
#   --profile-directory="Default"
```

### Step 2: Create a TuringTrader tab and log in

```bash
# Create a new tab pointing to TuringTrader
curl -s -X PUT "http://127.0.0.1:9222/json/new?https://www.turingtrader.com/"
```

Log in manually in the managed Edge window (click "Member Login", authenticate).

### Step 3: Run the CDP navigation capture

```bash
cd "C:\Users\<user>\OneDrive - Fertility Specialists Network\Documents\Playground\har_turingtrader\har\raw"
python cdp_nav_capture_tt.py
```

The script:
1. Reads `target_pages.txt` (84 URLs: portfolio catalog, dashboards, strategy pages)
2. Creates a new Edge tab per page via `/json/new` (PUT)
3. Connects CDP, enables Network/Page/Runtime domains
4. Waits 8s for page load + 3s for settle
5. Fetches response bodies via `Network.getResponseBody`
6. Closes the tab, moves to next page
7. Fetches cookies from a remaining TT tab
8. Saves a combined HAR file: `turingtrader_nav_{timestamp}.har`

### Step 4: Verify the HAR

```python
import json
with open("turingtrader_nav_*.har") as f:
    har = json.load(f)
entries = har["log"]["entries"]
api = [e for e in entries if "json" in e["response"]["content"].get("mimeType", "")]
print(f"Total: {len(entries)}, API/JSON: {len(api)}")
# Should see ~2000+ entries, ~700+ JSON responses
# Should see Firebase Storage alt=media endpoints for each strategy slug
```

## Known boundaries

- **25 pages per run** (first 25 in priority order from `target_pages.txt`).
  To capture all 84 pages, split into multiple runs or increase the limit in
  the script.
- **19 unique strategy slugs captured** in the initial run. The full catalog
  (`portfolio-comp.json`) lists 121 slugs. To capture more, add their URLs to
  `target_pages.txt`.
- **Firebase Storage two-step access**: each data file triggers two requests
  (metadata + content). The contract only accepts `alt=media` content
  requests; metadata requests are filtered as noise.
- **Response body eviction**: some bodies fail to fetch if CDP evicts them
  before `getResponseBody` runs. The 3-thread architecture fetches bodies
  immediately on `loadingFinished` to minimize this.
- **Private strategies** (`priv-*`): accessible if the account has permission.
  Access-restricted strategies return a small HTML "Access Restricted" page
  with no Firebase Storage data calls.

## Session envelope

```python
from datetime import datetime, timezone, timedelta
from quant_engine.web_contracts import SessionEnvelope

session = SessionEnvelope(
    captured_at=datetime(2026, 8, 2, 13, 25, tzinfo=timezone.utc),
    expires_at=datetime(2026, 8, 2, 14, 25, tzinfo=timezone.utc),
)
```
