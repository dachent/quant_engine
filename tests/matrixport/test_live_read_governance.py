from __future__ import annotations

import hashlib
import json
import os
import threading
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quant_engine.matrixport import cli
from quant_engine.matrixport import governance as governance_module
from quant_engine.matrixport.catalog import catalog_rows, get_endpoint
from quant_engine.matrixport.credentials import ApiCredentials, CredentialError
from quant_engine.matrixport.governance import (
    GovernanceError,
    TERMS_SHA256,
    TERMS_VERSION,
    authorize_live_read,
)
from quant_engine.matrixport.transport import ReadOnlyTransport
from quant_engine.matrixport.util import canonical_json
from quant_engine.matrixport.pipeline import ExtractionError, MatrixportExtractor


class FakeResponse:
    status_code = 200
    content = b'{"code":0}'
    headers: dict[str, str] = {}


class InspectingSession:
    def __init__(self, receipt_path: Path) -> None:
        self.receipt_path = receipt_path
        self.calls = 0
        self.receipt_at_call: dict[str, object] | None = None

    def request(self, **_kwargs: object) -> FakeResponse:
        self.calls += 1
        self.receipt_at_call = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        return FakeResponse()


def _catalog_sha256() -> str:
    return hashlib.sha256(canonical_json(catalog_rows()).encode("utf-8")).hexdigest()


def _evidence(
    tmp_path: Path,
    *,
    operation: str = "auth-test",
    scope: tuple[str, ...] = ("/mapi/v1/wallet/balance",),
    max_requests: int = 1,
    expires_at: datetime | None = None,
    terms_sha256: str = TERMS_SHA256,
    catalog_sha256: str | None = None,
    retry_count: int = 0,
    review_reference: str = "https://github.com/dachent/quant_engine/issues/374#issuecomment-1",
) -> Path:
    now = datetime.now(timezone.utc)
    document = {
        "schema_version": 1,
        "operation": operation,
        "mode": None,
        "scope": {"endpoints": [{"method": "GET", "path": path} for path in scope]},
        "review": {
            "identity": "reviewer@example.test",
            "reference": review_reference,
        },
        "authority": {
            "status": "CURRENT",
            "issued_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (expires_at or now + timedelta(minutes=5)).isoformat(),
        },
        "terms": {"version": TERMS_VERSION, "sha256": terms_sha256},
        "catalog": {"sha256": catalog_sha256 or _catalog_sha256()},
        "metering": {"max_requests": max_requests, "retry_count": retry_count},
    }
    path = tmp_path / "reviewed-live-read.json"
    path.write_text(canonical_json(document) + "\n", encoding="utf-8")
    return path


def test_same_evidence_cannot_be_consumed_from_an_alternate_output_workspace(
    tmp_path: Path, canonical_governance_root: Path
) -> None:
    workspace_a = tmp_path / "output-workspace-a"
    workspace_b = tmp_path / "output-workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    evidence = _evidence(tmp_path)

    authorize_live_read(evidence, operation="auth-test", mode=None)

    with pytest.raises(GovernanceError, match="already consumed"):
        authorize_live_read(evidence, operation="auth-test", mode=None)

    assert not (workspace_a / ".governance").exists()
    assert not (workspace_b / ".governance").exists()
    assert list(canonical_governance_root.glob("*.json")) == [
        canonical_governance_root
        / f"{hashlib.sha256(evidence.read_bytes()).hexdigest()}.json"
    ]


def test_governance_consumes_receipt_before_the_first_network_call(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path)
    grant = authorize_live_read(evidence, operation="auth-test", mode=None)
    receipt = grant.receipt_path
    session = InspectingSession(receipt)
    transport = ReadOnlyTransport(
        ApiCredentials("access-key-canary", "secret-key-canary"),
        session=session,
        governance=grant,
        requests_per_second=1000,
        sleeper=lambda _seconds: None,
    )

    transport.get("/mapi/v1/wallet/balance")

    assert session.calls == 1
    assert session.receipt_at_call is not None
    assert session.receipt_at_call["used_requests"] == 1
    assert (
        session.receipt_at_call["evidence_sha256"]
        == hashlib.sha256(evidence.read_bytes()).hexdigest()
    )


