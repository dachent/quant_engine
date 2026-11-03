"""Crosswalk authoring and the sign-convention check, offline and synthetic."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pytest

from alpaca_capture_support import ACCOUNT_ID, fill, nontrade
from quant_engine.alpaca.capture import cli
from quant_engine.alpaca.capture.ingest import (
    IngestRefused,
    ingest_run,
    load_crosswalk,
)
from quant_engine.alpaca.capture.signcheck import (
    SignCheckRefused,
    run_sign_check,
    write_crosswalk,
)
from quant_engine.alpaca.capture.util import sha256_text
from test_capture_ingest import (
    AFTER,
    INTERNAL,
    NET_CASH,
    PAGES,
    UNTIL,
    _make_run,
    _out,
)

DIGEST = sha256_text(ACCOUNT_ID)


def _pages(
    fee: str = "-0.10", jnl: str = "5", dividend: str = "0.42"
) -> list[list[dict[str, Any]]]:
    return [
        PAGES[0],
        [nontrade(3, "DIV", dividend, symbol="SYNA"), nontrade(4, "CSD", "200")],
        [nontrade(5, "FEE", fee), nontrade(6, "CSW", "-25")],
        [nontrade(7, "JNL", jnl), PAGES[3][1]],
        [],
    ]


def _package(tmp_path: Path, pages: list[list[dict[str, Any]]] | None = None) -> Path:
    run = _make_run(tmp_path, pages)
    crosswalk = tmp_path / "cw.json"
    write_crosswalk(plan_digest=DIGEST, internal_account_id=INTERNAL, out=crosswalk)
    return ingest_run(
        run_directory=run,
        crosswalk=load_crosswalk(crosswalk),
        internal_account_id=INTERNAL,
        expect_after=AFTER,
        expect_until=UNTIL,
        output_root=_out(tmp_path),
    ).package_directory


def _verdicts(report: Any) -> dict[str, str]:
    return {item.activity_type: item.verdict for item in report.types}


# --- crosswalk authoring -------------------------------------------------


def test_generated_crosswalk_is_accepted_by_the_ingest(tmp_path: Path) -> None:
    out = tmp_path / "crosswalk.json"
    digest = write_crosswalk(plan_digest=DIGEST, internal_account_id=INTERNAL, out=out)
    assert len(digest) == 64
    assert load_crosswalk(out) == {INTERNAL: DIGEST}
    run = _make_run(tmp_path)
    result = ingest_run(
        run_directory=run,
        crosswalk=load_crosswalk(out),
        internal_account_id=INTERNAL,
        expect_after=AFTER,
        expect_until=UNTIL,
        output_root=_out(tmp_path),
    )
    assert result.activity_count == 8
    assert ACCOUNT_ID not in out.read_text(encoding="utf-8")


def test_a_wrong_plan_digest_is_refused_by_the_ingest(tmp_path: Path) -> None:
    out = tmp_path / "wrong.json"
    write_crosswalk(
        plan_digest=sha256_text("SOME-OTHER-ACCOUNT"),
        internal_account_id=INTERNAL,
        out=out,
    )
    run = _make_run(tmp_path)
    with pytest.raises(IngestRefused, match="differs from the crosswalk"):
        ingest_run(
            run_directory=run,
            crosswalk=load_crosswalk(out),
            internal_account_id=INTERNAL,
            expect_after=AFTER,
            expect_until=UNTIL,
            output_root=_out(tmp_path),
        )


@pytest.mark.parametrize("digest", ["", "abc", "A" * 64, "g" * 64, "ab" * 33])
def test_malformed_plan_digests_are_refused(tmp_path: Path, digest: str) -> None:
    with pytest.raises(SignCheckRefused, match="SHA-256"):
        write_crosswalk(
            plan_digest=digest, internal_account_id=INTERNAL, out=tmp_path / "x.json"
        )
    assert not (tmp_path / "x.json").exists()


def test_crosswalk_authoring_is_create_only_and_validates_the_account(
    tmp_path: Path,
) -> None:
    out = tmp_path / "c.json"
    write_crosswalk(plan_digest=DIGEST, internal_account_id=INTERNAL, out=out)
    before = out.read_bytes()
    with pytest.raises(SignCheckRefused, match="create-only"):
        write_crosswalk(plan_digest=DIGEST, internal_account_id=INTERNAL, out=out)
    assert out.read_bytes() == before
    with pytest.raises(SignCheckRefused, match="account id"):
        write_crosswalk(
            plan_digest=DIGEST, internal_account_id="acct-1", out=tmp_path / "d.json"
        )
    with pytest.raises(SignCheckRefused, match="absolute"):
        write_crosswalk(
            plan_digest=DIGEST, internal_account_id=INTERNAL, out="rel.json"
        )


def test_cli_crosswalk_then_ingest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "cli.json"
    code = cli.main(
        [
            "crosswalk",
            "--plan-digest",
            DIGEST,
            "--internal-account-id",
            INTERNAL,
            "--out",
            str(out),
        ]
    )
    assert code == 0 and json.loads(capsys.readouterr().out)["crosswalk_sha256"]
    assert load_crosswalk(out) == {INTERNAL: DIGEST}
    code = cli.main(
        [
            "crosswalk",
            "--plan-digest",
            "00",
            "--internal-account-id",
            INTERNAL,
            "--out",
            str(tmp_path / "bad.json"),
        ]
    )
    assert code == 2 and "SHA-256" in capsys.readouterr().out


# --- the sign check ------------------------------------------------------


def test_reported_signs_that_reproduce_the_balance_movement_pass(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    report = run_sign_check(
        package_directory=package,
        opening_cash="1000",
        closing_cash="1181.32",
        closing_from_snapshot=False,
    )
    assert report.blessed and report.totals_reconcile
    assert report.activity_total == Decimal(NET_CASH)
    assert report.residual_as_reported == 0
    assert _verdicts(report) == {
        "CSD": "PASS",
        "CSW": "PASS",
        "DIV": "PASS",
        "FEE": "PASS",
        "FILL": "DERIVED",
        "JNL": "PASS",
        "JNLS": "INCONCLUSIVE",
    }
    assert report.to_dict()["overall"] == "PASS"


def test_a_fee_reported_with_the_opposite_sign_fails(tmp_path: Path) -> None:
    # The provider shows fees positive; the true balance fell by 0.10.
    package = _package(tmp_path, _pages(fee="0.10"))
    report = run_sign_check(
        package_directory=package,
        opening_cash="1000",
        closing_cash="1181.32",
        closing_from_snapshot=False,
    )
    verdicts = _verdicts(report)
    assert verdicts["FEE"] == "FAIL"
    assert verdicts["CSD"] == "PASS" and verdicts["JNL"] == "PASS"
    assert not report.blessed and report.residual_as_reported == Decimal("-0.20")
    fee = next(item for item in report.types if item.activity_type == "FEE")
    assert fee.residual_if_flipped == 0


def test_a_journal_reported_with_the_opposite_sign_fails(tmp_path: Path) -> None:
    package = _package(tmp_path, _pages(jnl="-5"))
    report = run_sign_check(
        package_directory=package,
        opening_cash="1000",
        closing_cash="1181.32",
        closing_from_snapshot=False,
    )
    assert _verdicts(report)["JNL"] == "FAIL" and not report.blessed


def test_a_global_inversion_of_every_non_fill_type_is_caught(tmp_path: Path) -> None:
    pages = [
        [nontrade(1, "DIV", "-0.42", symbol="SYNA"), nontrade(2, "FEE", "0.10")],
        [nontrade(3, "JNL", "-5")],
    ]
    package = _package(tmp_path, pages)
    # True movement is the opposite of every reported amount: +0.42 -0.10 +5.
    report = run_sign_check(
        package_directory=package,
        opening_cash="0",
        closing_cash="5.32",
        closing_from_snapshot=False,
    )
    assert report.global_inversion_fits_better and not report.blessed
    assert report.residual_all_flipped == 0


def test_a_residual_blocks_the_blessing_without_blaming_a_type(tmp_path: Path) -> None:
    package = _package(tmp_path)
    report = run_sign_check(
        package_directory=package,
        opening_cash="1000",
        closing_cash="1181.35",
        closing_from_snapshot=False,
    )
    assert not report.totals_reconcile and not report.blessed
    assert report.residual_as_reported == Decimal("0.03")
    assert "FAIL" not in _verdicts(report).values()
    blessed = run_sign_check(
        package_directory=package,
        opening_cash="1000",
        closing_cash="1181.35",
        closing_from_snapshot=False,
        tolerance="0.03",
    )
    assert blessed.blessed


def test_closing_from_the_account_snapshot(tmp_path: Path) -> None:
    package = _package(tmp_path)
    # Snapshot cash is 100.25, so the implied opening cash is 100.25 - 181.32.
    report = run_sign_check(
        package_directory=package,
        opening_cash="-81.07",
        closing_cash=None,
        closing_from_snapshot=True,
    )
    assert report.blessed and report.closing_basis == "account_snapshot_cash"
    assert report.closing_cash == Decimal("100.25")


def test_inputs_are_validated_and_the_report_is_create_only(tmp_path: Path) -> None:
    package = _package(tmp_path)
    for kwargs in (
        {"closing_cash": "1", "closing_from_snapshot": True},
        {"closing_cash": None, "closing_from_snapshot": False},
    ):
        with pytest.raises(SignCheckRefused, match="exactly one"):
            run_sign_check(package_directory=package, opening_cash="0", **kwargs)
    with pytest.raises(SignCheckRefused, match="plain decimal"):
        run_sign_check(
            package_directory=package,
            opening_cash="1e3",
            closing_cash="1",
            closing_from_snapshot=False,
        )
    with pytest.raises(SignCheckRefused, match="negative"):
        run_sign_check(
            package_directory=package,
            opening_cash="0",
            closing_cash="1",
            closing_from_snapshot=False,
            tolerance="-1",
        )
    (package / "stray.txt").write_text("x")
    with pytest.raises(SignCheckRefused, match="native activities refused"):
        run_sign_check(
            package_directory=package,
            opening_cash="0",
            closing_cash="1",
            closing_from_snapshot=False,
        )
    (package / "stray.txt").unlink()
    report_path = tmp_path / "sign-check.json"
    run_sign_check(
        package_directory=package,
        opening_cash="1000",
        closing_cash="1181.32",
        closing_from_snapshot=False,
        out=report_path,
    )
    value = json.loads(report_path.read_text(encoding="utf-8"))
    assert value["overall"] == "PASS" and len(value["binding_sha256"]) == 64
    with pytest.raises(SignCheckRefused, match="create-only"):
        run_sign_check(
            package_directory=package,
            opening_cash="1000",
            closing_cash="1181.32",
            closing_from_snapshot=False,
            out=report_path,
        )


def test_cli_signcheck_exit_codes_and_arithmetic_only_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = _package(tmp_path)
    base = ["signcheck", "--package-dir", str(package), "--opening-cash", "1000"]
    assert cli.main([*base, "--closing-cash", "1181.32"]) == 0
    output = capsys.readouterr().out
    value = json.loads(output)
    assert value["overall"] == "PASS" and value["blessed"] is True
    assert ACCOUNT_ID not in output and "synthetic" not in output
    assert cli.main([*base, "--closing-cash", "1000"]) == 1
    assert json.loads(capsys.readouterr().out)["overall"] == "FAIL"
    assert cli.main([*base]) == 2
    assert "exactly one" in capsys.readouterr().out


def test_fill_legs_are_derived_and_never_flipped(tmp_path: Path) -> None:
    package = _package(tmp_path, [[fill(1), fill(2, side="sell", price="11")], []])
    report = run_sign_check(
        package_directory=package,
        opening_cash="0",
        closing_cash="1",
        closing_from_snapshot=False,
    )
    assert _verdicts(report) == {"FILL": "DERIVED"} and report.blessed
