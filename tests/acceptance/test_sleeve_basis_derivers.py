"""#624 option B in the two acceptance derivers: declaration-driven, identity without.

Derivation A is exercised row by row on a synthetic ``Output``; derivation B end to end on
the existing synthetic combined toy tree (``test_derive_b_combined``), whose result is
then checked by the validator's own independent restatement (``diff_expected``).

SYNTHETIC throughout: invented accounts, strategies and values.
"""

from __future__ import annotations

import copy
import datetime as dt
from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "acceptance"))
from derive_a import basis, cli, derive  # noqa: E402
from derive_a.sources import InputError  # noqa: E402
import sleeve_basis_synthetic as synth  # noqa: E402
import test_derive_b_combined as combined  # noqa: E402

from acceptance.validate import sleeve_basis as validator  # noqa: E402

DECLARED = synth.DECLARED
CUTOFF = dt.date(2026, 2, 6)


def _declaration(tmp_path: Path, strategy: str = DECLARED) -> Path:
    path = tmp_path / "strategy-basis-declaration.json"
    path.write_bytes(synth.declaration_bytes(strategy))
    return path


# ------------------------------------------------------------------ derivation A


def _output(*, exact: bool = False) -> derive.Output:
    out = derive.Output()
    inputs = ["ledger.json"]
    for strategy, twr, mwr, capital in (
        (DECLARED, "0.050000000000", "0.300000000000", "175.00"),
        ("synthetic-beta", "0.100000000000", "0.110000000000", "3000.00"),
        ("synthetic-gamma", "0.200000000000", "0.210000000000", "5000.00"),
    ):
        out.metric("twr.strategy", strategy, "ITD", CUTOFF, twr, "ratio", inputs)
        out.metric("mwr.strategy", strategy, "ITD", CUTOFF, mwr, "ratio", inputs)
        out.metric(
            "capital.strategy.cutoff", strategy, None, CUTOFF, capital, "USD", inputs
        )
    out.metric(
        "capital.portfolio.systematic.cutoff",
        "portfolio",
        None,
        CUTOFF,
        "8175.01" if exact else "8175.00",
        "USD",
        inputs,
        ["sum of 3 active strategies"],
    )
    for strategy, capital in (
        (DECLARED, "175.00"),
        ("synthetic-beta", "3000.00"),
        ("synthetic-gamma", "5000.00"),
    ):
        out.metric(
            "weight.strategy.cutoff",
            strategy,
            None,
            CUTOFF,
            format(Decimal(capital) / Decimal("8175"), ".12f"),
            "ratio",
            inputs,
        )
    out.metric(
        "capital.account.cutoff", synth.PARENT, None, CUTOFF, "175.00", "USD", inputs
    )
    out.missing("capital.portfolio.liquid.cutoff", "portfolio", None, "no reserve")
    if exact:
        out.exact["capital:" + DECLARED] = Decimal("175.004")
        out.exact["capital:synthetic-beta"] = Decimal("3000.000")
        out.exact["capital:synthetic-gamma"] = Decimal("5000.002")
        out.exact["systematic"] = Decimal("8175.006")
    return out


def _sleeve_document(*, open_epochs: int = 1, with_twr: bool = True) -> dict[str, Any]:
    epoch = {
        "account_id": synth.SLEEVE,
        "strategy_id": DECLARED,
        "effective_start": "2026-02-02",
        "effective_end": None,
        "closing": "1575",
        "nav": {"2026-02-02": "992", "2026-02-06": "1575"},
    }
    epochs = [copy.deepcopy(epoch) for _ in range(open_epochs)]
    for number, item in enumerate(epochs):
        if open_epochs > 1:
            item["account_id"] += f"-{number}"
    return {
        "sleeve_derivation": {
            "epochs": epochs,
            "inputs": {"facts": {"path": "sleeve/facts.json", "sha256": "00"}},
        },
        "metrics": (
            [
                {
                    "metric_id": "twr.account",
                    "entity": epochs[0]["account_id"] if epochs else synth.SLEEVE,
                    "window": "ITD",
                    "as_of": "2026-02-06",
                    "value": "0.088737000000",
                    "unit": "ratio",
                    "inputs_used": ["sleeve/facts.json"],
                    "notes": "",
                }
            ]
            if with_twr
            else []
        ),
        "not_derivable": (
            []
            if with_twr
            else [
                {
                    "metric_id": "twr.account",
                    "entity": synth.SLEEVE,
                    "reason": "R3 TWR undefined",
                }
            ]
        ),
    }


