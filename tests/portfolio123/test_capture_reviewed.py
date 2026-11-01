"""Staging and verification of the capabilities confirmed from preserved evidence.

Fixtures are invented values in the REAL field structure of the designer-model
route and the real transactions CSV header.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from p123_capture_fixtures import (
    DESIGNER,
    DESIGNER_COMMENT,
    DESIGNER_PERSON,
    EXPIRES,
    NOW,
    ORIGIN,
    TRANSACTIONS_CSV,
    entry,
    standard_entries,
    write_har,
)
from quant_engine.portfolio123 import capture_catalog
from quant_engine.portfolio123.capture_catalog import CaptureRefusal
from quant_engine.portfolio123.capture_intake import StageRequest, stage_run, verify_run
from quant_engine.portfolio123.capture_shapes import canonical_bytes

DESIGNER_URL = f"{ORIGIN}/spr/strategy/model/9005?dm=true"
SCOPE = ("designer-model.json",)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "staging"
    path.mkdir()
    return path


def _request(
    root: Path,
    tmp_path: Path,
    entries: list[dict[str, Any]],
    scope: tuple[str, ...] = SCOPE,
    run_id: str = "designer-har",
) -> StageRequest:
    source = write_har(tmp_path / "export.har", entries)
    return StageRequest(root, run_id, "har", source, scope, NOW, EXPIRES)


def _code(request: StageRequest) -> str:
    with pytest.raises(CaptureRefusal) as caught:
        stage_run(request)
    return caught.value.code


def _stage_designer(root: Path, tmp_path: Path) -> Path:
    stage_run(_request(root, tmp_path, [entry(DESIGNER_URL, DESIGNER)]))
    return root / "p123-capture" / "designer-har"


def test_reviewed_scope_is_marked_and_the_verifier_recomputes_it(
    root: Path, tmp_path: Path
) -> None:
    run = _stage_designer(root, tmp_path)
    manifest = json.loads((run / "manifest.json").read_bytes())
    assert manifest["evidence_status"] == "REVIEWED_SHAPES"
    assert manifest["catalog_sha256"] == capture_catalog.catalog_sha256()
    assert verify_run(root, "designer-har").records == 1
    # A scope that includes any unverified capability is never marked reviewed.
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    stage_run(
        _request(
            root,
            mixed,
            standard_entries(),
            (
                "aifactor.predictor",
                "aifactor.model",
                "screen.definition",
                "simulation.result",
                "designer-model.json",
                "billing.credit-table",
            ),
            run_id="mixed-run",
        )
    )
    mixed_run = root / "p123-capture" / "mixed-run"
    flagged = json.loads((mixed_run / "manifest.json").read_bytes())
    assert flagged["evidence_status"] == "UNVERIFIED_SYNTHETIC_SHAPES"
    # Forging the status in either direction is refused by the verifier.
    for directory, document, status in (
        (mixed_run, flagged, "REVIEWED_SHAPES"),
        (run, manifest, "UNVERIFIED_SYNTHETIC_SHAPES"),
    ):
        (directory / "manifest.json").write_bytes(
            canonical_bytes({**document, "evidence_status": status})
        )
        with pytest.raises(CaptureRefusal) as caught:
            verify_run(root, directory.name)
        assert caught.value.code == "MANIFEST_INVALID"


def test_designer_personal_data_never_reaches_normalized_records(
    root: Path, tmp_path: Path
) -> None:
    har_run = _stage_designer(root, tmp_path)
    path = tmp_path / "model.json"
    path.write_bytes(json.dumps(DESIGNER).encode())
    stage_run(StageRequest(root, "designer-file", "file", path, SCOPE, NOW))
    file_run = root / "p123-capture" / "designer-file"
    responses = []
    for run, raw in ((har_run, "source.har"), (file_run, "source.json")):
        text = (run / "normalized" / "records.json").read_text()
        assert DESIGNER_PERSON not in text and DESIGNER_COMMENT not in text
        assert "[REDACTED]" in text
        # The restricted raw copy is a byte copy of the export and keeps them.
        assert DESIGNER_PERSON in (run / "raw" / raw).read_text()
        responses.append(json.loads(text)["records"][0]["response"])
    # The two forms of one capability normalize to the same redacted response.
    assert responses[0] == responses[1]
    assert verify_run(root, "designer-har").records == 1
    assert verify_run(root, "designer-file").records == 1


def test_designer_har_refuses_query_route_and_shape_drift(
    root: Path, tmp_path: Path
) -> None:
    def code(entries: list[dict[str, Any]]) -> str:
        return _code(_request(root, tmp_path, entries, run_id="x"))

    base = f"{ORIGIN}/spr/strategy/model/9005"
    assert code([entry(f"{base}?dm=false", DESIGNER)]) == "HAR_ENTRY_REJECTED"
    assert code([entry(base, DESIGNER)]) == "NO_REVIEWED_ROUTES"
    assert code([entry(f"{base}?dm=true&x=1", DESIGNER)]) == "NO_REVIEWED_ROUTES"
    assert code([entry(f"{base}/perf?dm=true", DESIGNER)]) == "NO_REVIEWED_ROUTES"
    assert code([entry(DESIGNER_URL, DESIGNER, method="POST")]) == "NO_REVIEWED_ROUTES"
    assert code([entry(DESIGNER_URL, DESIGNER, status=401)]) == "HAR_ENTRY_REJECTED"
    drifted = copy.deepcopy(DESIGNER)
    drifted["inceptionDt"] = "2028-01-03"
    assert code([entry(DESIGNER_URL, drifted)]) == "SHAPE_REFUSED"
    extra = copy.deepcopy(DESIGNER)
    extra["unreviewed"] = 1
    assert code([entry(DESIGNER_URL, extra)]) == "SHAPE_REFUSED"
    assert not (root / "p123-capture" / "x").exists()


def test_designer_file_refuses_unreviewed_fields(root: Path, tmp_path: Path) -> None:
    extra = copy.deepcopy(DESIGNER)
    extra["designerModel"]["unreviewed"] = 1
    path = tmp_path / "model.json"
    path.write_bytes(json.dumps(extra).encode())
    request = StageRequest(root, "bad-file", "file", path, SCOPE, NOW)
    assert _code(request) == "SHAPE_REFUSED"


def test_real_transactions_header_is_the_only_reviewed_download(
    root: Path, tmp_path: Path
) -> None:
    path = tmp_path / "tx.csv"
    path.write_bytes(TRANSACTIONS_CSV.encode())
    scope = ("transactions.download",)
    stage_run(StageRequest(root, "tx-run", "file", path, scope, NOW))
    manifest = json.loads(
        (root / "p123-capture" / "tx-run" / "manifest.json").read_bytes()
    )
    assert manifest["evidence_status"] == "REVIEWED_SHAPES"
    assert verify_run(root, "tx-run").records == 1
    placeholder_header = "Date,Ticker,Action,Shares,Price,Commission"
    old = tmp_path / "old.csv"
    old.write_text(placeholder_header + "\n2029-06-01,A,BUY,1,1,0\n", encoding="utf-8")
    assert (
        _code(StageRequest(root, "tx-old", "file", old, scope, NOW))
        == "CSV_COLUMNS_UNREVIEWED"
    )


def test_cli_catalog_reports_the_verdict_per_capability(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from quant_engine.portfolio123 import capture_cli

    assert capture_cli.main(["catalog"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["catalog_sha256"] == capture_catalog.catalog_sha256()
    verdicts = printed["verdicts"]
    assert verdicts["designer-model.json"] == "CONFIRMED"
    assert verdicts["transactions.download"] == "CONFIRMED"
    assert verdicts["aifactor.predictor"] == "UNVERIFIED"
    assert set(verdicts) == set(printed["capabilities"])
