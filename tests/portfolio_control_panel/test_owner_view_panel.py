"""#908: the panel shows the generated owner view first and carries no static verdict."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

PANEL = Path(__file__).resolve().parents[2] / "apps" / "portfolio-control-panel"
MARKER = "// #908: the generated owner view"

HARNESS = """
const els = {};
const make = (tag) => ({ tag, children: [], className: "", textContent: "",
  appendChild(c) { this.children.push(c); } });
for (const id of ["owner-view-verdict", "owner-view-items"]) els[id] = make("div");
const heads = [];
global.window = { location: { pathname: process.env.PATHNAME }, PORTFOLIO_OWNER_VIEW: JSON.parse(process.env.VIEW) };
global.document = { getElementById: (id) => els[id], createElement: make, head: { appendChild(s) { heads.push(s); } } };
eval(process.env.SRC);
const script = heads[0];
if (script && process.env.FAIL) script.onerror(); else if (script) script.onload();
process.stdout.write(JSON.stringify({ src: script && script.src, verdict: els["owner-view-verdict"].textContent,
  items: els["owner-view-items"].children.map((c) => ({ tag: c.tag, text: c.children.map((k) => k.textContent) , head: c.textContent })) }));
"""


def _block() -> str:
    text = (PANEL / "app.js").read_bytes().decode("utf-8")
    assert MARKER in text
    return text[text.index(MARKER) :]


def _run(view: dict, *, pathname: str, fail: bool = False) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    env = {
        **os.environ,
        "SRC": _block(),
        "VIEW": json.dumps(view),
        "PATHNAME": pathname,
    }
    if fail:
        env["FAIL"] = "1"
    done = subprocess.run(
        [node, "-e", HARNESS], env=env, capture_output=True, text=True, check=True
    )
    return json.loads(done.stdout)


def _item(klass: str, what: str, real: str, where: str = "w") -> dict:
    return {
        "class": klass,
        "word": klass,
        "what": what,
        "where": where,
        "number_affected": "unknown",
        "fix": "f",
        "who": "me",
        "how_long": "1d",
        "overdue": False,
        "real_or_checker": real,
        "decision": None,
    }


GEN = "a" * 64


def test_the_script_is_loaded_from_the_validation_directory_and_rendered() -> None:
    view = {
        "verdict": "V line",
        "items": [
            _item("UNCLASSIFIED", "x", "real"),
            _item("NOT_APPLICABLE", "y", "checker"),
        ],
    }
    out = _run(view, pathname=f"/root/control-panel/generations/{GEN}/index.html")
    assert out["src"] == f"../../validation/{GEN}/owner-view.js"
    assert out["verdict"] == "V line"
    assert [i["tag"] for i in out["items"]] == [
        "div",
        "details",
    ]  # checker rows collapsed, real rows open


def test_identical_items_group_into_one_row_listing_every_entity() -> None:
    view = {
        "verdict": "v",
        "items": [
            _item("REAL_DATA_GAP", "same", "real", "a1"),
            _item("REAL_DATA_GAP", "same", "real", "a2"),
        ],
    }
    out = _run(view, pathname=f"/c/generations/{GEN}/index.html")
    assert len(out["items"]) == 1 and "Where: a1, a2" in out["items"][0]["text"]


def test_missing_owner_view_says_treat_every_gap_as_real() -> None:
    out = _run(
        {"verdict": "", "items": []},
        pathname=f"/c/generations/{GEN}/index.html",
        fail=True,
    )
    assert "treat every gap as real" in out["verdict"]


def test_page_has_no_static_verdict_and_an_owner_view_slot() -> None:
    html = (PANEL / "index.html").read_bytes().decode("utf-8")
    assert "Do not expand systematic risk" not in html
    assert "Coverage gates remain unresolved" not in html
    assert 'id="owner-view"' in html and html.index('id="owner-view"') < html.index(
        'id="command-heading"'
    )
