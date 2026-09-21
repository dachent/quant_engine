"""
Schwab Historical Data Capture via Playwright
=============================================

Captures the two datasets that the Schwab Developer API cannot provide:
  1. All-time transaction history (with pagination) — API limits to 60 days
  2. Historical account balances — API has no such endpoint

Current positions and current balances are NOT captured here — use the
Schwab Developer API (GET /accounts/{hash}?fields=positions) for those.

Strategy:
  1. Launch Edge with --remote-debugging-port using a debug profile
  2. Connect Playwright via CDP
  3. Navigate to Schwab, let the SPA authenticate
  4. Extract the Bearer token directly from the SPA's network requests
  5. Use Playwright's context.request API (bypasses CORS) to make direct
     API calls for all accounts with the auth headers
  6. Save raw JSON + convert to CSV

Validated 2026-07-31 against manual Schwab UI export:
  - Transactions: exact match (2,984 total across 8 accounts)
  - Historical balances: exact match on all dates (except intraday
    values that Schwab restates while the market is open)

Requirements:
  - Playwright: pip install playwright && python -m playwright install msedge
  - Python 3.10+
  - Microsoft Edge with a Schwab login session
"""

import json
import os
import time
import csv
import subprocess
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright

# ─── Configuration ───────────────────────────────────────────────────────────

OUTPUT_DIR = os.path.join(
    os.path.expanduser("~"),
    "OneDrive - Fertility Specialists Network",
    "Documents", "Playground", "schwab_har"
)
JSON_DIR = os.path.join(OUTPUT_DIR, "playwright_json")
CSV_DIR = os.path.join(OUTPUT_DIR, "playwright_csv")

EDGE_EXE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
# Debug profile — a minimal copy of your Edge profile that allows CDP.
# Schwab session is stored here after first manual login.
EDGE_USER_DATA = r"C:\Users\BorisVaisman\schwab_debug_edge"
EDGE_PROFILE = "DebugProfile"
CDP_PORT = 9222

# Schwab API endpoints (ausgateway = the SPA's backend)
API_BASE = "https://ausgateway.schwab.com"
TXN_URL = f"{API_BASE}/api/is.TransactionHistoryWeb/TransactionHistoryInterface/TransactionHistory/brokerage/transactions"
HIST_BAL_URL = f"{API_BASE}/api/is.Balances/V1/Balances/balances/historical"
ACCOUNTS_URL = f"{API_BASE}/api/is.TradeOrderManagementWeb/v1/TradeOrderManagementWebPort/customer/accounts"

# All transaction types — pass every type to get complete history
ALL_TXN_TYPES = [
    "Adjustments", "AtmActivity", "BillPay", "CorporateActions", "Checks",
    "Deposits", "DividendsAndCapitalGains", "ElectronicTransfers", "Fees",
    "Interest", "Misc", "SecurityTransfers", "Taxes", "Trades",
    "VisaDebitCard", "Withdrawals",
]


# ─── Edge Launch ─────────────────────────────────────────────────────────────

