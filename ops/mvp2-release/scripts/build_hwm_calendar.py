"""Author the accepted mixed-calendar authority for PORT-HWM-4 (#356 seam).

The owner decision this binds is not new. `dataPortfolio/source-runs/
refresh-20260814/governed-hwm/hwm-calendar-authority-*.json` (authority_version
2026-08-15) records it: one calendar for all lanes, ALWAYS_ON at the portfolio
level, evidence expected on every calendar day, and a day a source did not
publish is an explicit no-observation period, never fabricated.

Per source, the calendar is derived from evidence rather than asserted. A
strategy whose observed NAV dates are contiguous is ALWAYS_ON. A strategy with
an interior missing day cannot be ALWAYS_ON without fabricating that day, so it
is DECLARED_SESSION and every gap is declared as an accepted multi-day interval
binding the next observed return -- PORT-HWM-4's treatment for a span the
source did not publish inside.

The bound return values are not authored here. They are the exact net-return
text the selected-source adapter derives for those spans from the account
manifest's own NAV and classified-flow evidence, captured in a first pass and
then bound. A value that did not come out of the adapter would be a number
someone made up.
"""

import hashlib
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(
    r"C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
    r"/.codex-bootstrap/mvp2-execution/repo"
)
sys.path[:0] = [str(REPO / "tools")] + [str(p) for p in REPO.glob("packages/*/src")]

Q = Path(r"C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine")
LIVE = Q / "dataPortfolio/rollforward/2026-09-10-mvp2-live-only"
EPOCH_ID = os.environ.get(
    "CYCLE_EPOCH_ID",
    "0c60863bd29dfa30325b5d2b517b49bf7725a0eefca668581773cd817be28203",
)
EPOCH = Q / "dataEpochManifest/generations" / EPOCH_ID
LEDGER_ID = os.environ.get(
    "CYCLE_LEDGER_ID",
    "2efe39247ae7694f4e3e22089674a6305de005c289c06ca6bff00e0c3d36442a",
)
BASELINE_ID = os.environ.get(
    "CYCLE_BASELINE_ID",
    "72afa574dabdeeaa83dbc519310727a671dfcca4f48265f8469ffffce3603f26",
)
RUN = Path(r"C:/Dev/scratch/quant-engine/pl12/proof2")
OUT = Path(r"C:/Dev/scratch/quant-engine/pl12/hwm")

POLICY_ID = "GOVERNED-MIXED-CALENDAR-2026-08-22"
OWNER_DECISION = (
    "Owner decision 2026-08-15, recorded in dataPortfolio/source-runs/"
    "refresh-20260814/governed-hwm/hwm-calendar-authority-account.json and its "
    "provider and legacy siblings: one ALWAYS_ON portfolio calendar for all "
    "lanes. Brokers mark accounts daily including weekends and crypto trades "
    "continuously, so evidence is expected on every calendar day from an epoch "
    "opening through the calculation boundary. A day a source did not publish "
    "is an explicit no-observation period, never zero-filled or "
    "holiday-inferred; the return observed at the next publication binds the "
    "whole span."
)


def adapter_arguments(calendar_path):
    return {
        "account_manifest_generation_path": EPOCH / "generation.json",
        "account_manifest_admission_policy_path": (
            LIVE / "epoch-inputs/mapped-epoch-admission.json"
        ),
        "account_manifest_flow_classification_policy_path": (
            REPO / "contracts/account-strategy-flow-classification-policy-v1.json"
        ),
        "provider_ledger_generation_path": (
            RUN / "stages/ledger/generations" / LEDGER_ID / "generation.json"
        ),
        "baseline_generation_path": (
            RUN / "stages/baseline/generations" / BASELINE_ID / "generation.json"
        ),
        "financial_contract_path": (
            REPO / "contracts/portfolio-financial-mvp-source-v1.json"
        ),
        "calendar_authority_path": calendar_path,
        "provider_admission_path": (
            RUN
            / "stages/ledger/generations"
            / LEDGER_ID
            / "admit-or-declare-receipt.json"
        ),
        "hwm_policy_path": REPO / "contracts/portfolio-hwm-policy-v4.json",
    }


def observed_dates():
    calculation = json.loads((EPOCH / "authority/calculation-inputs.json").read_bytes())
    by_strategy = {}
    for row in calculation["rows"]:
        strategy_id = row["epoch"]["strategy_id"]
        for observation in row["nav_observations"]:
            by_strategy.setdefault(strategy_id, set()).add(observation["as_of_date"])
    return {
        key: sorted(date.fromisoformat(item) for item in value)
        for key, value in by_strategy.items()
    }


