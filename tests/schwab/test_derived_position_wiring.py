"""The derived position row enters the Schwab generation only from pinned evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from quant_engine.schwab import SchwabGenerationError, build_generation

HERE = Path(__file__).parent
_spec = importlib.util.spec_from_file_location(
    "derived_wiring_fixture", HERE / "test_schwab_generation.py"
)
assert _spec is not None and _spec.loader is not None
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)

EXPORT = "\n".join(
    [
        '"Positions for account Joint Tenant ...691 as of 08:20 PM ET, 2026/11/17"',
        "",
        '"Symbol","Description","Qty (Quantity)","Prev Close","Cost Basis","Price","Price Chng $ (Price Change $)","Price Chng % (Price Change %)","Mkt Val (Market Value)","Day Chng $ (Day Change $)","Day Chng % (Day Change %)","Gain $ (Gain/Loss $)","Gain % (Gain/Loss %)","Ratings","Reinvest?","Reinvest Capital Gains?","% of Acct (% of Account)","Cost/Share","Last Tr (ET)","Exchange","Asset Type",',
        '"SWVXX","SCHWAB PRIME ADVANTAGE MONEY INVESTOR","1,000.00","1.00","$1,000.00","1.00","0","0%","$1,000.00","$0.00","0%","$0.00","0%","-","Yes","Yes","99%","$1.00","6:45:00 PM","Mutual Fund","Cash and Money Market",',
        '"Cash & Cash Investments","--","--","--","--","--","--","--","$5.00","$0.00","0%","--","--","--","--","--","1%","--","--","--","Cash and Money Market",',
        '"Positions Total","","--","--","$1,000.00","--","--","--","$1,005.00","$0.00","0%","$0.00","0%","--","--","--","--","--","--","--","--",',
        "",
    ]
)
BALANCES = "Account Value\n$1,005.00\nCash & Cash Investments\n$5.00\nMarket Value\n$1,000.00\n"


def _pin(root: Path, name: str, text: str) -> dict[str, object]:
    payload = text.encode("utf-8")
    (root / name).write_bytes(payload)
    return {
        "artifact_id": f"evidence/{name}",
        "expected_sha256": hashlib.sha256(payload).hexdigest(),
        "expected_size_bytes": len(payload),
        "relative_path": name,
        "root_id": "source",
    }


def _spec_with_derived(tmp_path: Path, **over: Any) -> Path:
    spec = fixture._web_snapshot_spec(tmp_path)  # type: ignore[attr-defined]
    first = fixture._without_positions_of_first_account(  # type: ignore[attr-defined]
        spec, liquidation_available=True
    )
    document = json.loads(spec.read_text(encoding="utf-8"))
    root = Path(document["roots"]["source"])
    as_of = document["as_of_date"]
    item: dict[str, Any] = {
        "account_id": first,
        "balances": _pin(root, "bal-691.txt", BALANCES),
        "capture_date": as_of,
        "cutoff": as_of,
        "earlier_balances": [
            {"day": as_of, "page": _pin(root, "bal-earlier.txt", BALANCES)}
        ],
        "export": _pin(root, "pos-691.csv", EXPORT),
        "history_through": as_of,
    }
    item.update(over)
    document["derived_position_rows"] = [item]
    document["baseline"]["position_count"] += 1
    spec.write_text(json.dumps(document), encoding="utf-8")
    return spec


def test_the_derived_row_enters_the_catalog_with_its_evidence(tmp_path: Path) -> None:
    catalog, manifest, _bytes = build_generation(_spec_with_derived(tmp_path))
    rows = [r for r in catalog["positions"] if r.get("symbol") == "SWVXX"]  # type: ignore[union-attr]
    assert len(rows) == 1
    row = rows[0]
    assert row["as_of_basis"] == "DERIVED_AT_CUTOFF_FROM_LATER_SNAPSHOT"
    assert row["market_value"]["value"] == "1000.00"
    assert row["record_id"].startswith("position-")
    ids = {a.artifact_id for a in manifest.artifacts}
    assert {
        "evidence/pos-691.csv",
        "evidence/bal-691.txt",
        "evidence/bal-earlier.txt",
    } <= ids


def test_a_pin_that_differs_refuses(tmp_path: Path) -> None:
    """KILL: evidence whose hash differs from its pin is refused."""

    spec = _spec_with_derived(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    root = Path(document["roots"]["source"])
    (root / "pos-691.csv").write_bytes(EXPORT.replace("1,000.00", "1,001.00").encode())
    with pytest.raises(SchwabGenerationError, match="hash differs"):
        build_generation(spec)


def test_a_snapshot_that_is_not_the_cutoff_refuses(tmp_path: Path) -> None:
    """KILL: an export that does not close, or an earlier market value that differs."""

    spec = _spec_with_derived(tmp_path)
    document = json.loads(spec.read_text(encoding="utf-8"))
    root = Path(document["roots"]["source"])
    item = document["derived_position_rows"][0]
    item["earlier_balances"][0]["page"] = _pin(
        root, "bal-moved.txt", BALANCES.replace("$1,000.00", "$900.00")
    )
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError, match="derived position refused"):
        build_generation(spec)


def test_the_spec_key_is_unknown_without_the_current_snapshot_schema(
    tmp_path: Path,
) -> None:
    spec = fixture._spec(tmp_path)  # type: ignore[attr-defined]
    document = json.loads(spec.read_text(encoding="utf-8"))
    document["derived_position_rows"] = []
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchwabGenerationError):
        build_generation(spec)


def test_an_account_with_a_derived_position_can_never_be_declared_empty(
    tmp_path: Path,
) -> None:
    """KILL (ci-qc-2): the money-market fund sits inside 'Cash & Cash Investments', so a genuine-looking
    Balances page can truthfully show Market Value 0.00 for an account that holds SWVXX (691).  With the
    derived row present the declaration must refuse, whatever the page says."""

    spec = _spec_with_derived(tmp_path)
    fixture._set_first_account_cash(spec, "100")  # type: ignore[attr-defined]
    document = json.loads(spec.read_text(encoding="utf-8"))
    root = Path(document["roots"]["source"])
    evidence = root / "balances-evidence.txt"
    evidence.write_bytes(fixture._page("001", "100.00", "0.00"))  # type: ignore[attr-defined]
    document["declared_empty_position_accounts"] = [
        fixture._empty_declaration(
            document["derived_position_rows"][0]["account_id"], evidence
        )  # type: ignore[attr-defined]
    ]
    spec.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(
        SchwabGenerationError, match="declared empty|has a position row"
    ):
        build_generation(spec)
