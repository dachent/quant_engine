"""Source-derived baseline projection for the synthetic MVP-2 vertical."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, localcontext
import hashlib
import json
from typing import Any, Mapping


EXCLUDED_STRATEGIES = {
    "composer-holy-grail": "Holy Grail [Composer]",
    "echotrade-growth-pivots": "Growth at Market Pivot Points [EchoTrade]",
}


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _amount(value: str) -> dict[str, str]:
    return {"currency": "USD", "status": "AVAILABLE", "value": value}


def project_coverage_metadata(
    baseline_doc: dict[str, Any], ledger_doc: dict[str, Any]
) -> None:
    """Derive presentation coverage with the existing baseline producers."""
    import portfolio_baseline as baseline

    as_of = date.fromisoformat(baseline_doc["as_of_date"])
    baseline_doc["provider_coverage"] = [
        {
            **row,
            "qualification": "SYNTHETIC_ACCOUNT_LIVE",
            "coverage": {
                **row["coverage"],
                "known_gaps": [],
                "out_of_boundary": ["REAL_PROVIDER_DATA"],
            },
        }
        for row in ledger_doc["provider_coverage"]
    ]
    claims, exceptions = baseline._fact_coverage(
        baseline_doc["accounts"],
        baseline_doc["assignments"],
        baseline_doc["transactions"],
        baseline_doc["flows"],
        baseline_doc["positions"],
        ["SYNTHETIC_NAV_DERIVED_POSITIONS_NOT_SECURITY_HOLDINGS"],
        sum(
            row["status"] == "UNAVAILABLE"
            for row in baseline_doc["strategy_nav_coverage"]
            if row["strategy_id"] not in EXCLUDED_STRATEGIES
        ),
        as_of,
        nav=baseline_doc["nav_observations"],
        costs=baseline_doc["costs"],
    )
    matrix, windows, dispositions = baseline._coverage_evidence(
        baseline_doc["accounts"],
        baseline_doc["assignments"],
        baseline_doc["provider_admissions"],
        baseline_doc["provider_coverage"],
        baseline_doc["nav_observations"],
        baseline_doc["positions"],
        baseline_doc["transactions"],
        baseline_doc["flows"],
        as_of,
    )
    baseline_doc.update(
        fact_coverage=claims,
        fact_coverage_matrix=matrix,
        observation_windows=windows,
        attribution_dispositions=dispositions,
        reconciliation_exceptions=exceptions,
    )


def _common_daily_suffix(
    returns: list[dict[str, Any]], strategy_ids: list[str]
) -> list[str]:
    dates_by_strategy = {
        strategy_id: {
            str(row["date"]) for row in returns if row.get("strategy_id") == strategy_id
        }
        for strategy_id in strategy_ids
    }
    common = sorted(set.intersection(*(dates_by_strategy.values())))
    if not common:
        raise AssertionError("synthetic MVP-2 returns lack a shared daily interval")
    suffix = [common[-1]]
    cursor = date.fromisoformat(common[-1])
    for observed in reversed(common[:-1]):
        candidate = date.fromisoformat(observed)
        if candidate != cursor - timedelta(days=1):
            break
        suffix.append(observed)
        cursor = candidate
    return list(reversed(suffix))


def _source_artifact(
    artifact_id: str, schema_name: str, as_of_date: str, payload: object
) -> dict[str, Any]:
    content = _canonical(payload)
    return {
        "artifact_id": artifact_id,
        "as_of_date": as_of_date,
        "schema_name": schema_name,
        "schema_version": "1",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "source_key": artifact_id,
    }


def _evidence_rows(
    reporter: Any,
    ledger: Mapping[str, Any],
    strategy_ids: list[str],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    catalog = {"strategy_returns": ledger["strategy_returns"]}
    canonical = reporter._account_live_return_series(catalog)
    prior = {
        str(row["strategy_id"]): dict(row)
        for row in ledger.get("strategy_evidence", [])
        if row.get("strategy_id") in strategy_ids
    }
    navs = list(ledger["nav_observations"])
    provider_generations = {
        row["provider_id"]: row for row in ledger["provider_generations"]
    }
    rows: list[dict[str, Any]] = []
    for strategy_id in strategy_ids:
        metrics = canonical[strategy_id]
        strategy_navs = [row for row in navs if row["strategy_id"] == strategy_id]
        account_scopes = []
        for account_id in sorted({str(row["account_id"]) for row in strategy_navs}):
            account_navs = [
                row for row in strategy_navs if row["account_id"] == account_id
            ]
            account_scopes.append(
                {
                    "account_id": account_id,
                    "observed_start": min(
                        str(row["as_of_date"]) for row in account_navs
                    ),
                    "observed_end": max(str(row["as_of_date"]) for row in account_navs),
                    "nav_observations": len(account_navs),
                    "synthetic_fixture": True,
                }
            )
        provider_id = str(metrics["provider_ids"][0])
        existing = prior.get(strategy_id, {})
        provider = existing.get("provider")
        if provider is not None and set(provider) == {"entity_id", "provider_id"}:
            # The P123 twin identifier selects history; it is not an observed
            # provider return path. The actual account observations remain here.
            provider = None
        account = (
            dict(existing["account"])
            if isinstance(existing.get("account"), Mapping)
            else {}
        )
        rows.append(
            {
                **existing,
                "strategy_id": strategy_id,
                "strategy_name": next(
                    str(row["strategy_name"])
                    for row in ledger["strategy_returns"]
                    if row["strategy_id"] == strategy_id
                ),
                "evidence_layer": "ACCOUNT_LIVE",
                "provider_id": provider_id,
                "generation_id": provider_generations[provider_id]["generation_id"],
                "generation_as_of_date": provider_generations[provider_id][
                    "generation_as_of_date"
                ],
                "provider": provider,
                "overlap": existing.get("overlap") if provider is not None else None,
                "account": {
                    **account,
                    **metrics,
                    "provider_id": provider_id,
                    "account_scopes": account_scopes,
                    "source_class": "SYNTHETIC_DAILY_ACCOUNT_NAV",
                },
            }
        )
    return rows, canonical


def _epoch_key(
    row: Mapping[str, Any],
) -> tuple[str, str, str, str | None]:
    return (
        str(row["account_id"]),
        str(row["strategy_id"]),
        str(row["effective_start"]),
        row["effective_end"],
    )


def _account_observations(
    baseline_doc: dict[str, Any],
) -> dict[str, dict[date, Decimal]]:
    observations: dict[str, dict[date, Decimal]] = {}
    for row in baseline_doc["nav_observations"]:
        account = observations.setdefault(str(row["account_id"]), {})
        account[date.fromisoformat(str(row["as_of_date"]))] = Decimal(
            str(row["nav"]["value"])
        )
    return observations


def _window(
    series: Mapping[date, Decimal], start: date, end: date | None
) -> dict[date, Decimal]:
    return {
        day: value
        for day, value in series.items()
        if start <= day and (end is None or day <= end)
    }


def _epoch_details(
    payloads: Mapping[tuple[str, str, str, str | None], dict[str, Any]],
) -> dict[tuple[str, str, str, str | None], dict[str, Any]]:
    """Wrap recomputed payloads in the manifest epoch-detail shape the report consumes."""

    return {
        key: {
            "account_label": None,
            "last_four": None,
            "epoch_admission_status": "FUNDED",
            "epoch_output_status": "OK",
            "epoch_blocking_reason": None,
            "performance_payload": payload,
        }
        for key, payload in payloads.items()
    }


def _engine_epoch_payloads(
    baseline_doc: dict[str, Any],
) -> tuple[
    dict[tuple[str, str, str, str | None], dict[str, Any]],
    set[tuple[str, str, str, str | None]],
]:
    """Recompute each required epoch's expected report payload from the engine.

    Conventions R1-R4 (state/briefs/acceptance-metric-spec.md) as implemented
    by ``quant_engine.portfolio_ledger.returns``: the R1 opening point is the
    last positive observation before the funded start, otherwise the first
    positive observation on or after it; exact-timestamp legs enter at the
    beginning of the sub-period ending at their NAV recognition date and
    date-only legs at its end; each account epoch carries its OWN linked path
    (a strategy-level composite is never substituted), and gap spans enter only
    where epoch HPR receipts bind -- this synthetic source binds none.  The
    synthetic source also binds no strategy-mapped flow legs, so each path is
    the pure chain-linked account NAV series inside the assignment window,
    linked in the manifest's 40-digit epoch context.  Returns the expected
    payloads plus the keys whose R1 opening is a pre-start observation (the
    P1-era window-first opening differs exactly there).
    """

    from quant_engine.portfolio_ledger import decimal_text
    from quant_engine.portfolio_ledger.returns import (
        AVAILABLE,
        link_epoch,
        opening_point,
    )

    if any(row.get("strategy_id") is not None for row in baseline_doc["flows"]):
        raise AssertionError(
            "synthetic MVP-2 oracle expects no strategy-mapped flow legs"
        )
    observations = _account_observations(baseline_doc)
    payloads: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
    pre_start_keys: set[tuple[str, str, str, str | None]] = set()
    for assignment in baseline_doc["assignments"]:
        strategy_id = assignment["strategy_id"]
        if strategy_id in EXCLUDED_STRATEGIES:
            continue
        start = date.fromisoformat(assignment["effective_start"])
        end = (
            None
            if assignment["effective_end"] is None
            else date.fromisoformat(assignment["effective_end"])
        )
        series = observations[str(assignment["account_id"])]
        window = _window(series, start, end)
        opening = opening_point(series, start)
        if opening is None:
            raise AssertionError(
                f"synthetic epoch {assignment['assignment_id']} has no R1 opening"
            )
        linked_observations = dict(window)
        linked_observations[opening.as_of] = opening.value
        with localcontext() as context:
            context.prec = 40
            linked = link_epoch(linked_observations, [], opening)
        if linked.status != AVAILABLE or linked.twr is None:
            raise AssertionError(
                f"synthetic epoch {assignment['assignment_id']} does not link: "
                f"{linked.reason}"
            )
        key = _epoch_key(assignment)
        payloads[key] = {
            "twr": decimal_text(linked.twr),
            "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
            "flow_count": 0,
            "flow_timing": None,
            "nav_observations": len(window),
            "observed_start": min(window).isoformat(),
            "observed_end": max(window).isoformat(),
        }
        if opening.basis == "LAST_OBSERVATION_BEFORE_START":
            pre_start_keys.add(key)
    return payloads, pre_start_keys


def _p1_era_epoch_payloads(
    baseline_doc: dict[str, Any],
) -> dict[tuple[str, str, str, str | None], dict[str, Any]]:
    """Recompute the retired P1-era oracle values this fixture once encoded.

    The retired report loop opened at the first NAV inside the assignment
    window (never a pre-start observation) and booked strategy flows as
    end-of-day subtractions; the synthetic source binds no strategy-mapped
    flows, so the window-first opening is the only semantic difference.
    """

    from quant_engine.portfolio_ledger import decimal_text

    observations = _account_observations(baseline_doc)
    payloads: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
    for assignment in baseline_doc["assignments"]:
        strategy_id = assignment["strategy_id"]
        if strategy_id in EXCLUDED_STRATEGIES:
            continue
        start = date.fromisoformat(assignment["effective_start"])
        end = (
            None
            if assignment["effective_end"] is None
            else date.fromisoformat(assignment["effective_end"])
        )
        window = _window(observations[str(assignment["account_id"])], start, end)
        days = sorted(window)
        if len(days) < 2:
            raise AssertionError(
                f"synthetic epoch {assignment['assignment_id']} lacks P1-era NAV pairs"
            )
        with localcontext() as context:
            context.prec = 34
            product = Decimal(1)
            for prior_day, day in zip(days, days[1:]):
                product *= window[day] / window[prior_day]
        payloads[_epoch_key(assignment)] = {
            "twr": decimal_text(product - Decimal(1)),
            "twr_status": "AVAILABLE_WITH_INFERRED_FLOW_TIMING",
            "flow_count": 0,
            "flow_timing": None,
            "nav_observations": len(days),
            "observed_start": days[0].isoformat(),
            "observed_end": days[-1].isoformat(),
        }
    return payloads


def mutate_baseline(baseline_doc: dict[str, Any], ledger_doc: dict[str, Any]) -> None:
    """Enrich a 13-epoch synthetic baseline from its projected provider ledger.

    Financial values come from the production broker-return projection already
    present in ``ledger_doc``.  This adapter adds only the catalog surfaces used
    by report, current-dependence, and the legacy HWM envelope consumer.
    """

    import portfolio_report as reporter

    accounts_before = [dict(row) for row in baseline_doc["accounts"]]
    assignments_before = [dict(row) for row in baseline_doc["assignments"]]
    navs = [dict(row) for row in ledger_doc["nav_observations"]]
    returns = [dict(row) for row in ledger_doc["strategy_returns"]]
    required_ids = sorted({str(row["strategy_id"]) for row in returns})
    if len(required_ids) != 7:
        raise AssertionError("synthetic MVP-2 source must project seven strategies")

    for assignment in baseline_doc["assignments"]:
        assignment.update(
            mapping_status="MAPPED",
            observed_label=f"SYNTHETIC:{assignment['assignment_id']}",
            pool_id="systematic-capital",
            pool_mapping_status="MAPPED",
            source_classification="SYNTHETIC_MVP2_FIXTURE",
            source_id="synthetic-provider-ledger",
        )
    if [
        {key: row[key] for key in original}
        for row, original in zip(baseline_doc["assignments"], assignments_before)
    ] != assignments_before:
        raise AssertionError("baseline assignment authority changed")
    if baseline_doc["accounts"] != accounts_before:
        raise AssertionError("baseline account authority changed")

    evidence, canonical = _evidence_rows(reporter, ledger_doc, required_ids)
    latest_by_account: dict[str, dict[str, Any]] = {}
    for row in navs:
        account_id = str(row["account_id"])
        if (
            account_id not in latest_by_account
            or row["as_of_date"] > latest_by_account[account_id]["as_of_date"]
        ):
            latest_by_account[account_id] = row

    shared_days = _common_daily_suffix(returns, required_ids)
    policy_exclusions = reporter.hwm_policy_exclusions(
        reporter._load_json(reporter.HWM_POLICY, "HWM policy")
    )
    if policy_exclusions:
        latest_exclusion = max(policy_exclusions.values()).isoformat()
        shared_days = [
            observed for observed in shared_days if observed >= latest_exclusion
        ]
    if not shared_days or shared_days[-1] != baseline_doc["as_of_date"]:
        raise AssertionError(
            "synthetic MVP-2 returns lack a complete current HWM interval"
        )
    returns_by_key = {
        (str(row["strategy_id"]), str(row["date"])): row
        for row in returns
        if row["strategy_id"] in required_ids and row["date"] in shared_days
    }
    endpoint_day = shared_days[-1]
    endpoint_values = {}
    for strategy_id in required_ids:
        endpoint_return = returns_by_key[(strategy_id, endpoint_day)]
        endpoint_values[strategy_id] = Decimal(
            str(endpoint_return["beginning_capital"])
        ) * (Decimal(1) + Decimal(str(endpoint_return["return"])))
    total = sum(endpoint_values.values(), Decimal(0))
    if total <= 0:
        raise AssertionError("synthetic MVP-2 endpoint capital must be positive")
    weights: dict[str, Decimal] = {}
    with localcontext() as context:
        context.prec = 34
        for strategy_id in required_ids[:-1]:
            weights[strategy_id] = endpoint_values[strategy_id] / total
    decimal_places = max(
        max(0, -weight.as_tuple().exponent) for weight in weights.values()
    )
    with localcontext() as context:
        context.prec = decimal_places + len(str(len(weights))) + 1
        assigned = sum(weights.values(), Decimal(0))
        weights[required_ids[-1]] = Decimal(1) - assigned
        if sum(weights.values(), Decimal(0)) != Decimal(1):
            raise AssertionError("synthetic MVP-2 weights must close exactly")

    allocations = [
        {
            "allocation_id": f"synthetic-allocation:{strategy_id}",
            "as_of_date": endpoint_day,
            "current_value": _amount(format(endpoint_values[strategy_id], "f")),
            "publication_date": baseline_doc["as_of_date"],
            "scope": "SYSTEMATIC",
            "source_effective_date": endpoint_day,
            "source_key": "SYNTHETIC_PROJECTED_ACCOUNT_NAV",
            "strategy_id": strategy_id,
            "systematic_weight": format(weights[strategy_id], "f"),
            "valuation_date": endpoint_day,
        }
        for strategy_id in required_ids
    ]
    allocations.extend(
        {
            "allocation_id": f"synthetic-excluded-allocation:{strategy_id}",
            "as_of_date": endpoint_day,
            "current_value": _amount("0"),
            "publication_date": baseline_doc["as_of_date"],
            "scope": "SYSTEMATIC",
            "source_effective_date": endpoint_day,
            "source_key": "SYNTHETIC_EXCLUDED_PRODUCT_HISTORY",
            "strategy_id": strategy_id,
            "systematic_weight": "0",
            "valuation_date": endpoint_day,
        }
        for strategy_id in sorted(EXCLUDED_STRATEGIES)
    )
    allocations.append(
        {
            "allocation_id": "synthetic-reserve-allocation",
            "as_of_date": endpoint_day,
            "account_id": None,
            "current_value": _amount("0"),
            "publication_date": baseline_doc["as_of_date"],
            "scope": "RESERVE",
            "source_effective_date": endpoint_day,
            "source_key": "SYNTHETIC_ZERO_RESERVE",
            "strategy_id": None,
            "systematic_weight": None,
            "valuation_date": endpoint_day,
        }
    )

    anchor_payload = ledger_doc["capital_anchors"]
    calendar_payload = [
        {
            "account_id": row["account_id"],
            "provider": row["provider_id"],
            "date": row["as_of_date"],
            "status": "OBSERVED",
        }
        for row in navs
    ]
    anchor_artifact = _source_artifact(
        "synthetic/hwm-anchor-acceptance.json",
        "hwm-anchor-acceptance",
        baseline_doc["as_of_date"],
        anchor_payload,
    )
    calendar_artifact = _source_artifact(
        "synthetic/hwm-calendar-account.json",
        "hwm-calendar-authority-account",
        baseline_doc["as_of_date"],
        calendar_payload,
    )
    periods = [
        {
            "strategy_id": strategy_id,
            "date": observed,
            "lane": "ACCOUNT",
            "disposition": "AVAILABLE",
            "beginning_capital_usd": str(
                returns_by_key[(strategy_id, observed)]["beginning_capital"]
            ),
            "return_source_record_id": str(
                returns_by_key[(strategy_id, observed)]["account_reconciliation_sha256"]
            ),
            "component_account_ids": list(
                returns_by_key[(strategy_id, observed)]["component_account_ids"]
            ),
            "flow_status": "OBSERVED",
            "fx_status": "NOT_REQUIRED",
            "flow_source_record_ids": [],
            "fx_source_record_ids": [],
        }
        for observed in shared_days
        for strategy_id in required_ids
    ]
    periods.extend(
        {
            "strategy_id": strategy_id,
            "date": observed,
            "lane": "ACCOUNT",
            "disposition": "EXCLUDED_BY_POLICY",
            "beginning_capital_usd": None,
            "return_source_record_id": None,
            "flow_status": "MISSING",
            "fx_status": "MISSING",
        }
        for observed in shared_days
        for strategy_id in sorted(EXCLUDED_STRATEGIES)
    )
    hwm_anchor = {
        "anchor_date": shared_days[0],
        "index_base_value": "1.0",
        "hwm_policy_id": "PORT-HWM-3",
        "acceptance_provenance": {
            "accepted_at": f"{baseline_doc['as_of_date']}T00:00:00Z",
            "accepted_by": "SYNTHETIC_MVP2_FIXTURE",
            "source_artifact_id": anchor_artifact["artifact_id"],
            "source_artifact_sha256": anchor_artifact["sha256"],
        },
    }

    provider_artifacts = [
        {
            "artifact_id": f"synthetic/provider/{row['provider_id']}/generation.json",
            "as_of_date": row["generation_as_of_date"],
            "schema_name": "synthetic-provider-generation",
            "schema_version": "1",
            "sha256": row["manifest_sha256"],
            "source_key": f"synthetic-provider-{row['provider_id']}",
        }
        for row in ledger_doc.get("provider_generations", [])
    ]
    capital_anchor_artifacts = [
        _source_artifact(
            f"synthetic/capital-anchor/{row['record_id']}.json",
            "synthetic-capital-anchor",
            baseline_doc["as_of_date"],
            row,
        )
        for row in ledger_doc["capital_anchors"]
    ]
    strategies = [
        {
            "strategy_id": strategy_id,
            "name": next(
                str(row["strategy_name"])
                for row in returns
                if row["strategy_id"] == strategy_id
            ),
            "economic_family": "SYNTHETIC_MVP2_SOURCE_DERIVED",
        }
        for strategy_id in required_ids
    ] + [
        {
            "strategy_id": strategy_id,
            "name": name,
            "economic_family": "SYNTHETIC_EXCLUDED_PRODUCT_HISTORY",
        }
        for strategy_id, name in sorted(EXCLUDED_STRATEGIES.items())
    ]
    positions = [
        {
            "record_id": f"synthetic-total-nav-position:{account_id}",
            "account_id": account_id,
            "as_of_date": row["as_of_date"],
            "evidence_class": "SYNTHETIC_NAV_DERIVED_POSITION",
            "mapping_status": "MAPPED",
            "market_value": dict(row["nav"]),
            "quantity": "1",
            "security_id": f"synthetic-total-nav:{account_id}",
            "strategy_id": row["strategy_id"],
            "symbol": "SYNTHETIC_TOTAL_NAV",
        }
        for account_id, row in sorted(latest_by_account.items())
    ]

    baseline_doc.update(
        {
            "policy_version": "PORT-PERF-1",
            "provenance": {
                "generation_id": hashlib.sha256(
                    _canonical(ledger_doc.get("provider_generations", []))
                ).hexdigest(),
                "catalog_sha256": hashlib.sha256(
                    _canonical(
                        {
                            "accounts": accounts_before,
                            "assignments": assignments_before,
                            "ledger_as_of_date": ledger_doc["as_of_date"],
                        }
                    )
                ).hexdigest(),
            },
            "source_artifacts": [
                *provider_artifacts,
                *capital_anchor_artifacts,
                anchor_artifact,
                calendar_artifact,
            ],
            "capital_pools": [
                {
                    "base_currency": "USD",
                    "name": "Synthetic systematic capital",
                    "pool_id": "systematic-capital",
                    "role": "SYSTEMATIC",
                },
                {
                    "base_currency": "USD",
                    "name": "Synthetic zero reserve",
                    "pool_id": "reserve-capital",
                    "role": "RESERVE",
                },
            ],
            "strategies": strategies,
            "allocations": allocations,
            "securities": [
                {
                    "security_id": row["security_id"],
                    "symbol": row["symbol"],
                    "name": "Synthetic account total NAV",
                    "security_type": "SYNTHETIC_NAV_PROXY",
                }
                for row in positions
            ],
            "positions": positions,
            "provider_admissions": [dict(row) for row in ledger_doc["admissions"]],
            "provider_strategy_returns": [],
            "transactions": [],
            "flows": [dict(row) for row in ledger_doc["flows"]],
            "nav_observations": navs,
            "calendar_coverage": calendar_payload,
            "strategy_nav_coverage": [
                {
                    "strategy_id": strategy_id,
                    "status": "AVAILABLE",
                    "daily_start": canonical[strategy_id]["observed_start"],
                    "daily_end": canonical[strategy_id]["observed_end"],
                    "return_observations": canonical[strategy_id][
                        "return_observations"
                    ],
                    "current_value_usd": format(endpoint_values[strategy_id], "f"),
                    "known_gap": None,
                }
                for strategy_id in required_ids
            ]
            + [
                {
                    "strategy_id": strategy_id,
                    "status": "UNAVAILABLE",
                    "daily_start": None,
                    "daily_end": None,
                    "return_observations": None,
                    "current_value_usd": "0",
                    "known_gap": "SYNTHETIC_EXCLUDED_PRODUCT_HISTORY",
                }
                for strategy_id in sorted(EXCLUDED_STRATEGIES)
            ],
            "costs": [
                {
                    "strategy_id": strategy_id,
                    "annual_external_cost_usd": None,
                    "cost_status": "UNAVAILABLE_SYNTHETIC_FIXTURE",
                }
                for strategy_id in sorted({*required_ids, *EXCLUDED_STRATEGIES.keys()})
            ],
            "evidence_links": [
                {
                    "evidence_layer": "ACCOUNT_LIVE",
                    "purpose": "SYNTHETIC_MVP2_VERTICAL",
                    "source_id": f"synthetic-account-nav:{strategy_id}",
                    "strategy_id": strategy_id,
                }
                for strategy_id in required_ids
            ]
            + [
                {
                    "evidence_layer": "ACCOUNT_LIVE",
                    "purpose": "SYNTHETIC_EXCLUDED_PRODUCT_HISTORY",
                    "source_id": f"synthetic-excluded:{strategy_id}",
                    "strategy_id": strategy_id,
                }
                for strategy_id in sorted(EXCLUDED_STRATEGIES)
            ],
            "evidence_layer_metrics": [],
            "strategy_live_metrics": [
                {
                    "strategy_id": strategy_id,
                    "metrics": dict(canonical[strategy_id]),
                    "source_class": "SYNTHETIC_DAILY_ACCOUNT_NAV",
                }
                for strategy_id in required_ids
            ],
            "strategy_provider_evidence": evidence,
            "strategy_returns": returns,
            "implementation_gaps": [
                {
                    "strategy_id": strategy_id,
                    "status": (
                        "AVAILABLE_SYNTHETIC_VERTICAL"
                        if strategy_id in required_ids
                        else "EXCLUDED_SYNTHETIC_HISTORY"
                    ),
                }
                for strategy_id in sorted({*required_ids, *EXCLUDED_STRATEGIES.keys()})
            ],
            "corrections": [],
            "assumptions": [
                {
                    "assumption_id": "synthetic-mvp2-fixture-only",
                    "status": "SYNTHETIC_NOT_PRODUCTION_AUTHORITY",
                }
            ],
            "unmapped_records": [],
            "reconciliation_checks": [
                {
                    "check_id": "synthetic-hwm-allocation-endpoint",
                    "status": "PASS",
                    "value_usd": format(total, "f"),
                },
                {
                    "check_id": "synthetic-systematic-total",
                    "status": "PASS",
                    "value_usd": format(total, "f"),
                },
                {
                    "check_id": "synthetic-reserve-total",
                    "status": "PASS",
                    "value_usd": "0",
                },
            ],
            "replay": {
                "status": "PASS",
                "total_liquid_usd": format(total, "f"),
                "systematic_total_usd": format(total, "f"),
                "reserve_total_usd": "0",
                "nav_coverage": "1",
                "correlation_one_stress": "-1",
                "full_systematic_hwm_status": "AVAILABLE",
                "portfolio_action": "SYNTHETIC_FIXTURE_ONLY",
            },
            "hwm_inputs": {
                "contract_version": 1,
                "anchor": hwm_anchor,
                "calendar_authorities": [
                    {
                        "lane": "ACCOUNT",
                        "authority_id": "SYNTHETIC_DAILY_CALENDAR",
                        "authority_version": "1",
                        "calendar_kind": "ALWAYS_ON",
                        "admitted_source_artifact_id": calendar_artifact["artifact_id"],
                    }
                ],
                "periods": sorted(
                    periods, key=lambda row: (row["date"], row["strategy_id"])
                ),
                "reconciliation": {
                    "period_capital_method": "NAV_ROLLFORWARD",
                    "allocation_endpoint_check": {
                        "matched": True,
                        "delta_usd": "0",
                    },
                },
            },
            "hwm_acceptance_record": {
                "anchor_date": hwm_anchor["anchor_date"],
                "index_base_value": hwm_anchor["index_base_value"],
                "hwm_policy_id": hwm_anchor["hwm_policy_id"],
            },
        }
    )

    project_coverage_metadata(baseline_doc, ledger_doc)
    expected_payloads, pre_start_keys = _engine_epoch_payloads(baseline_doc)
    performance = reporter._account_strategy_performance(
        baseline_doc, current_epoch_details=_epoch_details(expected_payloads)
    )
    required_performance = [
        row for row in performance if row["strategy_id"] in required_ids
    ]
    if (
        len(performance) != len(baseline_doc["assignments"])
        or len(required_performance) != 13
        or any(
            not str(row["twr_status"]).startswith("AVAILABLE")
            for row in required_performance
        )
    ):
        raise AssertionError("synthetic baseline does not reach report performance")
    for row in required_performance:
        expected = expected_payloads[_epoch_key(row)]
        for field, value in expected.items():
            if row[field] != value:
                raise AssertionError(
                    "synthetic report performance field "
                    f"{field} for {row['assignment_id']}: "
                    f"{row[field]!r} != {value!r}"
                )
    # Paired fail-before: the P1-era oracle values this fixture originally
    # encoded (window-first opening, strategy flows booked as returns) must
    # fail against the engine-correct report.  With no strategy-mapped flow
    # legs the only moving rule is the R1 opening point, so exactly the
    # successor epochs with a pre-start observation move.
    stale_payloads = _p1_era_epoch_payloads(baseline_doc)
    stale_performance = reporter._account_strategy_performance(
        baseline_doc, current_epoch_details=_epoch_details(stale_payloads)
    )
    stale_by_key = {_epoch_key(row): row for row in stale_performance}
    moved = {
        key
        for key, payload in expected_payloads.items()
        if abs(Decimal(stale_by_key[key]["twr"]) - Decimal(payload["twr"]))
        > Decimal("1e-12")
    }
    if moved != pre_start_keys:
        raise AssertionError(
            f"synthetic P1-era fail-before proof moved {sorted(moved)}, "
            f"expected exactly {sorted(pre_start_keys)}"
        )