def _declaration_a(tmp_path: Path, strategy: str = DECLARED) -> basis.Declaration:
    path = _declaration(tmp_path, strategy)
    return basis.load_declaration(path.read_bytes(), path.as_posix())


def _key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (row["metric_id"], row["entity"], row.get("window") or "")


def _by_key(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {_key(row): row for row in rows}


def test_a_restates_exactly_the_four_row_families_of_the_declared_strategy(
    tmp_path: Path,
) -> None:
    out = _output()
    before = copy.deepcopy(out.metrics)
    moved = basis.restate(out, _declaration_a(tmp_path), _sleeve_document())
    after, old = _by_key(out.metrics), _by_key(before)
    assert after[("twr.strategy", DECLARED, "ITD")]["value"] == "0.088737000000"
    assert after[("capital.strategy.cutoff", DECLARED, "")]["value"] == "1575.00"
    assert after[("capital.portfolio.systematic.cutoff", "portfolio", "")]["value"] == (
        "9575.00"
    )
    weight = after[("weight.strategy.cutoff", DECLARED, "")]["value"]
    assert Decimal(weight) == (Decimal(1575) / Decimal(9575)).quantize(Decimal("1e-12"))
    assert sorted(moved) == sorted(
        [
            f"twr.strategy|{DECLARED}",
            f"capital.strategy.cutoff|{DECLARED}",
            "capital.portfolio.systematic.cutoff|portfolio",
            *(
                f"weight.strategy.cutoff|{s}"
                for s in (DECLARED, "synthetic-beta", "synthetic-gamma")
            ),
        ]
    )
    moved_keys = {k for k in old if after[k] != old[k]}
    assert {k[0] for k in moved_keys} == {
        "twr.strategy",
        "capital.strategy.cutoff",
        "capital.portfolio.systematic.cutoff",
        "weight.strategy.cutoff",
    }
    # every other strategy's return and capital, every mwr and every other row is untouched
    for key in old:
        if key[0] in ("mwr.strategy", "capital.account.cutoff") or key[1] != DECLARED:
            if (
                key[0] != "weight.strategy.cutoff"
                and key[0] != "capital.portfolio.systematic.cutoff"
            ):
                assert after[key] == old[key], key
    assert out.not_derivable == [
        {
            "metric_id": "capital.portfolio.liquid.cutoff",
            "entity": "portfolio",
            "window": None,
            "reason": "no reserve",
        }
    ]
    assert "synthetic-alpha" in after[("twr.strategy", DECLARED, "ITD")]["notes"]
    assert (
        "sleeve/facts.json" in after[("twr.strategy", DECLARED, "ITD")]["inputs_used"]
    )


def test_a_the_systematic_total_follows_the_unrounded_values_when_they_were_kept(
    tmp_path: Path,
) -> None:
    exact = _output(exact=True)
    basis.restate(exact, _declaration_a(tmp_path), _sleeve_document())
    # 8175.006 - 175.004 + 1575 = 9575.002: a cents-only restatement says 9575.01
    assert (
        _by_key(exact.metrics)[
            ("capital.portfolio.systematic.cutoff", "portfolio", "")
        ]["value"]
        == "9575.00"
    )
    rounded = _output(exact=False)
    basis.restate(rounded, _declaration_a(tmp_path), _sleeve_document())
    assert (
        _by_key(rounded.metrics)[
            ("capital.portfolio.systematic.cutoff", "portfolio", "")
        ]["value"]
        == "9575.00"
    )
    cents = _output(exact=True)
    cents.exact.clear()
    basis.restate(cents, _declaration_a(tmp_path), _sleeve_document())
    assert (
        _by_key(cents.metrics)[
            ("capital.portfolio.systematic.cutoff", "portfolio", "")
        ]["value"]
        == "9575.01"
    )


@pytest.mark.parametrize("open_epochs", [0, 2])
def test_a_declared_strategy_without_exactly_one_open_epoch_is_refused(
    tmp_path: Path, open_epochs: int
) -> None:
    with pytest.raises(InputError, match="exactly one is required"):
        basis.restate(
            _output(),
            _declaration_a(tmp_path),
            _sleeve_document(open_epochs=open_epochs),
        )


def test_a_declared_strategy_the_sleeves_do_not_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="0 derived sleeve epochs"):
        basis.restate(
            _output(), _declaration_a(tmp_path, "synthetic-beta"), _sleeve_document()
        )


