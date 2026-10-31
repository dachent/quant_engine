"""Record GrizzlyBulls responses from an owner-authenticated browser.

The recorder attaches to a dedicated Edge/Chrome debug port that the owner signed
in to. It is origin-limited to https://grizzlybulls.com, never reads or persists
request headers or cookies, never reads identity or billing routes, and stores
reviewed responses as RAW bytes with a SHA-256 in a create-only run directory. The
sanitized and parsed layer is derived afterwards and never replaces the raw bytes.

Modes:
  discover  value-free metadata and body shape of every grizzlybulls.com response,
            used once to find the gated download routes.
  capture   raw bodies for contract-routed responses and gated CSV downloads, then
            the derived layer, then a manifest written last.
  verify    re-hash a finished run and re-derive its summaries from the raw bytes.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from quant_engine.web_contracts import (
    WebContractError,
    ORIGIN,
    SLUG_PATTERN,
    build_contract,
    classify,
    parse_json_bytes,
    sanitization_audit,
    summarize_capability,
)


STATUS_COMPLETE = "COMPLETE"
STATUS_FAILED = "FAILED_REQUIRED_CAPABILITY"
STATUS_MISSING = "INCOMPLETE_MISSING_CAPABILITY"
_MENU_BUTTON = "Download model trade history CSV"
_MENU_ITEMS = (
    ("blended", "Blended history (backtest + live)"),
    ("live", "Live history only"),
)
_QUERY_VALUE_CAPABILITIES = frozenset({"models.history", "models.historical"})
_AUTHENTICATED = _QUERY_VALUE_CAPABILITIES
#: modelsHistorical returned HTTP 500 for the signed-in owner on 2026-10-30; it is
#: contracted and recorded, but a failure there does not fail the run.
_OPTIONAL_CAPABILITIES = frozenset({"models.historical"})


class GrizzlyCaptureError(ValueError):
    """The run violates the capture contract and must not be used."""


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _write_new(path: Path, payload: bytes) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        raise GrizzlyCaptureError(f"capture artifact already exists: {path}") from exc
    return {"sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}


def shape_of(value: object) -> object:
    """Type skeleton of a JSON value: keys and types, never values."""

    if isinstance(value, dict):
        return {key: shape_of(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        shapes = {json.dumps(shape_of(item), sort_keys=True) for item in value[:50]}
        return [json.loads(item) for item in sorted(shapes)][:3] + (
            [f"len={len(value)}"] if value else []
        )
    return type(value).__name__


def assert_output_root(root: Path) -> None:
    """The run directory must be new and outside any Git worktree."""

    resolved = root.resolve()
    if resolved.exists():
        raise GrizzlyCaptureError("capture output root already exists")
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            raise GrizzlyCaptureError("capture output root is inside a Git worktree")


class Recorder:
    """Decide per response what, if anything, is persisted. Pure: no browser."""

    def __init__(self, root: Path, *, mode: str) -> None:
        if mode not in {"discover", "capture"}:
            raise GrizzlyCaptureError("mode must be discover or capture")
        assert_output_root(root)
        root.mkdir(parents=True)
        self.root = root
        self.mode = mode
        self.contract = build_contract()
        self.started = datetime.now(timezone.utc).isoformat()
        self.entries: list[dict[str, object]] = []
        self.artifacts: list[dict[str, object]] = []
        self.skipped_offorigin = 0
        self.failures: list[str] = []

    def handle(
        self,
        *,
        method: str,
        url: str,
        status: int,
        content_type: str,
        request_header_names: Sequence[str],
        read_body: Any,
    ) -> None:
        """Record one response. ``read_body`` is called only when allowed."""

        verdict, capability = classify(method, url, self.contract)
        if verdict == "offorigin":
            self.skipped_offorigin += 1
            return
        parsed = urlsplit(url)
        names = sorted({n.casefold() for n in request_header_names})
        entry: dict[str, object] = {
            "ordinal": len(self.entries),
            "method": method.upper(),
            "path": parsed.path,
            "query_keys": sorted(key for key, _ in parse_qsl(parsed.query)),
            "status": status,
            "content_type": content_type.split(";")[0].strip().lower(),
            "verdict": verdict,
            "capability": capability,
            "request_header_names": names,
        }
        if capability in _QUERY_VALUE_CAPABILITIES:
            # slug and mode are validated vocabulary, not secrets.
            entry["query"] = dict(parse_qsl(parsed.query))
        self.entries.append(entry)
        if verdict == "denied":
            return
        if self.mode == "discover":
            self._discover(entry, read_body)
        elif verdict == "routed" and capability is not None:
            self._persist_raw(entry, capability, names, read_body)

    def _discover(self, entry: dict[str, object], read_body: Any) -> None:
        if entry["content_type"] in {"application/json", "text/csv"}:
            body = bytes(read_body())
            entry["body_sha256"] = hashlib.sha256(body).hexdigest()
            entry["body_bytes"] = len(body)
            if entry["content_type"] == "application/json":
                try:
                    entry["body_shape"] = shape_of(parse_json_bytes(body))
                except WebContractError:
                    entry["body_shape"] = "not-json"
            else:
                first = body.decode("utf-8-sig", "replace").splitlines()[:1]
                entry["body_first_line_columns"] = (
                    len(first[0].split(",")) if first else 0
                )

    def _fail(self, entry: dict[str, object], reason: str) -> None:
        capability = str(entry["capability"])
        entry["failure"] = reason
        if capability not in _OPTIONAL_CAPABILITIES:
            self.failures.append(f"{capability}: {reason}")

    def _persist_raw(
        self,
        entry: dict[str, object],
        capability: str,
        header_names: Sequence[str],
        read_body: Any,
    ) -> None:
        status = entry["status"]
        if not isinstance(status, int) or not 200 <= status < 300:
            # Error bodies are short vendor messages, not data; keep the text.
            entry["error_body"] = bytes(read_body())[:500].decode("utf-8", "replace")
            self._fail(entry, f"HTTP {status}")
            return
        if capability in _AUTHENTICATED and "authorization" not in header_names:
            self._fail(entry, "authenticated request evidence is missing")
            return
        body = bytes(read_body())
        if not body:
            self._fail(entry, "empty body")
            return
        query = entry.get("query")
        suffix = (
            "-".join(str(v) for v in query.values()) if isinstance(query, dict) else ""
        )
        name = (
            f"{entry['ordinal']:04d}-{capability}{'-' + suffix if suffix else ''}.body"
        )
        record = _write_new(self.root / "raw" / name, body)
        entry.update(record)
        entry["raw_path"] = f"raw/{name}"
        self.artifacts.append({"path": f"raw/{name}", **record})

    def finish(self, *, expected_slugs: Sequence[str] = ()) -> dict[str, object]:
        """Derive from raw bytes, then write the manifest last."""

        derived: list[dict[str, object]] = []
        captured: set[str] = set()
        for entry in self.entries:
            raw_path = entry.get("raw_path")
            if not isinstance(raw_path, str):
                continue
            capability = str(entry["capability"])
            raw = (self.root / raw_path).read_bytes()
            record: dict[str, object] = {
                "raw_path": raw_path,
                "capability": capability,
            }
            try:
                record["summary"] = summarize_capability(capability, raw)
                if capability != "models.history":
                    audit = sanitization_audit(parse_json_bytes(raw))
                    if audit["data_fields_masked"] or audit["unexplained_removals"]:
                        raise WebContractError("sanitization would alter data fields")
                    record["sanitization_audit"] = audit
            except WebContractError as exc:
                self._fail(entry, f"parse: {exc}")
                record["failure"] = str(exc)
            else:
                query = entry.get("query")
                tag = ""
                if isinstance(query, dict):
                    tag = f"{capability}:{query.get('slug')}:{query.get('mode', '')}"
                captured.update({capability, tag})
            derived.append(record)
        if self.mode == "capture":
            self.artifacts.append(
                {
                    "path": "derived/summary.json",
                    **_write_new(
                        self.root / "derived" / "summary.json", _canonical(derived)
                    ),
                }
            )
        manifest: dict[str, object] = {
            "schema": "grizzlybulls-capture/1",
            "mode": self.mode,
            "origin": ORIGIN,
            "run_id": self.root.name,
            "started_at": self.started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": self._status(captured, expected_slugs),
            "failures": self.failures,
            "skipped_offorigin_count": self.skipped_offorigin,
            "entries": self.entries,
            "artifacts": sorted(self.artifacts, key=lambda a: str(a["path"])),
        }
        _write_new(self.root / "manifest.json", _canonical(manifest))
        return manifest

    def _status(self, captured: set[str], expected_slugs: Sequence[str]) -> str:
        if self.mode == "discover":
            return "DISCOVERY"
        if self.failures:
            return STATUS_FAILED
        needed = {"models.catalog"}
        for slug in expected_slugs:
            needed.add(f"models.history:{slug}:blended")
            needed.add(f"models.history:{slug}:live")
        needed.add("models.page")
        return STATUS_COMPLETE if needed <= captured else STATUS_MISSING


def verify_run(root: Path) -> dict[str, object]:
    """Re-hash every artifact, reject strays, and re-derive summaries from raw."""

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    listed = {item["path"]: item for item in manifest["artifacts"]}
    on_disk = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    if on_disk != set(listed):
        raise GrizzlyCaptureError("run directory differs from its manifest")
    for path, item in listed.items():
        payload = (root / path).read_bytes()
        if hashlib.sha256(payload).hexdigest() != item["sha256"]:
            raise GrizzlyCaptureError(f"artifact hash differs: {path}")
    if manifest["mode"] == "capture":
        derived = json.loads((root / "derived" / "summary.json").read_text("utf-8"))
        for record in derived:
            fresh = summarize_capability(
                record["capability"], (root / record["raw_path"]).read_bytes()
            )
            if fresh != record["summary"]:
                raise GrizzlyCaptureError("derived summary differs from raw bytes")
    return {"status": manifest["status"], "artifact_count": len(listed)}


def _cdp_run(args: argparse.Namespace) -> dict[str, object]:  # pragma: no cover
    try:
        sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
    except ImportError as exc:
        raise GrizzlyCaptureError("Playwright is required for live capture") from exc

    recorder = Recorder(args.output_root, mode=args.command)
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(args.cdp_url)
        if not browser.contexts:
            raise GrizzlyCaptureError("attached browser has no default context")
        context = browser.contexts[0]

        def on_response(response: Any) -> None:
            request = response.request
            headers = [name for name in request.headers if name.casefold()]
            recorder.handle(
                method=request.method,
                url=response.url,
                status=response.status,
                content_type=response.headers.get("content-type", ""),
                request_header_names=headers,
                read_body=response.body,
            )

        context.on("response", on_response)
        page = context.new_page()
        for slug in args.slug:
            if re.fullmatch(SLUG_PATTERN, slug) is None:
                raise GrizzlyCaptureError("slug is invalid")
            page.goto(f"{ORIGIN}/models/{slug}", wait_until="networkidle")
            if args.passive or (args.command != "capture" and not args.click_menu):
                continue
            # The page makes its own authenticated request; this driver never
            # reads the token and never issues a request of its own.
            for mode, label in _MENU_ITEMS:
                page.get_by_role("button", name=_MENU_BUTTON).click()
                with page.expect_response(
                    lambda r, m=mode: (
                        urlsplit(r.url).path == "/api/modelTradeHistory"
                        and dict(parse_qsl(urlsplit(r.url).query)).get("mode") == m
                    ),
                    timeout=30000,
                ):
                    page.get_by_role("menuitem", name=label).click()
        if args.manual_seconds:
            # Fallback: the owner clicks the menu items in the attached tab.
            page.wait_for_timeout(args.manual_seconds * 1000)
        page.close()
    return recorder.finish(expected_slugs=args.slug)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("discover", "capture"):
        item = sub.add_parser(name)
        item.add_argument("--cdp-url", required=True)
        item.add_argument("--output-root", required=True, type=Path)
        item.add_argument("--slug", action="append", default=["vix-ta-macro-advanced"])
        item.add_argument("--manual-seconds", type=int, default=0)
        item.add_argument("--click-menu", action="store_true")
        item.add_argument("--passive", action="store_true")
    check = sub.add_parser("verify")
    check.add_argument("--run-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "verify":
        result = verify_run(args.run_root)
    else:
        result = {"status": _cdp_run(args)["status"]}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] in {STATUS_COMPLETE, "DISCOVERY"} else 1


if __name__ == "__main__":
    sys.exit(main())
