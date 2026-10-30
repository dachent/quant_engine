"""The p123api wrapper stub: gates, allow-list, credentials; never touches a network."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from quant_engine.core.credentials import KeyVaultSource, SecretValue
from quant_engine.portfolio123 import capture_api as api
from quant_engine.portfolio123.capture_catalog import CaptureRefusal
from quant_engine.portfolio123.routing import ApiEffect, capability_policy

PLACEHOLDER_ID = "placeholder-key-id-0001"
PLACEHOLDER_KEY = "placeholder-key-value-0001"
NOW = datetime(2030, 1, 15, 12, tzinfo=timezone.utc)
REVIEW = "https://github.com/dachent/quant_engine/issues/14#issuecomment-1"


def _evidence(
    tmp_path: Path,
    *,
    operations: list[dict[str, str]] | None = None,
    budget: int = 10,
    calls: int = 3,
    name: str = "evidence.json",
    **override: Any,
) -> Path:
    document: dict[str, Any] = {
        "schema_version": 1,
        "operation": "api-read",
        "scope": {
            "operations": operations
            if operations is not None
            else [{"operation": "screen_run", "basis": api.BASIS_HAR_GAP}]
        },
        "review": {"identity": "synthetic-reviewer", "reference": REVIEW},
        "authority": {
            "status": "CURRENT",
            "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
        },
        "terms": {"version": api.TERMS_VERSION, "sha256": api.TERMS_SHA256},
        "allow_list": {"sha256": api.allow_list_sha256()},
        "metering": {"credit_budget": budget, "max_calls": calls, "retry_count": 0},
        "credit_table": {"captured_at": (NOW - timedelta(minutes=5)).isoformat()},
    }
    document.update(override)
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _authorize(tmp_path: Path, **kwargs: Any) -> api.ApiGovernance:
    return api.authorize_api_read(
        _evidence(tmp_path, **kwargs),
        receipt_root=tmp_path / "receipts",
        now=lambda: NOW,
    )


class FakeTransport:
    def __init__(self, credits_used: int = 2, quota: int = 98) -> None:
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.credits_used = credits_used
        self.quota = quota
        self.revealed: list[str] = []

    def invoke(
        self,
        operation: str,
        params: Mapping[str, object],
        *,
        key_id: SecretValue,
        key: SecretValue,
    ) -> api.ApiResponse:
        self.calls.append((operation, params))
        self.revealed += [key_id.reveal(), key.reveal()]
        return api.ApiResponse({"rows": []}, self.credits_used, self.quota)


def _credentials() -> dict[str, SecretValue]:
    return {"key_id": SecretValue(PLACEHOLDER_ID), "key": SecretValue(PLACEHOLDER_KEY)}


def _refusal(call: Callable[[], object]) -> str:
    with pytest.raises(CaptureRefusal) as caught:
        call()
    return caught.value.code


def test_allow_list_is_exactly_the_reviewed_read_only_operations() -> None:
    for operation, capability in api.OPERATION_CAPABILITY.items():
        policy = capability_policy(capability)
        assert policy.api_effect is ApiEffect.METERED_READ
        assert policy.api_operation == operation
    for forbidden in (
        "strategy_rebalance_commit",
        "strategy_transaction_import",
        "strategy_transaction_delete",
        "ai_factor_predict",
    ):
        assert forbidden not in api.OPERATION_CAPABILITY
    assert all(not name.startswith("_") for name in api.OPERATION_CAPABILITY)


def test_governance_accepts_valid_evidence_and_is_single_use(tmp_path: Path) -> None:
    governance = _authorize(tmp_path)
    assert governance.credit_budget == 10 and governance.max_calls == 3
    assert governance.receipt_path.is_file()
    assert _refusal(lambda: _authorize(tmp_path)) == "EVIDENCE_ALREADY_CONSUMED"


@pytest.mark.parametrize(
    "override,code",
    [
        ({"schema_version": 2}, "GOVERNANCE_INVALID"),
        ({"terms": {"version": "x", "sha256": "y"}}, "GOVERNANCE_INVALID"),
        ({"allow_list": {"sha256": "0" * 64}}, "GOVERNANCE_INVALID"),
        (
            {"metering": {"credit_budget": 0, "max_calls": 1, "retry_count": 0}},
            "GOVERNANCE_INVALID",
        ),
        (
            {"metering": {"credit_budget": 5, "max_calls": 1, "retry_count": 1}},
            "GOVERNANCE_INVALID",
        ),
        (
            {"review": {"identity": "x", "reference": "https://example.invalid/1"}},
            "GOVERNANCE_INVALID",
        ),
        ({"extra": 1}, "GOVERNANCE_INVALID"),
    ],
)
def test_governance_rejects_bad_evidence(
    tmp_path: Path, override: dict[str, Any], code: str
) -> None:
    assert _refusal(lambda: _authorize(tmp_path, **override)) == code
    assert not (tmp_path / "receipts").exists() or not list(
        (tmp_path / "receipts").iterdir()
    )


def test_governance_rejects_stale_missing_and_oversized_evidence(
    tmp_path: Path,
) -> None:
    stale = _evidence(tmp_path, name="stale.json")
    late = lambda: NOW + timedelta(hours=2)  # noqa: E731
    assert (
        _refusal(
            lambda: api.authorize_api_read(stale, receipt_root=tmp_path / "r", now=late)
        )
        == "GOVERNANCE_NOT_CURRENT"
    )
    absent = tmp_path / "absent.json"
    assert (
        _refusal(lambda: api.authorize_api_read(absent, receipt_root=tmp_path / "r"))
        == "GOVERNANCE_MISSING"
    )
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (70 * 1024))
    assert (
        _refusal(lambda: api.authorize_api_read(big, receipt_root=tmp_path / "r"))
        == "GOVERNANCE_INVALID"
    )


def test_har_first_unknown_and_mutation_scope_are_refused(tmp_path: Path) -> None:
    har_reviewed = [{"operation": "strategy_transactions", "basis": api.BASIS_HAR_GAP}]
    assert (
        _refusal(lambda: _authorize(tmp_path, operations=har_reviewed)) == "HAR_FIRST"
    )
    selected = [{"operation": "strategy_transactions", "basis": api.BASIS_OWNER}]
    governance = _authorize(tmp_path, operations=selected, name="selected.json")
    assert governance.operations == {"strategy_transactions": api.BASIS_OWNER}
    for operation in (
        "strategy_rebalance_commit",
        "strategy_transaction_delete",
        "request",
    ):
        scope = [{"operation": operation, "basis": api.BASIS_OWNER}]
        assert (
            _refusal(
                lambda: _authorize(tmp_path, operations=scope, name=f"{operation}.json")
            )
            == "GOVERNANCE_INVALID"
        )


def test_call_is_reserved_budgeted_and_recorded(tmp_path: Path) -> None:
    governance = _authorize(tmp_path, budget=5, calls=2)
    transport = FakeTransport(credits_used=2)
    capture = api.ReadOnlyApiCapture(governance, _credentials(), transport)
    result = capture.call("screen_run", {"screenId": 1}, estimated_credits=2)
    assert result.evidence.actual_credits == 2 and governance.spent_credits == 2
    assert transport.calls == [("screen_run", {"screenId": 1})]
    assert transport.revealed == [PLACEHOLDER_ID, PLACEHOLDER_KEY]
    ledger = governance.ledger_path.read_text()
    assert ledger.index("reserve") < ledger.index("complete")
    assert PLACEHOLDER_KEY not in ledger and PLACEHOLDER_ID not in ledger
    assert (
        _refusal(lambda: capture.call("screen_run", {}, estimated_credits=4))
        == "CREDIT_BUDGET_EXHAUSTED"
    )
    capture.call("screen_run", {}, estimated_credits=1)
    assert (
        _refusal(lambda: capture.call("screen_run", {}, estimated_credits=1))
        == "CALL_BUDGET_EXHAUSTED"
    )


def test_unapproved_and_unlisted_operations_never_reach_the_transport(
    tmp_path: Path,
) -> None:
    governance = _authorize(tmp_path)
    transport = FakeTransport()
    capture = api.ReadOnlyApiCapture(governance, _credentials(), transport)
    assert (
        _refusal(lambda: capture.call("rank_ranks", {}, estimated_credits=1))
        == "OPERATION_NOT_APPROVED"
    )
    for operation in ("strategy_rebalance_commit", "ai_factor_predict", "__class__"):
        assert (
            _refusal(lambda: capture.call(operation, {}, estimated_credits=1))
            == "OPERATION_NOT_ALLOWED"
        )
    for params in ({"bad key": 1}, {"k": {"nested": 1}}, {"k": object()}):
        assert (
            _refusal(lambda: capture.call("screen_run", params, estimated_credits=1))
            == "PARAMS_INVALID"
        )  # type: ignore[arg-type]
    assert transport.calls == []


def test_overspend_halts_further_calls(tmp_path: Path) -> None:
    governance = _authorize(tmp_path, budget=3, calls=5)
    capture = api.ReadOnlyApiCapture(
        governance, _credentials(), FakeTransport(credits_used=9)
    )
    assert (
        _refusal(lambda: capture.call("screen_run", {}, estimated_credits=2))
        == "CREDIT_BUDGET_EXCEEDED"
    )
    assert (
        _refusal(lambda: capture.call("screen_run", {}, estimated_credits=1))
        == "API_HALTED"
    )


def test_transport_failure_halts(tmp_path: Path) -> None:
    class Failing:
        def invoke(self, *args: object, **kwargs: object) -> api.ApiResponse:
            raise CaptureRefusal("API_CALL_FAILED")

    governance = _authorize(tmp_path)
    capture = api.ReadOnlyApiCapture(governance, _credentials(), Failing())
    assert (
        _refusal(lambda: capture.call("screen_run", {}, estimated_credits=1))
        == "API_CALL_FAILED"
    )
    assert (
        _refusal(lambda: capture.call("screen_run", {}, estimated_credits=1))
        == "API_HALTED"
    )


def test_credentials_require_exactly_the_two_resolver_fields() -> None:
    governance = api.ApiGovernance(
        Path("r"), Path("l"), "0" * 64, {}, 1, 1, REVIEW, NOW
    )
    assert (
        _refusal(
            lambda: api.ReadOnlyApiCapture(governance, {"key_id": SecretValue("x")})
        )
        == "CREDENTIAL_FIELDS_INVALID"
    )


def _config(extra: str = "") -> dict[str, Any]:
    import tomllib

    return tomllib.loads(
        "[credentials.sources.azure-keyvault]\n"
        'command = ["helper", "-File", "kv.ps1"]\n'
        "[credentials.p123]\n"
        'key_id = [{source = "azure-keyvault", name = "owner-supplied-id-name"}]\n'
        'key = [{source = "azure-keyvault", name = "owner-supplied-key-name"}]\n'
        + extra
    )


class _Runner:
    def __init__(self) -> None:
        self.names: list[str] = []

    def __call__(
        self, command: tuple[str, ...], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.names.append(command[-1])
        value = PLACEHOLDER_ID if command[-1].endswith("id-name") else PLACEHOLDER_KEY
        return subprocess.CompletedProcess(command, 0, value + "\n", None)


def test_credentials_resolve_through_the_core_resolver_only() -> None:
    from quant_engine.core.credentials import sources_from_config

    runner = _Runner()
    config = _config()
    values = api.resolve_api_credentials(
        config, sources=sources_from_config(config, runner=runner)
    )
    assert set(values) == {"key_id", "key"}
    assert values["key"].reveal() == PLACEHOLDER_KEY and PLACEHOLDER_KEY not in repr(
        values
    )
    assert sorted(runner.names) == ["owner-supplied-id-name", "owner-supplied-key-name"]
    extra = _config('note = [{source = "azure-keyvault", name = "other"}]\n')
    assert (
        _refusal(lambda: api.resolve_api_credentials(extra))
        == "CREDENTIAL_FIELDS_INVALID"
    )
    assert (
        _refusal(lambda: api.resolve_api_credentials({})) == "CREDENTIAL_PLAN_INVALID"
    )


def test_unresolvable_credentials_are_sanitized() -> None:
    failing = KeyVaultSource(
        ("helper",), runner=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", None)
    )
    code = _refusal(
        lambda: api.resolve_api_credentials(
            _config(), sources={"azure-keyvault": failing}
        )
    )
    assert code == "CREDENTIALS_UNRESOLVED"


def test_default_transport_refuses_without_the_client_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr(api.importlib, "import_module", missing)
    transport = api.P123ApiTransport()
    code = _refusal(
        lambda: transport.invoke(
            "screen_run", {}, key_id=SecretValue("a"), key=SecretValue("b")
        )
    )
    assert code == "API_CLIENT_UNAVAILABLE"
    assert (
        _refusal(
            lambda: transport.invoke(
                "anything", {}, key_id=SecretValue("a"), key=SecretValue("b")
            )
        )
        == "OPERATION_NOT_ALLOWED"
    )


def test_module_has_no_generic_request_surface() -> None:
    public = {name for name in dir(api.ReadOnlyApiCapture) if not name.startswith("_")}
    assert public == {"call"}
    assert not hasattr(api, "requests") and not hasattr(api, "urllib")
