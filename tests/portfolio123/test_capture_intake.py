from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from p123_capture_fixtures import (
    EXPIRES,
    INVOICE_CSV,
    NOW,
    ORIGIN,
    PREDICTOR,
    STANDARD_SCOPE,
    TRANSACTIONS_CSV,
    entry,
    predictor_entry,
    standard_entries,
    transactions_page,
    write_har,
)
from quant_engine.portfolio123.capture_shapes import (
    canonical_bytes as canonical_json_bytes,
)
from quant_engine.portfolio123 import capture_catalog, capture_cli, capture_intake
from quant_engine.portfolio123.capture_catalog import CaptureRefusal
from quant_engine.portfolio123.capture_intake import StageRequest, stage_run, verify_run


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "staging"
    path.mkdir()
    return path


def _har_request(
    root: Path,
    tmp_path: Path,
    entries: list[dict[str, Any]],
    scope: tuple[str, ...],
    *,
    run_id: str = "run-one",
    allow_partial: bool = False,
) -> StageRequest:
    source = write_har(tmp_path / "export.har", entries)
    return StageRequest(
        staging_root=root,
        run_id=run_id,
        form="har",
        source=source,
        capabilities=scope,
        captured_at=NOW,
        session_expires_at=EXPIRES,
        allow_partial=allow_partial,
    )


def _refusal(request: StageRequest) -> str:
    with pytest.raises(CaptureRefusal) as caught:
        stage_run(request)
    return caught.value.code


def _stage_standard(root: Path, tmp_path: Path) -> Path:
    summary = stage_run(
        _har_request(root, tmp_path, standard_entries(), STANDARD_SCOPE)
    )
    assert summary.complete and summary.records == len(STANDARD_SCOPE)
    return root / "p123-capture" / "run-one"


def test_har_stage_verify_round_trip_and_inventory(root: Path, tmp_path: Path) -> None:
    run = _stage_standard(root, tmp_path)
    summary = verify_run(root, "run-one")
    assert set(summary.capabilities) == set(STANDARD_SCOPE)
    manifest = json.loads((run / "manifest.json").read_bytes())
    assert manifest["evidence_status"] == "UNVERIFIED_SYNTHETIC_SHAPES"
    assert manifest["raw_contains_session_material"] is True
    totals = manifest["inventory_totals"]
    assert totals["entries"] == 8 and totals["matched"] == 6
    assert totals["unreviewed"] == 1 and totals["foreign"] == 1
    inventory = (run / "inventory.json").read_text()
    # Route templates only: no id, no body, no foreign host.
    assert "9001" not in inventory and "example.invalid" not in inventory
    assert "/app/ai-factor/predictor/{n}" in inventory
    assert (run / "raw" / "source.har").read_bytes() == (
        tmp_path / "export.har"
    ).read_bytes()


def test_staging_is_create_only(root: Path, tmp_path: Path) -> None:
    _stage_standard(root, tmp_path)
    before = (root / "p123-capture" / "run-one" / "manifest.json").read_bytes()
    request = _har_request(root, tmp_path, standard_entries(), STANDARD_SCOPE)
    assert _refusal(request) == "RUN_EXISTS"
    assert (root / "p123-capture" / "run-one" / "manifest.json").read_bytes() == before
    # A different run id does not touch the first run either.
    stage_run(
        _har_request(
            root, tmp_path, standard_entries(), STANDARD_SCOPE, run_id="run-two"
        )
    )
    assert (root / "p123-capture" / "run-one" / "manifest.json").read_bytes() == before


