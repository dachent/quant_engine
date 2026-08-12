(() => {
  "use strict";

  const data = window.PORTFOLIO_DATA;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const moneyExact = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const safe = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
  const domToken = (value) => String(value || "item").toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "item";
  const number = (value) => value == null || value === "" || Number.isNaN(Number(value)) ? null : Number(value);
  const currency = (value) => number(value) == null ? "—" : money.format(Number(value));
  const currencyExact = (value) => number(value) == null ? "Not available" : moneyExact.format(Number(value));
  const pct = (value, digits = 1) => number(value) == null ? "—" : `${(Number(value) * 100).toFixed(digits)}%`;
  const decimal = (value, digits = 3) => number(value) == null ? "—" : Number(value).toFixed(digits);
  const formatDate = (value) => {
    if (!value) return "—";
    const parsed = new Date(`${String(value).slice(0, 10)}T00:00:00Z`);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
  };

  function toneForStatus(status = "") {
    const text = String(status).toUpperCase();
    if (/FAIL|UNAVAILABLE|UNMAPPED|DO_NOT|BLOCK|NONE|NOT_AUTOMATABLE|INCOMPLETE/.test(text)) return "danger";
    if (/PARTIAL|INFERRED|UNKNOWN|CONDITIONAL|FROZEN|DIAGNOSTIC/.test(text)) return "warning";
    if (/PASS|AVAILABLE|COMPLETE|PUBLISHED|VERIFIED/.test(text)) return "positive";
    if (/RESERVE|ZERO RISK|EXCLUDED_DE_MINIMIS/.test(text)) return "reserve";
    return "neutral";
  }

  const STATUS_LABELS = {
    UNAVAILABLE_UNKNOWN_TARGET_SERIES: "No accepted return series",
    UNAVAILABLE_INSUFFICIENT_HISTORY: "Insufficient accepted history",
    UNAVAILABLE_MAPPING_MISSING: "Implementation mapping not recorded",
    UNAVAILABLE_INCOMPATIBLE_EVIDENCE_LAYERS: "Evidence layers are not comparable",
    SELECTED_PARSIMONIOUS_TRAIN_CV_MODEL: "Selected with training-only time-series CV",
    SELECTED_DIAGNOSTIC_ONLY_TRAIN_CV_MODEL: "Computed with training-only CV; PM use blocked by sealed holdout",
    DIAGNOSTIC_ONLY: "Diagnostic only",
    NON_POSITIVE_SEALED_HOLDOUT_PREDICTED_R2: "Non-positive sealed-holdout predicted R²",
    UNAVAILABLE_SEALED_HOLDOUT_PREDICTED_R2: "Sealed-holdout predicted R² unavailable",
    NO_DIVERSIFICATION_CREDIT_MISSING_OBSERVATION: "No diversification credit: observations missing",
    NOT_AUTOMATABLE_MISSING_COMPLETE_DAILY_NAV: "Not automatable: systematic daily NAV is incomplete",
    NOT_AUTOMATABLE_STALE_OR_MISSING_GOVERNED_HWM_DAILY_NAV: "Not automatable: governed HWM daily NAV is stale or incomplete",
    AVAILABLE_GOVERNED_HWM_PERIMETER: "Available across the governed HWM perimeter",
    ACCOUNT_LIVE_AVAILABLE: "Account-live available",
    ACCOUNT_LIVE_UNAVAILABLE: "Account-live unavailable",
    PROVIDER_LIVE_AVAILABLE: "Provider-live available",
    AVAILABLE_BACKTEST_PREHISTORY_PLUS_LIVE: "Available: version-matched backtest prehistory plus live",
    AVAILABLE_LIVE_ONLY_NO_MATCHED_BACKTEST_PREHISTORY: "Available live; no version-matched backtest prehistory",
    AVAILABLE_LIVE_ONLY: "Available: live only",
    AVAILABLE_HWM_QUALIFIED: "Available for HWM",
    EXCLUDED_DE_MINIMIS: "Excluded de minimis",
    UNAVAILABLE_MISSING_DAILY_NAV: "Daily NAV evidence missing",
    UNAVAILABLE_STALE_FOR_HWM: "Daily NAV present but stale for HWM",
    UNAVAILABLE_EVIDENCE_BOUNDARY_MISMATCH: "Daily NAV and freshness boundaries differ",
    UNAVAILABLE_UNACCEPTED_EVIDENCE: "Daily NAV layer not admitted for HWM",
    UNAVAILABLE_MISSING_FRESHNESS: "Daily NAV freshness unavailable",
    UNAVAILABLE_MISSING_VERSION_MATCHED_LIVE: "No version-matched live series",
    UNAVAILABLE_NO_VERSION_MATCHED_BACKTEST_PREHISTORY: "No version-matched backtest prehistory",
    SOURCE_DATA_AVAILABLE: "Source returns available",
    SOURCE_DATA_UNAVAILABLE: "Source returns unavailable",
    MODEL_VALIDATED_FOR_PM_USE: "Model validated for PM use",
    MODEL_COMPUTED_NOT_VALIDATED: "Model computed; PM use blocked",
    MODEL_UNAVAILABLE: "Model unavailable",
    TARGET_UNAVAILABLE_NOT_ADMITTED: "No accepted target decision",
    AVAILABLE_ACCOUNT_LIVE_RETURN_PATH: "Available account-live return path",
    NOT_APPLICABLE_RESERVE: "Reserve — strategy not applicable",
  };
  const humanStatus = (status) => STATUS_LABELS[String(status)] || String(status || "Unknown").replaceAll("_", " ").toLowerCase().replace(/^./, (character) => character.toUpperCase());
  const statusText = (status) => `<span class="status-text ${toneForStatus(status)}">${safe(humanStatus(status))}</span>`;
  const kpi = (label, value, note, tone = "") => `<article class="kpi-card ${tone ? `tone-${tone}` : ""}"><div class="kpi-label">${safe(label)}</div><div class="kpi-value">${safe(value)}</div><div class="kpi-note">${safe(note)}</div></article>`;
  const renderKpis = (selector, items) => { $(selector).innerHTML = items.map((item) => kpi(...item)).join(""); };
  const emptyRow = (columns, message = "No matching rows") => `<tr><td colspan="${columns}" class="cell-secondary">${safe(message)}</td></tr>`;
  const tableMarkup = (headers, rows) => `<thead><tr>${headers.map(([label, cls = ""]) => `<th scope="col" class="${cls}">${safe(label)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody>`;
  const strategyByName = (name) => data.strategies.find((row) => row.strategy_name === name);
  const providerName = (providerId) => ({
    alpaca: "Alpaca",
    fidelity: "Fidelity",
    ibkr: "IBKR",
    matrixport: "Matrixport",
    portfolio123: "Portfolio123",
    quantbase: "Quantbase",
    schwab: "Schwab",
  })[String(providerId || "").toLowerCase()] || String(providerId || "Provider");
  const evidenceCoverage = (row) => {
    const evidence = row.provider_evidence;
    if (!evidence) return "No accepted provider overlay";
    const account = evidence.account;
    const provider = evidence.provider;
    const parts = [`Accepted ${formatDate(evidence.generation_as_of_date)}`];
    if (account) parts.push(`${providerName(account.provider_id || evidence.provider_id)} account-live TWR: ${formatDate(account.observed_start)}–${formatDate(account.observed_end)} · ${integer.format(account.return_observations || 0)} returns`);
    if (provider) parts.push(`${providerName(provider.provider_id || evidence.provider_id)} provider-live book: ${formatDate(provider.observed_start)}–${formatDate(provider.observed_end)} · ${integer.format(provider.return_observations || 0)} returns · governed provider-ledger classification, not brokerage-account authority`);
    if (evidence.current_snapshot) parts.push(`${providerName(provider?.provider_id || evidence.provider_id)} account snapshot: ${formatDate(evidence.current_snapshot.observed_date)}`);
    return parts.join(" · ");
  };
  const evidenceAvailability = (row) => {
    const accountAvailable = row.account_live_status === "AVAILABLE";
    const providerLiveAvailable = row.nav_status === "AVAILABLE" && row.nav_evidence_layer === "PROVIDER_LIVE" && Boolean(row.provider_evidence?.provider);
    const primary = providerLiveAvailable && !accountAvailable
      ? statusText("PROVIDER_LIVE_AVAILABLE")
      : statusText(accountAvailable ? "ACCOUNT_LIVE_AVAILABLE" : "ACCOUNT_LIVE_UNAVAILABLE");
    const accountQualification = providerLiveAvailable && !accountAvailable
      ? '<span class="cell-secondary">Account-live unavailable</span>'
      : "";
    const lineage = row.provider_evidence
      ? `<span class="cell-secondary">${safe(evidenceCoverage(row))}</span>`
      : "";
    return `${primary}${accountQualification}${lineage}`;
  };
  const exactComparison = (row) => {
    const status = row.provider_comparison_status || row.implementation_gap_status;
    if (!String(status || "").startsWith("DESCRIPTIVE_EXACT_DATE")) {
      return `${statusText(status)}<span class="cell-secondary">${safe(row.provider_comparison_disposition || "No accepted exact-date provider/model comparison")}</span>`;
    }
    return `${statusText(status)}<span class="cell-secondary">Exact common dates ${formatDate(row.provider_comparison_observed_start)}–${formatDate(row.provider_comparison_observed_end)} · ${integer.format(row.provider_comparison_observations || 0)} observations</span><span class="cell-secondary">Account ${pct(row.provider_account_cumulative_return)} · reference ${pct(row.provider_reference_cumulative_return)} · cumulative gap ${pct(row.provider_cumulative_gap)} · active annualized ${pct(row.provider_active_return_annualized)}</span><span class="cell-secondary">Tracking error ${pct(row.provider_tracking_error_annualized)} · information ratio ${decimal(row.provider_information_ratio)} · beta ${decimal(row.provider_beta)} · correlation ${decimal(row.provider_correlation)}</span><span class="cell-secondary">Up capture ${pct(row.provider_upside_capture)} · down capture ${pct(row.provider_downside_capture)} · relative drawdown ${pct(row.provider_relative_drawdown)} · cash drag ${statusText(row.cash_drag_status)}</span><span class="cell-secondary">${safe(row.provider_comparison_disposition)}</span>`;
  };
  const costDisclosure = (row) => {
    const amount = number(row.annual_external_cost_usd) == null ? "Pre-tax cost unavailable" : `${currencyExact(row.annual_external_cost_usd)} annually`;
    const rate = number(row.annual_external_cost_rate_pre_tax) == null ? "rate unavailable" : `${pct(row.annual_external_cost_rate_pre_tax, 2)} of current sleeve`;
    return `<span class="cell-primary">${safe(row.cost_status)}</span><span class="cell-secondary">Fees ${statusText(row.fees_status)} · ${safe(amount)} · ${safe(rate)}</span><span class="cell-secondary">After-tax ${statusText(row.after_tax_status)}${number(row.after_tax_annual_cost_rate) == null ? "" : ` · ${pct(row.after_tax_annual_cost_rate, 2)}`}</span>`;
  };
  const benchmarkDisclosure = (row) => `<span class="cell-primary">${statusText(row.benchmark_assignment_status)}</span><span class="cell-secondary">Effective ${formatDate(row.benchmark_effective_start)}–${formatDate(row.benchmark_effective_end)} · ${safe(row.benchmark_id || "No admitted benchmark")}</span><span class="cell-secondary">${safe(row.benchmark_rationale || "No benchmark rationale recorded")}</span>`;
  const hwmQualification = (row) => {
    if (row.hwm_scope === "EXCLUDED_DE_MINIMIS") return { status: "EXCLUDED_DE_MINIMIS", note: row.hwm_exclusion_reason || "Explicit policy exclusion; not a computed size threshold" };
    if (row.nav_status !== "AVAILABLE") return { status: "UNAVAILABLE_MISSING_DAILY_NAV", note: "No daily NAV evidence is present" };
    if (row.hwm_evidence_status !== "ACCEPTED") return { status: "UNAVAILABLE_UNACCEPTED_EVIDENCE", note: "Daily NAV evidence is present but its evidence layer is not admitted for HWM" };
    if (row.hwm_freshness_status === "BOUNDARY_MISMATCH") return { status: "UNAVAILABLE_EVIDENCE_BOUNDARY_MISMATCH", note: `Daily NAV ends ${formatDate(row.nav_observed_end)} but freshness evidence ends ${formatDate(row.hwm_freshness_observed_end)}; HWM remains blocked until the boundaries agree` };
    if (row.hwm_freshness_status === "BLOCKED") return { status: "UNAVAILABLE_STALE_FOR_HWM", note: `Daily NAV evidence is accepted but ${String(row.hwm_freshness_state || "stale").toLowerCase()} for HWM` };
    if (row.hwm_freshness_status !== "ACCEPTED") return { status: "UNAVAILABLE_MISSING_FRESHNESS", note: "Daily NAV evidence is present but its freshness assessment is unavailable" };
    return { status: "AVAILABLE_HWM_QUALIFIED", note: "Daily NAV evidence is present and HWM-qualified" };
  };
  const hwmEvidenceLabel = (row) => {
    const evidence = row.provider_evidence || {};
    const sourceProvider = row.nav_evidence_layer === "PROVIDER_LIVE"
      ? evidence.provider?.provider_id || evidence.provider_id
      : evidence.account?.provider_id || evidence.provider_id;
    const layer = row.nav_evidence_layer === "PROVIDER_LIVE" ? "provider-live" : "account-live";
    return `${providerName(sourceProvider)} ${layer}`;
  };
  const hwmExclusionNote = () => (data.hwm_summary.excluded_details || []).map((item) => `${item.strategy_name}: ${currencyExact(item.current_value_usd)} · ${item.reason}`).join(" · ") || "No policy exclusions";
  const VIEW_TITLES = {
    command: "Portfolio",
    capital: "Allocation & lifecycle",
    risk: "Performance & HWM",
    signals: "Implementation detail",
    attribution: "Factors & alpha",
    dependence: "Dependence",
    weekly: "Weekly returns",
    candidates: "Frozen candidate appendix",
    data: "Data health",
  };
  const accountDimension = data.account_summary || [];
  const accountLabels = new Map();
  Object.entries(accountDimension.reduce((groups, row) => {
    const provider = providerName(row.provider_id || row.provider);
    (groups[provider] ||= []).push(row.account_id);
    return groups;
  }, {})).forEach(([provider, ids]) => ids.sort().forEach((accountId, index) => accountLabels.set(accountId, `${provider} account ${index + 1}`)));
  let selectedContextAccount = "ALL";
  let selectedContextStrategy = "ALL";
  const accountLabel = (accountId) => accountLabels.get(accountId) || "Unrecognized account";
  const matchesContext = (row) => (selectedContextAccount === "ALL" || row.account_id === selectedContextAccount)
    && (selectedContextStrategy === "ALL" || row.strategy_name === selectedContextStrategy || row.strategy_id === selectedContextStrategy);
  const targetState = (row) => {
    const target = number(row.accepted_target_weight ?? row.target_weight ?? row.target_systematic_weight);
    if (target == null) return { target: null, drift: null, status: "TARGET_UNAVAILABLE_NOT_ADMITTED" };
    const actual = number(row.systematic_weight);
    return { target, drift: actual == null ? null : actual - target, status: "AVAILABLE" };
  };
  const modelState = (row) => row.status === "AVAILABLE"
    ? "MODEL_VALIDATED_FOR_PM_USE"
    : row.status === "DIAGNOSTIC_ONLY" ? "MODEL_COMPUTED_NOT_VALIDATED" : "MODEL_UNAVAILABLE";
  const sourceState = (row) => {
    const strategy = strategyByName(row.series_id);
    const available = number(row.coverage?.aligned_observations) > 0
      || strategy?.account_live_status === "AVAILABLE"
      || strategy?.nav_status === "AVAILABLE"
      || Boolean(strategy?.provider_evidence?.provider || strategy?.provider_evidence?.account);
    return available ? "SOURCE_DATA_AVAILABLE" : "SOURCE_DATA_UNAVAILABLE";
  };

  function renderCommand() {
    $("#command-heading").textContent = data.advisory.readiness.replaceAll("_", " ");
    $("#command-reason").textContent = data.advisory.reason;
    renderKpis("#command-kpis", [
      ["Total liquid", currency(data.capital.total_liquid), "Systematic plus reserve"],
      ["Systematic capital", currency(data.capital.systematic), "Subject to the accepted HWM policy"],
      ["Reserve", currency(data.capital.reserve), "Zero strategy risk budget", "reserve"],
      ["Governed NAV coverage", pct(data.advisory.nav_coverage), `${currencyExact(data.hwm_summary.covered_value_usd)} covered of ${currencyExact(data.hwm_summary.governed_perimeter_value_usd)}; ${currencyExact(data.hwm_summary.uncovered_value_usd)} uncovered. ${hwmExclusionNote()}`, "warning"],
    ]);

    const gateRows = data.gates.map((gate) => {
      const key = `${gate.item || ""} ${gate.label || ""}`.toLowerCase();
      const summary = key.includes("position")
        ? `${integer.format(data.counts.positions || 0)} accepted current position rows; provider-boundary exceptions remain in Data health.`
        : key.includes("cash flow")
          ? "Accepted Schwab, IBKR, and Alpaca flow coverage is partial; full disposition remains in Data health."
          : gate.note;
      return `<div class="gate-row is-${toneForStatus(gate.status)}"><div class="gate-name">${safe(gate.label)}<small>${safe(summary)}</small></div><div class="gate-value">${statusText(gate.status)}</div></div>`;
    });
    $("#gate-stack").innerHTML = gateRows.join("");

    const total = Number(data.capital.total_liquid);
    const systematicShare = total ? Number(data.capital.systematic) / total * 100 : 0;
    $("#capital-ring").innerHTML = `<div class="capital-donut" style="background:conic-gradient(var(--accent) 0 ${systematicShare}%, var(--reserve) ${systematicShare}% 100%)"><div class="donut-center"><strong>${currency(total)}</strong><span>liquid</span></div></div><div class="legend-list"><div class="legend-item"><span class="legend-dot" style="background:var(--accent)"></span><span>Systematic · ${systematicShare.toFixed(1)}%</span><strong>${currency(data.capital.systematic)}</strong></div><div class="legend-item"><span class="legend-dot" style="background:var(--reserve)"></span><span>Reserve · ${(100 - systematicShare).toFixed(1)}%</span><strong>${currency(data.capital.reserve)}</strong></div></div>`;

    $("#strategy-coverage-bars").innerHTML = data.strategies.map((row) => {
      const qualification = hwmQualification(row);
      return `<div class="coverage-status-row"><div class="bar-label"><span>${safe(row.strategy_name)}</span><span>Evidence ${statusText(row.nav_status)} · HWM ${statusText(qualification.status)}</span></div><div class="cell-secondary">${safe(qualification.note)} · ${integer.format(row.available_twr_segments || 0)} coverage-qualified TWR segments · systematic weight ${pct(row.systematic_weight)}</div></div>`;
    }).join("");
    const currentMapped = data.positions.filter((row) => row.status === "AVAILABLE_MAPPED").length;
    const operatorLabels = {
      "full-systematic-hwm-unavailable": "Governed systematic HWM cannot yet be calculated",
      "current-strategy-attribution-partial": "Current pool-only intervals need strategy attribution",
      "historical-strategy-attribution-unresolved": "Historical pool-only intervals remain unresolved",
      "positions-partial": "Position coverage is partial",
      "account-nav-coverage-partial": "Account NAV coverage is partial",
      "transaction-coverage-partial": "Transaction coverage is partial",
      "flow-coverage-partial": "Cash-flow coverage is partial",
    };
    const remediationRoute = {
      "full-systematic-hwm-unavailable": { view: "risk", label: "Inspect HWM evidence" },
      "current-strategy-attribution-partial": { view: "capital", label: "Inspect assignment lifecycle" },
      "historical-strategy-attribution-unresolved": { view: "risk", label: "Inspect historical intervals" },
    };
    $("#operator-queue").innerHTML = data.blocking_items.map((item) => {
      const note = item.item === "current-strategy-attribution-partial"
        ? `${currentMapped}/${data.positions.length} accepted current positions are strategy-mapped; other current General intervals remain valid at the systematic-pool layer only. ${item.known_gap || ""}`
        : item.known_gap;
      const route = remediationRoute[item.item] || { view: "data", label: "Inspect data health" };
      const focus = route.view === "data" ? ` data-focus-coverage="${safe(item.item)}"` : "";
      return `<li><div><strong>${safe(operatorLabels[item.item] || humanStatus(item.item))}</strong><span>${safe(note)}</span><button type="button" class="text-action" data-open-view="${safe(route.view)}"${focus} aria-label="${safe(route.label)} for ${safe(humanStatus(item.item))}">${safe(route.label)} →</button></div></li>`;
    }).join("");
    const implementationRows = data.strategies.map((row) => {
      const factorRow = factorLenses.flatMap((lens) => lens.series || []).find((item) => item.series_id === row.strategy_name);
      const factorState = factorRow ? modelState(factorRow) : "MODEL_UNAVAILABLE";
      return `<tr><td>${safe(row.strategy_name)}</td><td>${evidenceAvailability(row)}</td><td>${statusText(row.implementation_gap_status)}</td><td>${safe(row.cost_status)}</td><td>${statusText(factorState)}</td></tr>`;
    });
    $("#portfolio-implementation-table").innerHTML = tableMarkup([["Strategy"], ["Source evidence"], ["Implementation gap"], ["Cost"], ["Factor model"]], implementationRows);
    $("#construction-points").innerHTML = [
      `Correlation-one stress is ${pct(data.advisory.correlation_one_stress)} against a ${pct(data.advisory.hwm_limit)} systematic HWM boundary.`,
      `${data.counts.twr_available} of ${data.counts.twr_rows} account-strategy segments have coverage-aware TWR.`,
      "Provider, predecessor, shadow, backtest, and account-live evidence remain separate.",
    ].map((point) => `<div class="thesis-point">${safe(point)}</div>`).join("");
  }

  let allocationFilter = "all";
  let positionSearch = "";
  let positionPage = 0;
  const positionPageSize = 50;
  function renderAllocationTable() {
    const accepted = data.allocations.filter((row) => (allocationFilter === "all" || row.scope === allocationFilter)
      && (selectedContextStrategy === "ALL" || row.strategy_name === selectedContextStrategy || row.strategy_id === selectedContextStrategy));
    const rows = accepted.map((row) => {
      const target = targetState(row);
      return `<tr><td><span class="cell-primary">${safe(row.strategy_name)}</span><span class="cell-secondary">${safe(row.strategy_id || "reserve-classification")}</span></td><td>${statusText(row.scope)}</td><td class="numeric">${currency(row.current_value_usd)}</td><td class="numeric">${pct(row.systematic_weight)}</td><td class="numeric">${pct(target.target)}</td><td class="numeric">${pct(target.drift)}</td><td>${statusText(target.status)}</td><td>${formatDate(row.as_of_date)}</td></tr>`;
    });
    const table = $("#allocation-table");
    table.dataset.visibleRows = String(accepted.length);
    table.innerHTML = tableMarkup([["Strategy / identity"], ["Scope"], ["Actual value", "numeric"], ["Actual weight", "numeric"], ["Accepted target", "numeric"], ["Drift", "numeric"], ["Target state"], ["As of"]], rows.length ? rows : [emptyRow(8)]);
  }

  function renderLifecycleTable() {
    const assignments = data.performance.filter(matchesContext);
    const rows = assignments.map((row) => {
      const reserve = row.strategy_mapping_disposition === "RESERVE_NOT_APPLICABLE" || row.pool_id === "reserve-capital";
      const assignment = row.strategy_name || (reserve ? "Reserve — strategy not applicable" : "Systematic pool only");
      const mapping = row.strategy_name ? "AVAILABLE" : reserve ? "NOT_APPLICABLE_RESERVE" : "UNMAPPED";
      return `<tr data-account-id="${safe(row.account_id)}"><td>${safe(accountLabel(row.account_id))}</td><td>${safe(assignment)}</td><td>${safe(row.effective_start || "Open/unknown")}</td><td>${safe(row.effective_end || "Open")}</td><td>${statusText(mapping)}</td><td><code>${safe(row.assignment_id || "—")}</code></td></tr>`;
    });
    $("#lifecycle-table").innerHTML = tableMarkup([["Account"], ["Assignment"], ["Effective start"], ["Effective end"], ["Mapping state"], ["Evidence identity"]], rows.length ? rows : [emptyRow(6, "No lifecycle intervals match the selected context")]);
  }

  function renderPositionTable() {
    const query = positionSearch.toLowerCase();
    const accepted = data.positions.filter((row) => matchesContext(row)
      && (!query || `${row.strategy_name || ""} ${row.symbol || ""} ${row.security_id || ""} ${accountLabel(row.account_id)}`.toLowerCase().includes(query)));
    const pageCount = Math.max(1, Math.ceil(accepted.length / positionPageSize));
    positionPage = Math.min(positionPage, pageCount - 1);
    const visible = accepted.slice(positionPage * positionPageSize, (positionPage + 1) * positionPageSize);
    const positionRows = visible.map((row) => `<tr data-account-id="${safe(row.account_id)}"><td><span class="cell-primary">${safe(row.strategy_name || "UNMAPPED")}</span><span class="cell-secondary">${safe(row.strategy_id || "No accepted strategy")}</span></td><td><span class="cell-primary">${safe(row.symbol || "—")}</span><span class="cell-secondary">${safe(row.security_id || "—")}</span></td><td>${safe(accountLabel(row.account_id))}</td><td class="numeric">${safe(row.quantity ?? "—")}</td><td class="numeric">${currency(row.market_value_usd)}</td><td>${formatDate(row.as_of_date)}</td><td>${statusText(row.status)}</td></tr>`);
    const positionTable = $("#position-table");
    positionTable.dataset.visibleRows = String(accepted.length);
    positionTable.innerHTML = tableMarkup([["Strategy / identity"], ["Security"], ["Account"], ["Quantity", "numeric"], ["Market value", "numeric"], ["As of"], ["Status"]], positionRows.length ? positionRows : [emptyRow(7)]);
    $("#position-summary").textContent = `${integer.format(accepted.length)} matching`;
    $("#position-page-status").textContent = `Page ${positionPage + 1} of ${pageCount}`;
    $("#position-page-previous").disabled = positionPage === 0;
    $("#position-page-next").disabled = positionPage >= pageCount - 1;
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
    const rows = data.strategies.map((row) => {
      const qualification = hwmQualification(row);
      return `<tr><td><span class="cell-primary">${safe(row.strategy_name)}</span><span class="cell-secondary">${safe(row.strategy_id)} · version ${statusText(row.version_disposition || "UNRESOLVED")}</span></td><td>${safe(row.economic_family)}</td><td class="numeric">${currency(row.current_value_usd)}</td><td class="numeric">${pct(row.systematic_weight)}</td><td><span class="cell-primary">Evidence ${statusText(row.nav_status)}</span><span class="cell-secondary">HWM ${statusText(qualification.status)} · ${safe(qualification.note)}</span><span class="cell-secondary">Design ${statusText(row.design_status)} · backtest ${statusText(row.backtest_status)} · live boundary ${statusText(row.live_boundary_status)}</span>${row.provider_evidence ? `<span class="cell-secondary">${safe(evidenceCoverage(row))}</span>` : ""}</td><td>${costDisclosure(row)}</td><td>${exactComparison(row)}<span class="cell-secondary">Version compatibility ${statusText(row.version_compatibility_status)}</span>${benchmarkDisclosure(row)}</td></tr>`;
    });
    $("#strategy-table").innerHTML = tableMarkup([["Strategy / identity"], ["Economic family"], ["Value", "numeric"], ["Weight", "numeric"], ["NAV and evidence coverage"], ["Cost"], ["Implementation gap"]], rows);
    renderLifecycleTable();
    renderPositionTable();
  }

  function renderPerformanceComparison() {
    const weeklyData = data.weekly_returns;
    const strategyRow = selectedContextStrategy === "ALL" ? null : weeklyData?.series?.find((row) => row.primary_group === "STRATEGY" && (row.series_id === selectedContextStrategy || row.display_name === selectedContextStrategy));
    const benchmarkRow = weeklyData?.series?.find((row) => row.primary_group === "CORE_BENCHMARK" || row.display_groups?.includes("CORE_BENCHMARK"));
    const completedWeeks = (weeklyData?.weeks || []).filter((week) => week !== weeklyData.current_week?.week_start).slice(-13);
    const cumulative = (row) => {
      if (!row) return null;
      const values = completedWeeks.map((week) => row.primary_group === "STRATEGY" ? row.values_by_mode?.LIVE_ONLY?.[week] : row.values?.[week]);
      if (!values.length || values.some((value) => number(value) == null)) return null;
      return values.reduce((wealth, value) => wealth * (1 + Number(value)), 1) - 1;
    };
    const strategyReturn = cumulative(strategyRow);
    const benchmarkReturn = cumulative(benchmarkRow);
    const relative = strategyReturn == null || benchmarkReturn == null ? null : strategyReturn - benchmarkReturn;
    $("#performance-comparison").innerHTML = `<div class="metric-block"><div class="metric-label">Account context</div><div class="metric-value metric-value-compact">${safe(selectedContextAccount === "ALL" ? "All accepted accounts" : accountLabel(selectedContextAccount))}</div><div class="metric-note">Persistent context; external account numbers are not shown</div></div><div class="metric-block"><div class="metric-label">Strategy context</div><div class="metric-value metric-value-compact">${safe(strategyRow?.display_name || (selectedContextStrategy === "ALL" ? "Select one strategy" : selectedContextStrategy))}</div><div class="metric-note">Account-live source remains separate from model acceptance</div></div><div class="metric-block"><div class="metric-label">13-week strategy return</div><div class="metric-value">${pct(strategyReturn)}</div><div class="metric-note">${strategyReturn == null ? "Unavailable unless every selected live-only week is accepted" : "Compounded accepted live-only weeks"}</div></div><div class="metric-block"><div class="metric-label">Benchmark</div><div class="metric-value">${pct(benchmarkReturn)}</div><div class="metric-note">${safe(benchmarkRow?.display_name || "No accepted core benchmark")}</div></div><div class="metric-block"><div class="metric-label">Relative return</div><div class="metric-value">${pct(relative)}</div><div class="metric-note">Arithmetic difference over the same completed weeks</div></div>`;
  }

  function renderRisk() {
    const includedHwm = data.strategies.filter((row) => row.hwm_scope === "INCLUDED").map((row) => ({ row, qualification: hwmQualification(row) }));
    const unavailableHwm = includedHwm.filter(({ qualification }) => qualification.status !== "AVAILABLE_HWM_QUALIFIED");
    const excludedNav = data.strategies.filter((row) => row.hwm_scope === "EXCLUDED_DE_MINIMIS").map((row) => row.strategy_name);
    const acceptedHwmEvidence = includedHwm.filter(({ qualification }) => qualification.status === "AVAILABLE_HWM_QUALIFIED").map(({ row }) => row);
    renderKpis("#risk-kpis", [
      ["Correlation-one stress", pct(data.advisory.correlation_one_stress), "Accepted adverse scenario", "danger"],
      ["Systematic HWM", humanStatus(data.advisory.hwm_status), `${currencyExact(data.hwm_summary.uncovered_value_usd)} across ${unavailableHwm.length} unqualified included sleeve${unavailableHwm.length === 1 ? "" : "s"}; no numeric HWM is calculated while unavailable`, unavailableHwm.length ? "danger" : "positive"],
      ["TWR available", `${data.counts.twr_available}/${data.counts.twr_rows}`, "Account-strategy segments", "warning"],
      ["MWR available", `${data.counts.mwr_available}/${data.counts.twr_rows}`, "Complete flows required", data.counts.mwr_available ? "positive" : "danger"],
    ]);
    const stress = Number(data.advisory.correlation_one_stress);
    const passes = stress <= Number(data.advisory.hwm_limit);
    $("#stress-bar").innerHTML = `<div class="scenario-row"><div class="scenario-label">Current portfolio</div><div class="scenario-track"><span class="scenario-limit" aria-hidden="true"></span><span class="scenario-fill ${passes ? "pass" : ""}" style="width:${Math.min(100, stress / 0.4 * 100)}%"></span></div><div class="scenario-value ${passes ? "" : "status-text danger"}">${pct(stress)}</div></div><div class="callout callout-danger">Stress exceeds the 15% HWM boundary. This is advisory evidence, not a liquidation instruction.</div>`;
    const unavailableHwmSummary = (data.hwm_summary.uncovered_details || []).map((item) => `${item.strategy_name}: ${currencyExact(item.current_value_usd)} · ${humanStatus(item.qualification_gap)} · last accepted ${formatDate(item.last_accepted_date)} · ${item.freshness_reason || "No freshness reason recorded"}`).join(" · ");
    const acceptedHwmSummary = acceptedHwmEvidence.map((row) => `${row.strategy_name}: ${integer.format(row.nav_return_observations)} ${hwmEvidenceLabel(row)} return observations from ${formatDate(row.nav_observed_start)} through ${formatDate(row.nav_observed_end)}`).join(" · ");
    $("#hwm-status").innerHTML = `<div class="validation-card"><div><strong>Governed systematic return / NAV evidence</strong><span>${unavailableHwm.length ? safe(unavailableHwmSummary) : "Every included HWM-perimeter sleeve has a qualified account-live or provider-live return path."} ${excludedNav.length ? `${safe(hwmExclusionNote())}.` : ""}</span></div><div class="validation-score">${statusText(data.advisory.hwm_status)}</div></div><div class="callout callout-neutral">Coverage is ${pct(data.advisory.nav_coverage)}: ${currencyExact(data.hwm_summary.covered_value_usd)} covered of ${currencyExact(data.hwm_summary.governed_perimeter_value_usd)}, with ${currencyExact(data.hwm_summary.uncovered_value_usd)} uncovered and ${currencyExact(data.hwm_summary.excluded_value_usd)} visibly excluded. ${unavailableHwm.length ? "The numerical full-systematic HWM is not calculated while any included sleeve is unqualified." : "The governed perimeter is fully qualified."} ${acceptedHwmSummary ? `Accepted account-live/provider-overlay evidence: ${safe(acceptedHwmSummary)}.` : "No governed return/NAV evidence currently qualifies for HWM."} Account NAV observations and canonical account-live return paths remain separately labeled; missing NAV and FX are not interpolated.</div>`;
    renderPerformanceComparison();
    const contextualPerformance = data.performance.filter(matchesContext);
    const mappedPerformance = contextualPerformance.filter((row) => row.strategy_name);
    const historicalGaps = contextualPerformance.filter((row) => !row.strategy_name && row.strategy_mapping_disposition !== "RESERVE_NOT_APPLICABLE" && row.pool_id !== "reserve-capital");
    const rows = mappedPerformance.map((row) => {
      const acceptedReturn = row.twr ?? row.return_path_cumulative_return;
      const returnStatus = row.twr != null ? row.twr_status : row.return_path_status || row.twr_status;
      const observedStart = row.observed_start || row.return_path_observed_start || row.effective_start || "—";
      const observedEnd = row.observed_end || row.return_path_observed_end || row.effective_end || "—";
      const evidence = row.twr != null ? `${integer.format(row.nav_observations)} account NAV observations` : row.return_path_status ? `${integer.format(row.return_path_observations)} canonical account-live returns` : `${integer.format(row.nav_observations)} account NAV observations`;
      return `<tr data-account-id="${safe(row.account_id)}"><td>${safe(accountLabel(row.account_id))}</td><td>${safe(row.strategy_name)}</td><td>${safe(observedStart)}<span class="cell-secondary">${safe(observedEnd)}</span></td><td>${safe(evidence)}</td><td class="numeric">${pct(acceptedReturn)}</td><td>${statusText(returnStatus)}</td><td class="numeric">${pct(row.mwr_xirr)}</td><td>${statusText(row.mwr_status)}</td></tr>`;
    });
    $("#performance-table").innerHTML = tableMarkup([["Account"], ["Strategy"], ["Observed interval"], ["Accepted evidence"], ["Accepted return", "numeric"], ["Return status"], ["MWR", "numeric"], ["MWR status"]], rows);
    $("#historical-gap-badge").textContent = `${historicalGaps.length} unresolved intervals`;
    const historicalRows = historicalGaps.map((row) => `<tr data-account-id="${safe(row.account_id)}"><td>${safe(accountLabel(row.account_id))}</td><td><code>${safe(row.assignment_id)}</code></td><td>${safe(row.effective_start || "Open/unknown")}<span class="cell-secondary">${safe(row.effective_end || "Open/unknown")}</span></td><td>${statusText(row.twr_status)}</td><td>${safe(row.strategy_mapping_disposition === "HISTORICAL_POOL_ONLY" ? "Systematic-pool interval requires historical strategy adjudication" : "Assignment requires adjudication")}</td></tr>`);
    $("#historical-gap-table").innerHTML = tableMarkup([["Account"], ["Assignment"], ["Effective interval"], ["Historical attribution"], ["Required disposition"]], historicalRows.length ? historicalRows : [emptyRow(5, "No unresolved historical assignment intervals")]);
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
    const implementationRows = data.strategies.map((row) => {
      return `<tr><td>${safe(row.strategy_name)}</td><td>${evidenceAvailability(row)}</td><td class="numeric">${pct(row.account_live_cumulative_return)}</td><td class="numeric">${pct(row.account_live_max_drawdown)}</td><td>${exactComparison(row)}</td><td>${costDisclosure(row)}</td><td><span class="cell-primary">${safe(row.version_ref || humanStatus(row.version_disposition || "UNRESOLVED"))}</span><span class="cell-secondary">Effective ${formatDate(row.version_effective_start)}–${formatDate(row.version_effective_end)} · design ${statusText(row.design_status)} · backtest ${statusText(row.backtest_status)} · ${statusText(row.version_compatibility_status)}</span><span class="cell-secondary">${safe(row.version_disposition_reason || "No version disposition recorded")}</span>${benchmarkDisclosure(row)}</td></tr>`;
    });
    $("#implementation-table").innerHTML = tableMarkup([["Strategy"], ["Evidence availability"], ["Account cumulative", "numeric"], ["Account max drawdown", "numeric"], ["Implementation gap"], ["Cost status"], ["Version"]], implementationRows);
    const reconciliationRows = [...data.reconciliation_checks, ...data.blocking_items].map((row) => `<tr><td>${safe(row.check_id || row.item)}</td><td>${statusText(row.status)}</td><td>${safe(row.known_gap || row.value_usd || row.value || "—")}</td></tr>`);
    $("#reconciliation-table").innerHTML = tableMarkup([["Check / exception"], ["Status"], ["Evidence / gap"]], reconciliationRows);
  }

  const factorLenses = data.decomposition_lenses?.lenses || [data.factor_analysis];
  const replicationRows = data.replication_analysis?.series || [];
  let selectedFactorLens = data.decomposition_lenses?.default_lens || factorLenses[0]?.lens_id;
  let selectedFactorSeries = null;
  const currentFactorAnalysis = () => factorLenses.find((item) => item.lens_id === selectedFactorLens) || factorLenses[0];

  function replicationAnalysisMarkup(seriesId) {
    if (selectedFactorLens !== "DYNAMIC_STYLE") return "";
    const row = replicationRows.find((item) => item.series_id === seriesId);
    if (!row) return `<div class="callout callout-danger"><strong>Tracker state:</strong> No governed replication disposition exists for ${safe(seriesId)}.</div>`;
    if (!row.metrics) return `<div class="callout callout-warning"><strong>Out-of-sample tracker:</strong> ${statusText(row.availability_status)} · ${safe((row.blocking_reasons || []).map(humanStatus).join(" · ") || "No selected dynamic model")}. No return, turnover, or cost value is imputed.</div>`;
    const metrics = row.metrics;
    const policy = row.investability_policy || {};
    return `<div class="panel span-12"><div class="panel-heading"><div><div class="eyebrow">Forward-only research tracker</div><h3>Sealed-holdout replication</h3></div><span class="badge ${row.availability_status === "AVAILABLE_RESEARCH_REPLICATION" ? "badge-positive" : "badge-warning"}">${statusText(row.availability_status)}</span></div><div class="analytics-detail-grid"><div class="metric-block"><div class="metric-label">Actual cumulative</div><div class="metric-value">${pct(metrics.cumulative_actual_excess_return)}</div><div class="metric-note">${safe(row.holdout_start)}–${safe(row.holdout_end)}</div></div><div class="metric-block"><div class="metric-label">Net tracker cumulative</div><div class="metric-value">${pct(metrics.cumulative_net_tracker_excess_return)}</div><div class="metric-note">Gross ${pct(metrics.cumulative_gross_tracker_excess_return)}</div></div><div class="metric-block"><div class="metric-label">Tracking error</div><div class="metric-value">${pct(metrics.annualized_tracking_error)}</div><div class="metric-note">${integer.format(metrics.observation_count)} causal holdout observations</div></div><div class="metric-block"><div class="metric-label">Residual alpha</div><div class="metric-value">${pct(metrics.annualized_descriptive_residual_alpha)}</div><div class="metric-note">Descriptive; no confidence claim</div></div><div class="metric-block"><div class="metric-label">Relative drawdown</div><div class="metric-value">${pct(metrics.max_relative_wealth_ratio_drawdown_loss)}</div><div class="metric-note">Actual versus net tracker wealth ratio</div></div><div class="metric-block"><div class="metric-label">Turnover / cost</div><div class="metric-value">${decimal(metrics.total_turnover)}×</div><div class="metric-note">${safe(policy.selected_cost_bps_per_one_way_turnover)} bps · ${pct(metrics.cumulative_cost_drag)} drag</div></div></div><div class="callout callout-neutral"><strong>Authority boundary:</strong> Research diagnostic only. Parameters are selected on training evidence, holdout recalibration is NONE, and the tracker has no readiness, allocation, HWM, execution, or order authority.</div></div>`;
  }

  function dynamicAnalysisMarkup(row) {
    const dynamic = row.diagnostics?.dynamic;
    const path = dynamic?.exposure_path || [];
    if (row.estimator !== "DYNAMIC_FLS" || path.length < 2) return replicationAnalysisMarkup(row.series_id);
    const factorIds = [...new Set(path.flatMap((item) => Object.keys(item.exposures || {})))];
    if (!factorIds.length) return replicationAnalysisMarkup(row.series_id);
    const colors = ["#56d6c9", "#7aa2f7", "#f4b860", "#d98cff", "#71d68d", "#ff7b8b"];
    const width = 920;
    const height = 280;
    const left = 54;
    const right = 18;
    const top = 18;
    const bottom = 34;
    const values = path.flatMap((item) => factorIds.map((factor) => Number(item.exposures?.[factor])).filter(Number.isFinite));
    const minimum = Math.min(0, ...values);
    const maximum = Math.max(0, ...values);
    const span = Math.max(maximum - minimum, 1e-9);
    const x = (index) => left + index / Math.max(path.length - 1, 1) * (width - left - right);
    const y = (value) => top + (maximum - value) / span * (height - top - bottom);
    const lines = factorIds.map((factor, index) => {
      const points = path.map((item, pointIndex) => `${x(pointIndex).toFixed(2)},${y(Number(item.exposures?.[factor] || 0)).toFixed(2)}`).join(" ");
      return `<polyline fill="none" stroke="${colors[index % colors.length]}" stroke-width="2" vector-effect="non-scaling-stroke" points="${points}"></polyline>`;
    }).join("");
    const holdoutIndex = path.findIndex((item) => String(item.estimation_state).startsWith("HOLDOUT"));
    const holdout = holdoutIndex >= 0 ? `<line class="holdout-marker" x1="${x(holdoutIndex)}" x2="${x(holdoutIndex)}" y1="${top}" y2="${height - bottom}"></line><text class="chart-label" x="${Math.min(x(holdoutIndex) + 6, width - 110)}" y="${top + 14}">sealed holdout</text>` : "";
    const legend = factorIds.map((factor, index) => `<span><i style="background:${colors[index % colors.length]}"></i>${safe(factor)}</span>`).join("");
    const comparison = dynamic.holdout_comparison || {};
    return `<div class="callout callout-neutral"><strong>Method boundary:</strong> Clean-room dynamic FLS is MPI-informed and non-equivalent. It implements only the openly described methodology and does not claim proprietary MPI production output.</div><div class="grid grid-12 dynamic-analysis-grid"><div class="panel span-8"><div class="panel-heading"><div><div class="eyebrow">Effective-dated factor exposures</div><h3>${safe(row.series_id)}</h3></div><span class="badge badge-positive">${safe(dynamic.validation || "TRAIN_ONLY_SELECTION")}</span></div><svg class="dynamic-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Dynamic factor exposure path for ${safe(row.series_id)}"><line class="zero-line" x1="${left}" x2="${width - right}" y1="${y(0)}" y2="${y(0)}"></line>${holdout}${lines}<text class="chart-label" x="${left}" y="${height - 10}">${safe(path[0].date)}</text><text class="chart-label" text-anchor="end" x="${width - right}" y="${height - 10}">${safe(path[path.length - 1].date)}</text><text class="chart-label" x="6" y="${top + 10}">${decimal(maximum)}</text><text class="chart-label" x="6" y="${height - bottom}">${decimal(minimum)}</text></svg><div class="chart-legend">${legend}<span><i style="background:var(--warning)"></i>Holdout boundary</span></div></div><div class="panel span-4"><div class="panel-heading"><div><div class="eyebrow">Dynamic validation</div><h3>Same-factor static benchmark</h3></div></div><div class="metric-list"><div><span>Selected smoothness</span><strong>${safe(dynamic.selected_smoothness || "—")}</strong></div><div><span>Train-only selection R²</span><strong>${pct(dynamic.selection_predicted_r_squared)}</strong></div><div><span>Dynamic holdout R²</span><strong>${pct(comparison.dynamic_predicted_r_squared)}</strong></div><div><span>Static holdout R²</span><strong>${pct(comparison.static_predicted_r_squared)}</strong></div><div><span>Dynamic minus static</span><strong>${pct(comparison.dynamic_minus_static_predicted_r_squared)}</strong></div><div><span>Comparison state</span><strong>${safe(humanStatus(comparison.status || "UNAVAILABLE"))}</strong></div></div></div>${replicationAnalysisMarkup(row.series_id)}</div>`;
  }

  function renderFactorDetail() {
    const analysis = currentFactorAnalysis();
    const row = analysis.series.find((item) => item.series_id === selectedFactorSeries) || analysis.series[0];
    if (!row) return;
    selectedFactorSeries = row.series_id;
    $$("[data-factor-strategy]").forEach((button) => button.classList.toggle("is-active", button.dataset.factorStrategy === selectedFactorSeries));
    if (row.status !== "AVAILABLE") {
      const aligned = row.coverage?.aligned_observations ?? 0;
      if (row.status === "DIAGNOSTIC_ONLY") {
        const holdout = row.diagnostics?.holdout;
        const design = row.diagnostics?.training_design || {};
        const maximumVif = Math.max(...(design.vif || []).map((item) => Number(item.value)).filter(Number.isFinite), 0);
        const inference = row.diagnostics?.inference || {};
        const reason = row.model_quality?.gate_reason || (row.unavailable_reasons || []).join(" · ");
        $("#factor-detail").innerHTML = `<div class="state-separation"><div><span class="eyebrow">Source state</span><strong>${safe(humanStatus(sourceState(row)))}</strong><p>${integer.format(aligned)} aligned observations are retained.</p></div><div><span class="eyebrow">Model state</span><strong>${safe(humanStatus(modelState(row)))}</strong><p>${safe(humanStatus(reason))}. Sealed-holdout predicted R²: ${pct(holdout?.predicted_r_squared)} across ${integer.format(holdout?.observation_count || 0)} observations.</p></div></div><div class="callout callout-warning">The fit and dynamic path remain available for forensic review, but no alpha or exposure is accepted for portfolio-manager conclusions.</div><div class="callout callout-neutral"><strong>Training design:</strong> n=${integer.format(design.design_observations || 0)} · rank ${safe(design.matrix_rank ?? "—")}/${safe(design.factor_count ?? "—")} · condition ${decimal(design.matrix_condition_number)} · max VIF ${decimal(maximumVif)} · ${statusText(design.unregularized_status || "UNAVAILABLE")}. <strong>Inference:</strong> ${statusText(inference.status || "UNAVAILABLE")}.</div>${dynamicAnalysisMarkup(row)}`;
        return;
      }
      $("#factor-detail").innerHTML = `<div class="state-separation"><div><span class="eyebrow">Source state</span><strong>${safe(humanStatus(sourceState(row)))}</strong><p>Provider or account return evidence is assessed independently of this factor universe.</p></div><div><span class="eyebrow">Model state</span><strong>${safe(humanStatus(modelState(row)))}</strong><p>${safe((row.unavailable_reasons || []).map(humanStatus).join(" · ") || "No accepted model")}. Aligned model observations: ${integer.format(aligned)}.</p></div></div><div class="callout callout-danger">This is a model or decomposition-universe limitation. It does not relabel accepted Volatis, Robuxio, or other provider/account source data as missing. No exposure or alpha is inferred.</div>${replicationAnalysisMarkup(row.series_id)}`;
      return;
    }
    const alpha = row.alpha;
    const holdout = row.diagnostics.holdout;
    const inference = row.diagnostics.inference || {};
    const trainResiduals = row.diagnostics.residuals?.train || {};
    const design = row.diagnostics.training_design || {};
    const maximumVif = Math.max(...(design.vif || []).map((item) => Number(item.value)).filter(Number.isFinite), 0);
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
    const methodBoundary = row.estimator === "DYNAMIC_FLS"
      ? "Dynamic exposures are effective-dated, selected on training evidence, and evaluated on a sealed holdout."
      : "Static exposures are an independent returns-based lens and are not concatenated with dynamic, sector, or industry models.";
    const proxyRows = (analysis.proxy_governance?.proxies || []).map((proxy) => `<tr><td><span class="cell-primary">${safe(proxy.factor_id)}</span><span class="cell-secondary">${safe(proxy.economic_family || "UNCLASSIFIED")}</span></td><td>${safe(proxy.source_id || "UNAVAILABLE")}</td><td>${safe(proxy.effective_start || "—")}<span class="cell-secondary">${safe(proxy.effective_end || "—")}</span></td><td>${statusText(proxy.candidate_disposition || "UNAVAILABLE")}</td><td>${safe(proxy.selection_rationale || "No governed rationale")}</td></tr>`);
    const proxyTable = proxyRows.length
      ? `<div class="panel span-12"><div class="panel-heading"><div><div class="eyebrow">Governed candidate universe</div><h3>Economic rationale and train-only disposition</h3></div><span class="badge">${integer.format(proxyRows.length)} proxies</span></div><div class="table-wrap wide"><table><caption>Factor proxy governance</caption>${tableMarkup([["Proxy / family"], ["Source"], ["Effective interval"], ["Disposition"], ["Selection rationale"]], proxyRows)}</table></div></div>`
      : "";
    $("#factor-detail").innerHTML = `<div class="analytics-detail-grid"><div class="metric-block"><div class="metric-label">Estimator</div><div class="metric-value metric-value-compact">${statusText(row.estimator)}</div><div class="metric-note">${safe(analysis.method_label)}</div></div><div class="metric-block"><div class="metric-label">Net alpha</div><div class="metric-value">${pct(alpha.net_annualized)}</div><div class="metric-note">${safe(alpha.net_status)} · observed basis</div></div><div class="metric-block"><div class="metric-label">Gross alpha</div><div class="metric-value">${pct(alpha.gross_annualized)}</div><div class="metric-note">${safe(alpha.gross_status)}</div></div><div class="metric-block"><div class="metric-label">Holdout predicted R²</div><div class="metric-value">${pct(holdout.predicted_r_squared)}</div><div class="metric-note">${integer.format(holdout.observation_count)} out-of-sample observations</div></div><div class="metric-block"><div class="metric-label">Tracking error</div><div class="metric-value">${pct(holdout.tracking_error_annualized)}</div><div class="metric-note">Annualized holdout residual volatility</div></div><div class="metric-block"><div class="metric-label">Exact-date coverage</div><div class="metric-value">${integer.format(row.coverage.aligned_observations)}</div><div class="metric-note">${formatDate(row.coverage.observed_start)}–${formatDate(row.coverage.observed_end)}</div></div><div class="metric-block"><div class="metric-label">Training design</div><div class="metric-value">${integer.format(design.design_observations || 0)} obs.</div><div class="metric-note">Rank ${safe(design.matrix_rank ?? "—")}/${safe(design.factor_count ?? "—")} · condition ${decimal(design.matrix_condition_number)} · max VIF ${decimal(maximumVif)} · ${safe(humanStatus(design.unregularized_status || "UNAVAILABLE"))}</div></div><div class="metric-block"><div class="metric-label">HAC inference state</div><div class="metric-value metric-value-compact">${statusText(inference.status || "UNAVAILABLE")}</div><div class="metric-note">Default lag ${safe(inference.default_lag ?? "—")} · no p-values or confidence intervals</div></div><div class="metric-block"><div class="metric-label">Train residual diagnostics</div><div class="metric-value">DW ${decimal(trainResiduals.durbin_watson)}</div><div class="metric-note">Lag-1 residual autocorrelation ${decimal(trainResiduals.autocorrelation?.["1"])}</div></div><div class="metric-block"><div class="metric-label">Reconciliation error</div><div class="metric-value">${safe(summary.maximum_reconciliation_error)}</div><div class="metric-note">Maximum absolute return identity error</div></div></div><div class="callout callout-neutral"><strong>Model boundary:</strong> ${safe(methodBoundary)} ${safe(inference.post_selection_caveat || "")}</div><div class="grid grid-12" style="margin-top:16px"><div class="panel span-7"><div class="panel-heading"><div><div class="eyebrow">Latest accepted exposures</div><h3>${safe(row.series_id)}</h3></div><span class="badge badge-positive">${safe(row.evidence_layer)}</span></div><div class="exposure-list">${exposures}</div></div><div class="panel span-5"><div class="panel-heading"><div><div class="eyebrow">Holdout mean partition</div><h3>Realized = fitted + residual</h3></div><span class="badge">n=${integer.format(summary.observations)}</span></div><div class="table-wrap"><table><caption>Holdout factor contribution reconciliation</caption>${tableMarkup([["Component"], ["Role"], ["Mean", "numeric"]], contributionRows)}</table></div></div>${proxyTable}</div>${dynamicAnalysisMarkup(row)}`;
  }

  function renderAttribution() {
    const analysis = currentFactorAnalysis();
    const firstAvailable = analysis.series.find((row) => row.status === "AVAILABLE");
    if (selectedContextStrategy !== "ALL" && analysis.series.some((row) => row.series_id === selectedContextStrategy)) selectedFactorSeries = selectedContextStrategy;
    if (!analysis.series.some((row) => row.series_id === selectedFactorSeries)) selectedFactorSeries = null;
    selectedFactorSeries ||= firstAvailable?.series_id || analysis.series[0]?.series_id;
    $("#factor-lens-selector").innerHTML = factorLenses.map((item) => `<button type="button" class="segment ${item.lens_id === selectedFactorLens ? "is-active" : ""}" data-factor-lens="${safe(item.lens_id)}" aria-pressed="${item.lens_id === selectedFactorLens}">${safe(item.lens_label || item.lens_id)}</button>`).join("");
    $$('[data-factor-lens]').forEach((button) => button.addEventListener("click", () => { selectedFactorLens = button.dataset.factorLens; selectedFactorSeries = null; renderAttribution(); }));
    const sourceRows = Array.isArray(analysis.sources) ? analysis.sources : [];
    const sourceSummary = sourceRows.map((item) => `${item.source_label || item.source_key} · through ${item.as_of_date}`).join(" · ") || "Single accepted historical proof generation";
    const proxyGovernance = analysis.proxy_governance || {};
    $("#factor-lens-boundary").innerHTML = `<strong>${safe(analysis.lens_label || analysis.lens_id)}</strong> · ${safe(analysis.return_basis || "CONFIGURED_RETURN_BASIS")}<br>${safe(sourceSummary)}. ${integer.format(proxyGovernance.candidate_count || 0)} governed candidates; ${safe(humanStatus(proxyGovernance.orthogonality_claim || "UNAVAILABLE"))}. Lenses remain separate and are never concatenated.`;
    renderKpis("#factor-kpis", [
      ["PM-usable models", `${analysis.available_count}/${analysis.series.length}`, "Positive sealed-holdout predicted R² required", analysis.available_count ? "positive" : "danger"],
      ["Diagnostic only", String(analysis.diagnostic_count || 0), "Computed fits excluded from alpha and exposure conclusions", "warning"],
      ["Unavailable", String(analysis.unavailable_count), "No inferred exposures or alpha", "danger"],
      ["Methodology", analysis.methodology_version, `${analysis.method_label} · ${analysis.lens_label || analysis.lens_id}`, "warning"],
    ]);
    $("#factor-method-badge").textContent = analysis.method_label.replaceAll("_", " ");
    const rows = analysis.series.map((row) => {
      const holdout = row.diagnostics?.holdout;
      const strategy = strategyByName(row.series_id);
      const decision = row.model_quality?.decision_status || row.status;
      const providerCoverage = strategy?.provider_evidence ? `<span class="cell-secondary">${safe(evidenceCoverage(strategy))}</span>` : "";
      return `<tr><td><span class="cell-primary">${safe(row.series_id)}</span><span class="cell-secondary">${safe(row.model_id || "NO ACCEPTED MODEL")}</span></td><td>${statusText(sourceState(row))}${providerCoverage}</td><td>${statusText(modelState(row))}<span class="cell-secondary">${statusText(decision)}</span></td><td>${safe(row.source_label || "—")}</td><td>${statusText(row.estimator || "UNAVAILABLE")}</td><td>${safe(row.exposures.map((item) => item.factor_id).join(" · ") || "—")}</td><td>${safe(row.coverage?.observed_start || "—")}<span class="cell-secondary">${safe(row.coverage?.observed_end || "—")} · model input</span></td><td class="numeric">${row.coverage ? integer.format(row.coverage.aligned_observations) : "—"}</td><td class="numeric">${pct(holdout?.predicted_r_squared)}</td><td class="numeric">${pct(row.alpha?.net_annualized)}</td><td>${statusText(row.alpha?.gross_status || row.model_quality?.gate_reason || (row.unavailable_reasons || []).join(" · "))}</td></tr>`;
    });
    $("#factor-model-table").innerHTML = tableMarkup([["Strategy / model"], ["Source returns"], ["Model state"], ["Model input source"], ["Estimator"], ["Accepted factors"], ["Observed interval"], ["Aligned", "numeric"], ["Sealed holdout R²", "numeric"], ["Accepted net alpha", "numeric"], ["Alpha / gate state"]], rows);
    $("#factor-strategy-selector").innerHTML = analysis.series.map((row) => `<button type="button" class="strategy-chip ${row.status === "AVAILABLE" ? "" : row.status === "DIAGNOSTIC_ONLY" ? "is-diagnostic" : "is-unavailable"}" data-factor-strategy="${safe(row.series_id)}">${safe(row.series_id)}</button>`).join("");
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
  let selectedCorrelationWindow = "full";
  let selectedPair = null;
  const dependenceModes = data.dependence_modes || { LIVE_ONLY: data.dependence_analysis };
  let selectedEvidenceMode = dependenceModes[data.default_dependence_mode] ? data.default_dependence_mode : "LIVE_ONLY";
  const currentDependence = () => dependenceModes[selectedEvidenceMode] || data.dependence_analysis;
  const shortSeries = (value) => String(value).replace(" Portfolio", "").replace(" Strategy", "").replace(" Factor [P123]", " P123").replace("GrizzlyBears VTMA", "Grizzly").replace("Robuxio Crypto (High Sharpe High Vol)", "Robuxio").replace("[Composer] - The Holy Grail (Buy Copy)", "Composer").replace("Growth at Market Pivot Points [EchoTrade]", "EchoTrade");
  const pairKey = (left, right) => [left, right].sort().join("\u0000");

  function renderCorrelationDetail(left, right) {
    const detail = $("#correlation-detail");
    if (left === right) {
      $("#correlation-detail-title").textContent = shortSeries(left);
      detail.innerHTML = `<div class="callout callout-neutral">Diagonal values are 1.0 by definition. Select a non-diagonal cell to inspect pair evidence.</div>`;
      return;
    }
    const dependence = currentDependence();
    const cell = dependence.cells.find((item) => pairKey(item.left, item.right) === pairKey(left, right));
    if (!cell) return;
    const observed = cell.observed;
    const coverage = observed.coverage;
    const blend = cell.blend;
    const implementation = cell.implementation;
    const horizon = observed.horizons?.[selectedCorrelationWindow];
    const drawdown = selectedCorrelationWindow === "either_drawdown"
      ? observed.drawdown_regimes?.either_strategy_underwater
      : selectedCorrelationWindow === "both_drawdown"
        ? observed.drawdown_regimes?.both_strategies_underwater
        : null;
    const selectedPeriod = horizon || drawdown;
    const composition = (series) => {
      const item = dependence.series_composition?.[series];
      if (!item) return "";
      return `${item.strategy_version} · live inception ${item.live_inception || "—"} · ${integer.format(item.live_observations || 0)} live · ${integer.format(item.backtest_observations_used || 0)} backtest used · ${integer.format(item.backtest_observations_excluded_at_or_after_live_inception || 0)} overlapping backtest excluded`;
    };
    const compositionBlock = dependence.series_composition?.[left] && dependence.series_composition?.[right]
      ? `<div class="callout callout-neutral"><strong>${safe(shortSeries(left))}</strong>: ${safe(composition(left))}<br /><strong>${safe(shortSeries(right))}</strong>: ${safe(composition(right))}</div>`
      : "";
    $("#correlation-detail-title").textContent = `${shortSeries(left)} × ${shortSeries(right)}`;
    detail.innerHTML = `<div class="validation-card"><div><strong>Observed status</strong><span>${safe(coverage.evidence_layer || "No compatible measured layer")}</span></div><div class="validation-score">${statusText(observed.status)}</div></div><div class="evidence-grid"><dl class="detail-list"><div><dt>Full-overlap Pearson</dt><dd>${decimal(observed.pearson)}</dd></div><div><dt>Spearman</dt><dd>${decimal(observed.spearman)}</dd></div><div><dt>Selected period</dt><dd>${decimal(selectedPeriod?.correlation ?? observed.pearson)}</dd></div><div><dt>Selected sample</dt><dd>${integer.format(selectedPeriod?.observations ?? coverage.common_observations)}</dd></div><div><dt>Selected interval</dt><dd>${safe(selectedPeriod?.observed_start || coverage.observed_start || "—")}–${safe(selectedPeriod?.observed_end || coverage.observed_end || "—")}</dd></div><div><dt>Rolling window</dt><dd>${integer.format(observed.rolling_pearson.window_observations || 0)} observations · ${decimal(observed.rolling_pearson.latest)}</dd></div></dl><dl class="detail-list"><div><dt>Calendar</dt><dd>${safe(coverage.frequency)}</dd></div><div><dt>Missing left / right</dt><dd>${integer.format(coverage.missing_from_left)} / ${integer.format(coverage.missing_from_right)}</dd></div><div><dt>Either in drawdown</dt><dd>${decimal(observed.drawdown_regimes?.either_strategy_underwater?.correlation)} · ${integer.format(observed.drawdown_regimes?.either_strategy_underwater?.observations || 0)} obs.</dd></div><div><dt>Both in drawdown</dt><dd>${decimal(observed.drawdown_regimes?.both_strategies_underwater?.correlation)} · ${integer.format(observed.drawdown_regimes?.both_strategies_underwater?.observations || 0)} obs.</dd></div><div><dt>Conceptual prior</dt><dd>${decimal(blend.conceptual_prior)}</dd></div><div><dt>Implementation mapping</dt><dd>${statusText(implementation.status)}</dd></div></dl></div>${compositionBlock}<div class="callout ${observed.status === "AVAILABLE" ? "callout-neutral" : "callout-danger"}">${statusText(blend.decision_status)}. ${safe(blend.conceptual_prior_note)}</div>`;
  }

  function renderCorrelationMatrix() {
    const dependence = currentDependence();
    let matrix = dependence.matrices[selectedMatrix];
    const series = dependence.series_ids;
    let label = matrixLabels[selectedMatrix];
    if (selectedMatrix === "observed_pearson" && selectedCorrelationWindow !== "full") {
      if (dependence.horizon_matrices[selectedCorrelationWindow]) {
        matrix = dependence.horizon_matrices[selectedCorrelationWindow];
        label = [`Observed Pearson · ${selectedCorrelationWindow} sessions`, "Trailing accepted weekday observations · no holiday inference"];
      } else if (selectedCorrelationWindow === "either_drawdown") {
        matrix = dependence.drawdown_matrices.either_strategy_underwater;
        label = ["Correlation · either strategy in drawdown", "Conditioned on either wealth index below its prior running HWM"];
      } else {
        matrix = dependence.drawdown_matrices.both_strategies_underwater;
        label = ["Correlation · both strategies in drawdown", "Conditioned on both wealth indices below their prior running HWM"];
      }
    }
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
    const dependence = currentDependence();
    const corrOne = data.dependence_analysis.correlation_one;
    const clusterBudget = data.dependence_analysis.economic_driver_cluster_budget;
    const unavailable = dependence.pair_count - dependence.available_pair_count;
    const combined = selectedEvidenceMode === "LIVE_PLUS_BACKTEST";
    const modeBadge = $("#evidence-mode-badge");
    modeBadge.textContent = combined ? "HYBRID BACKTEST + LIVE" : "LIVE ONLY · AUTHORITY";
    modeBadge.classList.toggle("badge-warning", combined);
    modeBadge.classList.toggle("badge-positive", !combined);
    $("#evidence-mode-boundary").textContent = combined
      ? "Exploratory hybrid view: only version-matched backtest observations before live inception are included. Live observations win from inception onward. This mode cannot change readiness, allocation, or risk-control authority."
      : "Live-only evidence is the authoritative default for readiness, allocation, and risk controls.";
    $$('[data-evidence-mode]').forEach((button) => {
      const available = Boolean(dependenceModes[button.dataset.evidenceMode]);
      button.disabled = !available;
      const active = button.dataset.evidenceMode === selectedEvidenceMode;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
      if (!available) button.title = "This accepted correlation generation has no backtest mode";
    });
    renderKpis("#dependence-kpis", [
      ["Measured pairs", `${dependence.available_pair_count}/${dependence.pair_count}`, combined ? "Version-matched pre-live backtest plus authoritative live" : "Exact-date live observations", "positive"],
      ["Unavailable pairs", String(unavailable), "No inferred diversification credit", "danger"],
      ["Correlation-one loss", pct(corrOne.loss), `${pct(clusterBudget.limit_breach)} above the ${pct(clusterBudget.systematic_loss_limit)} systematic limit`, "danger"],
      ["Analytics as of", formatDate(dependence.as_of_date), `Generation ${dependence.generation_id.slice(0, 12)}…`],
    ]);
    selectedPair ||= pairKey(dependence.cells.find((item) => item.observed.status === "AVAILABLE")?.left || dependence.series_ids[0], dependence.cells.find((item) => item.observed.status === "AVAILABLE")?.right || dependence.series_ids[1]);
    renderCorrelationMatrix();
    const selected = dependence.cells.find((item) => pairKey(item.left, item.right) === selectedPair) || dependence.cells[0];
    renderCorrelationDetail(selected.left, selected.right);
    $("#correlation-one-summary").innerHTML = `<div class="metric-block"><div class="metric-label">Loss</div><div class="metric-value">${pct(corrOne.loss)}</div><div class="metric-note">Weighted component adverse losses</div></div><div class="metric-block"><div class="metric-label">Systematic limit</div><div class="metric-value">${pct(clusterBudget.systematic_loss_limit)}</div><div class="metric-note">Breach ${pct(clusterBudget.limit_breach)}</div></div><div class="metric-block"><div class="metric-label">Expected</div><div class="metric-value">${pct(corrOne.expected_loss)}</div><div class="metric-note">Accepted baseline reference</div></div><div class="metric-block"><div class="metric-label">Reconciliation</div><div class="metric-value">${safe(corrOne.reconciliation_difference)}</div><div class="metric-note">${safe(corrOne.status)}</div></div>`;
    const componentRows = corrOne.components.map((row) => `<tr><td>${safe(row.series_id)}</td><td class="numeric">${pct(row.allocation_weight)}</td><td class="numeric">${pct(row.adverse_loss_fraction)}</td><td class="numeric">${pct(row.loss_contribution)}</td></tr>`);
    $("#correlation-one-table").innerHTML = tableMarkup([["Strategy"], ["Weight", "numeric"], ["Adverse loss", "numeric"], ["Contribution", "numeric"]], componentRows.length ? componentRows : [emptyRow(4, "No component detail in this accepted fixture")]);
    $("#economic-cluster-summary").innerHTML = `<strong>${statusText(clusterBudget.status)}</strong> · ${safe(humanStatus(clusterBudget.budget_rule))}. The frozen audit reference was ${pct(clusterBudget.historical_audit_reference.full_systematic_loss)} and ${pct(clusterBudget.historical_audit_reference.after_rejected_sleeves_loss)} after rejected sleeves; the operative control above is recomputed from current accepted components.`;
    const clusterRows = clusterBudget.clusters.map((cluster) => `<tr><td><span class="cell-primary">${safe(cluster.label)}</span><span class="cell-secondary">${safe(cluster.cluster_id)}</span></td><td>${safe(cluster.members.map(shortSeries).join(" · "))}</td><td class="numeric">${pct(cluster.allocation_weight)}</td><td class="numeric">${pct(cluster.loss_contribution)}</td><td>${cluster.observed_pair_correlation_minimum == null ? "—" : `${decimal(cluster.observed_pair_correlation_minimum)}–${decimal(cluster.observed_pair_correlation_maximum)}`}</td><td>${statusText(cluster.budget_status)}<span class="cell-secondary">${safe(cluster.rationale)}</span></td></tr>`);
    $("#economic-cluster-table").innerHTML = tableMarkup([["Driver cluster"], ["Members"], ["Aggregate weight", "numeric"], ["Adverse-loss budget", "numeric"], ["Observed pair range"], ["Budget state / rationale"]], clusterRows);
  }

  const weekly = data.weekly_returns;
  const weeklyGroupLabels = {
    ALL: "All",
    STRATEGY: "Strategies",
    ECONOMIC_FACTOR_US: "U.S. factors",
    ECONOMIC_FACTOR_EUROPE: "Europe factors",
    STYLE_US_EQUITY: "Equity style",
    STYLE_MULTI_ASSET: "Multi-asset style",
    SECTOR: "Sectors",
    INDUSTRY: "Industries",
    CORE_BENCHMARK: "Benchmarks",
  };
  let selectedWeeklyMode = weekly?.default_evidence_mode || "LIVE_ONLY";
  let selectedWeeklyGroup = "STRATEGY";
  let selectedWeeklyWindow = "13";
  let weeklyPage = 0;
  let weeklySearch = "";
  let includeCurrentWeek = false;
  let selectedWeeklyCell = null;

  function weeklyValue(row, week) {
    if (row.primary_group === "STRATEGY") return row.values_by_mode?.[selectedWeeklyMode]?.[week] ?? null;
    return row.values?.[week] ?? null;
  }

  function weeklyMeta(row, week) {
    const raw = row.primary_group === "STRATEGY" ? row.observation_meta_by_mode?.[selectedWeeklyMode]?.[week] : row.observation_meta?.[week];
    if (!Array.isArray(raw) || raw.length !== 5) return null;
    return {
      dailyCount: raw[0],
      actualStart: raw[1],
      actualEnd: raw[2],
      coverageStatus: raw[3],
      boundaryReasons: raw[4] || [],
      calendarBasis: row.primary_group === "STRATEGY" ? row.calendar_basis_by_mode?.[selectedWeeklyMode] : row.calendar_basis,
    };
  }

  function weeklyEnd(week) {
    const value = new Date(`${week}T00:00:00Z`);
    value.setUTCDate(value.getUTCDate() + 6);
    return value.toISOString().slice(0, 10);
  }

  function weeklyVisibleWeeks() {
    const all = (weekly?.weeks || []).filter((week) => includeCurrentWeek || week !== weekly.current_week?.week_start);
    if (selectedWeeklyWindow !== "full") return all.slice(-Number(selectedWeeklyWindow));
    const pageSize = 52;
    const end = Math.max(0, all.length - weeklyPage * pageSize);
    const start = Math.max(0, end - pageSize);
    return all.slice(start, end);
  }

  function renderWeeklyDetail() {
    if (!selectedWeeklyCell) {
      $("#weekly-detail-title").textContent = "Choose a weekly cell";
      $("#weekly-detail").innerHTML = `<div class="callout callout-neutral">A selected cell discloses its canonical series, source, return basis, evidence mode, week boundary, and explicit availability state.</div>`;
      return;
    }
    const row = weekly.series.find((item) => item.series_id === selectedWeeklyCell.seriesId);
    if (!row) return;
    const value = weeklyValue(row, selectedWeeklyCell.week);
    const meta = weeklyMeta(row, selectedWeeklyCell.week);
    $("#weekly-detail-title").textContent = `${row.display_name} · week of ${selectedWeeklyCell.week}`;
    $("#weekly-detail").innerHTML = `<div class="validation-card"><div><strong>Weekly return</strong><span>${value == null ? "No accepted observation in this week" : "Compounded from accepted daily returns"}</span></div><div class="validation-score">${value == null ? "UNAVAILABLE" : pct(value, 2)}</div></div><div class="evidence-grid"><dl class="detail-list"><div><dt>Canonical series</dt><dd>${safe(row.series_id)}</dd></div><div><dt>Group</dt><dd>${safe(weeklyGroupLabels[row.primary_group] || row.primary_group)}</dd></div><div><dt>Return basis</dt><dd>${statusText(row.return_basis)}</dd></div><div><dt>Calendar week</dt><dd>${safe(selectedWeeklyCell.week)} — ${safe(weeklyEnd(selectedWeeklyCell.week))}</dd></div></dl><dl class="detail-list"><div><dt>Source</dt><dd>${safe(row.source_key)}</dd></div><div><dt>Evidence mode</dt><dd>${row.primary_group === "STRATEGY" ? statusText(selectedWeeklyMode) : "NOT APPLICABLE"}</dd></div><div><dt>Accepted observations</dt><dd>${meta ? `${meta.dailyCount} · ${safe(meta.actualStart)} — ${safe(meta.actualEnd)}` : "UNAVAILABLE"}</dd></div><div><dt>Coverage / calendar</dt><dd>${meta ? `${statusText(meta.coverageStatus)} · ${statusText(meta.calendarBasis)}${meta.boundaryReasons.length ? ` · ${meta.boundaryReasons.map(statusText).join(", ")}` : ""}` : statusText(row.absent_value_status)}</dd></div></dl></div>`;
  }

  function renderWeeklyTable() {
    if (!weekly?.series?.length) {
      $("#weekly-table").innerHTML = `<div class="callout callout-danger">Weekly-return generation is unavailable in this snapshot.</div>`;
      renderKpis("#weekly-kpis", [["Status", "UNAVAILABLE", "No governed weekly generation", "danger"]]);
      return;
    }
    const query = weeklySearch.toLowerCase();
    const rows = weekly.series.filter((row) => (selectedWeeklyGroup === "ALL" || row.display_groups.includes(selectedWeeklyGroup)) && (!query || `${row.display_name} ${row.series_id} ${row.source_key}`.toLowerCase().includes(query)));
    const weeks = weeklyVisibleWeeks();
    const missingCells = rows.reduce((count, row) => count + weeks.filter((week) => weeklyValue(row, week) == null).length, 0);
    renderKpis("#weekly-kpis", [
      ["Visible series", String(rows.length), weeklyGroupLabels[selectedWeeklyGroup] || selectedWeeklyGroup],
      ["Displayed weeks", String(weeks.length), selectedWeeklyWindow === "full" ? `Full-history page ${weeklyPage + 1}` : `Latest ${selectedWeeklyWindow}`],
      ["Unavailable cells", String(missingCells), "Missing is never zero", missingCells ? "warning" : "positive"],
      ["Strategy evidence", selectedWeeklyMode, selectedWeeklyMode === "LIVE_ONLY" ? "Authoritative default" : "Version-matched pre-live backtest", selectedWeeklyMode === "LIVE_ONLY" ? "positive" : "warning"],
    ]);
    const header = `<thead><tr><th scope="col">Series / basis</th>${weeks.map((week) => `<th scope="col"><span class="weekly-date">${safe(week.slice(5))}</span><span class="cell-secondary">${safe(week.slice(0, 4))}</span></th>`).join("")}</tr></thead>`;
    const body = rows.map((row) => `<tr><th scope="row"><span class="cell-primary">${safe(row.display_name)}</span><span class="cell-secondary">${safe(row.return_basis)} · ${safe(row.source_key)}</span></th>${weeks.map((week) => {
      const value = weeklyValue(row, week);
      const meta = weeklyMeta(row, week);
      const numeric = value == null ? null : Number(value);
      const intensity = numeric == null ? 0 : Math.min(1, Math.abs(numeric) / 0.08);
      const partial = meta?.coverageStatus === "PARTIAL_BOUNDARY";
      return `<td><button type="button" class="weekly-cell ${numeric == null ? "is-unavailable" : numeric < 0 ? "is-negative" : "is-positive"} ${partial ? "is-partial" : ""}" style="--weekly-intensity:${intensity}" data-weekly-series="${safe(row.series_id)}" data-weekly-week="${safe(week)}" aria-label="${safe(row.display_name)} ${safe(week)} ${numeric == null ? "unavailable" : pct(value, 2)}${partial ? " partial boundary" : ""}">${numeric == null ? "—" : pct(value, 1)}</button></td>`;
    }).join("")}</tr>`).join("");
    $("#weekly-table").innerHTML = `<table class="weekly-table"><caption>Weekly returns; absent cells are unavailable, not zero</caption>${header}<tbody>${body || `<tr><td colspan="${weeks.length + 1}">No series match the current filter.</td></tr>`}</tbody></table>`;
    $("#weekly-table-title").textContent = selectedWeeklyWindow === "full" ? `Full history · 52-week page ${weeklyPage + 1}` : `Latest ${selectedWeeklyWindow} completed weeks`;
    $("#weekly-page-older").disabled = selectedWeeklyWindow !== "full" || weeks[0] === (weekly.weeks || [])[0];
    $("#weekly-page-newer").disabled = selectedWeeklyWindow !== "full" || weeklyPage === 0;
    $$('[data-weekly-series]').forEach((button) => button.addEventListener("click", () => {
      selectedWeeklyCell = { seriesId: button.dataset.weeklySeries, week: button.dataset.weeklyWeek };
      renderWeeklyDetail();
    }));
    renderWeeklyDetail();
  }

  function renderWeekly() {
    const groups = ["ALL", ...(weekly?.display_groups || [])];
    $("#weekly-group-selector").innerHTML = groups.map((group) => `<button type="button" class="segment ${group === selectedWeeklyGroup ? "is-active" : ""}" data-weekly-group="${safe(group)}" aria-pressed="${group === selectedWeeklyGroup}">${safe(weeklyGroupLabels[group] || group)}</button>`).join("");
    $$('[data-weekly-group]').forEach((button) => button.addEventListener("click", () => {
      selectedWeeklyGroup = button.dataset.weeklyGroup;
      weeklyPage = 0;
      selectedWeeklyCell = null;
      renderWeekly();
    }));
    $$('[data-weekly-mode]').forEach((button) => {
      const active = button.dataset.weeklyMode === selectedWeeklyMode;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    $$('[data-weekly-window]').forEach((button) => {
      const active = button.dataset.weeklyWindow === selectedWeeklyWindow;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    renderWeeklyTable();
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
    const factRows = data.coverage.filter((row) => row.category === "FACT_COVERAGE");
    const cleanFacts = factRows.filter((row) => row.status === "COMPLETE_WITHIN_DECLARED_BOUNDARY").length;
    const dispositionText = (value) => value && typeof value === "object" ? [value.code, value.effect].filter(Boolean).join(": ") : String(value || "Disposition not recorded");
<<<<<<< HEAD
    const observedWindow = (row) => {
      if (row.observed_start || row.observed_end) return `${formatDate(row.observed_start)}–${formatDate(row.observed_end)}`;
      if (row.category === "DATA_FRESHNESS") return `${row.last_accepted_date ? `Last accepted ${formatDate(row.last_accepted_date)}` : "No accepted observation"} · evaluated ${formatDate(row.evaluated_on)}`;
      return "Boundary not declared / no accepted observation";
=======
    const observedWindow = (row) => row.observed_start || row.observed_end ? `${formatDate(row.observed_start)}–${formatDate(row.observed_end)}` : "Boundary not declared / no accepted observation";
    const freshnessPolicy = (row) => {
      if (row.category !== "DATA_FRESHNESS") return "Not a recurring freshness assessment";
      if (row.freshness_mode === "FINITE") return `${row.policy_id} · finite history · reconciled ${formatDate(row.reconciled_on)}`;
      return `${row.policy_id} · ${humanStatus(row.freshness_calendar)} · every ${integer.format(row.expected_interval_days)} day(s) · grace ${integer.format(row.grace_days)} · stale after ${integer.format(row.stale_after_days)} · reconciled ${formatDate(row.reconciled_on)}`;
>>>>>>> b1537dc (feat(data): govern rollforward monitoring cycle)
    };
    renderKpis("#data-kpis", [
      ["Report generation", data.meta.report_generation_id.slice(0, 12), "Accepted local authority"],
      ["Baseline generation", data.meta.baseline_generation_id.slice(0, 12), "Hash-linked input"],
      ["Fact status", `${cleanFacts}/${factRows.length} clean`, `${factRows.length - cleanFacts} carry an explicit retained disposition`, cleanFacts === factRows.length ? "positive" : "warning"],
      ["External connections", "0", "Direct-file local operation", "reserve"],
    ]);
    const coverageRows = data.coverage.map((row) => `<tr id="coverage-${domToken(row.item)}" data-coverage-item="${safe(row.item)}" tabindex="-1"><th scope="row"><span class="cell-primary">${safe(row.item)}</span><span class="cell-secondary">${safe(row.category)}</span></th><td>${statusText(row.status)}${row.escalation_required ? '<span class="cell-secondary">Escalation required</span>' : ""}</td><td>${safe(observedWindow(row))}</td><td>${safe(freshnessPolicy(row))}</td><td>${safe(row.evidence_authority || "Evidence authority not assigned")}<span class="cell-secondary">${safe(row.resolution_authority || "Resolution authority not assigned")}</span></td><td>${safe(row.item === "full-systematic-hwm-unavailable" ? currencyExact(row.value) : row.value ?? "No accepted value")}</td><td>${safe(dispositionText(row.disposition))}</td><td>${safe(row.known_gap || "No known gap within the declared boundary")}</td></tr>`);
    $("#coverage-table").innerHTML = tableMarkup([["Fact / category"], ["Status / escalation"], ["Observation window / Freshness evaluated"], ["TTL / reconciled"], ["Evidence / resolution authority"], ["Value"], ["Disposition"], ["Known gap / missing interval"]], coverageRows);
    $("#validation-cards").innerHTML = `<div class="validation-card"><div><strong>Portfolio report</strong><span>${safe(data.meta.report_generation_id.slice(0, 12))}…</span></div><div class="validation-score">PUBLISHED</div></div><div class="validation-card"><div><strong>Factor / alpha</strong><span>${safe(data.meta.decomposition_generation_id.slice(0, 12))}…</span></div><div class="validation-score">PUBLISHED</div></div><div class="validation-card"><div><strong>Dependence</strong><span>${safe(data.meta.correlation_generation_id.slice(0, 12))}…</span></div><div class="validation-score">PUBLISHED</div></div><div class="validation-card"><div><strong>Frozen candidate appendix</strong><span>${safe(data.candidate_appendix.as_of_date)} · hash locked</span></div><div class="validation-score">FROZEN</div></div><div class="callout callout-neutral">Portfolio, analytics, and appendix generations retain separate authority. Candidate rows never enter current strategy totals.</div>`;
    const sourceRows = data.provenance.map((row) => `<tr><td><span class="cell-primary">${safe(row.artifact)}</span><span class="cell-secondary">${safe(row.schema)}</span></td><td>${formatDate(row.as_of_date)}</td><td><code>${safe(row.sha256.slice(0, 16))}…</code></td></tr>`);
    $("#source-table").innerHTML = tableMarkup([["Artifact / schema"], ["As of"], ["SHA-256"]], sourceRows);
    $("#limitations-list").innerHTML = data.limitations.map((item) => `<li>${safe(item)}</li>`).join("");
    const routes = {
      "full-systematic-hwm-unavailable": ["risk", "Qualify accepted daily NAV freshness across the governed HWM perimeter"],
      "current-strategy-attribution-partial": ["capital", "Adjudicate pool-only assignment intervals; current mapped positions remain valid"],
      "historical-strategy-attribution-unresolved": ["risk", "Adjudicate historical General intervals before complete account-by-strategy performance"],
    };
    const remediationRows = data.blocking_items.map((item) => {
      const [view, action] = item.category === "DATA_FRESHNESS"
        ? ["data", `Refresh or formally disposition the required ${humanStatus(item.fact_type)} evidence under ${item.policy_id}; reconciled ${formatDate(item.reconciled_on)}`]
        : routes[item.item] || ["data", "Resolve the declared coverage exception in its producing generation"];
      const focus = view === "data" ? ` data-focus-coverage="${safe(item.item)}"` : "";
      return `<tr><td><span class="cell-primary">${safe(humanStatus(item.item))}</span><span class="cell-secondary">${safe(item.known_gap || "No further detail recorded")}</span></td><td>${statusText(item.status)}</td><td>${safe(action)}</td><td><button type="button" class="text-action" data-open-view="${safe(view)}"${focus} aria-label="Open ${safe(VIEW_TITLES[view])} evidence for ${safe(humanStatus(item.item))}">Open ${safe(VIEW_TITLES[view])} →</button></td></tr>`;
    });
    $("#remediation-table").innerHTML = tableMarkup([["Exception"], ["State"], ["Required evidence"], ["Destination"]], remediationRows.length ? remediationRows : [emptyRow(4, "No material blocking exceptions")]);
  }

  function setView(name, updateHash = true) {
    const view = $(`[data-view="${name}"]`);
    if (!view) return;
    $$(".view").forEach((item) => { item.hidden = item !== view; item.classList.toggle("is-active", item === view); });
    $$('[data-nav]').forEach((button) => { const active = button.dataset.nav === name; button.classList.toggle("is-active", active); active ? button.setAttribute("aria-current", "page") : button.removeAttribute("aria-current"); });
    $("#page-title").textContent = VIEW_TITLES[name] || "Portfolio control";
    document.title = `V Capital — ${$("#page-title").textContent}`;
    if (updateHash) history.replaceState(null, "", `#${name}`);
    window.scrollTo({ top: 0, behavior: "auto" });
    if (updateHash) $("#main-content").focus({ preventScroll: true });
  }

  function populateContextControls() {
    const accountSelect = $("#context-account");
    accountSelect.innerHTML = `<option value="ALL">All accounts</option>${[...accountLabels.entries()].sort((left, right) => left[1].localeCompare(right[1])).map(([accountId, label]) => `<option value="${safe(accountId)}">${safe(label)}</option>`).join("")}`;
    const strategySelect = $("#context-strategy");
    strategySelect.innerHTML = `<option value="ALL">All strategies</option>${data.strategies.map((row) => `<option value="${safe(row.strategy_name)}">${safe(row.strategy_name)}</option>`).join("")}`;
    $("#context-as-of").textContent = formatDate(data.meta.portfolio_as_of);
  }

  function applyContext() {
    positionPage = 0;
    const account = selectedContextAccount === "ALL" ? "All accepted accounts" : accountLabel(selectedContextAccount);
    const strategy = selectedContextStrategy === "ALL" ? "all current strategies" : selectedContextStrategy;
    $("#context-summary").textContent = `${account} · ${strategy}`;
    $("#context-account").value = selectedContextAccount;
    $("#context-strategy").value = selectedContextStrategy;
    renderCapital();
    renderRisk();
    renderAttribution();
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
    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-open-view]");
      if (!button) return;
      setView(button.dataset.openView);
      const coverageItem = button.dataset.focusCoverage;
      if (!coverageItem) return;
      window.requestAnimationFrame(() => {
        const target = $(`[data-coverage-item="${CSS.escape(coverageItem)}"]`);
        if (!target) return;
        target.classList.add("is-remediation-focus");
        target.focus({ preventScroll: true });
        target.scrollIntoView({ block: "center", behavior: "auto" });
        window.setTimeout(() => target.classList.remove("is-remediation-focus"), 5000);
      });
    });
    const selectButton = (selector, button) => $$(selector).forEach((item) => { const active = item === button; item.classList.toggle("is-active", active); item.setAttribute("aria-pressed", String(active)); });
    $$('[data-allocation-filter]').forEach((button) => button.addEventListener("click", () => { allocationFilter = button.dataset.allocationFilter; positionPage = 0; selectButton('[data-allocation-filter]', button); renderAllocationTable(); }));
    $$('[data-tier-filter]').forEach((button) => button.addEventListener("click", () => { tierFilter = button.dataset.tierFilter; selectButton('[data-tier-filter]', button); renderCandidateTable(); }));
    $$('[data-evidence-mode]').forEach((button) => button.addEventListener("click", () => {
      const mode = button.dataset.evidenceMode;
      if (!dependenceModes[mode]) return;
      selectedEvidenceMode = mode;
      selectedPair = null;
      renderDependence();
    }));
    $$('[data-weekly-mode]').forEach((button) => button.addEventListener("click", () => {
      selectedWeeklyMode = button.dataset.weeklyMode;
      selectedWeeklyCell = null;
      renderWeekly();
    }));
    $$('[data-weekly-window]').forEach((button) => button.addEventListener("click", () => {
      selectedWeeklyWindow = button.dataset.weeklyWindow;
      weeklyPage = 0;
      selectedWeeklyCell = null;
      renderWeekly();
    }));
    $("#weekly-current-toggle").addEventListener("change", (event) => {
      includeCurrentWeek = event.target.checked;
      weeklyPage = 0;
      selectedWeeklyCell = null;
      renderWeekly();
    });
    $("#weekly-search").addEventListener("input", (event) => {
      weeklySearch = event.target.value.trim();
      selectedWeeklyCell = null;
      renderWeeklyTable();
    });
    $("#weekly-page-older").addEventListener("click", () => { weeklyPage += 1; selectedWeeklyCell = null; renderWeeklyTable(); });
    $("#weekly-page-newer").addEventListener("click", () => { weeklyPage = Math.max(0, weeklyPage - 1); selectedWeeklyCell = null; renderWeeklyTable(); });
    $$('[data-matrix]').forEach((button) => button.addEventListener("click", () => {
      selectedMatrix = button.dataset.matrix;
      if (selectedMatrix !== "observed_pearson") selectedCorrelationWindow = "full";
      $$('[data-matrix]').forEach((item) => { const active = item === button; item.classList.toggle("is-active", active); item.setAttribute("aria-pressed", String(active)); });
      $$('[data-correlation-window]').forEach((item) => { const active = item.dataset.correlationWindow === selectedCorrelationWindow; item.classList.toggle("is-active", active); item.setAttribute("aria-pressed", String(active)); });
      renderCorrelationMatrix();
    }));
    $$('[data-correlation-window]').forEach((button) => button.addEventListener("click", () => {
      selectedCorrelationWindow = button.dataset.correlationWindow;
      selectedMatrix = "observed_pearson";
      $$('[data-correlation-window]').forEach((item) => { const active = item === button; item.classList.toggle("is-active", active); item.setAttribute("aria-pressed", String(active)); });
      $$('[data-matrix]').forEach((item) => { const active = item.dataset.matrix === selectedMatrix; item.classList.toggle("is-active", active); item.setAttribute("aria-pressed", String(active)); });
      renderCorrelationMatrix();
      if (selectedPair) {
        const selected = currentDependence().cells.find((item) => pairKey(item.left, item.right) === selectedPair);
        if (selected) renderCorrelationDetail(selected.left, selected.right);
      }
    }));
    $("#candidate-search").addEventListener("input", (event) => { candidateSearch = event.target.value.trim(); renderCandidateTable(); });
    $("#position-search").addEventListener("input", (event) => { positionSearch = event.target.value.trim(); positionPage = 0; renderPositionTable(); });
    $("#position-page-previous").addEventListener("click", () => { positionPage = Math.max(0, positionPage - 1); renderPositionTable(); });
    $("#position-page-next").addEventListener("click", () => { positionPage += 1; renderPositionTable(); });
    $("#context-account").addEventListener("change", (event) => { selectedContextAccount = event.target.value; applyContext(); });
    $("#context-strategy").addEventListener("change", (event) => { selectedContextStrategy = event.target.value; applyContext(); });
    $("#context-reset").addEventListener("click", () => { selectedContextAccount = "ALL"; selectedContextStrategy = "ALL"; applyContext(); });
    $("#snapshot-button").addEventListener("click", () => showToast(`Portfolio facts ${formatDate(data.meta.portfolio_as_of)} · freshness evaluated ${formatDate(data.meta.freshness_evaluated_on)} · Report ${data.meta.report_generation_id.slice(0, 12)}… · read-only local artifact.`));
    $$('.segment').forEach((button) => button.setAttribute("aria-pressed", String(button.classList.contains("is-active"))));
    window.addEventListener("hashchange", () => setView(location.hash.slice(1) || "command", false));
  }

  function init() {
    try {
      if (!data || data.panel_schema_version !== 13) throw new Error("Embedded dashboard data is missing or unsupported");
      $("#portfolio-as-of").textContent = formatDate(data.meta.portfolio_as_of);
      $("#freshness-as-of").textContent = formatDate(data.meta.freshness_evaluated_on);
      $("#coverage-status").textContent = `${pct(data.advisory.nav_coverage)} NAV`;
      populateContextControls();
      renderCommand(); renderCapital(); renderRisk(); renderSignals(); renderAttribution(); renderDependence(); renderWeekly(); renderCandidates(); renderData(); bindEvents();
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
