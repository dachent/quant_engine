"""Provenance admission of a staged AI Factor capture (synthetic fixtures only).

The producer is the real staging code; the consumer is
``tools/portfolio_provenance.py``.  The AI Factor routes are still unverified in the
catalog, so the seam tests confirm them for the duration of one test (a stand-in
for the reviewed change that will make them real) and prove that a run staged by
the producer is admitted by the consumer, and that nothing else is.
"""

from __future__ import annotations

from collections.abc import Callable
import copy
import dataclasses
from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from typing import Any

import pytest

from p123_capture_fixtures import (
    DESIGNER,
    MODEL,
    NOW,
    ORIGIN,
    PREDICTOR,
    entry,
    predictor_entry,
    write_har,
)
from quant_engine.portfolio123 import capture_catalog, capture_intake
from quant_engine.portfolio123.capture_intake import StageRequest, stage_run
from quant_engine.portfolio123.capture_shapes import canonical_bytes

ROOT = Path(__file__).resolve().parents[2]
AS_OF = date(2030, 1, 31)
DESIGN = ("aifactor.model", "aifactor.predictor")


def _load_provenance() -> Any:
    spec = importlib.util.spec_from_file_location(
        "portfolio_provenance_capture_design",
        ROOT / "tools" / "portfolio_provenance.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provenance = _load_provenance()


def _set_design_verdict(monkeypatch: pytest.MonkeyPatch, verdict: str) -> None:
    rows = tuple(
        dataclasses.replace(row, verdict=verdict, finding="DIFFERS")
        if row.capability in DESIGN
        else row
        for row in capture_catalog.CATALOG
    )
    monkeypatch.setattr(capture_catalog, "CATALOG", rows)
    monkeypatch.setattr(
        capture_catalog, "_BY_CAPABILITY", {row.capability: row for row in rows}
    )


@pytest.fixture
def confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand-in for the future reviewed change: the AI Factor rows become CONFIRMED."""

    _set_design_verdict(monkeypatch, "CONFIRMED")


def _entries(predictor: Any = None, model: Any = None) -> list[dict[str, Any]]:
    return [
        predictor_entry(predictor),
        entry(f"{ORIGIN}/app/ai-factor/model/9002", MODEL if model is None else model),
    ]


def _stage(
    tmp_path: Path,
    entries: list[dict[str, Any]],
    *,
    run_id: str = "run-a",
    scope: tuple[str, ...] = DESIGN,
    captured_at: Any = NOW,
    allow_partial: bool = False,
) -> Path:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    source = write_har(tmp_path / f"{run_id}.har", entries)
    stage_run(
        StageRequest(
            staging,
            run_id,
            "har",
            source,
            scope,
            captured_at,
            captured_at + timedelta(hours=2),
            allow_partial,
        )
    )
    return staging / "p123-capture" / run_id


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Admitted:
    """A private source root holding one admitted (manifest, records) pair."""

    def __init__(self, tmp_path: Path, manifest: bytes, records: bytes) -> None:
        self.root = tmp_path / "private"
        run = self.root / "captures" / "run-a"
        (run / "normalized").mkdir(parents=True, exist_ok=True)
        (run / "manifest.json").write_bytes(manifest)
        (run / "normalized" / "records.json").write_bytes(records)
        self.records_sha = _sha(records)
        self.files = [
            {
                "relative_path": "captures/run-a/manifest.json",
                "sha256": _sha(manifest),
                "size_bytes": len(manifest),
            },
            {
                "relative_path": "captures/run-a/normalized/records.json",
                "sha256": self.records_sha,
                "size_bytes": len(records),
            },
        ]
        self.sources = [
            {
                "source_id": "p123-aifactor-capture",
                "root_id": "private",
                "files": self.files,
            }
        ]
        # The owner's independent pins (expected_hashes) for both admitted files.
        self.groups = [
            {
                "source_id": "p123-aifactor-capture",
                "expected_hashes": {
                    str(item["relative_path"]): str(item["sha256"])
                    for item in self.files
                },
            }
        ]

    def version_catalog(self, **design: Any) -> dict[str, Any]:
        slot = {
            "status": "EVIDENCED",
            "artifact_sha256": self.records_sha,
            "source_ids": ["p123-aifactor-capture"],
            "version_ref": None,
            **design,
        }
        return {"strategies": [{"strategy_id": "p123-ai-factor", "design": slot}]}

    def validate(
        self, catalog: dict[str, Any], sources: Any = None, groups: Any = None
    ) -> None:
        provenance._validate_capture_design_versions(
            catalog,
            roots={"private": self.root},
            source_records=self.sources if sources is None else sources,
            source_groups=self.groups if groups is None else groups,
            generation_as_of=AS_OF,
        )


def _from_run(tmp_path: Path, run: Path) -> _Admitted:
    return _Admitted(
        tmp_path,
        (run / "manifest.json").read_bytes(),
        (run / "normalized" / "records.json").read_bytes(),
    )


def _ref(run: Path) -> str:
    return str(provenance.capture_design_reference(run)["design_version_ref"])


def _refuses(admitted: _Admitted, catalog: dict[str, Any], match: str) -> None:
    with pytest.raises(provenance.ProvenanceValidationError, match=match):
        admitted.validate(catalog)


# ----------------------------------------------------------------------------- seam


def test_producer_and_consumer_agree_on_every_constant() -> None:
    assert provenance._CAPTURE_RUN_KIND == capture_intake.KIND
    assert provenance._CAPTURE_MANIFEST_KEYS == capture_intake._MANIFEST_KEYS
    assert provenance._CAPTURE_REVIEWED_STATUS == capture_catalog.EVIDENCE_REVIEWED
    assert provenance._CAPTURE_DESIGN_CAPABILITIES == DESIGN
    assert all(item in capture_catalog._BY_CAPABILITY for item in DESIGN)
    assert provenance._CAPTURE_RECORDS_NAME == "normalized/records.json"
    assert provenance._CAPTURE_DESIGN_VERSION_PREFIX == "p123-capture-design:"


def test_a_staged_reviewed_run_is_admitted(tmp_path: Path, confirmed: None) -> None:
    run = _stage(tmp_path, _entries())
    manifest = json.loads((run / "manifest.json").read_bytes())
    assert manifest["evidence_status"] == "REVIEWED_SHAPES"
    admitted = _from_run(tmp_path, run)
    reference = _ref(run)
    assert reference.startswith("p123-capture-design:") and len(reference) == 20 + 64
    admitted.validate(admitted.version_catalog(version_ref=reference))
    summary = provenance.capture_design_reference(run)
    assert summary["records_sha256"] == admitted.records_sha
    assert summary["manifest_sha256"] == _sha((run / "manifest.json").read_bytes())


def test_unreviewed_shapes_are_never_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_design_verdict(monkeypatch, "UNVERIFIED")
    run = _stage(tmp_path, _entries())
    manifest = json.loads((run / "manifest.json").read_bytes())
    assert manifest["evidence_status"] == "UNVERIFIED_SYNTHETIC_SHAPES"
    admitted = _from_run(tmp_path, run)
    with pytest.raises(provenance.ProvenanceValidationError, match="reviewed shapes"):
        provenance.capture_design_reference(run)
    _refuses(
        admitted,
        admitted.version_catalog(version_ref="p123-capture-design:" + "0" * 64),
        "reviewed shapes",
    )


def test_design_identity_follows_content_not_time_or_other_capabilities(
    tmp_path: Path, confirmed: None
) -> None:
    first = _stage(tmp_path, _entries())
    later = _stage(
        tmp_path,
        [*_entries(), entry(f"{ORIGIN}/spr/strategy/model/9005?dm=true", DESIGNER)],
        run_id="run-b",
        scope=(*DESIGN, "designer-model.json"),
        captured_at=NOW + timedelta(days=1),
    )
    assert _ref(first) == _ref(later)
    changed = copy.deepcopy(PREDICTOR)
    changed["predictions"][0]["score"] = 0.75
    other = _stage(tmp_path, _entries(predictor=changed), run_id="run-c")
    assert _ref(other) != _ref(first)


def test_the_cli_prints_the_reference_and_refuses_unreviewed_runs(
    tmp_path: Path, confirmed: None, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _stage(tmp_path, _entries())
    assert provenance.main(["capture-design-ref", "--run-dir", str(run)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["design_version_ref"] == _ref(run)
    (run / "normalized" / "records.json").write_bytes(b"{}")
    assert provenance.main(["capture-design-ref", "--run-dir", str(run)]) == 1


# ------------------------------------------------------------- consumer refusals


def test_ordinary_design_references_are_untouched(
    tmp_path: Path, confirmed: None
) -> None:
    admitted = _from_run(tmp_path, _stage(tmp_path, _entries()))
    admitted.validate(admitted.version_catalog(version_ref="p123-designer-model:1"))
    admitted.validate(
        admitted.version_catalog(status="REFERENCE_ONLY", version_ref=None)
    )


def test_a_wrong_or_label_made_reference_is_refused(
    tmp_path: Path, confirmed: None
) -> None:
    run = _stage(tmp_path, _entries())
    admitted = _from_run(tmp_path, run)
    for label in ("p123-capture-design:" + "0" * 64, "p123-capture-design:ai-factor"):
        _refuses(
            admitted,
            admitted.version_catalog(version_ref=label),
            "design version differs",
        )


def test_slot_and_source_binding_refusals(tmp_path: Path, confirmed: None) -> None:
    run = _stage(tmp_path, _entries())
    admitted = _from_run(tmp_path, run)
    good = _ref(run)
    _refuses(
        admitted,
        admitted.version_catalog(version_ref=good, status="REFERENCE_ONLY"),
        "EVIDENCED design slot",
    )
    _refuses(
        admitted,
        admitted.version_catalog(version_ref=good, artifact_sha256=None),
        "records hash",
    )
    _refuses(
        admitted,
        admitted.version_catalog(version_ref=good, artifact_sha256="1" * 64),
        "exactly once",
    )
    # The manifest must be admitted beside the records.
    only_records = [{**admitted.sources[0], "files": admitted.files[1:]}]
    with pytest.raises(provenance.ProvenanceValidationError, match="manifest"):
        admitted.validate(admitted.version_catalog(version_ref=good), only_records)
    # The same bytes admitted twice are ambiguous.
    twice = [
        {
            **admitted.sources[0],
            "files": [
                *admitted.files,
                {**admitted.files[1], "relative_path": "x/normalized/records.json"},
            ],
        }
    ]
    with pytest.raises(provenance.ProvenanceValidationError, match="exactly once"):
        admitted.validate(admitted.version_catalog(version_ref=good), twice)
    # Bytes changed on disk after admission no longer match the admitted hash.
    (admitted.root / "captures" / "run-a" / "normalized" / "records.json").write_bytes(
        b"{}"
    )
    _refuses(
        admitted, admitted.version_catalog(version_ref=good), "differ from the admitted"
    )


def _rewritten(
    tmp_path: Path,
    run: Path,
    manifest_edit: Callable[[dict[str, Any]], None] = lambda manifest: None,
    records_edit: Callable[[dict[str, Any]], None] = lambda records: None,
    rebind: bool = True,
) -> tuple[_Admitted, str]:
    """A forger rewrites records and manifest consistently (hashes re-bound)."""

    records = json.loads((run / "normalized" / "records.json").read_bytes())
    records_edit(records)
    records_bytes = canonical_bytes(records)
    manifest = json.loads((run / "manifest.json").read_bytes())
    if rebind:
        for item in manifest["files"]:
            if item["path"] == "normalized/records.json":
                item["sha256"] = _sha(records_bytes)
                item["bytes"] = len(records_bytes)
    manifest_edit(manifest)
    admitted = _Admitted(tmp_path, canonical_bytes(manifest), records_bytes)
    return admitted, provenance._capture_design_ref(records["records"])


def _status(value: int) -> Callable[[dict[str, Any]], None]:
    def edit(records: dict[str, Any]) -> None:
        records["records"][0]["status"] = value

    return edit


def _manifest(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def edit(manifest: dict[str, Any]) -> None:
        manifest[key] = value

    return edit


def _drop_capability(records: dict[str, Any]) -> None:
    records["records"] = [
        item for item in records["records"] if item["capability"] != "aifactor.model"
    ]


def _empty_response(records: dict[str, Any]) -> None:
    for item in records["records"]:
        if item["capability"] == "aifactor.predictor":
            item["response"] = {}


def _unknown_key(records: dict[str, Any]) -> None:
    records["records"][0]["extra"] = 1


@pytest.mark.parametrize(
    "manifest_edit,records_edit,match",
    [
        (
            _manifest("evidence_status", "UNVERIFIED_SYNTHETIC_SHAPES"),
            None,
            "reviewed shapes",
        ),
        (_manifest("allow_partial", True), None, "partial"),
        (_manifest("complete", False), None, "partial"),
        (_manifest("kind", "other"), None, "staged HAR run"),
        (_manifest("form", "file"), None, "staged HAR run"),
        (_manifest("restricted", False), None, "staged HAR run"),
        (_manifest("scope", ["aifactor.predictor"]), None, "scope lacks"),
        (_manifest("captured_at", "2030-02-02T00:00:00+00:00"), None, "after as_of"),
        (_manifest("captured_at", "2030-01-15T00:00:00"), None, "capture time"),
        (_manifest("catalog_sha256", "short"), None, "catalog identity"),
        (_manifest("surprise", 1), None, "keys differ"),
        (None, _status(500), "did not succeed"),
        (None, _status(True), "status is invalid"),
        (None, _drop_capability, "predictor or model"),
        (None, _empty_response, "empty"),
        (None, _unknown_key, "keys differ"),
    ],
)
def test_a_consistent_forgery_still_fails_the_semantic_checks(
    tmp_path: Path,
    confirmed: None,
    manifest_edit: Any,
    records_edit: Any,
    match: str,
) -> None:
    run = _stage(tmp_path, _entries())
    admitted, reference = _rewritten(
        tmp_path,
        run,
        manifest_edit or (lambda manifest: None),
        records_edit or (lambda records: None),
    )
    _refuses(admitted, admitted.version_catalog(version_ref=reference), match)


def test_a_manifest_that_does_not_bind_the_records_is_refused(
    tmp_path: Path, confirmed: None
) -> None:
    run = _stage(tmp_path, _entries())

    def reorder(records: dict[str, Any]) -> None:
        records["records"].reverse()

    admitted, reference = _rewritten(tmp_path, run, records_edit=reorder, rebind=False)
    _refuses(admitted, admitted.version_catalog(version_ref=reference), "does not bind")


def test_a_non_canonical_manifest_is_refused(tmp_path: Path, confirmed: None) -> None:
    run = _stage(tmp_path, _entries())
    manifest = json.loads((run / "manifest.json").read_bytes())
    pretty = json.dumps(manifest, indent=2).encode()
    admitted = _Admitted(
        tmp_path, pretty, (run / "normalized" / "records.json").read_bytes()
    )
    _refuses(
        admitted,
        admitted.version_catalog(version_ref=_ref(run)),
        "not canonical",
    )
    shutil.rmtree(tmp_path / "private")


def _invented_responses(records: dict[str, Any]) -> None:
    for item in records["records"]:
        if item["capability"] in DESIGN:
            item["response"] = {"invented": 1}


def _wrong_type(records: dict[str, Any]) -> None:
    for item in records["records"]:
        if item["capability"] == "aifactor.model":
            item["response"]["horizon_weeks"] = "four"


def test_invented_content_is_refused_even_when_everything_is_rebound(
    tmp_path: Path, confirmed: None
) -> None:
    # The reviewer finding: manifest, records, hash binding and reference all
    # regenerated consistently around fabricated responses.
    for index, edit in enumerate((_invented_responses, _wrong_type)):
        run = _stage(tmp_path, _entries(), run_id=f"run-inv-{index}")
        admitted, reference = _rewritten(tmp_path, run, records_edit=edit)
        _refuses(
            admitted,
            admitted.version_catalog(version_ref=reference),
            "reviewed catalog shape",
        )
        shutil.rmtree(tmp_path / "private")


def test_admission_needs_the_catalog_to_review_the_shape_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_design_verdict(monkeypatch, "CONFIRMED")
    run = _stage(tmp_path, _entries())
    admitted = _from_run(tmp_path, run)
    reference = _ref(run)
    admitted.validate(admitted.version_catalog(version_ref=reference))
    # The same bytes are refused once the catalog no longer confirms the shapes.
    _set_design_verdict(monkeypatch, "UNVERIFIED")
    _refuses(
        admitted, admitted.version_catalog(version_ref=reference), "does not yet review"
    )


def test_the_owner_must_pin_both_files_in_the_source_group(
    tmp_path: Path, confirmed: None
) -> None:
    run = _stage(tmp_path, _entries())
    admitted = _from_run(tmp_path, run)
    catalog = admitted.version_catalog(version_ref=_ref(run))
    manifest_rel, records_rel = (item["relative_path"] for item in admitted.files)
    for pins in (
        {},
        {records_rel: admitted.records_sha},
        {manifest_rel: admitted.files[0]["sha256"]},
    ):
        groups = [{**admitted.groups[0], "expected_hashes": pins}]
        with pytest.raises(provenance.ProvenanceValidationError, match="owner-pinned"):
            admitted.validate(catalog, groups=groups)
