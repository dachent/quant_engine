#!/usr/bin/env python3
"""
HAR-to-CSV Converter — Portfolio123
Parses the HAR file from p123_har_capture.py and extracts all strategy data
into CSV files. Also saves individual API response JSONs for reference.

Works by:
1. Loading the HAR file
2. Filtering for P123 JSON responses with bodies
3. Identifying data structures by JSON shape
4. Extracting to CSVs (equity curve, holdings, transactions, trading system, stats, etc.)
5. Dumping unknown structures as generic flattened CSVs
"""

import json
import os
import csv
import glob
import re
from datetime import datetime
from urllib.parse import urlparse, parse_qs

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HAR_DIR = os.path.join(SCRIPT_DIR, "json")
CSV_DIR = os.path.join(SCRIPT_DIR, "csv")
RESPONSES_DIR = os.path.join(HAR_DIR, "p123_api_responses")


def find_har_files():
    """Find all HAR files in the json directory (per-strategy or monolithic)."""
    # Per-strategy HARs (new format): strategy_{id}_*.har
    strategy_hars = sorted(glob.glob(os.path.join(HAR_DIR, "strategy_*_*.har")))
    # Monolithic HAR (old format): p123_all_*.har
    monolithic_hars = sorted(glob.glob(os.path.join(HAR_DIR, "p123_all_*.har")))
    
    all_hars = strategy_hars + monolithic_hars
    if not all_hars:
        print("ERROR: No HAR file found in", HAR_DIR)
        print("Run p123_har_capture.py first.")
        return []
    
    print(f"Found {len(all_hars)} HAR file(s):")
    for h in all_hars:
        print(f"  {os.path.basename(h)} ({os.path.getsize(h) / 1024 / 1024:.1f} MB)")
    return all_hars


def is_p123_json(entry):
    """Check if a HAR entry is a P123 JSON response with a body."""
    url = entry["request"]["url"]
    mime = entry["response"]["content"].get("mimeType", "")
    body = entry["response"]["content"].get("text", "")
    has_body = bool(body) and len(body) > 10

    # Must be from portfolio123.com
    if "portfolio123.com" not in url:
        return False

    # Must be JSON or text (some APIs return text/plain)
    if "json" not in mime and "text" not in mime and "javascript" not in mime:
        return False

    # Skip static assets
    if any(url.endswith(ext) for ext in [".js", ".css", ".png", ".gif", ".jpg", ".woff", ".woff2", ".ico"]):
        return False

    # Skip analytics/tracking
    if any(x in url for x in ["google", "facebook", "clarity", "sentry", "gtag", "analytics"]):
        return False

    return has_body


def is_p123_html(entry):
    """Check if a HAR entry is a P123 HTML page with data (JSP strategy pages)."""
    url = entry["request"]["url"]
    mime = entry["response"]["content"].get("mimeType", "")
    body = entry["response"]["content"].get("text", "")
    has_body = bool(body) and len(body) > 1000

    if "portfolio123.com" not in url:
        return False
    if "html" not in mime:
        return False
    # JSP strategy pages with data
    if not any(x in url for x in ["port_summary.jsp", "holdings.jsp", "transact_list.jsp",
                                   "performance_stats.jsp", "port_wiz.jsp", "port_simul6.jsp",
                                   "ranking-system"]):
        return False
    # Must have actual content (not login wall)
    if "Access Restricted" in body or len(body) < 2000:
        return False
    return has_body


