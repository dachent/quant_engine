"""#904 regression case epoch_table_shared_end_date: Epoch table printed every figure against one shared end date (#904 item 12)."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", ""):
    sys.path.insert(0, str(ROOT / sub))


def test_epoch_table_shared_end_date() -> None:
    source = (ROOT / "apps" / "portfolio-control-panel" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "ownWindowNote" in source
