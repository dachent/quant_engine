"""make-evidence / check-evidence: the generator, and the offline authorization check."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from tools import schwab_capture_driver as driver

REVIEW = "https://github.com/dachent/quant_engine/issues/1#issuecomment-1"
BODY = {"groupBySecurityType": True, "isExtendedPricing": False}
ISSUED = "2031-06-13T20:00:00Z"
EXPIRES = "2031-06-14T20:00:00Z"
NOW = datetime(2031, 6, 14, 12, 30, tzinfo=timezone.utc)


def _args(tmp_path: Path, **overrides: str) -> list[str]:
    body = tmp_path / "body.json"
    body.write_text(json.dumps(BODY), encoding="utf-8")
    values = {
        "--output": str(tmp_path / "evidence.json"),
        "--cutoff": "2031-06-13",
        "--account-count": "3",
        "--holdings-body": str(body),
        "--review-identity": "synthetic-reviewer",
        "--review-reference": REVIEW,
        "--issued-at": ISSUED,
        "--expires-at": EXPIRES,
        "--max-requests": "50",
    }
    values.update(overrides)
    return ["make-evidence", *[x for kv in values.items() for x in kv]]


def test_made_evidence_is_authorized_by_the_real_gate_and_single_use(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert driver.main(_args(tmp_path)) == 0
    summary = json.loads(capsys.readouterr().out)
    evidence = tmp_path / "evidence.json"
    assert summary["catalog_sha256"] == driver.catalog_sha256()
    assert summary["terms_sha256"] == driver.TERMS_SHA256
    governance = driver.authorize_live_read(
        evidence, receipt_root=tmp_path / "receipts", now=lambda: NOW
    )
    assert governance.evidence_sha256 == summary["evidence_sha256"]
    assert governance.max_requests == 50
    with pytest.raises(driver.GovernanceError, match="already consumed"):
        driver.authorize_live_read(
            evidence, receipt_root=tmp_path / "receipts", now=lambda: NOW
        )


def test_make_evidence_is_create_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert driver.main(_args(tmp_path)) == 0
    first = (tmp_path / "evidence.json").read_bytes()
    capsys.readouterr()
    assert driver.main(_args(tmp_path)) == 2
    assert "already exists" in capsys.readouterr().out
    assert (tmp_path / "evidence.json").read_bytes() == first


@pytest.mark.parametrize(
    "override",
    [
        {"--review-reference": "https://example.com/x"},
        {"--expires-at": ISSUED},
        {"--max-requests": "0"},
        {"--account-count": "0"},
        {"--exception-label": "bad"},
    ],
)
def test_make_evidence_refuses_what_authorize_refuses_and_leaves_no_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], override: dict[str, str]
) -> None:
    assert driver.main(_args(tmp_path, **override)) == 2
    assert "error" in json.loads(capsys.readouterr().out)
    assert not (tmp_path / "evidence.json").exists()


def test_check_evidence_does_not_consume_and_reports_consumption(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert driver.main(_args(tmp_path)) == 0
    capsys.readouterr()
    evidence = tmp_path / "evidence.json"
    roots = tmp_path / "receipts"
    # Expiry is checked against the real clock: 2031 evidence is current today.
    assert (
        driver.main(["check-evidence", str(evidence), "--receipt-root", str(roots)])
        == 2
    )
    assert "not yet current" in capsys.readouterr().out
    checked = driver.validate_evidence(evidence, now=lambda: NOW)
    assert not roots.exists()
    driver.authorize_live_read(evidence, receipt_root=roots, now=lambda: NOW)
    assert (roots / f"{checked.evidence_sha256}.json").exists()


def test_check_evidence_cli_on_current_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime.now(timezone.utc)
    issued = now.replace(year=now.year - 1).isoformat()
    expires = now.replace(year=now.year + 1).isoformat()
    cutoff = driver.parse_cutoff(  # a Friday is a valid declared cutoff
        "2031-06-13"
    ).as_of_date.isoformat()
    args = _args(
        tmp_path, **{"--issued-at": issued, "--expires-at": expires, "--cutoff": cutoff}
    )
    assert driver.main(args) == 0
    capsys.readouterr()
    roots = tmp_path / "receipts"
    evidence = tmp_path / "evidence.json"
    assert (
        driver.main(["check-evidence", str(evidence), "--receipt-root", str(roots)])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["already_consumed"] is False
    assert not roots.exists()


class _Target:
    def __init__(self, internal: str, provider: str) -> None:
        self.internal_account_id = internal
        self.provider_account_id = provider


def test_build_crosswalk_is_create_only_and_never_prints_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = (
        _Target("account-" + "2" * 16, "PROV-B"),
        _Target("account-" + "1" * 16, "PROV-A"),
    )
    monkeypatch.setattr(
        driver._TRANSACTION_CAPTURE, "load_retained_targets", lambda root: targets
    )
    out = tmp_path / "crosswalk.json"
    argv = [
        "build-crosswalk",
        "--selected-source-root",
        str(tmp_path),
        "--out",
        str(out),
    ]
    assert driver.main(argv) == 0
    stdout = capsys.readouterr().out
    assert "PROV-" not in stdout and json.loads(stdout)["account_count"] == 2
    parsed = driver.parse_crosswalk(json.loads(out.read_text(encoding="utf-8")))
    assert parsed == {t.provider_account_id: t.internal_account_id for t in targets}
    assert driver.main(argv) == 2  # create-only
    assert "already exists" in capsys.readouterr().out


def test_build_crosswalk_refuses_unloadable_or_redacted_targets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "crosswalk.json"
    argv = [
        "build-crosswalk",
        "--selected-source-root",
        str(tmp_path),
        "--out",
        str(out),
    ]
    assert driver.main(argv) == 2  # no manifest in the root
    monkeypatch.setattr(
        driver._TRANSACTION_CAPTURE,
        "load_retained_targets",
        lambda root: (_Target("account-" + "1" * 16, "[REDACTED]"),),
    )
    assert driver.main(argv) == 2
    assert not out.exists()


class _Page:
    def __init__(self, url: str) -> None:
        self.url = url


class _Browser:
    def __init__(self, urls: list[str]) -> None:
        self.contexts = [type("C", (), {"pages": [_Page(u) for u in urls]})()]


SCHWAB = "https://client.schwab.com/app/accounts/positions/"


def test_attach_check_reports_names_only_and_issues_no_governed_request() -> None:
    seen: list[str] = []
    result = driver.attach_check(
        cdp_url="http://127.0.0.1:9223",
        connect=lambda url: seen.append(url) or _Browser([SCHWAB, "about:blank"]),
        observe=lambda page: {"Authorization": "Bearer secret", "Schwab-Env": "x"},
    )
    assert result["ok"] is True and result["governed_requests"] == 0
    assert result["observed_header_names"] == ["Authorization", "Schwab-Env"]
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("urls", [[], [SCHWAB, SCHWAB], ["about:blank"]])
def test_attach_check_needs_exactly_one_schwab_page(urls: list[str]) -> None:
    with pytest.raises(driver.DriverError, match="exactly one"):
        driver.attach_check(
            cdp_url="x", connect=lambda url: _Browser(urls), observe=lambda page: {}
        )


def test_attach_check_not_ok_without_authorization() -> None:
    result = driver.attach_check(
        cdp_url="x", connect=lambda url: _Browser([SCHWAB]), observe=lambda page: {}
    )
    assert result["ok"] is False
