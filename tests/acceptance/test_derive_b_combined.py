"""SPEC-COMBINED tests: the effective epoch perimeter in the combined run.

Real-inputs cases run the acceptance derivation over the release inputs
(qe-root plus the pinned acceptance/sleeve input manifests) once per module;
the refusal cases call the perimeter binding directly with mutated declared
manifests.  The toy cases extend the SPEC-HARNESS toy tree with a sleeve on
the baseline strategy so the residual-parent replacement is hand-checkable,
and a golden hash pins the no---sleeve-inputs output bytes to the unmodified
derivation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from datetime import date
from decimal import Decimal, getcontext, setcontext
from pathlib import Path

import pytest

import test_derive_b_sleeves as sleeve_tests

MODULE_PATH = Path(__file__).parents[2] / "acceptance" / "derive_b" / "derive.py"
TREE = Path(__file__).parents[2]
QE_ROOT = TREE / "qe-root"
INPUTS_MANIFEST = TREE.parent / "inputs" / "acceptance-inputs-manifest.json"
SLEEVE_INPUTS = TREE.parent / "inputs" / "sleeve-inputs.json"
R2_ORACLE = QE_ROOT / "evidence" / "releases" / "2026-10-26-r2" / "oracle"

PARENT = "account-1b44c987969b9e47"
REPLACED_EPOCH_ACCOUNT = f"{PARENT}-s2"
DERIVED_ACCOUNTS = [f"{PARENT}-s{i}" for i in range(1, 6)] + [
    "account-58e75e8d952fa1b8-crisis",
    "account-58e75e8d952fa1b8-singleindex",
]
EPOCH_KEYED = ("twr.account", "mwr.account", "flows.account.net", "flows.account.count")
ACCOUNT_KEYED = ("capital.account.cutoff", "date.source_last_observation")

# sha256 of the UNMODIFIED derive() output on the minimal no-sleeve fixture
# below, rendered as main() renders it (json.dumps indent=2 + "\n").
NO_SLEEVE_GOLDEN_SHA256 = (
    "6aa66f8182a370e07291c5969488235a8a1a8e673ebe4084406747984d7f2fe6"
)


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their annotations through sys.modules[__module__]
    sys.modules[spec.name] = module
    # derive.py sets the decimal precision at import; keep the ambient
    # context so the load does not change other tests' arithmetic.
    ambient = getcontext().copy()
    try:
        spec.loader.exec_module(module)
    finally:
        setcontext(ambient)
    return module


def find_row(rows, metric_id, entity):
    return next(
        (
            row
            for row in rows
            if row["metric_id"] == metric_id and row["entity"] == entity
        ),
        None,
    )


@pytest.fixture(scope="module")
def derivation():
    return load_module("derive_b_combined")


@pytest.fixture(scope="module")
def real_inputs():
    if not R2_ORACLE.exists():
        pytest.skip("real R2 inputs are only present in the seat tree")


@pytest.fixture(scope="module")
def real_crosswalk(real_inputs):
    return json.loads((R2_ORACLE / "current-epoch-crosswalk.json").read_text("utf-8"))


@pytest.fixture(scope="module")
def declared_manifest(real_inputs):
    return json.loads((R2_ORACLE / "declared-cutoff-manifest.json").read_text("utf-8"))


@pytest.fixture(scope="module")
def perimeter_inputs(derivation, real_inputs):
    """The derived epochs of the real sleeve derivation, with their parents."""
    block = derivation._derive_sleeve_block(QE_ROOT, SLEEVE_INPUTS)
    records = block["epoch_records"]
    epochs = [derivation.epoch_from_record(record) for record in records]
    parents = {
        str(record["account_id"]): str(record["parent_account_id"])
        for record in records
    }
    return epochs, parents


@pytest.fixture(scope="module")
def combined_output(derivation, real_inputs):
    manifest = derivation.load_inputs_manifest(INPUTS_MANIFEST)
    return derivation.derive(QE_ROOT, manifest, sleeve_inputs=SLEEVE_INPUTS)


def mutated_manifest(declared_manifest, mutate):
    document = json.loads(json.dumps(declared_manifest))
    mutate(document["rows"])
    return document


def epoch_key(account_id, strategy_id, start, end):
    return (account_id, strategy_id, start, end)


# ------------------------------------------------------- perimeter acceptance


def test_perimeter_accepts_the_real_combined_inputs(
    derivation, declared_manifest, real_crosswalk, perimeter_inputs
):
    """The real manifest equals the effective perimeter exactly; the one drop
    is the residual IBKR parent's baseline epoch, and the declared closure of
    the liquidated tt-portfolio epoch still binds."""
    epochs, parents = perimeter_inputs
    closures, dropped = derivation.effective_perimeter_closures(
        declared_manifest, real_crosswalk["rows"], epochs, parents
    )
    assert dropped == {epoch_key(PARENT, "p123-ai-factor", "2026-01-22", None)}
    closure = closures[("account-702c489650e59956", date(2025, 11, 25))]
    assert closure["funded_end"] == date(2026, 6, 21)
    assert closure["post_liquidation_income"] == Decimal("59.56")


def test_real_combined_run_succeeds(combined_output):
    assert combined_output["metrics"]
    assert "sleeve_derivation" in combined_output


# ---------------------------------------------------------- perimeter refusals


def test_perimeter_refuses_an_extra_declared_row(
    derivation, declared_manifest, real_crosswalk, perimeter_inputs
):
    epochs, parents = perimeter_inputs
    document = mutated_manifest(
        declared_manifest,
        lambda rows: rows.append(
            {
                "account_id": "account-bogus",
                "strategy_id": "qsi",
                "effective_start": "2026-01-05",
                "effective_end": None,
            }
        ),
    )
    with pytest.raises(
        derivation.SleeveInputError, match="declared-only epoch account-bogus"
    ):
        derivation.effective_perimeter_closures(
            document, real_crosswalk["rows"], epochs, parents
        )


def test_perimeter_refuses_a_missing_derived_row(
    derivation, declared_manifest, real_crosswalk, perimeter_inputs
):
    epochs, parents = perimeter_inputs
    document = mutated_manifest(
        declared_manifest,
        lambda rows: rows.remove(
            next(row for row in rows if row["account_id"] == f"{PARENT}-s3")
        ),
    )
    with pytest.raises(
        derivation.SleeveInputError,
        match=f"missing declared epoch {PARENT}-s3",
    ):
        derivation.effective_perimeter_closures(
            document, real_crosswalk["rows"], epochs, parents
        )


def test_perimeter_refuses_a_kept_epoch_of_a_residual_parent(
    derivation, declared_manifest, real_crosswalk, perimeter_inputs
):
    epochs, parents = perimeter_inputs
    document = mutated_manifest(
        declared_manifest,
        lambda rows: rows.append(
            {
                "account_id": PARENT,
                "strategy_id": "p123-ai-factor",
                "effective_start": "2026-01-22",
                "effective_end": None,
            }
        ),
    )
    with pytest.raises(derivation.SleeveInputError, match="keeps its baseline epoch"):
        derivation.effective_perimeter_closures(
            document, real_crosswalk["rows"], epochs, parents
        )


def test_perimeter_refuses_a_dropped_epoch_of_a_non_parent_account(
    derivation, declared_manifest, real_crosswalk, perimeter_inputs
):
    epochs, parents = perimeter_inputs
    document = mutated_manifest(
        declared_manifest,
        lambda rows: rows.remove(
            next(row for row in rows if row["account_id"] == "account-cff10c44aedcf5e8")
        ),
    )
    with pytest.raises(
        derivation.SleeveInputError, match="is not a parent of derived epochs"
    ):
        derivation.effective_perimeter_closures(
            document, real_crosswalk["rows"], epochs, parents
        )


def test_perimeter_refuses_a_partial_drop(derivation):
    """A parent that drops one baseline epoch must drop all of them."""
    crosswalk_rows = [
        {
            "account_id": "account-p",
            "strategy_id": strategy_id,
            "effective_start": "2026-01-22",
            "effective_end": None,
            "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
        }
        for strategy_id in ("p123-ai-factor", "p123-eu")
    ]
    epoch = derivation.Epoch(
        "account-p-s1",
        "s1-strat",
        date(2026, 2, 1),
        None,
        [(date(2026, 2, 1), Decimal("100"))],
        [],
    )
    manifest = {
        "rows": [
            {
                "account_id": "account-p",
                "strategy_id": "p123-ai-factor",
                "effective_start": "2026-01-22",
                "effective_end": None,
            },
            {
                "account_id": "account-p-s1",
                "strategy_id": "s1-strat",
                "effective_start": "2026-02-01",
                "effective_end": None,
            },
        ]
    }
    with pytest.raises(
        derivation.SleeveInputError, match="never stays partly a strategy"
    ):
        derivation.effective_perimeter_closures(
            manifest, crosswalk_rows, [epoch], {"account-p-s1": "account-p"}
        )


def test_perimeter_refuses_two_derived_epochs_one_identity(derivation):
    def epoch(end):
        return derivation.Epoch(
            "account-p-s1",
            "s1-strat",
            date(2026, 2, 1),
            end,
            [(date(2026, 2, 1), Decimal("100"))],
            [],
        )

    with pytest.raises(
        derivation.SleeveInputError,
        match="two derived epochs claim one account/strategy/start",
    ):
        derivation.effective_perimeter_closures(
            {"rows": []},
            [],
            [epoch(None), epoch(date(2026, 3, 1))],
            {"account-p-s1": "account-p"},
        )


def test_perimeter_refuses_a_repeated_declared_row(
    derivation, declared_manifest, real_crosswalk, perimeter_inputs
):
    """R4: downstream consumers require one row per epoch.  A declared
    manifest repeating an ordinary epoch row (the same four-part identity,
    nothing distinguishing the copies) would collapse silently in the
    declared set, so it is refused, naming the duplicated identity."""
    epochs, parents = perimeter_inputs
    repeated = declared_manifest["rows"][0]
    document = mutated_manifest(
        declared_manifest, lambda rows: rows.append(dict(repeated))
    )
    identity = derivation._format_epoch_key(
        derivation._epoch_identity_key(
            repeated["account_id"],
            repeated["strategy_id"],
            repeated["effective_start"],
            repeated.get("effective_end"),
        )
    )
    with pytest.raises(
        derivation.SleeveInputError,
        match=re.escape(
            f"declared account-strategy manifest repeats epoch identity {identity}"
        ),
    ):
        derivation.effective_perimeter_closures(
            document, real_crosswalk["rows"], epochs, parents
        )


def test_cli_perimeter_refusal_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """End to end: a declared manifest keeping the residual parent's epoch is
    refused with exit status 2 and an 'input error:' message."""
    derivation = load_module("derive_b_combined_cli")
    paths = sleeve_tests.write_toy_tree(tmp_path)
    tag_map_path = tmp_path / paths["tag_map"]
    tag_map = json.loads(tag_map_path.read_text(encoding="utf-8"))
    # The sleeve carries the baseline strategy: account-ibkr is a residual
    # parent and its baseline epoch must be dropped by the declared manifest.
    tag_map["parents"][0]["sleeves"][0]["strategy_id"] = "p123-ai-factor"
    tag_map_path.write_text(json.dumps(tag_map), encoding="utf-8")
    selection = sleeve_tests.write_sleeve_inputs(tmp_path, paths)

    block = derivation._derive_sleeve_block(tmp_path, selection)
    declared_rows = [
        {
            "account_id": "account-qb",
            "strategy_id": "volatis-strategy",
            "effective_start": "2024-10-15",
            "effective_end": None,
        },
        *(
            {
                "account_id": record["account_id"],
                "strategy_id": record["strategy_id"],
                "effective_start": record["effective_start"],
                "effective_end": record["effective_end"],
            }
            for record in block["epoch_records"]
        ),
        # the refusal trigger: the residual parent keeps its baseline epoch
        {
            "account_id": "account-ibkr",
            "strategy_id": "p123-ai-factor",
            "effective_start": "2026-01-01",
            "effective_end": None,
        },
    ]
    ledger = {
        "as_of_date": "2026-09-17",
        "nav_observations": [],
        "flows": [],
        "capital_anchors": [],
    }
    crosswalk = {
        "as_of_date": "2026-09-17",
        "calculation_cutoff_date": "2026-09-17",
        "rows": [
            {
                "account_id": "account-ibkr",
                "strategy_id": "p123-ai-factor",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2026-01-01",
                "effective_end": None,
            },
            {
                "account_id": "account-qb",
                "strategy_id": "volatis-strategy",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2024-10-15",
                "effective_end": None,
            },
        ],
    }
    files = {
        "provider-ledger.json": json.dumps(ledger),
        "account-strategy-current-epoch-crosswalk.json": json.dumps(crosswalk),
        "declared-cutoff-manifest.json": json.dumps(
            {"calculation_cutoff_date": "2026-09-17", "rows": declared_rows}
        ),
        # Declared for the manifest roles but never read: the perimeter
        # refusal fires before any raw provider capture is opened.
        "dashboard_Volatis_Strategy_pricehistory.csv": "placeholder\n",
        "account_activity.csv": "placeholder\n",
        "nav_daily.csv": "placeholder\n",
        "wallet_deposits.csv": "placeholder\n",
        "wallet_bills.csv": "placeholder\n",
        "yfinance-history.json": "placeholder\n",
    }
    entries = []
    for name, text in files.items():
        path = tmp_path / "pinned" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        entry = {
            "path": f"pinned/{name}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
        if name == "declared-cutoff-manifest.json":
            entry["family"] = "declared_cutoff_date"
        entries.append(entry)
    inputs_manifest = tmp_path / "inputs-manifest.json"
    inputs_manifest.write_text(
        json.dumps(
            {
                "schema": "qe-cp-acceptance-inputs/2",
                "cutoff": "2026-09-17",
                "inputs": entries,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "derive.py",
            "--qe-root",
            str(tmp_path),
            "--out",
            str(tmp_path / "out.json"),
            "--inputs",
            str(inputs_manifest),
            "--sleeve-inputs",
            str(selection),
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        derivation.main()
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "input error:" in err
    assert "keeps its baseline epoch" in err


# -------------------------------------------------------------- M1 (real run)


def test_m1_residual_parent_has_no_epoch_keyed_rows(combined_output):
    for metric_id in EPOCH_KEYED:
        assert find_row(combined_output["metrics"], metric_id, PARENT) is None
        assert find_row(combined_output["not_derivable"], metric_id, PARENT) is None


def test_m1_residual_parent_keeps_account_keyed_rows(combined_output):
    for metric_id in ACCOUNT_KEYED:
        assert find_row(combined_output["metrics"], metric_id, PARENT) is not None


def test_m1_derived_accounts_have_epoch_keyed_rows(combined_output):
    for account in DERIVED_ACCOUNTS:
        for metric_id in EPOCH_KEYED:
            row = find_row(combined_output["metrics"], metric_id, account)
            assert row is not None, (metric_id, account)
            assert row["window"] == "ITD"


# ---------------------------------------------------------- M2/M3 (real run)


def test_m2_replaced_strategy_mwr_equals_derived_epoch_mwr(combined_output):
    """p123-ai-factor's only perimeter epoch is the derived s2 epoch."""
    strategy = find_row(combined_output["metrics"], "mwr.strategy", "p123-ai-factor")
    epoch = find_row(combined_output["metrics"], "mwr.account", REPLACED_EPOCH_ACCOUNT)
    assert strategy is not None and epoch is not None
    assert strategy["value"] == epoch["value"]
    assert strategy["window"] == "ITD" and strategy["as_of"] == "2026-09-17"
    # the figure is the manifest's own declared s2 XIRR (0.2509173983014...)
    assert strategy["value"] == "0.2509173983"