def extract_trading_system_from_html(html_body, strategy_id):
    """Extract trading system details from port_wiz.jsp HTML.
    
    The page contains buy rules, sell rules, universe, ranking system, 
    rebalance settings, commission, slippage, margin, etc. as labeled text sections.
    """
    csvs = []
    
    # Remove HTML tags to get clean text
    import re
    clean_text = re.sub(r'<[^>]+>', '\n', html_body)
    clean_text = re.sub(r'\n\s*\n', '\n', clean_text)
    clean_text = clean_text.replace('&nbsp;', ' ').replace('&amp;', '&')
    clean_text = clean_text.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
    clean_text = clean_text.replace('&#39;', "'").replace('&le;', '<=').replace('&ge;', '>=')
    lines = [l.strip() for l in clean_text.split('\n') if l.strip()]
    
    # Extract key-value pairs for general settings
    general_settings = {}
    buy_rules = []
    sell_rules = []
    
    # Known section labels to extract as key-value
    setting_keys = [
        'Currency', 'Starting Capital', 'Mode', 'Early Rebalance', 'Commission',
        'Slippage', 'Price for Transactions', 'Transaction Type', 'Use Margin',
        'Leverage', 'Margin Carry Cost', 'Min Maintenance Margin', 'When Margin is Called',
        'Management Fee', 'PIT Method - Prelim', 'Sizing Method',
        'Ideal Size of a New Position', 'Ideal Number of Positions',
        'Rebalance Frequency', 'Allow Immediate Buyback', 'Buy Constraint',
        'Universe', 'Benchmark', 'Ranking System', 'Ranking Method',
        'Force Positions into Universe',
        'Minimum Rebalance Transaction', 'Gross Exposure', 'Net Exposure',
        'Allocation Algorithm',
    ]
    
    # Parse the text lines to extract settings and rules
    in_buy_rules = False
    in_sell_rules = False
    rule_num = 0
    
    for i, line in enumerate(lines):
        # Check for buy/sell rule sections
        # "Buy Rules" and "(Implicit AND)" may be on separate lines
        if line == 'Buy Rules':
            in_buy_rules = True
            in_sell_rules = False
            rule_num = 0
            continue
        if line == 'Sell Rules':
            in_buy_rules = False
            in_sell_rules = True
            rule_num = 0
            continue
        # Skip the "(Implicit AND)" / "(Implicit OR)" lines
        if line.startswith('(Implicit'):
            continue
        if 'Hedge' in line and i > 10:
            in_buy_rules = False
            in_sell_rules = False
            continue
        if 'Restrictions' in line and i > 10:
            in_buy_rules = False
            in_sell_rules = False
            continue
        if line == 'copy to screen':
            continue
        
        # Extract buy/sell rules (lines after "Buy1", "Buy2", etc.)
        if in_buy_rules or in_sell_rules:
            if re.match(r'^(Buy|Sell)\d+$', line):
                rule_num += 1
                # Next line should be the formula
                if i + 1 < len(lines):
                    formula = lines[i + 1]
                    if in_buy_rules:
                        buy_rules.append({'rule': line, 'formula': formula})
                    else:
                        sell_rules.append({'rule': line, 'formula': formula})
            continue
        
        # Extract general settings
        for key in setting_keys:
            if line == key and i + 1 < len(lines):
                val = lines[i + 1]
                # Don't capture if the value is another setting key
                if val not in setting_keys and not val.startswith('Buy') and not val.startswith('Sell'):
                    general_settings[key] = val
                break
    
    # Save general settings
    if general_settings:
        path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_trading_system_settings.csv")
        rows = [[k, v] for k, v in general_settings.items()]
        save_csv(path, rows, ["setting", "value"])
        csvs.append((path, len(rows), "Trading system settings (universe, ranking, rebalance, margin, etc.)"))
    
    # Save buy rules
    if buy_rules:
        path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_buy_rules.csv")
        rows = [[r['rule'], r['formula']] for r in buy_rules]
        save_csv(path, rows, ["rule", "formula"])
        csvs.append((path, len(rows), "Buy rules with formulas"))
    
    # Save sell rules
    if sell_rules:
        path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_sell_rules.csv")
        rows = [[r['rule'], r['formula']] for r in sell_rules]
        save_csv(path, rows, ["rule", "formula"])
        csvs.append((path, len(rows), "Sell rules with formulas"))
    
    return csvs


def try_parse_json(body_text, encoding=None):
    """Try to parse a response body as JSON."""
    if not body_text:
        return None
    try:
        if encoding == "base64":
            import base64
            body_text = base64.b64decode(body_text).decode("utf-8", errors="replace")
        return json.loads(body_text)
    except (json.JSONDecodeError, ValueError):
        return None


