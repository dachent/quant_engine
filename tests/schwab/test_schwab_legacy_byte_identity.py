"""Legacy byte identity of the Schwab generation after the optional valuation cutoff.

A snapshot without ``valuation_cutoff`` must rebuild to the same generation, byte for
byte, as the code before the change produced. Two proofs:

* the retained Schwab generation f81b1f73 (as-of 2026-09-18, the R2.1 candidate's
  provider) is rebuilt from its spec and retained inputs and compared with the retained
  catalog bytes (skipped where the retained evidence is not mounted);
* a synthetic WEB_CONTRACT generation whose digests were recorded from the
  PRE-change ``generation.py`` (blob b8ceb743, parent of this change) with the same
  build, and are compared here with the post-change output.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

from quant_engine.schwab import SchwabGenerationError, build_generation


HERE = Path(__file__).parent
RETAINED = Path(
    "C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine/dataSchwab"
)
RETAINED_SPEC = (
    RETAINED
    / "source-runs/r21-schwab-0917-official/schwab-generation-spec-official.json"
)
# Recorded from the pre-change generation.py; prefixes keep fixture digests short.
F81B_ID_PREFIX = "f81b1f733a5b53c9e6b303c3"
F81B_CATALOG_PREFIX = "36ae762f16c69b54b4e6946917432ddebb37bbb0bd8775f5"
FIXTURE_ID_PREFIX = "742dc96c9f8e9036ec38759e"
FIXTURE_CATALOG_PREFIX = "d6edfc9b2e4cc7bc1b1e69484533e72c63fb9a2f73a16514"


def _fixture_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "legacy_identity_fixture", HERE / "test_schwab_generation.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_synthetic_legacy_generation_equals_the_pre_change_digests(
    tmp_path: Path,
) -> None:
    module = _fixture_module()
    spec = module._web_snapshot_spec(tmp_path)  # type: ignore[attr-defined]
    catalog, manifest, catalog_bytes = build_generation(spec)
    assert set(catalog["position_snapshot_as_of"]) == {"observed_at"}
    assert manifest.generation_id.startswith(FIXTURE_ID_PREFIX)
    assert hashlib.sha256(catalog_bytes).hexdigest().startswith(FIXTURE_CATALOG_PREFIX)


@pytest.mark.skipif(
    not RETAINED_SPEC.is_file(), reason="retained Schwab f81b1f73 inputs not mounted"
)
def test_retained_f81b1f73_rebuilds_byte_identically() -> None:
    try:
        catalog, manifest, catalog_bytes = build_generation(RETAINED_SPEC)
    except SchwabGenerationError as exc:
        if "configured source" in str(exc) or "root" in str(exc):
            pytest.skip("retained Schwab source roots are not mounted")
        raise
    assert manifest.generation_id.startswith(F81B_ID_PREFIX)
    retained = (
        RETAINED / "generations" / manifest.generation_id / "schwab-generation.json"
    )
    assert retained.is_file()
    assert catalog_bytes == retained.read_bytes()
    assert hashlib.sha256(catalog_bytes).hexdigest().startswith(F81B_CATALOG_PREFIX)
    assert set(catalog["position_snapshot_as_of"]) == {"observed_at"}
