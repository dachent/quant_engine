"""#912: the rendered published panel is compared with its payload, and stale banners fail.

Synthetic panels only: a hand-written page with the same element ids as the real shell and a
payload built from the failure shape. The kill case is a page that keeps the static
"Do not expand systematic risk" banner while the payload says otherwise.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from browser_runtime import require_browser_runtime

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import published_render_check as check  # noqa: E402

PAGE = """<!doctype html><html><body><div id="app" data-app-state="ready">
<h2 id="command-heading">{heading}</h2><p id="command-reason">{reason}</p>
<div id="gate-stack">{gates}</div>
<svg id="chart"><text class="chart-label">2026-01-02</text><text class="chart-label">{end}</text></svg>
</div></body></html>"""

CONTRACT = {
    "contract_id": "test",
    "checks": [
        {
            "id": "heading",
            "kind": "text_equals",
            "selector": "#command-heading",
            "pointer": "/advisory/readiness",
            "transform": "underscore_to_space",
        },
        {
            "id": "reason",
            "kind": "text_equals",
            "selector": "#command-reason",
            "pointer": "/advisory/reason",
        },
        {
            "id": "stale",
            "kind": "forbidden_text",
            "phrases": ["Do not expand systematic risk"],
            "unless": {
                "pointer": "/advisory/readiness",
                "equals": "DO_NOT_EXPAND_SYSTEMATIC_RISK",
            },
        },
        {
            "id": "gates",
            "kind": "count_equals",
            "selector": "#gate-stack .gate-row",
            "pointer": "/gates",
        },
        {
            "id": "figure",
            "kind": "number_in_text",
            "selector": "#command-reason",
            "pointer": "/figure",
            "scale": 100,
        },
        {
            "id": "chart_end",
            "kind": "svg_end_label",
            "selector": "#chart",
            "pointer": "/chart_end",
        },
    ],
}


def _panel(
    tmp: Path,
    *,
    heading="READY",
    reason="Reason 12.5%",
    gates=2,
    end="2026-06-30",
    readiness="READY",
    payload_gates=2,
    chart_end="2026-06-30",
) -> Path:
    (tmp / "data").mkdir(parents=True)
    rows = "".join('<div class="gate-row">g</div>' for _ in range(gates))
    (tmp / "index.html").write_text(
        PAGE.format(heading=heading, reason=reason, gates=rows, end=end),
        encoding="utf-8",
    )
    (tmp / "data" / "dashboard_data.json").write_text(
        json.dumps(
            {
                "advisory": {"readiness": readiness, "reason": "Reason 12.5%"},
                "gates": [{}] * payload_gates,
                "figure": 0.125,
                "chart_end": chart_end,
            }
        ),
        encoding="utf-8",
    )
    return tmp


def _run(tmp_path: Path, monkeypatch, **kw) -> dict:
    runtime = require_browser_runtime(ROOT)
    monkeypatch.setenv("PLAYWRIGHT_MODULE", runtime.playwright_module)
    monkeypatch.setenv("PLAYWRIGHT_EXECUTABLE_PATH", runtime.executable)
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(CONTRACT), encoding="utf-8")
    panel = _panel(tmp_path / "panel", **kw)
    return check.run(panel, tmp_path / "out", contract)


def _failed(receipt: dict) -> dict[str, str]:
    return {c["id"]: c["code"] for c in receipt["checks"] if c["status"] != "PASS"}


def test_matching_panel_passes_and_receipt_is_value_free(tmp_path, monkeypatch) -> None:
    receipt = _run(tmp_path, monkeypatch)
    assert receipt["status"] == "PASS" and _failed(receipt) == {}
    text = (tmp_path / "out" / "published-render-receipt.json").read_text("utf-8")
    assert "12.5" not in text and "Reason" not in text and "2026-06-30" not in text
    assert (tmp_path / "out" / "command-desktop.png").is_file()


def test_stale_static_banner_fails(tmp_path, monkeypatch) -> None:
    # Kill case: the page still shows the old banner and the payload says READY.
    receipt = _run(tmp_path, monkeypatch, heading="Do not expand systematic risk")
    failed = _failed(receipt)
    assert receipt["status"] == "FAIL"
    assert (
        failed["stale"] == "FORBIDDEN_TEXT_PRESENT"
        and failed["heading"] == "TEXT_DIFFERS"
    )


def test_banner_allowed_when_the_release_says_do_not_expand(
    tmp_path, monkeypatch
) -> None:
    receipt = _run(
        tmp_path,
        monkeypatch,
        heading="DO NOT EXPAND SYSTEMATIC RISK",
        readiness="DO_NOT_EXPAND_SYSTEMATIC_RISK",
    )
    assert "stale" not in _failed(receipt) and receipt["status"] == "PASS"


def test_every_mismatch_is_reported_not_just_the_first(tmp_path, monkeypatch) -> None:
    receipt = _run(
        tmp_path, monkeypatch, reason="Reason 99.0%", gates=1, end="2026-05-01"
    )
    assert _failed(receipt) == {
        "reason": "TEXT_DIFFERS",
        "gates": "COUNT_DIFFERS",
        "figure": "NUMBER_DIFFERS",
        "chart_end": "TEXT_DIFFERS",
    }


def test_missing_runtime_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_MODULE", str(tmp_path / "nope.mjs"))
    panel = _panel(tmp_path / "panel")
    receipt = check.run(panel, tmp_path / "out")
    assert (
        receipt["status"] == "FAIL" and receipt["code"] == "BROWSER_RUNTIME_UNAVAILABLE"
    )
    assert os.path.exists(tmp_path / "out" / "published-render-receipt.json")


def test_output_is_create_only(tmp_path, monkeypatch) -> None:
    _run(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        check.run(tmp_path / "panel", tmp_path / "out")


def test_shipped_contract_names_the_stale_banner_rule() -> None:
    shipped = json.loads(check.CONTRACT.read_text("utf-8"))
    rule = next(
        c for c in shipped["checks"] if c["id"] == "no_stale_do_not_expand_banner"
    )
    assert rule["phrases"] == ["Do not expand systematic risk"]