def extract_strategy_id(url):
    """Extract strategy/portid from URL."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    sid = qs.get("portid", qs.get("id", [None]))[0]
    if sid:
        return sid
    # Try to extract from path (e.g., /app/investment/holdings/1913284)
    path_parts = parsed.path.strip("/").split("/")
    for part in path_parts:
        if part.isdigit():
            return part
    return "unknown"


def parse_html_tables(html_body):
    """Extract all data tables from an HTML page.
    Returns list of (headers, rows) tuples.
    """
    tables = []
    # Find all table elements
    table_pattern = r'<table[^>]*>(.*?)</table>'
    for table_html in re.findall(table_pattern, html_body, re.DOTALL):
        # Extract headers
        header_cells = re.findall(r'<th[^>]*>(.*?)</th>', table_html, re.DOTALL)
        headers = [re.sub(r'<[^>]+>', '', c).strip() for c in header_cells]
        
        # Extract rows
        rows = []
        for row_html in re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL):
            cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, re.DOTALL)
            cells_clean = []
            for c in cells:
                # Remove HTML tags, &nbsp;, extra whitespace
                clean = re.sub(r'<[^>]+>', '', c)
                clean = clean.replace('&nbsp;', ' ').replace('\n', ' ').strip()
                # Remove 3M 1Y 3Y suffix patterns (P123 UI artifacts)
                clean = re.sub(r'\s*3M\s+1Y\s+3Y\s*', ' ', clean)
                clean = ' '.join(clean.split())  # Normalize whitespace
                cells_clean.append(clean)
            if cells_clean and any(c for c in cells_clean):
                rows.append(cells_clean)
        
        if rows:
            tables.append((headers, rows))
    return tables


def get_url_slug(url):
    """Create a short slug from URL for naming files."""
    parsed = urlparse(url)
    path = parsed.path.strip("/").replace("/", "_")
    if not path:
        path = "root"
    # Truncate
    return re.sub(r'[^a-zA-Z0-9_]', '', path)[:60]


def save_csv(filepath, rows, headers=None):
    """Save a list of dicts (or list of lists + headers) to CSV."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if headers:
            writer.writerow(headers)
        for row in rows:
            if isinstance(row, dict):
                writer.writerow([row.get(h, "") for h in headers])
            else:
                writer.writerow(row)
    return filepath


def save_dict_as_csv(filepath, d, key_col="field", val_col="value"):
    """Save a flat dict as a 2-column CSV (key, value)."""
    rows = [[k, v] for k, v in d.items()]
    save_csv(filepath, rows, [key_col, val_col])


def flatten_dict(d, prefix=""):
    """Flatten a nested dict into a single-level dict with dot-notation keys."""
    result = {}
    for k, v in d.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(flatten_dict(v, full_key))
        elif isinstance(v, list):
            result[full_key] = json.dumps(v)  # Keep lists as JSON
        else:
            result[full_key] = v
    return result


# ─── Data structure identifiers ──────────────────────────────────────────────

def is_strategy_details(data):
    """Check if JSON looks like strategy details (summary + stats + dailyPerf)."""
    if not isinstance(data, dict):
        return False
    return "summary" in data and ("dailyPerf" in data or "stats" in data)


def is_holdings(data):
    """Check if JSON looks like holdings data."""
    if not isinstance(data, dict):
        return False
    return "holdings" in data and isinstance(data["holdings"], list)


def is_trading_system(data):
    """Check if JSON looks like a trading system."""
    if not isinstance(data, dict):
        return False
    return "tradingSystem" in data or "buyRules" in data or "sellRules" in data


def is_transactions(data):
    """Check if JSON looks like transactions."""
    if not isinstance(data, dict):
        return False
    return "trans" in data and isinstance(data.get("trans"), list)


def is_equity_curve(data):
    """Check if JSON has parallel arrays that look like daily performance."""
    if not isinstance(data, dict):
        return False
    # Look for date array + at least one numeric array
    has_dates = any(isinstance(v, list) and v and isinstance(v[0], str) for v in data.values())
    has_nums = any(isinstance(v, list) and v and isinstance(v[0], (int, float)) for v in data.values())
    return has_dates and has_nums


