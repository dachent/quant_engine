"""make-evidence / check-evidence: the generator, and the offline authorization check."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class _Target:
    internal_account_id: str
    provider_account_id: str


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
        observe=lambda page: (
            {"Authorization": "Bearer secret", "Schwab-Env": "x"},
            None,
            None,
        ),
    )
    assert result["ok"] is True and result["governed_requests"] == 0
    assert result["observed_header_names"] == ["Authorization", "Schwab-Env"]
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("urls", [[], [SCHWAB, SCHWAB], ["about:blank"]])
def test_attach_check_needs_exactly_one_schwab_page(urls: list[str]) -> None:
    with pytest.raises(driver.DriverError, match="exactly one"):
        driver.attach_check(
            cdp_url="x",
            connect=lambda url: _Browser(urls),
            observe=lambda page: ({}, None, None),
        )


def test_attach_check_not_ok_without_authorization() -> None:
    result = driver.attach_check(
        cdp_url="x",
        connect=lambda url: _Browser([SCHWAB]),
        observe=lambda page: ({}, None, None),
    )
    assert result["ok"] is False


def _attach(tmp_path: Path, post_data: str | None) -> dict[str, object]:
    return driver.attach_check(
        cdp_url="x",
        connect=lambda url: _Browser([SCHWAB]),
        observe=lambda page: ({"Authorization": "t"}, post_data, None),
        capture_body_out=tmp_path / "body.json",
    )


def test_captured_body_is_filtered_create_only_and_feeds_make_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = json.dumps({**BODY, "acctLabel": "SYNTHETIC-label", "token": "x"})
    result = _attach(tmp_path, raw)
    saved = json.loads((tmp_path / "body.json").read_text(encoding="utf-8"))
    assert saved == BODY and result["holdings_body_keys"] == sorted(BODY)
    assert "SYNTHETIC-label" not in json.dumps(result)
    with pytest.raises(driver.DriverError, match="already exists"):
        _attach(tmp_path, raw)
    args = _args(tmp_path, **{"--holdings-body": str(tmp_path / "body.json")})
    assert driver.main(args) == 0


def test_no_observed_holdings_request_is_refused_with_the_operator_action(
    tmp_path: Path,
) -> None:
    with pytest.raises(driver.DriverError, match="no HoldingV2 request.*STOP"):
        _attach(tmp_path, None)
    assert not (tmp_path / "body.json").exists()


@pytest.mark.parametrize("post_data", ["", "[]", "not json", '{"token": "x"}'])
def test_unusable_observed_body_is_refused(tmp_path: Path, post_data: str) -> None:
    with pytest.raises(driver.DriverError):
        _attach(tmp_path, post_data)
    assert not (tmp_path / "body.json").exists()


XW = {"EXT-A": "account-" + "1" * 16, "EXT-B": "account-" + "2" * 16}


def _compare(page_accounts: list[str] | None) -> dict[str, object]:
    return driver.attach_check(
        cdp_url="x",
        connect=lambda url: _Browser([SCHWAB]),
        observe=lambda page: ({"Authorization": "t"}, None, page_accounts),
        crosswalk=XW,
    )


def test_account_set_match_reports_counts_only() -> None:
    result = _compare(["EXT-B", "EXT-A"])
    assert result["account_set_matches"] is True
    assert result["accounts_on_page"] == result["accounts_in_crosswalk"] == 2
    assert "EXT-" not in json.dumps(result)


def test_account_set_difference_is_refused_without_naming_provider_ids() -> None:
    with pytest.raises(driver.DriverError) as extra:
        _compare(["EXT-A", "EXT-B", "EXT-SECRET"])
    assert "EXT-" not in str(extra.value) and "hash8" in str(extra.value)
    with pytest.raises(driver.DriverError, match="differs") as missing:
        _compare(["EXT-A"])
    assert "account-" + "2" * 16 in str(missing.value)
    assert "EXT-" not in str(missing.value)


def test_account_compare_refuses_when_no_response_observed_or_duplicates() -> None:
    with pytest.raises(driver.DriverError, match="no HoldingV2 response"):
        _compare(None)
    with pytest.raises(driver.DriverError, match="twice"):
        _compare(["EXT-A", "EXT-A"])


def test_a_budget_that_cannot_complete_a_capture_is_refused_and_leaves_no_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # three accounts need 1 holdings + 3 balances + 3 transaction pages = 7 requests
    assert driver.main(_args(tmp_path, **{"--max-requests": "6"})) == 2
    assert "at least 7" in capsys.readouterr().out
    assert not (tmp_path / "evidence.json").exists()
    assert driver.main(_args(tmp_path, **{"--max-requests": "7"})) == 0


def test_check_evidence_refuses_an_unworkable_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = driver.build_evidence(
        cutoff=driver.parse_cutoff("2031-06-13"),
        account_count=3,
        holdings_body=BODY,
        review_identity="synthetic-reviewer",
        review_reference=REVIEW,
        issued_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(2999, 1, 1, tzinfo=timezone.utc),
        max_requests=1,
    )
    path = tmp_path / "low.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert driver.main(["check-evidence", str(path)]) == 2
    assert "cannot complete a capture" in capsys.readouterr().out


def test_replay_headers_drop_only_the_transport_managed_ones() -> None:
    observed = {
        "Authorization": "t",
        "Schwab-Client-Ids": "",
        "Cookie": "c",
        "content-length": "9",
        "Host": "h",
        ":authority": "x",
        "Schwab-Env": "e",
    }
    assert driver.replay_headers(observed) == {
        "Authorization": "t",
        "Schwab-Client-Ids": "",  # an empty scoping header is kept as the page sends it
        "Schwab-Env": "e",
    }


def test_the_page_body_is_replayed_only_when_it_equals_the_approved_body() -> None:
    raw = '{"isNWE":false}'
    assert driver.check_observed_body(raw, {"isNWE": False}) == raw
    with pytest.raises(driver.DriverError) as extra:
        driver.check_observed_body(
            '{"isNWE":false,"accountsFilter":["SECRET"]}', {"isNWE": False}
        )
    assert "accountsFilter" in str(extra.value) and "SECRET" not in str(extra.value)
    with pytest.raises(driver.DriverError, match="only in the evidence: \\['isNWE'\\]"):
        driver.check_observed_body("{}", {"isNWE": False})
    with pytest.raises(driver.DriverError, match=r"values differ for: \['isNWE'\]"):
        driver.check_observed_body('{"isNWE":true}', {"isNWE": False})
    for bad in (None, "", "not json", "[]"):
        with pytest.raises(driver.DriverError):
            driver.check_observed_body(bad, {"isNWE": False})


def test_attach_check_names_the_body_keys_the_evidence_filter_drops(
    tmp_path: Path,
) -> None:
    body = json.dumps({**BODY, "someOtherKey": 1})
    result = driver.attach_check(
        cdp_url="x",
        connect=lambda url: _Browser([SCHWAB]),
        observe=lambda page: ({"Authorization": "t"}, body, None),
        capture_body_out=tmp_path / "b.json",
    )
    assert result["holdings_body_dropped_keys"] == ["someOtherKey"]
    assert driver.dropped_body_keys("not json") == []


def test_page_fetch_headers_drop_only_what_a_page_script_cannot_set() -> None:
    replay = {
        "Authorization": "t",
        "Schwab-Client-Ids": "",
        "Sec-Fetch-Mode": "cors",
        "sec-ch-ua": "x",
        "Origin": "o",
        "Referer": "r",
        "Content-Type": "application/json",
    }
    assert driver.page_fetch_headers(replay) == {
        "Authorization": "t",
        "Schwab-Client-Ids": "",
        "Content-Type": "application/json",
    }


def test_exception_text_is_value_free_and_bounded() -> None:
    text = driver.value_free_error(
        ValueError("Failed for account 123456789 at 2026-11-08\n" + "x" * 400)
    )
    assert text.startswith("ValueError: Failed for account #")
    assert "123456789" not in text and "\n" not in text and len(text) <= 160