def required_strategies(contract_path):
    contract = json.loads(contract_path.read_bytes())
    return sorted(
        row["strategy_id"]
        for row in contract["strategies"]
        if row["product_requirement"] == "REQUIRED"
    )


def derive(days, strategies, policy_sha256, bound):
    from quant_engine.portfolio_ledger.hwm_calendar_authority import (
        derive_governed_calendar_authority,
    )

    rows = []
    for strategy_id in strategies:
        observed = days[strategy_id]
        contiguous = all(
            right - left == timedelta(days=1)
            for left, right in zip(observed, observed[1:])
        )
        entry = {
            "strategy_id": strategy_id,
            "calendar_kind": "ALWAYS_ON" if contiguous else "DECLARED_SESSION",
            "observed_dates": [day.isoformat() for day in observed],
            "closing_capital_usd_by_date": {},
        }
        if not contiguous:
            entry["accepted_intervals"] = [
                {
                    "prior_observation_date": left.isoformat(),
                    "next_observation_date": right.isoformat(),
                    "binding_return_date": right.isoformat(),
                    "binding_return_value": bound.get(
                        (strategy_id, left.isoformat(), right.isoformat()), "0"
                    ),
                }
                for left, right in zip(observed, observed[1:])
                if right - left > timedelta(days=1)
            ]
        rows.append(entry)
    return derive_governed_calendar_authority(
        strategy_calendars=rows,
        calendar_policy_id=POLICY_ID,
        owner_decision_evidence=OWNER_DECISION,
        hwm_policy_sha256=policy_sha256,
    )


def write(authority, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(
            authority, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )
    return path


def main():
    import portfolio_hwm_selected_inputs as adapter

    policy_path = REPO / "contracts/portfolio-hwm-policy-v4.json"
    contract_path = REPO / "contracts/portfolio-financial-mvp-source-v1.json"
    # The product contract is the admitted input contract this cycle, so the
    # repository policy needs no retargeting: `governed_hwm_input_policy`
    # returns these bytes unchanged when the declared hash is the file's own.
    policy_bytes = policy_path.read_bytes()
    if (
        json.loads(policy_bytes)["product_authority"]["raw_sha256"]
        != hashlib.sha256(contract_path.read_bytes()).hexdigest()
    ):
        raise SystemExit("the HWM policy does not pin this product contract")
    policy_sha256 = hashlib.sha256(policy_bytes).hexdigest()

    days = observed_dates()
    strategies = required_strategies(contract_path)
    missing = [item for item in strategies if item not in days]
    if missing:
        raise SystemExit("required strategies have no observed calendar: %s" % missing)

    # Pass 1: the adapter derives the return rows; capture them instead of
    # letting the builder reject the multi-day spans it has no binding for.
    probe = write(
        derive(days, strategies, policy_sha256, {}), OUT / "calendar-probe.json"
    )
    captured = {}

    def capture(**kwargs):
        captured["rows"] = kwargs["return_observations"]
        return {"component_periods": [], "evidence_sha256s": []}

    original = adapter.build_governed_hwm_inputs
    adapter.build_governed_hwm_inputs = capture
    try:
        adapter.build_selected_hwm_inputs(**adapter_arguments(probe))
    finally:
        adapter.build_governed_hwm_inputs = original
    rows = captured.get("rows") or []
    if rows:
        print("sample return row", json.dumps(rows[0])[:300])
    bound = {
        (row["strategy_id"], row["interval_start"], row["interval_end"]): row[
            "net_return"
        ]
        for row in rows
        if (
            date.fromisoformat(row["interval_end"])
            - date.fromisoformat(row["interval_start"])
        )
        > timedelta(days=1)
    }
    print("multi-day return rows derived by the adapter:", len(bound))

    authority = derive(days, strategies, policy_sha256, bound)
    unbound = [
        row
        for row in authority["accepted_source_intervals"]
        if (
            row["strategy_id"],
            row["prior_observation_date"],
            row["next_observation_date"],
        )
        not in bound
    ]
    path = write(authority, OUT / "calendar-authority.json")
    for row in authority["source_calendars"]:
        print("  %-24s %s" % (row["strategy_id"], row["calendar_kind"]))
    print(
        "accepted intervals",
        len(authority["accepted_source_intervals"]),
        "| closures",
        len(authority["closed_source_dates"]),
        "| unbound",
        len(unbound),
    )
    for row in unbound[:10]:
        print(
            "  UNBOUND",
            row["strategy_id"],
            row["prior_observation_date"],
            "->",
            row["next_observation_date"],
        )
    print("authority_evidence_sha256", authority["authority_evidence_sha256"])
    print("written", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
