#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "http://vixcentral.com"
PLACEHOLDER_RESPONSES = {"hello", "hello historical"}
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
IMPORTANT_LIVE_INDEXES = {
    "time_data": 1,
    "last": 2,
    "open": 3,
    "bid": 4,
    "high": 5,
    "ask": 6,
    "low": 7,
    "vix": 8,
    "vxv": 9,
    "vxst": 10,
    "vxmt": 11,
    "vcurve": 12,
    "vixmo": 13,
    "weeklies": 14,
}
XHR_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": f"{BASE_URL}/",
    "Origin": BASE_URL,
}
USER_AGENT = "Codex agent-market-data-terminal/1.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch VIXCentral live or historical data.")
    parser.add_argument("--mode", required=True, choices=["live", "historical"], help="Fetch live term structure or a historical curve.")
    parser.add_argument("--date", help="Historical date in YYYY-MM-DD.")
    parser.add_argument("--days", type=int, help="Bulk historical days for /historical/?days=N.")
    parser.add_argument("--output", required=True, help="Output JSON path.")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.mode == "historical":
        if args.date and args.days:
            raise ValueError("Use either --date or --days for historical mode, not both.")
        if not args.date and not args.days:
            raise ValueError("Historical mode requires --date or --days.")
        if args.date:
            date.fromisoformat(args.date)
        if args.days is not None and args.days < 1:
            raise ValueError("--days must be >= 1.")
    elif args.date or args.days:
        raise ValueError("--date and --days are only valid with --mode historical.")


def fetch_text(url: str, *, headers: dict[str, str] | None = None) -> str:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    with urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def direct_route_for(args: argparse.Namespace) -> str:
    if args.mode == "live":
        return f"{BASE_URL}/ajax_update"
    if args.days is not None:
        return f"{BASE_URL}/historical/?{urlencode({'days': args.days})}"
    return f"{BASE_URL}/ajax_historical?{urlencode({'n1': args.date})}"


def parse_json_text(raw_text: str) -> Any | None:
    stripped = raw_text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def is_placeholder_or_invalid(raw_text: str, parsed: Any | None, *, accept_html: bool) -> bool:
    stripped = raw_text.strip().strip('"').strip().lower()
    if stripped in PLACEHOLDER_RESPONSES:
        return True
    if raw_text.lstrip().startswith("<!DOCTYPE") or raw_text.lstrip().startswith("<html"):
        return not accept_html
    if parsed is None:
        return not accept_html
    if isinstance(parsed, str) and parsed.strip().lower() in PLACEHOLDER_RESPONSES:
        return True
    return False


def decode_live_bundle(bundle: Any) -> dict[str, Any]:
    if not isinstance(bundle, list):
        return {}
    decoded: dict[str, Any] = {}
    for label, index in IMPORTANT_LIVE_INDEXES.items():
        decoded[label] = bundle[index] if index < len(bundle) else None
    return decoded


def decode_historical_curve(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        return []
    try:
        start_month = int(payload[0])
    except (TypeError, ValueError):
        return []

    curve: list[dict[str, Any]] = []
    for offset, value in enumerate(payload[1:]):
        month_index = (start_month + offset - 1) % 12
        curve.append(
            {
                "month": MONTH_NAMES[month_index],
                "value": value,
                "is_active": isinstance(value, (int, float)) and value > 0,
            }
        )
    return curve


def import_requests_module():
    try:
        return importlib.import_module("requests")
    except ImportError as exc:
        raise RuntimeError("VIXCentral session fallback requires the 'requests' package.") from exc


def fetch_with_requests_session(endpoint: str) -> str:
    requests = import_requests_module()
    session = requests.Session()

    home_response = session.get(f"{BASE_URL}/", headers={"User-Agent": USER_AGENT}, timeout=60)
    home_response.raise_for_status()

    response = session.get(endpoint, headers={"User-Agent": USER_AGENT, **XHR_HEADERS}, timeout=60)
    response.raise_for_status()
    return response.text


def fetch_with_curl_cffi(endpoint: str) -> str:
    try:
        curl_requests = importlib.import_module("curl_cffi.requests")
        curl_module = importlib.import_module("curl_cffi")
    except ImportError as exc:
        raise RuntimeError("Optional VIXCentral TLS impersonation fallback requires the 'curl_cffi' package.") from exc

    http_version = getattr(curl_module, "CurlHttpVersion").V1_1
    session = curl_requests.Session()

    home_response = session.get(
        f"{BASE_URL}/",
        impersonate="chrome",
        http_version=http_version,
        headers={"User-Agent": USER_AGENT},
        timeout=60,
    )
    if getattr(home_response, "status_code", 200) >= 400:
        raise RuntimeError(f"curl_cffi homepage priming failed with status {home_response.status_code}")

    response = session.get(
        endpoint,
        impersonate="chrome",
        http_version=http_version,
        headers={"User-Agent": USER_AGENT, **XHR_HEADERS},
        timeout=60,
    )
    if getattr(response, "status_code", 200) >= 400:
        raise RuntimeError(f"curl_cffi endpoint fetch failed with status {response.status_code}")
    return response.text


def fetch_payload(args: argparse.Namespace, endpoint: str) -> tuple[Any, str, str]:
    accept_html = args.mode == "historical" and args.days is not None

    raw_text = fetch_text(endpoint, headers=XHR_HEADERS)
    parsed = parse_json_text(raw_text)
    if not is_placeholder_or_invalid(raw_text, parsed, accept_html=accept_html):
        return parsed if parsed is not None else raw_text, raw_text, "direct"

    raw_text = fetch_with_requests_session(endpoint)
    parsed = parse_json_text(raw_text)
    if not is_placeholder_or_invalid(raw_text, parsed, accept_html=accept_html):
        return parsed if parsed is not None else raw_text, raw_text, "requests_session"

    raw_text = fetch_with_curl_cffi(endpoint)
    parsed = parse_json_text(raw_text)
    if not is_placeholder_or_invalid(raw_text, parsed, accept_html=accept_html):
        return parsed if parsed is not None else raw_text, raw_text, "curl_cffi"

    raise RuntimeError("VIXCentral returned placeholder or non-JSON content after direct, session-primed, and curl_cffi retries.")


def build_output(args: argparse.Namespace, endpoint: str, raw_text: str, payload: Any, fetch_strategy: str) -> dict[str, Any]:
    used_fallback = fetch_strategy != "direct"
    output: dict[str, Any] = {
        "source": "vixcentral",
        "mode": args.mode,
        "endpoint": endpoint,
        "used_browser_fallback": used_fallback,
        "fetch_strategy": fetch_strategy,
        "raw": payload,
    }
    if args.date:
        output["date"] = args.date
    if args.days is not None:
        output["days"] = args.days

    if args.mode == "live" and isinstance(payload, list):
        output["decoded"] = decode_live_bundle(payload)
    elif args.mode == "historical" and isinstance(payload, list):
        output["decoded"] = {
            "start_month": payload[0] if payload else None,
            "curve": decode_historical_curve(payload),
        }
    else:
        output["raw_text"] = raw_text

    return output


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
        endpoint = direct_route_for(args)
        payload, raw_text, fetch_strategy = fetch_payload(args, endpoint)
        artifact = build_output(args, endpoint, raw_text, payload, fetch_strategy)
    except (HTTPError, URLError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved VIXCentral artifact to {output_path}")
    if artifact.get("fetch_strategy") != "direct":
        print(f"Fallback strategy used: {artifact['fetch_strategy']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
