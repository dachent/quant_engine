"""Derive the governed INVESTED_CAPITAL input generation from this cycle's ledger.

Every row here is computed from the provider-ledger generation this release
admitted. Nothing is copied from an earlier cycle and nothing is asserted:

* `required_epochs` are the epoch-manifest generation's own epochs, with the
  sleeve alias resolved through the contract census assignment that names the
  same account, strategy and start.
* `opening_basis` is one AVAILABLE_ACCOUNT_LEDGER_NET row per epoch, built by
  the exact recipe `_verify_account_ledger_opening` re-derives: every
  owner-capital ledger fact for that account dated before the epoch opens,
  sorted, with the descriptor digest as both source and FX evidence.
* `events` are the remaining owner-capital facts, attributed to the epoch whose
  account they belong to and whose window contains their effective date.
* `flow_coverage` cites the ledger's own AUTHORITATIVE `flows` admission row
  for that account.

The result is then built and verified by the repository producer, so a row this
script got wrong fails closed rather than shipping.
"""

import hashlib
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

REPO = Path(
    r"C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
    r"/.codex-bootstrap/mvp2-execution/repo"
)
sys.path[:0] = [str(REPO / "tools")] + [str(p) for p in REPO.glob("packages/*/src")]

Q = Path(r"C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine")


def _cycle_id(env_name, result_name, key="generation_id"):
    """Read an identity from THIS cycle's stage result rather than a literal.

    These used to default to ids from an earlier cycle. Nothing forced the
    environment variable to be set, so a run that forgot it silently bound
    generations that no longer exist on disk -- which is how a stale ledger id
    survived into several drivers and cost a day of one-gate-per-run discovery.
    A missing stage result is now an error, not a fallback.
    """

    override = os.environ.get(env_name)
    if override:
        return override
    path = Path(r"C:/Dev/scratch/quant-engine/pl12/cycle2") / result_name
    if not path.is_file():
        raise SystemExit(
            "cycle result is absent, cannot derive %s: %s" % (env_name, path)
        )
    return json.loads(path.read_text(encoding="utf-8"))[key]


EPOCH_ID = _cycle_id("CYCLE_EPOCH_ID", "epoch-manifest.json")
EPOCH = Q / "dataEpochManifest/generations" / EPOCH_ID
LEDGER_ID = _cycle_id("CYCLE_LEDGER_ID", "stage-ledger.json")
# The production specification binds capital's provider ledger to the copy the
# PIPELINE staged, and requires the path this file records to sit inside it, so
# the root has to be selectable. The ledger generation id is identical in both
# roots, which is what makes the two bindings interchangeable at all.
RUN = Path(
    os.environ.get("CAPITAL_RUN_ROOT", r"C:/Dev/scratch/quant-engine/pl12/proof2")
)
LEDGER = RUN / "stages/ledger/generations" / LEDGER_ID
OUT = Path(r"C:/Dev/scratch/quant-engine/pl12/capital")

CONTRACT = REPO / "contracts/portfolio-financial-mvp-source-v1.json"
REGISTRY = REPO / "docs/analytics/current-strategy-decomposition-plan.json"
LEDGER_SPEC = (
    Q
    / "dataPortfolio/rollforward/2026-09-10-mvp2-live-only/config"
    / "provider-ledger-spec-release.json"
)
CROSSWALK = (
    Q
    / "dataPortfolio/rollforward/2026-09-05-mvp2-execution/source-repair/evidence"
    / "schwab-sleeve-account-crosswalk-v2.json"
)
EVALUATION_DATE = "2026-09-11"

_MAGNITUDE = {
    "EXTERNAL_WITHDRAWAL",
    "DIVIDEND_INTEREST_REINVESTMENT",
    "FEE_TAX_REBATE_EXPENSE",
    "MARGIN_FINANCING",
}


