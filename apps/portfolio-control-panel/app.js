(() => {
  "use strict";

  const data = window.PORTFOLIO_DATA;
  // F48: the comparison and heat-map scales are named, and the labels below read them.
  const COMPARISON_WEEKS = 13;
  const WEEKLY_FULL_INTENSITY_RETURN = 0.08;
  const performanceLineageMismatch = data?.meta?.performance_lineage?.status === "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH";
  const performanceLineageReason = String(data?.meta?.performance_lineage?.reason || "Report performance is withheld because its source lineage does not match the accepted identity.");
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

  const formatDateHtml = (value) => safe(formatDate(value));
  // F07/F23/F24: how many days a dated figure is older than the portfolio cutoff.  Derived
  // from the two dates only; a date at or after the cutoff, or a missing date, is no flag.
  const dayNumber = (value) => {
    const parsed = new Date(`${String(value || "").slice(0, 10)}T00:00:00Z`);
    return Number.isNaN(parsed.getTime()) ? null : Math.floor(parsed.getTime() / 86400000);
  };
  const daysBeforeCutoff = (value) => {
    const cutoff = dayNumber(data?.meta?.portfolio_as_of);
    const day = dayNumber(value);
    return cutoff == null || day == null || day >= cutoff ? 0 : cutoff - day;
  };
  const olderThanCutoffText = (value) => {
    const days = daysBeforeCutoff(value);
    return days > 0 ? `older than cutoff by ${integer.format(days)} day${days === 1 ? "" : "s"}` : "";
  };
  const olderThanCutoffTag = (value) => {
    const text = olderThanCutoffText(value);
    return text ? `<span class="stale-tag">${safe(text)}</span>` : "";
  };
  const staleSuffix = (value) => { const tag = olderThanCutoffTag(value); return tag ? ` · ${tag}` : ""; };
  // F50: a null effective end is an open interval, not an unknown date.
  const endDateHtml = (value) => (value ? formatDateHtml(value) : "open");
  // F13/F50: a producer annualization label can already carry its own parenthesis.
  const annualizationSuffix = (label) => (label ? (String(label).includes("(") ? ` · ${safe(label)}` : ` (${safe(label)})`) : "");
  // F27/F39/F50: values far from order one are shown in scientific notation, never as 0.000.
  const smallDecimal = (value, digits = 6) => {
    const parsed = number(value);
    if (parsed == null) return "—";
    return parsed !== 0 && Math.abs(parsed) < 10 ** -digits ? parsed.toExponential(2) : parsed.toFixed(digits);
  };

  // F15/F17/F33: an explicit status -> tone table.  The expressions below remain only for
  // tokens the table does not name.  A validated model and a not-validated model are named
  // here, and a "*_NOT_COMPLETENESS_PROOF" disposition is never a positive one.
  const STATUS_TONES = Object.freeze({
    MODEL_VALIDATED_FOR_PM_USE: "positive",
    MODEL_COMPUTED_NOT_VALIDATED: "warning",
    MODEL_UNAVAILABLE: "danger",
    NOT_VALIDATED: "warning",
    VALIDATED: "positive",
    tested: "positive",
    not_testable: "warning",
    partial_only: "warning",
    not_same_vintage: "warning",
  });
  function toneForStatus(status = "") {
    if (Object.hasOwn(STATUS_TONES, String(status))) return STATUS_TONES[String(status)];
    const text = String(status).toUpperCase();
    if (/NOT_COMPLETENESS_PROOF$/.test(text)) return "neutral";
    if (/(^|_)NOT_VALIDATED$/.test(text)) return STATUS_TONES.NOT_VALIDATED;
    if (/FAIL|UNAVAILABLE|UNMAPPED|DO_NOT|BLOCK|NONE|NOT_AUTOMATABLE|INCOMPLETE|NO_ACCEPTED_FACT_PATH/.test(text)) return "danger";
    if (/PARTIAL|INFERRED|UNKNOWN|CONDITIONAL|FROZEN|DIAGNOSTIC|NOT_READY|UPPER_BOUND/.test(text)) return "warning";
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
    UNAVAILABLE_SOURCE_LINEAGE_MISMATCH: "Report performance withheld: source-lineage mismatch",
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
    NOT_READY_STALE_CONTRACT_LEDGER_VINTAGE: "Not ready: stale contract (ledger vintage)",
    PARTIAL_CAUSAL_ATTRIBUTION_RECONCILIATION_NOT_READY: "Partial: causal attribution ready, reconciliation not ready",
    PARTIAL_RECONCILIATION_ATTRIBUTION_NOT_READY: "Partial: reconciliation ready, causal attribution not ready",
    UPPER_BOUND_CONTRACT_CAP_PRE_TAX: "Upper bound (contract cap)",
    DIAGNOSTIC_PROXY_NOT_OFFICIAL_PROVIDER_NAV: "Diagnostic proxy not official provider NAV",
    // F15: `implementation_gap_status` carries the producer's lower-case audit tokens.
    tested: "Tested",
    not_testable: "Not testable",
    partial_only: "Partial only",
    not_same_vintage: "Not same vintage",
    // F33: dispositions that say a fact is observed without proving it complete.
    OBSERVED_NOT_COMPLETENESS_PROOF: "Observed; not a completeness proof",
    NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF: "No reported events; not a completeness proof",
    NO_REPORTED_FACT_NOT_COMPLETENESS_PROOF: "No reported fact; not a completeness proof",
  };
  const ACCOUNT_MANIFEST_AUTHORITY_LABELS = Object.freeze({
    INDEPENDENT_EXACT_REBUILD: "Independent exact rebuild",
    VERIFIED_GENERATION_BUNDLE_EXACT_BYTES: "Report-attested static publication — verified generation bundle exact bytes",
  });
  const EMBEDDED_DOWNLOAD_TYPES = Object.freeze({
    "portfolio-report-data.json": Object.freeze({ mimeType: "application/json", payloadPrefix: "download-payload-report-json", encoding: "gzip" }),
    "portfolio-report.xlsx": Object.freeze({ mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", payloadPrefix: "download-payload-report-xlsx", encoding: "identity" }),
  });
  const EMBEDDED_DOWNLOAD_CHUNK_SIZE = 65536;
  // The report data is embedded rather than linked to the sibling file because
  // Chromium ignores the `download` attribute when a file:// page links to a
  // file:// target: it navigates instead and no download event ever fires.
  // Every browser and interaction QA capture loads the panel over file://, so
  // the sibling route cannot satisfy the gate.
  //
  // The report JSON is embedded gzip-compressed and inflated with
  // DecompressionStream on click; the chunks are decoded one at a time and never
  // joined into a single string. 8191 is a sanity bound on the chunk count, and
  // `_DOWNLOAD_MAX_BASE64_CHUNKS` in tools/portfolio_control_panel.py must equal
  // this; a test pins the two together.
  const EMBEDDED_DOWNLOAD_MAX_CHUNKS = 8191;
  const humanStatus = (status) => STATUS_LABELS[String(status)] || String(status || "Unknown").replaceAll("_", " ").toLowerCase().replace(/^./, (character) => character.toUpperCase());
  // F3: gate notes arrive from the producer already respelled as plain words (`note`; the
  // verbatim original is `note_source` and is never shown).  Free `known_gap` text on the other
  // cards, and a payload that predates the note contract, can still carry machine tokens, so the
  // same respelling applies here: UPPER_SNAKE becomes lower-case words (acronyms stay upper
  // case) and snake_key:value becomes "snake key: value".  Readable text passes through
  // unchanged, so applying it to an already-respelled note changes nothing.
  const NOTE_ACRONYMS = new Set(["API", "FX", "HWM", "IBKR", "ID", "MWR", "NAV", "TWR", "USD", "UTC"]);
  const operatorText = (text) => String(text ?? "")
    .replace(/\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/g, (token) => token.split("_").map((word) => (NOTE_ACRONYMS.has(word) ? word : word.toLowerCase())).join(" "))
    .replace(/\b[a-z]+(?:_[a-z]+)+:[A-Za-z0-9_:.-]+/g, (token) => {
      const at = token.indexOf(":");
      return `${token.slice(0, at).replaceAll("_", " ")}: ${token.slice(at + 1).replaceAll("_", " ")}`;
    });
  const statusText = (status) => `<span class="status-text ${toneForStatus(status)}">${safe(humanStatus(status))}</span>`;
  const kpi = (label, value, note, tone = "") => `<article class="kpi-card ${tone ? `tone-${tone}` : ""}"><div class="kpi-label">${safe(label)}</div><div class="kpi-value">${safe(value)}</div><div class="kpi-note">${safe(note)}</div></article>`;
  const renderKpis = (selector, items) => { $(selector).innerHTML = items.map((item) => kpi(...item)).join(""); };
  const emptyRow = (columns, message = "No matching rows") => `<tr><td colspan="${columns}" class="cell-secondary">${safe(message)}</td></tr>`;
  // F20/F12: `footer` is the markup of a <tfoot> body (total rows computed from the rows shown).
  const tableMarkup = (headers, rows, caption = null, footer = "") => `${caption ? `<caption>${safe(caption)}</caption>` : ""}<thead><tr>${headers.map(([label, cls = "", title = ""]) => `<th scope="col" class="${cls}"${title ? ` title="${safe(title)}"` : ""}>${safe(label)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody>${footer ? `<tfoot>${footer}</tfoot>` : ""}`;
  const strategyByName = (name) => data.strategies.find((row) => row.strategy_name === name);
  // F04: the operator sees a strategy's name, never its id slug, where the payload can map it.
  const strategyNameById = (strategyId) => (data.strategies || []).find((row) => row.strategy_id === strategyId)?.strategy_name || strategyId;
  const PROVIDER_NAMES = Object.freeze({
    alpaca: "Alpaca",
    fidelity: "Fidelity",
    ibkr: "IBKR",
    internal: "Internal",
    "john hancock": "John Hancock",
    matrixport: "Matrixport",
    portfolio123: "Portfolio123",
    quantbase: "Quantbase",
    schwab: "Schwab",
  });
  // F35: a provider id the table does not name is title-cased, never printed as a raw id.
  const providerName = (providerId) => {
    const key = String(providerId || "").toLowerCase();
    if (Object.hasOwn(PROVIDER_NAMES, key)) return PROVIDER_NAMES[key];
    return key ? key.replaceAll("_", " ").replace(/\b[a-z]/g, (letter) => letter.toUpperCase()) : "Provider";
  };
  const evidenceCoverage = (row) => {
    if (performanceLineageMismatch) return `UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${performanceLineageReason}`;
    const evidence = row.provider_evidence;
    if (!evidence) return "No accepted provider overlay";
    const account = evidence.account;
    const provider = evidence.provider;
    // The provider generation date is when the provider data was captured; the accepted
    // cutoff is the portfolio as-of.  A capture after the cutoff is named once per table
    // by postCutoffCaptureNote (K1, F26), not repeated on every row.
    const parts = [`Provider capture ${formatDate(evidence.generation_as_of_date)}`];
    if (account) parts.push(`${providerName(account.provider_id || evidence.provider_id)} account-live TWR: ${formatDate(account.observed_start)}–${formatDate(account.observed_end)} · ${integer.format(account.return_observations || 0)} returns`);
    if (provider) parts.push(`${providerName(provider.provider_id || evidence.provider_id)} provider-live book: ${formatDate(provider.observed_start)}–${formatDate(provider.observed_end)} · ${integer.format(provider.return_observations || 0)} returns · governed provider-ledger classification, not brokerage-account authority`);
    if (evidence.current_snapshot) parts.push(`${providerName(provider?.provider_id || evidence.provider_id)} account snapshot: ${formatDate(evidence.current_snapshot.observed_date)}`);
    return parts.join(" · ");
  };
  // F26: one note per table, naming how many strategies carry a capture after the cutoff.
  const postCutoffCaptureNote = () => {
    const cutoffDate = String(data.meta?.portfolio_as_of || "").slice(0, 10);
    const strategies = Array.isArray(data.strategies) ? data.strategies : [];
    const late = strategies.filter((row) => cutoffDate !== "" && String(row.provider_evidence?.generation_as_of_date || "").slice(0, 10) > cutoffDate);
    if (!late.length) return "";
    // The through-date is the observed_end of each accepted evidence path, never the capture date.
    const ends = late.flatMap((row) => [row.provider_evidence?.account, row.provider_evidence?.provider]
      .filter(Boolean)
      .map((path) => String(path.observed_end || "").slice(0, 10)));
    const allThroughCutoff = ends.length > 0 && ends.every((end) => end !== "" && end >= cutoffDate);
    const head = `Post-cutoff capture: ${integer.format(late.length)} of ${integer.format(strategies.length)} provider captures are dated after the ${formatDate(cutoffDate)} cutoff`;
    return allThroughCutoff
      ? `${head}; the observed intervals they carry run through the cutoff.`
      : `${head}; observed intervals end on their own dates, shown per row.`;
  };
  const evidenceCaption = (title) => {
    const note = postCutoffCaptureNote();
    return note ? `${title}. ${note}` : title;
  };
  const evidenceAvailability = (row) => {
    if (performanceLineageMismatch) return `<span class="cell-secondary">— UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${safe(performanceLineageReason)}</span>`;
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
    if (performanceLineageMismatch) return `<span class="cell-secondary">— UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${safe(performanceLineageReason)}</span>`;
    // One column, one field: `implementation_gap_status` is the account-versus-model
    // status on the Portfolio view; this column is the provider comparison (K11).
    const status = row.provider_comparison_status || "UNAVAILABLE";
    if (!String(status || "").startsWith("DESCRIPTIVE_EXACT_DATE")) {
      // N55 (general): `provider_comparison_reason` is the not-ready reason surface for
      // ANY comparison path, not a benchmark-only or N55-only field; render it beside the
      // disposition whenever the report publishes one.
      return `${statusText(status)}<span class="cell-secondary">${safe(row.provider_comparison_disposition || "No accepted exact-date provider/model comparison")}</span>${row.provider_comparison_reason ? `<span class="cell-secondary">${safe(row.provider_comparison_reason)}</span>` : ""}`;
    }
    // A5: `provider_annualization.label` already encodes basis/window (and "(<1 year)"
    // when short); render it beside the annualized figure it describes.
    const providerAnnualizedLabel = row.provider_annualization?.label;
    return `${statusText(status)}<span class="cell-secondary">Exact common dates ${formatDateHtml(row.provider_comparison_observed_start)}–${formatDateHtml(row.provider_comparison_observed_end)} · ${integer.format(row.provider_comparison_observations || 0)} observations${staleSuffix(row.provider_comparison_observed_end)}</span><span class="cell-secondary">Account ${pct(row.provider_account_cumulative_return)} · reference ${pct(row.provider_reference_cumulative_return)} · cumulative gap ${pct(row.provider_cumulative_gap)} · active annualized ${pct(row.provider_active_return_annualized)}${annualizationSuffix(providerAnnualizedLabel)}</span><span class="cell-secondary">Tracking error ${pct(row.provider_tracking_error_annualized)} · information ratio ${decimal(row.provider_information_ratio)} · beta ${decimal(row.provider_beta)} · correlation ${decimal(row.provider_correlation)}</span><span class="cell-secondary">Up capture ${pct(row.provider_upside_capture)} · down capture ${pct(row.provider_downside_capture)} · relative drawdown ${pct(row.provider_relative_drawdown)} · cash drag ${statusText(row.cash_drag_status)} · ${safe(row.cash_drag_reason)}</span><span class="cell-secondary">${safe(row.provider_comparison_disposition)}</span>${row.provider_comparison_reason ? `<span class="cell-secondary">${safe(row.provider_comparison_reason)}</span>` : ""}`;
  };
  const costDisclosure = (row) => {
    const amount = number(row.annual_external_cost_usd) == null ? "Pre-tax cost unavailable" : `${currencyExact(row.annual_external_cost_usd)} annually`;
    // N64: a rate divides by the sleeve value at the cutoff and says which date that value
    // and the cost schedule carry.  A report older than schema 4 publishes neither date.
    const rateStatus = typeof row.cost_rate_status === "string" ? row.cost_rate_status : null;
    let rate;
    if (rateStatus === "AVAILABLE_AT_CUTOFF_SLEEVE_VALUE" && number(row.annual_external_cost_rate_pre_tax) != null) {
      rate = `${pct(row.annual_external_cost_rate_pre_tax, 2)} of sleeve value at ${formatDate(row.cost_rate_denominator_as_of_date)} · cost schedule as of ${formatDate(row.cost_schedule_as_of_date)}`;
    } else if (rateStatus !== null) {
      rate = `${humanStatus(rateStatus)}${row.cost_rate_reason ? ` · ${row.cost_rate_reason}` : ""}`;
    } else if (number(row.annual_external_cost_rate_pre_tax) == null) {
      rate = "rate unavailable";
    } else {
      rate = `${pct(row.annual_external_cost_rate_pre_tax, 2)} of sleeve value (denominator date not published by this report schema)`;
    }
    return `<span class="cell-primary">${safe(humanStatus(row.cost_status))}</span><span class="cell-secondary">Fees ${statusText(row.fees_status)} · ${safe(amount)} · ${safe(rate)}</span><span class="cell-secondary">After-tax ${statusText(row.after_tax_status)}${number(row.after_tax_annual_cost_rate) == null ? "" : ` · ${pct(row.after_tax_annual_cost_rate, 2)}`}</span>`;
  };
  // N55: who authorized the current benchmark assignment.  An owner-made override is
  // named explicitly; a row that has never been overridden retains the prior status quo
  // label rather than implying a decision was made.
  const BENCHMARK_ASSIGNMENT_AUTHORITY_LABELS = {
    OWNER_POLICY_DECISION: "Owner policy decision",
    RETAINED_ATTRIBUTION_SPEC: "Retained attribution spec",
  };
  const benchmarkDisclosure = (row) => {
    if (performanceLineageMismatch) return `<span class="cell-secondary">— UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${safe(performanceLineageReason)}</span>`;
    const authority = row.benchmark_assignment_authority
      ? `<span class="cell-secondary">Assignment authority: ${safe(BENCHMARK_ASSIGNMENT_AUTHORITY_LABELS[row.benchmark_assignment_authority] || humanStatus(row.benchmark_assignment_authority))}</span>`
      : "";
    const assignment = `<span class="cell-primary">Benchmark ${statusText(row.benchmark_assignment_status)}</span><span class="cell-secondary">Effective ${formatDateHtml(row.benchmark_effective_start)}–${endDateHtml(row.benchmark_effective_end)} · ${safe(row.benchmark_id || "No admitted benchmark")}</span><span class="cell-secondary">${safe(row.benchmark_rationale || "No benchmark rationale recorded")}</span>${authority}`;
    // N55 (general): `benchmark_comparison_reason` is the not-ready reason surface for the
    // benchmark comparison path; render it beside the disposition whenever published.
    const reason = row.benchmark_comparison_reason ? `<span class="cell-secondary">${safe(row.benchmark_comparison_reason)}</span>` : "";
    if (!String(row.benchmark_comparison_status || "").startsWith("DESCRIPTIVE_EXACT_DATE")) return `${assignment}<span class="cell-secondary">${statusText(row.benchmark_comparison_status || "UNAVAILABLE")} · ${safe(row.benchmark_comparison_disposition || "No exact-date benchmark-relative comparison is admitted")}</span>${reason}`;
    // A5: `benchmark_annualization.label` already encodes basis/window (and "(<1 year)"
    // when short); render it beside the annualized figure it describes.
    const benchmarkAnnualizedLabel = row.benchmark_annualization?.label;
    return `${assignment}<span class="cell-secondary">Exact common dates ${formatDateHtml(row.benchmark_comparison_observed_start)}–${formatDateHtml(row.benchmark_comparison_observed_end)} · ${integer.format(row.benchmark_comparison_observations || 0)} observations</span><span class="cell-secondary">Strategy ${pct(row.benchmark_account_cumulative_return)} · benchmark ${pct(row.benchmark_reference_cumulative_return)} · gap ${pct(row.benchmark_cumulative_gap)} · active annualized ${pct(row.benchmark_active_return_annualized)}${annualizationSuffix(benchmarkAnnualizedLabel)}</span><span class="cell-secondary">Tracking error ${pct(row.benchmark_tracking_error_annualized)} · information ratio ${decimal(row.benchmark_information_ratio)} · beta ${decimal(row.benchmark_beta)} · correlation ${decimal(row.benchmark_correlation)}</span><span class="cell-secondary">Up capture ${pct(row.benchmark_upside_capture)} · down capture ${pct(row.benchmark_downside_capture)} · relative drawdown ${pct(row.benchmark_relative_drawdown)}</span><span class="cell-secondary">${safe(row.benchmark_comparison_disposition)}</span>${reason}`;
  };
  const hwmQualification = (row) => {
    if (performanceLineageMismatch) return { status: "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH", note: performanceLineageReason };
    if (row.hwm_scope === "EXCLUDED_DE_MINIMIS") return { status: "EXCLUDED_DE_MINIMIS", note: row.hwm_exclusion_reason || "No exclusion reason recorded in the payload" };
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
  const hwmObservationSummary = (row) => {
    const observations = number(row.nav_return_observations);
    const observationText = observations == null
      ? `${hwmEvidenceLabel(row)} return observation count unavailable`
      : `${integer.format(observations)} ${hwmEvidenceLabel(row)} return observations`;
    const intervalText = row.nav_observed_start && row.nav_observed_end
      ? ` from ${formatDate(row.nav_observed_start)} through ${formatDate(row.nav_observed_end)}`
      : "; observation interval unavailable";
    return `${observationText}${intervalText}`;
  };
  const hwmExclusionNote = () => (data.hwm_summary.excluded_details || []).map((item) => `${item.strategy_name}: ${currencyExact(item.current_value_usd)} · ${item.reason}`).join(" · ") || "No policy exclusions";
  // Current capital (cluster 2): every figure below is the payload's value, status
  // and date.  A component that is not ready at the cutoff is shown as not ready and
  // is never sized, painted as a remainder, or described as part of a sum.
  const CAPITAL_COMPONENTS = [
    { key: "systematic", label: "Systematic", color: "var(--accent)" },
    { key: "reserve", label: "Reserve", color: "var(--reserve)" },
    { key: "retained_excluded", label: "Retained excluded", color: "var(--neutral)" },
  ];
  const CAPITAL_HATCH = "repeating-linear-gradient(135deg, var(--border-strong) 0 4px, transparent 4px 9px)";
  const capitalState = (key) => {
    const value = data.capital?.[key];
    const status = data.capital_status?.[key] ?? null;
    const ready = status === "AVAILABLE" && number(value) != null;
    return { key, value, status, ready, asOf: ready ? data.capital_as_of?.[key] ?? null : null };
  };
  const capitalAmount = (key) => (capitalState(key).ready ? currency(data.capital[key]) : "Not ready");
  const capitalAsOf = (key) => {
    const state = capitalState(key);
    return state.ready && state.asOf ? ` · as of ${formatDate(state.asOf)}` : "";
  };
  const capitalNotReadyNote = () => `Not ready at the cutoff: ${CAPITAL_COMPONENTS.filter((part) => !capitalState(part.key).ready).map((part) => part.label.toLowerCase()).join(", ")}`;
  const capitalRingMarkup = () => {
    const parts = CAPITAL_COMPONENTS.map((part) => ({ ...part, ...capitalState(part.key), share: null }));
    const total = capitalState("total_liquid");
    const sized = total.ready && Number(total.value) > 0 && parts.every((part) => part.ready);
    const stops = [];
    let cursor = 0;
    if (sized) {
      parts.forEach((part) => {
        part.share = Number(part.value) / Number(total.value) * 100;
        stops.push(`${part.color} ${cursor.toFixed(3)}% ${(cursor + part.share).toFixed(3)}%`);
        cursor += part.share;
      });
    }
    const background = sized ? `conic-gradient(${stops.join(", ")})` : CAPITAL_HATCH;
    const legend = parts.map((part) => {
      const label = !part.ready ? `${part.label} · Not ready at the cutoff` : sized ? `${part.label} · ${part.share.toFixed(1)}%` : part.label;
      const dot = part.ready ? `<span class="legend-dot" style="background:${part.color}"></span>` : `<span class="legend-dot is-hatched" style="background:${CAPITAL_HATCH}"></span>`;
      return `<div class="legend-item" data-capital-component="${part.key}" data-capital-status="${safe(part.status)}">${dot}<span>${safe(label)}</span><strong>${part.ready ? currency(part.value) : "—"}</strong></div>`;
    }).join("");
    return `<div class="capital-donut" data-capital-state="${sized ? "CURRENT" : "NOT_READY"}" style="background:${background}"><div class="donut-center"><strong>${sized ? currency(total.value) : "Not ready"}</strong><span>${sized ? "liquid" : "liquid at cutoff"}</span></div></div><div class="legend-list">${legend}</div>`;
  };
  const weightBasisText = (basis) => {
    const text = String(basis || "");
    if (text === "CURRENT_CUTOFF") return "Current cutoff weights";
    if (text === "POLICY_PORT_DEPENDENCE_1") return "Policy weights (PORT-DEPENDENCE-1)";
    const historical = /^HISTORICAL_SNAPSHOT_(\d{4})_(\d{2})_(\d{2})$/.exec(text);
    return historical ? `Historical snapshot ${historical[1]}-${historical[2]}-${historical[3]}` : "Weight basis not labeled";
  };
  const weightBasisSuffix = (basis, value) => (value == null ? "" : ` (${safe(weightBasisText(basis))})`);
  // N66/K8: every account and model return names its basis; "verified" is never a label of a
  // diagnostic proxy.  Codes are the implementation contract's `*_basis` fields.
  const ACCOUNT_BASIS_LABELS = Object.freeze({ BASELINE_ACCOUNT_LIVE: "governed account-live series" });
  const MODEL_BASIS_LABELS = Object.freeze({
    SIGNAL_STATE_TIMES_SPXL_ADJUSTED_CLOSE_PROXY: "Signal-state × SPXL adjusted-close proxy model",
    PROVIDER_LIVE_MODEL_RETURNS: "P123 provider live model returns",
    TT_SOURCE_MANIFEST_MODEL_RETURNS: "TT source-manifest model returns",
  });
  const BRIDGE_BASIS_LABELS = Object.freeze({
    DAILY_OPEN_SIGNAL_STATE_PROXY: "signal-state path at SPXL daily open (proxy)",
    EXACT_PROVIDER_LIVE_RETURNS: "exact provider live returns",
    EXACT_WORKBOOK_MODEL_RETURNS: "exact workbook model returns",
  });
  const accountBasisText = (code) => ACCOUNT_BASIS_LABELS[code] || (code ? humanStatus(code).toLowerCase() : "account basis not recorded");
  // N68: the stress scenario is labelled from the weight set it was computed on.  Only the
  // cutoff weight basis is a "current portfolio"; a policy set names its policy and date.
  const stressWeightLabel = () => {
    const dependence = data.dependence_analysis;
    const basis = typeof dependence?.weight_basis === "string" ? dependence.weight_basis : "";
    const authority = dependence?.weight_authority && typeof dependence.weight_authority === "object" ? dependence.weight_authority : null;
    const asOf = typeof authority?.as_of_date === "string" ? authority.as_of_date : null;
    // F07: a weight set dated before the cutoff says by how many days.
    const staleNote = asOf && olderThanCutoffText(asOf) ? `; ${olderThanCutoffText(asOf)}` : "";
    if (basis === "CURRENT_CUTOFF") return "Current portfolio (cutoff weights)";
    if (basis.startsWith("POLICY_")) {
      const policy = typeof authority?.policy_id === "string" ? authority.policy_id : weightBasisText(basis);
      return `Policy weights (${policy}${asOf ? `, as of ${asOf}` : ""}${staleNote})`;
    }
    return `${weightBasisText(basis)}${asOf ? ` (as of ${asOf}${staleNote})` : ""}`;
  };
  // N62: "current holdings" are the rows of the latest accepted snapshot at the cutoff.  The
  // panel producer classifies every row; a payload without classes is listed whole and says so.
  const allPositionRows = () => (Array.isArray(data.positions) ? data.positions : []);
  const positionsAreClassified = () => allPositionRows().some((row) => typeof row.position_class === "string");
  const currentPositionRows = () => (positionsAreClassified()
    ? allPositionRows().filter((row) => row.position_class === "CURRENT_AT_CUTOFF")
    : allPositionRows());
  const positionSnapshots = () => {
    const value = data.position_snapshots;
    return value && typeof value === "object" && typeof value.cutoff_date === "string" ? value : null;
  };
  const positionSplitClauses = (snapshots) => {
    const clauses = [`${integer.format(snapshots.historical_rows || 0)} historical snapshot rows kept as history`];
    if (snapshots.latest_before_cutoff_rows) clauses.push(`${integer.format(snapshots.latest_before_cutoff_rows)} rows from a latest snapshot dated before the cutoff (stale, not current)`);
    if (snapshots.after_cutoff_rows) clauses.push(`${integer.format(snapshots.after_cutoff_rows)} rows dated after the cutoff (named, not counted as current)`);
    return clauses;
  };
  const positionGateSummary = () => {
    const snapshots = positionSnapshots();
    const count = integer.format(data.counts.positions || 0);
    if (!snapshots) return `${count} accepted position rows (snapshot dates are not classified in this payload); provider-boundary exceptions remain in Data health.`;
    return `${count} position rows are current at the ${formatDate(snapshots.cutoff_date)} cutoff (latest accepted snapshot); ${positionSplitClauses(snapshots).join("; ")}; provider-boundary exceptions remain in Data health.`;
  };
  const positionHistoryNote = () => {
    const snapshots = positionSnapshots();
    if (!snapshots) return positionsAreClassified() ? "" : "Snapshot dates are not classified in this payload, so every accepted row is listed.";
    const range = snapshots.history_first_as_of && snapshots.history_last_as_of
      ? ` across ${integer.format(snapshots.history_snapshot_dates || 0)} snapshot dates (${formatDate(snapshots.history_first_as_of)}–${formatDate(snapshots.history_last_as_of)})`
      : "";
    const parts = [`Accepted position history: ${integer.format(snapshots.historical_rows || 0)} historical snapshot rows${range}; these are not current holdings.`];
    if (snapshots.latest_before_cutoff_rows) parts.push(`${integer.format(snapshots.latest_before_cutoff_rows)} rows from a latest snapshot dated before the cutoff (stale, not current).`);
    if (snapshots.after_cutoff_rows) parts.push(`${integer.format(snapshots.after_cutoff_rows)} rows dated after the cutoff (named, not counted as current).`);
    return parts.join(" ");
  };
  // Data health: the total is the baseline's accepted-row count, split from the panel's counts.
  const positionTotalAccounting = (total, observedEnd) => {
    const snapshots = positionSnapshots();
    const rows = number(total);
    if (!snapshots || rows == null) return "";
    const parts = [`${integer.format(snapshots.current_at_cutoff_rows || 0)} current at the cutoff`, `${integer.format(snapshots.historical_rows || 0)} historical`];
    if (snapshots.latest_before_cutoff_rows) parts.push(`${integer.format(snapshots.latest_before_cutoff_rows)} stale`);
    if (snapshots.after_cutoff_rows) parts.push(`${integer.format(snapshots.after_cutoff_rows)} dated after the cutoff`);
    const unpublished = rows - (snapshots.accepted_rows_total || 0);
    if (unpublished > 0) {
      const later = typeof observedEnd === "string" && observedEnd.slice(0, 10) > snapshots.cutoff_date;
      parts.push(`${integer.format(unpublished)} not published as position rows${later ? ` (the window runs to ${formatDate(observedEnd)}, after the cutoff)` : ""}`);
    }
    return parts.join(" · ");
  };
  // K9: the flow providers are the providers that have accepted flow records.
  const flowProviderList = (gateNote) => {
    // The flows fact names its providers ("N accepted flow records across A, B, C; ..."); that
    // sentence is the authority, the matrix rows with records are the fallback.
    const named = /across ([A-Z][A-Z0-9 ,]*[A-Z0-9])(?:;|$)/.exec(String(gateNote || ""));
    if (named) {
      const listed = named[1].split(",").map((item) => providerName(item.trim().toLowerCase()));
      return listed.length === 1 ? listed[0] : listed.length === 2 ? `${listed[0]} and ${listed[1]}` : `${listed.slice(0, -1).join(", ")}, and ${listed[listed.length - 1]}`;
    }
    const matrix = Array.isArray(data.fact_coverage_matrix) ? data.fact_coverage_matrix : [];
    const names = [...new Set(matrix
      .filter((row) => row.fact_type === "flows" && row.status !== "NO_ACCEPTED_FACT_PATH" && Number(row.record_count) > 0)
      .map((row) => providerName(row.provider_id)))].sort();
    if (!names.length) return null;
    if (names.length === 1) return names[0];
    if (names.length === 2) return `${names[0]} and ${names[1]}`;
    return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
  };
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
  const accountDimension = data?.account_summary || [];
  const accountLabels = new Map();
  // F03: the producer's own account label (for example a masked number and registration) is
  // the account's name.  The numbered "<Provider> account N" label is only the fallback for
  // an account that publishes none, and it depends on the order of the opaque ids.
  Object.entries(accountDimension.reduce((groups, row) => {
    const provider = providerName(row.provider_id || row.provider);
    (groups[provider] ||= []).push(row);
    return groups;
  }, {})).forEach(([provider, rows]) => rows
    .sort((left, right) => String(left.account_id).localeCompare(String(right.account_id)))
    .forEach((row, index) => accountLabels.set(
      row.account_id,
      typeof row.account_label === "string" && row.account_label.trim() !== "" ? row.account_label.trim() : `${provider} account ${index + 1}`,
    )));
  let selectedContextAccount = "ALL";
  let selectedContextStrategy = "ALL";
  const accountLabel = (accountId) => accountLabels.get(accountId) || "Unrecognized account";
  // F04/F35: an operator cell names the provider and the account, never a raw id.  A row with
  // no account id is a provider-level row; an id the account dimension does not know is said
  // to be unrecognized rather than printed.
  const accountProviderText = (row) => {
    const provider = row?.provider_id ? providerName(row.provider_id) : "";
    if (!row?.account_id) return provider;
    const label = accountLabel(row.account_id);
    return provider && !label.startsWith(provider) ? `${provider} · ${label}` : label;
  };
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
  // F01/F02: a strategy has one row per lens x surface (x source), and those rows disagree.  The
  // Portfolio view reduces all of them by a declared rule, never by whichever row happens to be
  // listed first: validated if any row is validated, otherwise diagnostic-only if any is, and the
  // counts are always stated so a diagnostic-only or unavailable surface is not hidden.  A lens
  // that publishes surfaces is read through them; its lens-level series repeats its default surface.
  const factorRowsForStrategy = (strategyName) => {
    const rows = [];
    (factorLensModes.LIVE_ONLY || activeFactorLenses()).forEach((lens) => {
      const surfaces = lens?.surfaces && typeof lens.surfaces === "object" ? Object.values(lens.surfaces) : [lens];
      surfaces.forEach((surface) => (surface?.series || []).forEach((item) => { if (item.series_id === strategyName) rows.push(item); }));
    });
    return rows;
  };
  const factorModelSummary = (strategyName) => {
    const rows = factorRowsForStrategy(strategyName);
    const validated = rows.filter((item) => item.status === "AVAILABLE").length;
    const diagnostic = rows.filter((item) => item.status === "DIAGNOSTIC_ONLY").length;
    const unavailable = rows.length - validated - diagnostic;
    const state = !rows.length ? "MODEL_UNAVAILABLE" : validated ? "MODEL_VALIDATED_FOR_PM_USE" : diagnostic ? "MODEL_COMPUTED_NOT_VALIDATED" : "MODEL_UNAVAILABLE";
    const note = rows.length
      ? `PM-validated on ${integer.format(validated)} of ${integer.format(rows.length)} lens/surface rows; diagnostic only ${integer.format(diagnostic)}; unavailable ${integer.format(unavailable)}`
      : "No lens or surface publishes this strategy";
    return { state, note, validated, diagnostic, unavailable, total: rows.length };
  };
  const sourceState = (row) => {
    const strategy = strategyByName(row.series_id);
    const available = number(row.coverage?.aligned_observations) > 0
      || strategy?.account_live_status === "AVAILABLE"
      || strategy?.nav_status === "AVAILABLE"
      || Boolean(strategy?.provider_evidence?.provider || strategy?.provider_evidence?.account);
    return available ? "SOURCE_DATA_AVAILABLE" : "SOURCE_DATA_UNAVAILABLE";
  };

  function renderCommand() {
    $("#command-heading").textContent = performanceLineageMismatch ? "REPORT PERFORMANCE WITHHELD" : data.advisory.readiness.replaceAll("_", " ");
    $("#command-reason").textContent = performanceLineageMismatch
      ? `UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${performanceLineageReason}`
      : data.advisory.reason;
    const liquidNote = capitalState("total_liquid").ready ? `Systematic plus reserve plus retained excluded${capitalAsOf("total_liquid")}` : capitalNotReadyNote();
    const reserveNote = capitalState("reserve").ready ? `Zero strategy risk budget${capitalAsOf("reserve")}` : "Not ready at the cutoff · zero strategy risk budget";
    renderKpis("#command-kpis", performanceLineageMismatch ? [
      ["Total liquid", capitalAmount("total_liquid"), liquidNote],
      ["Systematic capital", capitalAmount("systematic"), "Report-derived risk-control status withheld"],
      ["Reserve", capitalAmount("reserve"), reserveNote, "reserve"],
      ["Report coverage", "—", `UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${performanceLineageReason}`, "danger"],
    ] : [
      ["Total liquid", capitalAmount("total_liquid"), liquidNote],
      ["Systematic capital", capitalAmount("systematic"), `Subject to the accepted HWM policy${capitalAsOf("systematic")}`],
      ["Reserve", capitalAmount("reserve"), reserveNote, "reserve"],
      ["Governed NAV coverage", pct(data.advisory.nav_coverage), `${currencyExact(data.hwm_summary.covered_value_usd)} covered of ${currencyExact(data.hwm_summary.governed_perimeter_value_usd)}; ${currencyExact(data.hwm_summary.uncovered_value_usd)} uncovered. ${hwmExclusionNote()}`, "warning"],
    ]);

    const gateRows = performanceLineageMismatch ? [
      `<div class="gate-row is-danger"><div class="gate-name">Daily NAV gate<small>UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${safe(performanceLineageReason)}</small></div><div class="gate-value">${statusText("UNAVAILABLE_SOURCE_LINEAGE_MISMATCH")}</div></div>`,
    ] : data.gates.map((gate) => {
      const key = `${gate.item || ""} ${gate.label || ""}`.toLowerCase();
      const flowProviders = flowProviderList(gate.note);
      const summary = key.includes("position")
        ? positionGateSummary()
        : key.includes("cash flow")
          ? (flowProviders ? `Accepted ${flowProviders} flow coverage is partial; full disposition remains in Data health.` : operatorText(gate.note))
          : operatorText(gate.note);
      return `<div class="gate-row is-${toneForStatus(gate.status)}"><div class="gate-name">${safe(gate.label)}<small>${safe(summary)}</small></div><div class="gate-value">${statusText(gate.status)}</div></div>`;
    });
    $("#gate-stack").innerHTML = gateRows.join("");

    $("#capital-ring").innerHTML = capitalRingMarkup();

    $("#strategy-coverage-bars").innerHTML = data.strategies.map((row) => {
      const qualification = hwmQualification(row);
      if (performanceLineageMismatch) {
        return `<div class="coverage-status-row"><div class="bar-label"><span>${safe(row.strategy_name)}</span><span>Evidence UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · HWM UNAVAILABLE_SOURCE_LINEAGE_MISMATCH</span></div><div class="cell-secondary">— ${safe(performanceLineageReason)}</div></div>`;
      }
      return `<div class="coverage-status-row"><div class="bar-label"><span>${safe(row.strategy_name)}</span><span>Evidence ${statusText(row.nav_status)} · HWM ${statusText(qualification.status)}</span></div><div class="cell-secondary">${safe(qualification.note)} · ${integer.format(row.available_twr_segments || 0)} coverage-qualified TWR segments · systematic weight ${pct(row.systematic_weight)}</div></div>`;
    }).join("");
    const currentRows = currentPositionRows();
    const currentMapped = currentRows.filter((row) => row.status === "AVAILABLE_MAPPED").length;
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
    const operatorItems = performanceLineageMismatch ? [
      `<li><div><strong>Report performance lineage</strong><span>UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${safe(performanceLineageReason)}</span><button type="button" class="text-action" data-open-view="risk" aria-label="Inspect withheld report evidence">Inspect withheld evidence →</button></div></li>`,
    ] : data.blocking_items.map((item) => {
      const note = item.item === "current-strategy-attribution-partial"
        ? `${currentMapped}/${currentRows.length} current positions are strategy-mapped; other current General intervals remain valid at the systematic-pool layer only. ${operatorText(item.known_gap)}`
        : operatorText(item.known_gap);
      const route = remediationRoute[item.item] || { view: "data", label: "Inspect data health" };
      const focus = route.view === "data" ? ` data-focus-coverage="${safe(item.item)}"` : "";
      return `<li><div><strong>${safe(operatorLabels[item.item] || humanStatus(item.item))}</strong><span>${safe(note)}</span><button type="button" class="text-action" data-open-view="${safe(route.view)}"${focus} aria-label="${safe(route.label)} for ${safe(humanStatus(item.item))}">${safe(route.label)} →</button></div></li>`;
    }).join("");
    $("#operator-queue").innerHTML = Array.isArray(operatorItems) ? operatorItems.join("") : operatorItems;
    const implementationRows = data.strategies.map((row) => {
      const factor = factorModelSummary(row.strategy_name);
      return `<tr><td>${safe(row.strategy_name)}</td><td>${evidenceAvailability(row)}</td><td>${statusText(row.implementation_gap_status)}</td><td>${safe(humanStatus(row.cost_status))}</td><td>${statusText(factor.state)}<span class="cell-secondary">${safe(factor.note)}</span></td></tr>`;
    });
    $("#portfolio-implementation-table").innerHTML = tableMarkup([["Strategy"], ["Source evidence"], ["Implementation gap (account vs model)"], ["Cost"], ["Factor model"]], implementationRows, evidenceCaption("Implementation, costs, and model readiness"));
    const constructionPoints = performanceLineageMismatch
      ? [
        `UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${performanceLineageReason}`,
        "Report-derived strategy coverage and HWM qualification are withheld.",
        "Provider, predecessor, shadow, backtest, and account-live evidence remain separate.",
      ]
      : [
        `Correlation-one stress is ${pct(data.advisory.correlation_one_stress)} against a ${pct(data.advisory.hwm_limit)} systematic HWM boundary (weights: ${stressWeightLabel()}).`,
        `${data.counts.twr_available} of ${data.counts.twr_rows} account-strategy segments have coverage-aware TWR.`,
        "Provider, predecessor, shadow, backtest, and account-live evidence remain separate.",
      ];
    $("#construction-points").innerHTML = constructionPoints.map((point) => `<div class="thesis-point">${safe(point)}</div>`).join("");
  }

  let allocationFilter = "all";
  let positionSearch = "";
  let positionPage = 0;
  const positionPageSize = 50;
  // N65: a dated snapshot's weight is its value over the snapshot's own recorded systematic
  // perimeter (not the current seven), and the row says which denominator that is.
  const allocationPerimeterNote = (row) => {
    if (row.weight_perimeter !== "SNAPSHOT_RECORDED_SYSTEMATIC_PERIMETER" || row.systematic_weight == null) return "";
    const components = number(row.weight_perimeter_component_count);
    const detail = [
      components == null ? null : `${integer.format(components)} components`,
      number(row.weight_denominator_usd) == null ? null : currency(row.weight_denominator_usd),
    ].filter(Boolean).join(", ");
    return `<span class="cell-secondary">Weight is the share of the snapshot's recorded systematic perimeter${detail ? ` (${detail})` : ""}</span>`;
  };

  // F06/F22: the reserve at the cutoff.  The custody current-capital block is read when the
  // payload carries it; otherwise the panel's own capital block gives the same status and date.
  // A reserve that is not ready is shown as not ready, never sized.
  const reserveCurrentState = () => {
    const custody = data.custody_current_capital?.reserve;
    if (custody && typeof custody === "object") {
      const raw = custody.current_value && typeof custody.current_value === "object" ? custody.current_value.value : custody.value;
      const value = number(raw) == null ? null : raw;
      return { status: custody.status ?? null, value, ready: custody.status === "AVAILABLE" && value != null };
    }
    const state = capitalState("reserve");
    return { status: state.status, value: state.value, ready: state.ready };
  };
  const allocationCurrent = (row) => {
    if (row.scope === "RESERVE") {
      const reserve = reserveCurrentState();
      return { value: reserve.ready ? number(reserve.value) : null, weight: null, status: reserve.ready ? null : reserve.status || "NOT_READY" };
    }
    const strategy = strategyByName(row.strategy_name);
    return strategy ? { value: number(strategy.current_value_usd), weight: number(strategy.systematic_weight), status: null } : { value: null, weight: null, status: null };
  };
  const sumOf = (values) => values.reduce((total, value) => total + value, 0);

  function renderAllocationTable() {
    const accepted = data.allocations.filter((row) => (allocationFilter === "all" || row.scope === allocationFilter)
      && (selectedContextStrategy === "ALL" || row.strategy_name === selectedContextStrategy || row.strategy_id === selectedContextStrategy));
    const snapshotDates = [...new Set(accepted.map((row) => String(row.as_of_date || "").slice(0, 10)).filter(Boolean))].sort();
    const snapshotLabel = snapshotDates.length === 1 ? formatDate(snapshotDates[0]) : "per-row date";
    const currents = accepted.map(allocationCurrent);
    const rows = accepted.map((row, index) => {
      const target = targetState(row);
      const current = currents[index];
      const currentValue = current.value != null ? currency(current.value) : current.status ? statusText(current.status) : "—";
      const identity = "Reserve classification";
      return `<tr><td><span class="cell-primary">${safe(row.strategy_name)}</span>${row.strategy_id ? "" : `<span class="cell-secondary">${safe(identity)}</span>`}</td><td>${statusText(row.scope)}</td><td class="numeric">${currency(row.current_value_usd)}</td><td class="numeric">${pct(row.systematic_weight)}</td><td class="numeric">${currentValue}</td><td class="numeric">${pct(current.weight)}</td><td class="numeric">${pct(target.target)}</td><td class="numeric">${pct(target.drift)}</td><td>${statusText(target.status)}</td><td>${formatDateHtml(row.as_of_date)}${staleSuffix(row.as_of_date)}<span class="cell-secondary">${safe(weightBasisText(row.weight_basis))}</span>${allocationPerimeterNote(row)}</td></tr>`;
    });
    // F20: a total only for the additive columns, from the rows shown.  A current value that is
    // not ready is left out of its total and the footer says how many.
    const snapshotValues = accepted.map((row) => number(row.current_value_usd)).filter((value) => value != null);
    const snapshotWeights = accepted.map((row) => number(row.systematic_weight)).filter((value) => value != null);
    const currentValues = currents.map((item) => item.value).filter((value) => value != null);
    const currentWeights = currents.map((item) => item.weight).filter((value) => value != null);
    const notReady = accepted.length - currentValues.length;
    const footer = accepted.length
      ? `<tr><th scope="row" colspan="2">Total</th><td class="numeric">${snapshotValues.length ? currency(sumOf(snapshotValues)) : "—"}</td><td class="numeric">${snapshotWeights.length ? pct(sumOf(snapshotWeights)) : "—"}</td><td class="numeric">${currentValues.length ? currency(sumOf(currentValues)) : "—"}${notReady ? `<span class="cell-secondary">excludes ${integer.format(notReady)} not ready</span>` : ""}</td><td class="numeric">${currentWeights.length ? pct(sumOf(currentWeights)) : "—"}</td><td colspan="4"></td></tr>`
      : "";
    const table = $("#allocation-table");
    table.dataset.visibleRows = String(accepted.length);
    table.innerHTML = tableMarkup([["Strategy / identity"], ["Scope"], [`Snapshot value (as of ${snapshotLabel})`, "numeric"], [`Snapshot weight (as of ${snapshotLabel})`, "numeric"], ["Current value (cutoff)", "numeric"], ["Current weight", "numeric"], ["Accepted target", "numeric"], ["Drift", "numeric"], ["Target state"], ["As of"]], rows.length ? rows : [emptyRow(10)], null, footer);
    const note = $("#allocation-snapshot-note");
    if (note) {
      const gaps = snapshotDates.map(daysBeforeCutoff).filter((days) => days > 0);
      note.textContent = !snapshotDates.length ? "" : gaps.length
        ? `Snapshot value and weight are dated ${snapshotDates.map(formatDate).join(", ")}, up to ${integer.format(Math.max(...gaps))} days before the ${formatDate(data.meta?.portfolio_as_of)} cutoff; current value and weight are the cutoff figures.`
        : `Snapshot dates ${snapshotDates.map(formatDate).join(", ")} are not before the ${formatDate(data.meta?.portfolio_as_of)} cutoff.`;
    }
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

  // E1: the producer states why a position has no symbol.  A not-ready symbol says so in
  // words (reason as the tooltip); a row that is not an accepted position claims nothing.
  const symbolCell = (row) => {
    const identity = `<span class="cell-secondary">${safe(row.security_id || "—")}</span>`;
    if (row.symbol_status === "NOT_READY") {
      const reason = typeof row.symbol_reason === "string" && row.symbol_reason ? ` title="${safe(row.symbol_reason)}"` : "";
      return `<span class="cell-primary symbol-not-ready"${reason}>Symbol not ready</span>${identity}`;
    }
    return `<span class="cell-primary">${safe(row.symbol || "—")}</span>${identity}`;
  };

  function renderPositionTable() {
    const query = positionSearch.toLowerCase();
    const accepted = currentPositionRows().filter((row) => matchesContext(row)
      && (!query || `${row.strategy_name || ""} ${row.symbol || ""} ${row.security_id || ""} ${accountLabel(row.account_id)}`.toLowerCase().includes(query)));
    const pageCount = Math.max(1, Math.ceil(accepted.length / positionPageSize));
    positionPage = Math.min(positionPage, pageCount - 1);
    const visible = accepted.slice(positionPage * positionPageSize, (positionPage + 1) * positionPageSize);
    const positionRows = visible.map((row) => `<tr data-account-id="${safe(row.account_id)}"><td><span class="cell-primary">${safe(row.strategy_name || "UNMAPPED")}</span><span class="cell-secondary">${safe(row.strategy_id || "No accepted strategy")}</span></td><td>${symbolCell(row)}</td><td>${safe(accountLabel(row.account_id))}</td><td class="numeric">${safe(row.quantity ?? "—")}</td><td class="numeric">${currency(row.market_value_usd)}</td><td>${formatDateHtml(row.as_of_date)}</td><td>${statusText(row.status)}</td></tr>`);
    const positionTable = $("#position-table");
    positionTable.dataset.visibleRows = String(accepted.length);
    positionTable.innerHTML = tableMarkup([["Strategy / identity"], ["Security"], ["Account"], ["Quantity (units)", "numeric"], ["Market value", "numeric"], ["As of"], ["Status"]], positionRows.length ? positionRows : [emptyRow(7)]);
    $("#position-summary").textContent = `${integer.format(accepted.length)} matching`;
    const historyNote = positionHistoryNote();
    $("#position-history-note").textContent = historyNote;
    $("#position-history-note").hidden = historyNote === "";
    $("#position-page-status").textContent = `Page ${positionPage + 1} of ${pageCount}`;
    $("#position-page-previous").disabled = positionPage === 0;
    $("#position-page-next").disabled = positionPage >= pageCount - 1;
  }

  function positionKpiNote() {
    const snapshots = positionSnapshots();
    const count = integer.format(data.counts.positions || 0);
    if (!snapshots) return `${count} accepted position rows (snapshot dates not classified)`;
    return `${count} current rows at ${formatDate(snapshots.cutoff_date)} · ${integer.format(snapshots.historical_rows || 0)} historical rows kept as history`;
  }

  function renderCapitalKpis() {
    renderKpis("#capital-kpis", [
      ["Liquid capital", capitalAmount("total_liquid"), capitalState("total_liquid").ready ? `Current at the cutoff${capitalAsOf("total_liquid")}` : capitalNotReadyNote()],
      ["Current strategies", String(data.counts.strategies), "Reserve is not a strategy"],
      ["Accounts", String(data.counts.accounts), "Stable internal identities"],
      ["Positions", humanStatus(data.fact_status.positions), data.counts.positions ? positionKpiNote() : "No accepted position source", data.counts.positions ? "warning" : "danger"],
    ]);
  }

  function renderCapital() {
    renderCapitalKpis();
    renderAllocationTable();
    renderInvestedCapital();
    const providers = Object.entries(data.provider_counts).sort((a, b) => b[1] - a[1]);
    const maxCount = Math.max(...providers.map(([, count]) => count), 1);
    $("#provider-bars").innerHTML = providers.map(([provider, count]) => `<div><div class="bar-label"><span>${safe(providerName(provider))}</span><strong>${integer.format(count)}</strong></div><div class="bar-track"><div class="bar-fill" style="width:${count / maxCount * 100}%"></div></div></div>`).join("");
    const rows = data.strategies.map((row) => {
      const qualification = hwmQualification(row);
      const navStatus = performanceLineageMismatch ? "UNAVAILABLE_SOURCE_LINEAGE_MISMATCH" : row.nav_status;
      return `<tr><td><span class="cell-primary">${safe(row.strategy_name)}</span><span class="cell-secondary">${safe(row.strategy_id)} · version ${statusText(row.version_disposition || "UNRESOLVED")}</span></td><td>${safe(row.economic_family)}</td><td class="numeric">${currency(row.current_value_usd)}</td><td class="numeric">${pct(row.systematic_weight)}</td><td><span class="cell-primary">Evidence ${statusText(navStatus)}</span><span class="cell-secondary">HWM ${statusText(qualification.status)} · ${safe(qualification.note)}</span><span class="cell-secondary">Design ${statusText(row.design_status)} · backtest ${statusText(row.backtest_status)} · live boundary ${statusText(row.live_boundary_status)}</span>${row.provider_evidence ? `<span class="cell-secondary">${safe(evidenceCoverage(row))}</span>` : ""}</td><td>${costDisclosure(row)}</td><td>${exactComparison(row)}<span class="cell-secondary">Version compatibility ${statusText(row.version_compatibility_status)}</span>${benchmarkDisclosure(row)}</td></tr>`;
    });
    $("#strategy-table").innerHTML = tableMarkup([["Strategy / identity"], ["Economic family"], ["Value", "numeric"], ["Weight", "numeric"], ["NAV and evidence coverage"], ["Cost"], ["Provider/model comparison"]], rows, evidenceCaption("Current strategy evidence"));
    renderLifecycleTable();
    renderPositionTable();
    renderLeverageDisclosures();
  }

  function renderInvestedCapital() {
    const capital = data.invested_capital;
    if (!capital) {
      $("#invested-capital-status").textContent = "Not supplied";
      renderKpis("#invested-capital-kpis", [["Current invested capital", "—", "A verified governed result was not supplied", "warning"]]);
      $("#invested-capital-current-table").innerHTML = tableMarkup([["Strategy"], ["Invested capital", "numeric"]], [emptyRow(2)]);
      $("#invested-capital-daily-table").innerHTML = tableMarkup([["Date"], ["Strategy"], ["Invested capital", "numeric"]], [emptyRow(3)]);
      return;
    }
    $("#invested-capital-status").innerHTML = statusText(capital.status);
    renderKpis("#invested-capital-kpis", [
      ["Current invested capital", currency(capital.current_aggregate_usd), `Complete through ${formatDate(capital.complete_through)}`],
      ["Daily history", integer.format(capital.daily_aggregate.length), "Verified portfolio-total observations"],
      ["Authority", humanStatus(capital.authority_profile), `Policy ${safe(capital.policy_id)}`],
    ]);
    const currentRows = [
      ...capital.current_by_strategy.map((row) => `<tr><td>${safe(strategyNameById(row.strategy_id))}</td><td class="numeric">${currency(row.invested_capital_usd)}</td></tr>`),
      `<tr><td><strong>Portfolio total</strong></td><td class="numeric"><strong>${currency(capital.current_aggregate_usd)}</strong></td></tr>`,
    ];
    $("#invested-capital-current-table").innerHTML = tableMarkup([["Strategy"], ["Invested capital", "numeric"]], currentRows);
    const selectedStrategyId = selectedContextStrategy === "ALL"
      ? null
      : (strategyByName(selectedContextStrategy)?.strategy_id ?? selectedContextStrategy);
    const selectedRows = selectedStrategyId === null ? [] : capital.daily_by_strategy.filter((row) => row.strategy_id === selectedStrategyId);
    const dailyRows = [
      ...capital.daily_aggregate.map((row) => `<tr><td>${safe(formatDate(row.date))}</td><td>Portfolio total</td><td class="numeric">${currency(row.invested_capital_usd)}</td></tr>`),
      ...selectedRows.map((row) => `<tr><td>${safe(formatDate(row.date))}</td><td>${safe(strategyNameById(row.strategy_id))}</td><td class="numeric">${currency(row.invested_capital_usd)}</td></tr>`),
    ];
    $("#invested-capital-daily-table").innerHTML = tableMarkup([["Date"], ["Strategy"], ["Invested capital", "numeric"]], dailyRows);
  }

  // The report has always carried leverage_disclosures and the panel payload has always
  // forwarded them; nothing read them. An unproved leverage FACT fails hard, but an
  // unrendered leverage DISCLOSURE failed silently, which is precisely the silently omitted
  // sink the operating model forbids. Rendered state-faithfully: an empty list means the
  // measure was not evaluated rather than that it is zero, an unavailable amount keeps the
  // source's own reason token, and nothing is aggregated across data classes.
  // F05: the detail table is paged like the positions table; the coverage table counts every row.
  const LEVERAGE_PAGE_SIZE = 250;
  let leveragePage = 0;

  // D6: the report labels each account row with its role.  A product account is the
  // unmarked case; a reserve account or one with no current assignment is named
  // beside the account so a history-only account cannot read as a current one.
  const ACCOUNT_ROLE_NOTES = {
    RESERVE: "Reserve account",
    HISTORY_ONLY_NO_CURRENT_ASSIGNMENT: "History only · no current assignment",
  };
  function accountRoleNote(row) {
    const role = row && row.account_role;
    if (!role || role === "PRODUCT") return "";
    const label = ACCOUNT_ROLE_NOTES[role] || humanStatus(role);
    return `<span class="cell-secondary" data-account-role="${safe(role)}">${safe(label)}</span>`;
  }

  function renderLeverageDisclosures() {
    const rows = Array.isArray(data.leverage_disclosures) ? data.leverage_disclosures : [];
    const status = $("#leverage-status");
    const truncation = $("#leverage-truncation");
    if (!rows.length) {
      status.textContent = "Not evaluated";
      status.className = "badge badge-warning";
      renderKpis("#leverage-kpis", [[
        "Leverage observations",
        "—",
        "No leverage disclosure was produced; this is not a zero exposure",
        "warning",
      ]]);
      $("#leverage-coverage-table").innerHTML = tableMarkup([["Data class"], ["Coverage"], ["Observations", "numeric"]], [emptyRow(3)]);
      $("#leverage-table").innerHTML = tableMarkup([["Data class"], ["Provider / account"], ["Strategy"], ["Amount (USD or source status)", "numeric"], ["Coverage and reason"]], [emptyRow(5)]);
      truncation.textContent = "";
      const emptyPager = $("#leverage-pagination");
      if (emptyPager) emptyPager.hidden = true;
      return;
    }
    const observed = rows.filter((row) => row.coverage_status === "OBSERVED_ONLY");
    const unavailable = rows.filter((row) => row.coverage_status !== "OBSERVED_ONLY");
    // F43: the badge says how many rows were observed and how many were not, never "observed only".
    status.textContent = unavailable.length ? `Observed ${integer.format(observed.length)} / unavailable ${integer.format(unavailable.length)}` : "Observed";
    status.className = unavailable.length ? "badge badge-warning" : "badge badge-positive";
    const classes = [...new Set(rows.map((row) => row.data_class))].sort();
    renderKpis("#leverage-kpis", [
      ["Observations", integer.format(rows.length), `${classes.length} data class${classes.length === 1 ? "" : "es"} declared`],
      ["Observed measures", integer.format(observed.length), "Source-observed, never completed by inference", observed.length ? "positive" : "warning"],
      ["Unavailable measures", integer.format(unavailable.length), "Carried with the source reason rather than zero", unavailable.length ? "warning" : "positive"],
    ]);
    const coverage = new Map();
    for (const row of rows) {
      const key = JSON.stringify([row.data_class, row.coverage_status, row.coverage_reason || ""]);
      coverage.set(key, (coverage.get(key) || 0) + 1);
    }
    const coverageRows = [...coverage.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([key, count]) => {
      const [dataClass, coverageStatus, reason] = JSON.parse(key);
      return `<tr><td>${safe(humanStatus(dataClass))}</td><td><span class="cell-primary">${statusText(coverageStatus)}</span>${reason ? `<span class="cell-secondary">${safe(reason)}</span>` : ""}</td><td class="numeric">${integer.format(count)}</td></tr>`;
    });
    $("#leverage-coverage-table").innerHTML = tableMarkup([["Data class"], ["Coverage"], ["Observations", "numeric"]], coverageRows);
    const pageCount = Math.max(1, Math.ceil(rows.length / LEVERAGE_PAGE_SIZE));
    leveragePage = Math.min(leveragePage, pageCount - 1);
    const shown = rows.slice(leveragePage * LEVERAGE_PAGE_SIZE, (leveragePage + 1) * LEVERAGE_PAGE_SIZE);
    const detailRows = shown.map((row) => {
      const amount = row.amount && typeof row.amount === "object" ? row.amount : null;
      const measure = amount && amount.status === "AVAILABLE" && amount.value !== null
        ? currencyExact(amount.value)
        : statusText(amount ? amount.status : "UNAVAILABLE");
      const account = accountProviderText(row);
      return `<tr><td><span class="cell-primary">${safe(humanStatus(row.data_class))}</span>${row.measurement_basis ? `<span class="cell-secondary">${safe(humanStatus(row.measurement_basis))}</span>` : ""}</td><td>${safe(account || "—")}${accountRoleNote(row)}</td><td>${safe(row.strategy_id ? strategyNameById(row.strategy_id) : "—")}</td><td class="numeric">${measure}</td><td><span class="cell-primary">${statusText(row.coverage_status)}</span>${row.reason ? `<span class="cell-secondary">${safe(row.reason)}</span>` : ""}${row.included_in_nav === false ? `<span class="cell-secondary">Not included in NAV</span>` : ""}</td></tr>`;
    });
    $("#leverage-table").innerHTML = tableMarkup([["Data class"], ["Provider / account"], ["Strategy"], ["Amount (USD or source status)", "numeric"], ["Coverage and reason"]], detailRows);
    const firstShown = leveragePage * LEVERAGE_PAGE_SIZE + 1;
    truncation.textContent = rows.length > shown.length
      ? `Showing ${integer.format(firstShown)}–${integer.format(firstShown + shown.length - 1)} of ${integer.format(rows.length)} observations; the coverage table above counts every one.`
      : "";
    const pager = $("#leverage-pagination");
    if (pager) pager.hidden = pageCount <= 1;
    $("#leverage-page-status").textContent = `Page ${leveragePage + 1} of ${pageCount}`;
    $("#leverage-page-previous").disabled = leveragePage === 0;
    $("#leverage-page-next").disabled = leveragePage >= pageCount - 1;
  }

  function renderPerformanceComparison() {
    if (performanceLineageMismatch) {
      $("#performance-comparison").innerHTML = `<div class="metric-block"><div class="metric-label">Account context</div><div class="metric-value metric-value-compact">${safe(selectedContextAccount === "ALL" ? "All accounts" : accountLabel(selectedContextAccount))}</div><div class="metric-note">Context only; report performance is withheld</div></div><div class="metric-block"><div class="metric-label">Strategy context</div><div class="metric-value metric-value-compact">${safe(selectedContextStrategy === "ALL" ? "Select one strategy" : selectedContextStrategy)}</div><div class="metric-note">Context only; report performance is withheld</div></div><div class="metric-block"><div class="metric-label">${COMPARISON_WEEKS}-week strategy return</div><div class="metric-value">—</div><div class="metric-note">Report performance withheld due to source-lineage mismatch</div></div><div class="metric-block"><div class="metric-label">Benchmark</div><div class="metric-value">—</div><div class="metric-note">Report performance withheld due to source-lineage mismatch</div></div><div class="metric-block"><div class="metric-label">Relative return</div><div class="metric-value">—</div><div class="metric-note">Report performance withheld due to source-lineage mismatch</div></div>`;
      return;
    }
    const weeklyData = data.weekly_returns;
    const strategyRow = selectedContextStrategy === "ALL" ? null : weeklyData?.series?.find((row) => row.primary_group === "STRATEGY" && (row.series_id === selectedContextStrategy || row.display_name === selectedContextStrategy));
    const benchmarkRow = weeklyData?.series?.find((row) => row.primary_group === "CORE_BENCHMARK" || row.display_groups?.includes("CORE_BENCHMARK"));
    const completedWeeks = (weeklyData?.weeks || []).filter((week) => week !== weeklyData.current_week?.week_start).slice(-COMPARISON_WEEKS);
    const cumulative = (row) => {
      if (!row) return null;
      const values = completedWeeks.map((week) => row.primary_group === "STRATEGY" ? row.values_by_mode?.LIVE_ONLY?.[week] : row.values?.[week]);
      if (!values.length || values.some((value) => number(value) == null)) return null;
      return values.reduce((wealth, value) => wealth * (1 + Number(value)), 1) - 1;
    };
    const strategyReturn = cumulative(strategyRow);
    const benchmarkReturn = cumulative(benchmarkRow);
    const relative = strategyReturn == null || benchmarkReturn == null ? null : strategyReturn - benchmarkReturn;
    $("#performance-comparison").innerHTML = `<div class="metric-block"><div class="metric-label">Account context</div><div class="metric-value metric-value-compact">${safe(selectedContextAccount === "ALL" ? "All accepted accounts" : accountLabel(selectedContextAccount))}</div><div class="metric-note">Persistent context; external account numbers are not shown</div></div><div class="metric-block"><div class="metric-label">Strategy context</div><div class="metric-value metric-value-compact">${safe(strategyRow?.display_name || (selectedContextStrategy === "ALL" ? "Select one strategy" : selectedContextStrategy))}</div><div class="metric-note">Account-live source remains separate from model acceptance</div></div><div class="metric-block"><div class="metric-label">${COMPARISON_WEEKS}-week strategy return</div><div class="metric-value">${pct(strategyReturn)}</div><div class="metric-note">${strategyReturn == null ? "Unavailable unless every selected live-only week is accepted" : "Compounded accepted live-only weeks"}</div></div><div class="metric-block"><div class="metric-label">Benchmark</div><div class="metric-value">${pct(benchmarkReturn)}</div><div class="metric-note">${safe(benchmarkRow?.display_name || "No accepted core benchmark")}</div></div><div class="metric-block"><div class="metric-label">Relative return</div><div class="metric-value">${pct(relative)}</div><div class="metric-note">Arithmetic difference over the same completed weeks</div></div>`;
  }

  // F08: a governed HWM proof publishes its whole daily index path.  The panel shows what an
  // operator reads from it: the point count and date range, the last point, the high-water point
  // and the lowest point.  The values are the payload's own strings.
  const hwmPathSummary = (proof) => {
    const path = Array.isArray(proof?.index_path) ? proof.index_path : [];
    if (!path.length) return "no index path published";
    const pointDate = (point) => point?.date || point?.as_of_date || "";
    const indexOf = (point) => Number(point.index);
    let high = path[0];
    let low = path[0];
    path.forEach((point) => {
      if (indexOf(point) > indexOf(high)) high = point;
      if (indexOf(point) < indexOf(low)) low = point;
    });
    const last = path[path.length - 1];
    const range = pointDate(path[0]) && pointDate(last) ? ` ${safe(pointDate(path[0]))}..${safe(pointDate(last))}` : "";
    const at = (point) => (pointDate(point) ? ` on ${safe(pointDate(point))}` : "");
    return `${integer.format(path.length)} daily points${range}; last ${safe(last.index)}${at(last)}; high-water point ${safe(high.index)}${at(high)}; lowest point ${safe(low.index)}${at(low)}`;
  };
  // F08: how old the calculation is against the age the policy allows.
  const hwmAgeText = (proof) => {
    const age = number(proof?.calculation_age_days);
    const allowed = number(proof?.maximum_age_days);
    if (age == null) return "age not published";
    return `${integer.format(age)} days old${allowed == null ? "" : ` (maximum ${integer.format(allowed)})`}`;
  };
  // F10/F09: the date a second sleeve is first funded, from the invested-capital history.  Before
  // it the perimeter has one funded sleeve, so a path that starts earlier describes that sleeve.
  const sleeveFundingStarts = () => {
    const rows = Array.isArray(data.invested_capital?.daily_by_strategy) ? data.invested_capital.daily_by_strategy : [];
    const first = new Map();
    rows.forEach((row) => {
      const day = String(row.date || "").slice(0, 10);
      if (!day || !(number(row.invested_capital_usd) > 0)) return;
      if (!first.has(row.strategy_id) || day < first.get(row.strategy_id)) first.set(row.strategy_id, day);
    });
    return [...first.entries()].sort((left, right) => left[1].localeCompare(right[1]) || String(left[0]).localeCompare(String(right[0])));
  };
  const perimeterStartText = (proof) => {
    const starts = sleeveFundingStarts();
    if (starts.length < 2 || starts[1][1] <= starts[0][1]) return "";
    const pathStart = String(proof?.index_path?.[0]?.date || proof?.index_path?.[0]?.as_of_date || "").slice(0, 10);
    if (pathStart && pathStart >= starts[1][1]) return "";
    return `Perimeter start: only ${strategyNameById(starts[0][0])} is funded from ${starts[0][1]} until ${starts[1][1]}, when ${strategyNameById(starts[1][0])} is first funded; the index path before ${starts[1][1]} describes one sleeve, not the ${integer.format(starts.length)}-sleeve perimeter.`;
  };
  // F10: the policy ids the HWM figures cite, named side by side so a mismatch is visible.
  const hwmPolicyText = (proof, candidate) => {
    const proofPolicy = proof?.policy_id || candidate?.policy_id || null;
    const summaryPolicy = data.hwm_summary?.policy_id || null;
    if (!proofPolicy && !summaryPolicy) return "";
    const differ = proofPolicy && summaryPolicy && proofPolicy !== summaryPolicy;
    return `Policy ids: governed proof ${proofPolicy || "not published"}; HWM summary ${summaryPolicy || "not published"}${differ ? "; the two policy ids differ" : ""}.`;
  };
  const STRESS_LIMIT_POSITION = 0.375;

  function renderRisk() {
    const includedHwm = data.strategies.filter((row) => row.hwm_scope === "INCLUDED").map((row) => ({ row, qualification: hwmQualification(row) }));
    const unavailableHwm = includedHwm.filter(({ qualification }) => qualification.status !== "AVAILABLE_HWM_QUALIFIED");
    const excludedNav = data.strategies.filter((row) => row.hwm_scope === "EXCLUDED_DE_MINIMIS").map((row) => row.strategy_name);
    const acceptedHwmEvidence = includedHwm.filter(({ qualification }) => qualification.status === "AVAILABLE_HWM_QUALIFIED").map(({ row }) => row);
    // N67: while the producer publishes a governed PORT-HWM-4 candidate the numeric HWM is
    // not accepted (its register reason says why) and the candidate is shown labelled; the
    // sentence "no numeric HWM is calculated" is only true without a candidate.
    const hwmCandidate = data.advisory.hwm_candidate && typeof data.advisory.hwm_candidate === "object" ? data.advisory.hwm_candidate : null;
    const hwmNotAccepted = hwmCandidate
      ? `Numeric HWM not accepted (${hwmCandidate.not_accepted_register || "register"}: ${humanStatus(hwmCandidate.not_accepted_reason || data.advisory.hwm_numerical_reason).toLowerCase()}). Governed ${hwmCandidate.policy_id || data.hwm_governed_proof?.policy_id || "HWM"} candidate shown, pending owner acceptance`
      : "";
    const hwmNumericSentence = hwmCandidate ? hwmNotAccepted : "no numeric HWM is calculated while unavailable";
    // F10: the KPI is the numerical HWM's own status; its tone follows that status, not the perimeter's.
    const hwmKpiTone = data.advisory.hwm_numerical_status
      ? ({ danger: "danger", warning: "warning", positive: "positive" })[toneForStatus(data.advisory.hwm_numerical_status)] || ""
      : unavailableHwm.length ? "danger" : "positive";
    renderKpis("#risk-kpis", performanceLineageMismatch ? [
      ["Correlation-one stress", pct(data.advisory.correlation_one_stress), "Stress scenario", "danger"],
      ["Systematic HWM", "—", `UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${performanceLineageReason}`, "danger"],
      ["TWR evidence", "—", `UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${performanceLineageReason}`, "danger"],
      ["MWR evidence", "—", `UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${performanceLineageReason}`, "danger"],
    ] : [
      ["Correlation-one stress", pct(data.advisory.correlation_one_stress), "Accepted adverse scenario", "danger"],
      ["Systematic HWM", humanStatus(data.advisory.hwm_numerical_status || data.advisory.hwm_status), `${currencyExact(data.hwm_summary.uncovered_value_usd)} across ${unavailableHwm.length} unqualified included sleeve${unavailableHwm.length === 1 ? "" : "s"}; ${hwmNumericSentence}.${data.advisory.hwm_numerical_status ? ` Governed perimeter: ${humanStatus(data.advisory.hwm_status)}.` : ""}`, hwmKpiTone],
      ["TWR available", `${data.counts.twr_available}/${data.counts.twr_rows}`, "Account-strategy segments", "warning"],
      ["MWR available", `${data.counts.mwr_available}/${data.counts.twr_rows}`, typeof data.fact_status?.flows === "string" ? `Receipt-complete flows; flow fact ${humanStatus(data.fact_status.flows).toLowerCase()} (see rows)` : "Receipt-complete flows; flow-path completeness is not declared in this payload (see rows)", data.counts.mwr_available ? "positive" : "danger"],
    ]);
    const stress = number(data.advisory.correlation_one_stress);
    const limit = number(data.advisory.hwm_limit);
    const comparable = Number.isFinite(stress) && Number.isFinite(limit);
    const passes = comparable && stress <= limit;
    const breaches = comparable && stress > limit;
    // F30: the bar's scale puts the governed limit at a fixed place on the track, so the fill and the
    // limit marker both follow advisory.hwm_limit; a stress below zero fills nothing.
    const barScale = comparable && limit > 0 ? limit / STRESS_LIMIT_POSITION : Number.isFinite(stress) && stress > 0 ? stress * 1.25 : 1;
    const barWidth = Number.isFinite(stress) ? Math.max(0, Math.min(100, stress / barScale * 100)) : 0;
    const limitMarker = Number.isFinite(limit) && limit > 0 ? `<span class="scenario-limit" aria-hidden="true" style="left:${(limit / barScale * 100).toFixed(2)}%"></span>` : "";
    const limitBadge = $("#stress-limit-badge");
    if (limitBadge) limitBadge.textContent = Number.isFinite(limit) ? `${pct(limit)} HWM boundary` : "HWM boundary not declared";
    $("#stress-bar").innerHTML = `<div class="scenario-row"><div class="scenario-label">${safe(stressWeightLabel())}</div><div class="scenario-track">${limitMarker}<span class="scenario-fill ${passes ? "pass" : ""}" style="width:${barWidth.toFixed(2)}%"></span></div><div class="scenario-value ${breaches ? "status-text danger" : ""}">${pct(stress)}</div></div>${breaches ? `<div class="callout callout-danger">Stress exceeds the ${pct(limit)} HWM boundary. This is advisory evidence, not a liquidation instruction.</div>` : ""}`;
    const unavailableHwmSummary = (data.hwm_summary.uncovered_details || []).map((item) => `${item.strategy_name}: ${currencyExact(item.current_value_usd)} · ${humanStatus(item.qualification_gap)} · last accepted ${formatDate(item.last_accepted_date)} · ${item.freshness_reason || "No freshness reason recorded"}`).join(" · ");
    const acceptedHwmSummary = acceptedHwmEvidence.map((row) => `${row.strategy_name}: ${hwmObservationSummary(row)}`).join(" · ");
    const portableProof = data.hwm_portable_proof;
    const portableProofContext = data.advisory.hwm_numerical_status === "AVAILABLE" ? "The governed financial HWM remains independently available; these portable values do not replace it." : "The governed financial HWM remains unavailable; these portable values do not replace or populate it.";
    const portableProofCallout = portableProof ? `<div class="callout callout-neutral"><strong>DEVELOPMENT — NOT FINANCIAL ACCEPTANCE</strong><br>Portable ${safe(portableProof.policy_id || "HWM")} proof only: ${hwmPathSummary(portableProof)}; index ${safe(portableProof.current_index)}, HWM ${safe(portableProof.current_hwm)}, current drawdown ${pct(portableProof.current_drawdown)}, maximum drawdown ${pct(portableProof.maximum_drawdown)}. Complete data through ${formatDateHtml(portableProof.complete_through)}, evaluated ${formatDateHtml(portableProof.evaluated_on)} (${hwmAgeText(portableProof)}; ${statusText(portableProof.age_status)}). Receipt ${safe(portableProof.receipt_sha256.slice(0, 12))}…. ${portableProofContext}</div>` : "";
    const governedProof = data.hwm_governed_proof;
    const governedProofCallout = governedProof ? `<div class="callout callout-neutral"><strong>GOVERNED REAL DATA — PENDING OWNER ACCEPTANCE</strong><br>${hwmCandidate ? `${safe(hwmNotAccepted)}. Candidate result` : `Governed ${safe(governedProof.policy_id || "HWM")} result (${safe(humanStatus(governedProof.financial_acceptance || "UNSPECIFIED"))})`}: ${hwmPathSummary(governedProof)}; index ${safe(governedProof.current_index)}, HWM ${safe(governedProof.current_hwm)}, current drawdown ${pct(governedProof.current_drawdown)}, maximum drawdown ${pct(governedProof.maximum_drawdown)}. Complete data through ${formatDateHtml(governedProof.complete_through)}, evaluated ${formatDateHtml(governedProof.evaluated_on)} (${hwmAgeText(governedProof)}; ${statusText(governedProof.age_status)}). Basis ${safe(humanStatus(governedProof.return_basis || "UNAVAILABLE"))}. ${safe(hwmPolicyText(governedProof, hwmCandidate))} ${safe(perimeterStartText(governedProof))} Receipt ${safe(String(governedProof.receipt_sha256 || "").slice(0, 12))}….</div>` : "";
    $("#hwm-status").innerHTML = performanceLineageMismatch
      ? `<div class="validation-card"><div><strong>Report performance withheld</strong><span>— UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${safe(performanceLineageReason)}</span></div><div class="validation-score">UNAVAILABLE_SOURCE_LINEAGE_MISMATCH</div></div><div class="callout callout-neutral">Report-derived return, MWR, observation-count, return-path, and HWM qualification evidence is withheld until the source-lineage identity matches.</div>`
      : `${governedProofCallout}${portableProofCallout}<div class="validation-card"><div><strong>Governed systematic return / NAV evidence</strong><span>${unavailableHwm.length ? safe(unavailableHwmSummary) : "Every included HWM-perimeter sleeve has a qualified account-live or provider-live return path."} ${excludedNav.length ? `${safe(hwmExclusionNote())}.` : ""}</span></div><div class="validation-score">${statusText(data.advisory.hwm_status)}</div></div><div class="callout callout-neutral">Coverage is ${pct(data.advisory.nav_coverage)}: ${currencyExact(data.hwm_summary.covered_value_usd)} covered of ${currencyExact(data.hwm_summary.governed_perimeter_value_usd)}, with ${currencyExact(data.hwm_summary.uncovered_value_usd)} uncovered and ${currencyExact(data.hwm_summary.excluded_value_usd)} visibly excluded. ${unavailableHwm.length ? (hwmCandidate ? "The numerical full-systematic HWM is not accepted while any included sleeve is unqualified." : "The numerical full-systematic HWM is not calculated while any included sleeve is unqualified.") : "The governed perimeter is fully qualified."} ${acceptedHwmSummary ? `Accepted account-live/provider-overlay evidence: ${safe(acceptedHwmSummary)}.` : "No governed return/NAV evidence currently qualifies for HWM."} Account NAV observations and canonical account-live return paths remain separately labeled; missing NAV and FX are not interpolated.</div>`;
    renderPerformanceComparison();
    const contextualPerformance = data.performance.filter(matchesContext);
    const mappedPerformance = contextualPerformance.filter((row) => row.strategy_name);
    // N63: the unresolved historical intervals are the attribution dispositions of the
    // HISTORICAL period that are not reserve; the performance rows carry no unmapped
    // interval once their pool-only assignments are mapped at the pool layer.
    const historicalGaps = Array.isArray(data.attribution_dispositions)
      ? data.attribution_dispositions.filter((row) => matchesContext(row) && row.period === "HISTORICAL" && row.disposition !== "RESERVE_NOT_APPLICABLE")
      : contextualPerformance.filter((row) => !row.strategy_name && row.strategy_mapping_disposition !== "RESERVE_NOT_APPLICABLE" && row.pool_id !== "reserve-capital");
    const rows = mappedPerformance.map((row) => {
      if (performanceLineageMismatch) {
        const withheld = `—<span class="cell-secondary">UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${safe(performanceLineageReason)}</span>`;
        return `<tr data-account-id="${safe(row.account_id)}"><td>${safe(accountLabel(row.account_id))}</td><td>${safe(row.strategy_name)}</td><td>${withheld}</td><td>${withheld}</td><td class="numeric">${withheld}</td><td>${withheld}</td><td class="numeric">${withheld}</td><td>${withheld}</td></tr>`;
      }
      const acceptedReturn = row.twr;
      // F13: the producer's enum says inferred flow timing for every available row; the row's own
      // flow_timing says whether any flow timing was inferred.  Without one the label is the base status.
      const returnStatus = row.flow_timing ? row.twr_status : String(row.twr_status || "").replace(/_WITH_INFERRED_FLOW_TIMING$/, "");
      const flowTiming = row.flow_timing ? `<span class="cell-secondary">${safe(humanStatus(row.flow_timing))}</span>` : "";
      const observedStart = row.observed_start || row.effective_start || "—";
      const observedEnd = row.observed_end || row.effective_end || "—";
      const evidence = row.nav_observations != null ? `${integer.format(row.nav_observations)} account NAV observations` : "—";
      return `<tr data-account-id="${safe(row.account_id)}"><td>${safe(accountLabel(row.account_id))}</td><td>${safe(row.strategy_name)}</td><td>${safe(observedStart)}<span class="cell-secondary">${safe(observedEnd)}</span></td><td>${safe(evidence)}</td><td class="numeric">${pct(acceptedReturn)}</td><td>${statusText(returnStatus)}${flowTiming}</td><td class="numeric">${pct(row.mwr_xirr)}</td><td>${statusText(row.mwr_status)}${["NO_ACCEPTED_FACT_PATH", "NO_REPORTED_EVENTS_NOT_COMPLETENESS_PROOF"].includes(row.mwr_flow_path_status) ? `<span class="cell-secondary">flow path unproven</span>` : ""}</td></tr>`;
    });
    const performanceHeaders = performanceLineageMismatch
      ? [["Account"], ["Strategy"], ["Observed interval"], ["Evidence"], ["Report return", "numeric"], ["Return status"], ["MWR", "numeric"], ["MWR status"]]
      : [["Account"], ["Strategy"], ["Observed interval"], ["Accepted evidence"], ["Accepted return", "numeric"], ["Return status"], ["MWR", "numeric"], ["MWR status"]];
    $("#performance-table").innerHTML = tableMarkup(performanceHeaders, rows);
    $("#historical-gap-badge").textContent = `${historicalGaps.length} unresolved intervals`;
    const historicalRows = historicalGaps.map((row) => `<tr data-account-id="${safe(row.account_id)}"><td>${safe(accountLabel(row.account_id))}</td><td><code>${safe(row.assignment_id || row.authority || "—")}</code></td><td>${safe(row.effective_start || "Open/unknown")}<span class="cell-secondary">${safe(row.effective_end || "Open/unknown")}</span></td><td>${statusText(row.disposition || row.twr_status)}</td><td>${safe(row.disposition === "POOL_ONLY_UNALLOCATED" || row.strategy_mapping_disposition === "HISTORICAL_POOL_ONLY" ? "Systematic-pool interval requires historical strategy adjudication" : "Assignment requires adjudication")}</td></tr>`);
    $("#historical-gap-table").innerHTML = tableMarkup([["Account"], ["Assignment"], ["Effective interval"], ["Historical attribution"], ["Required disposition"]], historicalRows.length ? historicalRows : [emptyRow(5, "No unresolved historical assignment intervals")]);
    renderAccountManifest();
  }

  // Epoch-manifest decimals are evidence values, not browser floating point.
  // Format exact decimal strings directly so large values and half-even ties do
  // not change merely because the renderer coerced them through Number.
  const manifestDecimal = (value) => typeof value === "string" && /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value) ? value : null;
  const manifestShift = (value, places) => {
    const source = manifestDecimal(value);
    if (source == null) return null;
    const negative = source.startsWith("-");
    const unsigned = negative ? source.slice(1) : source;
    const [whole, fraction = ""] = unsigned.split(".");
    const digits = whole + fraction;
    const point = whole.length + places;
    const rawShifted = point >= digits.length
      ? `${digits}${"0".repeat(point - digits.length)}`
      : `${digits.slice(0, point)}.${digits.slice(point)}`;
    const [shiftedWhole, shiftedFraction = ""] = rawShifted.split(".");
    const normalizedWhole = shiftedWhole.replace(/^0+(?=\d)/, "") || "0";
    const shifted = shiftedFraction
      ? `${normalizedWhole}.${shiftedFraction}`
      : normalizedWhole;
    return negative && /[1-9]/.test(shifted) ? `-${shifted}` : shifted;
  };
  const manifestFixed = (value, places = 2) => {
    const source = manifestDecimal(value);
    if (source == null) return "—";
    const negative = source.startsWith("-");
    const unsigned = negative ? source.slice(1) : source;
    const [whole, rawFraction = ""] = unsigned.split(".");
    const fraction = rawFraction.padEnd(places + 1, "0");
    let rounded = BigInt(`${whole}${fraction.slice(0, places) || "0"}`);
    const next = fraction[places] || "0";
    const remaining = rawFraction.slice(places + 1);
    const tie = next === "5" && !/[1-9]/.test(remaining);
    if (next > "5" || (next === "5" && (!tie || rounded % 2n !== 0n))) rounded += 1n;
    let text = rounded.toString().padStart(places + 1, "0");
    const fractional = places ? text.slice(-places) : "";
    text = places ? `${text.slice(0, -places)}.${fractional}` : text;
    const [integer, decimals = ""] = text.split(".");
    const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    const nonzero = /[1-9]/.test(text);
    return `${negative && nonzero ? "-" : ""}${grouped}${places ? `.${decimals}` : ""}`;
  };
  const manifestNegative = (value) => {
    const source = manifestDecimal(value);
    return source != null && source.startsWith("-") && /[1-9]/.test(source);
  };
  const manifestMoney = (value) => manifestFixed(value, 2);
  // F11: the manifest stores the observed maximum drawdown as a positive depth; every other
  // drawdown in the panel is signed.  It is shown signed, from the same decimal string.
  const manifestDrawdown = (value) => {
    const source = manifestDecimal(value);
    return source == null || source.startsWith("-") || !/[1-9]/.test(source) ? source : `-${source}`;
  };
  const manifestPct = (value) => {
    const shifted = manifestShift(value, 2);
    return shifted == null ? "—" : `${manifestFixed(shifted, 2)}%`;
  };
  const manifestBlockerText = (row) => {
    if (row.status === "FLOWS_AFTER_CLOSING_NAV") {
      return safe(row.status);
    }
    return `${safe(row.status)}: ${safe(row.blocking_reason || "blocking reason unavailable")}`;
  };

  // F12: the manifest rows are not additive (an internal transfer is capital in on one row and
  // withdrawn on another), so no column is summed.  The consolidated figure is the governed
  // invested-capital result's own aggregate.
  const manifestFooter = () => {
    const aggregate = data.invested_capital?.current_aggregate_usd;
    const total = number(aggregate) == null ? "" : `<tr><th scope="row" colspan="3">Net invested (consolidated)</th><td class="numeric" colspan="10">${currencyExact(aggregate)}<span class="cell-secondary">Governed invested-capital aggregate through ${formatDateHtml(data.invested_capital.complete_through)}</span></td></tr>`;
    return `${total}<tr><td colspan="13" class="cell-secondary">Rows are not additive: internal transfers appear in both capital in and withdrawn, so the epoch columns are not summed.</td></tr>`;
  };

  function renderAccountManifest() {
    const manifest = data.account_manifest;
    const legend = $("#account-manifest-legend");
    if (!manifest || !Array.isArray(manifest.rows)) {
      $("#account-manifest-table").innerHTML = tableMarkup([["Epoch manifest unavailable"]], [emptyRow(1, "No account-manifest evidence was supplied to this build")], "Epoch manifest unavailable");
      legend.innerHTML = "No epoch-manifest artifact was bound to this panel build; nothing is inferred.";
      $("#account-manifest-authority").textContent = "Manifest not supplied — non-releaseable";
      return;
    }
    const authorityProfile = data.meta.account_manifest_authority_profile;
    const panelVerification = data.meta.account_manifest_verification;
    const accountingCurrency = String(manifest.accounting_currency || "UNAVAILABLE");
    const schemaVersion = Number(manifest.schema_version);
    const manifestLabel = (row) => {
      if (schemaVersion >= 9) {
        return safe(typeof row.account_label === "string" && row.account_label.length > 0
          ? row.account_label
          : "Account label unavailable — manifest validation required");
      }
      return safe(row.account_label || accountLabel(row.account_id));
    };
    const calculationCutoff = typeof manifest.calculation_cutoff_date === "string"
      && manifest.calculation_cutoff_date.length > 0
      ? `Calculations through ${safe(manifest.calculation_cutoff_date)}.`
      : "Calculation cutoff unavailable — manifest validation required.";
    // The ledger artifact's as-of is the ledger's financial as-of, not a capture date (A7).
    // The manifest records no native capture window, so the legend says so rather than
    // binding the word "capture" to a date it does not describe.
    const ledgerIdentity = manifest.source_identities?.classified_flows?.ledger_identity;
    const ledgerAsOf = ledgerIdentity?.artifact?.as_of_date;
    const ledgerGenerated = ledgerIdentity?.identity_artifact?.as_of_date;
    const generatedAfterCutoff = typeof ledgerGenerated === "string" && typeof manifest.calculation_cutoff_date === "string"
      && ledgerGenerated.slice(0, 10) > manifest.calculation_cutoff_date.slice(0, 10);
    const captureDisclosure = `Ledger as-of: ${safe(ledgerAsOf || "unavailable")}.${typeof ledgerGenerated === "string" && ledgerGenerated ? ` Ledger generation dated ${safe(ledgerGenerated)}${generatedAfterCutoff ? " (after the cutoff)" : ""}.` : ""} Provider capture date not recorded in this manifest.`;
    $("#account-manifest-authority").textContent = ACCOUNT_MANIFEST_AUTHORITY_LABELS[authorityProfile]
      || "Unknown authority profile — non-releaseable";
    // T5d-lite: a row opened from a provider capital anchor measures Capital in, Net
    // capital and Gain on the sleeve allocation basis (what entered the strategy
    // sleeve), not the deposits into the provider cash or wallet account.  The label
    // is an attribute and a legend sentence: every header and cell text stays what the
    // attended interaction QA compares.
    const sleeveRow = (row) => row.opening_basis === "PROVIDER_CAPITAL_ANCHOR";
    const hasSleeveRows = manifest.rows.some(sleeveRow);
    // Without a governed invested-capital result the sleeve allocation is the only
    // capital figure on these rows, so the label says it is an allocation and that
    // invested capital was not supplied (N36).
    const governedCapitalSupplied = Boolean(data.invested_capital);
    const sleeveTitle = "Sleeve allocation basis: the amount that entered the strategy sleeve from its provider capital anchor, not the deposits into the provider cash or wallet account."
      + (governedCapitalSupplied ? "" : " Governed invested capital was not supplied to this build, so this is an allocation, not invested capital.");
    const sleeveAttribute = (row) => (sleeveRow(row) ? ` title="${safe(sleeveTitle)}"` : "");
    const sleeveHeader = hasSleeveRows ? "Sleeve allocation basis for rows opened from a provider capital anchor" : "";
    const rows = manifest.rows.map((row) => {
      const label = manifestLabel(row);
      if (row.status !== "OK") {
        return `<tr data-account-id="${safe(row.account_id)}"><td>${label}</td><td>${safe(row.strategy_id)}</td><td>${safe(row.effective_start)}<span class="cell-secondary">${safe(row.effective_end || "open")} · ${manifestBlockerText(row)}</span></td><td class="numeric">—</td><td class="numeric">—</td><td class="numeric">—</td><td class="numeric">—</td><td class="numeric">—</td><td class="numeric">—</td><td class="numeric">—</td><td class="numeric">—</td><td class="numeric">—</td><td class="numeric">—</td></tr>`;
      }
      return `<tr data-account-id="${safe(row.account_id)}" data-opening-basis="${safe(row.opening_basis)}"><td>${label}</td><td>${safe(row.strategy_id)}</td><td>${safe(row.start)}<span class="cell-secondary">${safe(row.end)}</span></td><td class="numeric"${sleeveAttribute(row)}>${manifestMoney(row.invested)}${sleeveRow(row) ? `<span class="cell-secondary">sleeve allocation</span>` : ""}</td><td class="numeric">${manifestMoney(row.withdrawn)}</td><td class="numeric ${manifestNegative(row.net_capital) ? "is-negative" : ""}"${sleeveAttribute(row)}>${manifestMoney(row.net_capital)}</td><td class="numeric">${manifestMoney(row.current_value)}</td><td class="numeric ${manifestNegative(row.gain) ? "is-negative" : ""}"${sleeveAttribute(row)}>${manifestMoney(row.gain)}</td><td class="numeric">${manifestPct(row.return_on_net_capital)}</td><td class="numeric">${manifestPct(row.annualized_return_on_net_capital)}</td><td class="numeric">${manifestPct(row.observation_linked_return)}</td><td class="numeric">${manifestFixed(row.observed_hwm_index, 2)}</td><td class="numeric ${manifestNegative(manifestDrawdown(row.observed_max_drawdown)) ? "is-negative" : ""}">${manifestPct(manifestDrawdown(row.observed_max_drawdown))}</td></tr>`;
    });
    $("#account-manifest-table").innerHTML = tableMarkup([
      ["Account"], ["Strategy"], ["Observed interval"],
      ["Capital in", "numeric", sleeveHeader], ["Withdrawn", "numeric"], ["Net capital", "numeric", sleeveHeader],
      ["Current", "numeric"], ["Gain", "numeric", sleeveHeader],
      ["RONC", "numeric"], ["Ann. RONC", "numeric"], ["Observed return", "numeric"],
      ["Observed HWM (index, base 100)", "numeric"], ["Observed Max DD (signed)", "numeric"],
    ], rows, `Epoch manifest by account and strategy — monetary values ${accountingCurrency}`, manifestFooter());
    legend.innerHTML = `<strong>${calculationCutoff}</strong> ${captureDisclosure} <strong>Observed diagnostics, not accepted TWR/MWR.</strong> All monetary values are ${safe(accountingCurrency)}. Capital in = explicit opening capital + deposits + internal-in after start. A first-funded-day flow is used as opening basis only when predecessor continuity proves it; otherwise the row is blocked. Withdrawn = external withdrawals + internal-out. Net capital = Capital in − Withdrawn. Current = last funded NAV in the epoch. Gain = Current − Net capital, exact to the cent. RONC = Gain ÷ Net capital; annualized RONC is simple under 365 days and geometric at 365 days or more (the PORT annualization kernel), with the span counted from the opening-capital date. Observed return, observed HWM, and observed max drawdown use only the linked observed NAV intervals; they are not daily valuation performance, TWR, MWR, or Modified Dietz.${hasSleeveRows ? ` Rows opened from a provider capital anchor show Capital in, Net capital and Gain on the sleeve allocation basis: the amount that entered the strategy sleeve, not the deposits into the provider cash or wallet account. ${governedCapitalSupplied ? "Governed invested capital (the deposits) and the strategy cash line are reported separately." : "Governed invested capital was not supplied to this build: on these rows Capital in is an allocation to the strategy sleeve, not invested capital."}` : ""}`;
    const disclosure = $("#account-manifest-disclosure");
    const producerReadiness = panelVerification.producer_readiness;
    const notes = [
      "Recorded when this panel was built, before release. These are the epoch manifest's authority fields, not the release state; the release state is in the coordinated release record, not in this panel.",
      `Epoch manifest authority profile at build: ${authorityProfile}`,
      `Epoch manifest verification at build: ${panelVerification.status}; ${panelVerification.method}; manifest readiness to stand as publication authority: ${panelVerification.publication_readiness}.`,
      producerReadiness
        ? `Epoch manifest producer readiness at build: ${producerReadiness.authority_status}; independent authority verification ${producerReadiness.independent_authority_verification}; manifest readiness to stand as publication authority: ${producerReadiness.publication_readiness}.`
        : "Epoch manifest producer readiness at build: not supplied.",
      `Epoch manifest self-declared readiness at build: ${manifest.authority_readiness.authority_status}; independent verification ${manifest.authority_readiness.independent_authority_verification}; manifest readiness to stand as publication authority: ${manifest.authority_readiness.publication_readiness}.`,
      `Flow reconciliation: ${manifest.flow_reconciliation.status}; ${manifest.flow_reconciliation.method}.`,
    ];
    if (notes.length) {
      disclosure.hidden = false;
      $("#account-manifest-disclosure-list").innerHTML = notes.map((note) => `<li>${safe(note)}</li>`).join("");
    } else {
      disclosure.hidden = true;
    }
  }

  // F32: the execution effect each underwriting row declares.  A payload that declares none keeps the
  // policy statement, and says that it is policy text.
  const underwritingEffects = (key) => [...new Set((data.strategy_underwriting?.rows || []).map((row) => row.advisory_disposition?.[key]).filter((value) => typeof value === "string"))];
  const advisoryExecutionEffect = () => underwritingEffects("execution_effect").join(" / ") || "NONE";
  const advisoryExecutionEffectNote = () => (underwritingEffects("execution_effect").length ? "Read-only advisory surface; declared by every underwriting row" : "Policy statement: read-only advisory surface; no row declares an execution effect");

  function renderSignals() {
    const gapTested = data.strategies.filter((row) => row.implementation_gap_status === "tested").length;
    const knownCost = data.strategies.filter((row) => String(row.cost_status).startsWith("known")).length;
    renderKpis("#signal-kpis", [
      ["Implementation gap tested", `${gapTested}/${data.counts.strategies}`, "Provider versus account evidence"],
      ["Known cost status", `${knownCost}/${data.counts.strategies}`, "Unknown stays disclosed", "warning"],
      ["Reconciliations passed", `${data.counts.reconciliation_passed}/${data.counts.reconciliation_checks}`, "Accepted report checks"],
      ["Automated action", advisoryExecutionEffect(), advisoryExecutionEffectNote(), "reserve"],
    ]);
    // A5: the account-live CAGR and strategy-level MWR each carry an annualization label
    // (basis/window, already marked "(<1 year)" when short) that must render beside the
    // figure it describes. The cumulative return stays unannualized; the CAGR value
    // renders separately with its own annualization label so the two numbers and their
    // labels are never crossed. Neither CAGR nor MWR has its own cell, so both join the
    // account-cumulative cell rather than being silently dropped.
    // F19: each strategy's account-live window starts on its own date; the header cannot, so the cell does.
    const accountWindowStart = (row) => row.provider_evidence?.account?.observed_start || null;
    const implementationRows = data.strategies.map((row) => {
      const withheld = `—<span class="cell-secondary">UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${safe(performanceLineageReason)}</span>`;
      const cagrLabel = row.account_live_cagr_annualization?.label;
      const mwrLabel = row.mwr_annualization?.label;
      const cumulativeCell = performanceLineageMismatch
        ? withheld
        : `${pct(row.account_live_cumulative_return)}${accountWindowStart(row) ? `<span class="cell-secondary">since ${formatDateHtml(accountWindowStart(row))}</span>` : ""}${row.account_live_cagr != null ? `<span class="cell-secondary">CAGR ${pct(row.account_live_cagr)}${annualizationSuffix(cagrLabel)}</span>` : ""}${row.mwr_xirr != null ? `<span class="cell-secondary">MWR ${pct(row.mwr_xirr)}${annualizationSuffix(mwrLabel)}</span>` : ""}`;
      const drawdownCell = performanceLineageMismatch ? withheld : pct(row.account_live_max_drawdown);
      return `<tr><td>${safe(row.strategy_name)}</td><td>${evidenceAvailability(row)}</td><td class="numeric">${cumulativeCell}</td><td class="numeric">${drawdownCell}</td><td>${exactComparison(row)}</td><td>${costDisclosure(row)}</td><td><span class="cell-primary">${safe(row.version_ref || humanStatus(row.version_disposition || "UNRESOLVED"))}</span><span class="cell-secondary">Effective ${formatDateHtml(row.version_effective_start)}–${endDateHtml(row.version_effective_end)} · design ${statusText(row.design_status)} · backtest ${statusText(row.backtest_status)} · ${statusText(row.version_compatibility_status)}</span><span class="cell-secondary">${safe(row.version_disposition_reason || "No version disposition recorded")}</span>${benchmarkDisclosure(row)}</td></tr>`;
    });
    $("#implementation-table").innerHTML = tableMarkup([["Strategy"], ["Evidence availability"], ["Account cumulative (since observed start)", "numeric"], ["Account max drawdown (same window)", "numeric"], ["Provider/model comparison"], ["Cost status"], ["Version"]], implementationRows, evidenceCaption("Strategy implementation gaps and costs"));
    // F36: a blocking item's value is shown beside its gap text, and a check's tolerance and maximum
    // difference are shown when the report publishes them (or said not to be published).
    const reconciliationEvidence = (row) => {
      const parts = [];
      if (row.known_gap) parts.push(operatorText(row.known_gap));
      const value = row.value_usd ?? row.value;
      if (value != null && value !== "") parts.push(`value ${value}`);
      if (row.check_id) {
        parts.push(row.tolerance != null ? `tolerance ${row.tolerance}` : "tolerance not published");
        parts.push(row.maximum_absolute_difference != null ? `maximum absolute difference ${smallDecimal(row.maximum_absolute_difference)}` : "difference not published");
      }
      return parts.join(" · ") || "—";
    };
    const reconciliationRows = [...data.reconciliation_checks, ...data.blocking_items].map((row) => `<tr><td>${safe(row.check_id || row.item)}</td><td>${statusText(row.status)}</td><td>${safe(reconciliationEvidence(row))}</td></tr>`);
    $("#reconciliation-table").innerHTML = tableMarkup([["Check / exception"], ["Status"], ["Evidence / gap"]], reconciliationRows);
  }

  const factorLensModes = data?.decomposition_lenses?.modes || { LIVE_ONLY: data?.decomposition_lenses?.lenses || [data?.factor_analysis] };
  let selectedFactorEvidenceMode = factorLensModes[data?.decomposition_lenses?.default_evidence_mode] ? data?.decomposition_lenses?.default_evidence_mode : "LIVE_ONLY";
  const activeFactorLenses = () => factorLensModes[selectedFactorEvidenceMode] || factorLensModes.LIVE_ONLY || [];
  const replicationRows = data?.replication_analysis?.series || [];
  let selectedFactorLens = data?.decomposition_lenses?.default_lens || activeFactorLenses()[0]?.lens_id;
  let selectedFactorSurface = "STATIC";
  let selectedFactorSource = null;
  let selectedFactorSeries = null;
  const currentFactorLens = () => activeFactorLenses().find((item) => item.lens_id === selectedFactorLens) || activeFactorLenses()[0];
  const currentFactorSurfaceAnalysis = () => {
    const lens = currentFactorLens();
    const surfaces = lens?.surfaces || {};
    const defaultSurface = lens?.default_surface || "STATIC";
    if (!surfaces[selectedFactorSurface]) selectedFactorSurface = surfaces[defaultSurface] ? defaultSurface : Object.keys(surfaces)[0] || defaultSurface;
    return surfaces[selectedFactorSurface] || lens;
  };
  const currentFactorAnalysis = () => {
    const analysis = currentFactorSurfaceAnalysis();
    const sourceKeys = analysis?.available_source_keys || (Array.isArray(analysis?.sources) ? analysis.sources : []).map((item) => item.source_key).filter(Boolean);
    if (!sourceKeys.includes(selectedFactorSource)) selectedFactorSource = analysis?.default_source_key || sourceKeys[0] || null;
    if (!selectedFactorSource) return analysis;
    const series = (analysis.series || []).filter((row) => !row.source_key || row.source_key === selectedFactorSource);
    const sources = Array.isArray(analysis.sources) ? analysis.sources.filter((row) => row.source_key === selectedFactorSource) : analysis.sources;
    return {
      ...analysis,
      available_count: series.filter((row) => row.status === "AVAILABLE").length,
      diagnostic_count: series.filter((row) => row.status === "DIAGNOSTIC_ONLY").length,
      series,
      sources,
      unavailable_count: series.filter((row) => row.status === "UNAVAILABLE").length,
    };
  };
  const lensSemantics = {
    ECONOMIC_FACTOR: {
      question: "Which systematic economic return drivers explain the strategy?",
      method: "Joint excess-return regression against a predeclared market, size, value, profitability, investment, and momentum candidate hierarchy.",
      beta: "Beta is return sensitivity to each factor, holding the other selected factors constant.",
      caveat: "A loading is not a holdings weight. Alpha is the residual intercept after the selected factor betas."
    },
    ETF_STYLE: {
      question: "Which investable equity-style proxies best reproduce the strategy return path?",
      method: "Joint constrained returns-based style analysis using predeclared ETF cells; raw ETF candidates are not assumed orthogonal.",
      beta: "Beta is the fitted proxy loading. Under the simplex model the loadings behave like non-negative replicating weights and sum to one.",
      caveat: "This is a return-mimicking portfolio, not a claim about actual holdings."
    },
    RETURN_SECTOR: {
      question: "Which sector return combinations explain the strategy?",
      method: "Joint regularized return decomposition is the primary lens; single-sector similarity, when shown, is a separate diagnostic and not the decomposition.",
      beta: "Sector beta is marginal return sensitivity conditional on the other selected sector proxies.",
      caveat: "Return-based sector beta can differ from point-in-time holdings exposure because timing, cash, leverage, and trading alter realized returns."
    },
    RETURN_INDUSTRY: {
      question: "Which industry return combinations explain the strategy at a finer resolution?",
      method: "Train-only dimension-controlled joint return decomposition; any one-industry match is labeled similarity rather than attribution.",
      beta: "Industry beta is conditional return sensitivity to a selected industry portfolio, not an ownership percentage.",
      caveat: "The 49 industry portfolios are collinear, so sample length, regularization, and stability gates are part of the result."
    },
    DYNAMIC_STYLE: {
      question: "How do factor and style sensitivities change through time?",
      method: "Clean-room filtered least squares with train-only smoothness selection and causal walk-forward holdout updates.",
      beta: "Each dated path value is the beta available in that estimation state; the latest beta is the final dated value.",
      caveat: "MPI-informed and non-equivalent; the sealed holdout is evaluation evidence, never refit input."
    }
  };

  function renderUnderwriting() {
    const underwriting = data.strategy_underwriting;
    if (!underwriting?.rows?.length) {
      $("#underwriting-boundary").innerHTML = `<strong>Underwriting unavailable.</strong> This snapshot predates the governed layered-underwriting contract.`;
      $("#underwriting-table").innerHTML = tableMarkup([["Status"]], [emptyRow(1, "No governed strategy underwriting")]);
      return;
    }
    renderKpis("#underwriting-kpis", [
      ["Authoritative designs", `${underwriting.counts.authoritative_design}/${underwriting.counts.strategies}`, "Unverified archetypes stay inferred", underwriting.counts.authoritative_design === underwriting.counts.strategies ? "positive" : "warning"],
      ["Version-matched long history", `${underwriting.counts.version_matched_long_history}/${underwriting.counts.strategies}`, "Backtest plus live; exact strategy version required", underwriting.counts.version_matched_long_history ? "positive" : "warning"],
      ["Model/account gaps", `${underwriting.counts.quantified_model_account_gap}/${underwriting.counts.strategies}`, "Exact-common-date totals; causes remain separate", underwriting.counts.quantified_model_account_gap ? "warning" : "danger"],
      ["Regime comparisons", `${underwriting.counts.empirical_regime_comparison}/${underwriting.counts.strategies}`, "Pre-live thresholds and version-matched empirical range", underwriting.counts.empirical_regime_comparison ? "positive" : "warning"],
    ]);
    $("#underwriting-boundary").innerHTML = `<strong>${safe(underwriting.policy_id)}</strong> · policy <code>${safe(underwriting.policy_sha256.slice(0, 14))}…</code> · evidence layers are never silently concatenated. Long-history membership requires an exact version match; implementation fidelity uses account-live exact-common-date evidence. Regime underwriting admits complete accepted-calendar weeks only; partial first, last, and current weeks are excluded. Regime results are observable behavior diagnostics, not proof of hidden strategy logic. Allocation effect <strong>${safe(underwritingEffects("allocation_effect").join(" / ") || "not declared")}</strong> and execution effect <strong>${safe(underwritingEffects("execution_effect").join(" / ") || "not declared")}</strong> on every row; automatic drop, readiness, and order authority are policy-fixed at <strong class="policy-text" title="Policy statement; the payload carries no field for it">NONE</strong>.`;
    const rows = underwriting.rows.map((row) => {
      const layers = row.evidence_layers;
      const history = row.long_history;
      const gap = row.model_account_gap;
      const implementation = layers.implementation_reconciliation;
      const implementationDetail = implementation.detail;
      const regimes = row.regime_conformance;
      const fit = row.portfolio_fit;
      const disposition = row.advisory_disposition;
      const lineage = layers.design.implementation_lineage;
      const lineageSummary = (lineage?.layers || []).map((item) => `${humanStatus(item.layer_id)}: ${item.instrument}`).join(" → ");
      const layerMarkup = [
        ["Design", layers.design.status, lineage?.status || layers.design.version_ref || layers.design.authority_status],
        ["Backtest", layers.backtest.status, `${integer.format(layers.backtest.matched_observations || 0)} matched · ${layers.backtest.compatibility_status || "unresolved"}`],
        ["Model/provider live", layers.provider_or_model_live.status, layers.provider_or_model_live.comparison_status],
        ["Account live", layers.account_live.status, `${formatDate(layers.account_live.observed_start)}–${formatDate(layers.account_live.observed_end)} · ${integer.format(layers.account_live.observations || 0)} returns`],
        ["Reconciliation", layers.implementation_reconciliation.status, layers.implementation_reconciliation.comparison_status],
      ].map(([label, status, note]) => `<span class="evidence-line"><strong>${safe(label)}</strong>${statusText(status)}<small>${safe(humanStatus(note))}</small></span>`).join("");
      const verifiedMetrics = implementationDetail?.return_reconciliation;
      const returnAttribution = implementationDetail?.return_attribution;
      const causalBridge = returnAttribution?.bridge;
      const confirmedCauses = (implementationDetail?.root_causes || []).filter((item) => item.status === "CONFIRMED");
      const trade = implementationDetail?.trade_reconciliation;
      const comparisonPolicyUnavailable = verifiedMetrics?.status === "UNAVAILABLE_POLICY_NOT_ADMITTED";
      const actualAccountReturns = implementationDetail?.account_live_return_summary;
      // A contract older than the PROVIDER_ADMIT_OR_DECLARE_V1 exclusion says so beside its date
      // (N56 shape).  The producer publishes this per contract kind under
      // layers.implementation_reconciliation.contracts[kind] -- {as_of_date, stale,
      // pre_policy} -- plus one shared contracts.provider_return_policy {policy_id,
      // effective_start}; this never reads implementationDetail/.return_attribution
      // fields that the producer does not emit.
      const contracts = implementation.contracts || {};
      const contractDateText = (kind) => {
        const contract = contracts[kind];
        if (!contract || typeof contract.as_of_date !== "string" || !contract.as_of_date) return "";
        const policy = contracts.provider_return_policy;
        const before = contract.pre_policy && policy && typeof policy.effective_start === "string"
          ? `, before the ${policy.policy_id} exclusion effective ${formatDate(policy.effective_start)}`
          : "";
        return ` · contract as of ${formatDate(contract.as_of_date)}${before}`;
      };
      const staleReconciliation = implementationDetail?.stale_reconciliation;
      const staleAttribution = implementationDetail?.stale_return_attribution;
      const staleWindow = (stale) => `${formatDateHtml(stale.window_start)}–${formatDateHtml(stale.window_end)}`;
      const staleReconciliationMarkup = staleReconciliation
        ? `<span class="cell-primary">Governed account return ${pct(staleReconciliation.governed_account_return, 2)} (${safe(accountBasisText(staleReconciliation.governed_account_basis))}) · ${staleWindow(staleReconciliation)} · ${integer.format(staleReconciliation.governed_observations || 0)} governed dates</span><span class="cell-secondary">Model comparison not ready: ${safe(staleReconciliation.reason)} Repair ${safe(staleReconciliation.repair_ref)}</span>`
        : "";
      const staleAttributionMarkup = staleAttribution
        ? `<span class="cell-secondary">Causal waterfall not ready${staleReconciliation ? "" : ` · governed account return ${pct(staleAttribution.governed_account_return, 2)} (${safe(accountBasisText(staleAttribution.governed_account_basis))}) · ${staleWindow(staleAttribution)} · ${integer.format(staleAttribution.governed_observations || 0)} governed dates`}: ${safe(staleAttribution.reason)} Repair ${safe(staleAttribution.repair_ref)}</span>`
        : "";
      const verifiedGapMarkup = staleReconciliation
        ? staleReconciliationMarkup
        : comparisonPolicyUnavailable
        ? `<span class="cell-primary">Model comparison unavailable under the current source policy</span>${actualAccountReturns ? `<span class="cell-secondary">Actual account return ${pct(actualAccountReturns.cumulative_return)} · ${formatDateHtml(actualAccountReturns.observed_start)}–${formatDateHtml(actualAccountReturns.observed_end)} · ${integer.format(actualAccountReturns.observations)} returns</span>` : ""}<span class="cell-secondary">Account and trade evidence is retained. No model return or comparison gap is inferred.</span>`
        : verifiedMetrics
        ? `<span class="cell-primary">${safe(MODEL_BASIS_LABELS[verifiedMetrics.model_basis] || "Model (basis not recorded)")} ${pct(verifiedMetrics.model_cumulative_return, 2)} · account ${pct(verifiedMetrics.account_cumulative_return, 2)}${verifiedMetrics.account_basis ? ` (${safe(accountBasisText(verifiedMetrics.account_basis))})` : ""} · gap ${pct(verifiedMetrics.account_minus_model_cumulative, 2)}</span><span class="cell-secondary">${formatDateHtml(verifiedMetrics.observed_start)}–${formatDateHtml(verifiedMetrics.observed_end)} · ${integer.format(verifiedMetrics.observations)} exact common dates${safe(contractDateText("reconciliation"))} · β ${decimal(verifiedMetrics.beta_to_model)} · correlation ${decimal(verifiedMetrics.daily_return_correlation)} · tracking error ${pct(verifiedMetrics.annualized_tracking_error)}</span><span class="cell-secondary">Information ratio ${decimal(verifiedMetrics.information_ratio)} · up/down capture ${pct(verifiedMetrics.up_capture)}/${pct(verifiedMetrics.down_capture)} · relative drawdown ${pct(verifiedMetrics.relative_drawdown)}</span><span class="cell-secondary">Confirmed causes: ${safe(confirmedCauses.map((item) => humanStatus(item.cause)).join(" · ") || "none")}</span>${trade ? `<span class="cell-secondary">Model events ${integer.format(trade.model_event_count)} · matched ${integer.format((trade.matched_same_date_count || 0) + (trade.matched_delayed_count || 0))} · missed ${integer.format(trade.missed_model_event_count)} · unexplained broker events ${integer.format(trade.actual_unexplained_count)}</span>` : ""}<span class="cell-secondary">${safe(humanStatus(verifiedMetrics.status))}</span>`
        : "";
      const causalCategoryMarkup = (causalBridge?.categories || []).map((item) => {
        const effect = Number(item.effect_pp);
        const effectText = `${effect > 0 ? "+" : ""}${decimal(effect, 3)} pp`;
        return `<span class="cell-secondary"><strong>${safe(humanStatus(item.category))}</strong> ${safe(effectText)} · ${safe(humanStatus(item.status))}${item.method ? ` · ${safe(item.method)}` : ""}</span>`;
      }).join("");
      const causalMixMarkup = causalBridge?.effective_account_model_mix_return == null
        ? ""
        : `<span class="cell-secondary"><strong>Correct connected-model comparator:</strong> effective model mix ${pct(causalBridge.effective_account_model_mix_return)} · account-vs-mix gap ${decimal(causalBridge.effective_account_model_mix_gap_pp, 3)} pp. The primary AI model alone is not the whole-account benchmark.</span>`;
      const causalGapMarkup = staleAttribution
        ? staleAttributionMarkup
        : causalBridge?.status === "UNAVAILABLE_POLICY_NOT_ADMITTED"
        ? `<span class="cell-secondary">Actual account return ${pct(causalBridge.account_return)} · ${formatDateHtml(returnAttribution.observed_start)}–${formatDateHtml(returnAttribution.observed_end)}. Trade P&amp;L attribution is retained; a model-gap waterfall is unavailable.</span>`
        : causalBridge
        ? `<span class="cell-primary">Causal return waterfall · bridge model (${safe(BRIDGE_BASIS_LABELS[causalBridge.model_basis] || "basis not recorded")}) ${pct(causalBridge.model_return, 2)} · account ${pct(causalBridge.account_return, 2)}${causalBridge.account_basis ? ` (${safe(accountBasisText(causalBridge.account_basis))})` : ""} · exact gap ${decimal(causalBridge.gap_pp, 3)} pp · ${formatDateHtml(returnAttribution.observed_start)}–${formatDateHtml(returnAttribution.observed_end)}${safe(contractDateText("return_attribution"))}</span>${causalMixMarkup}${causalCategoryMarkup}<span class="cell-secondary">${safe(humanStatus(causalBridge.status))} · ${safe(humanStatus(returnAttribution.verification))}</span>`
        : "";
      // The retained provider/reference comparison is its own quantity.  Under a stale
      // implementation contract it is the only older gap shown, with its basis and window.
      const legacyGapMarkup = gap.total_gap == null
        ? `${statusText(gap.status)}<span class="cell-secondary">No accepted model/provider path can be compared with the account. No gap or cause is inferred.</span>`
        : `<span class="cell-primary">Reference ${pct(gap.reference_cumulative_return)} · account ${pct(gap.account_cumulative_return)} · gap ${pct(gap.total_gap)}</span><span class="cell-secondary">${safe(humanStatus(gap.signature))} · β ${decimal(gap.beta)} · correlation ${decimal(gap.correlation)} · tracking error ${pct(gap.tracking_error_annualized)}</span><span class="cell-secondary">Causally bound components ${integer.format(gap.causally_bound_components.length)} · unresolved residual ${pct(gap.unresolved_residual)} · reconciliation error ${decimal(gap.reconciliation_error, 8)}</span><span class="cell-secondary">Unknown: ${safe((gap.unresolved_causes || []).map(humanStatus).join(" · ") || "none")}</span>`;
      const staleShadowMarkup = staleReconciliation && gap.total_gap != null && typeof gap.observed_start === "string" && typeof gap.observed_end === "string"
        ? `<span class="cell-primary">Provider composite shadow (retained) · reference ${pct(gap.reference_cumulative_return, 2)} · account ${pct(gap.account_cumulative_return, 2)} · ${formatDateHtml(gap.observed_start)}–${formatDateHtml(gap.observed_end)}</span><span class="cell-secondary">Gap ${pct(gap.total_gap, 2)}${gap.observations == null ? "" : ` · ${integer.format(gap.observations)} observations`} · ${safe(humanStatus(gap.signature))}; not the implementation gap</span>`
        : "";
      const gapMarkup = staleReconciliation
        ? `${staleReconciliationMarkup}${staleShadowMarkup}${causalGapMarkup}`
        : `${verifiedGapMarkup || legacyGapMarkup}${causalGapMarkup}`;
      const regimeRows = (regimes.regimes || []).map((item) => `<span class="regime-line"><strong>${safe(humanStatus(item.regime_id))}</strong><small>${integer.format(item.live_observations)} live · mean ${pct(item.live_mean_return)}; ${integer.format(item.matched_prehistory_observations)} matched pre-live · mean ${pct(item.matched_prehistory_mean_return)}; latest ${formatDateHtml(item.latest_week)} ${pct(item.latest_strategy_return)} versus benchmark ${pct(item.latest_benchmark_return)} · ${safe(humanStatus(item.latest_event_range_status))}</small></span>`).join("");
      const benchmark = regimes.benchmark ? `${regimes.benchmark.components.join(" + ")} · ${humanStatus(regimes.benchmark.method)}` : "No admitted benchmark path";
      return `<tr data-underwriting-strategy="${safe(row.strategy_id)}">
        <th scope="row" data-label="Strategy / design"><span class="cell-primary">${safe(row.strategy_name)}</span><span class="cell-secondary">${safe(row.design_fit.economic_family || "No economic family")}</span><span class="cell-secondary">${statusText(row.design_fit.status)} · ${safe(row.design_fit.portfolio_role || "No governed portfolio role")}</span>${lineageSummary ? `<span class="cell-secondary">${safe(lineageSummary)} · portfolio binding ${safe(humanStatus(lineage.portfolio_return_series_binding))}</span>` : ""}</th>
        <td data-label="Five evidence layers">${layerMarkup}</td>
        <td data-label="Long-history statistical membership">${statusText(history.status)}<span class="cell-secondary">Matched backtest ${integer.format(history.matched_backtest_observations)} · live/hybrid aligned ${integer.format(history.live_aligned_observations || 0)}/${integer.format(history.hybrid_aligned_observations || 0)}</span><span class="cell-secondary">Net alpha live ${pct(history.live_net_alpha)} · full ${pct(history.hybrid_net_alpha)}; sealed holdout R² live ${pct(history.live_holdout_predicted_r_squared)} · full ${pct(history.hybrid_holdout_predicted_r_squared)}</span></td>
        <td data-label="Model versus account">${gapMarkup}</td>
        <td data-label="Regime behavior">${statusText(regimes.status)}<span class="cell-secondary">${safe(benchmark)} · ${integer.format(regimes.threshold_history_weeks)} pre-live threshold weeks · lower tail ${pct(regimes.lower_tail_threshold)} · high absolute return ${pct(regimes.high_absolute_return_threshold)}</span>${regimeRows || '<span class="cell-secondary">No eligible regime rows</span>'}</td>
        <td data-label="Portfolio fit / leave-one-out">${statusText(fit.portfolio_fit_status)}<span class="cell-secondary">${safe(fit.cluster_label || "No governed cluster")} · cluster weight ${pct(fit.cluster_allocation_weight)}${weightBasisSuffix(fit.cluster_weight_basis, fit.cluster_allocation_weight)} · pair range ${decimal(fit.cluster_observed_pair_correlation_minimum)}–${decimal(fit.cluster_observed_pair_correlation_maximum)}</span><span class="cell-secondary">Strategy weight ${pct(fit.strategy_systematic_weight)}${weightBasisSuffix(fit.strategy_weight_basis, fit.strategy_systematic_weight)} · correlation-one contribution ${pct(fit.strategy_correlation_one_loss_contribution)} (${pct(fit.strategy_share_of_correlation_one_loss)} of loss) · leave-one-out loss ${pct(fit.leave_one_out_correlation_one_loss)}</span></td>
        <td data-label="Advisory disposition">${statusText(disposition.action)}<span class="cell-secondary">${safe((disposition.reasons || []).map(humanStatus).join(" · "))}</span><span class="cell-secondary">Drop authority ${safe(disposition.drop_decision_status)} · allocation ${safe(disposition.allocation_effect)} · execution ${safe(disposition.execution_effect)}</span></td>
      </tr>`;
    });
    $("#underwriting-table").innerHTML = tableMarkup([["Strategy / design"], ["Five evidence layers"], ["Long-history statistical membership"], ["Model versus account"], ["Regime behavior"], ["Portfolio fit / leave-one-out"], ["Advisory disposition"]], rows);
  }

  function replicationAnalysisMarkup(seriesId) {
    if (selectedFactorLens !== "DYNAMIC_STYLE") return "";
    const row = replicationRows.find((item) => item.series_id === seriesId);
    if (!row) return `<div class="callout callout-danger"><strong>Tracker state:</strong> No governed replication disposition exists for ${safe(seriesId)}.</div>`;
    if (!row.metrics) return `<div class="callout callout-warning"><strong>Out-of-sample tracker:</strong> ${statusText(row.availability_status)} · ${safe((row.blocking_reasons || []).map(humanStatus).join(" · ") || "No selected dynamic model")}. No return, turnover, or cost value is imputed.</div>`;
    const metrics = row.metrics;
    const policy = row.investability_policy || {};
    const parameterSelection = row.parameter_selection || {};
    const factorLabels = {
      SPY_XS: "SPY excess return",
      IWM_XS: "IWM excess return",
      HYG_XS: "HYG excess return",
    };
    const factors = (row.factor_ids || []).map((factorId) => factorLabels[factorId] || factorId).join(" + ");
    return `<div class="panel span-12"><div class="panel-heading"><div><div class="eyebrow">Forward-only research tracker</div><h3>Sealed-holdout replication</h3></div><span class="badge ${row.availability_status === "AVAILABLE_RESEARCH_REPLICATION" ? "badge-positive" : "badge-warning"}">${statusText(row.availability_status)}</span></div><div class="analytics-detail-grid"><div class="metric-block"><div class="metric-label">Actual cumulative</div><div class="metric-value">${pct(metrics.cumulative_actual_excess_return)}</div><div class="metric-note">${safe(row.holdout_start)}–${safe(row.holdout_end)}</div></div><div class="metric-block"><div class="metric-label">Net tracker cumulative</div><div class="metric-value">${pct(metrics.cumulative_net_tracker_excess_return)}</div><div class="metric-note">Gross ${pct(metrics.cumulative_gross_tracker_excess_return)}</div></div><div class="metric-block"><div class="metric-label">Tracking error</div><div class="metric-value">${pct(metrics.annualized_tracking_error)}</div><div class="metric-note">${integer.format(metrics.observation_count)} causal holdout observations</div></div><div class="metric-block"><div class="metric-label">Residual alpha</div><div class="metric-value">${pct(metrics.annualized_descriptive_residual_alpha)}</div><div class="metric-note">Descriptive; no confidence claim</div></div><div class="metric-block"><div class="metric-label">Relative drawdown</div><div class="metric-value">${pct(metrics.max_relative_wealth_ratio_drawdown_loss)}</div><div class="metric-note">Actual versus net tracker wealth ratio</div></div><div class="metric-block"><div class="metric-label">Turnover / cost</div><div class="metric-value">${decimal(metrics.total_turnover)}×</div><div class="metric-note">${safe(policy.selected_cost_bps_per_one_way_turnover)} bps · ${pct(metrics.cumulative_cost_drag)} drag</div></div></div><div class="callout callout-neutral"><strong>Replication rule:</strong> At each accepted daily observation, use only the prior model state to set dynamic weights on ${safe(factors || "the selected factors")}; clip each factor to ±${safe(policy.max_absolute_factor_weight || "unavailable")}×, cap gross factor exposure at ${safe(policy.max_gross_factor_exposure || "unavailable")}×, put the residual in zero-excess-return cash, and charge ${safe(policy.selected_cost_bps_per_one_way_turnover || "unavailable")} bps per one-way turnover. Smoothness ${safe(parameterSelection.smoothness || "unavailable")} was selected on training data by ${safe(humanStatus(parameterSelection.selection_rule || "UNAVAILABLE"))}; holdout recalibration is NONE under the governed policy (payload: ${safe(parameterSelection.holdout_recalibration || "NONE")}).</div><div class="callout callout-neutral"><strong>Interpretation:</strong> This is a simple three-input research replicator when three factors are selected; it is not a recovered copy of the provider's hidden trading rules. <strong>Authority boundary:</strong> Research diagnostic only, with no readiness, allocation, HWM, execution, or order authority.</div></div>`;
  }

  function rejectedBucketNote(row) {
    // Receipts the producer writes when it drops a factor bucket rather than fit across it.
    const rejected = Array.isArray(row.interval_association?.rejected_buckets) ? row.interval_association.rejected_buckets : [];
    if (!rejected.length) return "";
    const nouns = {
      DECLARED_UNAVAILABLE_LABEL: ["declared source gap", "declared source gaps"],
      SOURCE_SEGMENT_GAP: ["source-segment gap", "source-segment gaps"],
      INCOMPATIBLE_RETURN_BASIS_SOURCE_LAYER_BOUNDARY: ["return-basis boundary", "return-basis boundaries"],
    };
    const counts = new Map();
    rejected.forEach((bucket) => counts.set(bucket.reason, (counts.get(bucket.reason) || 0) + 1));
    const reasons = [...counts].map(([reason, count]) => {
      const noun = nouns[reason] ? nouns[reason][count === 1 ? 0 : 1] : humanStatus(reason);
      return `${integer.format(count)} ${noun}`;
    }).join(", ");
    const declared = [...new Set(rejected.flatMap((bucket) => bucket.declared_unavailable_labels || []))].sort();
    const dates = declared.length ? ` Source-declared unavailable dates: ${declared.map(formatDate).join(", ")}.` : "";
    return ` ${integer.format(rejected.length)} factor ${rejected.length === 1 ? "bucket was" : "buckets were"} excluded from this fit (${reasons}); no return is filled across them.${dates}`;
  }

  function factorEvidenceComposition(row) {
    const composition = row.series_composition || {};
    if (!composition.status) {
      // F38: a series with no model has no composition to declare; only a fitted legacy analysis lacks the contract.
      if (row.status === "UNAVAILABLE") return `<div class="callout callout-warning"><strong>Evidence composition:</strong> No composition: series unavailable. No backtest-history claim is made.${safe(rejectedBucketNote(row))}</div>`;
      return `<div class="callout callout-warning"><strong>Evidence composition:</strong> This legacy analysis does not declare live-versus-backtest row composition. No backtest-history claim is made.${safe(rejectedBucketNote(row))}</div>`;
    }
    const liveCount = Number(composition.live_observations || 0);
    const backtestCount = Number(composition.backtest_observations_used || 0);
    const rejectedOverlap = Number(composition.overlapping_backtest_observations_rejected || 0);
    const noMatchedPrehistory = selectedFactorEvidenceMode === "LIVE_PLUS_BACKTEST" && backtestCount === 0;
    const detail = noMatchedPrehistory
      ? "No accepted version-matched backtest observations exist before live inception; this result therefore equals the live-only history and is not described as a long-history result."
      : selectedFactorEvidenceMode === "LIVE_PLUS_BACKTEST"
        ? "Accepted version-matched backtest observations end before live inception; live observations remain authoritative from inception forward."
        : "Only accepted live observations enter this result; backtest observations are excluded.";
    return `<div class="callout ${noMatchedPrehistory ? "callout-warning" : "callout-neutral"}"><strong>Evidence composition:</strong> ${integer.format(liveCount)} live + ${integer.format(backtestCount)} backtest observations · live inception ${formatDateHtml(composition.live_inception)} · ${statusText(composition.status)}. ${safe(detail)}${rejectedOverlap ? ` ${integer.format(rejectedOverlap)} overlapping backtest observations were rejected because live evidence has precedence.` : ""}${safe(rejectedBucketNote(row))}</div>`;
  }

  function factorCoverageText(row) {
    const coverage = row.coverage || {};
    const aligned = `${coverage.observed_start || "—"}–${coverage.observed_end || "—"}`;
    const target = `${coverage.target_observed_start || "—"}–${coverage.target_observed_end || "—"}`;
    const factors = `${coverage.factor_observed_start || "—"}–${coverage.factor_observed_end || "—"}`;
    const lag = coverage.source_lag_calendar_days == null ? "—" : integer.format(coverage.source_lag_calendar_days);
    return `Aligned ${aligned}; strategy ${target}; factor source ${factors}; limiting input ${humanStatus(coverage.limiting_input || "UNAVAILABLE")}; source lag ${lag} calendar days`;
  }

  function dynamicAnalysisMarkup(row) {
    const dynamic = row.diagnostics?.dynamic;
    const path = dynamic?.exposure_path || [];
    if (row.estimator !== "DYNAMIC_FLS" || path.length < 2) return replicationAnalysisMarkup(row.series_id);
    const factorIds = [...new Set(path.flatMap((item) => Object.keys(item.betas || item.exposures || {})))];
    if (!factorIds.length) return replicationAnalysisMarkup(row.series_id);
    const colors = ["#56d6c9", "#7aa2f7", "#f4b860", "#d98cff", "#71d68d", "#ff7b8b"];
    const width = 920;
    const height = 280;
    const left = 54;
    const right = 18;
    const top = 18;
    const bottom = 34;
    const values = path.flatMap((item) => factorIds.map((factor) => Number((item.betas || item.exposures || {})[factor])).filter(Number.isFinite));
    const minimum = Math.min(0, ...values);
    const maximum = Math.max(0, ...values);
    const span = Math.max(maximum - minimum, 1e-9);
    const x = (index) => left + index / Math.max(path.length - 1, 1) * (width - left - right);
    const y = (value) => top + (maximum - value) / span * (height - top - bottom);
    const lines = factorIds.map((factor, index) => {
      const points = path.map((item, pointIndex) => `${x(pointIndex).toFixed(2)},${y(Number((item.betas || item.exposures || {})[factor] || 0)).toFixed(2)}`).join(" ");
      return `<polyline fill="none" stroke="${colors[index % colors.length]}" stroke-width="2" vector-effect="non-scaling-stroke" points="${points}"></polyline>`;
    }).join("");
    const holdoutIndex = path.findIndex((item) => String(item.estimation_state).startsWith("HOLDOUT"));
    const holdout = holdoutIndex >= 0 ? `<line class="holdout-marker" x1="${x(holdoutIndex)}" x2="${x(holdoutIndex)}" y1="${top}" y2="${height - bottom}"></line><text class="chart-label" x="${Math.min(x(holdoutIndex) + 6, width - 110)}" y="${top + 14}">sealed holdout</text>` : "";
    const legend = factorIds.map((factor, index) => `<span><i style="background:${colors[index % colors.length]}"></i>${safe(factor)}</span>`).join("");
    const latest = path[path.length - 1];
    const latestBetas = factorIds.map((factor) => `<div><span>${safe(factor)} beta</span><strong>${decimal((latest.betas || latest.exposures || {})[factor])}</strong></div>`).join("");
    const comparison = dynamic.holdout_comparison || {};
    return `<div class="callout callout-neutral"><strong>Method boundary:</strong> Clean-room dynamic FLS is MPI-informed and non-equivalent. It implements only the openly described methodology and does not claim proprietary MPI production output.</div><div class="grid grid-12 dynamic-analysis-grid"><div class="panel span-8"><div class="panel-heading"><div><div class="eyebrow">Effective-dated factor betas</div><h3>${safe(row.series_id)}</h3></div><span class="badge badge-positive">${safe(humanStatus(dynamic.validation || "UNAVAILABLE"))}</span></div><svg class="dynamic-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Dynamic factor beta path for ${safe(row.series_id)}"><line class="zero-line" x1="${left}" x2="${width - right}" y1="${y(0)}" y2="${y(0)}"></line>${holdout}${lines}<text class="chart-label" x="${left}" y="${height - 10}">${safe(path[0].date)}</text><text class="chart-label" text-anchor="end" x="${width - right}" y="${height - 10}">${safe(path[path.length - 1].date)}</text><text class="chart-label" x="6" y="${top + 10}">${decimal(maximum)}</text><text class="chart-label" x="6" y="${height - bottom}">${decimal(minimum)}</text></svg><div class="chart-legend">${legend}<span><i style="background:var(--warning)"></i>Holdout boundary</span></div><div class="metric-list">${latestBetas}</div></div><div class="panel span-4"><div class="panel-heading"><div><div class="eyebrow">Dynamic validation</div><h3>Same-factor static benchmark</h3></div></div><div class="metric-list"><div><span>Latest beta date</span><strong>${safe(latest.date)}</strong></div><div><span>Selected smoothness</span><strong>${safe(dynamic.selected_smoothness || "—")}</strong></div><div><span>Train-only selection R²</span><strong>${pct(dynamic.selection_predicted_r_squared)}</strong></div><div><span>Dynamic holdout R²</span><strong>${pct(comparison.dynamic_predicted_r_squared)}</strong></div><div><span>Static holdout R²</span><strong>${pct(comparison.static_predicted_r_squared)}</strong></div><div><span>Dynamic minus static</span><strong>${pct(comparison.dynamic_minus_static_predicted_r_squared)}</strong></div><div><span>Comparison state</span><strong>${safe(humanStatus(comparison.status || "UNAVAILABLE"))}</strong></div></div></div>${replicationAnalysisMarkup(row.series_id)}</div>`;
  }

  const ORTHOGONAL_DATED_ROW_CAP = 120;

  function orthogonalAttributionMarkup(row) {
    const attribution = row.orthogonal_attribution;
    if (!attribution) return "";
    const identity = row.analytics_identities?.orthogonal || {};
    const interval = row.interval_association || {};
    const coefficients = attribution.mode_coefficients;
    const perDate = Array.isArray(attribution.per_date) ? attribution.per_date : [];
    // F39: a static fit's coefficients are one set, shown once; the dated rows carry only what is
    // dated (the contribution) and are capped, with the count said.  A coefficient list with one
    // entry per date keeps its coefficient on each dated row.
    const staticCoefficients = coefficients && !Array.isArray(coefficients) ? coefficients : null;
    const allModeRows = perDate.flatMap((dated, index) => {
      const datedCoefficients = Array.isArray(coefficients) ? coefficients[index] : staticCoefficients ? {} : coefficients || {};
      const contributions = dated.mode_contributions || {};
      return [...new Set([...Object.keys(datedCoefficients || {}), ...Object.keys(contributions)])]
        .sort()
        .map((mode) => `<tr><td>${safe(dated.date || "—")}</td><td>${safe(humanStatus(dated.partition || "UNAVAILABLE"))}</td><td>${safe(mode)}</td><td class="numeric">${staticCoefficients ? "static fit" : decimal(datedCoefficients?.[mode])}</td><td class="numeric">${pct(contributions[mode], 3)}</td></tr>`);
    });
    const modeRows = allModeRows.slice(0, ORTHOGONAL_DATED_ROW_CAP);
    const staticRows = staticCoefficients
      ? Object.entries(staticCoefficients).map(([mode, value]) => `<tr><td>—</td><td>Train</td><td>${safe(mode)}</td><td class="numeric">${decimal(value)}</td><td class="numeric">—</td></tr>`)
      : [];
    const riskBudget = attribution.train_fitted_risk_budget || {};
    // F27: the Euler allocations are of order 1e-5 to 1e-3 and a three-decimal figure cannot rank
    // them, so each is shown as its share of the total of the same values.
    const eulerEntries = Object.entries(riskBudget.euler_allocations || {});
    const eulerTotal = sumOf(eulerEntries.map(([, value]) => Number(value)).filter(Number.isFinite));
    const riskRows = eulerEntries
      .map(([mode, value]) => `<tr><td>${safe(mode)}</td><td class="numeric">${eulerTotal > 0 && number(value) != null ? pct(Number(value) / eulerTotal, 2) : smallDecimal(value)}</td><td>${safe(humanStatus(riskBudget.interpretation || "UNAVAILABLE"))}</td></tr>`);
    const identityText = identity.surface === "ORTHOGONAL_RISK_ATTRIBUTION"
      ? `${humanStatus(identity.surface)} · ${identity.model_id || "model id not published"} · ${identity.methodology_manifest_sha256 || "methodology id not published"}`
      : "orthogonal identity not published";
    return `<div class="grid grid-12" style="margin-top:16px"><div class="panel span-12"><div class="panel-heading"><div><div class="eyebrow">Orthogonal risk attribution</div><h3>Mixed modes, separate from raw factor betas</h3></div><span class="badge ${attribution.status === "AVAILABLE" ? "badge-positive" : "badge-warning"}">${safe(humanStatus(attribution.status || "UNAVAILABLE"))}</span></div><div class="callout callout-neutral"><strong>Named surface:</strong> ${safe(identityText)}. ${safe(attribution.method_id || "method not published")}. Mode IDs are mixed orthogonal coordinates, not raw factor betas. <strong>Information set:</strong> ${safe(interval.evaluation_information_set || "UNAVAILABLE")}. <strong>Holdout interpretation:</strong> ${safe(interval.holdout_display_claim || "UNAVAILABLE")}</div>${(modeRows.length || staticRows.length) ? `<div class="table-wrap wide"><table id="orthogonal-coefficient-table"><caption>Orthogonal mixed-mode coefficients and dated contributions</caption>${tableMarkup([["Date"], ["Partition"], ["Mixed mode"], ["Coefficient (mode loading)", "numeric"], ["Contribution (return per period)", "numeric"]], [...staticRows, ...modeRows])}</table></div>${allModeRows.length > modeRows.length ? `<p class="cell-secondary">Showing the first ${integer.format(modeRows.length)} of ${integer.format(allModeRows.length)} dated contribution rows${staticCoefficients ? "; the coefficients are the static fit listed once above" : ""}.</p>` : ""}` : `<div class="callout callout-warning">No orthogonal mode coefficients or contributions are available for this selected model.</div>`}${riskRows.length ? `<div class="table-wrap"><table id="orthogonal-risk-table"><caption>Train-fitted orthogonal risk allocation</caption>${tableMarkup([["Mixed mode"], ["Euler allocation (share of total)", "numeric"], ["Risk interpretation"]], riskRows)}</table></div>` : ""}</div></div>`;
  }

  function renderFactorDetail() {
    const analysis = currentFactorAnalysis();
    const row = analysis.series.find((item) => item.series_id === selectedFactorSeries) || analysis.series[0];
    if (!row) return;
    selectedFactorSeries = row.series_id;
    const compositionMarkup = factorEvidenceComposition(row);
    const coverageText = factorCoverageText(row);
    $$("[data-factor-strategy]").forEach((button) => button.classList.toggle("is-active", button.dataset.factorStrategy === selectedFactorSeries));
    if (row.status !== "AVAILABLE") {
      const aligned = row.coverage?.aligned_observations ?? 0;
      if (row.status === "DIAGNOSTIC_ONLY") {
        const holdout = row.diagnostics?.holdout;
        const design = row.diagnostics?.training_design || {};
        const maximumVif = Math.max(...(design.vif || []).map((item) => Number(item.value)).filter(Number.isFinite), 0);
        const inference = row.diagnostics?.inference || {};
        const reason = row.model_quality?.gate_reason || (row.unavailable_reasons || []).join(" · ");
        const diagnosticBetaRows = (row.exposures || []).map((item) => `<tr><td>${safe(item.factor_id)}</td><td class="numeric">${decimal(item.beta ?? item.exposure)}</td><td>${safe(item.standard_error == null ? "Coefficient SE unavailable" : `SE ${decimal(item.standard_error)}`)}</td></tr>`);
        const diagnosticBetas = diagnosticBetaRows.length ? `<div class="panel"><div class="panel-heading"><div><div class="eyebrow">Forensic diagnostic</div><h3>Computed beta / loading</h3></div><span class="badge badge-warning">NOT PM-ACCEPTED</span></div><div class="table-wrap"><table id="factor-diagnostic-beta-table"><caption>Diagnostic factor betas</caption>${tableMarkup([["Factor"], ["Beta (return per unit factor return)", "numeric"], ["Uncertainty"]], diagnosticBetaRows)}</table></div></div>` : "";
        const diagnosticAlpha = row.alpha || {};
        const diagnosticAlphaMarkup = `<div class="analytics-detail-grid"><div class="metric-block"><div class="metric-label">Diagnostic net alpha</div><div class="metric-value">${pct(diagnosticAlpha.net_annualized)}</div><div class="metric-note">${safe(diagnosticAlpha.net_status || "UNAVAILABLE")}</div></div><div class="metric-block"><div class="metric-label">Diagnostic gross alpha</div><div class="metric-value">${pct(diagnosticAlpha.gross_annualized)}</div><div class="metric-note">${safe(diagnosticAlpha.gross_status || "UNAVAILABLE")}</div></div><div class="metric-block"><div class="metric-label">Diagnostic periodic alpha</div><div class="metric-value">${pct(diagnosticAlpha.periodic_intercept, 3)}</div><div class="metric-note">${safe(diagnosticAlpha.confidence_status || "UNAVAILABLE")} · annualized SE ${pct(diagnosticAlpha.annualized_standard_error)}</div></div></div>`;
        const diagnosticSummary = row.contribution_summary;
        const diagnosticContributionRows = diagnosticSummary ? [
          ...Object.entries(diagnosticSummary.factor_means).map(([factor, value]) => `<tr><td>${safe(factor)}</td><td>Factor contribution</td><td class="numeric">${pct(value, 3)}</td></tr>`),
          `<tr><td>Alpha</td><td>Periodic intercept contribution</td><td class="numeric">${pct(diagnosticSummary.alpha_mean, 3)}</td></tr>`,
          `<tr><td>Fitted</td><td>Factor plus alpha</td><td class="numeric">${pct(diagnosticSummary.fitted_mean, 3)}</td></tr>`,
          `<tr><td>Residual</td><td>Realized minus fitted</td><td class="numeric">${pct(diagnosticSummary.residual_mean, 3)}</td></tr>`,
          `<tr><td>Realized</td><td>Observed excess return</td><td class="numeric">${pct(diagnosticSummary.realized_mean, 3)}</td></tr>`,
        ] : [];
        const diagnosticContributions = diagnosticSummary ? `<div class="panel"><div class="panel-heading"><div><div class="eyebrow">Forensic diagnostic · holdout mean partition</div><h3>Realized = fitted + residual</h3></div><span class="badge badge-warning">NOT PM-ACCEPTED · n=${integer.format(diagnosticSummary.observations)}</span></div><p>Means use the dated model contribution rows, including changing dynamic betas. Maximum return identity error: ${safe(smallDecimal(diagnosticSummary.maximum_reconciliation_error))}.</p><div class="table-wrap"><table id="factor-diagnostic-contribution-table"><caption>Diagnostic holdout factor contributions and residuals</caption>${tableMarkup([["Component"], ["Role"], ["Mean", "numeric"]], diagnosticContributionRows)}</table></div></div>` : `<div class="callout callout-neutral">Diagnostic contribution and residual summary unavailable: no dated model contributions are supplied.</div>`;
        $("#factor-detail").innerHTML = `${compositionMarkup}<div class="state-separation"><div><span class="eyebrow">Source state</span><strong>${safe(humanStatus(sourceState(row)))}</strong><p>${integer.format(aligned)} aligned observations are retained.</p></div><div><span class="eyebrow">Model state</span><strong>${safe(humanStatus(modelState(row)))}</strong><p>${safe(humanStatus(reason))}. Sealed-holdout predicted R²: ${pct(holdout?.predicted_r_squared)} across ${integer.format(holdout?.observation_count || 0)} observations.</p></div></div><div class="callout callout-warning">The computed alpha, betas, contributions, and residuals remain visible for forensic review, but the sealed-holdout gate excludes them from portfolio-manager conclusions.</div>${diagnosticAlphaMarkup}${diagnosticBetas}${diagnosticContributions}<div class="callout callout-neutral"><strong>Training design:</strong> n=${integer.format(design.design_observations || 0)} · rank ${safe(design.matrix_rank ?? "—")}/${safe(design.factor_count ?? "—")} · condition ${decimal(design.matrix_condition_number)} · max VIF ${decimal(maximumVif)} · ${statusText(design.unregularized_status || "UNAVAILABLE")}. <strong>Inference:</strong> ${statusText(inference.status || "UNAVAILABLE")}.</div>${dynamicAnalysisMarkup(row)}${orthogonalAttributionMarkup(row)}`;
        prepareScrollableTableRegions();
        return;
      }
      $("#factor-detail").innerHTML = `${compositionMarkup}<div class="state-separation"><div><span class="eyebrow">Source state</span><strong>${safe(humanStatus(sourceState(row)))}</strong><p>Provider or account return evidence is assessed independently of this factor universe.</p></div><div><span class="eyebrow">Model state</span><strong>${safe(humanStatus(modelState(row)))}</strong><p>${safe((row.unavailable_reasons || []).map(humanStatus).join(" · ") || "No accepted model")}. Aligned model observations: ${integer.format(aligned)}.</p></div></div><div class="callout callout-danger">This is a model or decomposition-universe limitation. It does not relabel accepted Volatis, Robuxio, or other provider/account source data as missing. No beta or alpha is inferred.</div>${replicationAnalysisMarkup(row.series_id)}`;
      prepareScrollableTableRegions();
      return;
    }
    const alpha = row.alpha;
    const holdout = row.diagnostics.holdout;
    const inference = row.diagnostics.inference || {};
    const trainResiduals = row.diagnostics.residuals?.train || {};
    const design = row.diagnostics.training_design || {};
    const maximumVif = Math.max(...(design.vif || []).map((item) => Number(item.value)).filter(Number.isFinite), 0);
    const summary = row.contribution_summary;
    const maximumExposure = Math.max(...row.exposures.map((item) => Math.abs(Number(item.beta ?? item.exposure))), 1);
    const exposures = row.exposures.map((item) => {
      const exposure = Number(item.beta ?? item.exposure);
      const width = Math.min(48, Math.abs(exposure) / maximumExposure * 48);
      const left = exposure < 0 ? 50 - width : 50;
      const uncertainty = item.standard_error == null ? "Coefficient SE unavailable for fitted method" : `SE ${decimal(item.standard_error)}`;
      return `<div class="exposure-row"><div class="exposure-name"><strong>${safe(item.factor_id)}</strong><span>${safe(uncertainty)}</span></div><div class="exposure-track" aria-hidden="true"><span class="exposure-fill ${exposure < 0 ? "is-negative" : ""}" style="left:${left}%;width:${width}%"></span></div><div class="exposure-value">β ${decimal(exposure)}</div></div>`;
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
      ? `<div class="panel span-12"><div class="panel-heading"><div><div class="eyebrow">Governed candidate universe</div><h3>Economic rationale and train-only disposition</h3></div><span class="badge">${integer.format(proxyRows.length)} proxies</span></div><div class="table-wrap wide"><table id="factor-proxy-table"><caption>Factor proxy governance</caption>${tableMarkup([["Proxy / family"], ["Source"], ["Effective interval"], ["Disposition"], ["Selection rationale"]], proxyRows)}</table></div></div>`
      : "";
    $("#factor-detail").innerHTML = `${compositionMarkup}<div class="analytics-detail-grid"><div class="metric-block"><div class="metric-label">Estimator</div><div class="metric-value metric-value-compact">${statusText(row.estimator)}</div><div class="metric-note">${safe(analysis.method_label)}</div></div><div class="metric-block"><div class="metric-label">Net alpha</div><div class="metric-value">${pct(alpha.net_annualized)}</div><div class="metric-note">${safe(alpha.net_status)} · observed basis</div></div><div class="metric-block"><div class="metric-label">Gross alpha</div><div class="metric-value">${pct(alpha.gross_annualized)}</div><div class="metric-note">${safe(alpha.gross_status)}</div></div><div class="metric-block"><div class="metric-label">Holdout predicted R²</div><div class="metric-value">${pct(holdout.predicted_r_squared)}</div><div class="metric-note">${integer.format(holdout.observation_count)} sealed out-of-sample observations; above zero beats the training-mean baseline</div></div><div class="metric-block"><div class="metric-label">Tracking error</div><div class="metric-value">${pct(holdout.tracking_error_annualized)}</div><div class="metric-note">Annualized holdout residual volatility</div></div><div class="metric-block"><div class="metric-label">Exact-date model coverage</div><div class="metric-value">${integer.format(row.coverage.aligned_observations)}</div><div class="metric-note">${safe(coverageText)}</div></div><div class="metric-block"><div class="metric-label">Training design</div><div class="metric-value">${integer.format(design.design_observations || 0)} obs.</div><div class="metric-note">Rank ${safe(design.matrix_rank ?? "—")}/${safe(design.factor_count ?? "—")} · condition ${decimal(design.matrix_condition_number)} · max VIF ${decimal(maximumVif)} · ${safe(humanStatus(design.unregularized_status || "UNAVAILABLE"))}</div></div><div class="metric-block"><div class="metric-label">HAC inference state</div><div class="metric-value metric-value-compact">${statusText(inference.status || "UNAVAILABLE")}</div><div class="metric-note">Default lag ${safe(inference.default_lag ?? "—")} · no p-values or confidence intervals</div></div><div class="metric-block"><div class="metric-label">Train residual diagnostics</div><div class="metric-value">DW ${decimal(trainResiduals.durbin_watson)}</div><div class="metric-note">Lag-1 residual autocorrelation ${decimal(trainResiduals.autocorrelation?.["1"])}</div></div><div class="metric-block"><div class="metric-label">Reconciliation error</div><div class="metric-value">${safe(smallDecimal(summary.maximum_reconciliation_error))}</div><div class="metric-note">Maximum absolute return identity error</div></div></div><div class="callout callout-neutral"><strong>Model boundary:</strong> ${safe(methodBoundary)} ${safe(inference.post_selection_caveat || "")}</div><div class="grid grid-12" style="margin-top:16px"><div class="panel span-7"><div class="panel-heading"><div><div class="eyebrow">Latest accepted betas / loadings</div><h3>${safe(row.series_id)}</h3></div><span class="badge badge-positive">${safe(humanStatus(row.evidence_layer))}</span></div><div class="exposure-list">${exposures}</div></div><div class="panel span-5"><div class="panel-heading"><div><div class="eyebrow">Holdout mean partition</div><h3>Realized = fitted + residual</h3></div><span class="badge">n=${integer.format(summary.observations)}</span></div><div class="table-wrap"><table id="factor-contribution-table"><caption>Holdout factor contribution reconciliation</caption>${tableMarkup([["Component"], ["Role"], ["Mean", "numeric"]], contributionRows)}</table></div></div>${proxyTable}</div>${orthogonalAttributionMarkup(row)}${dynamicAnalysisMarkup(row)}`;
    prepareScrollableTableRegions();
  }

  // K7: the counts are true of the rendered lens and source only, so each subtitle says which.
  function renderFactorKpis(analysis = currentFactorAnalysis()) {
    const sourceLabel = (Array.isArray(analysis.sources) ? analysis.sources : []).map((item) => item.source_label || item.source_key).filter(Boolean).join(" + ") || "single accepted source";
    const scope = `${analysis.lens_label || analysis.lens_id} · ${sourceLabel}`;
    renderKpis("#factor-kpis", [
      ["PM-usable models", `${analysis.available_count}/${analysis.series.length}`, `Positive sealed-holdout predicted R² required · ${scope}`, analysis.available_count ? "positive" : "danger"],
      ["Diagnostic only", String(analysis.diagnostic_count || 0), `Computed fits excluded from alpha and exposure conclusions · ${scope}`, "warning"],
      ["Unavailable", String(analysis.unavailable_count), `No inferred exposures or alpha · ${scope}`, "danger"],
      ["Methodology", analysis.methodology_version, `${analysis.method_label} · ${analysis.lens_label || analysis.lens_id}`, "warning"],
    ]);
  }

  function renderAttribution() {
    const analysis = currentFactorAnalysis();
    $$('[data-factor-evidence-mode]').forEach((button) => {
      const available = Boolean(factorLensModes[button.dataset.factorEvidenceMode]);
      const active = button.dataset.factorEvidenceMode === selectedFactorEvidenceMode;
      button.disabled = !available;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
      button.title = available ? "" : "No accepted version-compatible backtest decomposition is published for this mode.";
      button.onclick = () => {
        if (!available) return;
        selectedFactorEvidenceMode = button.dataset.factorEvidenceMode;
        selectedFactorSeries = null;
        if (!activeFactorLenses().some((item) => item.lens_id === selectedFactorLens)) selectedFactorLens = activeFactorLenses()[0]?.lens_id;
        selectedFactorSurface = currentFactorLens()?.default_surface || "STATIC";
        selectedFactorSource = null;
        renderAttribution();
      };
    });
    const firstAvailable = analysis.series.find((row) => row.status === "AVAILABLE");
    if (selectedContextStrategy !== "ALL" && analysis.series.some((row) => row.series_id === selectedContextStrategy)) selectedFactorSeries = selectedContextStrategy;
    if (!analysis.series.some((row) => row.series_id === selectedFactorSeries)) selectedFactorSeries = null;
    selectedFactorSeries ||= firstAvailable?.series_id || analysis.series[0]?.series_id;
    $("#factor-lens-selector").innerHTML = activeFactorLenses().map((item) => `<button type="button" class="segment ${item.lens_id === selectedFactorLens ? "is-active" : ""}" data-factor-lens="${safe(item.lens_id)}" aria-pressed="${item.lens_id === selectedFactorLens}">${safe(item.lens_label || item.lens_id)}</button>`).join("");
    $$('[data-factor-lens]').forEach((button) => button.addEventListener("click", () => { selectedFactorLens = button.dataset.factorLens; selectedFactorSurface = currentFactorLens()?.default_surface || "STATIC"; selectedFactorSource = null; selectedFactorSeries = null; renderAttribution(); }));
    const lensRecord = currentFactorLens();
    const surfaces = lensRecord?.surfaces || { [lensRecord?.default_surface || "STATIC"]: lensRecord };
    const surfaceLabels = { STATIC: "Static RBSA", DYNAMIC_FLS: "Dynamic FLS" };
    $("#factor-surface-selector").innerHTML = Object.keys(surfaces).map((surface) => `<button type="button" class="segment ${surface === selectedFactorSurface ? "is-active" : ""}" data-factor-surface="${safe(surface)}" aria-pressed="${surface === selectedFactorSurface}">${safe(surfaceLabels[surface] || humanStatus(surface))}</button>`).join("");
    $$('[data-factor-surface]').forEach((button) => button.addEventListener("click", () => { selectedFactorSurface = button.dataset.factorSurface; selectedFactorSource = null; selectedFactorSeries = null; renderAttribution(); }));
    const surfaceAnalysis = currentFactorSurfaceAnalysis();
    const sourceOptions = (Array.isArray(surfaceAnalysis.sources) ? surfaceAnalysis.sources : []).map((item) => ({ key: item.source_key, label: item.source_label || item.source_key }));
    $("#factor-source-selector").innerHTML = sourceOptions.map((item) => `<button type="button" class="segment ${item.key === selectedFactorSource ? "is-active" : ""}" data-factor-source="${safe(item.key)}" aria-pressed="${item.key === selectedFactorSource}">${safe(item.label)}</button>`).join("");
    $$('[data-factor-source]').forEach((button) => button.addEventListener("click", () => { selectedFactorSource = button.dataset.factorSource; selectedFactorSeries = null; renderAttribution(); }));
    const sourceRows = Array.isArray(analysis.sources) ? analysis.sources : [];
    // F24: lenses are dated separately; one older than the cutoff says by how many days.
    const lensDate = (item) => item.as_of_date ?? analysis.as_of_date;
    const sourceSummaryHtml = sourceRows.map((item) => `${safe(item.source_label || item.source_key)} · through ${safe(item.as_of_date)}${staleSuffix(lensDate(item))}`).join(" · ") || `Single accepted historical proof generation${analysis.as_of_date ? ` through ${safe(analysis.as_of_date)}${staleSuffix(analysis.as_of_date)}` : ""}`;
    const proxyGovernance = analysis.proxy_governance || {};
    const semantics = lensSemantics[analysis.lens_id] || { question: "What return drivers explain the strategy?", method: "Returns-based decomposition.", beta: "Beta is fitted return sensitivity.", caveat: "No holdings inference is made." };
    $("#factor-lens-boundary").innerHTML = `<strong>${safe(analysis.lens_label || analysis.lens_id)}</strong> · ${safe(humanStatus(analysis.return_basis || "UNAVAILABLE"))} · ${safe(humanStatus(selectedFactorEvidenceMode))} · ${safe(surfaceLabels[selectedFactorSurface] || humanStatus(selectedFactorSurface))}<br><strong>Question:</strong> ${safe(semantics.question)} <strong>Method:</strong> ${safe(semantics.method)} <strong>Beta:</strong> ${safe(semantics.beta)} <strong>Boundary:</strong> ${safe(semantics.caveat)}<br><strong>Selection boundary:</strong> ${safe(humanStatus(proxyGovernance.selection_boundary || "UNAVAILABLE_NOT_DECLARED"))}. ${sourceSummaryHtml}. ${integer.format(proxyGovernance.candidate_count || 0)} governed candidates; ${safe(humanStatus(proxyGovernance.orthogonality_claim || "UNAVAILABLE"))}. Static and dynamic are separately planned, computed, and published surfaces. Live only is authoritative; live + backtest uses only compatible pre-live history. Lenses remain separate and are never concatenated.`;
    renderUnderwriting();
    renderFactorKpis(analysis);
    $("#factor-method-badge").textContent = analysis.method_label.replaceAll("_", " ");
    const rows = analysis.series.map((row) => {
      const holdout = row.diagnostics?.holdout;
      const strategy = strategyByName(row.series_id);
      const decision = row.model_quality?.decision_status || row.status;
      const providerCoverage = strategy?.provider_evidence ? `<span class="cell-secondary">${safe(evidenceCoverage(strategy))}</span>` : "";
      const betas = row.exposures.map((item) => `${item.factor_id} β ${decimal(item.beta ?? item.exposure)}`).join(" · ") || "—";
      const composition = row.series_composition || {};
      const compositionText = composition.status
        ? `${integer.format(composition.live_observations || 0)} live + ${integer.format(composition.backtest_observations_used || 0)} backtest<span class="cell-secondary">${safe(humanStatus(composition.status))} · inception ${formatDateHtml(composition.live_inception)}</span>`
        : row.status === "UNAVAILABLE" ? `No composition: series unavailable<span class="cell-secondary">No backtest-history claim</span>` : `Undeclared legacy composition<span class="cell-secondary">No backtest-history claim</span>`;
      const coverageText = factorCoverageText(row);
      return `<tr><td><span class="cell-primary">${safe(row.series_id)}</span><span class="cell-secondary">${safe(row.model_id || "NO ACCEPTED MODEL")}</span></td><td>${statusText(sourceState(row))}${providerCoverage}</td><td>${statusText(modelState(row))}<span class="cell-secondary">${statusText(decision)}</span></td><td>${safe(row.source_label || "—")}</td><td>${statusText(row.estimator || "UNAVAILABLE")}</td><td>${safe(betas)}</td><td>${compositionText}</td><td><span class="cell-primary">${safe(row.coverage?.observed_start || "—")}–${safe(row.coverage?.observed_end || "—")}</span><span class="cell-secondary">${safe(coverageText)}</span></td><td class="numeric">${row.coverage ? integer.format(row.coverage.aligned_observations) : "—"}</td><td class="numeric">${pct(holdout?.predicted_r_squared)}</td><td class="numeric">${pct(row.alpha?.net_annualized)}</td><td>${statusText(row.alpha?.gross_status || row.model_quality?.gate_reason || (row.unavailable_reasons || []).join(" · "))}</td></tr>`;
    });
    $("#factor-model-table").innerHTML = tableMarkup([["Strategy / model"], ["Source returns"], ["Model state"], ["Model input source"], ["Estimator"], ["Beta / loading"], ["Evidence composition"], ["Aligned / source intervals"], ["Aligned (observations)", "numeric"], ["Sealed holdout R²", "numeric"], ["Accepted net alpha", "numeric"], ["Alpha / gate state"]], rows);
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
  const dependenceModes = data?.dependence_modes || { LIVE_ONLY: data?.dependence_analysis };
  let selectedEvidenceMode = dependenceModes[data?.default_dependence_mode] ? data?.default_dependence_mode : "LIVE_ONLY";
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
    // F28: with a horizon or drawdown window selected, the selected-period figure is that window's own.
    // A window that has no value (or is absent for this pair) shows no value and its status; it never
    // falls back to the full-overlap Pearson, which is shown on its own line.
    const windowSelected = selectedCorrelationWindow !== "full";
    const selectedCorrelation = windowSelected ? selectedPeriod?.correlation : observed.pearson;
    const selectedObservations = windowSelected ? selectedPeriod?.observations : coverage.common_observations;
    const composition = (series) => {
      const item = dependence.series_composition?.[series];
      if (!item) return "";
      return `${item.strategy_version} · live inception ${item.live_inception || "—"} · ${integer.format(item.live_observations || 0)} live · ${integer.format(item.backtest_observations_used || 0)} backtest used · ${integer.format(item.backtest_observations_excluded_at_or_after_live_inception || 0)} overlapping backtest excluded`;
    };
    const compositionBlock = dependence.series_composition?.[left] && dependence.series_composition?.[right]
      ? `<div class="callout callout-neutral"><strong>${safe(shortSeries(left))}</strong>: ${safe(composition(left))}<br /><strong>${safe(shortSeries(right))}</strong>: ${safe(composition(right))}</div>`
      : "";
    $("#correlation-detail-title").textContent = `${shortSeries(left)} × ${shortSeries(right)}`;
    detail.innerHTML = `<div class="validation-card"><div><strong>Observed status</strong><span>${safe(coverage.evidence_layer ? humanStatus(coverage.evidence_layer) : "No compatible measured layer")}</span></div><div class="validation-score">${statusText(observed.status)}</div></div><div class="evidence-grid"><dl class="detail-list"><div><dt>Full-overlap Pearson</dt><dd>${decimal(observed.pearson)}</dd></div><div><dt>Spearman</dt><dd>${decimal(observed.spearman)}</dd></div><div><dt>Selected period</dt><dd>${decimal(selectedCorrelation)}</dd></div>${windowSelected ? `<div><dt>Selected-period status</dt><dd>${statusText(selectedPeriod?.status || "UNAVAILABLE")}</dd></div>` : ""}<div><dt>Selected sample</dt><dd>${selectedObservations == null ? "—" : integer.format(selectedObservations)}${windowSelected ? " weekday observations" : ""}</dd></div><div><dt>Selected interval</dt><dd>${safe((windowSelected ? selectedPeriod?.observed_start : coverage.observed_start) || "—")}–${safe((windowSelected ? selectedPeriod?.observed_end : coverage.observed_end) || "—")}</dd></div><div><dt>Rolling window</dt><dd>${integer.format(observed.rolling_pearson.window_observations || 0)} observations · ${decimal(observed.rolling_pearson.latest)}</dd></div></dl><dl class="detail-list"><div><dt>Calendar</dt><dd>${safe(humanStatus(coverage.frequency))}</dd></div><div><dt>Missing observations left / right</dt><dd>${integer.format(coverage.missing_from_left)} / ${integer.format(coverage.missing_from_right)}</dd></div><div><dt>Either in drawdown</dt><dd>${decimal(observed.drawdown_regimes?.either_strategy_underwater?.correlation)} · ${integer.format(observed.drawdown_regimes?.either_strategy_underwater?.observations || 0)} obs.</dd></div><div><dt>Both in drawdown</dt><dd>${decimal(observed.drawdown_regimes?.both_strategies_underwater?.correlation)} · ${integer.format(observed.drawdown_regimes?.both_strategies_underwater?.observations || 0)} obs.</dd></div><div><dt>Conceptual prior</dt><dd>${decimal(blend.conceptual_prior)}</dd></div><div><dt>Implementation mapping</dt><dd>${statusText(implementation.status)}</dd></div></dl></div>${compositionBlock}<div class="callout ${observed.status === "AVAILABLE" ? "callout-neutral" : "callout-danger"}">${statusText(blend.decision_status)}. ${safe(blend.conceptual_prior_note)}</div>`;
  }

  function renderCorrelationMatrix() {
    const dependence = currentDependence();
    let matrix = dependence.matrices[selectedMatrix];
    const series = dependence.series_ids;
    // The heading counts what the matrix below draws, never a literal.
    $("#matrix-eyebrow").textContent = `${dependence.series_ids.length} current strategies`;
    let label = matrixLabels[selectedMatrix];
    if (selectedMatrix === "observed_pearson" && selectedCorrelationWindow !== "full") {
      if (dependence.horizon_matrices[selectedCorrelationWindow]) {
        matrix = dependence.horizon_matrices[selectedCorrelationWindow];
        label = [`Observed Pearson · ${selectedCorrelationWindow} weekday observations`, "Trailing accepted weekday observations · no holiday inference"];
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
    prepareScrollableTableRegions();
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
    const centralCase = dependence.central_case;
    const clusterBudget = data.dependence_analysis.economic_driver_cluster_budget;
    const unavailable = dependence.pair_count - dependence.available_pair_count;
    const combined = selectedEvidenceMode === "LIVE_PLUS_BACKTEST";
    const modeBadge = $("#evidence-mode-badge");
    modeBadge.textContent = combined ? "HYBRID BACKTEST + LIVE" : "LIVE ONLY · AUTHORITY";
    modeBadge.classList.toggle("badge-warning", combined);
    modeBadge.classList.toggle("badge-positive", !combined);
    // F45: a partly eligible hybrid view names the strategies left out of it, not only a tooltip.
    const blockedHybrid = Array.isArray(data.hybrid_mode_gate?.blocked_strategies) ? data.hybrid_mode_gate.blocked_strategies : [];
    const blockedHybridText = blockedHybrid.length ? ` Excluded from this view (live only): ${blockedHybrid.map((item) => `${item.strategy_name} (${humanStatus(item.compatibility_status || "UNAVAILABLE").toLowerCase()})`).join("; ")}.` : "";
    $("#evidence-mode-boundary").textContent = combined
      ? `Exploratory hybrid view: only version-matched backtest observations before live inception are included. Live observations win from inception onward. This mode cannot change readiness, allocation, or risk-control authority.${blockedHybridText}`
      : "Live-only evidence is the authoritative default for readiness, allocation, and risk controls.";
    $$('[data-evidence-mode]').forEach((button) => {
      const available = Boolean(dependenceModes[button.dataset.evidenceMode]);
      button.disabled = !available;
      const active = button.dataset.evidenceMode === selectedEvidenceMode;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
      if (!available) button.title = button.dataset.evidenceMode === "LIVE_PLUS_BACKTEST" && data.hybrid_mode_gate.status !== "ELIGIBLE_PROVEN_COMPATIBLE" ? data.hybrid_mode_gate.reason : "This accepted correlation generation has no backtest mode";
      else if (button.dataset.evidenceMode === "LIVE_PLUS_BACKTEST" && data.hybrid_mode_gate?.status !== "ELIGIBLE_PROVEN_COMPATIBLE" && data.hybrid_mode_gate?.reason) button.title = data.hybrid_mode_gate.reason;
    });
    renderKpis("#dependence-kpis", [
      ["Measured pairs", `${dependence.available_pair_count}/${dependence.pair_count}`, combined ? "Version-matched pre-live backtest plus authoritative live" : "Exact-date live observations", "positive"],
      ["Unavailable pairs", String(unavailable), "No inferred diversification credit", "danger"],
      ["Correlation-one bound", pct(corrOne.loss), `${number(clusterBudget.limit_breach) == null ? "—" : (Number(clusterBudget.limit_breach) * 100).toFixed(1)} pp above the ${pct(clusterBudget.systematic_loss_limit)} systematic limit; governs readiness`, "danger"],
      ["Analytics as of", formatDate(dependence.as_of_date), `Generation ${dependence.generation_id.slice(0, 12)}…`],
    ]);
    selectedPair ||= pairKey(dependence.cells.find((item) => item.observed.status === "AVAILABLE")?.left || dependence.series_ids[0], dependence.cells.find((item) => item.observed.status === "AVAILABLE")?.right || dependence.series_ids[1]);
    renderCorrelationMatrix();
    const selected = dependence.cells.find((item) => pairKey(item.left, item.right) === selectedPair) || dependence.cells[0];
    renderCorrelationDetail(selected.left, selected.right);
    const centralLoss = centralCase.loss == null ? "Unavailable" : pct(centralCase.loss);
    $("#correlation-one-summary").innerHTML = `<div class="metric-block"><div class="metric-label">Conservative bound</div><div class="metric-value">${pct(corrOne.loss)}</div><div class="metric-note">Governs readiness · ${safe(humanStatus(corrOne.method || corrOne.status))}</div></div><div class="metric-block"><div class="metric-label">Systematic limit</div><div class="metric-value">${pct(clusterBudget.systematic_loss_limit)}</div><div class="metric-note">Breach ${pct(clusterBudget.limit_breach)}</div></div><div class="metric-block"><div class="metric-label">Central case</div><div class="metric-value">${centralLoss}</div><div class="metric-note">Descriptive only · ${safe(centralCase.status)}</div></div><div class="metric-block"><div class="metric-label">Reconciliation</div><div class="metric-value">${safe(smallDecimal(corrOne.reconciliation_difference))}</div><div class="metric-note">${safe(humanStatus(corrOne.status))}</div></div>`;
    const componentRows = corrOne.components.map((row) => `<tr><td>${safe(row.series_id)}</td><td class="numeric">${pct(row.allocation_weight)}</td><td class="numeric">${pct(row.conservative_loss_fraction ?? row.adverse_loss_fraction)}</td><td class="numeric">${pct(row.loss_contribution)}</td></tr>`);
    // F20: weight and contribution add across the components shown; the conservative loss fraction does not.
    const componentWeights = corrOne.components.map((row) => number(row.allocation_weight)).filter((value) => value != null);
    const componentLosses = corrOne.components.map((row) => number(row.loss_contribution)).filter((value) => value != null);
    const componentFooter = componentRows.length ? `<tr><th scope="row">Total</th><td class="numeric">${componentWeights.length ? pct(sumOf(componentWeights)) : "—"}</td><td class="numeric"></td><td class="numeric">${componentLosses.length ? pct(sumOf(componentLosses)) : "—"}</td></tr>` : "";
    $("#correlation-one-table").innerHTML = tableMarkup([["Strategy"], ["Weight", "numeric"], ["Conservative loss", "numeric"], ["Contribution", "numeric"]], componentRows.length ? componentRows : [emptyRow(4, "No component detail in this accepted fixture")], null, componentFooter);
    $("#economic-cluster-summary").innerHTML = `<strong>${statusText(clusterBudget.status)}</strong> · ${safe(humanStatus(clusterBudget.budget_rule))}. The frozen audit reference was ${pct(clusterBudget.historical_audit_reference.full_systematic_loss)} and ${pct(clusterBudget.historical_audit_reference.after_rejected_sleeves_loss)} after rejected sleeves; the operative control above is recomputed from current accepted components.`;
    const clusterRows = clusterBudget.clusters.map((cluster) => `<tr><td><span class="cell-primary">${safe(cluster.label)}</span><span class="cell-secondary">${safe(cluster.cluster_id)}</span></td><td>${safe(cluster.members.map(shortSeries).join(" · "))}</td><td class="numeric">${pct(cluster.allocation_weight)}</td><td class="numeric">${pct(cluster.loss_contribution)}</td><td>${cluster.observed_pair_correlation_minimum == null ? "—" : `${decimal(cluster.observed_pair_correlation_minimum)}–${decimal(cluster.observed_pair_correlation_maximum)}`}</td><td>${statusText(cluster.budget_status)}<span class="cell-secondary">${safe(cluster.rationale)}</span></td></tr>`);
    const clusterWeights = clusterBudget.clusters.map((cluster) => number(cluster.allocation_weight)).filter((value) => value != null);
    const clusterLosses = clusterBudget.clusters.map((cluster) => number(cluster.loss_contribution)).filter((value) => value != null);
    const clusterFooter = clusterRows.length ? `<tr><th scope="row" colspan="2">Total</th><td class="numeric">${clusterWeights.length ? pct(sumOf(clusterWeights)) : "—"}</td><td class="numeric">${clusterLosses.length ? pct(sumOf(clusterLosses)) : "—"}</td><td colspan="2"></td></tr>` : "";
    $("#economic-cluster-table").innerHTML = tableMarkup([["Driver cluster"], ["Members"], ["Aggregate weight", "numeric"], ["Adverse-loss contribution", "numeric"], ["Observed pair range"], ["Budget state / rationale"]], clusterRows, null, clusterFooter);
  }

  const weekly = data?.weekly_returns;
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
  let includeCurrentWeek = weekly?.current_week?.default_visible === true;
  let selectedWeeklyCell = null;

  function weeklyValue(row, week) {
    if (performanceLineageMismatch && row.primary_group === "STRATEGY") return null;
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
    if (performanceLineageMismatch && row.primary_group === "STRATEGY") {
      $("#weekly-detail-title").textContent = `${row.display_name} · week of ${selectedWeeklyCell.week}`;
      $("#weekly-detail").innerHTML = `<div class="validation-card"><div><strong>Weekly return withheld</strong><span>— UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${safe(performanceLineageReason)}</span></div><div class="validation-score">UNAVAILABLE_SOURCE_LINEAGE_MISMATCH</div></div><div class="callout callout-neutral">Strategy weekly return, observation count, and return-path evidence are withheld until the source-lineage identity matches.</div>`;
      return;
    }
    const value = weeklyValue(row, selectedWeeklyCell.week);
    const meta = weeklyMeta(row, selectedWeeklyCell.week);
    $("#weekly-detail-title").textContent = `${row.display_name} · week of ${selectedWeeklyCell.week}`;
    $("#weekly-detail").innerHTML = `<div class="validation-card"><div><strong>Weekly return</strong><span>${value == null ? "No accepted observation in this week" : "Compounded from accepted daily returns"}</span></div><div class="validation-score">${value == null ? "UNAVAILABLE" : pct(value, 2)}</div></div><div class="evidence-grid"><dl class="detail-list"><div><dt>Canonical series</dt><dd>${safe(row.series_id)}</dd></div><div><dt>Group</dt><dd>${safe(weeklyGroupLabels[row.primary_group] || row.primary_group)}</dd></div><div><dt>Return basis</dt><dd>${statusText(row.return_basis)}</dd></div><div><dt>Calendar week</dt><dd>${safe(selectedWeeklyCell.week)} — ${safe(weeklyEnd(selectedWeeklyCell.week))}</dd></div></dl><dl class="detail-list"><div><dt>Source</dt><dd>${safe(humanStatus(String(row.source_key || "").replaceAll("-", "_")))}</dd></div><div><dt>Evidence mode</dt><dd>${row.primary_group === "STRATEGY" ? statusText(selectedWeeklyMode) : "NOT APPLICABLE"}</dd></div><div><dt>Accepted observations</dt><dd>${meta ? `${meta.dailyCount} · ${safe(meta.actualStart)} — ${safe(meta.actualEnd)}` : "UNAVAILABLE"}</dd></div><div><dt>Coverage / calendar</dt><dd>${meta ? `${statusText(meta.coverageStatus)} · ${statusText(meta.calendarBasis)}${meta.boundaryReasons.length ? ` · ${meta.boundaryReasons.map(statusText).join(", ")}` : ""}` : statusText(row.absent_value_status)}</dd></div></dl></div>`;
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
      performanceLineageMismatch && selectedWeeklyGroup === "STRATEGY"
        ? ["Strategy evidence", "—", `UNAVAILABLE_SOURCE_LINEAGE_MISMATCH · ${performanceLineageReason}`, "danger"]
        : ["Strategy evidence", humanStatus(selectedWeeklyMode), selectedWeeklyMode === "LIVE_ONLY" ? "Authoritative default" : "Version-matched pre-live backtest", selectedWeeklyMode === "LIVE_ONLY" ? "positive" : "warning"],
    ]);
    const header = `<thead><tr><th scope="col">Series / basis</th>${weeks.map((week) => `<th scope="col"><span class="weekly-date">${safe(week.slice(5))}</span><span class="cell-secondary">${safe(week.slice(0, 4))}</span></th>`).join("")}</tr></thead>`;
    const body = rows.map((row) => `<tr><th scope="row"><span class="cell-primary">${safe(row.display_name)}</span><span class="cell-secondary">${safe(humanStatus(row.return_basis))} · ${safe(humanStatus(String(row.source_key || "").replaceAll("-", "_")))}</span></th>${weeks.map((week) => {
      const value = weeklyValue(row, week);
      const meta = weeklyMeta(row, week);
      const numeric = value == null ? null : Number(value);
      const intensity = numeric == null ? 0 : Math.min(1, Math.abs(numeric) / WEEKLY_FULL_INTENSITY_RETURN);
      const partial = meta?.coverageStatus === "PARTIAL_BOUNDARY";
      return `<td><button type="button" class="weekly-cell ${numeric == null ? "is-unavailable" : numeric < 0 ? "is-negative" : "is-positive"} ${partial ? "is-partial" : ""}" style="--weekly-intensity:${intensity}" data-weekly-series="${safe(row.series_id)}" data-weekly-week="${safe(week)}" aria-label="${safe(row.display_name)} ${safe(week)} ${numeric == null ? "unavailable" : pct(value, 2)}${partial ? " partial boundary" : ""}">${numeric == null ? "—" : pct(value, 1)}</button></td>`;
    }).join("")}</tr>`).join("");
    const currentWeek = weekly.current_week || {};
    const currentWeekVisible = weeks.includes(currentWeek.week_start);
    const partialLabel = currentWeekVisible && currentWeek.is_partial === true
      ? ` · PARTIAL · data through ${safe(currentWeek.data_through_date || "unavailable")}`
      : "";
    $("#weekly-table").innerHTML = `<table class="weekly-table"><caption>Weekly returns${partialLabel}; absent cells are unavailable, not zero</caption>${header}<tbody>${body || `<tr><td colspan="${weeks.length + 1}">No series match the current filter.</td></tr>`}</tbody></table>`;
    prepareScrollableTableRegions();
    const periodTitle = selectedWeeklyWindow === "full" ? `Full history · 52-week page ${weeklyPage + 1}` : `Latest ${selectedWeeklyWindow} weeks`;
    $("#weekly-table-title").textContent = `${periodTitle}${partialLabel}`;
    $("#weekly-page-older").disabled = selectedWeeklyWindow !== "full" || weeks[0] === (weekly.weeks || [])[0];
    $("#weekly-page-newer").disabled = selectedWeeklyWindow !== "full" || weeklyPage === 0;
    $$('[data-weekly-series]').forEach((button) => button.addEventListener("click", () => {
      selectedWeeklyCell = { seriesId: button.dataset.weeklySeries, week: button.dataset.weeklyWeek };
      renderWeeklyDetail();
    }));
    renderWeeklyDetail();
  }

  function renderWeekly() {
    $("#weekly-current-toggle").checked = includeCurrentWeek;
    const groups = ["ALL", ...(weekly?.display_groups || [])];
    $("#weekly-group-selector").innerHTML = groups.map((group) => `<button type="button" class="segment ${group === selectedWeeklyGroup ? "is-active" : ""}" data-weekly-group="${safe(group)}" aria-pressed="${group === selectedWeeklyGroup}">${safe(weeklyGroupLabels[group] || group)}</button>`).join("");
    $$('[data-weekly-group]').forEach((button) => button.addEventListener("click", () => {
      selectedWeeklyGroup = button.dataset.weeklyGroup;
      weeklyPage = 0;
      selectedWeeklyCell = null;
      renderWeekly();
    }));
    $$('[data-weekly-mode]').forEach((button) => {
      const available = (weekly?.strategy_evidence_modes || []).includes(button.dataset.weeklyMode);
      button.disabled = !available;
      const active = button.dataset.weeklyMode === selectedWeeklyMode;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
      if (!available) button.title = button.dataset.weeklyMode === "LIVE_PLUS_BACKTEST" ? weekly.hybrid_mode_reason : "This accepted weekly generation has no requested mode";
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
    const driverMap = data.candidate_driver_map;
    const sourceRows = driverMap?.candidates || data.candidate_appendix.providers;
    const accepted = sourceRows.filter((row) => (tierFilter === "all" || row.tier === tierFilter) && (!query || `${row.candidate_id || row.candidate} ${row.portfolio_role} ${row.category}`.toLowerCase().includes(query)));
    const rows = driverMap
      ? accepted.map((row) => {
        const tactical = row.cluster_scores.find((item) => item.cluster_id === "TACTICAL_TIMING_MULTI_ASSET");
        const metricWindow = row.live_metrics.status.startsWith("AVAILABLE") ? `${formatDate(row.live_metrics.start_date)}–${formatDate(row.live_metrics.end_date)} · ${integer.format(row.live_metrics.observations)} obs` : humanStatus(row.live_metrics.reason);
        const scoreDetail = tactical.downside_correlation_mean == null ? humanStatus(tactical.status) : `rank ${integer.format(tactical.descriptive_rank)} · downside ${decimal(tactical.downside_correlation_mean)} · Pearson ${decimal(tactical.pearson_mean)} · worst-decile ${pct(tactical.worst_decile_candidate_return_minimum)}`;
        const missing = tactical.missing_members.length ? `Missing: ${tactical.missing_members.join(", ")}` : "All cluster members measured";
        const cost = `expense ${pct(row.cost.expense_ratio)} · fixed ${row.cost.annual_fixed_cost == null ? "—" : `$${decimal(row.cost.annual_fixed_cost, 2)}`}`;
        return `<tr><td><span class="cell-primary">${safe(row.candidate_id)}</span><span class="cell-secondary">${safe(row.ticker || row.category)}</span></td><td>${statusText(row.tier)}</td><td>${statusText(tactical.status)}<span class="cell-secondary">${safe(scoreDetail)}</span><span class="cell-secondary">${safe(missing)} · ${safe(tactical.window_start || "—")}–${safe(tactical.window_end || "—")}</span></td><td>${statusText(row.cost.status)}<span class="cell-secondary">${safe(cost)}</span><span class="cell-secondary">All-in cost unavailable without a modeled notional</span></td><td>${statusText(row.live_metrics.status)}<span class="cell-secondary">${safe(metricWindow)}</span></td><td><span class="cell-primary">${safe(row.portfolio_role)}</span><span class="cell-secondary">Allocation / execution authority NONE</span></td></tr>`;
      })
      : accepted.map((row) => `<tr><td><span class="cell-primary">${safe(row.candidate)}</span><span class="cell-secondary">${safe(row.ticker || row.category)}</span></td><td>${statusText(row.tier)}</td><td><span class="cell-primary">${safe(row.portfolio_role)}</span><span class="cell-secondary">${safe(row.philosophy_module)}</span></td><td>${safe(row.execution)}<span class="cell-secondary">${safe(row.cadence)}</span></td><td>${safe(row.principal_gap)}</td></tr>`);
    const table = $("#candidate-table");
    table.dataset.visibleRows = String(accepted.length);
    const headers = driverMap ? [["Candidate"], ["Tier"], ["Tactical-cluster evidence"], ["Cost evidence"], ["Live-vehicle window"], ["Role / boundary"]] : [["Candidate"], ["Frozen tier"], ["Role / module"], ["Implementation"], ["Principal gap"]];
    table.innerHTML = tableMarkup(headers, rows.length ? rows : [emptyRow(headers.length)]);
  }

  function renderCandidates() {
    const appendix = data.candidate_appendix;
    const driverMap = data.candidate_driver_map;
    if (driverMap) {
      const coverage = driverMap.coverage;
      $("#candidate-boundary").textContent = `Frozen evidence through ${driverMap.as_of_date} · driver map ${data.meta?.candidate_driver_map_sha256 ? `${String(data.meta.candidate_driver_map_sha256).slice(0, 14)}…` : "digest not published"} · ${humanStatus(coverage.refresh_status)}. Descriptive ordering only; no capital authority.`;
      renderKpis("#candidate-kpis", [
        ["Research status", humanStatus(driverMap.research_status), "Dated evidence, not current", "warning"],
        ["Candidates", String(coverage.candidate_count), `${integer.format(coverage.measured_candidate_count)} measured · ${integer.format(coverage.unavailable_candidate_count)} unavailable`],
        ["Tactical loss budget", pct(driverMap.driver_clusters.find((row) => row.cluster_id === "TACTICAL_TIMING_MULTI_ASSET").adverse_loss_budget), `Systematic limit ${pct(driverMap.portfolio.systematic_loss_limit)}`, "danger"],
        ["Capital authority", "NONE", "No automatic add, drop, or allocation", "reserve"],
      ]);
      const clusterRows = driverMap.driver_clusters.map((row) => `<tr><th scope="row"><span class="cell-primary">${safe(humanStatus(row.cluster_id))}</span><span class="cell-secondary">${safe(row.members.join(" · "))}</span></th><td>${pct(row.allocation_weight)}</td><td>${pct(row.adverse_loss_budget)}<span class="cell-secondary">${safe(humanStatus(row.budget_status))}</span></td><td>${row.observed_pair_count ? `${decimal(row.observed_pair_correlation_minimum)}–${decimal(row.observed_pair_correlation_maximum)}<span class="cell-secondary">${integer.format(row.observed_pair_count)} measured pairs</span>` : "UNAVAILABLE"}</td></tr>`);
      $("#candidate-driver-table").innerHTML = tableMarkup([["Driver cluster / members"], ["Systematic weight"], ["Adverse-loss budget"], ["Observed pair range"]], clusterRows);
      const leaveRows = driverMap.tactical_leave_one_out.map((row) => `<tr><th scope="row">${safe(row.series_id)}</th><td>${pct(row.correlation_one_loss_reduction)}</td><td>${pct(row.central_case_loss_reduction)}</td><td>${pct(row.cluster_loss_without)}<span class="cell-secondary">${safe(humanStatus(row.cluster_budget_status_without))}</span></td></tr>`);
      $("#candidate-leave-one-out-table").innerHTML = tableMarkup([["Tactical sleeve"], ["Conservative reduction"], ["Central reduction"], ["Cluster loss without"]], leaveRows);
      $("#candidate-driver-grid").hidden = false;
    } else {
      $("#candidate-boundary").textContent = `Frozen ${appendix.as_of_date} audit appendix · source ${appendix.source_sha256.slice(0, 14)}… · no refresh or capital authority.`;
      renderKpis("#candidate-kpis", [
        ["Appendix status", humanStatus(appendix.status), "Dated research evidence", "warning"],
        ["Candidates", String(appendix.providers.length), "Not Strategy identities"],
        ["Tier 1", String(appendix.tier_counts["Tier 1"] || 0), "Diligence priority only"],
        ["Capital authority", "NONE", "No selection or allocation", "reserve"],
      ]);
      $("#candidate-driver-grid").hidden = true;
    }
    renderCandidateTable();
  }

  let factMatrixFilter = "ALL";
  function renderFactScopeTables() {
    const factMatrix = data.fact_coverage_matrix || [];
    const noPathRows = factMatrix.filter((row) => row.status === "NO_ACCEPTED_FACT_PATH");
    const visible = factMatrixFilter === "ALL" ? factMatrix : noPathRows;
    $$('[data-fact-matrix-filter]').forEach((button) => {
      const active = button.dataset.factMatrixFilter === factMatrixFilter;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
      button.onclick = () => {
        factMatrixFilter = button.dataset.factMatrixFilter;
        renderFactScopeTables();
      };
    });
    $("#fact-matrix-summary").innerHTML = `<strong>${integer.format(factMatrix.length)} declared fact paths</strong> · <span class="status-text danger">${integer.format(noPathRows.length)} NO_ACCEPTED_FACT_PATH</span>. No-path means the system has no admitted source for that account/fact boundary; it is not a zero balance, zero activity, or proof the fact does not exist. Showing ${integer.format(visible.length)} row(s).`;
    const intervalText = (row) => {
      if (Array.isArray(row.missing_intervals) && row.missing_intervals.length) return row.missing_intervals.map((item) => typeof item === "object" ? `${item.start || item.missing_interval_start || "?"}–${item.end || item.missing_interval_end || "?"}: ${item.reason || item.missing_interval_reason || "reason not recorded"}` : String(item)).join(" · ");
      if (row.missing_interval_start || row.missing_interval_end) return `${row.missing_interval_start || "?"}–${row.missing_interval_end || "?"}: ${row.missing_interval_reason || "reason not recorded"}`;
      return row.missing_interval_reason || "No missing interval declared";
    };
    const matrixRows = visible.map((row) => `<tr data-fact-path-status="${safe(row.status)}"><th scope="row" data-label="Account / provider"><span class="cell-primary">${safe(row.account_id ? accountLabel(row.account_id) : "Provider-level (no account)")}</span><span class="cell-secondary">${safe(providerName(row.provider_id))}</span>${accountRoleNote(row)}</th><td data-label="Fact / cadence"><span class="cell-primary">${safe(humanStatus(row.fact_type))}</span><span class="cell-secondary">${safe(row.cadence || "Cadence unavailable")}</span></td><td data-label="Path status">${statusText(row.status)}</td><td data-label="Declared boundary">${formatDateHtml(row.declared_start)}–${formatDateHtml(row.declared_end)}<span class="cell-secondary">${safe(row.boundary_basis || "Boundary basis unavailable")}</span></td><td data-label="Observed evidence">${formatDateHtml(row.observed_start)}–${formatDateHtml(row.observed_end)}<span class="cell-secondary">${integer.format(row.record_count || 0)} accepted records · ${integer.format(row.unavailable_amount_count || 0)} unavailable amounts</span></td><td data-label="Named missing interval">${safe(intervalText(row))}</td><td data-label="Authority / disposition">${safe(row.authority || "Authority unavailable")}<span class="cell-secondary">${safe(typeof row.disposition === "object" ? [row.disposition.code, row.disposition.effect].filter(Boolean).join(": ") : row.disposition || "Disposition unavailable")}</span></td></tr>`);
    const matrixTable = $("#fact-coverage-matrix-table");
    matrixTable.dataset.totalRows = String(factMatrix.length);
    matrixTable.dataset.noPathRows = String(noPathRows.length);
    matrixTable.dataset.visibleRows = String(visible.length);
    matrixTable.innerHTML = tableMarkup([["Account / provider"], ["Fact / cadence"], ["Path status"], ["Declared boundary"], ["Observed evidence"], ["Named missing interval"], ["Authority / disposition"]], matrixRows.length ? matrixRows : [emptyRow(7, "No rows match the current fact-path filter")]);

    // A6: a PROVIDER_SOURCE window names its provider and carries its boundary text; it has
    // no fact type, boundary basis or record count, so none is shown as unknown or zero.
    const windowRows = (data.observation_windows || []).map((row) => {
      if (row.window_type === "PROVIDER_SOURCE") {
        return `<tr><th scope="row" data-label="Fact / window"><span class="cell-primary">${safe(providerName(row.provider_id))}</span><span class="cell-secondary">${safe(humanStatus(row.window_type))}</span></th><td data-label="Status">${statusText(row.status)}</td><td data-label="Declared">${formatDateHtml(row.declared_start)}–${formatDateHtml(row.declared_end)}</td><td data-label="Observed">${formatDateHtml(row.observed_start)}–${formatDateHtml(row.observed_end)}</td><td class="numeric" data-label="Records">—</td><td data-label="Boundary basis">${safe(row.boundary || "Boundary text not recorded")}</td></tr>`;
      }
      const accounting = row.fact_type === "positions" ? positionTotalAccounting(row.record_count, row.observed_end) : "";
      const recordNote = row.fact_type === "positions"
        ? `<span class="cell-secondary">all accepted snapshot rows${accounting ? `: ${safe(accounting)}` : ""}</span>`
        : "";
      return `<tr><th scope="row" data-label="Fact / window"><span class="cell-primary">${safe(humanStatus(row.fact_type))}</span><span class="cell-secondary">${safe(humanStatus(row.window_type))} · ${safe(row.cadence || "Cadence unavailable")}</span></th><td data-label="Status">${statusText(row.status)}</td><td data-label="Declared">${formatDateHtml(row.declared_start)}–${formatDateHtml(row.declared_end)}</td><td data-label="Observed">${formatDateHtml(row.observed_start)}–${formatDateHtml(row.observed_end)}</td><td class="numeric" data-label="Records">${integer.format(row.record_count || 0)}${recordNote}</td><td data-label="Boundary basis">${safe(row.boundary_basis || "Boundary basis unavailable")}</td></tr>`;
    });
    $("#observation-windows-table").innerHTML = tableMarkup([["Fact / window"], ["Status"], ["Declared"], ["Observed"], ["Records (count)", "numeric"], ["Boundary basis"]], windowRows.length ? windowRows : [emptyRow(6, "No observation-window contract")]);

    const attributionRows = (data.attribution_dispositions || []).map((row) => `<tr><th scope="row" data-label="Account / provider"><span class="cell-primary">${safe(row.account_id ? accountLabel(row.account_id) : "Provider-level (no account)")}</span><span class="cell-secondary">${safe(providerName(row.provider_id))}</span></th><td data-label="Effective interval">${formatDateHtml(row.effective_start)}–${formatDateHtml(row.effective_end)}</td><td data-label="Period">${safe(row.period || "Period unavailable")}</td><td data-label="Authority">${safe(row.authority || "Authority unavailable")}</td><td data-label="Disposition">${statusText(row.disposition)}</td></tr>`);
    $("#attribution-dispositions-table").innerHTML = tableMarkup([["Account / provider"], ["Effective interval"], ["Period"], ["Authority"], ["Disposition"]], attributionRows.length ? attributionRows : [emptyRow(5, "No attribution dispositions")]);
  }

  function renderLimitations() {
    if (!Array.isArray(data.limitations)) throw new Error("Dashboard limitations are missing or unsupported");
    $("#limitations-list").innerHTML = data.limitations.map((item) => `<li>${safe(item)}</li>`).join("");
  }

  function renderData() {
    const factRows = data.coverage.filter((row) => row.category === "FACT_COVERAGE");
    const cleanFacts = factRows.filter((row) => row.status === "COMPLETE_WITHIN_DECLARED_BOUNDARY").length;
    const dispositionText = (value) => value && typeof value === "object" ? [value.code, value.effect].filter(Boolean).join(": ") : String(value || "Disposition not recorded");
    const observedWindow = (row) => {
      if (row.observed_start || row.observed_end) return `${formatDate(row.observed_start)}–${formatDate(row.observed_end)}`;
      if (row.category === "DATA_FRESHNESS") return `${row.last_accepted_date ? `Last accepted ${formatDate(row.last_accepted_date)}` : "No accepted observation"} · ${row.evaluated_on && row.evaluated_on === data.meta.portfolio_as_of ? `assessed at the ${formatDate(row.evaluated_on)} cutoff` : `evaluated ${formatDate(row.evaluated_on)}`}`;
      return "Boundary not declared / no accepted observation";
    };
    // N62: the positions value is the baseline's accepted snapshot rows; the panel's own counts say how many are current.
    const coverageValueText = (row) => {
      if (row.item === "full-systematic-hwm-unavailable") return currencyExact(row.value);
      if (row.item === "positions" || row.item === "positions-partial") {
        const positionsWindow = (data.observation_windows || []).find((item) => item.fact_type === "positions");
        const accounting = positionTotalAccounting(row.value, positionsWindow?.observed_end);
        if (accounting) return `${row.value} accepted snapshot rows = ${accounting}`;
      }
      return row.value ?? "No accepted value";
    };
    const freshnessPolicy = (row) => {
      if (row.category !== "DATA_FRESHNESS") return "Not a recurring freshness assessment";
      if (row.freshness_mode === "FINITE") return `${row.policy_id} · finite history · reconciled ${formatDate(row.reconciled_on)}`;
      return `${row.policy_id} · ${humanStatus(row.freshness_calendar)} · every ${integer.format(row.expected_interval_days)} day(s) · grace ${integer.format(row.grace_days)} · stale after ${integer.format(row.stale_after_days)} · reconciled ${formatDate(row.reconciled_on)}`;
    };
    renderKpis("#data-kpis", [
      ["Report generation", data.meta.report_generation_id.slice(0, 12), "Accepted local authority"],
      ["Baseline generation", data.meta.baseline_generation_id.slice(0, 12), "Hash-linked input"],
      ["Fact status", `${cleanFacts}/${factRows.length} clean`, `${factRows.length - cleanFacts} carry an explicit retained disposition`, cleanFacts === factRows.length ? "positive" : "warning"],
      ["External connections", "0", "Policy statement: direct-file local operation; not counted from the payload", "reserve"],
    ]);
    const coverageRows = data.coverage.map((row) => `<tr id="coverage-${domToken(row.item)}" data-coverage-item="${safe(row.item)}" tabindex="-1"><th scope="row"><span class="cell-primary">${safe(row.item)}</span><span class="cell-secondary">${safe(row.category)}</span></th><td>${statusText(row.status)}${row.escalation_required ? '<span class="cell-secondary">Escalation required</span>' : ""}</td><td>${safe(observedWindow(row))}</td><td>${safe(freshnessPolicy(row))}</td><td>${safe(row.evidence_authority || "Evidence authority not assigned")}<span class="cell-secondary">${safe(row.resolution_authority || "Resolution authority not assigned")}</span></td><td>${safe(coverageValueText(row))}</td><td>${safe(dispositionText(row.disposition))}</td><td>${safe(row.known_gap ? operatorText(row.known_gap) : "No known gap within the declared boundary")}</td></tr>`);
    $("#coverage-table").innerHTML = tableMarkup([["Fact / category"], ["Status / escalation"], ["Observation window / Freshness evaluated"], ["TTL / reconciled"], ["Evidence / resolution authority"], ["Value"], ["Disposition"], ["Known gap / missing interval"]], coverageRows);
    renderFactScopeTables();
    $("#input-bindings").innerHTML = `<div class="binding-card"><div><strong>Portfolio report</strong><span>generation ${safe(data.meta.report_generation_id.slice(0, 12))}… · sha256 ${safe(data.meta.report_sha256.slice(0, 12))}…</span></div><div class="binding-note">stage input, hash-bound at build</div></div><div class="binding-card"><div><strong>Factor / alpha</strong><span>generation ${safe(data.meta.decomposition_generation_id.slice(0, 12))}… · sha256 ${safe(data.meta.decomposition_sha256.slice(0, 12))}…</span></div><div class="binding-note">stage input, hash-bound at build</div></div><div class="binding-card"><div><strong>Dependence</strong><span>generation ${safe(data.meta.correlation_generation_id.slice(0, 12))}… · sha256 ${safe(data.meta.correlation_sha256.slice(0, 12))}…</span></div><div class="binding-note">stage input, hash-bound at build</div></div><div class="binding-card"><div><strong>Candidate appendix</strong><span>${safe(data.candidate_appendix.as_of_date)} · sha256 ${safe(data.candidate_appendix.source_sha256.slice(0, 12))}…</span></div><div class="binding-note">${statusText(data.candidate_appendix.status)}</div></div><div class="callout callout-neutral">Portfolio, analytics, and appendix generations retain separate authority. Candidate rows never enter current strategy totals. Each input was read from its stage's current pointer and refused unless its generation was verified and complete. This panel was sealed before release and records no release state.</div>`;
    const sourceRows = data.provenance.map((row) => `<tr><td><span class="cell-primary">${safe(row.artifact)}</span><span class="cell-secondary">${safe(row.schema)}</span></td><td>${formatDateHtml(row.as_of_date)}${staleSuffix(row.as_of_date)}</td><td><code>${safe(row.sha256.slice(0, 16))}…</code></td></tr>`);
    $("#source-table").innerHTML = tableMarkup([["Artifact / schema"], ["As of"], ["SHA-256"]], sourceRows);
    renderLimitations();
    // F37: the route is chosen by the item's id.  The five coverage exceptions the report declares
    // each name their own action; an item the table does not name keeps the generic one.
    const routes = {
      "full-systematic-hwm-unavailable": ["risk", "Qualify accepted daily NAV freshness across the governed HWM perimeter"],
      "current-strategy-attribution-partial": ["capital", "Adjudicate pool-only assignment intervals; current mapped positions remain valid"],
      "historical-strategy-attribution-unresolved": ["risk", "Adjudicate historical General intervals before complete account-by-strategy performance"],
      "positions-partial": ["data", "Obtain accepted position snapshots for the providers the coverage row names"],
      "account-nav-coverage-partial": ["data", "Obtain complete daily account NAV for the accounts the coverage row names"],
      "transaction-coverage-partial": ["data", "Obtain the missing transaction records, or formally disposition the gap"],
      "flow-coverage-partial": ["data", "Obtain the missing cash-flow records, or formally disposition the gap"],
    };
    // A blocking item carries no fact type or policy id; its coverage row (same item id) does.
    const coverageByItem = new Map((Array.isArray(data.coverage) ? data.coverage : []).map((row) => [row.item, row]));
    const remediationRows = data.blocking_items.map((item) => {
      const detail = coverageByItem.get(item.item) || item;
      const [view, action] = Object.hasOwn(routes, item.item)
        ? routes[item.item]
        : detail.category === "DATA_FRESHNESS"
          ? ["data", `Refresh or formally disposition the required ${humanStatus(detail.fact_type)} evidence under ${detail.policy_id}; reconciled ${formatDate(detail.reconciled_on)}`]
          : ["data", "Resolve the declared coverage exception in its producing generation"];
      const focus = view === "data" ? ` data-focus-coverage="${safe(item.item)}"` : "";
      return `<tr><td><span class="cell-primary">${safe(humanStatus(item.item))}</span><span class="cell-secondary">${safe(item.known_gap ? operatorText(item.known_gap) : "No further detail recorded")}</span></td><td>${statusText(item.status)}</td><td>${safe(action)}</td><td><button type="button" class="text-action" data-open-view="${safe(view)}"${focus} aria-label="Open ${safe(VIEW_TITLES[view])} evidence for ${safe(humanStatus(item.item))}">Open ${safe(VIEW_TITLES[view])} →</button></td></tr>`;
    });
    $("#remediation-table").innerHTML = tableMarkup([["Exception"], ["State"], ["Required evidence"], ["Destination"]], remediationRows.length ? remediationRows : [emptyRow(4, "No material blocking exceptions")]);
  }

  const viewRenderers = {
    command: renderCommand,
    capital: renderCapital,
    risk: renderRisk,
    signals: renderSignals,
    attribution: renderAttribution,
    dependence: renderDependence,
    weekly: renderWeekly,
    candidates: renderCandidates,
    data: renderData,
  };
  const renderedViews = new Set();
  const dirtyViews = new Set();

  function renderView(name) {
    const renderer = viewRenderers[name];
    if (!renderer || (renderedViews.has(name) && !dirtyViews.has(name))) return;
    renderer();
    // F47: tables built by a render (not only at load) are keyboard-reachable regions too.
    prepareScrollableTableRegions();
    renderedViews.add(name);
    dirtyViews.delete(name);
  }

  function showAppError(error) {
    console.error(error);
    $("#loading-state").hidden = true;
    $("#error-state").hidden = false;
    $("#app").dataset.appState = "error";
  }

  function renderViewSafely(name) {
    try {
      renderView(name);
      return true;
    } catch (error) {
      showAppError(error);
      return false;
    }
  }

  // F47: a view name comes from the URL hash, so it is escaped before it enters a selector.
  const viewSelector = (name) => `[data-view="${CSS.escape(String(name))}"]`;

  function setView(name, updateHash = true) {
    const view = $(viewSelector(name));
    if (!view) return false;
    $$(".view").forEach((item) => { item.hidden = item !== view; item.classList.toggle("is-active", item === view); });
    $$('[data-nav]').forEach((button) => { const active = button.dataset.nav === name; button.classList.toggle("is-active", active); active ? button.setAttribute("aria-current", "page") : button.removeAttribute("aria-current"); });
    $("#page-title").textContent = VIEW_TITLES[name] || "Portfolio control";
    document.title = `V Capital — ${$("#page-title").textContent}`;
    if (updateHash) history.replaceState(null, "", `#${name}`);
    window.scrollTo({ top: 0, behavior: "auto" });
    if (updateHash) $("#main-content").focus({ preventScroll: true });
    return renderViewSafely(name);
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
    ["capital", "risk", "attribution"].forEach((view) => dirtyViews.add(view));
    const activeView = $$(".view").find((view) => view.classList.contains("is-active"));
    if (activeView) renderViewSafely(activeView.dataset.view);
  }

  function showToast(message) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.hidden = false;
    window.clearTimeout(showToast.timeout);
    showToast.timeout = window.setTimeout(() => { toast.hidden = true; }, 7000);
  }

  function bindEvents() {
    $$('[data-nav]').forEach((button) => {
      const view = $(`[data-view="${button.dataset.nav}"]`);
      if (view) {
        view.id ||= `view-${button.dataset.nav}`;
        button.setAttribute("aria-controls", view.id);
      }
      button.addEventListener("click", () => setView(button.dataset.nav));
    });
    $$('[data-open-view]').forEach((button) => {
      const view = $(`[data-view="${button.dataset.openView}"]`);
      if (!view) return;
      view.id ||= `view-${button.dataset.openView}`;
      button.setAttribute("aria-controls", view.id);
    });
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
    $("#leverage-page-previous").addEventListener("click", () => { leveragePage = Math.max(0, leveragePage - 1); renderLeverageDisclosures(); });
    $("#leverage-page-next").addEventListener("click", () => { leveragePage += 1; renderLeverageDisclosures(); });
    $("#context-account").addEventListener("change", (event) => { selectedContextAccount = event.target.value; applyContext(); });
    $("#context-strategy").addEventListener("change", (event) => { selectedContextStrategy = event.target.value; applyContext(); });
    $("#context-reset").addEventListener("click", () => { selectedContextAccount = "ALL"; selectedContextStrategy = "ALL"; applyContext(); });
    $("#snapshot-button").addEventListener("click", () => showToast(`Portfolio facts ${formatDate(data.meta.portfolio_as_of)} · freshness assessed ${formatDate(data.meta.freshness_evaluated_on)} (not a current-day claim) · Report ${data.meta.report_generation_id.slice(0, 12)}… · read-only local artifact.`));
    $$('.segmented, .strategy-selector').forEach((group) => group.setAttribute("role", "group"));
    $$('.segment').forEach((button) => button.setAttribute("aria-pressed", String(button.classList.contains("is-active"))));
    window.addEventListener("hashchange", () => {
      try {
        setView(location.hash.slice(1) || "command", false);
      } catch (error) {
        showAppError(error);
      }
    });
  }

  function prepareScrollableTableRegions() {
    $$(".table-wrap, .matrix-shell, .weekly-table-shell").forEach((region) => {
      const caption = $("caption", region)?.textContent?.trim();
      region.tabIndex = 0;
      region.setAttribute("role", "region");
      region.setAttribute("aria-label", caption ? `${caption} scrollable table` : "Scrollable table");
    });
  }

  function bindEmbeddedDownloads() {
    $$('a[data-download-payload-prefix]').forEach((link) => link.addEventListener("click", async (event) => {
      event.preventDefault();
      const filename = String(link.getAttribute("data-download-filename") || "");
      const metadata = EMBEDDED_DOWNLOAD_TYPES[filename];
      const mimeType = metadata?.mimeType;
      const payloadPrefix = String(link.getAttribute("data-download-payload-prefix") || "");
      const chunkCount = Number(link.getAttribute("data-download-chunk-count"));
      const rawBytes = Number(link.getAttribute("data-download-raw-bytes"));
      if (!mimeType
          || payloadPrefix !== metadata.payloadPrefix
          || !Number.isInteger(chunkCount)
          || chunkCount < 1
          || chunkCount > EMBEDDED_DOWNLOAD_MAX_CHUNKS
          || link.getAttribute("data-download-mime") !== mimeType
          || link.getAttribute("data-download-encoding") !== metadata.encoding
          || !Number.isInteger(rawBytes)
          || rawBytes < 1
          || link.getAttribute("href") !== `downloads/${filename}`
          || link.getAttribute("download") !== filename) {
        console.error("Embedded download metadata is invalid.");
        showToast("The download could not start: its embedded metadata is invalid.");
        return;
      }
      try {
        const parts = [];
        for (let index = 0; index < chunkCount; index += 1) {
          const payloadElement = document.getElementById(`${payloadPrefix}-${String(index).padStart(5, "0")}`);
          const chunk = String(payloadElement?.textContent || "");
          const validChunk = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(chunk);
          if (!(payloadElement instanceof HTMLScriptElement)
              || payloadElement.type !== "application/octet-stream"
              || payloadElement.getAttribute("data-download-filename") !== filename
              || payloadElement.getAttribute("data-download-encoding") !== metadata.encoding
              || payloadElement.getAttribute("data-download-chunk-index") !== String(index)
              || !chunk
              || chunk.length > EMBEDDED_DOWNLOAD_CHUNK_SIZE
              || (index < chunkCount - 1 && chunk.length !== EMBEDDED_DOWNLOAD_CHUNK_SIZE)
              || !validChunk) {
            throw new Error("Embedded download chunk is invalid.");
          }
          // Each chunk is a multiple of 4 base64 characters, so it decodes on
          // its own; only one chunk's binary string is alive at a time.
          const binary = window.atob(chunk);
          const bytes = new Uint8Array(binary.length);
          for (let offset = 0; offset < binary.length; offset += 1) bytes[offset] = binary.charCodeAt(offset);
          parts.push(bytes);
        }
        let blob = new Blob(parts, { type: mimeType });
        if (metadata.encoding === "gzip") {
          if (typeof DecompressionStream !== "function") throw new Error("This browser cannot decompress the embedded download.");
          blob = await new Response(blob.stream().pipeThrough(new DecompressionStream("gzip"))).blob();
          blob = blob.slice(0, blob.size, mimeType);
        }
        if (blob.size !== rawBytes) throw new Error("Embedded download length differs from its declared size.");
        const objectUrl = URL.createObjectURL(blob);
        const trigger = document.createElement("a");
        trigger.href = objectUrl;
        trigger.download = filename;
        trigger.hidden = true;
        document.body.appendChild(trigger);
        try {
          trigger.click();
        } finally {
          trigger.remove();
          window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
        }
      } catch (error) {
        console.error("Embedded download payload could not be decoded.", error);
        showToast("The download failed: the embedded report data could not be decoded in this browser.");
      }
    }));
  }

  function init() {
    try {
      if (!data || ![21, 22, 23, 24, 25].includes(data.panel_schema_version)) throw new Error("Embedded dashboard data is missing or unsupported");
      renderLimitations();
      $("#portfolio-as-of").textContent = formatDate(data.meta.portfolio_as_of);
      // Schema-4 freshness is judged at the catalog cutoff (N46/N54); the header says so.
      const freshnessAtCutoff = data.meta.freshness_evaluated_on === data.meta.portfolio_as_of;
      $("#freshness-as-of").textContent = freshnessAtCutoff
        ? `freshness assessed at cutoff ${formatDate(data.meta.freshness_evaluated_on)}; not a current-day freshness claim`
        : `freshness assessed ${formatDate(data.meta.freshness_evaluated_on)} (cutoff ${formatDate(data.meta.portfolio_as_of)}); not a current-day freshness claim`;
      $("#coverage-status").textContent = `${pct(data.advisory.nav_coverage)} NAV`; // header label: HWM-perimeter NAV coverage
      populateContextControls();
      bindEvents(); bindEmbeddedDownloads();
      const requested = location.hash.slice(1);
      if (!setView($(viewSelector(requested)) ? requested : "command", false)) return;
      prepareScrollableTableRegions();
      $("#loading-state").hidden = true;
      $("#app").dataset.appState = "ready";
    } catch (error) {
      showAppError(error);
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
