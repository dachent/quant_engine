"""Author the HWM calendar authority a release cycle pins before its attended run.

RELEASE-DRIVER-001, hand-script replacement block B6 (replaces ``mk_calendar2.py``; ``mk_calendar.py``
only re-issued a seed for an older policy hash and is not needed).

The attended run derives its calendar from an *accepted* authority (``calendar_authority`` in the run
config, pinned by ``calendar_authority_sha256``): which strategies are ``ALWAYS_ON``, which gaps are
accepted multi-day intervals, and the exact net-return text each interval binds.  That authority is
authored here from the first attempt's own staged evidence, in two passes:

1. every strategy's calendar kind and gaps are derived from its observed NAV dates (contiguous means
   ``ALWAYS_ON``; an interior missing day means ``DECLARED_SESSION`` with every gap declared);
2. the selected-source adapter derives the return for each declared span from the account manifest's
   NAV and classified-flow evidence, and exactly those values are bound.  A value the adapter did not
   derive is never written (an unbound interval is a refusal).

The calendar policy id and the owner decision text are carried from the predecessor authority
(``--seed-authority``), as the owner decision is unchanged between cycles.  Everything else comes from
files: the resolved cycle (``release_cycle_manifest.py resolve``), the first attempt's run directory
and the prepared spec.  No date, id or path lives in this module.

``author``  write the authority create-only (``--out``) plus a receipt next to it; ``--check`` re-derives
            and refuses when the file at ``--out`` differs.

Exit 0 ok, 1 refusal (``CODE: detail`` on stderr), 2 usage.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
RESOLVED_VERSION = "qe-release-resolved-cycle/1"
RECEIPT_VERSION = "qe-release-hwm-calendar-receipt/1"
# Repository contracts the attended preparation also reads (it names them the same way).
DEFAULT_HWM_POLICY = "contracts/portfolio-hwm-policy-v4.json"
DEFAULT_CONTRACT = "contracts/portfolio-financial-mvp-source-v1.json"
DEFAULT_FLOW_POLICY = "contracts/account-strategy-flow-classification-policy-v1.json"


class CalendarAuthorityError(Exception):
    """A refusal. ``code`` is the message class; ``str(error)`` is ``CODE: detail``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CalendarAuthorityError("INPUT", f"{what} is unreadable: {exc}") from exc