def main():
    import portfolio_invested_capital as capital

    ledger_path = LEDGER / "provider-ledger.json"
    ledger = json.loads(ledger_path.read_bytes())
    contract_bytes = CONTRACT.read_bytes()
    contract_raw = json.loads(contract_bytes)
    contract = capital.load_contract(CONTRACT, registry_path=REGISTRY)
    rules = capital._load_flow_classification_rules()

    source_rows = {
        capital._digest(row): row
        for name in ("capital_anchors", "flows")
        for row in ledger.get(name, [])
    }

    # --- epochs -------------------------------------------------------------
    # A census sleeve names no account: the private sleeve -> account mapping is
    # resolved from the provider spec's own provenance, exactly as
    # `_verify_epoch_accounts` re-resolves it -- candidates that carry every
    # assignment in a sleeve's signature, then sorted-to-sorted within a group
    # of sleeves that share one signature. The owner-accepted crosswalk is the
    # tie-break the verifier then checks, so it must agree by construction.
    from provider_ledger import _provenance

    manifest = json.loads(
        (EPOCH / "outputs/account-strategy-manifest.json").read_bytes()
    )
    spec = json.loads(LEDGER_SPEC.read_bytes())
    provenance, _identity = _provenance(spec)
    account_ids = {row["account_id"] for row in provenance["accounts"]}
    groups = {}
    for sleeve in contract_raw["current_census"]["account_sleeves"]:
        if not sleeve["assignments"]:
            continue
        signature = tuple(
            sorted(
                (
                    item["strategy_id"],
                    item["effective_start"],
                    item.get("effective_end"),
                )
                for item in sleeve["assignments"]
            )
        )
        groups.setdefault(signature, []).append(sleeve["sleeve_alias"])
    alias_by_account = {}
    for signature, aliases in groups.items():
        candidates = None
        for strategy_id, start, end in signature:
            matches = {
                str(row["account_id"])
                for row in provenance["assignments"]
                if row.get("strategy_id") == strategy_id
                and row.get("effective_start") == start
                and row.get("effective_end") == end
                and row.get("account_id") in account_ids
                and row.get("mapping_status") == "MAPPED"
                and row.get("strategy_mapping_disposition") == "STRATEGY_MAPPED"
            }
            candidates = matches if candidates is None else candidates & matches
        if not candidates:
            continue
        if len(candidates) != len(aliases):
            raise SystemExit("sleeve/account cardinality differs for %s" % (aliases,))
        for alias, account_id in zip(sorted(aliases), sorted(candidates)):
            alias_by_account[account_id] = alias
    crosswalk = json.loads(CROSSWALK.read_bytes())
    for row in crosswalk["bindings"]:
        bound = alias_by_account.get(row["account_id"])
        if bound is not None and bound != row["sleeve_alias"]:
            raise SystemExit(
                "derived sleeve mapping contradicts the accepted crosswalk: %s"
                % row["account_id"]
            )
    epochs = []
    for row in manifest["rows"]:
        alias = alias_by_account.get(row["account_id"])
        if alias is None:
            raise SystemExit("no census sleeve names account %s" % row["account_id"])
        epochs.append(
            {
                "account_id": row["account_id"],
                "effective_end": row["effective_end"],
                "effective_start": row["effective_start"],
                "epoch_id": "%s|%s|%s"
                % (alias, row["strategy_id"], row["effective_start"]),
                "sleeve_alias": alias,
                "strategy_id": row["strategy_id"],
            }
        )
    print("epochs", len(epochs))

    required_account_ids = {row["account_id"] for row in epochs}

    # --- opening basis ------------------------------------------------------
    # One opening per ACCOUNT, not per epoch. An account's later epoch opens on
    # its predecessor's closing capital, which the calculation carries; giving
    # it a second ledger-net basis would consume the same facts twice.
    first_epoch_by_account = {}
    for epoch in epochs:
        current = first_epoch_by_account.get(epoch["account_id"])
        if current is None or epoch["effective_start"] < current["effective_start"]:
            first_epoch_by_account[epoch["account_id"]] = epoch
    opening_basis = []
    consumed = set()
    for epoch in sorted(
        first_epoch_by_account.values(), key=lambda row: row["epoch_id"]
    ):
        account_id = epoch["account_id"]
        end_exclusive = date.fromisoformat(epoch["effective_start"])
        eligible = {
            digest: row
            for digest, row in source_rows.items()
            if row.get("account_id") == account_id
            and capital._is_owner_capital_source(row, rules)
            and date.fromisoformat(row["event_date"]) < end_exclusive
        }
        components = sorted(eligible)
        start = (
            min(
                date.fromisoformat(row["event_date"]) for row in eligible.values()
            ).isoformat()
            if eligible
            else end_exclusive.isoformat()
        )
        descriptor = {
            "basis_scope": capital._ACCOUNT_LEDGER_BASIS_SCOPE,
            "account_id": account_id,
            "ledger_period_start": start,
            "ledger_period_end_exclusive": end_exclusive.isoformat(),
            "source_component_sha256s": components,
        }
        evidence = capital._digest(descriptor)
        effect = sum(
            (capital._source_effect_usd(source_rows[item]) for item in components),
            Decimal(0),
        )
        opening_basis.append(
            {
                "account_id": account_id,
                "amount": capital._number(effect),
                "basis_scope": capital._ACCOUNT_LEDGER_BASIS_SCOPE,
                "classification": "OPENING_CONTRIBUTED_BASIS",
                "currency": "USD",
                "date": end_exclusive.isoformat(),
                "epoch_id": epoch["epoch_id"],
                "event_id": "account-ledger-opening:%s:%s"
                % (account_id, end_exclusive.isoformat()),
                "fx_evidence_sha256": evidence,
                "fx_rate": "1",
                "ledger_period_end_exclusive": end_exclusive.isoformat(),
                "ledger_period_start": start,
                "source_component_sha256s": components,
                "source_evidence_sha256": evidence,
                "timestamp": None,
            }
        )
        consumed |= set(components)
    print(
        "opening basis rows", len(opening_basis), "| components consumed", len(consumed)
    )

    # The boundary is the calculation cutoff every series is truncated to, not
    # the ledger's own as-of date: the ledger observes two days past the last
    # date common to all sources.
    complete_through = manifest["calculation_cutoff_date"]

    # --- events -------------------------------------------------------------
    # Disposition comes from the capital tool's own classifier over the whole
    # selected perimeter, so the EXTERNAL/INTERNAL call here is the same call
    # the verifier makes. The effective date is the raw account-ledger event
    # date, which is also the date the omitted-flow check scopes by.
    dispositions = capital._account_flow_dispositions(
        list(source_rows.values()), rules, required_account_ids
    )
    epoch_by_account = {}
    for epoch in epochs:
        epoch_by_account.setdefault(epoch["account_id"], []).append(epoch)
    events = []
    unattributed = []
    for digest, row in sorted(source_rows.items()):
        if digest in consumed:
            continue
        if not capital._is_owner_capital_source(row, rules):
            continue
        account_id = row.get("account_id")
        if account_id not in epoch_by_account:
            continue
        effective = row["event_date"]
        if effective > complete_through:
            continue
        candidates = [
            epoch
            for epoch in epoch_by_account[account_id]
            if epoch["effective_start"] <= effective
            and (epoch["effective_end"] is None or effective <= epoch["effective_end"])
        ]
        if len(candidates) != 1:
            unattributed.append(
                (row.get("record_id"), account_id, effective, len(candidates))
            )
            continue
        native = Decimal(row["amount"]["value"])
        disposition = dispositions.get(row["record_id"])
        if disposition == "INTERNAL":
            classification = "INTERNAL_TRANSFER"
            amount = native
        elif native > 0:
            classification = "EXTERNAL_CONTRIBUTION"
            amount = native
        else:
            classification = "EXTERNAL_WITHDRAWAL"
            amount = native.copy_abs()
        event = {
            "amount": capital._number(amount),
            "classification": classification,
            "currency": row["amount"]["currency"],
            "date": effective,
            "epoch_id": candidates[0]["epoch_id"],
            "event_id": str(row["record_id"]),
            "fx_evidence_sha256": digest,
            "fx_rate": "1",
            "source_date_basis": capital._RAW_ACCOUNT_LEDGER_DATE,
            "source_evidence_sha256": digest,
            "timestamp": row.get("event_timestamp"),
        }
        if classification == "INTERNAL_TRANSFER":
            event["transfer_id"] = row.get("provider_activity_id_sha256")
        if row["amount"]["currency"] != "USD":
            fx = row.get("event_time_fx") or {}
            event["fx_rate"] = fx.get("rate")
        events.append(event)
    print("events", len(events), "| unattributed", len(unattributed))
    for item in unattributed[:10]:
        print("  UNATTRIBUTED", item)

    # --- flow coverage ------------------------------------------------------
    coverage = []
    for epoch in epochs:
        epoch_end = epoch["effective_end"] or complete_through
        matches = [
            row
            for row in ledger["admissions"]
            if row.get("account_id") == epoch["account_id"]
            and row.get("fact_type") == "flows"
            and row.get("disposition") == "AUTHORITATIVE"
            and row.get("effective_start") <= epoch["effective_start"]
            and row.get("effective_end") >= min(epoch_end, complete_through)
        ]
        if not matches:
            raise SystemExit(
                "no authoritative flow admission covers %s" % epoch["epoch_id"]
            )
        coverage.append(
            {
                "end_date": min(epoch_end, complete_through),
                "epoch_id": epoch["epoch_id"],
                "evidence_sha256": capital._digest(matches[0]),
                "start_date": epoch["effective_start"],
            }
        )

    generation = {
        "authority_profile": "ACCEPTED_GOVERNED",
        "complete_through": complete_through,
        "correction_bindings": [],
        "evaluation_date": EVALUATION_DATE,
        "events": events,
        "flow_coverage": coverage,
        "opening_basis": opening_basis,
        "policy_sha256": capital.invested_capital_policy_digest(contract),
        "product_contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
        "required_epochs": epochs,
        "schema_version": 1,
        "source_bindings": {
            "provider_ledger": capital._file_binding(ledger_path)[0],
            "provider_ledger_spec": capital._file_binding(LEDGER_SPEC)[0],
            "account_crosswalk": capital._file_binding(CROSSWALK)[0],
            "strategy_registry": capital._file_binding(REGISTRY)[0],
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    input_path = OUT / "capital-input-generation.json"
    input_path.write_bytes(capital._canonical(generation))
    print("written", input_path)

    result = capital.build_governed_capital_result(input_path, CONTRACT)
    result_path = OUT / "invested-capital.json"
    result_path.write_bytes(capital._canonical(result))
    print("status           ", result.get("status"))
    print("authority_profile", result.get("authority_profile"))
    print("result_id        ", result.get("result_id"))
    print("current aggregate", result.get("current_aggregate_usd"))
    print("written", result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
