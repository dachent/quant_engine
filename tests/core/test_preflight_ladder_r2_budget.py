"""Round 2 of the preflight ladder: false refusals and gaps found in review.

Every case is built from the reviewer's shape. Path lengths below are measured from the
real attended runs under C:\\Dev\\q (2026-10-04), copied here as literals so the gate is
checked against what the producers wrote, not against its own tables.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

import pytest

import portfolio_pipeline_materialize as materializer

pytestmark = pytest.mark.real_attended_path_budget

LIMIT = 260
GEN = "0" * 64

# ---- measured from the real runs: relative to the output / cache root ----
REAL_OUTPUT_PATHS = (
    # stages\epoch static-snapshot receipt (longest of the r2 run, 183 characters)
    "stages\\epoch\\generations\\" + GEN + "\\receipt\\static-snapshot\\"
    "account-61092059d066f056\\grizzlybears-vtma\\2026-01-02-2026-09-17.json",
    # deepest decomposition lens (182)
    "stages\\analytics\\decomposition-lenses\\LIVE_PLUS_BACKTEST\\DYNAMIC_FLS\\"
    "multi-asset-style\\generations\\" + GEN + "\\decomposition.json",
    # preparation plan provenance (168)
    "preparation\\plans\\live_plus_backtest\\" + GEN + "\\inputs\\base\\provenance\\"
    "05-base-proxies-adjusted.json.manifest.json",
)
REAL_CACHE_PATHS = (
    ".decomposition-training-evidence\\" + GEN + ".json",  # 102
    ".decomposition-training\\" + GEN + ".json",
    ".decomposition-index\\" + GEN + ".json",
    ".correlation-index\\" + GEN + ".json",
    "candidates\\" + GEN + "\\decomposition.json",
    # Z2: the production run receipt, keyed by pipeline id and a 16-hex run key
    # (134, now the longest cache tail; see `_production_receipt_tail`).
    "pipelines\\" + "p" * 48 + "-" + "0" * 16 + "\\receipts\\"
    "publish-hybrid-multi-asset-style-dynamic-fls.json",
)


def _root(length: int, prefix: str = "C:\\Dev\\scratch\\") -> Path:
    return Path(prefix + "x" * (length - len(prefix)))


# ------------------------------------------------------------ 2: path budget


def test_real_layout_literals_have_the_measured_lengths():
    assert [len(p) for p in REAL_OUTPUT_PATHS] == [183, 182, 168]
    assert max(len(p) for p in REAL_CACHE_PATHS) == 134


def test_gate_never_underestimates_a_real_output_path():
    for sample in REAL_OUTPUT_PATHS:
        root = _root(LIMIT - 1 - len(sample))  # the path would be exactly 260
        with pytest.raises(materializer.MaterializationError):
            materializer._attended_path_budget(root, Path("C:\\Dev\\q\\cache"))


def test_real_output_root_up_to_the_true_bound_passes():
    # The registry's longest strategy id (echotrade-growth-pivots) is six characters
    # longer than the longest id of the measured run, so the true bound is 189.
    longest = len(
        REAL_OUTPUT_PATHS[0].replace("grizzlybears-vtma", "echotrade-growth-pivots")
    )
    assert longest == 189
    materializer._attended_path_budget(
        _root(LIMIT - 2 - longest), Path("C:/Dev/q/cache")
    )
    with pytest.raises(materializer.MaterializationError, match="Windows limit"):
        materializer._attended_path_budget(
            _root(LIMIT - 1 - longest), Path("C:/Dev/q/cache")
        )


def test_a_valid_run_with_a_110_character_cache_root_is_admitted():
    # Astra: refused through a fictitious 269-character path; the real longest cache
    # file is 102 characters, so 110 + 1 + 102 = 213.
    materializer._attended_path_budget(
        Path("C:\\Dev\\q\\out"), _root(110, "C:\\Dev\\scratch\\cache")
    )


def test_cache_root_bound_follows_the_real_cache_layout():
    longest = max(len(p) for p in REAL_CACHE_PATHS)
    materializer._attended_path_budget(
        Path("C:\\Dev\\q\\out"), _root(LIMIT - 2 - longest)
    )
    with pytest.raises(materializer.MaterializationError, match="retained cache"):
        materializer._attended_path_budget(
            Path("C:\\Dev\\q\\out"), _root(LIMIT - 1 - longest)
        )


def test_no_tail_invents_provider_capture_paths():
    for _label, tail in materializer.generated_tails() + materializer._cache_tails():
        assert "har_output" not in tail and "source\\csv" not in tail


def test_qa_evidence_names_are_measured_not_sampled():
    scratch = Path("C:/Dev/scratch/quant_engine")
    if not scratch.is_dir():
        pytest.skip("no short scratch root on this machine")
    qa = Path(tempfile.mkdtemp(prefix="qa", dir=scratch))
    try:
        (qa / "desktop").mkdir()
        (qa / "desktop" / "short.json").write_bytes(b"{}")
        out, cache = Path("C:/Dev/q/out"), Path("C:/Dev/q/cache")
        materializer._attended_path_budget(out, cache, qa_dir=qa)
        long_name = "n" * 150 + ".json"
        (qa / "desktop" / long_name).write_bytes(b"{}")
        with pytest.raises(materializer.MaterializationError, match=long_name):
            materializer._attended_path_budget(out, cache, qa_dir=qa)
    finally:
        shutil.rmtree(qa, ignore_errors=True)