def is_perf_table(data):
    """Check if JSON has period/model/bench arrays (performance table)."""
    if not isinstance(data, dict):
        return False
    return all(k in data for k in ["period", "model", "bench"]) or \
           all(k in data for k in ["period", "model", "benchmark"])


# ─── Extractors ──────────────────────────────────────────────────────────────

def extract_summary(data, strategy_id):
    """Extract generalInfo and quickStats from strategy details."""
    csvs = []
    summary = data.get("summary", {})
    gen_info = summary.get("generalInfo", {})
    quick_stats = summary.get("quickStats", {})

    if gen_info:
        path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_summary.csv")
        save_dict_as_csv(path, gen_info)
        csvs.append((path, len(gen_info), "Strategy summary (general info)"))

    if quick_stats:
        path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_quickstats.csv")
        save_dict_as_csv(path, quick_stats)
        csvs.append((path, len(quick_stats), "Quick stats"))

    return csvs


def extract_equity_curve(data, strategy_id):
    """Extract daily performance parallel arrays into CSV."""
    csvs = []
    dp = data.get("dailyPerf", data)

    if not isinstance(dp, dict):
        return csvs

    # Find the date array
    date_key = None
    for k in ["date", "dates"]:
        if k in dp and isinstance(dp[k], list):
            date_key = k
            break

    if not date_key:
        return csvs

    dates = dp[date_key]
    if not dates:
        return csvs

    # Collect all parallel arrays (numeric or string arrays of same length)
    arrays = {}
    for k, v in dp.items():
        if k == date_key:
            continue
        if isinstance(v, list) and len(v) == len(dates):
            arrays[k] = v

    if not arrays:
        return csvs

    # Build CSV rows
    headers = [date_key] + list(arrays.keys())
    rows = []
    for i in range(len(dates)):
        row = [dates[i]]
        for k in arrays:
            row.append(arrays[k][i])
        rows.append(row)

    path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_equity_curve.csv")
    save_csv(path, rows, headers)
    csvs.append((path, len(rows), "Daily equity curve / performance"))
    return csvs


def extract_holdings(data, strategy_id):
    """Extract holdings array into CSV."""
    csvs = []
    holdings = data.get("holdings", [])

    if not holdings or not isinstance(holdings, list):
        return csvs

    # Get all unique keys across holdings
    all_keys = set()
    for h in holdings:
        if isinstance(h, dict):
            all_keys.update(h.keys())
    headers = sorted(all_keys)

    rows = []
    for h in holdings:
        if isinstance(h, dict):
            rows.append([h.get(k, "") for k in headers])

    path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_holdings.csv")
    save_csv(path, rows, headers)
    csvs.append((path, len(rows), "Current holdings"))
    return csvs


def extract_transactions(data, strategy_id):
    """Extract transactions array into CSV."""
    csvs = []
    trans = data.get("trans", [])

    if not trans or not isinstance(trans, list):
        return csvs

    all_keys = set()
    for t in trans:
        if isinstance(t, dict):
            all_keys.update(t.keys())
    headers = sorted(all_keys)

    rows = []
    for t in trans:
        if isinstance(t, dict):
            rows.append([t.get(k, "") for k in headers])

    path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_transactions.csv")
    save_csv(path, rows, headers)
    csvs.append((path, len(rows), "Transaction history"))
    return csvs


