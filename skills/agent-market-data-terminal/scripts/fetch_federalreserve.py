#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


USER_AGENT = "Codex agent-market-data-terminal/1.0"
DOWNLOAD_BASE = "https://www.federalreserve.gov/datadownload/Output.aspx"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Federal Reserve DDP releases (SDMX-XML ZIP).")
    parser.add_argument(
        "--release",
        default="H15",
        help="DDP release identifier (e.g. H10, H15, G17, Z1). Defaults to H15.",
    )
    parser.add_argument("--url", help="Optional explicit override URL.")
    parser.add_argument("--format", required=True, choices=["raw", "parsed"])
    parser.add_argument("--output", required=True, help="Output path.")
    return parser.parse_args()


def build_url(release: str) -> str:
    query = urlencode([("filetype", "zip"), ("rel", release.upper())])
    return f"{DOWNLOAD_BASE}?{query}"


def fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=180) as response:
        return response.read()


def strip_namespace(tag: str) -> str:
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def parse_sdmx_xml(xml_bytes: bytes, member_name: str) -> list[dict[str, object]]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise RuntimeError(f"Failed to parse SDMX XML member {member_name!r}: {exc}") from exc

    rows: list[dict[str, object]] = []

    def walk(element: ET.Element, inherited: dict[str, str]) -> None:
        tag = strip_namespace(element.tag)
        attrs = {k: v for k, v in element.attrib.items()}
        local_attrs = dict(inherited)
        if tag == "Series":
            local_attrs.update(attrs)
        if tag == "Obs":
            row = dict(local_attrs)
            row.update(attrs)
            row["_member"] = member_name
            rows.append(row)
            return
        for child in element:
            walk(child, local_attrs)

    walk(root, {})
    return rows


def parse_archive(payload: bytes, source_url: str, release: str) -> dict[str, object]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = archive.namelist()
        xml_members = [m for m in members if m.lower().endswith(".xml")]
        if not xml_members:
            raise RuntimeError("Federal Reserve ZIP did not contain any XML members.")
        all_rows: list[dict[str, object]] = []
        per_member_counts: dict[str, int] = {}
        for member in xml_members:
            with archive.open(member) as fp:
                xml_bytes = fp.read()
            member_rows = parse_sdmx_xml(xml_bytes, member)
            per_member_counts[member] = len(member_rows)
            all_rows.extend(member_rows)

    return {
        "provider": "federalreserve",
        "source_url": source_url,
        "release": release.upper(),
        "archive_members": members,
        "xml_members": xml_members,
        "rows_per_member": per_member_counts,
        "rows": all_rows,
        "row_count": len(all_rows),
    }


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_url = args.url or build_url(args.release)

    try:
        payload = fetch_bytes(source_url)
        if args.format == "raw":
            output_path.write_bytes(payload)
            print(f"Saved raw Federal Reserve archive to {output_path}")
            print(f"URL: {source_url}")
            return 0

        artifact = parse_archive(payload, source_url, args.release)
        output_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved parsed Federal Reserve artifact to {output_path}")
        print(f"release={artifact['release']} rows={artifact['row_count']}")
        return 0
    except (HTTPError, URLError, OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
