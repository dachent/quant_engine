from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest

import portfolio_implementation_reconciliation as reconciliation


def test_the_observed_end_table_reads_each_shape_and_marks_an_absent_window() -> None:
    table = reconciliation.observed_end_table(
        [
            {
                "strategy_id": "tt-portfolio",
                "return_reconciliation": {"observed_end": "2026-09-16"},
            },
            {
                "strategy_id": "p123-ai-factor",
                "return_reconciliation": {"status": "UNAVAILABLE_POLICY_NOT_ADMITTED"},
                "account_live_return_summary": {"observed_end": "2026-09-17"},
            },
            {"strategy_id": "grizzlybears-vtma", "observed_end": "2026-09-10"},
            {"strategy_id": "no-window", "return_reconciliation": {}},
        ],
        date(2026, 9, 17),
    )
    assert [
        (row["strategy_id"], row["observed_end"], row["lag_days"]) for row in table
    ] == [
        ("tt-portfolio", "2026-09-16", 1),
        ("p123-ai-factor", "2026-09-17", 0),
        ("grizzlybears-vtma", "2026-09-10", 7),
        ("no-window", None, None),
    ]


def test_the_observed_end_table_prints_one_line_per_strategy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reconciliation.print_observed_end_table(
        reconciliation.observed_end_table(
            [{"strategy_id": "tt-portfolio", "observed_end": "2026-09-16"}],
            date(2026, 9, 17),
        )
    )
    assert (
        capsys.readouterr().err
        == "observed_end tt-portfolio: 2026-09-16 (as-of 2026-09-17, lag 1 d)\n"
    )


# -- the builders' main() seam: stdout summary (the driver's stdout_to log) and stderr table --------

STRATEGIES = [
    {
        "strategy_id": "tt-portfolio",
        "return_reconciliation": {"observed_end": "2026-09-16"},
    },
    {"strategy_id": "grizzlybears-vtma", "observed_end": "2026-09-10"},
]


def _reconciliation_args(tmp_path: Path) -> list[str]:
    paths = [
        "--p123-generation-root",
        "--ibkr-generation-root",
        "--ibkr-xml-root",
        "--ibkr-current-xml",
        "--schwab-catalog",
        "--provider-ledger-root",
        "--tt-source-manifest",
        "--grizzly-signals",
        "--spxl-market-data",
    ]
    args: list[str] = []
    for flag in paths:
        args += [flag, str(tmp_path / flag.strip("-"))]
    return args + ["--as-of-date", "2026-09-17", "--output", str(tmp_path / "out.json")]


def test_the_reconciliation_main_keeps_its_summary_and_adds_the_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    content = json.dumps(
        {
            "as_of_date": "2026-09-17",
            "reconciliation_id": "recon-1",
            "strategies": STRATEGIES,
            "verification": "V",
        }
    ).encode("utf-8")
    monkeypatch.setattr(reconciliation, "build_reconciliation", lambda **_: content)
    assert reconciliation.main(_reconciliation_args(tmp_path)) == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1  # one JSON line: what the driver's stdout_to log holds
    summary = json.loads(lines[0])
    assert summary["reconciliation_id"] == "recon-1"
    assert summary["strategies"] == ["tt-portfolio", "grizzlybears-vtma"]
    assert summary["verification"] == "V"
    assert summary["output"] == str(tmp_path / "out.json")
    assert [
        (row["strategy_id"], row["observed_end"], row["lag_days"])
        for row in summary["observed_end_vs_as_of"]
    ] == [("tt-portfolio", "2026-09-16", 1), ("grizzlybears-vtma", "2026-09-10", 7)]
    assert captured.err.splitlines() == [
        "observed_end tt-portfolio: 2026-09-16 (as-of 2026-09-17, lag 1 d)",
        "observed_end grizzlybears-vtma: 2026-09-10 (as-of 2026-09-17, lag 7 d)",
    ]
    assert (tmp_path / "out.json").read_bytes() == content


def test_the_attribution_main_keeps_its_summary_and_adds_the_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import portfolio_implementation_return_attribution as attribution

    content = json.dumps(
        {
            "as_of_date": "2026-09-17",
            "attribution_id": "attr-1",
            "strategies": STRATEGIES,
        }
    ).encode("utf-8")
    monkeypatch.setattr(attribution, "build_attribution", lambda **_: content)
    flags = [
        "--provider-ledger-root",
        "--schwab-catalog",
        "--p123-generation-root",
        "--ibkr-generation-root",
        "--ibkr-xml-root",
        "--ibkr-current-xml",
        "--grizzly-signals",
        "--grizzly-market-data",
        "--tt-source-manifest",
        "--tt-market-data",
    ]
    argv: list[str] = []
    for flag in flags:
        argv += [flag, str(tmp_path / flag.strip("-"))]
    argv += ["--as-of-date", "2026-09-17", "--output", str(tmp_path / "attr.json")]
    assert attribution.main(argv) == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert summary["attribution_id"] == "attr-1"
    assert summary["output"] == str(tmp_path / "attr.json")
    assert [row["lag_days"] for row in summary["observed_end_vs_as_of"]] == [1, 7]
    assert captured.err.count("observed_end ") == 2
    assert (tmp_path / "attr.json").read_bytes() == content
