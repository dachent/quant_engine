"""The evidence generator assembles, never approves, and the live gate accepts it.

Everything is synthetic: no network, no credential, no secret. The review URL is a
shape-valid placeholder; the gate only shape-checks it, so a placeholder proves
the file layout and the single-use behaviour, not any real approval.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quant_engine.matrixport import cli
from quant_engine.matrixport.catalog import ENDPOINTS, get_endpoint
from quant_engine.matrixport.credentials import ApiCredentials, plan_identity
from quant_engine.matrixport.evidence import (
    EvidenceGenerationError,
    EvidenceRequest,
    build_evidence,
    planned_scope,
    verify_evidence,
    write_evidence,
)
from quant_engine.matrixport.governance import (
    TERMS_SHA256,
    GovernanceError,
    authorize_live_read,
    catalog_sha256,
    validate_live_read_evidence,
)
from quant_engine.matrixport.transport import ReadOnlyTransport

REVIEW = "https://github.com/dachent/quant_engine/issues/374#issuecomment-1"
SECRET_NAME = "synthetic-vault-secret-name"
PLAN = f"""
[credentials.sources.azure-keyvault]
command = ["helper", "-File", "kv.ps1"]

[credentials.matrixport]
access_key = [{{source = "azure-keyvault", name = "{SECRET_NAME}-a"}}]
signing_secret = [{{source = "azure-keyvault", name = "{SECRET_NAME}-b"}}]
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _request(**overrides: object) -> EvidenceRequest:
    now = _now()
    fields: dict[str, object] = {
        "operation": "extract",
        "mode": "full",
        "cutoff": now.date().isoformat(),
        "review_url": REVIEW,
        "review_identity": "synthetic-reviewer",
        "issued_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(hours=2)).isoformat(),
        "max_requests": 5000,
    }
    fields.update(overrides)
    return EvidenceRequest(**fields)  # type: ignore[arg-type]


def _out(tmp_path: Path, name: str = "evidence.json") -> Path:
    directory = tmp_path / "outside"
    directory.mkdir(exist_ok=True)
    return directory / name


class FakeResponse:
    status_code = 200
    content = b'{"code":0}'
    headers: dict[str, str] = {}


class FakeSession:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, **_kwargs: object) -> FakeResponse:
        self.calls += 1
        return FakeResponse()