def test_failed_stage_leaves_no_partial_run(root: Path, tmp_path: Path) -> None:
    bad = predictor_entry({**PREDICTOR, "unreviewed": 1})
    assert (
        _refusal(_har_request(root, tmp_path, [bad], ("aifactor.predictor",)))
        == "SHAPE_REFUSED"
    )
    assert not (root / "p123-capture" / "run-one").exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: {**b, "extra": 1},
        lambda b: {k: v for k, v in b.items() if k != "universe"},
        lambda b: {**b, "model_id": "9002"},
        lambda b: {**b, "predictions": []},
        lambda b: {**b, "predictions": [{"ticker": "AAAA", "score": 1, "rank": True}]},
        lambda b: {**b, "as_of": "2030-13-45"},
        lambda b: {
            **b,
            "predictions": [{"ticker": "AAAA", "score": 1, "rank": 1, "x": 1}],
        },
    ],
)
def test_unknown_or_malformed_shapes_are_refused(
    root: Path, tmp_path: Path, mutate: Any
) -> None:
    body = mutate(copy.deepcopy(PREDICTOR))
    request = _har_request(
        root, tmp_path, [predictor_entry(body)], ("aifactor.predictor",)
    )
    assert _refusal(request) == "SHAPE_REFUSED"


def test_unreviewed_routes_are_inventoried_never_parsed(
    root: Path, tmp_path: Path
) -> None:
    only_noise = [entry(f"{ORIGIN}/app/other/12", {"a": 1})]
    request = _har_request(root, tmp_path, only_noise, ("aifactor.predictor",))
    assert _refusal(request) == "NO_REVIEWED_ROUTES"


def test_query_shape_drift_is_unreviewed(root: Path, tmp_path: Path) -> None:
    drifted = entry(f"{ORIGIN}/app/ai-factor/predictor/9001?cachebust=1", PREDICTOR)
    request = _har_request(root, tmp_path, [drifted], ("aifactor.predictor",))
    assert _refusal(request) == "NO_REVIEWED_ROUTES"


@pytest.mark.parametrize(
    "bad",
    [
        entry(f"{ORIGIN}/app/ai-factor/predictor/9001", PREDICTOR, status=404),
        entry(f"{ORIGIN}/app/ai-factor/predictor/9001", PREDICTOR, headers=False),
        entry(f"{ORIGIN}/app/ai-factor/predictor/9001", None, raw_text="not json"),
        entry(f"{ORIGIN}/app/ai-factor/predictor/9001", PREDICTOR, method="POST"),
    ],
)
def test_matched_entry_failures_refuse_the_run(
    root: Path, tmp_path: Path, bad: dict[str, Any]
) -> None:
    request = _har_request(root, tmp_path, [bad], ("aifactor.predictor",))
    assert _refusal(request) in {"HAR_ENTRY_REJECTED", "NO_REVIEWED_ROUTES"}


def test_scope_must_match_exactly(root: Path, tmp_path: Path) -> None:
    entries = standard_entries()
    assert (
        _refusal(_har_request(root, tmp_path, entries, STANDARD_SCOPE[:-1]))
        == "SCOPE_MISMATCH"
    )
    assert (
        _refusal(
            _har_request(
                root, tmp_path, entries[:1], ("aifactor.predictor", "aifactor.model")
            )
        )
        == "SCOPE_MISMATCH"
    )
    assert (
        _refusal(_har_request(root, tmp_path, entries, ("not.a.capability",)))
        == "CAPABILITY_UNKNOWN"
    )
    assert _refusal(_har_request(root, tmp_path, entries, ())) == "SCOPE_REQUIRED"


def test_duplicates_dedupe_only_when_identical(root: Path, tmp_path: Path) -> None:
    same = [predictor_entry(), predictor_entry()]
    summary = stage_run(_har_request(root, tmp_path, same, ("aifactor.predictor",)))
    assert summary.records == 1
    changed = {**PREDICTOR, "name": "Synthetic predictor changed"}
    conflict = [predictor_entry(), predictor_entry(changed)]
    request = _har_request(
        root, tmp_path, conflict, ("aifactor.predictor",), run_id="run-two"
    )
    assert _refusal(request) == "CONFLICTING_DUPLICATE"


def _pages(*numbers: int, page_count: int = 3, total: int = 5) -> list[dict[str, Any]]:
    sizes = {1: 2, 2: 2, 3: 1}
    return [
        entry(
            f"{ORIGIN}/app/account/transactions/9006?page={n}",
            transactions_page(n, page_count, total, sizes[n]),
        )
        for n in numbers
    ]