def extract_trading_system(data, strategy_id):
    """Extract trading system details: buy/sell rules, rebalance, universe, etc."""
    csvs = []
    ts = data.get("tradingSystem", data)

    if not isinstance(ts, dict):
        return csvs

    # Buy rules
    buy_rules = ts.get("buyRules", [])
    if buy_rules:
        headers = ["name", "formula", "disabled"]
        rows = [[r.get("name", ""), r.get("formula", ""), r.get("disabled", "")] for r in buy_rules]
        path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_buy_rules.csv")
        save_csv(path, rows, headers)
        csvs.append((path, len(rows), "Buy rules"))

    # Sell rules
    sell_rules = ts.get("sellRules", [])
    if sell_rules:
        headers = ["name", "formula", "disabled"]
        rows = [[r.get("name", ""), r.get("formula", ""), r.get("disabled", "")] for r in sell_rules]
        path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_sell_rules.csv")
        save_csv(path, rows, headers)
        csvs.append((path, len(rows), "Sell rules"))

    # Rebalance settings
    rebalance = ts.get("rebalance", {})
    if rebalance:
        path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_rebalance.csv")
        save_dict_as_csv(path, rebalance)
        csvs.append((path, len(rebalance), "Rebalance settings"))

    # Book assets (if this is a Book)
    assets = ts.get("assets", [])
    if assets:
        all_keys = set()
        for a in assets:
            if isinstance(a, dict):
                all_keys.update(a.keys())
        headers = sorted(all_keys)
        rows = [[a.get(k, "") for k in headers] for a in assets if isinstance(a, dict)]
        path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_book_assets.csv")
        save_csv(path, rows, headers)
        csvs.append((path, len(rows), "Book assets/constituents"))

    # General trading system info (universe, ranking, etc.) — exclude arrays already extracted
    ts_flat = {}
    for k, v in ts.items():
        if k not in ("buyRules", "sellRules", "rebalance", "assets"):
            if isinstance(v, (dict, list)):
                ts_flat[k] = json.dumps(v)
            else:
                ts_flat[k] = v
    if ts_flat:
        path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_trading_system.csv")
        save_dict_as_csv(path, ts_flat)
        csvs.append((path, len(ts_flat), "Trading system settings"))

    return csvs


def extract_stats(data, strategy_id):
    """Extract performance stats, risk measurements, trading summary."""
    csvs = []
    stats = data.get("stats", {})
    if not stats:
        return csvs

    # Performance return percentages
    perf = stats.get("perf", {})
    perf_return = perf.get("returnPct", {})
    if perf_return:
        rows = []
        for period, comp in perf_return.items():
            if isinstance(comp, dict):
                rows.append([period, comp.get("model", ""), comp.get("bench", "")])
        if rows:
            path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_perf_returns.csv")
            save_csv(path, rows, ["period", "model", "bench"])
            csvs.append((path, len(rows), "Performance returns by period"))

    # Performance tables (monthly, yearly, weekly)
    for table_name in ["monthly", "yearly", "weekly"]:
        table = perf.get(table_name, {})
        if table and isinstance(table.get("model"), list):
            period_arr = table.get("period", [])
            model_arr = table.get("model", [])
            bench_arr = table.get("bench", table.get("benchmark", []))
            rows = list(zip(period_arr, model_arr, bench_arr))
            path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_perf_{table_name}.csv")
            save_csv(path, rows, ["period", "model", "bench"])
            csvs.append((path, len(rows), f"Performance table ({table_name})"))

    # Trading stats
    trading = stats.get("trading", {})
    if trading:
        # Parameters
        params = trading.get("parameters", {})
        if params:
            path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_trading_params.csv")
            save_dict_as_csv(path, params)
            csvs.append((path, len(params), "Trading parameters"))

        # Summary
        summary = trading.get("summary", {})
        if summary:
            path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_trading_summary.csv")
            save_dict_as_csv(path, summary)
            csvs.append((path, len(summary), "Trading summary"))

        # Realized / Unrealized
        for cat in ["realized", "unrealized"]:
            cat_data = trading.get(cat, {})
            if cat_data:
                flat = flatten_dict(cat_data)
                path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_trading_{cat}.csv")
                save_dict_as_csv(path, flat)
                csvs.append((path, len(flat), f"Trading {cat} details"))

    # Risk measurements
    risk = stats.get("riskMeasurements", {})
    if risk:
        rows = []
        for freq_key, freq_label in [("daily", "daily"), ("weekly", "weekly"), ("monthly", "monthly"), ("yearly", "yearly")]:
            freq_data = risk.get(freq_key, {})
            for span_key in ["trailing3Year", "sinceInception"]:
                span_data = freq_data.get(span_key, {})
                for metric in ["totalReturn", "annualizedReturn", "maxDrawdown", "standardDeviation",
                               "sharpeRatio", "sortinoRatio", "benchCorrel", "rSquared", "beta", "alpha",
                               "periodSamples"]:
                    metric_data = span_data.get(metric, {})
                    if isinstance(metric_data, dict):
                        rows.append([
                            freq_label, span_key, metric,
                            metric_data.get("model", ""),
                            metric_data.get("bench", ""),
                        ])
        if rows:
            path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_risk_measurements.csv")
            save_csv(path, rows, ["frequency", "span", "metric", "model", "bench"])
            csvs.append((path, len(rows), "Risk measurements"))

    return csvs


