#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


USER_AGENT = "Codex agent-market-data-terminal/1.0"
ROOT = "https://www.ishares.com"
HOLDINGS_AJAX_PATH = "1467271812596.ajax"
LANDING_PAGE_URL = f"{ROOT}/us/products/etf-investments"

CURATED_TICKER_MAP: dict[str, str] = {
    "IWV": "us/products/239714/ishares-russell-3000-etf",
    "IVV": "us/products/239726/ishares-core-sp-500-etf",
    "IWM": "us/products/239710/ishares-russell-2000-etf",
    "IWB": "us/products/239707/ishares-russell-1000-etf",
    "IWF": "us/products/239706/ishares-russell-1000-growth-etf",
    "IWD": "us/products/239708/ishares-russell-1000-value-etf",
    "AGG": "us/products/239458/ishares-core-total-us-bond-market-etf",
    "TLT": "us/products/239454/ishares-20-year-treasury-bond-etf",
    "SHY": "us/products/239452/ishares-13-year-treasury-bond-etf",
    "EFA": "us/products/239623/ishares-msci-eafe-etf",
    "EEM": "us/products/239637/ishares-msci-emerging-markets-etf",
}

LINK_PATTERN = re.compile(
    r'<a[^>]+href\s*=\s*"(?P<href>[^"]*us/products/[^"]+)"[^>]*>(?P<label>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TAG_PATTERN = re.compile(r"<[^>]+>")
TICKER_NEAR_LINK = re.compile(r"\b([A-Z]{2,6})\b")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch iShares ETF universe or holdings (current / historical).")
    parser.add_argument("--mode", required=True, choices=["universe", "holdings", "history"])
    parser.add_argument("--ticker", help="iShares ETF ticker; resolves via curated map.")
    parser.add_argument("--product-url", help="Explicit product URL slug, e.g. us/products/239707/ishares-russell-1000-etf.")
    parser.add_argument("--asof", help="As-of date in YYYY-MM-DD (history mode).")
    parser.add_argument("--file-type", choices=["json", "csv"], default="json", help="iShares fileType parameter.")
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    return parser.parse_args()


def fetch_bytes(url: str) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(2):
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=60) as response:
                return response.read()
        except HTTPError as exc:
            last_exc = exc
            if exc.code and 500 <= exc.code < 600 and attempt == 0:
                time.sleep(5)
                continue
            raise
        except URLError as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(5)
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Unreachable retry path.")


def clean_text(value: str) -> str:
    text = TAG_PATTERN.sub(" ", value)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def resolve_product_url(args: argparse.Namespace) -> str:
    if args.product_url:
        slug = args.product_url.strip().lstrip("/")
        if slug.startswith("http"):
            raise ValueError("--product-url must be a slug like us/products/239707/...; do not include the host.")
        return slug
    if args.ticker:
        ticker = args.ticker.strip().upper()
        if ticker not in CURATED_TICKER_MAP:
            raise ValueError(
                f"Ticker {ticker!r} is not in the curated map. Pass --product-url instead, "
                f"or look up the slug at {LANDING_PAGE_URL}."
            )
        return CURATED_TICKER_MAP[ticker]
    raise ValueError("Provide --ticker or --product-url for holdings/history modes.")


def normalize_asof(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("--asof must be in YYYY-MM-DD format.") from exc
    return parsed.strftime("%Y%m%d")


def build_holdings_url(product_slug: str, file_type: str, asof_yyyymmdd: str | None) -> str:
    params = [("fileType", file_type), ("tab", "all")]
    if asof_yyyymmdd:
        params.append(("asOfDate", asof_yyyymmdd))
    return f"{ROOT}/{product_slug}/{HOLDINGS_AJAX_PATH}?{urlencode(params)}"


def parse_universe(text: str, source_url: str) -> dict[str, object]:
    seen: set[str] = set()
    entries: list[dict[str, str]] = []
    for match in LINK_PATTERN.finditer(text):
        href = (match.group("href") or "").strip()
        label = clean_text(match.group("label") or "")
        if not href:
            continue
        absolute = href if href.startswith("http") else urljoin(ROOT + "/", href.lstrip("/"))
        slug = absolute.split("ishares.com/", 1)[-1] if "ishares.com/" in absolute else absolute
        if slug in seen:
            continue
        seen.add(slug)
        ticker_match = TICKER_NEAR_LINK.search(label)
        ticker = ticker_match.group(1) if ticker_match else None
        entries.append({"ticker": ticker or "", "name": label, "product_url": slug})
    return {
        "provider": "ishares",
        "source_url": source_url,
        "mode": "universe",
        "entries": entries,
        "entry_count": len(entries),
    }


def parse_holdings(payload: bytes, source_url: str, product_slug: str, asof_yyyymmdd: str | None, file_type: str) -> dict[str, object]:
    if file_type == "json":
        try:
            data = json.loads(payload.decode("utf-8-sig", errors="replace"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"iShares JSON payload could not be parsed: {exc}") from exc
        return {
            "provider": "ishares",
            "source_url": source_url,
            "mode": "holdings",
            "product_url": product_slug,
            "asof_date": asof_yyyymmdd,
            "file_type": "json",
            "payload": data,
        }
    text = payload.decode("utf-8-sig", errors="replace")
    return {
        "provider": "ishares",
        "source_url": source_url,
        "mode": "holdings",
        "product_url": product_slug,
        "asof_date": asof_yyyymmdd,
        "file_type": "csv",
        "csv_text": text,
    }


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if args.mode == "universe":
            payload = fetch_bytes(LANDING_PAGE_URL)
            if args.format == "raw":
                output_path.write_bytes(payload)
                print(f"Saved raw iShares universe HTML to {output_path}")
                print(f"URL: {LANDING_PAGE_URL}")
                return 0
            artifact = parse_universe(payload.decode("utf-8", errors="replace"), LANDING_PAGE_URL)
            output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Saved parsed iShares universe to {output_path}")
            print(f"entries={artifact['entry_count']}")
            return 0

        product_slug = resolve_product_url(args)
        asof_yyyymmdd = normalize_asof(args.asof) if args.mode == "history" else None
        if args.mode == "history" and asof_yyyymmdd is None:
            raise ValueError("--asof YYYY-MM-DD is required for history mode.")

        source_url = build_holdings_url(product_slug, args.file_type, asof_yyyymmdd)
        payload = fetch_bytes(source_url)
        if args.format == "raw":
            output_path.write_bytes(payload)
            print(f"Saved raw iShares payload to {output_path}")
            print(f"URL: {source_url}")
            return 0

        artifact = parse_holdings(payload, source_url, product_slug, asof_yyyymmdd, args.file_type)
        output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed iShares artifact to {output_path}")
        print(f"product={product_slug} asof={asof_yyyymmdd or 'current'} file_type={args.file_type}")
        return 0
    except (HTTPError, URLError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