def test_a_an_epoch_without_a_return_makes_the_strategy_return_not_derivable(
    tmp_path: Path,
) -> None:
    out = _output()
    basis.restate(out, _declaration_a(tmp_path), _sleeve_document(with_twr=False))
    assert ("twr.strategy", DECLARED, "ITD") not in _by_key(out.metrics)
    reasons = [r for r in out.not_derivable if r["metric_id"] == "twr.strategy"]
    assert len(reasons) == 1 and "R3 TWR undefined" in reasons[0]["reason"]


def test_a_without_a_systematic_row_only_the_strategy_rows_move(tmp_path: Path) -> None:
    out = _output()
    out.metrics = [
        r
        for r in out.metrics
        if r["metric_id"] != "capital.portfolio.systematic.cutoff"
    ]
    moved = basis.restate(out, _declaration_a(tmp_path), _sleeve_document())
    assert sorted(moved) == [
        f"capital.strategy.cutoff|{DECLARED}",
        f"twr.strategy|{DECLARED}",
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema="x"),
        lambda d: d.update(strategies=[]),
        lambda d: d["strategies"][0].update(basis="PARENT_ACCOUNT_LIVE_SERIES"),
        lambda d: d["strategies"].append(dict(d["strategies"][0])),
    ],
)
def test_a_an_unusable_declaration_is_refused(mutate: Any) -> None:
    document = copy.deepcopy(synth.DECLARATION)
    mutate(document)
    with pytest.raises(InputError):
        basis.load_declaration(json.dumps(document).encode(), "d.json")
    with pytest.raises(InputError):
        basis.load_declaration(b"not json", "d.json")


def test_a_a_declaration_without_a_combined_run_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(InputError, match="combined run"):
        derive.derive(
            tmp_path,
            tmp_path,
            cutoff=CUTOFF,
            model="test",
            code_commit="test",
            strategy_basis=_declaration(tmp_path),
        )
    status = cli.main(
        [
            "--qe-root",
            str(tmp_path),
            "--out",
            str(tmp_path / "out.json"),
            "--cutoff",
            CUTOFF.isoformat(),
            "--strategy-basis",
            str(_declaration(tmp_path)),
        ]
    )
    assert status == 2
    assert "combined run" in capsys.readouterr().err
    assert not (tmp_path / "out.json").exists()


def test_a_the_unrounded_values_are_never_serialized() -> None:
    out = _output(exact=True)
    assert "exact" not in json.dumps({"metrics": out.metrics, "x": out.not_derivable})


# ------------------------------------------------------------------ derivation B


def _rows_as_baseline(document: dict[str, Any]) -> dict[str, Any]:
    """A derivation document read as an expected baseline (value as the expected)."""

    def row(source: dict[str, Any]) -> dict[str, Any]:
        return {
            "metric_id": source["metric_id"],
            "entity": source["entity"],
            "window": source.get("window", ""),
            "expected": source["value"],
            "unit": source["unit"],
            "tolerance": "0.01" if source["unit"] == "USD" else "0.00000001",
            "as_of": source["as_of"],
        }

    return {
        "metrics": [row(r) for r in document["metrics"]],
        "not_derivable": [
            {
                "metric_id": r["metric_id"],
                "entity": r["entity"],
                "window": r.get("window"),
            }
            for r in document["not_derivable"]
        ],
    }


def _toy(tmp_path: Path) -> tuple[Any, Path]:
    derivation = combined.load_module("derive_b_basis_toy")
    return derivation, combined._write_combined_toy(derivation, tmp_path)


TOY_STRATEGY = "p123-ai-factor"


def _find(rows: list[dict[str, Any]], metric: str, entity: str) -> dict[str, Any]:
    return next(r for r in rows if r["metric_id"] == metric and r["entity"] == entity)


def test_b_without_a_declaration_nothing_changes(tmp_path: Path) -> None:
    derivation, selection = _toy(tmp_path)
    first = derivation.derive(tmp_path, sleeve_inputs=selection)
    second = derivation.derive(tmp_path, sleeve_inputs=selection, strategy_basis=None)
    assert first == second
    assert "strategy_basis" not in first
    assert "SLEEVE BASIS" not in " ".join(first["convention_notes"])