def test_complete_pagination_is_accepted_and_partial_is_refused(
    root: Path, tmp_path: Path
) -> None:
    scope = ("transactions.account",)
    complete = stage_run(_har_request(root, tmp_path, _pages(1, 2, 3), scope))
    assert complete.capabilities["transactions.account"]["coverage"] == "COMPLETE"
    request = _har_request(root, tmp_path, _pages(1, 3), scope, run_id="run-two")
    assert _refusal(request) == "COVERAGE_PARTIAL"
    partial = stage_run(
        _har_request(
            root, tmp_path, _pages(1, 3), scope, run_id="run-three", allow_partial=True
        )
    )
    assert not partial.complete
    with pytest.raises(CaptureRefusal) as caught:
        verify_run(root, "run-three")
    assert caught.value.code == "COVERAGE_PARTIAL"
    assert not verify_run(root, "run-three", require_complete=False).complete


def test_inconsistent_pagination_totals_are_refused(root: Path, tmp_path: Path) -> None:
    mixed = _pages(1, 2) + [
        entry(
            f"{ORIGIN}/app/account/transactions/9006?page=3",
            transactions_page(3, 4, 5, 1),
        )
    ]
    request = _har_request(root, tmp_path, mixed, ("transactions.account",))
    assert _refusal(request) == "PAGINATION_INCONSISTENT"


def test_session_envelope_and_paths_are_enforced(root: Path, tmp_path: Path) -> None:
    request = _har_request(root, tmp_path, standard_entries(), STANDARD_SCOPE)
    expired = StageRequest(**{**request.__dict__, "session_expires_at": NOW})
    assert _refusal(expired) == "HAR_ENTRY_REJECTED"
    naive = StageRequest(
        **{**request.__dict__, "captured_at": NOW.replace(tzinfo=None)}
    )
    assert _refusal(naive) == "TIME_REQUIRED"
    relative = StageRequest(**{**request.__dict__, "source": Path("export.har")})
    assert _refusal(relative) == "PATH_NOT_ABSOLUTE"
    missing = StageRequest(**{**request.__dict__, "source": tmp_path / "absent.har"})
    assert _refusal(missing) == "SOURCE_NOT_A_REGULAR_FILE"
    bad_id = StageRequest(**{**request.__dict__, "run_id": "../escape"})
    assert _refusal(bad_id) == "RUN_ID_INVALID"


def test_staging_root_inside_a_repository_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    inside = repo / "staging"
    inside.mkdir()
    source = write_har(tmp_path / "export.har", standard_entries())
    request = StageRequest(
        inside, "run-one", "har", source, STANDARD_SCOPE, NOW, EXPIRES
    )
    assert _refusal(request) == "STAGING_ROOT_IN_REPOSITORY"


def test_non_strict_json_sources_are_refused(root: Path, tmp_path: Path) -> None:
    source = tmp_path / "export.har"
    source.write_bytes(b'{"lo' + b'g": {"entr' + b'ies": [1]}, "lo' + b'g": 2}')
    request = StageRequest(
        root, "run-one", "har", source, ("aifactor.predictor",), NOW, EXPIRES
    )
    assert _refusal(request) == "SOURCE_NOT_STRICT_JSON"
    source.write_bytes(b'{"x": 1}')
    assert _refusal(request) == "HAR_MALFORMED"


def _file_request(root: Path, path: Path, capability: str) -> StageRequest:
    return StageRequest(root, "file-run", "file", path, (capability,), NOW)


def test_file_intake_csv_pdf_json_round_trip(root: Path, tmp_path: Path) -> None:
    cases = [
        ("transactions.download", "tx.csv", TRANSACTIONS_CSV.encode()),
        ("billing.invoice", "bill.csv", INVOICE_CSV.encode()),
        ("billing.invoice", "bill.pdf", b"%PDF-1.7\nsynthetic\n"),
        (
            "designer-model.json",
            "model.json",
            json.dumps(capture_fixture_designer()).encode(),
        ),
    ]
    for index, (capability, name, data) in enumerate(cases):
        path = tmp_path / name
        path.write_bytes(data)
        request = StageRequest(
            root, f"file-run-{index}", "file", path, (capability,), NOW
        )
        stage_run(request)
        assert verify_run(root, f"file-run-{index}").records == 1