def test_m2_baseline_strategies_mwr_unchanged_path(combined_output):
    """Every other active strategy still reports a baseline-path MWR."""
    for strategy_id in (
        "grizzlybears-vtma",
        "p123-eu",
        "qsi",
        "robuxio-crypto",
        "tt-portfolio",
        "volatis-strategy",
    ):
        row = find_row(combined_output["metrics"], "mwr.strategy", strategy_id)
        assert row is not None, strategy_id
        assert row["window"] == "ITD"


def test_m3_replaced_strategy_keeps_its_other_strategy_rows(combined_output):
    twr = find_row(combined_output["metrics"], "twr.strategy", "p123-ai-factor")
    assert twr is not None and twr["window"] == "ITD"
    # the canonical account-live series is the residual parent's: the
    # strategy capital is the parent's account-level cutoff capital
    capital = find_row(
        combined_output["metrics"], "capital.strategy.cutoff", "p123-ai-factor"
    )
    parent_capital = find_row(
        combined_output["metrics"], "capital.account.cutoff", PARENT
    )
    assert capital is not None and parent_capital is not None
    assert capital["value"] == parent_capital["value"]
    weight = find_row(
        combined_output["metrics"], "weight.strategy.cutoff", "p123-ai-factor"
    )
    assert weight is not None


