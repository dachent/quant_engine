"""The underwriting waterfall names each return for the basis its contract recorded (#624).

``app.js`` is one page closure, so the label helpers sit between two markers and are
evaluated here in Node with the page's own ``humanStatus`` fallback.  A sleeve return is not
called an account return, a bridge model basis the contract recorded is never printed as
"not recorded", and the labels of the account-basis bridges are unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

PANEL = Path(__file__).resolve().parents[2] / "apps" / "portfolio-control-panel"
APP_JS = (PANEL / "app.js").read_bytes().decode("utf-8")

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is not available")

PRELUDE = (
    "const humanStatus = (status) => String(status || 'Unknown').replaceAll('_', ' ')"
    ".toLowerCase().replace(/^./, (character) => character.toUpperCase());\n"
)


def _block() -> str:
    found = re.search(
        r"// BASIS-LABELS-BEGIN\r?\n(.*?)// BASIS-LABELS-END", APP_JS, flags=re.DOTALL
    )
    assert found is not None
    return found.group(1)


def _evaluate(tmp_path: Path, expression: str) -> object:
    script = tmp_path / "labels.js"
    script.write_text(
        PRELUDE + _block() + f"\nprocess.stdout.write(JSON.stringify({expression}));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(NODE), str(script)], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def test_a_sleeve_return_is_not_named_an_account_return(tmp_path: Path) -> None:
    got = _evaluate(
        tmp_path,
        "[basisSubject('SLEEVE_EPOCH'), accountBasisText('SLEEVE_EPOCH'), "
        "basisSubject('BASELINE_ACCOUNT_LIVE'), accountBasisText('BASELINE_ACCOUNT_LIVE'), "
        "basisSubject(undefined), accountBasisText(undefined), basisSubject(null)]",
    )
    assert got == [
        "sleeve",
        "epoch series, opening NAV to end date",
        "account",
        "governed account-live series",
        "account",
        "account basis not recorded",
        "account",
    ]


def test_a_recorded_bridge_model_basis_is_never_printed_as_not_recorded(
    tmp_path: Path,
) -> None:
    got = _evaluate(
        tmp_path,
        "[bridgeBasisText('P123_BOOK_TOTAL_EQUITY_NET_OF_CASH_ADDED'), "
        "bridgeBasisText('EXACT_WORKBOOK_MODEL_RETURNS'), "
        "bridgeBasisText('A_BASIS_NOBODY_LABELLED'), bridgeBasisText(undefined), "
        "modelBasisText('P123_BOOK_TOTAL_EQUITY_NET_OF_CASH_ADDED'), "
        "modelBasisText('PROVIDER_LIVE_MODEL_RETURNS'), modelBasisText(undefined)]",
    )
    assert got == [
        "P123 model book total equity, net of cash added",
        "exact workbook model returns",
        "a basis nobody labelled",
        "basis not recorded",
        "P123 book total equity net of cash added",
        "P123 provider live model returns",
        "Model (basis not recorded)",
    ]


def test_the_waterfall_and_the_model_line_use_the_helpers_not_literals() -> None:
    assert "basisSubject(causalBridge.account_basis)" in APP_JS
    assert "bridgeBasisText(causalBridge.model_basis)" in APP_JS
    assert "basisSubject(verifiedMetrics.account_basis)" in APP_JS
    assert "modelBasisText(verifiedMetrics.model_basis)" in APP_JS
    # no template still prints the word "account" in front of a basis-bearing return
    assert "· account ${pct(causalBridge.account_return" not in APP_JS
    assert "· account ${pct(verifiedMetrics.account_cumulative_return" not in APP_JS