def capture_fixture_designer() -> dict[str, Any]:
    from p123_capture_fixtures import DESIGNER

    return DESIGNER


@pytest.mark.parametrize(
    "capability,name,data,code",
    [
        ("transactions.download", "tx.csv", b"A,B\n1,2\n", "CSV_COLUMNS_UNREVIEWED"),
        (
            "transactions.download",
            "tx.csv",
            TRANSACTIONS_CSV.encode() + b"1,2\n",
            "CSV_SHAPE_REFUSED",
        ),
        (
            "transactions.download",
            "tx.csv",
            TRANSACTIONS_CSV.splitlines(keepends=True)[0].encode(),
            "EXPORT_HAS_NO_ROWS",
        ),
        ("transactions.download", "tx.pdf", b"%PDF-1.7", "MEDIA_TYPE_REFUSED"),
        ("billing.invoice", "bill.pdf", b"not a pdf", "MEDIA_SIGNATURE_MISMATCH"),
        ("designer-model.json", "m.json", b'{"model_id": 1}', "SHAPE_REFUSED"),
        ("aifactor.predictor", "p.json", b"{}", "FORM_NOT_ACCEPTED"),
    ],
)
def test_file_intake_refusals(
    root: Path, tmp_path: Path, capability: str, name: str, data: bytes, code: str
) -> None:
    path = tmp_path / name
    path.write_bytes(data)
    assert _refusal(_file_request(root, path, capability)) == code


def test_verifier_detects_tampering(root: Path, tmp_path: Path) -> None:
    run = _stage_standard(root, tmp_path)
    normalized = run / "normalized" / "records.json"
    original = normalized.read_bytes()
    normalized.write_bytes(original.replace(b"Synthetic", b"Altered"))
    with pytest.raises(CaptureRefusal) as caught:
        verify_run(root, "run-one")
    assert caught.value.code == "FILE_HASH_MISMATCH"
    normalized.write_bytes(original)
    (run / "extra.txt").write_bytes(b"x")
    with pytest.raises(CaptureRefusal) as caught:
        verify_run(root, "run-one")
    assert caught.value.code == "UNDECLARED_FILES"
    (run / "extra.txt").unlink()
    verify_run(root, "run-one")
    with pytest.raises(CaptureRefusal) as caught:
        verify_run(root, "run-one", expect_manifest_sha256="0" * 64)
    assert caught.value.code == "MANIFEST_HASH_MISMATCH"


