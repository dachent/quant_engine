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

import argparse
import hashlib
import json
import sys
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path

_MAGNITUDE = {
    "EXTERNAL_WITHDRAWAL",
    "DIVIDEND_INTEREST_REINVESTMENT",
    "FEE_TAX_REBATE_EXPENSE",
    "MARGIN_FINANCING",
}


def _canonical_date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def build_input_generation(
    *,
    provider_ledger_generation_root: Path,
    epoch_manifest_generation_root: Path,
    provider_ledger_spec_path: Path,
    financial_contract_path: Path,
    strategy_registry_path: Path,
    account_crosswalk_path: Path,
    requested_as_of: str,
    evaluated_on: str,
) -> tuple[dict, dict[str, str]]:
    """Build the capital input generation and this machine's locators for it."""

    requested_date = _canonical_date(requested_as_of, "requested_as_of")
    evaluation_date = _canonical_date(evaluated_on, "evaluated_on")
    if evaluation_date < requested_date:
        raise ValueError("evaluated_on cannot be before requested_as_of")

    import portfolio_invested_capital as capital
    from portfolio_hwm_selected_inputs import _source_generation_from_path
    from quant_engine.core.publication import read_generation_bundle

    provider_ledger_generation_root = Path(provider_ledger_generation_root)
    epoch_manifest_generation_root = Path(epoch_manifest_generation_root)
    provider_ledger_spec_path = Path(provider_ledger_spec_path)
    financial_contract_path = Path(financial_contract_path)
    strategy_registry_path = Path(strategy_registry_path)
    account_crosswalk_path = Path(account_crosswalk_path)

    _source_generation_from_path(
        provider_ledger_generation_root / "generation.json", "provider-ledger"
    )
    epoch_bundle = read_generation_bundle(
        epoch_manifest_generation_root.parents[1], epoch_manifest_generation_root.name
    )
    if epoch_bundle.manifest.verification.value != "VERIFIED":
        raise ValueError("epoch-manifest generation is not VERIFIED")

    ledger_path = provider_ledger_generation_root / "provider-ledger.json"
    ledger = json.loads(ledger_path.read_bytes())
    contract_bytes = financial_contract_path.read_bytes()
    contract_raw = json.loads(contract_bytes)
    contract = capital.load_contract(
        financial_contract_path, registry_path=strategy_registry_path
    )
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
        (
            epoch_manifest_generation_root / "outputs/account-strategy-manifest.json"
        ).read_bytes()
    )
    spec = json.loads(provider_ledger_spec_path.read_bytes())
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
    crosswalk = json.loads(account_crosswalk_path.read_bytes())
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
    capital_policy = next(
        row["calculation_contract"]
        for row in contract["financial_outcomes"]
        if row["outcome_id"] == "INVESTED_CAPITAL"
    )
    custody_accounts = capital._verify_provider_custody_perimeter(
        ledger, epochs, capital_policy
    )
    # A provider whose one account funds several funds (policy, #536) counts
    # the allocations to the strategy's fund as capital, not account deposits.
    rules = capital.with_fund_scope(rules, capital_policy)

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
            and (
                account_id not in custody_accounts
                or capital._is_provider_custody_external_flow(row)
            )
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
        with localcontext() as context:
            context.prec = capital._capital_precision(
                [source_rows[item] for item in components]
            )
            context.rounding = ROUND_HALF_EVEN
            effect = sum(
                (capital._source_effect_usd(source_rows[item]) for item in components),
                Decimal(0),
            )
            exact_amount = capital._number(effect)
        opening_basis.append(
            {
                "account_id": account_id,
                "amount": exact_amount,
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
    if complete_through != requested_as_of:
        raise ValueError(
            "epoch-manifest calculation_cutoff_date differs from requested_as_of"
        )

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
        if (
            account_id in custody_accounts
            and not capital._is_provider_custody_external_flow(row)
        ):
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
        "evaluation_date": evaluated_on,
        "events": events,
        "flow_coverage": coverage,
        "opening_basis": opening_basis,
        "policy_sha256": capital.invested_capital_policy_digest(contract),
        "product_contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
        "required_epochs": epochs,
        "schema_version": 1,
        "source_bindings": {
            # The generation id is portable identity, not a locator, and it
            # is what proves this ledger came from the SELECTED generation
            # rather than merely having the same bytes as some other one.
            "provider_ledger": {
                **capital._file_binding(ledger_path)[0],
                "generation_id": provider_ledger_generation_root.name,
            },
            "provider_ledger_spec": capital._file_binding(provider_ledger_spec_path)[0],
            "account_crosswalk": capital._file_binding(account_crosswalk_path)[0],
            "strategy_registry": capital._file_binding(strategy_registry_path)[0],
        },
    }
    # The generation binds bytes by identity only, so it is the same document
    # wherever the run happened. Where those bytes live on this machine is
    # returned separately and written to a sidecar that is never hashed,
    # published, or copied into the report candidate.
    locators = {
        "provider_ledger": capital._local_binding(ledger_path),
        "provider_ledger_spec": capital._local_binding(provider_ledger_spec_path),
        "account_crosswalk": capital._local_binding(account_crosswalk_path),
        "strategy_registry": capital._local_binding(strategy_registry_path),
    }
    return generation, locators


def main(argv=None):
    repository_root = Path(__file__).resolve().parents[3]
    cli_import_roots = [
        repository_root / "tools",
        *sorted(repository_root.glob("packages/*/src")),
    ]
    for import_root in reversed(cli_import_roots):
        import_root_text = str(import_root)
        if import_root_text not in sys.path:
            sys.path.insert(0, import_root_text)

    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-ledger-generation-root", type=Path, required=True)
    parser.add_argument("--epoch-manifest-generation-root", type=Path, required=True)
    parser.add_argument("--provider-ledger-spec", type=Path, required=True)
    parser.add_argument("--financial-contract", type=Path, required=True)
    parser.add_argument("--strategy-registry", type=Path, required=True)
    parser.add_argument("--account-crosswalk", type=Path, required=True)
    parser.add_argument("--requested-as-of", required=True)
    parser.add_argument("--evaluated-on", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)

    import portfolio_invested_capital as capital

    generation, locators = build_input_generation(
        provider_ledger_generation_root=args.provider_ledger_generation_root,
        epoch_manifest_generation_root=args.epoch_manifest_generation_root,
        provider_ledger_spec_path=args.provider_ledger_spec,
        financial_contract_path=args.financial_contract,
        strategy_registry_path=args.strategy_registry,
        account_crosswalk_path=args.account_crosswalk,
        requested_as_of=args.requested_as_of,
        evaluated_on=args.evaluated_on,
    )
    args.output_root.mkdir(parents=True, exist_ok=True)
    input_path = args.output_root / "capital-input-generation.json"
    capital.assert_no_absolute_locators(
        generation, field_name="capital input generation"
    )
    input_path.write_bytes(capital._canonical(generation))
    capital.write_local_sidecar(input_path, locators)
    result = capital.build_governed_capital_result(input_path, args.financial_contract)
    capital.verify_governed_capital_result(result, input_path, args.financial_contract)
    result_path = args.output_root / "invested-capital.json"
    result_path.write_bytes(capital._canonical(result))
    print("written", input_path)
    print("written", result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
