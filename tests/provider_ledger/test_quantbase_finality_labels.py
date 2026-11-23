"""Quantbase finality: which daily-label shapes the selected capture may have."""

from __future__ import annotations

from datetime import date, timedelta
import hashlib
import importlib.util
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "provider_ledger_quantbase_finality_test", ROOT / "tools" / "provider_ledger.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load provider-ledger builder")
ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger)

END = date(2026, 11, 20)
RELATIVE = "csv/dashboard_Volatis_Strategy_pricehistory.csv"
HOLDINGS = "csv/dashboard_Volatis_Strategy_holdings.csv"


def _day(offset: int) -> date:
    return END + timedelta(days=offset)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    labels: list[int],
    *,
    intraday: object,
    latest: object = END.isoformat(),
    captured_at: str = "2026-11-23T15:05:00.000000Z",
) -> dict[str, Any]:
    content = (
        "time,value,cost_basis\n"
        + "".join(f"{_day(n).strftime('%m/%d/%Y')},1,1\n" for n in labels)
    ).encode()
    monkeypatch.setattr(ledger, "_bound_source_bytes", lambda _m, _a, _p: content)
    evidence: dict[str, Any] = {
        "refreshed_datasets": [Path(HOLDINGS).name, Path(RELATIVE).name],
        "preserved_companions": [],
    }
    if intraday is not None:
        evidence["intraday_unfinalized_dates"] = intraday
    if latest is not None:
        evidence["latest_complete_daily_cutoff"] = latest
    catalog = {
        "legacy_lineage": {"run_id": "run"},
        "datasets": [
            {
                "relative_path": HOLDINGS,
                "snapshot_kind": "CAPTURE_TIME_SNAPSHOT",
                "captured_at": captured_at,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            },
            {
                "relative_path": RELATIVE,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            },
        ],
    }
    context = ledger._FinalityContext(
        selected={},
        path=Path("C:/finality/evidence.json"),
        content=b"",
        digest="",
        size=0,
        manifest=None,  # type: ignore[arg-type]
        catalog=catalog,
        root=Path("C:/root"),
        end=END,
        result={},
        evidence=evidence,
    )
    return ledger._finality_quantbase(context)


BASIS = "CONVERSION_RECEIPT_POLICY_SELECTED_NATIVE_LABEL"
NEXT_DAY_BASIS = "NEXT_DAY_CAPTURE_NO_UNFINALIZED_LABEL"


def test_same_day_capture_with_an_intraday_label_after_the_cutoff_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(monkeypatch, [-1, 0, 1], intraday=[_day(1).isoformat()])
    assert result["basis"] == BASIS


def test_next_day_capture_ending_at_the_cutoff_label_passes_with_no_intraday(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(monkeypatch, [-2, -1, 0], intraday=[])
    assert result["basis"] == NEXT_DAY_BASIS


@pytest.mark.parametrize(
    ("labels", "intraday", "latest"),
    [
        # labels beyond the cutoff but nothing declared intraday
        ([-1, 0, 1], [], END.isoformat()),
        # the file stops before the cutoff
        ([-3, -2, -1], [], END.isoformat()),
        # the cutoff day is not the last finalized label
        ([-2, -1, 0], [], (END - timedelta(days=1)).isoformat()),
        # the complete-through claim is absent
        ([-1, 0], [], None),
        # a declared intraday date that is not a label of the file
        ([-1, 0], [_day(1).isoformat()], END.isoformat()),
        # an intraday date at or before the cutoff
        ([-1, 0, 1], [END.isoformat(), _day(1).isoformat()], END.isoformat()),
        # an intraday date before the cutoff (finalized days are not intraday)
        ([-2, -1, 0], [_day(-1).isoformat()], END.isoformat()),
        # every label declared intraday leaves nothing finalized
        ([1], [_day(1).isoformat()], END.isoformat()),
        # an empty file
        ([], [], END.isoformat()),
        # the intraday list is missing altogether
        ([-1, 0], None, END.isoformat()),
    ],
)
def test_every_other_label_shape_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
    labels: list[int],
    intraday: object,
    latest: object,
) -> None:
    with pytest.raises(ledger.ProviderLedgerError):
        _run(monkeypatch, labels, intraday=intraday, latest=latest)


def test_a_same_day_capture_keeps_the_existing_basis_whatever_its_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(
        monkeypatch,
        [-1, 0, 1],
        intraday=[_day(1).isoformat()],
        captured_at="2026-11-21T21:30:00.000000Z",
    )
    assert result["basis"] == BASIS


@pytest.mark.parametrize(
    "captured_at",
    [
        # the cutoff day itself (the bar may still be intraday), before it, and the
        # New York day of a capture just after midnight UTC
        "2026-11-20T21:30:00.000000Z",
        "2026-11-20T03:00:00.000000Z",
        "2026-11-19T12:00:00.000000Z",
        "2026-11-21T02:00:00.000000Z",
    ],
)
def test_the_next_day_shape_needs_a_capture_after_the_cutoff_day(
    monkeypatch: pytest.MonkeyPatch, captured_at: str
) -> None:
    with pytest.raises(ledger.ProviderLedgerError, match="after the cutoff day"):
        _run(monkeypatch, [-2, -1, 0], intraday=[], captured_at=captured_at)


def test_the_next_day_shape_accepts_the_first_new_york_day_after_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(
        monkeypatch,
        [-2, -1, 0],
        intraday=[],
        captured_at="2026-11-21T15:00:00.000000Z",
    )
    assert result["basis"] == NEXT_DAY_BASIS
