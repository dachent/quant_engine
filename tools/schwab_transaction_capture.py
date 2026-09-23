"""Capture Schwab all-time transaction pages from an authenticated browser.

The live path attaches to an owner-authenticated browser. Authentication headers
remain in memory. Output is create-only and uses the existing WCH transaction
shape consumed by ``quant_engine.schwab.generation``.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.web_contracts import SCHWAB_CONTRACT, WebContractError


_TRANSACTION_URL = (
    "https://ausgateway.schwab.com/api/is.TransactionHistoryWeb/"
    "TransactionHistoryInterface/TransactionHistory/brokerage/transactions"
)
_POSITIONS_URL = "https://client.schwab.com/app/accounts/positions/"
_ACCOUNT_ID = re.compile(r"account-[0-9a-f]{16}\Z")
_TRANSACTION_TYPES = (
    "Adjustments",
    "AtmActivity",
    "BillPay",
    "Checks",
    "CorporateActions",
    "Deposits",
    "DividendsAndCapitalGains",
    "ElectronicTransfers",
    "Fees",
    "Interest",
    "Misc",
    "SecurityTransfers",
    "Taxes",
    "Trades",
    "VisaDebitCard",
    "Withdrawals",
)
_AUTH_HEADER_NAMES = (
    "authorization",
    "correlatorid",
    "schwab-channelcode",
    "schwab-client-appid",
    "schwab-client-channel",
    "schwab-client-correlid",
    "schwab-clientapp-name",
    "schwab-env",
    "schwab-environment",
    "schwab-resource-version",
)
_HEADER_CASE = {
    "authorization": "Authorization",
    "correlatorid": "CorrelatorID",
    "schwab-channelcode": "Schwab-ChannelCode",
    "schwab-client-appid": "Schwab-Client-AppId",
    "schwab-client-channel": "Schwab-Client-Channel",
    "schwab-client-correlid": "Schwab-Client-CorrelId",
    "schwab-clientapp-name": "Schwab-ClientApp-Name",
    "schwab-env": "Schwab-Env",
    "schwab-environment": "Schwab-Environment",
    "schwab-resource-version": "Schwab-Resource-Version",
}


class SchwabCaptureError(ValueError):
    """The retained account scope or one transaction page is invalid."""


@dataclass(frozen=True)
class AccountTarget:
    """One retained provider account mapped to its sanitized account ID."""

    internal_account_id: str
    provider_account_id: str
    provider_account_nickname: str


PageRequester = Callable[[str, dict[str, object]], tuple[int, object]]


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SchwabCaptureError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchwabCaptureError(f"{name} must be nonempty text")
    return value.strip()


def _write_new(path: Path, value: object) -> tuple[str, int]:
    payload = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except OSError as exc:
        raise SchwabCaptureError(f"capture artifact already exists: {path}") from exc
    return hashlib.sha256(payload).hexdigest(), len(payload)


def _artifact(root: Path, path: Path, value: object) -> dict[str, object]:
    digest, size = _write_new(path, value)
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": digest,
        "size_bytes": size,
    }


def _redact(value: object) -> object:
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for key in sorted(value):
            lowered = key.casefold()
            item = value[key]
            if lowered in SCHWAB_CONTRACT.sensitive_fields or any(
                marker in lowered
                for marker in ("authorization", "cookie", "password", "secret", "token")
            ):
                sanitized[key] = None if item is None else "[REDACTED]"
            else:
                sanitized[key] = _redact(item)
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SchwabCaptureError("provider response contains an unsupported value")


def _cursor_identity(value: object) -> str:
    try:
        return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise SchwabCaptureError(
            "transaction bookmark is not JSON serializable"
        ) from exc


def _request(target: AccountTarget, bookmark: object) -> dict[str, object]:
    if bookmark is not None and not isinstance(bookmark, (str, Mapping)):
        raise SchwabCaptureError("transaction bookmark has an unsupported shape")
    body: dict[str, object] = {
        "accountNickname": target.provider_account_nickname,
        "bookmark": bookmark,
        "exportType": "Csv",
        "includeOptionsInSearch": False,
        "isSpsLinkedUkAccount": False,
        "selectedAccountId": target.provider_account_id,
        "selectedTransactionTypes": list(_TRANSACTION_TYPES),
        "shouldPaginate": True,
        "sortColumn": "Date",
        "sortDirection": "Descending",
        "symbol": "",
        "timeFrame": "All",
    }
    try:
        endpoint = SCHWAB_CONTRACT.route("POST", _TRANSACTION_URL)
        if endpoint.validate_request is None:
            raise SchwabCaptureError("Schwab transaction request is not reviewed")
        # The reviewed first-page request has bookmark=null. Live Schwab returns
        # an opaque object cursor on subsequent pages; it is replayed unchanged.
        reviewed = dict(body)
        if isinstance(bookmark, Mapping):
            reviewed["bookmark"] = None
        endpoint.validate_request(reviewed)
    except WebContractError as exc:
        raise SchwabCaptureError(
            "Schwab transaction request differs from contract"
        ) from exc
    return body


def _sanitized_request(body: Mapping[str, object]) -> dict[str, object]:
    sanitized = dict(body)
    sanitized["accountNickname"] = "[REDACTED]"
    sanitized["selectedAccountId"] = "[REDACTED]"
    if sanitized.get("bookmark") is not None:
        sanitized["bookmark"] = "[REDACTED]"
    return sanitized


def capture_transaction_history(
    *,
    request_page: PageRequester,
    targets: Sequence[AccountTarget],
    output_root: Path,
    captured_at: str | None = None,
) -> dict[str, object]:
    """Capture every retained account through a provider-terminal null cursor."""

    if not targets:
        raise SchwabCaptureError("retained account scope is empty")
    internal_ids = [target.internal_account_id for target in targets]
    provider_ids = [target.provider_account_id for target in targets]
    if len(set(internal_ids)) != len(internal_ids) or len(set(provider_ids)) != len(
        provider_ids
    ):
        raise SchwabCaptureError("retained account scope is duplicated")
    if any(_ACCOUNT_ID.fullmatch(value) is None for value in internal_ids):
        raise SchwabCaptureError("internal account ID is invalid")
    if any(
        value == "[REDACTED]"
        for target in targets
        for value in (target.provider_account_id, target.provider_account_nickname)
    ):
        raise SchwabCaptureError("retained account mapping source is redacted")
    if output_root.exists():
        raise SchwabCaptureError("capture output root already exists")
    output_root.mkdir(parents=True)

    started = captured_at or datetime.now(timezone.utc).isoformat()
    artifacts: list[dict[str, object]] = []
    accounts: dict[str, dict[str, object]] = {}
    for target in sorted(targets, key=lambda item: item.internal_account_id):
        bookmark: object = None
        seen_cursors: set[str] = set()
        transactions: list[object] = []
        page_number = 0
        while True:
            body = _request(target, bookmark)
            status, response_value = request_page(target.provider_account_id, body)
            if status != 200:
                raise SchwabCaptureError(
                    f"Schwab transaction request failed for {target.internal_account_id}: {status}"
                )
            response = _object(response_value, "transaction response")
            rows = response.get("brokerageTransactions")
            if not isinstance(rows, list):
                raise SchwabCaptureError("transaction response rows are missing")
            transactions.extend(_redact(row) for row in rows)

            page_dir = output_root / "pages" / target.internal_account_id
            artifacts.append(
                _artifact(
                    output_root,
                    page_dir / f"{page_number:04d}.request.json",
                    _sanitized_request(body),
                )
            )
            artifacts.append(
                _artifact(
                    output_root,
                    page_dir / f"{page_number:04d}.json",
                    _redact(response),
                )
            )
            page_number += 1

            if "bookmark" not in response:
                raise SchwabCaptureError(
                    "transaction response omits the provider terminal bookmark"
                )
            next_bookmark = response["bookmark"]
            if next_bookmark is None:
                break
            if not isinstance(next_bookmark, (str, Mapping)):
                raise SchwabCaptureError(
                    "transaction bookmark has an unsupported shape"
                )
            cursor_identity = _cursor_identity(next_bookmark)
            if cursor_identity in seen_cursors:
                raise SchwabCaptureError(
                    "transaction bookmark repeated before terminal page"
                )
            seen_cursors.add(cursor_identity)
            bookmark = next_bookmark

        normalized_path = (
            output_root
            / "normalized"
            / "wch"
            / "transactions"
            / f"{target.internal_account_id}.json"
        )
        artifacts.append(_artifact(output_root, normalized_path, transactions))
        accounts[target.internal_account_id] = {
            "page_count": page_number,
            "status": "TERMINAL",
            "terminal": True,
            "transaction_count": len(transactions),
        }

    finished = captured_at or datetime.now(timezone.utc).isoformat()
    manifest: dict[str, object] = {
        "accounts": accounts,
        "artifacts": sorted(artifacts, key=lambda item: str(item["path"])),
        "context_mode": "default-attached",
        "finished_at": finished,
        "run_id": output_root.name,
        "started_at": started,
    }
    _write_new(output_root / "manifest.json", manifest)
    return manifest


def load_retained_targets(source_root: Path) -> tuple[AccountTarget, ...]:
    """Load the exact provider-to-internal mapping from a terminal capture."""

    manifest = _object(
        json.loads((source_root / "manifest.json").read_text(encoding="utf-8")),
        "retained manifest",
    )
    manifest_accounts = _object(manifest.get("accounts"), "retained accounts")
    discovered_nicknames: dict[str, str] | None = None
    targets: list[AccountTarget] = []
    for internal_account_id in sorted(manifest_accounts):
        state = _object(
            manifest_accounts[internal_account_id], "retained account state"
        )
        if state.get("terminal") is not True or state.get("status") != "TERMINAL":
            raise SchwabCaptureError("retained account history is not terminal")
        if _ACCOUNT_ID.fullmatch(internal_account_id) is None:
            raise SchwabCaptureError("retained internal account ID is invalid")
        request_path = source_root / "pages" / internal_account_id / "0000.request.json"
        request = _object(
            json.loads(request_path.read_text(encoding="utf-8")),
            "retained transaction request",
        )
        provider_account_id = _text(
            request.get("selectedAccountId"), "retained provider account ID"
        )
        retained_nickname = request.get("accountNickname")
        if provider_account_id == "[REDACTED]" or retained_nickname == "[REDACTED]":
            raise SchwabCaptureError("retained account mapping source is redacted")
        if isinstance(retained_nickname, str) and retained_nickname.strip():
            provider_account_nickname = retained_nickname.strip()
        else:
            if discovered_nicknames is None:
                discovery = _object(
                    json.loads(
                        (source_root / "snapshots/customer_accounts.json").read_text(
                            encoding="utf-8"
                        )
                    ),
                    "retained account discovery",
                )
                rows = discovery.get("brokerageAccounts")
                if not isinstance(rows, list):
                    raise SchwabCaptureError(
                        "retained account discovery brokerageAccounts must be a list"
                    )
                discovered_nicknames = {}
                for item in rows:
                    row = _object(item, "retained discovered account")
                    discovered_id = str(row.get("brokerageAccountId", "")).strip()
                    discovered_nickname = _text(
                        row.get("nickName"), "retained discovered account nickname"
                    )
                    if not discovered_id:
                        raise SchwabCaptureError(
                            "retained discovered account ID must be nonempty"
                        )
                    if discovered_id in discovered_nicknames:
                        raise SchwabCaptureError(
                            "retained account discovery contains a duplicate account ID"
                        )
                    discovered_nicknames[discovered_id] = discovered_nickname
            try:
                provider_account_nickname = discovered_nicknames[provider_account_id]
            except KeyError as exc:
                raise SchwabCaptureError(
                    "retained account request is absent from account discovery"
                ) from exc
        _provider_scope = provider_account_id
        targets.append(
            AccountTarget(
                internal_account_id=internal_account_id,
                provider_account_id=_provider_scope,
                provider_account_nickname=provider_account_nickname,
            )
        )
    return tuple(targets)


def _capture_auth_headers(page: Any) -> dict[str, str]:
    def is_schwab_api(request: Any) -> bool:
        return (
            request.method == "GET"
            and request.url.startswith("https://ausgateway.schwab.com/")
            and "/api/" in request.url
        )

    with page.expect_request(is_schwab_api) as pending:
        if page.url.startswith("https://client.schwab.com/"):
            page.reload(wait_until="domcontentloaded")
        else:
            page.goto(_POSITIONS_URL, wait_until="domcontentloaded")
    observed = {key.casefold(): value for key, value in pending.value.headers.items()}
    captured = {key: observed[key] for key in _AUTH_HEADER_NAMES if observed.get(key)}
    if not captured.get("authorization"):
        raise SchwabCaptureError("authenticated Schwab SPA request was not observed")
    return captured


def _playwright_requester(
    context: Any, auth_headers: Mapping[str, str]
) -> PageRequester:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": "https://client.schwab.com/",
        **{
            _HEADER_CASE[key]: value
            for key, value in auth_headers.items()
            if key in _HEADER_CASE
        },
    }

    def request_page(
        provider_account_id: str, body: dict[str, object]
    ) -> tuple[int, object]:
        scoped_headers = {**headers, "Schwab-Client-Ids": provider_account_id}
        response = context.request.post(
            _TRANSACTION_URL,
            headers=scoped_headers,
            data=json.dumps(body, separators=(",", ":")),
        )
        try:
            value = response.json()
        except Exception as exc:
            raise SchwabCaptureError("Schwab transaction response is not JSON") from exc
        return int(response.status), value

    return request_page


def capture_from_authenticated_browser(
    *, cdp_url: str, selected_source_root: Path, output_root: Path
) -> dict[str, object]:
    """Attach to the owner's existing browser and capture the retained scope."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SchwabCaptureError(
            "Playwright is required for live Schwab capture"
        ) from exc

    targets = load_retained_targets(selected_source_root)
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise SchwabCaptureError("attached browser has no default context")
        context = browser.contexts[0]
        pages = [
            page
            for page in context.pages
            if page.url.startswith("https://client.schwab.com/")
        ]
        if not pages:
            raise SchwabCaptureError("open an authenticated Schwab page before capture")
        auth_headers = _capture_auth_headers(pages[0])
        return capture_transaction_history(
            request_page=_playwright_requester(context, auth_headers),
            targets=targets,
            output_root=output_root,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp-url", required=True)
    parser.add_argument("--selected-source-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = capture_from_authenticated_browser(
        cdp_url=args.cdp_url,
        selected_source_root=args.selected_source_root,
        output_root=args.output_root,
    )
    print(
        json.dumps(
            {
                "account_count": len(manifest["accounts"]),
                "output_root": str(args.output_root),
                "terminal": all(
                    bool(item["terminal"]) for item in manifest["accounts"].values()
                ),
                "transaction_count": sum(
                    int(item["transaction_count"])
                    for item in manifest["accounts"].values()
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