def test_b_the_declared_strategy_moves_only_for_the_stated_reason(
    tmp_path: Path,
) -> None:
    derivation, selection = _toy(tmp_path)
    declaration = _declaration(tmp_path, TOY_STRATEGY)
    before = derivation.derive(tmp_path, sleeve_inputs=selection)
    after = derivation.derive(
        tmp_path, sleeve_inputs=selection, strategy_basis=declaration
    )
    block = after["strategy_basis"]
    assert block["strategies"] == [TOY_STRATEGY]
    assert block["path"] == declaration.as_posix()
    assert len(block["sha256"]) == 64
    epoch = next(
        r
        for r in before["sleeve_derivation"]["epochs"]
        if r["strategy_id"] == TOY_STRATEGY
    )
    account = epoch["account_id"]
    twr = _find(before["metrics"], "twr.account", account)["value"]
    closing = _find(before["metrics"], "capital.epoch.closing", account)["value"]
    # the four row families
    assert _find(after["metrics"], "twr.strategy", TOY_STRATEGY)["value"] == twr
    assert (
        _find(after["metrics"], "capital.strategy.cutoff", TOY_STRATEGY)["value"]
        == closing
    )
    old_capital = Decimal(
        _find(before["metrics"], "capital.strategy.cutoff", TOY_STRATEGY)["value"]
    )
    old_total = Decimal(
        _find(before["metrics"], "capital.portfolio.systematic.cutoff", "portfolio")[
            "value"
        ]
    )
    new_total = Decimal(
        _find(after["metrics"], "capital.portfolio.systematic.cutoff", "portfolio")[
            "value"
        ]
    )
    assert new_total == old_total - old_capital + Decimal(closing)
    for row in after["metrics"]:
        if row["metric_id"] == "weight.strategy.cutoff":
            capital = Decimal(
                _find(after["metrics"], "capital.strategy.cutoff", row["entity"])[
                    "value"
                ]
            )
            assert abs(Decimal(row["value"]) - capital / new_total) <= Decimal("1e-9")
    # everything else is identical, row by row
    moved = {tuple(item.split("|")) for item in block["restated_rows"]}
    old_rows, new_rows = _by_key(before["metrics"]), _by_key(after["metrics"])
    assert set(old_rows) == set(new_rows)
    for key, old in old_rows.items():
        if key[:2] in {(m, e) for m, e in moved}:
            continue
        assert new_rows[key] == old, key
    assert (
        before["not_derivable"]
        == [r for r in after["not_derivable"] if r["metric_id"] != "twr.strategy"]
        or before["not_derivable"] == after["not_derivable"]
    )
    assert "SLEEVE BASIS" in " ".join(after["convention_notes"])
    # mwr already stood on the sleeve: untouched
    assert _find(after["metrics"], "mwr.strategy", TOY_STRATEGY) == _find(
        before["metrics"], "mwr.strategy", TOY_STRATEGY
    )


def test_b_the_validators_independent_restatement_agrees_with_the_deriver(
    tmp_path: Path,
) -> None:
    derivation, selection = _toy(tmp_path)
    declaration = _declaration(tmp_path, TOY_STRATEGY)
    before = _rows_as_baseline(derivation.derive(tmp_path, sleeve_inputs=selection))
    after = _rows_as_baseline(
        derivation.derive(tmp_path, sleeve_inputs=selection, strategy_basis=declaration)
    )
    declared = {TOY_STRATEGY: "SLEEVE_EPOCH"}
    assert validator.diff_expected(before, after, declared) == []
    # and the same two documents are a finding when no declaration is in force
    codes = {f.code for f in validator.diff_expected(before, after, None)}
    assert codes == {"ROW_CHANGED_WITHOUT_REASON"}


@pytest.mark.parametrize(
    ("strategy", "message"),
    [
        ("volatis-strategy", "0 derived sleeve epochs"),
        ("no-such-strategy", "0 derived"),
    ],
)
def test_b_a_declared_strategy_without_an_open_sleeve_is_refused(
    tmp_path: Path, strategy: str, message: str
) -> None:
    derivation, selection = _toy(tmp_path)
    with pytest.raises(derivation.SleeveInputError, match=message):
        derivation.derive(
            tmp_path,
            sleeve_inputs=selection,
            strategy_basis=_declaration(tmp_path, strategy),
        )


def test_b_a_declaration_without_sleeve_inputs_or_unusable_is_refused(
    tmp_path: Path,
) -> None:
    derivation, selection = _toy(tmp_path)
    with pytest.raises(derivation.SleeveInputError, match="requires --sleeve-inputs"):
        derivation.derive(tmp_path, strategy_basis=_declaration(tmp_path, TOY_STRATEGY))
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"schema": "x", "strategies": []}), encoding="utf-8")
    with pytest.raises(derivation.SleeveInputError, match="schema differs"):
        derivation.derive(tmp_path, sleeve_inputs=selection, strategy_basis=broken)
    with pytest.raises(derivation.SleeveInputError, match="cannot read"):
        derivation.derive(
            tmp_path, sleeve_inputs=selection, strategy_basis=tmp_path / "missing.json"
        )
