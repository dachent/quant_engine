from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    REPOSITORY_ROOT
    / "skills"
    / "agent-market-data-terminal"
    / "scripts"
    / "fetch_famafrench.py"
)
SPEC = importlib.util.spec_from_file_location("fetch_famafrench", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load Fama/French helper")
famafrench = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = famafrench
SPEC.loader.exec_module(famafrench)


def test_comma_only_separator_is_not_a_header() -> None:
    assert famafrench.is_blank_line(",,")
    assert not famafrench.is_header_line(",,")
    assert famafrench.is_header_line(",Mom,")


def test_daily_momentum_feed_preserves_column_and_date_boundary() -> None:
    text = """This file describes the factor.,,
contains a daily momentum factor,
,,
Missing data are indicated by -99.99 or -999.,,
,,
,Mom,
19261103,0.35,
19261104,-0.61,
,,
Copyright provider,
"""

    preamble, sections = famafrench.parse_csv_member(
        text, "F-F_Momentum_Factor_daily_CSV"
    )

    assert preamble == [
        "This file describes the factor.,, contains a daily momentum factor,",
        "Missing data are indicated by -99.99 or -999.,,",
    ]
    assert len(sections) == 1
    assert sections[0]["title"] == "Daily"
    assert sections[0]["columns"] == ["period", "Mom"]
    assert sections[0]["rows"] == [
        {"period": "19261103", "Mom": 0.35},
        {"period": "19261104", "Mom": -0.61},
    ]
