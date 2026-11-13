"""IBKR Flex equity-history stage (#913 source ``ibkr_flex_history``): DERIVE_FROM_RETAINED.

The Flex history for every account is already held in hashed retained source runs (monthly XMLs plus daily
statements); a weekly refresh first extracts from them and only then asks the Flex driver for the missing days.
Streaming parse (the files are tens of MB); per account and day: the ``EquitySummaryByReportDateInBase`` row
(cash, stock, total ...) and the ``ChangeInNAV`` ending value. No value is logged.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET


def extract(
    xml_files: Iterable[Path], account_ids: Iterable[str]
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{account_id: {report_date: {"row", "ending", "source", "conflict"}}}`` for the EXACT account ids given.

    The first file that carries a day wins (files are passed oldest first). A later file that carries the same day with
    a different row sets ``conflict`` (a restatement is reported, never silently dropped or silently preferred).
    """
    ids = tuple(dict.fromkeys(account_ids))
    if not ids or any(not i for i in ids):
        raise ValueError("ACCOUNT_IDS")
    out: dict[str, dict[str, dict[str, Any]]] = {i: {} for i in ids}
    ending: dict[str, dict[str, str]] = {i: {} for i in ids}
    for f in xml_files:
        for _, el in ET.iterparse(str(f), events=("end",)):
            tag, acct = el.tag, el.get("accountId", "")
            if acct in out:
                if tag == "EquitySummaryByReportDateInBase":
                    day = el.get("reportDate", "")
                    seen = out[acct].get(day)
                    if seen is None:
                        out[acct][day] = {
                            "row": dict(el.attrib),
                            "source": Path(f).name,
                            "conflict": False,
                        }
                    elif seen["row"] != dict(el.attrib):
                        seen["conflict"] = True
                elif tag == "ChangeInNAV":
                    ending[acct].setdefault(
                        el.get("toDate", ""), el.get("endingValue", "")
                    )
            if tag in (
                "EquitySummaryByReportDateInBase",
                "ChangeInNAV",
                "FlexStatement",
            ):
                el.clear()
    for i in ids:
        for d, rec in out[i].items():
            rec["ending"] = ending[i].get(d)
    return out


def nav_vs_change_in_nav(
    series: dict[str, dict[str, Any]], *, tol: float = 0.01
) -> dict[str, list[str]]:
    """Compare the equity-summary total with the ChangeInNAV ending value where one exists.

    ``mismatch``: both present and different, or the total missing/non-numeric; ``unchecked``: no ChangeInNAV ending for
    that date (in a monthly statement only the period end has one). Both lists are reported, never merged into a pass.
    """
    mismatch: list[str] = []
    unchecked: list[str] = []
    for d, rec in sorted(series.items()):
        try:
            total = float(rec["row"]["total"])
        except (KeyError, ValueError):
            mismatch.append(d)
            continue
        end = rec.get("ending")
        if end in (None, ""):
            unchecked.append(d)
            continue
        try:
            if not abs(total - float(end)) <= tol:
                mismatch.append(d)
        except ValueError:
            mismatch.append(d)
    return {"mismatch": mismatch, "unchecked": unchecked}
