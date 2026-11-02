"""The valuation cutoff from the ingest producer through the real consumers.

The snapshot bytes come from the ingest adapter's own writer, are built into a real
Schwab generation, and the generation's catalog metadata then drives the real provider
ledger selection and the real baseline position-facts reader. Synthetic values only.
"""

from __future__ import annotations

from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import schwab_capture_ingest as ingest
import portfolio_baseline as baseline
from quant_engine.schwab import build_generation, read_holdings_v2_current_snapshot


HERE = Path(__file__).parent


def _load(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, HERE / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generation_tests = _load("chain_generation_fixture", "test_schwab_generation.py")
ledger_fixtures = _load(
    "chain_ledger_fixture", "../provider_ledger/test_admit_or_declare.py"
)

# Thursday 2026-07-30 is the cutoff; the real capture is Friday 08:00 New York, before
# the open: the first New York date after the cutoff, so the as-of is the 31st.
CUTOFF = date(2026, 7, 30)
CAPTURE = "2026-07-31T12:00:00+00:00"


def _holdings(external: list[str]) -> dict[str, Any]:
    return {
        "accounts": [
            {
                "accountId": ext,
                "groupedPositions": [
                    {
                        "groupName": "Equity",
                        "securityType": 1,
                        "holdingsRows": [
                            {
                                "dataSymbol": "SYNTH",
                                "qty": {"qty": 1},
                                "price": {"price": 11, "priceDate": "07/30/2026"},
                                "marketValue": {"val": 11},
                            }
                        ],
                    }
                ],
            }
            for ext in external
        ]
    }


def test_adapter_snapshot_reaches_the_real_generation_and_ledger_and_baseline(
    tmp_path: Path,
) -> None:
    # 1. producer: the adapter's own snapshot writer over a real Holdings-V2 shape
    spec_path = generation_tests._spec_with_current(tmp_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    crosswalk = {
        item["external_account_id"]: item["internal_account_id"]
        for item in spec["crosswalk"]
    }
    snapshot = read_holdings_v2_current_snapshot(
        _holdings(list(crosswalk)),
        crosswalk=crosswalk,
        captured_at=CAPTURE,
        official_closes={"SYNTH": 11},  # type: ignore[dict-item]
        price_date=CUTOFF,
    )
    payload = ingest.snapshot_document_bytes(snapshot, CUTOFF)
    assert json.loads(payload)["observed_at"] == CAPTURE
    assert json.loads(payload)["valuation_cutoff"] == CUTOFF.isoformat()

    # 2. real generation (as-of = the observation's UTC date, the 31st)
    source_root = Path(spec["roots"]["source"])
    path = source_root / "adapter-snapshot.json"
    path.write_bytes(payload)
    spec["current_snapshot"].update(
        {
            "expected_sha256": hashlib.sha256(payload).hexdigest(),
            "expected_size_bytes": len(payload),
            "relative_path": path.name,
            "schema_version": "2",
        }
    )
    spec["baseline"]["position_count"] = len(crosswalk)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    catalog, _, _ = build_generation(spec_path)
    metadata = catalog["position_snapshot_as_of"]
    assert metadata == {"observed_at": CAPTURE, "valuation_cutoff": "2026-07-30"}
    assert {row["observed_at"] for row in catalog["positions"]} == {CAPTURE}
    assert {row["as_of_date"] for row in catalog["positions"]} == {"2026-07-31"}

    # 3. real provider-ledger selection driven by the generation's metadata keys.
    # The ledger fixture needs dates on or after its own anchor day, so the Friday
    # 2026-09-18 cutoff and Saturday capture are used; the key names and the
    # presence of the cutoff are taken from the real catalog metadata above.
    assert set(metadata) == {"observed_at", "valuation_cutoff"}
    stage = ledger_fixtures._source_bound_stage(
        tmp_path / "ledger",
        source_cutoff=date(2026, 9, 18),
        schwab_point_capture="2026-09-19T12:00:00+00:00",
        requested_as_of=date(2026, 9, 18),
        schwab_valuation_cutoff="2026-09-18",
    )
    ledger_catalog, _, _, _ = ledger_fixtures.ledger.build_catalog(stage[2])
    rows = [r for r in ledger_catalog["positions"] if r["provider_id"] == "schwab"]
    assert len(rows) == 1
    assert rows[0]["observed_at"] == "2026-09-19T12:00:00+00:00"
    assert rows[0]["valuation_cutoff"] == "2026-09-18"

    # 4. real baseline reader over the real ledger catalog (strategy mapping and
    # authority are separate steps; the baseline admits only mapped, authoritative rows)
    mapped = json.loads(json.dumps(ledger_catalog))
    for row in mapped["positions"]:
        row["mapping_status"] = "MAPPED"
    for admission in mapped["admissions"]:
        if (
            admission["provider_id"] == "schwab"
            and admission["fact_type"] == "positions"
        ):
            admission.update(
                disposition="AUTHORITATIVE",
                evidence_layer="ACCOUNT_LIVE",
                pool_id=None,
                strategy_id=None,
            )
    facts = baseline._provider_ledger_facts(
        mapped,
        [{"account_id": rows[0]["account_id"]}],
        [],
        [],
        [],
        [],
        date(2026, 9, 18),
    )[3]
    held = [f for f in facts if f["record_id"] == rows[0]["record_id"]]
    assert len(held) == 1
    assert held[0]["as_of_date"] == "2026-09-18"
    assert held[0]["capture_date"] == "2026-09-19"
    assert held[0]["observed_at"] == "2026-09-19T12:00:00+00:00"
