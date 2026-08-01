#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


USER_AGENT = "Codex agent-market-data-terminal/1.0"
REST_BASE = "https://mymarketnews.ams.usda.gov/mymarketnews-api/v1/"
PUBLIC_DATA_URL = "https://mymarketnews.ams.usda.gov/public_data"

CURATED_SLUGS: dict[str, str] = {
    "corn": "CN_GR210",
    "wheat": "WH_GR110",
    "soybeans": "SB_GR110",
    "soybean_meal": "SB_GR210",
    "soybean_oil": "SB_GR310",
    "live_cattle": "LM_CT155",
    "lean_hogs": "LM_HG200",
    "cotton": "CN_CT100",
}

LINK_PATTERN = re.compile(
    r'<a[^>]+href\s*=\s*(?:"(?P<double>[^"]+)"|\'(?P<single>[^\']+)\'|(?P<bare>[^\s>"\']+))[^>]*>(?P<label>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TAG_PATTERN = re.compile(r"<[^>]+>")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch USDA AMS Market News reports or the bulk-data catalog.")
    parser.add_argument("--slug", help="AMS report slug, e.g. CN_GR210 (corn).")
    parser.add_argument("--commodity", choices=sorted(CURATED_SLUGS.keys()), help="Curated commodity name; resolves to a known slug.")
    parser.add_argument("--bulk", action="store_true", help="Fetch the /public_data directory listing.")
    parser.add_argument("--list-slugs", action="store_true", help="Print the curated commodity-to-slug mapping and exit.")
    parser.add_argument("--url", help="Optional explicit override URL.")
    parser.add_argument("--format", choices=["raw", "parsed"], help="Required unless --list-slugs is set.")
    parser.add_argument("--output", help="Output path. Required unless --list-slugs is set.")
    return parser.parse_args()


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=120) as response:
        return response.read()


def clean_text(value: str) -> str:
    text = TAG_PATTERN.sub(" ", value)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def resolve_slug(args: argparse.Namespace) -> str | None:
    if args.slug:
        return args.slug.strip()
    if args.commodity:
        return CURATED_SLUGS[args.commodity]
    return None


def build_url(args: argparse.Namespace) -> str:
    if args.url:
        return args.url
    if args.bulk:
        return PUBLIC_DATA_URL
    slug = resolve_slug(args)
    if slug is None:
        raise ValueError("Provide --slug, --commodity, --bulk, --url, or --list-slugs.")
    return urljoin(REST_BASE, slug)


def parse_directory_listing(text: str, source_url: str) -> dict[str, object]:
    entries: list[dict[str, str]] = []
    for match in LINK_PATTERN.finditer(text):
        href = match.group("double") or match.group("single") or match.group("bare") or ""
        label = clean_text(match.group("label") or "")
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(source_url, href)
        entries.append({"href": absolute, "label": label})
    return {
        "provider": "usda_ams",
        "source_url": source_url,
        "mode": "bulk_listing",
        "entries": entries,
        "entry_count": len(entries),
    }


def parse_rest_payload(text: str, source_url: str, slug: str | None) -> dict[str, object]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AMS REST endpoint did not return JSON: {exc}") from exc
    return {
        "provider": "usda_ams",
        "source_url": source_url,
        "mode": "rest",
        "slug": slug,
        "payload": payload,
    }


def main() -> int:
    args = parse_args()

    if args.list_slugs:
        print(json.dumps({"provider": "usda_ams", "curated_slugs": CURATED_SLUGS}, indent=2))
        return 0

    if not args.format or not args.output:
        print("Error: --format and --output are required unless --list-slugs is set.", file=sys.stderr)
        return 1

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        source_url = build_url(args)
        payload = fetch_bytes(source_url)
        if args.format == "raw":
            output_path.write_bytes(payload)
            print(f"Saved raw USDA AMS payload to {output_path}")
            print(f"URL: {source_url}")
            return 0

        text = payload.decode("utf-8", errors="replace")
        if args.bulk:
            artifact = parse_directory_listing(text, source_url)
        else:
            artifact = parse_rest_payload(text, source_url, resolve_slug(args))

        output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed USDA AMS artifact to {output_path}")
        if "entry_count" in artifact:
            print(f"entries={artifact['entry_count']}")
        else:
            print(f"slug={artifact.get('slug')}")
        return 0
    except (HTTPError, URLError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
