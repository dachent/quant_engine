from __future__ import annotations

import json
from pathlib import Path

import pytest

from quant_engine.matrixport.pipeline import (
    DATABASE_NAME,
    ExtractionError,
    MatrixportExtractor,
    stable_key,
    validate_run_id,
)
from quant_engine.matrixport.storage import RunStorage
from quant_engine.matrixport.transport import SanitizedResponse


class CountingTransport:
    """Small deterministic transport that never leaves the test process."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, path: str, *, params=None) -> SanitizedResponse:
        safe_params = dict(params or {})
        self.calls.append((path, safe_params))
        content = json.dumps(
            {"code": 0, "data": [{"product_id": "p1", "shares": "1.25"}]},
            separators=(",", ":"),
        ).encode("utf-8")
        return SanitizedResponse(
            status_code=200,
            content=content,
            request_id=f"offline-{len(self.calls)}",
            elapsed_ms=1,
            endpoint_name="strategy_holding",
        )


def _disable_network_extractors(extractor: MatrixportExtractor) -> None:
    extractor._extract_strategy = lambda: None  # type: ignore[method-assign]
    extractor._extract_wallet = lambda: None  # type: ignore[method-assign]
    extractor._extract_rfq = lambda: None  # type: ignore[method-assign]
    extractor._extract_prime = lambda: None  # type: ignore[method-assign]


def test_keyboard_interrupt_preserves_running_run_and_resume_reuses_raw(
    tmp_path: Path,
) -> None:
    run_id = "20260725T120000.000Z"
    first_transport = CountingTransport()
    interrupted = MatrixportExtractor(
        tmp_path,
        mode="full",
        run_id=run_id,
        transport=first_transport,
    )

    def interrupt_after_durable_response() -> None:
        interrupted._call("/strategy/api/v2/user/holding")
        raise KeyboardInterrupt

    interrupted._extract_strategy = interrupt_after_durable_response  # type: ignore[method-assign]
    interrupted._extract_wallet = lambda: None  # type: ignore[method-assign]
    interrupted._extract_rfq = lambda: None  # type: ignore[method-assign]
    interrupted._extract_prime = lambda: None  # type: ignore[method-assign]
    try:
        with pytest.raises(KeyboardInterrupt):
            interrupted.extract()
    finally:
        interrupted.close()

    run_dir = tmp_path / "output" / run_id
    with RunStorage(run_dir / "state" / DATABASE_NAME) as storage:
        assert storage.get_run(run_id)["status"] == "RUNNING"
        requests = list(storage.iter_requests(run_id))
        assert len(requests) == 1
        assert requests[0]["endpoint"] == "/strategy/api/v2/user/holding"

    assert len(first_transport.calls) == 1
    raw_before = sorted(
        (path.relative_to(run_dir), path.read_bytes())
        for path in (run_dir / "raw").rglob("*")
        if path.is_file()
    )
    assert raw_before

    resume_transport = CountingTransport()
    resumed = MatrixportExtractor(
        tmp_path,
        mode="full",
        run_id=run_id,
        resume=True,
        transport=resume_transport,
    )

    def replay_durable_response() -> None:
        call = resumed._call("/strategy/api/v2/user/holding")
        assert call.resumed is True

    resumed._extract_strategy = replay_durable_response  # type: ignore[method-assign]
    resumed._extract_wallet = lambda: None  # type: ignore[method-assign]
    resumed._extract_rfq = lambda: None  # type: ignore[method-assign]
    resumed._extract_prime = lambda: None  # type: ignore[method-assign]
    try:
        result = resumed.extract()
    finally:
        resumed.close()

    assert result.status == "COMPLETED"
    assert result.request_count == 1
    assert resume_transport.calls == []
    raw_after = sorted(
        (path.relative_to(run_dir), path.read_bytes())
        for path in (run_dir / "raw").rglob("*")
        if path.is_file()
    )
    assert raw_after == raw_before

    with RunStorage(run_dir / "state" / DATABASE_NAME) as storage:
        assert storage.get_run(run_id)["status"] == "COMPLETED"
        assert len(list(storage.iter_requests(run_id))) == 1


@pytest.mark.parametrize(
    "run_id",
    [
        "../outside",
        "..\\outside",
        "20260725T120000.000Z/child",
        "20260725T120000.000Z\\child",
        "20260725T120000.000Z..",
        "not-a-run-id",
        "",
    ],
)
def test_invalid_and_path_traversal_run_ids_are_rejected(
    tmp_path: Path, run_id: str
) -> None:
    with pytest.raises(ExtractionError, match="Invalid run ID"):
        validate_run_id(run_id)
    with pytest.raises(ExtractionError, match="Invalid run ID"):
        MatrixportExtractor(
            tmp_path,
            mode="full",
            run_id=run_id,
            transport=CountingTransport(),
        )
    assert not (tmp_path / "output").exists()


def test_incremental_run_copies_forward_prior_canonical_history(tmp_path: Path) -> None:
    prior_run_id = "20260725T120100.000Z"
    prior = MatrixportExtractor(
        tmp_path,
        mode="full",
        run_id=prior_run_id,
        transport=CountingTransport(),
    )
    prior_row = {
        "id": "bill-before-incremental-window",
        "currency": "USD",
        "amount": "12.3400",
        "timestamp_ms": "1609459200000",
    }
    try:
        prior.storage.upsert_canonical_rows(
            prior_run_id,
            "wallet_bills",
            [(stable_key("wallet_bills", prior_row), prior_row, None)],
        )
        prior.storage.finish_run(prior_run_id, status="COMPLETED")
    finally:
        prior.close()
    reports = tmp_path / "output" / prior_run_id / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "verification.json").write_text(
        '{"ok":true,"status":"PASS"}\n', encoding="utf-8"
    )

    incremental_run_id = "20260725T120200.000Z"
    incremental_transport = CountingTransport()
    incremental = MatrixportExtractor(
        tmp_path,
        mode="incremental",
        run_id=incremental_run_id,
        transport=incremental_transport,
    )
    _disable_network_extractors(incremental)
    try:
        result = incremental.extract()
    finally:
        incremental.close()

    assert result.status == "COMPLETED"
    assert incremental_transport.calls == []
    current_db = tmp_path / "output" / incremental_run_id / "state" / DATABASE_NAME
    with RunStorage(current_db) as storage:
        copied = [
            item["row"]
            for item in storage.iter_canonical_rows(incremental_run_id, "wallet_bills")
        ]
        assert copied == [{**prior_row, "source_prior_run_id": prior_run_id}]
        lineage = [
            item["row"]
            for item in storage.iter_canonical_rows(
                incremental_run_id, "incremental_lineage"
            )
        ]
        assert len(lineage) == 1
        assert lineage[0]["base_run_id"] == prior_run_id
        assert lineage[0]["policy"] == "copy_forward_then_overlay_30d_history_7d_nav"