def test_receipt_creation_commits_its_directory_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _evidence(tmp_path)
    committed: list[Path] = []
    monkeypatch.setattr(
        governance_module,
        "_commit_receipt_directory_entry",
        committed.append,
        raising=False,
    )

    grant = authorize_live_read(evidence, operation="auth-test", mode=None)

    assert committed == [grant.receipt_path]


def test_request_reservation_uses_a_mode_0600_temp_and_commits_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _evidence(tmp_path)
    grant = authorize_live_read(evidence, operation="auth-test", mode=None)
    opened_modes: list[int] = []
    committed: list[Path] = []
    real_open = os.open

    def recording_open(path: str | bytes | Path, flags: int, mode: int = 0o777) -> int:
        opened_modes.append(mode)
        return real_open(path, flags, mode)

    monkeypatch.setattr(governance_module.os, "open", recording_open)
    monkeypatch.setattr(
        governance_module,
        "_commit_receipt_directory_entry",
        committed.append,
        raising=False,
    )

    grant.reserve_request(get_endpoint("/mapi/v1/wallet/balance"))

    assert 0o600 in opened_modes
    assert committed == [grant.receipt_path]
    assert (
        json.loads(grant.receipt_path.read_text(encoding="utf-8"))["used_requests"] == 1
    )


def test_transport_cannot_be_created_without_consumed_governance_evidence() -> None:
    with pytest.raises(ValueError, match="governance"):
        ReadOnlyTransport(ApiCredentials("access-key-canary", "secret-key-canary"))


def test_programmatic_extraction_refuses_to_create_a_live_transport_without_evidence(
    tmp_path: Path,
) -> None:
    with pytest.raises(ExtractionError, match="governance"):
        MatrixportExtractor(
            tmp_path,
            mode="full",
            credentials=ApiCredentials("access-key-canary", "secret-key-canary"),
        )
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    ("kwargs", "operation", "match"),
    [
        (
            {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
            "auth-test",
            "expired",
        ),
        ({"scope": ("/mapi/v1/wallet/bills",)}, "auth-test", "not approved"),
        ({"terms_sha256": "0" * 64}, "auth-test", "terms"),
        ({"catalog_sha256": "1" * 64}, "auth-test", "catalog"),
        ({"max_requests": 0}, "auth-test", "max_requests"),
        ({"retry_count": 1}, "auth-test", "retry_count"),
    ],
)
def test_governance_rejects_invalid_reviewed_evidence_before_transport_creation(
    tmp_path: Path, kwargs: dict[str, object], operation: str, match: str
) -> None:
    evidence = _evidence(tmp_path, **kwargs)

    with pytest.raises(GovernanceError, match=match):
        authorize_live_read(evidence, operation=operation, mode=None)

    assert not (tmp_path / ".governance" / "live-read-receipts").exists()


def test_governance_blocks_replay_and_allows_only_one_concurrent_consumer(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path)
    successes: list[object] = []
    failures: list[Exception] = []
    start = threading.Barrier(2)

    def consume() -> None:
        start.wait()
        try:
            successes.append(
                authorize_live_read(evidence, operation="auth-test", mode=None)
            )
        except GovernanceError as error:
            failures.append(error)

    first = threading.Thread(target=consume)
    second = threading.Thread(target=consume)
    first.start()
    second.start()
    first.join()
    second.join()

    assert len(successes) == 1
    assert len(failures) == 1
    assert "already consumed" in str(failures[0])


def test_governance_replay_is_blocked_for_any_second_consumer(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path)
    authorize_live_read(evidence, operation="auth-test", mode=None)

    with pytest.raises(GovernanceError, match="already consumed"):
        authorize_live_read(
            evidence,
            operation="auth-test",
            mode=None,
        )


