"""Contract tests for the non-tautological interaction-QA source seam."""

from pathlib import Path
import json
import hashlib
import os
import shutil
import subprocess

import pytest

from browser_runtime import resolve_browser_runtime

D8_FIRST_NOTE = (
    "Recorded when this panel was built, before release. These are the epoch "
    "manifest's authority fields, not the release state; the release state is in "
    "the coordinated release record, not in this panel."
)
SCRIPT = (
    Path(__file__).parents[2]
    / "apps"
    / "portfolio-control-panel"
    / "interaction_qa.mjs"
)


def _synthetic_epoch_manifest() -> dict[str, object]:
    strategy_ids = (
        "qsi",
        "p123-ai-factor",
        "p123-eu",
        "grizzlybears-vtma",
        "robuxio-crypto",
        "volatis-strategy",
        "tt-portfolio",
    )
    rows = []
    for index in range(13):
        last_four = f"{index + 1:04d}"
        rows.append(
            {
                "account_id": f"synthetic-account-{index:02d}",
                "account_label": f"Synthetic account {index:02d} ({last_four})",
                "last_four": last_four,
                "strategy_id": strategy_ids[index % len(strategy_ids)],
                "effective_start": "2026-01-01",
                "effective_end": None,
                "status": "OK",
                "start": "2026-01-01",
                "end": "2026-08-13",
                "invested": "1000",
                "withdrawn": "0",
                "net_capital": "1000",
                "current_value": "1100",
                "gain": "100",
                "return_on_net_capital": "0.1",
                "annualized_return_on_net_capital": "0.2",
                "observation_linked_return": "0.1",
                "observed_hwm_index": "110",
                "observed_max_drawdown": "0.05",
            }
        )
    return {
        "schema_version": 9,
        "accounting_currency": "USD",
        "calculation_cutoff_date": "2026-08-13",
        "source_identities": {
            "classified_flows": {
                "ledger_identity": {"artifact": {"as_of_date": "2026-08-16"}}
            }
        },
        "rows": rows,
    }


