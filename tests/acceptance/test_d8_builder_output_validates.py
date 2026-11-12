"""D8 reachability: the real builders' verification record satisfies the real check.

The validator's own fixtures are hand-written, so a check whose passing value no builder
emits looks exactly like a check that works (Ruling C, M8).  This test takes the record
the panel builder (``_verified_bundle_account_manifest``) writes for a genuine
verified-bundle epoch manifest, places it, with the producer's build-time readiness
constants and ``state STAGED``, in a generation under a committed release, and requires
``identity.publication_state`` and ``identity.release_pinned`` to pass.  The old check,
which read those constants as release state, fails here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

from test_validate_generation import Spec, _set, build, run

from acceptance.validate import checks

ROOT = Path(__file__).resolve().parents[2]
_E2E = ROOT / "tests" / "core" / "test_task6_e2e_bundle_path.py"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _real_identity(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    e2e = _load("_d8_e2e_fixture", _E2E)
    root, generation_id, payload, _report = e2e._stage_tracked_schema8_bundle(
        tmp_path / "generation"
    )
    panel = _load("_d8_control_panel", ROOT / "tools" / "portfolio_control_panel.py")
    financial = _load(
        "_d8_financial", ROOT / "tools" / "portfolio_financial_mvp_contract.py"
    )
    release = _load("_d8_release", ROOT / "tools" / "portfolio_release_builder.py")
    staged, _source, _identity = release._stage_verified_epoch_manifest_bundle(
        root, generation_id, tmp_path / "release"
    )
    value, identity = panel._verified_bundle_account_manifest(
        staged, root, generation_id
    )
    assert financial.read_generation_bundle(root, generation_id) is not None
    return value, identity


def test_real_panel_builder_verification_record_passes_publication_state(
    tmp_path: Path,
) -> None:
    value, identity = _real_identity(tmp_path / "real")
    record = dict(identity["verification"])
    record["epoch_manifest_source"] = identity["epoch_manifest_source"]
    profile = identity["authority_profile"]
    assert record["publication_readiness"] == "NOT_READY"
    assert value["authority_readiness"]["independent_authority_verification"] == (
        "NOT_PERFORMED"
    )

    def panel(doc: dict[str, Any]) -> None:
        _set(("meta", "account_manifest_verification"), record)(doc)
        _set(("meta", "account_manifest_authority_profile"), profile)(doc)
        _set(("meta", "account_manifest_sha256"), identity["sha256"])(doc)
        _set(("account_manifest", "authority_readiness"), value["authority_readiness"])(
            doc
        )
        _set(("account_manifest", "rows"), value["rows"])(doc)

    spec = Spec(
        panel=[panel],
        candidate=[
            _set(("account_manifest_verification",), record),
            _set(("account_manifest_authority_profile",), profile),
            _set(("state",), "STAGED"),
        ],
    )
    findings = run(build(tmp_path / "gen", spec))
    assert [
        f
        for f in findings
        if f.check_id in ("identity.publication_state", "identity.release_pinned")
    ] == []
    # kill: the same real record with one recorded discrepancy must block.
    broken = {
        **record,
        "independent_report": {
            **record["independent_report"],
            "numeric_verification": {
                **record["independent_report"]["numeric_verification"],
                "discrepancy_count": 1,
            },
        },
    }
    spec_bad = Spec(
        panel=[
            panel,
            _set(("meta", "account_manifest_verification"), broken),
        ],
        candidate=[
            _set(("account_manifest_verification",), broken),
            _set(("account_manifest_authority_profile",), profile),
        ],
    )
    bad = run(build(tmp_path / "bad", spec_bad))
    assert [f.klass for f in bad if f.check_id == "identity.publication_state"] == [
        checks.BLOCKER_AUTHORITY
    ]