def test_governance_blocks_a_request_outside_the_metered_allowance(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path, max_requests=1)
    grant = authorize_live_read(evidence, operation="auth-test", mode=None)
    receipt = grant.receipt_path
    session = InspectingSession(receipt)
    transport = ReadOnlyTransport(
        ApiCredentials("access-key-canary", "secret-key-canary"),
        session=session,
        governance=grant,
        requests_per_second=1000,
        sleeper=lambda _seconds: None,
    )
    transport.get("/mapi/v1/wallet/balance")

    with pytest.raises(GovernanceError, match="request budget"):
        transport.get("/mapi/v1/wallet/balance")
    assert session.calls == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["auth-test"],
        ["extract", "--full"],
        ["run", "--full"],
    ],
)
def test_live_cli_commands_require_a_governance_evidence_path(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(argv)
    assert raised.value.code == 2


def test_run_reuses_the_exact_credentials_that_signed_live_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second credential read must not strand or mis-verify a completed run."""

    signed_credentials = ApiCredentials("signed-key", "signed-secret")
    credential_reads = 0
    extractor_credentials: list[ApiCredentials] = []
    verification_needles: list[bytes] = []

    def sequential_credentials(_config: object = None) -> ApiCredentials:
        nonlocal credential_reads
        credential_reads += 1
        if credential_reads == 1:
            return signed_credentials
        raise CredentialError("credential rotated after the live read")

    class FakeExtractor:
        def __init__(self, workspace: Path, *, credentials=None, **_kwargs: object):
            selected = credentials or sequential_credentials()
            extractor_credentials.append(selected)
            self.run_dir = workspace / "output" / "run-1"

        def extract(self):
            return SimpleNamespace(
                run_id="run-1",
                run_dir=self.run_dir,
                request_count=1,
                status="COMPLETED",
            )

        def close(self) -> None:
            return None

    def verify(_run_dir: Path, needles: list[bytes]) -> dict[str, object]:
        verification_needles.extend(needles)
        return {"ok": True, "status": "VERIFIED"}

    monkeypatch.setattr(cli, "load_credentials", sequential_credentials)
    monkeypatch.setattr(cli, "MatrixportExtractor", FakeExtractor)
    monkeypatch.setattr(cli, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(
        cli,
        "export_run",
        lambda *_args: {"csv": [tmp_path / "one.csv"], "xlsx": {}},
    )
    monkeypatch.setattr(cli, "verify_run", verify)
    monkeypatch.setattr(cli, "atomic_write", lambda *_args, **_kwargs: None)

    assert (
        cli.main(
            ["run", "--full", "--governance-evidence", str(tmp_path / "grant.json")]
        )
        == 0
    )
    assert credential_reads == 1
    assert extractor_credentials == [signed_credentials]
    assert signed_credentials.key_identifier.encode("utf-8") in verification_needles
    assert signed_credentials.signing_material.encode("utf-8") in verification_needles


def test_copied_evidence_cannot_reset_the_governed_consumption_receipt(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    evidence = _evidence(tmp_path)
    copied = tmp_path / "copied" / evidence.name
    copied.parent.mkdir()
    copied.write_bytes(evidence.read_bytes())
    authorize_live_read(evidence, operation="auth-test", mode=None)

    with pytest.raises(GovernanceError, match="already consumed"):
        authorize_live_read(copied, operation="auth-test", mode=None)


def test_changing_cwd_cannot_change_the_canonical_receipt_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _evidence(tmp_path)
    authorize_live_read(evidence, operation="auth-test", mode=None)
    other_cwd = tmp_path / "other-cwd"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    with pytest.raises(GovernanceError, match="already consumed"):
        authorize_live_read(evidence, operation="auth-test", mode=None)


def test_reparse_canonical_state_root_is_rejected_before_receipt_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "canonical-governance-target"
    target.mkdir()
    state_root_link = tmp_path / "canonical-governance-link"
    try:
        state_root_link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Windows policy does not permit creating a test directory symlink")
    evidence = _evidence(tmp_path)
    monkeypatch.setattr(
        governance_module, "_canonical_state_root", lambda: state_root_link
    )

    with pytest.raises(GovernanceError, match="reparse"):
        authorize_live_read(evidence, operation="auth-test", mode=None)
    assert not list(target.glob("*.json"))


def test_detected_reparse_component_is_rejected_before_receipt_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _evidence(tmp_path)
    monkeypatch.setattr(
        governance_module,
        "_is_reparse_point",
        lambda path: path.name == "canonical-governance",
    )

    with pytest.raises(GovernanceError, match="reparse"):
        authorize_live_read(evidence, operation="auth-test", mode=None)
    assert not list((tmp_path / "canonical-governance").glob("*.json"))


def test_review_reference_must_be_a_quant_engine_issue_comment(tmp_path: Path) -> None:
    evidence = _evidence(
        tmp_path,
        review_reference="https://github.example.test/acme/quant-engine/issues/374#issuecomment-1",
    )

    with pytest.raises(GovernanceError, match="issue comment"):
        authorize_live_read(evidence, operation="auth-test", mode=None)