def launch_edge():
    """Launch Edge with CDP debug port. Returns the subprocess.Popen handle."""
    subprocess.run(["taskkill", "/F", "/IM", "msedge.exe"],
                   capture_output=True, timeout=10)
    time.sleep(2)

    cmd = [
        EDGE_EXE,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={EDGE_USER_DATA}",
        f"--profile-directory={EDGE_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"  Edge launched (PID {proc.pid})")

    # Wait for CDP to be ready
    for _ in range(30):
        try:
            resp = requests.get(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2)
            if resp.status_code == 200:
                print(f"  CDP ready at port {CDP_PORT}")
                return proc
        except requests.ConnectionError:
            pass
        time.sleep(1)

    raise RuntimeError("CDP did not become ready in 30 seconds")


# ─── Auth Header Capture ─────────────────────────────────────────────────────

def capture_auth_headers(page):
    """Capture auth headers from the SPA's API requests.

    Attaches a request listener, then reloads the page to trigger fresh
    API calls. The SPA fires requests to ausgateway.schwab.com with
    Bearer token + custom Schwab headers — we intercept those.
    """
    captured = {}

    def on_request(request):
        url = request.url
        if "ausgateway.schwab.com" in url and "/api/" in url and not captured:
            headers = request.headers
            for key in ["authorization", "schwab-client-correlid",
                        "schwab-clientapp-name", "schwab-client-appid",
                        "schwab-channelcode", "schwab-client-channel",
                        "schwab-environment", "schwab-env",
                        "schwab-resource-version", "correlatorid"]:
                # Headers come back lowercased from Playwright
                val = headers.get(key, "")
                if val:
                    captured[key] = val
            if captured.get("authorization"):
                print(f"  [AUTH] Bearer token captured ({captured['authorization'][:20]}...)")

    page.on("request", on_request)

    # Navigate + reload to trigger fresh API calls
    print("  Navigating to Schwab positions page...")
    page.goto("https://client.schwab.com/app/accounts/positions/",
              wait_until="domcontentloaded", timeout=60000)
    page.reload(wait_until="domcontentloaded", timeout=60000)

    # Wait for the SPA to fire API calls
    print("  Waiting for auth headers...")
    for i in range(30):
        if captured.get("authorization"):
            return captured
        time.sleep(1)

    # Fallback: try balances page
    if not captured.get("authorization"):
        print("  No auth on positions page, trying balances page...")
        page.goto("https://client.schwab.com/app/accounts/balances/",
                  wait_until="domcontentloaded", timeout=60000)
        page.reload(wait_until="domcontentloaded", timeout=60000)
        for i in range(20):
            if captured.get("authorization"):
                return captured
            time.sleep(1)

    return captured


# ─── Account Discovery ───────────────────────────────────────────────────────

def discover_accounts(context, auth_headers):
    """Dynamically discover all brokerage accounts from the API.

    Returns a list of {"id": "...", "name": "..."} dicts.
    Falls back to a hardcoded list if discovery fails.
    """
    headers = build_headers(auth_headers)

    try:
        resp = context.request.get(ACCOUNTS_URL, headers=headers, timeout=30000)
        if resp.status == 200:
            data = json.loads(resp.text())
            accounts = []
            for acct in data.get("brokerageAccounts", []):
                accounts.append({
                    "id": str(acct["brokerageAccountId"]),
                    "name": acct.get("nickName", acct["brokerageAccountId"]),
                })
            if accounts:
                print(f"  Discovered {len(accounts)} accounts from API")
                return accounts
    except Exception as e:
        print(f"  Account discovery failed: {e}")

    # Fallback — hardcoded list (last known 2026-07-31)
    print("  WARNING: Using hardcoded account list — discovery failed")
    return [
        {"id": "25255620", "name": "PCRA Trust"},
        {"id": "57772824", "name": "Joint Tenant 824"},
        {"id": "60497703", "name": "Roth Jess"},
        {"id": "69333293", "name": "IRA Jess"},
        {"id": "72184054", "name": "Joint Tenant 054"},
        {"id": "75362691", "name": "Joint Tenant 691"},
        {"id": "84386757", "name": "Roth Boris"},
        {"id": "90435722", "name": "IRA Boris"},
    ]


# ─── API Call Helper ─────────────────────────────────────────────────────────

def build_headers(auth_headers, account_id=None):
    """Build the headers dict for Schwab API calls."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": "https://client.schwab.com/",
    }

    # Map Playwright's lowercased headers to Schwab's expected casing
    header_map = {
        "authorization": "Authorization",
        "schwab-client-correlid": "Schwab-Client-CorrelId",
        "schwab-clientapp-name": "Schwab-ClientApp-Name",
        "schwab-client-appid": "Schwab-Client-AppId",
        "schwab-channelcode": "Schwab-ChannelCode",
        "schwab-client-channel": "Schwab-Client-Channel",
        "schwab-environment": "Schwab-Environment",
        "schwab-env": "Schwab-Env",
        "schwab-resource-version": "Schwab-Resource-Version",
        "correlatorid": "CorrelatorID",
    }

    for lower_key, proper_key in header_map.items():
        val = auth_headers.get(lower_key, "")
        if val:
            headers[proper_key] = val

    if account_id:
        headers["Schwab-Client-Ids"] = account_id

    return headers


def api_get(context, url, auth_headers, account_id=None):
    """GET request via Playwright context.request (bypasses CORS)."""
    headers = build_headers(auth_headers, account_id)
    try:
        resp = context.request.get(url, headers=headers, timeout=30000)
        return {"status": resp.status, "body": resp.text()}
    except Exception as e:
        return {"status": 0, "body": str(e)}


def api_post(context, url, post_body, auth_headers, account_id=None):
    """POST request via Playwright context.request (bypasses CORS)."""
    headers = build_headers(auth_headers, account_id)
    try:
        resp = context.request.post(url, headers=headers,
                                    data=json.dumps(post_body), timeout=30000)
        return {"status": resp.status, "body": resp.text()}
    except Exception as e:
        return {"status": 0, "body": str(e)}


# ─── Data Capture Functions ──────────────────────────────────────────────────

def capture_transactions(context, account, auth_headers):
    """Capture all-time transactions for an account with pagination."""
    account_id = account["id"]
    account_name = account["name"]
    all_transactions = []
    bookmark = None
    page_num = 1

    while True:
        post_body = {
            "accountNickname": account_name,
            "exportType": "Csv",
            "includeOptionsInSearch": False,
            "isSpsLinkedUkAccount": False,
            "selectedAccountId": account_id,
            "selectedTransactionTypes": ALL_TXN_TYPES,
            "sortColumn": "Date",
            "sortDirection": "Descending",
            "symbol": "",
            "timeFrame": "All",
            "bookmark": bookmark,
            "shouldPaginate": True,
        }

        print(f"  [TXN] {account_name} ({account_id}) — page {page_num}...")
        result = api_post(context, TXN_URL, post_body, auth_headers, account_id)

        if result["status"] != 200:
            print(f"    ERROR: status {result['status']}, body: {result['body'][:200]}")
            break

        data = json.loads(result["body"])
        txns = data.get("brokerageTransactions", [])
        all_transactions.extend(txns)

        bookmark = data.get("bookmark")
        print(f"    Got {len(txns)} transactions (total: {len(all_transactions)})")

        if not bookmark:
            break

        page_num += 1
        time.sleep(0.5)

    # Save raw JSON
    safe_name = account_name.replace(" ", "_")
    out_path = os.path.join(JSON_DIR, f"transactions_{account_id}_{safe_name}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_transactions, f, indent=2, ensure_ascii=False)
    print(f"    Saved {len(all_transactions)} transactions")

    return all_transactions


def capture_historical_balances(context, account, auth_headers):
    """Capture historical balance history for an account."""
    account_id = account["id"]
    account_name = account["name"]

    print(f"  [HIST] {account_name} ({account_id})...")
    result = api_get(context, HIST_BAL_URL, auth_headers, account_id)

    if result["status"] != 200:
        print(f"    ERROR: status {result['status']}, body: {result['body'][:200]}")
        return None

    data = json.loads(result["body"])
    histories = data.get("histories", {})
    print(f"    Got {len(histories)} historical balance data points")

    safe_name = account_name.replace(" ", "_")
    out_path = os.path.join(JSON_DIR, f"historical_balances_{account_id}_{safe_name}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return data


# ─── CSV Conversion ──────────────────────────────────────────────────────────

def transactions_to_csv(account, transactions):
    """Convert transaction list to CSV."""
    if not transactions:
        print(f"    {account['name']}: NO transactions")
        return

    rows = []
    for t in transactions:
        td = t.get("transactionDetail") or {}
        rows.append({
            "account_id": account["id"],
            "account_name": account["name"],
            "transaction_date": t.get("transactionDate", ""),
            "action": t.get("action", ""),
            "symbol": t.get("symbol", ""),
            "description": t.get("description", ""),
            "share_quantity": t.get("shareQuantity", ""),
            "execution_price": t.get("executionPrice", ""),
            "fees_and_commission": t.get("feesAndCommission", ""),
            "amount": t.get("amount", ""),
            "source_code": t.get("sourceCode", ""),
            "effective_date": t.get("effectiveDate", ""),
            "settle_date": td.get("settleDate", ""),
            "commission": td.get("commission", ""),
            "principal": td.get("principal", ""),
            "total": td.get("total", ""),
            "cusip_id": td.get("cusipId", ""),
            "check_number": t.get("checkNumber", ""),
            "check_date": t.get("checkDate", ""),
            "schwab_order_id": t.get("schwabOrderId", ""),
        })

    safe_name = account["name"].replace(" ", "_").replace(".", "")
    filepath = os.path.join(CSV_DIR, f"transactions_{account['id']}_{safe_name}.csv")
    headers = list(rows[0].keys())
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"    CSV: {os.path.basename(filepath)} ({len(rows)} rows)")


def historical_balances_to_csv(account, hist_data):
    """Convert historical balances JSON to CSV."""
    if not hist_data:
        print(f"    {account['name']}: NO historical balances")
        return

    histories = hist_data.get("histories", {})
    rows = []
    for date, val in sorted(histories.items()):
        rows.append({
            "account_id": account["id"],
            "account_name": account["name"],
            "date": date,
            "account_value": val.get("AccountValue", ""),
        })

    if not rows:
        print(f"    {account['name']}: NO historical balance rows")
        return

    safe_name = account["name"].replace(" ", "_").replace(".", "")
    filepath = os.path.join(CSV_DIR, f"historical_balances_{account['id']}_{safe_name}.csv")
    headers = list(rows[0].keys())
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"    CSV: {os.path.basename(filepath)} ({len(rows)} rows)")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(JSON_DIR, exist_ok=True)
    os.makedirs(CSV_DIR, exist_ok=True)

    print("=" * 70)
    print(f"SCHWAB HISTORICAL DATA CAPTURE — {timestamp}")
    print("=" * 70)
    print(f"Output: {OUTPUT_DIR}")
    print(f"Captures: all-time transactions + historical balances (per account)")
    print(f"NOT captured: current positions/balances (use Schwab Developer API)")
    print()

    # Step 1: Launch Edge with CDP
    print("[1] Launching Edge with CDP debug port...")
    edge_proc = launch_edge()

    with sync_playwright() as p:
        # Step 2: Connect via CDP
        print("\n[2] Connecting Playwright to Edge via CDP...")
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        print(f"  Connected. Current URL: {page.url}")

        # Step 3: Check if logged in, capture auth headers
        print("\n[3] Capturing auth headers from Schwab SPA...")
        auth_headers = capture_auth_headers(page)

        if not auth_headers.get("authorization"):
            print("\n*** FAILED to capture auth headers.")
            print("*** Schwab is not logged in. Please log in manually in the")
            print("*** Edge window, then re-run this script.")
            browser.close()
            return

        print(f"  Auth headers captured: {len(auth_headers)} headers")

        # Step 4: Discover accounts dynamically
        print("\n[4] Discovering brokerage accounts...")
        accounts = discover_accounts(context, auth_headers)
        for a in accounts:
            print(f"    {a['id']} — {a['name']}")

        # Step 5: Capture historical balances
        print("\n[5] Capturing historical balances (per account)...")
        all_hist = {}
        for account in accounts:
            hist = capture_historical_balances(context, account, auth_headers)
            all_hist[account["id"]] = hist
            time.sleep(0.5)

        # Step 6: Capture transactions with pagination
        print("\n[6] Capturing all-time transactions (per account, with pagination)...")
        all_txns = {}
        for account in accounts:
            txns = capture_transactions(context, account, auth_headers)
            all_txns[account["id"]] = txns
            time.sleep(0.5)

        # Done with browser
        print("\n[7] Disconnecting from browser...")
        browser.close()

    # Kill Edge
    print("  Closing Edge...")
    edge_proc.terminate()
    try:
        edge_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        edge_proc.kill()

    # Step 8: Convert to CSV
    print("\n[8] Converting to CSV...")
    print("  Historical balances:")
    for account in accounts:
        historical_balances_to_csv(account, all_hist.get(account["id"]))
    print("  Transactions:")
    for account in accounts:
        transactions_to_csv(account, all_txns.get(account["id"], []))

    # Summary
    print("\n" + "=" * 70)
    print("CAPTURE SUMMARY")
    print("=" * 70)
    print(f"  Historical balances:")
    for account in accounts:
        hist = all_hist.get(account["id"])
        count = len(hist.get("histories", {})) if hist else 0
        print(f"    {account['name']:<22} {count:>5} data points {'✓' if count else '✗'}")
    print(f"  Transactions:")
    for account in accounts:
        txns = all_txns.get(account["id"], [])
        print(f"    {account['name']:<22} {len(txns):>5} transactions {'✓' if txns else '✗'}")
    print(f"\n  JSON: {JSON_DIR}")
    print(f"  CSV:  {CSV_DIR}")
    print("=" * 70)


if __name__ == "__main__":
    main()