def extract_generic(data, strategy_id, url_slug):
    """Generic fallback: flatten any JSON to CSV."""
    csvs = []
    if isinstance(data, dict):
        # Check if it's a simple dict of values
        flat = flatten_dict(data)
        if flat:
            path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_{url_slug}.csv")
            save_dict_as_csv(path, flat)
            csvs.append((path, len(flat), f"Generic data ({url_slug})"))

    elif isinstance(data, list) and data:
        # Array of objects — try to make a table
        if isinstance(data[0], dict):
            all_keys = set()
            for item in data:
                if isinstance(item, dict):
                    all_keys.update(item.keys())
            headers = sorted(all_keys)
            rows = [[item.get(k, "") for k in headers] for item in data if isinstance(item, dict)]
            path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_{url_slug}.csv")
            save_csv(path, rows, headers)
            csvs.append((path, len(rows), f"Generic array ({url_slug})"))

    return csvs


# ─── Main ────────────────────────────────────────────────────────────────────

def merge_transaction_csvs(csv_dir):
    """Merge all paginated transaction CSVs into one per strategy."""
    import collections
    
    # Group transaction offset CSVs by strategy
    transact_files = collections.defaultdict(list)
    for f in os.listdir(csv_dir):
        m = re.match(r'strategy_(\d+)_holdings_transact_list_offset(\d+)\.csv', f)
        if m:
            sid = m.group(1)
            offset = int(m.group(2))
            transact_files[sid].append((offset, os.path.join(csv_dir, f)))
    
    merged_count = 0
    for sid, files in transact_files.items():
        if len(files) <= 1:
            continue
        
        # Sort by offset
        files.sort(key=lambda x: x[0])
        
        merged_path = os.path.join(csv_dir, f"strategy_{sid}_transactions_all.csv")
        with open(merged_path, "w", newline="", encoding="utf-8") as outfile:
            writer = None
            total_rows = 0
            for offset, filepath in files:
                with open(filepath, "r", encoding="utf-8") as infile:
                    reader = csv.reader(infile)
                    try:
                        header = next(reader)
                    except StopIteration:
                        continue
                    if writer is None:
                        writer = csv.writer(outfile)
                        writer.writerow(header)
                    for row in reader:
                        writer.writerow(row)
                        total_rows += 1
        print(f"  Merged {len(files)} pages → strategy_{sid}_transactions_all.csv ({total_rows} rows)")
        merged_count += 1
    
    return merged_count


