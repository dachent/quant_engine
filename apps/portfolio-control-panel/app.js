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
  const decimal = (value, digits = 3) => number(value) == null ? "—" : Number(value).toFixed(digits);
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
      ["Positions", data.fact_status.positions, data.counts.positions ? `${integer.format(data.counts.positions)} accepted current rows` : "No accepted position source", data.counts.positions ? "warning" : "danger"],
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

  let selectedFactorSeries = null;
  function renderFactorDetail() {
    const analysis = data.factor_analysis;
    const row = analysis.series.find((item) => item.series_id === selectedFactorSeries) || analysis.series[0];
    if (!row) return;
    selectedFactorSeries = row.series_id;
    $$("[data-factor-strategy]").forEach((button) => button.classList.toggle("is-active", button.dataset.factorStrategy === selectedFactorSeries));
    if (row.status !== "AVAILABLE") {
      const aligned = row.coverage?.aligned_observations ?? 0;
      $("#factor-detail").innerHTML = `<div class="callout callout-danger"><strong>${safe(row.series_id)} — UNAVAILABLE</strong><br>${safe(row.unavailable_reasons.join(" · "))}. Accepted aligned observations: ${integer.format(aligned)}. No exposure or alpha is inferred.</div>`;
      return;
    }
    const alpha = row.alpha;
    const holdout = row.diagnostics.holdout;
    const summary = row.contribution_summary;
    const maximumExposure = Math.max(...row.exposures.map((item) => Math.abs(Number(item.exposure))), 1);
    const exposures = row.exposures.map((item) => {
      const exposure = Number(item.exposure);
      const width = Math.min(48, Math.abs(exposure) / maximumExposure * 48);
      const left = exposure < 0 ? 50 - width : 50;
      const uncertainty = item.standard_error == null ? "Coefficient SE unavailable for fitted method" : `SE ${decimal(item.standard_error)}`;
      return `<div class="exposure-row"><div class="exposure-name"><strong>${safe(item.factor_id)}</strong><span>${safe(uncertainty)}</span></div><div class="exposure-track" aria-label="${safe(item.factor_id)} exposure ${decimal(item.exposure)}"><span class="exposure-fill ${exposure < 0 ? "is-negative" : ""}" style="left:${left}%;width:${width}%"></span></div><div class="exposure-value">${decimal(item.exposure)}×</div></div>`;
    }).join("");
    const contributionRows = [
      ...Object.entries(summary.factor_means).map(([factor, value]) => `<tr><td>${safe(factor)}</td><td>Factor contribution</td><td class="numeric">${pct(value, 3)}</td></tr>`),
      `<tr><td>Alpha</td><td>Periodic intercept contribution</td><td class="numeric">${pct(summary.alpha_mean, 3)}</td></tr>`,
      `<tr><td>Fitted</td><td>Factor plus alpha</td><td class="numeric">${pct(summary.fitted_mean, 3)}</td></tr>`,
      `<tr><td>Residual</td><td>Realized minus fitted</td><td class="numeric">${pct(summary.residual_mean, 3)}</td></tr>`,
      `<tr><td>Realized</td><td>Observed excess return</td><td class="numeric">${pct(summary.realized_mean, 3)}</td></tr>`,
    ];
    $("#factor-detail").innerHTML = `<div class="analytics-detail-grid"><div class="metric-block"><div class="metric-label">Net alpha</div><div class="metric-value">${pct(alpha.net_annualized)}</div><div class="metric-note">${safe(alpha.net_status)} · observed basis</div></div><div class="metric-block"><div class="metric-label">Gross alpha</div><div class="metric-value">${pct(alpha.gross_annualized)}</div><div class="metric-note">${safe(alpha.gross_status)}</div></div><div class="metric-block"><div class="metric-label">Holdout predicted R²</div><div class="metric-value">${pct(holdout.predicted_r_squared)}</div><div class="metric-note">${integer.format(holdout.observation_count)} out-of-sample observations</div></div><div class="metric-block"><div class="metric-label">Tracking error</div><div class="metric-value">${pct(holdout.tracking_error_annualized)}</div><div class="metric-note">Annualized holdout residual volatility</div></div><div class="metric-block"><div class="metric-label">Exact-date coverage</div><div class="metric-value">${integer.format(row.coverage.aligned_observations)}</div><div class="metric-note">${formatDate(row.coverage.observed_start)}–${formatDate(row.coverage.observed_end)}</div></div><div class="metric-block"><div class="metric-label">Reconciliation error</div><div class="metric-value">${safe(summary.maximum_reconciliation_error)}</div><div class="metric-note">Maximum absolute return identity error</div></div></div><div class="grid grid-12" style="margin-top:16px"><div class="panel span-7"><div class="panel-heading"><div><div class="eyebrow">Selected exposures</div><h3>${safe(row.series_id)}</h3></div><span class="badge badge-positive">${safe(row.evidence_layer)}</span></div><div class="exposure-list">${exposures}</div></div><div class="panel span-5"><div class="panel-heading"><div><div class="eyebrow">Holdout mean partition</div><h3>Realized = fitted + residual</h3></div><span class="badge">n=${integer.format(summary.observations)}</span></div><div class="table-wrap"><table><caption>Holdout factor contribution reconciliation</caption>${tableMarkup([["Component"], ["Role"], ["Mean", "numeric"]], contributionRows)}</table></div></div></div>`;
  }

  function renderAttribution() {
    const analysis = data.factor_analysis;
    const firstAvailable = analysis.series.find((row) => row.status === "AVAILABLE");
    selectedFactorSeries ||= firstAvailable?.series_id || analysis.series[0]?.series_id;
    renderKpis("#factor-kpis", [
      ["Validated models", `${analysis.available_count}/${analysis.series.length}`, "Parsimony-selected holdout models", "positive"],
      ["Unavailable", String(analysis.unavailable_count), "No inferred exposures or alpha", "danger"],
      ["Methodology", analysis.methodology_version, analysis.method_label, "warning"],
      ["Analytics as of", formatDate(analysis.as_of_date), `Generation ${analysis.generation_id.slice(0, 12)}…`],
    ]);
    $("#factor-method-badge").textContent = analysis.method_label.replaceAll("_", " ");
    const rows = analysis.series.map((row) => {
      const holdout = row.diagnostics?.holdout;
      return `<tr><td><span class="cell-primary">${safe(row.series_id)}</span><span class="cell-secondary">${safe(row.model_id || "NO VALIDATED MODEL")}</span></td><td>${statusText(row.status)}</td><td>${safe(row.exposures.map((item) => item.factor_id).join(" · ") || "—")}</td><td>${safe(row.coverage?.observed_start || "—")}<span class="cell-secondary">${safe(row.coverage?.observed_end || "—")}</span></td><td class="numeric">${row.coverage ? integer.format(row.coverage.aligned_observations) : "—"}</td><td class="numeric">${pct(holdout?.predicted_r_squared)}</td><td class="numeric">${pct(row.alpha?.net_annualized)}</td><td>${statusText(row.alpha?.gross_status || row.unavailable_reasons.join(" · "))}</td></tr>`;
    });
    $("#factor-model-table").innerHTML = tableMarkup([["Strategy / model"], ["Status"], ["Factors"], ["Observed interval"], ["Aligned", "numeric"], ["Holdout R²", "numeric"], ["Net alpha", "numeric"], ["Gross / unavailable state"]], rows);
    $("#factor-strategy-selector").innerHTML = analysis.series.map((row) => `<button type="button" class="strategy-chip ${row.status === "AVAILABLE" ? "" : "is-unavailable"}" data-factor-strategy="${safe(row.series_id)}">${safe(row.series_id)}</button>`).join("");
    $$("[data-factor-strategy]").forEach((button) => button.addEventListener("click", () => { selectedFactorSeries = button.dataset.factorStrategy; renderFactorDetail(); }));
    renderFactorDetail();
  }

  const matrixLabels = {
    observed_pearson: ["Observed Pearson correlation", "Measured · compatible exact-date evidence only"],
    observed_spearman: ["Observed rank dependence", "Measured · robust Spearman rank check"],
    posterior: ["Posterior correlation", "Conceptual prior plus capped observed weight · PSD projected"],
    stress: ["Stress correlation", "Stress assumption · PSD projected"],
    conceptual_prior: ["Conceptual prior", "Assumption · not measured data"],
    decision_no_missing_diversification_credit: ["No-credit decision view", "Missing observed pairs set to 1.0 · not a covariance input"],
  };
  let selectedMatrix = "observed_pearson";
  let selectedPair = null;
  const shortSeries = (value) => String(value).replace(" Portfolio", "").replace(" Strategy", "").replace(" Factor [P123]", " P123").replace("GrizzlyBears VTMA", "Grizzly").replace("Robuxio Crypto (High Sharpe High Vol)", "Robuxio").replace("[Composer] - The Holy Grail (Buy Copy)", "Composer").replace("Growth at Market Pivot Points [EchoTrade]", "EchoTrade");
  const pairKey = (left, right) => [left, right].sort().join("\u0000");

  function renderCorrelationDetail(left, right) {
    const detail = $("#correlation-detail");
    if (left === right) {
      $("#correlation-detail-title").textContent = shortSeries(left);
      detail.innerHTML = `<div class="callout callout-neutral">Diagonal values are 1.0 by definition. Select a non-diagonal cell to inspect pair evidence.</div>`;
      return;
    }
    const cell = data.dependence_analysis.cells.find((item) => pairKey(item.left, item.right) === pairKey(left, right));
    if (!cell) return;
    const observed = cell.observed;
    const coverage = observed.coverage;
    const blend = cell.blend;
    const implementation = cell.implementation;
    $("#correlation-detail-title").textContent = `${shortSeries(left)} × ${shortSeries(right)}`;
    detail.innerHTML = `<div class="validation-card"><div><strong>Observed status</strong><span>${safe(coverage.evidence_layer || "No compatible measured layer")}</span></div><div class="validation-score">${safe(observed.status)}</div></div><div class="evidence-grid"><dl class="detail-list"><div><dt>Pearson</dt><dd>${decimal(observed.pearson)}</dd></div><div><dt>Spearman</dt><dd>${decimal(observed.spearman)}</dd></div><div><dt>Downside</dt><dd>${decimal(observed.downside.correlation)}</dd></div><div><dt>Latest rolling</dt><dd>${decimal(observed.rolling_pearson.latest)}</dd></div><div><dt>Common observations</dt><dd>${integer.format(coverage.common_observations)}</dd></div><div><dt>Exact-date interval</dt><dd>${safe(coverage.observed_start || "—")}–${safe(coverage.observed_end || "—")}</dd></div></dl><dl class="detail-list"><div><dt>Missing left / right</dt><dd>${integer.format(coverage.missing_from_left)} / ${integer.format(coverage.missing_from_right)}</dd></div><div><dt>Conceptual prior</dt><dd>${decimal(blend.conceptual_prior)}</dd></div><div><dt>Observed weight</dt><dd>${pct(blend.observed_weight)}</dd></div><div><dt>Posterior pre-PSD</dt><dd>${decimal(blend.posterior_pre_psd)}</dd></div><div><dt>Stress pre-PSD</dt><dd>${decimal(blend.stress_pre_psd)}</dd></div><div><dt>Implementation mapping</dt><dd>${safe(implementation.status)}</dd></div></dl></div><div class="callout ${observed.status === "AVAILABLE" ? "callout-neutral" : "callout-danger"}">${safe(blend.decision_status)}. ${safe(blend.conceptual_prior_note)}</div>`;
  }

  function renderCorrelationMatrix() {
    const dependence = data.dependence_analysis;
    const matrix = dependence.matrices[selectedMatrix];
    const series = dependence.series_ids;
    const label = matrixLabels[selectedMatrix];
    $("#matrix-title").textContent = label[0];
    const header = `<thead><tr><th scope="col">Strategy</th>${series.map((name) => `<th scope="col" title="${safe(name)}"><span class="matrix-abbr">${safe(shortSeries(name))}</span></th>`).join("")}</tr></thead>`;
    const rows = series.map((left, rowIndex) => `<tr><th scope="row" title="${safe(left)}"><span class="matrix-abbr">${safe(shortSeries(left))}</span></th>${series.map((right, columnIndex) => {
      const raw = matrix.values[rowIndex][columnIndex];
      const numeric = number(raw);
      const diagonal = rowIndex === columnIndex;
      const unavailable = numeric == null;
      const intensity = numeric == null ? 0 : .12 + Math.min(1, Math.abs(numeric)) * .34;
      const target = numeric < 0 ? [130, 174, 242] : [92, 200, 192];
      const base = [16, 26, 36];
      const color = base.map((channel, index) => Math.round(channel + (target[index] - channel) * intensity));
      const selected = selectedPair === pairKey(left, right);
      return `<td><button type="button" class="matrix-cell-button ${numeric < 0 ? "is-negative" : ""} ${unavailable ? "is-unavailable" : ""} ${diagonal ? "is-diagonal" : ""} ${selected ? "is-selected" : ""}" style="--cell-color:rgb(${color.join(",")})" data-correlation-left="${safe(left)}" data-correlation-right="${safe(right)}" ${diagonal ? "disabled" : ""} aria-label="${safe(left)} and ${safe(right)}: ${unavailable ? "unavailable" : Number(numeric).toFixed(3)}">${unavailable ? "—" : Number(numeric).toFixed(2)}</button></td>`;
    }).join("")}</tr>`).join("");
    $("#correlation-matrix").innerHTML = `<table class="matrix-table"><caption>${safe(label[0])}: ${safe(label[1])}</caption>${header}<tbody>${rows}</tbody></table><div class="callout callout-neutral">${safe(matrix.classification)} · ${safe(label[1])}</div>`;
    $$('[data-correlation-left]').forEach((button) => button.addEventListener("click", () => {
      const left = button.dataset.correlationLeft;
      const right = button.dataset.correlationRight;
      selectedPair = pairKey(left, right);
      renderCorrelationMatrix();
      renderCorrelationDetail(left, right);
    }));
  }

  function renderDependence() {
    const dependence = data.dependence_analysis;
    const corrOne = dependence.correlation_one;
    const unavailable = dependence.pair_count - dependence.available_pair_count;
    renderKpis("#dependence-kpis", [
      ["Measured pairs", `${dependence.available_pair_count}/${dependence.pair_count}`, "Exact-date same-layer observations", "positive"],
      ["Unavailable pairs", String(unavailable), "No inferred diversification credit", "danger"],
      ["Correlation-one loss", pct(corrOne.loss), "Separate conservative portfolio bound", "danger"],
      ["Analytics as of", formatDate(dependence.as_of_date), `Generation ${dependence.generation_id.slice(0, 12)}…`],
    ]);
    selectedPair ||= pairKey(dependence.cells.find((item) => item.observed.status === "AVAILABLE")?.left || dependence.series_ids[0], dependence.cells.find((item) => item.observed.status === "AVAILABLE")?.right || dependence.series_ids[1]);
    renderCorrelationMatrix();
    const selected = dependence.cells.find((item) => pairKey(item.left, item.right) === selectedPair) || dependence.cells[0];
    renderCorrelationDetail(selected.left, selected.right);
    $("#correlation-one-summary").innerHTML = `<div class="metric-block"><div class="metric-label">Loss</div><div class="metric-value">${pct(corrOne.loss)}</div><div class="metric-note">Weighted component adverse losses</div></div><div class="metric-block"><div class="metric-label">Expected</div><div class="metric-value">${pct(corrOne.expected_loss)}</div><div class="metric-note">Accepted baseline reference</div></div><div class="metric-block"><div class="metric-label">Reconciliation</div><div class="metric-value">${safe(corrOne.reconciliation_difference)}</div><div class="metric-note">${safe(corrOne.status)}</div></div>`;
    const componentRows = corrOne.components.map((row) => `<tr><td>${safe(row.series_id)}</td><td class="numeric">${pct(row.allocation_weight)}</td><td class="numeric">${pct(row.adverse_loss_fraction)}</td><td class="numeric">${pct(row.loss_contribution)}</td></tr>`);
    $("#correlation-one-table").innerHTML = tableMarkup([["Strategy"], ["Weight", "numeric"], ["Adverse loss", "numeric"], ["Contribution", "numeric"]], componentRows.length ? componentRows : [emptyRow(4, "No component detail in this accepted fixture")]);
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
    $("#validation-cards").innerHTML = `<div class="validation-card"><div><strong>Portfolio report</strong><span>${safe(data.meta.report_generation_id.slice(0, 12))}…</span></div><div class="validation-score">PUBLISHED</div></div><div class="validation-card"><div><strong>Factor / alpha</strong><span>${safe(data.meta.decomposition_generation_id.slice(0, 12))}…</span></div><div class="validation-score">PUBLISHED</div></div><div class="validation-card"><div><strong>Dependence</strong><span>${safe(data.meta.correlation_generation_id.slice(0, 12))}…</span></div><div class="validation-score">PUBLISHED</div></div><div class="validation-card"><div><strong>Frozen candidate appendix</strong><span>${safe(data.candidate_appendix.as_of_date)} · hash locked</span></div><div class="validation-score">FROZEN</div></div><div class="callout callout-neutral">Portfolio, analytics, and appendix generations retain separate authority. Candidate rows never enter current strategy totals.</div>`;
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
    $$('[data-matrix]').forEach((button) => button.addEventListener("click", () => { selectedMatrix = button.dataset.matrix; $$('[data-matrix]').forEach((item) => item.classList.toggle("is-active", item === button)); renderCorrelationMatrix(); }));
    $("#candidate-search").addEventListener("input", (event) => { candidateSearch = event.target.value.trim(); renderCandidateTable(); });
    $("#snapshot-button").addEventListener("click", () => showToast(`Report ${data.meta.report_generation_id.slice(0, 12)}… · factors ${data.meta.decomposition_generation_id.slice(0, 12)}… · dependence ${data.meta.correlation_generation_id.slice(0, 12)}… · read-only local artifact.`));
    window.addEventListener("hashchange", () => setView(location.hash.slice(1) || "command", false));
  }

  function init() {
    try {
      if (!data || data.panel_schema_version !== 2) throw new Error("Embedded dashboard data is missing or unsupported");
      $("#portfolio-as-of").textContent = formatDate(data.meta.portfolio_as_of);
      $("#coverage-status").textContent = `${pct(data.advisory.nav_coverage)} NAV`;
      renderCommand(); renderCapital(); renderRisk(); renderSignals(); renderAttribution(); renderDependence(); renderCandidates(); renderData(); bindEvents();
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