def test_verifier_catches_a_rehashed_forgery(root: Path, tmp_path: Path) -> None:
    """Altering normalized bytes AND the manifest hashes still fails re-derivation."""

    import hashlib

    run = _stage_standard(root, tmp_path)
    normalized = run / "normalized" / "records.json"
    forged = normalized.read_bytes().replace(
        b"Synthetic predictor", b"Forged predictor"
    )
    normalized.write_bytes(forged)
    manifest = json.loads((run / "manifest.json").read_bytes())
    for item in manifest["files"]:
        if item["path"] == "normalized/records.json":
            item["sha256"] = hashlib.sha256(forged).hexdigest()
            item["bytes"] = len(forged)
    (run / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(CaptureRefusal) as caught:
        verify_run(root, "run-one")
    assert caught.value.code == "REDERIVATION_MISMATCH"


def test_verifier_detects_catalog_drift(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_standard(root, tmp_path)
    monkeypatch.setattr(capture_intake, "catalog_sha256", lambda: "1" * 64)
    with pytest.raises(CaptureRefusal) as caught:
        verify_run(root, "run-one")
    assert caught.value.code == "CATALOG_DRIFT"


def test_verifier_missing_run(root: Path) -> None:
    with pytest.raises(CaptureRefusal) as caught:
        verify_run(root, "absent")
    assert caught.value.code == "RUN_MISSING"


def test_cli_prints_only_aggregates_and_reason_codes(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_har(tmp_path / "export.har", standard_entries())
    arguments = [
        "stage-har",
        "--staging-root",
        str(root),
        "--run-id",
        "cli-run",
        "--input",
        str(source),
        "--captured-at",
        NOW.isoformat(),
        "--session-expires-at",
        EXPIRES.isoformat(),
    ]
    for capability in STANDARD_SCOPE:
        arguments += ["--capability", capability]
    assert capture_cli.main(arguments) == 0
    staged = capsys.readouterr().out
    assert "Synthetic" not in staged and str(tmp_path) not in staged
    assert json.loads(staged)["complete"] is True
    assert capture_cli.main(arguments) == 2
    assert capsys.readouterr().err.strip() == "REFUSED RUN_EXISTS"
    assert (
        capture_cli.main(["verify", "--staging-root", str(root), "--run-id", "cli-run"])
        == 0
    )
    assert capture_cli.main(["catalog"]) == 0
    assert capture_catalog.catalog_sha256() in capsys.readouterr().out


def test_inventory_is_create_only_id_free_and_parses_no_body(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    drifted = entry(f"{ORIGIN}/app/other/12345?x=1", None, raw_text="not json")
    source = write_har(tmp_path / "export.har", [drifted, *standard_entries()])
    summary = capture_intake.inventory_har(root, "inv-one", source)
    assert summary["entries"] == 9 and summary["matched"] == 6
    written = (root / "p123-capture-inventory" / "inv-one.json").read_text()
    assert (
        "/app/{x}/{n}" in written and "12345" not in written and "9001" not in written
    )
    assert "Synthetic" not in written
    with pytest.raises(CaptureRefusal) as caught:
        capture_intake.inventory_har(root, "inv-one", source)
    assert caught.value.code == "DESTINATION_EXISTS"
    arguments = ["inventory-har", "--staging-root", str(root), "--run-id", "inv-two"]
    assert capture_cli.main([*arguments, "--input", str(source)]) == 0
    assert "inventory_sha256" in capsys.readouterr().out


def test_runbook_lists_every_capability_and_command() -> None:
    runbook = Path(__file__).parents[2] / "docs" / "providers" / "p123-capture.md"
    text = runbook.read_text(encoding="utf-8")
    for route in capture_catalog.CATALOG:
        assert f"`{route.capability}`" in text
    for command in ("inventory-har", "stage-har", "stage-file", "verify", "catalog"):
        assert f"capture_cli {command}" in text
    assert "UNVERIFIED" in text and "[credentials.p123]" in text


def test_inventory_redacts_unknown_words_unless_revealed(
    root: Path, tmp_path: Path
) -> None:
    source = write_har(
        tmp_path / "export.har",
        [
            entry(f"{ORIGIN}/app/account/alice-example/12", None, raw_text="x"),
            *standard_entries(),
        ],
    )
    capture_intake.inventory_har(root, "inv-redacted", source)
    text = (root / "p123-capture-inventory" / "inv-redacted.json").read_text()
    assert "alice" not in text and "/app/account/{x}/{n}" in text
    capture_intake.inventory_har(root, "inv-revealed", source, reveal_segments=True)
    text = (root / "p123-capture-inventory" / "inv-revealed.json").read_text()
    assert "alice-example" in text and '"segments_revealed":true' in text


def test_verifier_enforces_evidence_classification(root: Path, tmp_path: Path) -> None:
    run = _stage_standard(root, tmp_path)
    manifest = json.loads((run / "manifest.json").read_bytes())
    for key, value in (
        ("evidence_status", "VERIFIED"),
        ("raw_contains_session_material", False),
    ):
        forged = {**manifest, key: value}
        (run / "manifest.json").write_bytes(canonical_json_bytes(forged))
        with pytest.raises(CaptureRefusal) as caught:
            verify_run(root, "run-one")
        assert caught.value.code == "MANIFEST_INVALID"


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_run_files_are_owner_only(root: Path, tmp_path: Path) -> None:
    root.chmod(0o700)
    run = _stage_standard(root, tmp_path)
    assert (run / "raw" / "source.har").stat().st_mode & 0o077 == 0
    assert run.stat().st_mode & 0o077 == 0
    root.chmod(0o755)
    with pytest.raises(CaptureRefusal) as caught:
        verify_run(root, "run-one")
    assert caught.value.code == "STAGING_ROOT_NOT_PRIVATE"
