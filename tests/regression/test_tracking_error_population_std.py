"""#904 regression case tracking_error_population_std: Tracking error computed with the population standard deviation and unlabelled (#904 item 13)."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))


def test_tracking_error_population_std() -> None:
    source = (ROOT / "tools" / "portfolio_target_series.py").read_text(encoding="utf-8")
    assert "statistics.stdev(differences) * math.sqrt(252)" in source
    assert "pstdev(differences)" not in source