def test_m4_universe_rows_unchanged(combined_output):
    ids = find_row(
        combined_output["metrics"], "universe.active_strategy_ids", "portfolio"
    )
    count = find_row(
        combined_output["metrics"], "universe.active_strategy_count", "portfolio"
    )
    assert ids is not None and count is not None
    assert ids["value"] == [
        "grizzlybears-vtma",
        "p123-ai-factor",
        "p123-eu",
        "qsi",
        "robuxio-crypto",
        "tt-portfolio",
        "volatis-strategy",
    ]
    assert count["value"] == "7"


# --------------------------------------------------------------- toy M1/M2/M3


def _write_combined_toy(derivation, root: Path) -> Path:
    """Baseline side plus the SPEC-HARNESS toy tree, with toy sleeve s1
    carrying the baseline strategy so account-ibkr is a residual parent whose
    only perimeter epoch is the derived one.  Returns the sleeve-inputs path.
    The parent's ledger NAV falls 100 -> 90 so the replaced mwr.strategy is
    distinguishable from the parent series' own money-weighted return."""
    derivation.LEDGER_PATH = Path("ledger.json")
    derivation.CROSSWALK_PATH = Path("crosswalk.json")
    ledger = {
        "as_of_date": "2026-09-17",
        "nav_observations": [
            {
                "account_id": "account-ibkr",
                "as_of_date": "2026-01-22",
                "nav": {"status": "AVAILABLE", "currency": "USD", "value": "100"},
                "mapping_status": "MAPPED",
            },
            {
                "account_id": "account-ibkr",
                "as_of_date": "2026-09-17",
                "nav": {"status": "AVAILABLE", "currency": "USD", "value": "90"},
                "mapping_status": "MAPPED",
            },
            {
                "account_id": "account-qb",
                "as_of_date": "2026-09-16",
                "nav": {"status": "AVAILABLE", "currency": "USD", "value": "100"},
                "mapping_status": "MAPPED",
            },
            {
                "account_id": "account-qb",
                "as_of_date": "2026-09-17",
                "nav": {"status": "AVAILABLE", "currency": "USD", "value": "100"},
                "mapping_status": "MAPPED",
            },
        ],
        "flows": [],
        "capital_anchors": [],
    }
    crosswalk = {
        "as_of_date": "2026-09-17",
        "calculation_cutoff_date": "2026-09-17",
        "rows": [
            {
                "account_id": "account-ibkr",
                "strategy_id": "p123-ai-factor",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2026-01-01",
                "effective_end": None,
            },
            {
                "account_id": "account-qb",
                "strategy_id": "volatis-strategy",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2024-10-15",
                "effective_end": None,
            },
        ],
    }
    (root / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    (root / "crosswalk.json").write_text(json.dumps(crosswalk), encoding="utf-8")
    paths = sleeve_tests.write_toy_tree(root)
    tag_map_path = root / paths["tag_map"]
    tag_map = json.loads(tag_map_path.read_text(encoding="utf-8"))
    tag_map["parents"][0]["sleeves"][0]["strategy_id"] = "p123-ai-factor"
    tag_map_path.write_text(json.dumps(tag_map), encoding="utf-8")
    return sleeve_tests.write_sleeve_inputs(root, paths)


def test_toy_residual_parent_epoch_keyed_rows_move_to_the_derived_epoch(tmp_path):
    """M1 toy: the residual parent keeps only its account-keyed rows; the
    derived epoch account carries the four epoch-keyed rows."""
    derivation = load_module("derive_b_combined_toy1")
    selection = _write_combined_toy(derivation, tmp_path)

    extended = derivation.derive(tmp_path, sleeve_inputs=selection)

    for metric_id in EPOCH_KEYED:
        assert find_row(extended["metrics"], metric_id, "account-ibkr") is None
        assert find_row(extended["not_derivable"], metric_id, "account-ibkr") is None
        assert find_row(extended["metrics"], metric_id, "account-ibkr-s1") is not None
    for metric_id in ACCOUNT_KEYED:
        assert find_row(extended["metrics"], metric_id, "account-ibkr") is not None


def test_toy_strategy_mwr_pools_the_derived_epoch(tmp_path):
    """M2 toy: the replaced strategy's mwr.strategy is the derived epoch's
    mwr.account, not the parent series' own money-weighted return."""
    derivation = load_module("derive_b_combined_toy2")
    selection = _write_combined_toy(derivation, tmp_path)

    plain = derivation.derive(tmp_path)
    extended = derivation.derive(tmp_path, sleeve_inputs=selection)

    plain_mwr = find_row(plain["metrics"], "mwr.strategy", "p123-ai-factor")
    pooled = find_row(extended["metrics"], "mwr.strategy", "p123-ai-factor")
    epoch_mwr = find_row(extended["metrics"], "mwr.account", "account-ibkr-s1")
    assert plain_mwr is not None and pooled is not None and epoch_mwr is not None
    # the parent series fell 100 -> 90 over eight months: its own MWR is
    # negative, the pooled strategy MWR is the derived epoch's (flat) figure
    assert Decimal(plain_mwr["value"]) < 0
    assert pooled["value"] == epoch_mwr["value"]
    assert pooled["value"] != plain_mwr["value"]


def test_toy_other_strategy_rows_are_unchanged(tmp_path):
    """M3/M4 toy: twr/capital/weight of the replaced strategy and the universe
    rows are byte-identical with and without the sleeve section, as is the
    baseline strategy's mwr.strategy."""
    derivation = load_module("derive_b_combined_toy3")
    selection = _write_combined_toy(derivation, tmp_path)

    plain = derivation.derive(tmp_path)
    extended = derivation.derive(tmp_path, sleeve_inputs=selection)

    for metric_id in (
        "twr.strategy",
        "capital.strategy.cutoff",
        "weight.strategy.cutoff",
    ):
        assert find_row(plain["metrics"], metric_id, "p123-ai-factor") == find_row(
            extended["metrics"], metric_id, "p123-ai-factor"
        )
    assert find_row(plain["metrics"], "mwr.strategy", "volatis-strategy") == find_row(
        extended["metrics"], "mwr.strategy", "volatis-strategy"
    )
    for metric_id in ("universe.active_strategy_ids", "universe.active_strategy_count"):
        assert find_row(plain["metrics"], metric_id, "portfolio") == find_row(
            extended["metrics"], metric_id, "portfolio"
        )


# ------------------------------------------- no --sleeve-inputs: golden bytes


def test_without_sleeve_inputs_the_output_bytes_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The output without --sleeve-inputs is byte-for-byte the unmodified
    derivation's output on the same fixture (golden sha256 captured from the
    pre-change code)."""
    derivation = load_module("derive_b_combined_golden")
    derivation.LEDGER_PATH = Path("ledger.json")
    derivation.CROSSWALK_PATH = Path("crosswalk.json")
    # the golden digest was captured outside a git checkout
    monkeypatch.setattr(
        derivation, "code_commit", lambda: "unavailable:no-git-metadata"
    )
    ledger = {
        "as_of_date": "2026-09-17",
        "nav_observations": [
            {
                "account_id": "account-ibkr",
                "as_of_date": "2026-09-16",
                "nav": {"status": "AVAILABLE", "currency": "USD", "value": "100"},
                "mapping_status": "MAPPED",
            },
            {
                "account_id": "account-ibkr",
                "as_of_date": "2026-09-17",
                "nav": {"status": "AVAILABLE", "currency": "USD", "value": "100"},
                "mapping_status": "MAPPED",
            },
        ],
        "flows": [],
        "capital_anchors": [],
    }
    crosswalk = {
        "as_of_date": "2026-09-17",
        "calculation_cutoff_date": "2026-09-17",
        "rows": [
            {
                "account_id": "account-ibkr",
                "strategy_id": "p123-ai-factor",
                "product_epoch_disposition": "INCLUDED_PRODUCT_EPOCH",
                "effective_start": "2026-01-01",
                "effective_end": None,
            }
        ],
    }
    (tmp_path / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    (tmp_path / "crosswalk.json").write_text(json.dumps(crosswalk), encoding="utf-8")

    output = derivation.derive(tmp_path)
    rendered = json.dumps(output, indent=2, ensure_ascii=False) + "\n"

    assert (
        hashlib.sha256(rendered.encode("utf-8")).hexdigest() == NO_SLEEVE_GOLDEN_SHA256
    )
