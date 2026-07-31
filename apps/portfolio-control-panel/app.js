(() => {
  "use strict";

  const data = window.PORTFOLIO_DATA;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const safe = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
  const number = (value) => value == null || value === "" || Number.isNaN(Number(value)) ? null : Number(value);
  const currency = (value) => number(value) == null ? "—" : money.format(Number(value));
  const pct = (value, digits = 1) => number(value) == null ? "—" : `${(Number(value) * 100).toFixed(digits)}%`;
  const formatDate = (value) => {
    if (!value) return "—";
    const parsed = new Date(`${String(value).slice(0, 10)}T00:00:00Z`);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
  };

  function toneForStatus(status = "") {
    const text = String(status).toUpperCase();
    if (/FAIL|UNAVAILABLE|UNMAPPED|DO_NOT|BLOCK|NONE/.test(text)) return "danger";
    if (/PARTIAL|INFERRED|UNKNOWN|CONDITIONAL|FROZEN/.test(text)) return "warning";
    if (/PASS|AVAILABLE|COMPLETE|PUBLISHED|VERIFIED/.test(text)) return "positive";
    if (/RESERVE|ZERO RISK/.test(text)) return "reserve";
    return "neutral";
  }

  const statusText = (status) => `<span class="status-text ${toneForStatus(status)}">${safe(status)}</span>`;
  const kpi = (label, value, note, tone = "") => `<article class="kpi-card ${tone ? `tone-${tone}` : ""}"><div class="kpi-label">${safe(label)}</div><div class="kpi-value">${safe(value)}</div><div class="kpi-note">${safe(note)}</div></article>`;
  const renderKpis = (selector, items) => { $(selector).innerHTML = items.map((item) => kpi(...item)).join(""); };
  const emptyRow = (columns, message = "No matching rows") => `<tr><td colspan="${columns}" class="cell-secondary">${safe(message)}</td></tr>`;
  const tableMarkup = (headers, rows) => `<thead><tr>${headers.map(([label, cls = ""]) => `<th class="${cls}">${safe(label)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody>`;

  function renderCommand() {
    $("#command-heading").textContent = data.advisory.readiness.replaceAll("_", " ");
    $("#command-reason").textContent = data.advisory.reason;
    renderKpis("#command-kpis", [
      ["Total liquid", currency(data.capital.total_liquid), "Systematic plus reserve"],
      ["Systematic capital", currency(data.capital.systematic), "Subject to the accepted HWM policy"],
      ["Reserve", currency(data.capital.reserve), "Zero strategy risk budget", "reserve"],
      ["Daily NAV coverage", pct(data.advisory.nav_coverage), "Capital-weighted and explicitly partial", "warning"],
    ]);

    const gateRows = data.gates.map((gate) => {
      const width = gate.status === "COMPLETE_WITHIN_DECLARED_BOUNDARY" ? 100 : gate.status === "PARTIAL" ? 55 : 8;
      return `<div class="gate-row is-${toneForStatus(gate.status)}"><div class="gate-name">${safe(gate.label)}<small>${safe(gate.note)}</small></div><div class="gate-track" aria-hidden="true"><div class="gate-fill" style="width:${width}%"></div></div><div class="gate-value">${safe(gate.status)}</div></div>`;
    });
    $("#gate-stack").innerHTML = gateRows.join("");

    const total = Number(data.capital.total_liquid);
    const systematicShare = total ? Number(data.capital.systematic) / total * 100 : 0;
    $("#capital-ring").innerHTML = `<div class="capital-donut" style="background:conic-gradient(var(--accent) 0 ${systematicShare}%, var(--reserve) ${systematicShare}% 100%)"><div class="donut-center"><strong>${currency(total)}</strong><span>liquid</span></div></div><div class="legend-list"><div class="legend-item"><span class="legend-dot" style="background:var(--accent)"></span><span>Systematic · ${systematicShare.toFixed(1)}%</span><strong>${currency(data.capital.systematic)}</strong></div><div class="legend-item"><span class="legend-dot" style="background:var(--reserve)"></span><span>Reserve · ${(100 - systematicShare).toFixed(1)}%</span><strong>${currency(data.capital.reserve)}</strong></div></div>`;

    $("#strategy-coverage-bars").innerHTML = data.strategies.map((row) => `<div><div class="bar-label"><span>${safe(row.strategy_name)}</span><strong>${safe(row.nav_status)}</strong></div><div class="bar-track"><div class="bar-fill" style="width:${Math.max(1, Number(row.systematic_weight || 0) * 100)}%"></div></div></div>`).join("");
    $("#operator-queue").innerHTML = data.blocking_items.map((item) => `<li><div><strong>${safe(item.item)}</strong><span>${safe(item.known_gap)}</span></div></li>`).join("");
    $("#construction-points").innerHTML = [
      `Correlation-one stress is ${pct(data.advisory.correlation_one_stress)} against a ${pct(data.advisory.hwm_limit)} systematic HWM boundary.`,
      `${data.counts.twr_available} of ${data.counts.twr_rows} account-strategy segments have coverage-aware TWR.`,
      "Provider, predecessor, shadow, backtest, and account-live evidence remain separate.",
    ].map((point) => `<div class="thesis-point">${safe(point)}</div>`).join("");
  }

  let allocationFilter = "all";
  function renderAllocationTable() {
    const accepted = data.allocations.filter((row) => allocationFilter === "all" || row.scope === allocationFilter);
    const rows = accepted.map((row) => `<tr><td><span class="cell-primary">${safe(row.strategy_name)}</span><span class="cell-secondary">${safe(row.strategy_id || "reserve-classification")}</span></td><td>${statusText(row.scope)}</td><td class="numeric">${currency(row.current_value_usd)}</td><td class="numeric">${pct(row.systematic_weight)}</td><td>${formatDate(row.as_of_date)}</td><td>${safe(row.source_key)}</td></tr>`);
    const table = $("#allocation-table");
    table.dataset.visibleRows = String(accepted.length);
    table.innerHTML = tableMarkup([["Strategy / identity"], ["Scope"], ["Value", "numeric"], ["Systematic weight", "numeric"], ["As of"], ["Evidence"]], rows.length ? rows : [emptyRow(6)]);
  }

  function renderCapital() {
    renderKpis("#capital-kpis", [
      ["Liquid capital", currency(data.capital.total_liquid), "Accepted dated baseline"],
      ["Current strategies", String(data.counts.strategies), "Reserve is not a strategy"],
      ["Accounts", String(data.counts.accounts), "Stable internal identities"],
      ["Positions", data.fact_status.positions, "No accepted position source", "danger"],
    ]);
    renderAllocationTable();
    const providers = Object.entries(data.provider_counts).sort((a, b) => b[1] - a[1]);
    const maxCount = Math.max(...providers.map(([, count]) => count), 1);
    $("#provider-bars").innerHTML = providers.map(([provider, count]) => `<div><div class="bar-label"><span>${safe(provider)}</span><strong>${integer.format(count)}</strong></div><div class="bar-track"><div class="bar-fill" style="width:${count / maxCount * 100}%"></div></div></div>`).join("");
    const rows = data.strategies.map((row) => `<tr><td><span class="cell-primary">${safe(row.strategy_name)}</span><span class="cell-secondary">${safe(row.strategy_id)}</span></td><td>${safe(row.economic_family)}</td><td class="numeric">${currency(row.current_value_usd)}</td><td class="numeric">${pct(row.systematic_weight)}</td><td>${statusText(row.nav_status)}</td><td>${safe(row.cost_status)}</td><td>${statusText(row.implementation_gap_status)}</td></tr>`);
    $("#strategy-table").innerHTML = tableMarkup([["Strategy / identity"], ["Economic family"], ["Value", "numeric"], ["Weight", "numeric"], ["NAV"], ["Cost"], ["Implementation gap"]], rows);
  }

  function renderRisk() {
    renderKpis("#risk-kpis", [
      ["Correlation-one stress", pct(data.advisory.correlation_one_stress), "Accepted adverse scenario", "danger"],
      ["Systematic HWM", data.advisory.hwm_status, "Unavailable means unavailable", "danger"],
      ["TWR available", `${data.counts.twr_available}/${data.counts.twr_rows}`, "Account-strategy segments", "warning"],
      ["MWR available", `${data.counts.mwr_available}/${data.counts.twr_rows}`, "Complete flows required", data.counts.mwr_available ? "positive" : "danger"],
    ]);
    const stress = Number(data.advisory.correlation_one_stress);
    const passes = stress <= Number(data.advisory.hwm_limit);
    $("#stress-bar").innerHTML = `<div class="scenario-row"><div class="scenario-label">Current portfolio</div><div class="scenario-track"><span class="scenario-limit" aria-hidden="true"></span><span class="scenario-fill ${passes ? "pass" : ""}" style="width:${Math.min(100, stress / 0.4 * 100)}%"></span></div><div class="scenario-value ${passes ? "" : "status-text danger"}">${pct(stress)}</div></div><div class="callout callout-danger">Stress exceeds the 15% HWM boundary. This is advisory evidence, not a liquidation instruction.</div>`;
    $("#hwm-status").innerHTML = `<div class="validation-card"><div><strong>Full-systematic daily NAV</strong><span>Three current strategies lack account-specific daily series</span></div><div class="validation-score">${safe(data.advisory.hwm_status)}</div></div><div class="callout callout-neutral">Closed accounts are not forward-filled. Missing NAV and FX are not interpolated.</div>`;
    const rows = data.performance.map((row) => `<tr><td><code>${safe(row.account_id)}</code></td><td>${safe(row.strategy_name || "UNMAPPED")}</td><td>${safe(row.observed_start || "—")}<span class="cell-secondary">${safe(row.observed_end || "—")}</span></td><td class="numeric">${integer.format(row.nav_observations)}</td><td class="numeric">${pct(row.twr)}</td><td>${statusText(row.twr_status)}</td><td class="numeric">${pct(row.mwr_xirr)}</td><td>${statusText(row.mwr_status)}</td></tr>`);
    $("#performance-table").innerHTML = tableMarkup([["Account"], ["Strategy"], ["Observed interval"], ["NAV obs.", "numeric"], ["TWR", "numeric"], ["TWR status"], ["MWR", "numeric"], ["MWR status"]], rows);
  }

  function renderSignals() {
    const gapTested = data.strategies.filter((row) => row.implementation_gap_status === "tested").length;
    const knownCost = data.strategies.filter((row) => String(row.cost_status).startsWith("known")).length;
    renderKpis("#signal-kpis", [
      ["Implementation gap tested", `${gapTested}/${data.counts.strategies}`, "Provider versus account evidence"],
      ["Known cost status", `${knownCost}/${data.counts.strategies}`, "Unknown stays disclosed", "warning"],
      ["Reconciliations passed", `${data.counts.reconciliation_passed}/${data.counts.reconciliation_checks}`, "Accepted report checks"],
      ["Automated action", "NONE", "Read-only advisory surface", "reserve"],
    ]);
    const implementationRows = data.strategies.map((row) => `<tr><td>${safe(row.strategy_name)}</td><td>${statusText(row.account_live_status)}</td><td class="numeric">${pct(row.account_live_cumulative_return)}</td><td class="numeric">${pct(row.account_live_max_drawdown)}</td><td>${statusText(row.implementation_gap_status)}</td><td>${safe(row.cost_status)}</td><td>${safe(row.version_ref || "UNVERSIONED")}</td></tr>`);
    $("#implementation-table").innerHTML = tableMarkup([["Strategy"], ["Account-live"], ["Cumulative", "numeric"], ["Max drawdown", "numeric"], ["Gap status"], ["Cost status"], ["Version"]], implementationRows);
    const reconciliationRows = [...data.reconciliation_checks, ...data.blocking_items].map((row) => `<tr><td>${safe(row.check_id || row.item)}</td><td>${statusText(row.status)}</td><td>${safe(row.known_gap || row.value_usd || row.value || "—")}</td></tr>`);
    $("#reconciliation-table").innerHTML = tableMarkup([["Check / exception"], ["Status"], ["Evidence / gap"]], reconciliationRows);
  }

  let tierFilter = "all";
  let candidateSearch = "";
  function renderCandidateTable() {
    const query = candidateSearch.toLowerCase();
    const accepted = data.candidate_appendix.providers.filter((row) => (tierFilter === "all" || row.tier === tierFilter) && (!query || `${row.candidate} ${row.portfolio_role} ${row.category}`.toLowerCase().includes(query)));
    const rows = accepted.map((row) => `<tr><td><span class="cell-primary">${safe(row.candidate)}</span><span class="cell-secondary">${safe(row.ticker || row.category)}</span></td><td>${statusText(row.tier)}</td><td><span class="cell-primary">${safe(row.portfolio_role)}</span><span class="cell-secondary">${safe(row.philosophy_module)}</span></td><td>${safe(row.execution)}<span class="cell-secondary">${safe(row.cadence)}</span></td><td>${safe(row.principal_gap)}</td></tr>`);
    const table = $("#candidate-table");
    table.dataset.visibleRows = String(accepted.length);
    table.innerHTML = tableMarkup([["Candidate"], ["Frozen tier"], ["Role / module"], ["Implementation"], ["Principal gap"]], rows.length ? rows : [emptyRow(5)]);
  }

  function renderCandidates() {
    const appendix = data.candidate_appendix;
    $("#candidate-boundary").textContent = `Frozen ${appendix.as_of_date} audit appendix · source ${appendix.source_sha256.slice(0, 14)}… · no refresh or capital authority.`;
    renderKpis("#candidate-kpis", [
      ["Appendix status", appendix.status, "Dated research evidence", "warning"],
      ["Candidates", String(appendix.providers.length), "Not Strategy identities"],
      ["Tier 1", String(appendix.tier_counts["Tier 1"] || 0), "Diligence priority only"],
      ["Capital authority", "NONE", "No selection or allocation", "reserve"],
    ]);
    renderCandidateTable();
  }

  function renderData() {
    renderKpis("#data-kpis", [
      ["Report generation", data.meta.report_generation_id.slice(0, 12), "Accepted local authority"],
      ["Baseline generation", data.meta.baseline_generation_id.slice(0, 12), "Hash-linked input"],
      ["Coverage exceptions", String(data.blocking_items.length), "Visible and blocking where declared", "warning"],
      ["External connections", "0", "Direct-file local operation", "reserve"],
    ]);
    const coverageRows = data.coverage.map((row) => `<tr><td><span class="cell-primary">${safe(row.item)}</span><span class="cell-secondary">${safe(row.category)}</span></td><td>${statusText(row.status)}</td><td>${safe(row.value ?? "—")}</td><td>${safe(row.known_gap || "—")}</td></tr>`);
    $("#coverage-table").innerHTML = tableMarkup([["Fact / category"], ["Status"], ["Value"], ["Known gap"]], coverageRows);
    $("#validation-cards").innerHTML = `<div class="validation-card"><div><strong>Portfolio report</strong><span>Current accepted generation</span></div><div class="validation-score">PUBLISHED</div></div><div class="validation-card"><div><strong>Frozen candidate appendix</strong><span>${safe(data.candidate_appendix.as_of_date)} · hash locked</span></div><div class="validation-score">FROZEN</div></div><div class="callout callout-neutral">The report and appendix have separate authority. Candidate rows never enter current strategy totals.</div>`;
    const sourceRows = data.provenance.map((row) => `<tr><td><span class="cell-primary">${safe(row.artifact)}</span><span class="cell-secondary">${safe(row.schema)}</span></td><td>${formatDate(row.as_of_date)}</td><td><code>${safe(row.sha256.slice(0, 16))}…</code></td></tr>`);
    $("#source-table").innerHTML = tableMarkup([["Artifact / schema"], ["As of"], ["SHA-256"]], sourceRows);
    $("#limitations-list").innerHTML = data.limitations.map((item) => `<li>${safe(item)}</li>`).join("");
  }

  function setView(name, updateHash = true) {
    const view = $(`[data-view="${name}"]`);
    if (!view) return;
    $$(".view").forEach((item) => { item.hidden = item !== view; item.classList.toggle("is-active", item === view); });
    $$('[data-nav]').forEach((button) => { const active = button.dataset.nav === name; button.classList.toggle("is-active", active); active ? button.setAttribute("aria-current", "page") : button.removeAttribute("aria-current"); });
    $("#page-title").textContent = $(`[data-nav="${name}"] span:last-child`)?.textContent || "Portfolio control";
    document.title = `V Capital — ${$("#page-title").textContent}`;
    if (updateHash) history.replaceState(null, "", `#${name}`);
    window.scrollTo({ top: 0, behavior: "auto" });
  }

  function showToast(message) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.hidden = false;
    window.clearTimeout(showToast.timeout);
    showToast.timeout = window.setTimeout(() => { toast.hidden = true; }, 7000);
  }

  function bindEvents() {
    $$('[data-nav]').forEach((button) => button.addEventListener("click", () => setView(button.dataset.nav)));
    $$('[data-allocation-filter]').forEach((button) => button.addEventListener("click", () => { allocationFilter = button.dataset.allocationFilter; $$('[data-allocation-filter]').forEach((item) => item.classList.toggle("is-active", item === button)); renderAllocationTable(); }));
    $$('[data-tier-filter]').forEach((button) => button.addEventListener("click", () => { tierFilter = button.dataset.tierFilter; $$('[data-tier-filter]').forEach((item) => item.classList.toggle("is-active", item === button)); renderCandidateTable(); }));
    $("#candidate-search").addEventListener("input", (event) => { candidateSearch = event.target.value.trim(); renderCandidateTable(); });
    $("#snapshot-button").addEventListener("click", () => showToast(`Report ${data.meta.report_generation_id.slice(0, 12)}… · portfolio as of ${data.meta.portfolio_as_of} · read-only local artifact.`));
    window.addEventListener("hashchange", () => setView(location.hash.slice(1) || "command", false));
  }

  function init() {
    try {
      if (!data || data.panel_schema_version !== 1) throw new Error("Embedded dashboard data is missing or unsupported");
      $("#portfolio-as-of").textContent = formatDate(data.meta.portfolio_as_of);
      $("#coverage-status").textContent = `${pct(data.advisory.nav_coverage)} NAV`;
      renderCommand(); renderCapital(); renderRisk(); renderSignals(); renderCandidates(); renderData(); bindEvents();
      $("#loading-state").hidden = true;
      $("#app").dataset.appState = "ready";
      const requested = location.hash.slice(1);
      setView($(`[data-view="${requested}"]`) ? requested : "command", false);
    } catch (error) {
      console.error(error);
      $("#loading-state").hidden = true;
      $("#error-state").hidden = false;
      $("#app").dataset.appState = "error";
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