def test_interaction_qa_rejects_non_calendar_expected_cutoff() -> None:
    """The acceptance input is a real calendar date, not a normalizable string."""

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    result = subprocess.run(
        [
            node,
            str(SCRIPT),
            "--html",
            "unused.html",
            "--output",
            "unused.json",
            "--expected-calculation-cutoff",
            "2026-02-31",
        ],
        env={**os.environ, "PLAYWRIGHT_MODULE": "unused-playwright.mjs"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "--expected-calculation-cutoff must be an ISO calendar date" in result.stderr


def test_interaction_qa_accepts_external_manifest_and_binds_identity() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'options["epoch-manifest"]' in source
    assert "readFile(externalManifestPath)" in source
    assert 'createHash("sha256").update(externalManifestBytes)' in source
    assert (
        "JSON.stringify(dashboardData.account_manifest) === JSON.stringify(externalManifest)"
        in source
    )
    assert '"Panel payload equals the independently supplied epoch manifest"' in source


def test_dom_expected_manifest_prefers_external_source() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert (
        "const epochManifest = externalManifest ?? dashboardData.account_manifest;"
        in source
    )
    assert source.index(
        "const epochManifest = externalManifest ?? dashboardData.account_manifest;"
    ) < source.index("const rowMatches =")


def test_legacy_mode_remains_payload_bound_without_external_argument() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "const externalManifest = externalManifestBytes ?" in source
    assert "externalManifest ?? dashboardData.account_manifest" in source
    assert "if (externalManifestPath) {" in source


def test_external_manifest_behavioral_binding_and_last_four_guard(
    tmp_path: Path,
) -> None:
    """Exercise the real harness, including failure on an authority-only suffix mutation."""
    repo = Path(__file__).parents[2]
    runtime = resolve_browser_runtime(repo)
    if runtime is None:
        pytest.skip("Chromium runtime is unavailable")
    node, playwright_module, executable = runtime

    dashboard_manifest = _synthetic_epoch_manifest()
    external_data = _synthetic_epoch_manifest()
    dashboard_bytes = json.dumps(dashboard_manifest, indent=2).encode("utf-8")
    authority_bytes = json.dumps(external_data, separators=(",", ":")).encode("utf-8")
    assert dashboard_manifest == external_data
    assert dashboard_manifest is not external_data
    assert dashboard_bytes != authority_bytes
    candidate = tmp_path / "candidate"
    (candidate / "data").mkdir(parents=True)
    (candidate / "candidate.json").write_text(
        '{"artifact_sha256":{}}\n', encoding="utf-8"
    )
    dashboard = {
        "account_manifest": dashboard_manifest,
        "meta": {
            "account_manifest_authority_profile": "INDEPENDENT_EXACT_REBUILD",
            "report_generation_id": "r" * 64,
            "decomposition_generation_id": "d" * 64,
            "correlation_generation_id": "c" * 64,
        },
        "dependence_analysis": {"series_ids": []},
        "performance": [],
        "strategies": [],
    }
    (candidate / "data" / "dashboard_data.json").write_text(
        json.dumps(dashboard), encoding="utf-8"
    )
    rows = []
    em_dash = chr(8212)
    for row in dashboard_manifest["rows"]:
        interval_start = (
            row["start"] if row["status"] == "OK" else row["effective_start"]
        )
        interval_end = (
            row["end"]
            if row["status"] == "OK"
            else (row.get("effective_end") or "open")
        )
        interval_suffix = (
            ""
            if row["status"] == "OK"
            else f" · {row['status']}: {row.get('blocking_reason') or 'blocking reason unavailable'}"
        )
        cells = [
            f"<td>{row['strategy_id']}</td>",
            f'<td>{interval_start}<span class="cell-secondary">{interval_end}{interval_suffix}</span></td>',
        ]
        values = [
            row.get(k)
            for k in ("invested", "withdrawn", "net_capital", "current_value", "gain")
        ]
        values += [
            row.get("return_on_net_capital"),
            row.get("annualized_return_on_net_capital"),
            row.get("observation_linked_return"),
            row.get("observed_hwm_index"),
            row.get("observed_max_drawdown"),
        ]
        cells.extend(
            f"<td>{em_dash if row['status'] != 'OK' else (f'{float(v):,.2f}' if i < 5 else (f'{float(v) * 100:.2f}%' if i in (5, 6, 7, 9) else f'{float(v):,.2f}'))}</td>"
            for i, v in enumerate(values)
        )
        rows.append(
            f'<tr data-account-id="{row["account_id"]}"><td>{row["account_label"]}</td>'
            + "".join(cells)
            + "</tr>"
        )
    html = (
        '<html><body><main id="app" data-app-state="ready"><table id="account-manifest-table"><caption>Epoch manifest by account and strategy — monetary values USD</caption><thead><tr>'
        + "".join(
            f"<th>{x}</th>"
            for x in [
                "Account",
                "Strategy",
                "Observed interval",
                "Capital in",
                "Withdrawn",
                "Net capital",
                "Current",
                "Gain",
                "RONC",
                "Ann. RONC",
                "Observed return",
                "Observed HWM",
                "Observed Max DD",
            ]
        )
        + "</tr></thead><tbody>"
        + "".join(rows)
        + '</tbody></table><span id="account-manifest-authority">Independent exact rebuild</span><div id="account-manifest-legend">Calculations through 2026-08-13. Ledger as-of: 2026-08-16. Provider capture date not recorded in this manifest. Capital in = explicit opening capital + deposits + internal-in after start. Net capital = Capital in − Withdrawn.</div><div id="input-bindings"><div class="binding-card">rrrrrrrrrrrr stage input, hash-bound at build</div><div class="binding-card">dddddddddddd stage input, hash-bound at build</div><div class="binding-card">cccccccccccc stage input, hash-bound at build</div></div><ul id="account-manifest-disclosure-list"><li>'
        + D8_FIRST_NOTE
        + "</li></ul></main></body></html>"
    )
    (candidate / "index.html").write_text(html, encoding="utf-8")
    external = tmp_path / "epoch-manifest.json"
    external.write_bytes(authority_bytes)

    env = dict(
        os.environ,
        PLAYWRIGHT_MODULE=str(playwright_module),
        PLAYWRIGHT_EXECUTABLE_PATH=executable,
    )
    output = tmp_path / "qa.json"
    command = [
        node,
        str(repo / "apps" / "portfolio-control-panel" / "interaction_qa.mjs"),
        "--html",
        str(candidate / "index.html"),
        "--output",
        str(output),
        "--exclude-candidates",
        "true",
        "--epoch-only",
        "true",
        "--epoch-manifest",
        str(external),
        "--expected-calculation-cutoff",
        "2026-08-13",
    ]
    passed = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=90
    )
    assert passed.returncode == 0, passed.stderr
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["all_passed"] is True
    binding = next(
        item
        for item in result["checks"]
        if item["check"]
        == "Panel payload equals the independently supplied epoch manifest"
    )
    assert binding["status"] == "PASS"
    assert (
        binding["evidence"]["external_manifest_sha256"]
        == hashlib.sha256(external.read_bytes()).hexdigest()
    )
    assert binding["evidence"]["external_manifest_size"] == external.stat().st_size

    row = external_data["rows"][0]
    assert row.get("last_four")
    external_data["rows"][0]["last_four"] = "9999"
    external.write_text(
        json.dumps(external_data, separators=(",", ":")), encoding="utf-8"
    )
    failed = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=90
    )
    assert failed.returncode != 0
    failed_result = json.loads(output.read_text(encoding="utf-8"))
    assert any(
        item["check"] == "Epoch manifest DOM matches exact source values and blockers"
        and item["status"] == "FAIL"
        for item in failed_result["checks"]
    )

    legacy_output = tmp_path / "legacy-qa.json"
    legacy = subprocess.run(
        [
            node,
            str(repo / "apps" / "portfolio-control-panel" / "interaction_qa.mjs"),
            "--html",
            str(candidate / "index.html"),
            "--output",
            str(legacy_output),
            "--exclude-candidates",
            "true",
            "--epoch-only",
            "true",
            "--expected-calculation-cutoff",
            "2026-08-13",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert legacy.returncode == 0, legacy.stderr
    legacy_result = json.loads(legacy_output.read_text(encoding="utf-8"))
    assert legacy_result["all_passed"] is True
    assert not any(
        item["check"]
        == "Panel payload equals the independently supplied epoch manifest"
        for item in legacy_result["checks"]
    )

    # A rendered manifest can be internally self-consistent yet extend an
    # observed calculation beyond the independently accepted cutoff.  The QA
    # must reject it rather than deriving the boundary from that same manifest.
    cutoff_breach = _synthetic_epoch_manifest()
    cutoff_breach["rows"][0]["end"] = "2026-08-14"
    dashboard["account_manifest"] = cutoff_breach
    (candidate / "data" / "dashboard_data.json").write_text(
        json.dumps(dashboard), encoding="utf-8"
    )
    external.write_text(
        json.dumps(cutoff_breach, separators=(",", ":")), encoding="utf-8"
    )
    (candidate / "index.html").write_text(
        html.replace("2026-08-13</span>", "2026-08-14</span>", 1), encoding="utf-8"
    )
    cutoff_failed = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=90
    )
    assert cutoff_failed.returncode != 0
    cutoff_result = json.loads(output.read_text(encoding="utf-8"))
    assert any(
        item["check"]
        == "No epoch calculation fact extends beyond the explicit accepted cutoff"
        and item["status"] == "FAIL"
        for item in cutoff_result["checks"]
    )

    # The trace itself is not a rendered table field.  Keep every visible
    # value, the dashboard payload, and the independent manifest aligned, then
    # prove a post-cutoff NAV observation still fails the acceptance boundary.
    nav_trace_breach = _synthetic_epoch_manifest()
    nav_trace_breach["rows"][0]["nav_observation_trace"] = [
        {"date": "2026-08-11", "nav": "1000"},
        {"date": "2026-08-12", "nav": "1050"},
        {"date": "2026-08-14", "nav": "1100"},
    ]
    dashboard["account_manifest"] = nav_trace_breach
    (candidate / "data" / "dashboard_data.json").write_text(
        json.dumps(dashboard), encoding="utf-8"
    )
    external.write_text(
        json.dumps(nav_trace_breach, separators=(",", ":")), encoding="utf-8"
    )
    (candidate / "index.html").write_text(html, encoding="utf-8")
    nav_trace_failed = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=90
    )
    assert nav_trace_failed.returncode != 0
    nav_trace_result = json.loads(output.read_text(encoding="utf-8"))
    trace_check = next(
        item
        for item in nav_trace_result["checks"]
        if item["check"]
        == "No epoch calculation fact extends beyond the explicit accepted cutoff"
    )
    assert trace_check["status"] == "FAIL"
    assert any(
        item["field"] == "nav_observation_trace.date"
        for item in trace_check["evidence"]["violations"]
    )