def _file(path: Path, what: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise CalendarAuthorityError("MISSING", f"{what} is not a regular file: {path}")
    return path


def load_resolved_cycle(path: Path) -> Mapping[str, Any]:
    cycle = _read_json(_file(path, "resolved cycle"), "resolved cycle")
    if not isinstance(cycle, dict) or cycle.get("schema_version") != RESOLVED_VERSION:
        raise CalendarAuthorityError(
            "RESOLVED_CYCLE", f"schema_version is not {RESOLVED_VERSION}"
        )
    try:
        date.fromisoformat(str(cycle["cutoff"]))
        policy = cycle["epoch_admission_policy"]
        Path(policy["path"])
        str(cycle["cycle_manifest_sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CalendarAuthorityError(
            "RESOLVED_CYCLE",
            "cutoff, epoch_admission_policy and cycle_manifest_sha256 are required",
        ) from exc
    return cycle


def _only_generation(run: Path, stage: str, chosen: str | None) -> Path:
    """The one generation a stage of the first attempt staged (or the one named)."""

    directory = run / "stages" / stage / "generations"
    if not directory.is_dir():
        raise CalendarAuthorityError("MISSING", f"stage {stage} has no generations")
    found = sorted(item.name for item in directory.iterdir() if item.is_dir())
    if chosen is not None:
        if chosen not in found:
            raise CalendarAuthorityError(
                "MISSING", f"stage {stage} has no generation {chosen}"
            )
        return directory / chosen
    if len(found) != 1:
        raise CalendarAuthorityError(
            "AMBIGUOUS_GENERATION",
            f"stage {stage} holds {len(found)} generations; name one",
        )
    return directory / found[0]


def observed_dates(calculation: Mapping[str, Any]) -> dict[str, list[date]]:
    """Each strategy's observed NAV dates from the epoch calculation inputs."""

    by_strategy: dict[str, set[str]] = {}
    for row in calculation["rows"]:
        strategy_id = row["epoch"]["strategy_id"]
        for observation in row["nav_observations"]:
            by_strategy.setdefault(strategy_id, set()).add(observation["as_of_date"])
    return {
        key: sorted(date.fromisoformat(item) for item in value)
        for key, value in by_strategy.items()
    }


def required_strategies(contract: Mapping[str, Any]) -> list[str]:
    return sorted(
        row["strategy_id"]
        for row in contract["strategies"]
        if row["product_requirement"] == "REQUIRED"
    )


def _calendar_rows(
    days: Mapping[str, Sequence[date]],
    strategies: Sequence[str],
    bound: Mapping[tuple[str, str, str], str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for strategy_id in strategies:
        observed = days[strategy_id]
        contiguous = all(
            right - left == timedelta(days=1)
            for left, right in zip(observed, observed[1:])
        )
        entry: dict[str, Any] = {
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
    return rows


def render(authority: Mapping[str, Any]) -> bytes:
    """The authority as the R2 cycle wrote it: sorted keys, indent 1, LF, ASCII-escaped."""

    return json.dumps(authority, indent=1, sort_keys=True).encode("utf-8")


def derive_authority(
    *,
    resolved: Mapping[str, Any],
    run: Path,
    spec: Mapping[str, Any],
    seed: Mapping[str, Any],
    repo_root: Path = REPO_ROOT,
    hwm_policy: str = DEFAULT_HWM_POLICY,
    contract: str = DEFAULT_CONTRACT,
    flow_policy: str = DEFAULT_FLOW_POLICY,
    epoch_generation: str | None = None,
    ledger_generation: str | None = None,
    baseline_generation: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Two-pass derivation.  Returns the authority and the receipt facts."""

    import portfolio_hwm_selected_inputs as selected_inputs
    import portfolio_invested_capital as capital
    from quant_engine.portfolio_ledger import derive_governed_calendar_authority

    adapter: Any = selected_inputs

    for key in ("calendar_policy_id", "owner_decision_evidence"):
        if not isinstance(seed.get(key), str) or not seed[key].strip():
            raise CalendarAuthorityError("SEED", f"seed authority lacks {key}")
    cutoff = date.fromisoformat(str(resolved["cutoff"]))
    contract_path = _file(repo_root / contract, "financial contract")
    policy_path = _file(repo_root / hwm_policy, "HWM policy")
    flow_path = _file(repo_root / flow_policy, "flow classification policy")
    policy_current = policy_path.read_bytes()
    effective_policy = capital.governed_hwm_input_policy(
        policy_current, contract_path.read_bytes(), _sha256(policy_current)
    )
    policy_sha = _sha256(effective_policy)

    epoch = _only_generation(run, "epoch", epoch_generation)
    ledger = _only_generation(run, "ledger", ledger_generation)
    baseline = _only_generation(run, "baseline", baseline_generation)
    sleeves = sorted((run / "preparation" / "sleeve-facts").glob("*-facts.json"))
    if not sleeves:
        raise CalendarAuthorityError("MISSING", "the run staged no sleeve facts")
    try:
        census = Path(spec["epoch_membership_census"]["path"])
    except (KeyError, TypeError) as exc:
        raise CalendarAuthorityError(
            "SPEC", "spec lacks epoch_membership_census.path"
        ) from exc
    admission_policy = Path(resolved["epoch_admission_policy"]["path"])

    calculation = _read_json(
        _file(epoch / "authority" / "calculation-inputs.json", "calculation inputs"),
        "calculation inputs",
    )
    days = observed_dates(calculation)
    strategies = required_strategies(_read_json(contract_path, "financial contract"))
    missing = [item for item in strategies if item not in days]
    if missing:
        raise CalendarAuthorityError(
            "STRATEGY_PERIMETER",
            f"required strategies have no observed dates: {missing}",
        )
    late = sorted(
        {
            f"{strategy}:{day.isoformat()}"
            for strategy in strategies
            for day in days[strategy]
            if day > cutoff
        }
    )
    if late:
        raise CalendarAuthorityError(
            "AFTER_CUTOFF", f"observed dates after the cycle cutoff: {late[:5]}"
        )

    def derive(bound: Mapping[tuple[str, str, str], str]) -> dict[str, Any]:
        return dict(
            derive_governed_calendar_authority(
                strategy_calendars=_calendar_rows(days, strategies, bound),
                calendar_policy_id=seed["calendar_policy_id"],
                owner_decision_evidence=seed["owner_decision_evidence"],
                hwm_policy_sha256=policy_sha,
            )
        )

    def arguments(calendar_path: Path) -> dict[str, Any]:
        return {
            "account_manifest_generation_path": epoch / "generation.json",
            "account_manifest_admission_policy_path": admission_policy,
            "membership_census_path": census,
            "sleeve_facts_paths": tuple(sleeves),
            "account_manifest_flow_classification_policy_path": flow_path,
            "provider_ledger_generation_path": ledger / "generation.json",
            "baseline_generation_path": baseline / "generation.json",
            "financial_contract_path": contract_path,
            "calendar_authority_path": calendar_path,
            "provider_admission_path": ledger / "admit-or-declare-receipt.json",
            "hwm_policy_path": policy_path,
            "hwm_policy_bytes": effective_policy,
        }

    captured: dict[str, Any] = {}

    def capture(**kwargs: Any) -> dict[str, Any]:
        captured["rows"] = kwargs["return_observations"]
        return {"component_periods": [], "evidence_sha256s": []}

    with tempfile.TemporaryDirectory(prefix="hwm-calendar-probe-") as scratch:
        probe = Path(scratch) / "calendar-probe.json"
        probe.write_bytes(render(derive({})))
        original = adapter.build_governed_hwm_inputs
        adapter.build_governed_hwm_inputs = capture
        try:
            adapter.build_selected_hwm_inputs(**arguments(probe))
        finally:
            adapter.build_governed_hwm_inputs = original
    rows = captured.get("rows")
    if rows is None:
        raise CalendarAuthorityError("ADAPTER", "the adapter produced no return rows")
    bound = {
        (row["strategy_id"], row["interval_start"], row["interval_end"]): row[
            "net_return"
        ]
        for row in rows
        if date.fromisoformat(row["interval_end"])
        - date.fromisoformat(row["interval_start"])
        > timedelta(days=1)
    }
    authority = derive(bound)
    unbound = sorted(
        (item["strategy_id"], item["prior_observation_date"])
        for item in authority["accepted_source_intervals"]
        if (
            item["strategy_id"],
            item["prior_observation_date"],
            item["next_observation_date"],
        )
        not in bound
    )
    if unbound:
        raise CalendarAuthorityError(
            "UNBOUND_INTERVAL", f"no adapter-derived return for: {unbound[:5]}"
        )

    from quant_engine.portfolio_ledger import validate_governed_hwm_calendar_authority

    validate_governed_hwm_calendar_authority(
        authority,
        hwm_policy_bytes=effective_policy,
        accepted_authority_sha256=str(authority["authority_evidence_sha256"]),
    )
    facts = {
        "effective_hwm_policy_sha256": policy_sha,
        "epoch_generation_id": epoch.name,
        "ledger_generation_id": ledger.name,
        "baseline_generation_id": baseline.name,
        "sleeve_facts": [item.name for item in sleeves],
        "bound_interval_count": len(bound),
        "strategies": strategies,
    }
    return authority, facts


def _receipt(
    resolved: Mapping[str, Any],
    seed_bytes: bytes,
    authority_bytes: bytes,
    facts: Mapping[str, Any],
) -> bytes:
    document = {
        "schema_version": RECEIPT_VERSION,
        "cycle_manifest_sha256": resolved["cycle_manifest_sha256"],
        "cutoff": resolved["cutoff"],
        "predecessor": resolved.get("predecessor"),
        "seed_authority_sha256": _sha256(seed_bytes),
        "authority_sha256": _sha256(authority_bytes),
        **facts,
    }
    return (json.dumps(document, indent=1, sort_keys=True) + "\n").encode("utf-8")


def _write_create_only(path: Path, data: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError as exc:
        raise CalendarAuthorityError("OUTPUT_EXISTS", str(path)) from exc


def author(args: argparse.Namespace) -> int:
    resolved = load_resolved_cycle(args.resolved_cycle)
    spec = _read_json(_file(args.spec, "spec"), "spec")
    seed_path = _file(args.seed_authority, "seed authority")
    seed_bytes = seed_path.read_bytes()
    seed = _read_json(seed_path, "seed authority")
    authority, facts = derive_authority(
        resolved=resolved,
        run=args.run,
        spec=spec,
        seed=seed,
        repo_root=args.repo_root,
        hwm_policy=args.hwm_policy,
        contract=args.contract,
        flow_policy=args.flow_policy,
        epoch_generation=args.epoch_generation,
        ledger_generation=args.ledger_generation,
        baseline_generation=args.baseline_generation,
    )
    data = render(authority)
    if args.check:
        if not args.out.is_file() or args.out.read_bytes() != data:
            raise CalendarAuthorityError(
                "DIFFERS", f"{args.out} is not what the run derives"
            )
        print(json.dumps({"authority_sha256": _sha256(data), "check": "ok"}))
        return 0
    receipt_path = args.out.with_name(args.out.name + ".receipt.json")
    if args.out.exists() or receipt_path.exists():
        raise CalendarAuthorityError("OUTPUT_EXISTS", str(args.out))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    _write_create_only(args.out, data)
    _write_create_only(receipt_path, _receipt(resolved, seed_bytes, data, facts))
    print(
        json.dumps(
            {
                "authority_sha256": _sha256(data),
                "authority_evidence_sha256": authority["authority_evidence_sha256"],
                "bound_interval_count": facts["bound_interval_count"],
            }
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser(
        "author", help="derive the authority from a first attempt"
    )
    cmd.add_argument("--resolved-cycle", type=Path, required=True)
    cmd.add_argument("--run", type=Path, required=True, help="first attempt run dir")
    cmd.add_argument("--spec", type=Path, required=True, help="prepared spec JSON")
    cmd.add_argument("--seed-authority", type=Path, required=True)
    cmd.add_argument("--out", type=Path, required=True)
    cmd.add_argument("--check", action="store_true")
    cmd.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    cmd.add_argument("--hwm-policy", default=DEFAULT_HWM_POLICY)
    cmd.add_argument("--contract", default=DEFAULT_CONTRACT)
    cmd.add_argument("--flow-policy", default=DEFAULT_FLOW_POLICY)
    cmd.add_argument("--epoch-generation")
    cmd.add_argument("--ledger-generation")
    cmd.add_argument("--baseline-generation")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return author(args)
    except CalendarAuthorityError as exc:
        print(f"release-hwm-calendar: FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