def test_generated_extract_evidence_matches_the_gate_schema_and_catalog(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "plan.toml"
    plan.write_text(PLAN, encoding="utf-8")
    out = _out(tmp_path)
    summary = write_evidence(_request(credentials_config=plan), out)

    document = json.loads(out.read_bytes())
    assert document["terms"]["sha256"] == TERMS_SHA256
    assert document["catalog"]["sha256"] == catalog_sha256()
    live = {f"{e.method} {e.path}" for e in ENDPOINTS if e.live_enabled}
    assert {
        f"{row['method']} {row['path']}" for row in document["scope"]["endpoints"]
    } == live
    assert document["metering"] == {"max_requests": 5000, "retry_count": 0}
    assert document["review"] == {
        "identity": "synthetic-reviewer",
        "reference": REVIEW,
    }
    assert out.read_bytes().endswith(b"\n") and b"\r" not in out.read_bytes()

    validated = validate_live_read_evidence(out, operation="extract", mode="full")
    assert validated.evidence_sha256 == summary["evidence_sha256"]

    sidecar = json.loads(Path(str(summary["provenance"])).read_bytes())
    assert sidecar["credential_identity"] == plan_identity(plan)
    assert sidecar["cutoff"] == _request().cutoff
    assert sidecar["evidence_sha256"] == summary["evidence_sha256"]
    for path in (out, Path(str(summary["provenance"]))):
        assert SECRET_NAME.encode() not in path.read_bytes()
    assert b"BIT.com-API" not in out.read_bytes()


def test_auth_test_scope_is_exactly_the_wallet_read(tmp_path: Path) -> None:
    out = _out(tmp_path)
    write_evidence(_request(operation="auth-test", mode=None, max_requests=1), out)
    assert planned_scope("auth-test") == ["GET /mapi/v1/wallet/balance"]
    validate_live_read_evidence(out, operation="auth-test", mode=None)
    with pytest.raises(EvidenceGenerationError, match="exactly one"):
        write_evidence(
            _request(operation="auth-test", mode=None, max_requests=2),
            _out(tmp_path, "b.json"),
        )


def test_run_operation_is_full_only() -> None:
    evidence, _ = build_evidence(_request(operation="run", mode=None))
    assert evidence["mode"] == "full"
    with pytest.raises(EvidenceGenerationError, match="run mode"):
        build_evidence(_request(operation="run", mode="incremental"))


@pytest.mark.parametrize(
    "review_url",
    [
        "",
        "TBD",
        "https://github.com/dachent/quant_engine/issues/374",
        "https://github.com/dachent/quant_engine/issues/0#issuecomment-1",
        "https://github.com/other/quant_engine/issues/374#issuecomment-1",
        "http://github.com/dachent/quant_engine/issues/374#issuecomment-1",
        REVIEW + " ",
    ],
)
def test_refuses_missing_or_malformed_review_url(
    tmp_path: Path, review_url: str
) -> None:
    out = _out(tmp_path)
    with pytest.raises(EvidenceGenerationError, match="review URL"):
        write_evidence(_request(review_url=review_url), out)
    assert not out.exists()


def test_refuses_blank_review_identity() -> None:
    with pytest.raises(EvidenceGenerationError, match="review identity"):
        build_evidence(_request(review_identity="  "))


def test_refuses_expired_authority(tmp_path: Path) -> None:
    now = _now()
    out = _out(tmp_path)
    with pytest.raises(EvidenceGenerationError, match="expired"):
        write_evidence(
            _request(
                issued_at=(now - timedelta(hours=3)).isoformat(),
                expires_at=(now - timedelta(hours=1)).isoformat(),
            ),
            out,
        )
    assert not out.exists()


def test_refuses_window_longer_than_declared_maximum(tmp_path: Path) -> None:
    now = _now()
    request = _request(
        issued_at=(now - timedelta(minutes=1)).isoformat(),
        expires_at=(now + timedelta(hours=30)).isoformat(),
        max_window_hours=24.0,
    )
    with pytest.raises(EvidenceGenerationError, match="exceeds the declared maximum"):
        write_evidence(request, _out(tmp_path))
    # a wider declared maximum is the caller's explicit choice
    build_evidence(
        _request(
            issued_at=request.issued_at,
            expires_at=request.expires_at,
            max_window_hours=48.0,
        )
    )


@pytest.mark.parametrize(
    "override, match",
    [
        ({"issued_at": "2099-01-01T00:00:00Z"}, "future|after issued_at"),
        ({"issued_at": "2026-01-01T00:00:00"}, "timezone"),
        ({"expires_at": "not-a-time"}, "RFC3339"),
        ({"cutoff": "2099-01-01"}, "future"),
        ({"cutoff": "09/17/2026"}, "YYYY-MM-DD"),
        ({"max_requests": 0}, "between 1 and 10000"),
        ({"max_requests": 3}, "below one call per endpoint"),
        ({"mode": "weekly"}, "extract mode"),
        ({"operation": "export"}, "operation must be"),
    ],
)
def test_refuses_bad_inputs(override: dict[str, object], match: str) -> None:
    with pytest.raises(EvidenceGenerationError, match=match):
        build_evidence(_request(**override))


def test_refuses_scope_that_differs_from_the_catalog() -> None:
    live = [e.path for e in ENDPOINTS if e.live_enabled]
    build_evidence(_request(scope_paths=tuple(live)))
    for wrong in (
        tuple(live[:-1]),
        tuple(live) + ("/mapi/v1/not/in/catalog",),
        ("/mapi/v1/wallet/balance",),
    ):
        with pytest.raises(EvidenceGenerationError, match="differs from the catalog"):
            build_evidence(_request(scope_paths=wrong))
    blocked = next(e.path for e in ENDPOINTS if not e.live_enabled)
    with pytest.raises(EvidenceGenerationError, match="differs from the catalog"):
        build_evidence(_request(scope_paths=tuple(live) + (blocked,)))


def test_output_is_create_only(tmp_path: Path) -> None:
    out = _out(tmp_path)
    write_evidence(_request(), out)
    first = out.read_bytes()
    with pytest.raises(EvidenceGenerationError, match="already exists"):
        write_evidence(_request(), out)
    assert out.read_bytes() == first
    # an orphan sidecar also blocks a run, so nothing is ever overwritten
    other = _out(tmp_path, "other.json")
    (other.parent / "other.provenance.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EvidenceGenerationError, match="already exists"):
        write_evidence(_request(), other)
    assert not other.exists()


def test_output_must_be_absolute_json_existing_directory_outside_repo(
    tmp_path: Path,
) -> None:
    with pytest.raises(EvidenceGenerationError, match="absolute"):
        write_evidence(_request(), Path("relative.json"))
    with pytest.raises(EvidenceGenerationError, match=r"\.json"):
        write_evidence(_request(), _out(tmp_path, "evidence.txt"))
    with pytest.raises(EvidenceGenerationError, match="already exist"):
        write_evidence(_request(), tmp_path / "missing-dir" / "e.json")
    repo = Path(__file__).resolve().parents[2]
    with pytest.raises(EvidenceGenerationError, match="outside"):
        write_evidence(_request(), repo / "evidence.json")
    fake_repo = tmp_path / "other-checkout"
    (fake_repo / ".git").mkdir(parents=True)
    with pytest.raises(EvidenceGenerationError, match="outside"):
        write_evidence(_request(), fake_repo / "evidence.json")
    assert not (repo / "evidence.json").exists()


def test_verify_accepts_without_consuming_and_rederives_hashes(
    tmp_path: Path, canonical_governance_root: Path
) -> None:
    out = _out(tmp_path)
    write_evidence(_request(), out)
    for _ in range(2):
        report = verify_evidence(out)
        assert report["ok"] is True and report["consumed"] is False
    assert not canonical_governance_root.exists() or not list(
        canonical_governance_root.glob("*.json")
    )
    # still consumable afterwards
    authorize_live_read(out, operation="extract", mode="full")


def test_verify_refuses_tampered_stale_or_over_wide_files(tmp_path: Path) -> None:
    out = _out(tmp_path)
    write_evidence(_request(), out)
    good = json.loads(out.read_bytes())

    def variant(name: str, mutate) -> Path:  # type: ignore[no-untyped-def]
        doc = json.loads(json.dumps(good))
        mutate(doc)
        path = _out(tmp_path, name)
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    bad_catalog = variant("c.json", lambda d: d["catalog"].update(sha256="0" * 64))
    with pytest.raises(EvidenceGenerationError, match="catalog"):
        verify_evidence(bad_catalog)
    bad_terms = variant("t.json", lambda d: d["terms"].update(sha256="0" * 64))
    with pytest.raises(EvidenceGenerationError, match="terms"):
        verify_evidence(bad_terms)
    narrow = variant("s.json", lambda d: d["scope"]["endpoints"].pop())
    with pytest.raises(EvidenceGenerationError, match="exactly the catalog"):
        verify_evidence(narrow)
    gone = variant(
        "e.json",
        lambda d: d["authority"].update(
            expires_at=(_now() - timedelta(minutes=1)).isoformat()
        ),
    )
    with pytest.raises(EvidenceGenerationError, match="expired"):
        verify_evidence(gone)
    with pytest.raises(EvidenceGenerationError, match="exceeds the declared maximum"):
        verify_evidence(out, max_window_hours=1.0)
    junk = _out(tmp_path, "j.json")
    junk.write_bytes(b"not json")
    with pytest.raises(EvidenceGenerationError, match="UTF-8 JSON"):
        verify_evidence(junk)
    extra = variant("x.json", lambda d: d.update(cutoff="2026-09-17"))
    with pytest.raises(EvidenceGenerationError, match="field set"):
        verify_evidence(extra)


def test_generated_file_is_accepted_by_the_gate_and_is_single_use(
    tmp_path: Path, canonical_governance_root: Path
) -> None:
    out = _out(tmp_path)
    write_evidence(_request(operation="auth-test", mode=None, max_requests=1), out)
    grant = authorize_live_read(out, operation="auth-test", mode=None)
    session = FakeSession()
    transport = ReadOnlyTransport(
        ApiCredentials("access-key-canary", "secret-key-canary"),
        session=session,
        governance=grant,
        requests_per_second=1000,
        sleeper=lambda _seconds: None,
    )
    transport.get("/mapi/v1/wallet/balance")
    assert session.calls == 1
    receipt = json.loads(grant.receipt_path.read_text(encoding="utf-8"))
    assert receipt["used_requests"] == 1 and receipt["max_requests"] == 1
    assert receipt["review"]["reference"] == REVIEW

    # the budget is spent and a second consumption is refused
    with pytest.raises(GovernanceError, match="budget is exhausted"):
        transport.get("/mapi/v1/wallet/balance")
    with pytest.raises(GovernanceError, match="already consumed"):
        authorize_live_read(out, operation="auth-test", mode=None)
    with pytest.raises(EvidenceGenerationError, match="already consumed"):
        verify_evidence(out)
    assert get_endpoint("/mapi/v1/wallet/balance").live_enabled


def test_cli_generates_then_verifies_and_reports_refusals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = _now()
    out = _out(tmp_path, "cli.json")
    base = [
        "governance-evidence",
        "--operation",
        "extract",
        "--mode",
        "full",
        "--cutoff",
        now.date().isoformat(),
        "--review-identity",
        "synthetic-reviewer",
        "--issued-at",
        (now - timedelta(minutes=1)).isoformat(),
        "--expires-at",
        (now + timedelta(hours=1)).isoformat(),
        "--max-requests",
        "5000",
        "--out",
        str(out),
    ]
    assert cli.main([*base, "--review-url", "nope"]) == 4
    assert not out.exists()
    assert cli.main([*base, "--review-url", REVIEW]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is True and printed["evidence"] == str(out)
    assert cli.main(["verify-evidence", str(out)]) == 0
    assert cli.main([*base, "--review-url", REVIEW]) == 4


def test_output_through_a_link_into_the_repo_is_refused(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    link = tmp_path / "link"
    try:
        link.symlink_to(repo / "docs", target_is_directory=True)
    except OSError:
        import subprocess

        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(repo / "docs")],
            capture_output=True,
        )
        if made.returncode != 0:
            pytest.skip("cannot create a directory link here")
    with pytest.raises(EvidenceGenerationError, match="outside"):
        write_evidence(_request(), link / "evidence.json")
    assert not (repo / "docs" / "evidence.json").exists()


def test_sidecar_failure_removes_the_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant_engine.matrixport import evidence as module

    real = module._create_only
    calls: list[Path] = []

    def flaky(path: Path, content: bytes) -> None:
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        real(path, content)

    monkeypatch.setattr(module, "_create_only", flaky)
    out = _out(tmp_path)
    with pytest.raises(EvidenceGenerationError, match="sidecar"):
        write_evidence(_request(), out)
    assert not out.exists()
    monkeypatch.setattr(module, "_create_only", real)
    write_evidence(_request(), out)