def main():
    os.makedirs(CSV_DIR, exist_ok=True)
    os.makedirs(RESPONSES_DIR, exist_ok=True)

    har_paths = find_har_files()
    if not har_paths:
        return

    all_csvs = []
    seen_urls = set()

    for har_path in har_paths:
        print(f"\n{'='*60}")
        print(f"Processing: {os.path.basename(har_path)}")
        print(f"{'='*60}")

        with open(har_path, "r", encoding="utf-8") as f:
            har = json.load(f)

        entries = har["log"]["entries"]
        print(f"Total HAR entries: {len(entries)}")

        # Filter for P123 JSON responses with bodies
        p123_json_entries = [e for e in entries if is_p123_json(e)]
        p123_html_entries = [e for e in entries if is_p123_html(e)]
        print(f"P123 JSON responses with bodies: {len(p123_json_entries)}")
        print(f"P123 HTML pages with data: {len(p123_html_entries)}")

        # Process HTML pages
        for entry in p123_html_entries:
            url = entry["request"]["url"]
            body_text = entry["response"]["content"].get("text", "")
            encoding = entry["response"]["content"].get("encoding")

            if encoding == "base64":
                import base64
                body_text = base64.b64decode(body_text).decode("utf-8", errors="replace")

            strategy_id = extract_strategy_id(url)
            parsed = urlparse(url)
            page_type = parsed.path.split("/")[-1].replace(".jsp", "")

            # For port_wiz.jsp, extract strategy ID from HTML body
            if "port_wiz" in url:
                edit_uid_match = re.search(r'editUid[=:]\s*["\']?(\d+)', body_text)
                if edit_uid_match:
                    strategy_id = edit_uid_match.group(1)
                page_type = "port_wiz"

            print(f"\n  Processing HTML page: {page_type} for strategy {strategy_id}")

            # Save raw HTML
            html_path = os.path.join(RESPONSES_DIR, f"strategy_{strategy_id}_{page_type}.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(body_text)

            # port_wiz.jsp or port_simul6.jsp — trading system details
            if "port_wiz" in url or "port_simul6" in url:
                if "port_simul6" in url:
                    for pattern in [r'editUid[=:]\s*["\']?(\d+)', r'value="(\d{6,})"', r'portUid[=:]\s*["\']?(\d+)']:
                        edit_uid_match = re.search(pattern, body_text)
                        if edit_uid_match:
                            strategy_id = edit_uid_match.group(1)
                            break
                    page_type = "port_simul6"
                ts_csvs = extract_trading_system_from_html(body_text, strategy_id)
                all_csvs.extend(ts_csvs)
                print(f"    Trading system: {len(ts_csvs)} CSVs extracted")
                html_path = os.path.join(RESPONSES_DIR, f"strategy_{strategy_id}_{page_type}.html")
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(body_text)
                continue

            # ranking-system pages
            if "ranking-system" in url:
                tables = parse_html_tables(body_text)
                print(f"    Found {len(tables)} tables")
                for i, (headers, rows) in enumerate(tables):
                    csv_path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_ranking_system_table{i}.csv")
                    actual_headers = headers if headers else [f"col{j}" for j in range(len(rows[0]) if rows else 0)]
                    save_csv(csv_path, rows, actual_headers)
                    all_csvs.append((csv_path, len(rows), f"Ranking system table {i}"))
                continue

            # Other JSP pages — extract tables
            tables = parse_html_tables(body_text)

            if "transact_list" in url and "offset=" in url:
                qs = parse_qs(parsed.query)
                offset_val = qs.get("offset", ["0"])[0]
                page_type = f"transact_list_offset{offset_val}"
            print(f"    Found {len(tables)} tables")

            for i, (headers, rows) in enumerate(tables):
                if not rows:
                    continue
                table_content = " ".join(headers + rows[0]).lower() if rows else ""

                if "quick stats" in table_content or "total return" in table_content:
                    csv_path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_quickstats_{page_type}.csv")
                    kv_rows = [[r[0], r[1] if len(r) > 1 else ""] for r in rows if len(r) >= 2 and r[0]]
                    save_csv(csv_path, kv_rows, ["field", "value"])
                    all_csvs.append((csv_path, len(kv_rows), f"Quick stats ({page_type})"))
                elif "ticker" in table_content and ("weight" in table_content or "shares" in table_content):
                    csv_path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_holdings_{page_type}.csv")
                    actual_headers = headers
                    actual_rows = rows
                    if rows and not headers and "ticker" in rows[0]:
                        actual_headers = rows[0]
                        actual_rows = rows[1:]
                    data_rows = [r for r in actual_rows if r and len(r) > 1]
                    save_csv(csv_path, data_rows, actual_headers if actual_headers else [f"col{j}" for j in range(len(data_rows[0]) if data_rows else 0)])
                    all_csvs.append((csv_path, len(data_rows), f"Holdings ({page_type})"))
                elif "date" in table_content and ("type" in table_content or "shares" in table_content):
                    csv_path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_holdings_{page_type}.csv")
                    actual_headers = headers
                    actual_rows = rows
                    if rows and not headers and "date" in rows[0]:
                        actual_headers = rows[0]
                        actual_rows = rows[1:]
                    save_csv(csv_path, actual_rows, actual_headers if actual_headers else [f"col{j}" for j in range(len(actual_rows[0]) if actual_rows else 0)])
                    all_csvs.append((csv_path, len(actual_rows), f"Transactions ({page_type})"))
                elif "total market value" in table_content or "number of positions" in table_content:
                    csv_path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_summary_{page_type}.csv")
                    kv_rows = [[r[0], r[1] if len(r) > 1 else ""] for r in rows if len(r) >= 2 and r[0] and "PDF" not in r[0]]
                    save_csv(csv_path, kv_rows, ["field", "value"])
                    all_csvs.append((csv_path, len(kv_rows), f"Summary ({page_type})"))
                else:
                    csv_path = os.path.join(CSV_DIR, f"strategy_{strategy_id}_{page_type}_table{i}.csv")
                    actual_headers = headers if headers else [f"col{j}" for j in range(len(rows[0]) if rows else 0)]
                    save_csv(csv_path, rows, actual_headers)
                    all_csvs.append((csv_path, len(rows), f"Table {i} ({page_type})"))

        # Process JSON responses
        for entry in p123_json_entries:
            url = entry["request"]["url"]
            body_text = entry["response"]["content"].get("text", "")
            encoding = entry["response"]["content"].get("encoding")

            data = try_parse_json(body_text, encoding)
            if data is None:
                continue

            strategy_id = extract_strategy_id(url)
            url_slug = get_url_slug(url)

            resp_filename = f"strategy_{strategy_id}_{url_slug}_{hash(url) % 100000}.json"
            resp_path = os.path.join(RESPONSES_DIR, resp_filename)
            with open(resp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            url_key = url.split("?")[0]
            if url_key in seen_urls and strategy_id == "unknown":
                continue
            seen_urls.add(url_key)

            csvs = []
            if is_strategy_details(data):
                csvs.extend(extract_summary(data, strategy_id))
                csvs.extend(extract_equity_curve(data, strategy_id))
                csvs.extend(extract_stats(data, strategy_id))
            if is_holdings(data):
                csvs.extend(extract_holdings(data, strategy_id))
            if is_trading_system(data):
                csvs.extend(extract_trading_system(data, strategy_id))
            if is_transactions(data):
                csvs.extend(extract_transactions(data, strategy_id))

            if isinstance(data, dict):
                if "holdings" in data and not is_holdings(data):
                    csvs.extend(extract_holdings(data, strategy_id))
                if "dailyPerf" in data and not is_strategy_details(data):
                    csvs.extend(extract_equity_curve(data.get("dailyPerf"), strategy_id))

            if not csvs:
                csvs = extract_generic(data, strategy_id, url_slug)
            all_csvs.extend(csvs)

    # Merge transaction pagination CSVs
    print(f"\n{'='*60}")
    print("Merging transaction CSVs...")
    merged = merge_transaction_csvs(CSV_DIR)
    print(f"Merged {merged} strategy transaction sets")

    # Print summary
    print(f"\n{'='*80}")
    print(f"Extraction complete — {len(all_csvs)} CSV files created")
    print(f"{'='*80}")
    print(f"\n{'File':<65} {'Rows':>6}  Description")
    print(f"{'-'*65} {'-'*6}  {'-'*40}")
    for path, rows, desc in all_csvs:
        rel_path = os.path.relpath(path, SCRIPT_DIR)
        print(f"{rel_path:<65} {rows:>6}  {desc}")

    print(f"\n{'='*80}")
    print(f"Individual API response JSONs saved to: {RESPONSES_DIR}")
    resp_files = os.listdir(RESPONSES_DIR)
    print(f"Total response files: {len(resp_files)}")
    print(f"{'='*80}")

    # List unique API URLs
    print(f"\nUnique P123 API URLs captured:")
    for url_key in sorted(seen_urls):
        print(f"  {url_key}")


if __name__ == "__main__":
    main()
