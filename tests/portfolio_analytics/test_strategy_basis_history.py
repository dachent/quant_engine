"""The layered history carries the declared strategy's sleeve live segment (#624).

A ``strategy-basis-declaration/1`` plus the sleeve facts replace the parent account's live
rows of each declared strategy with the open derived sleeve's own return path, in the
generation builder and, independently, in its verifier.  Everything else, and every
generation built without a declaration, is unchanged.  All inputs are synthetic.
"""

# ruff: noqa: F811  (the shared fixtures are imported, then used as parameters)

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
import pytest

import nested_strategy_tag_map
import strategy_basis
from test_portfolio_layered_history import (
    _artifact,
    _core_generation_manifest,
    _matrixport_fx_boundary_fixture,
    _pointer,
    _refresh_materialized_segment_identities,
    _restore_retained_echo_correction,
    _synthetic_current_admission,
    _update_correction_generations,
    _update_source_generation,
    _write_json,
    synthetic_history_authority,  # noqa: F401
    target_series,
)

AI = "p123-ai-factor"
PARENT = "synthetic-account-01"
PROVIDER = "synthetic"
BOUND_GENERATION = "7" * 64
ACCOUNT = f"{PARENT}-s2"
DECLARATION = {
    "schema": "strategy-basis-declaration/1",
    "strategies": [{"strategy_id": AI, "basis": "SLEEVE_EPOCH"}],
}
# sha256 of every output of the history generation built from the synthetic fixture by the
# unmodified main (84b2671e) with no declaration; the switch off must reproduce them
MAIN_OFF_DIGESTS: dict[str, str] = {
    "coverage.json": "1e7ec390f52b2f90a2f06b8b2a93b4d80243ccd0549197d3b92bd2310510a433",
    "excluded.csv": "8eee0f630ee66b2280884623c71acc414e72665c44869949cc203b20a5807360",
    "live-only.csv": "1bb846d9ce46e553449adedb485ff5e84451446c9b841cf749a715abeb270e15",
    "live-plus-backtest.csv": "1bb846d9ce46e553449adedb485ff5e84451446c9b841cf749a715abeb270e15",
    "returns.csv": "1bb846d9ce46e553449adedb485ff5e84451446c9b841cf749a715abeb270e15",
    "segments.csv": "eabb05df608be6369ebc98ee6e4e5dd56b1baf3a769e2079715f7eba999a8bf9",
}


def _facts(*, open_sleeve: bool = True) -> dict[str, Any]:
    days = [
        ("2026-01-05", "100"),
        ("2026-01-06", "101"),
        ("2026-01-07", "103.02"),
        ("2026-01-08", "102"),
        ("2026-01-09", "105"),
    ]
    rows: list[dict[str, Any]] = [
        {"day": day, "event": "", "nav_end": nav, "nav_end_after_alloc": nav}
        for day, nav in days
    ]
    if not open_sleeve:
        rows[-1]["event"] = "EXIT"
    return {
        "schema_version": 1,
        "artifact_id": "sleeve-account-facts",
        "parent_account_id": PARENT,
        "provider_id": PROVIDER,
        "bindings": {
            "parent_ledger_generation_id": BOUND_GENERATION,
            "tag_map_sha256": _tag_map_digest(_tag_map()),
        },
        # the unallocated cash and margin bucket the lens lane label reads (branch L)
        "perimeter_account": {
            "account_id": PARENT + "-unallocated",
            "opening_nav": "0",
            "days": [
                {
                    "day": day,
                    "nav_end": "-20",
                    "external_flow": "0",
                    "internal_flow_net": "0",
                    "residual_pnl": "0",
                }
                for day, _nav in days
            ],
        },
        "sleeves": [
            {
                "sleeve_id": "s2",
                "account_id": ACCOUNT,
                "strategy_id": AI,
                "first_day": days[0][0],
                "last_day": days[-1][0],
                "days": rows,
            }
        ],
        "flows": [
            {
                "pair_id": "2026-01-05:0001",
                "account_id": ACCOUNT,
                "class": "INTERNAL_IN",
                "flow_type": "TRANSFER:INTERNAL:SLEEVE",
                "event_date": "2026-01-05",
                "nav_recognition_date": "2026-01-05",
                "amount": "100",
            }
        ],
    }


def _bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _tag_map_digest(raw: bytes) -> str:
    return str(nested_strategy_tag_map.validate_tag_map(raw).sha256)


def _tag_map(parent: str = PARENT) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "artifact_id": "nested-strategy-tag-map",
            "as_of_date": "2026-01-10",
            "parents": [
                {
                    "parent_account_id": parent,
                    "provider_id": PROVIDER,
                    "tag_field": "orderReference",
                    "tag_normalization": "NONE",
                    "sleeves": [
                        {
                            "sleeve_id": "s2",
                            "strategy_id": AI,
                            "label": "Sleeve two",
                            "effective_start": "2026-01-05",
                            "effective_end": None,
                            "tags": [{"match": "exact", "pattern": "T2"}],
                        }
                    ],
                    "unmatched": {"mode": "BLOCK"},
                }
            ],
        }
    ).encode()


def _bind_ledger(world: dict[str, Any], rows: object) -> None:
    """Record ``provider_generations`` in the selected ledger catalog and re-bind it."""

    fixture = world["fixture"]
    catalog_path = fixture["provider_ledger_catalog_path"]
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if rows is None:
        catalog.pop("provider_generations", None)
    else:
        catalog["provider_generations"] = rows
    _write_json(catalog_path, catalog)
    manifest_path = fixture["provider_ledger_manifest_path"]
    previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts: list[dict[str, object]] = []
    for item in previous["artifacts"]:
        if item["artifact_id"] == "normalized/provider-ledger.json":
            artifacts.append(_artifact(catalog_path, item["artifact_id"]))
        else:
            artifacts.append(dict(item))
    generation_id = _core_generation_manifest(
        manifest_path,
        "quant-engine-provider-ledger",
        fixture["as_of_date"],
        artifacts,
    )
    _pointer(fixture["provider_ledger_pointer_path"], generation_id, manifest_path)
    contract_path = fixture["contract_path"]
    contract = json.loads(contract_path.read_bytes())
    _update_source_generation(
        contract, "owner-account-live-history", manifest_path, fixture["as_of_date"]
    )
    _update_correction_generations(
        contract,
        "owner-account-live-history",
        manifest_path,
        fixture["as_of_date"],
        "PROVIDER_LEDGER",
    )
    _restore_retained_echo_correction(contract)
    _write_json(contract_path, contract)
    with pytest.MonkeyPatch.context() as patch:
        # the fixture refresh builds without an admission
        patch.setattr(
            target_series, "_current_source_admission_authority", lambda **_: None
        )
        _refresh_materialized_segment_identities(fixture)
    world["admission"] = _synthetic_current_admission(fixture)


@pytest.fixture
def plain_world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_history_authority: None,
) -> dict[str, Any]:
    fixture = _matrixport_fx_boundary_fixture(tmp_path)
    holder: dict[str, Any] = {"admission": _synthetic_current_admission(fixture)}
    monkeypatch.setattr(
        target_series,
        "_current_source_admission_authority",
        lambda **_: holder["admission"],
    )
    receipt = tmp_path / "admit-or-declare-receipt.json"
    receipt.write_bytes(b"{}")
    declaration = tmp_path / "strategy-basis-declaration.json"
    declaration.write_bytes(json.dumps(DECLARATION).encode())
    facts = tmp_path / "sleeve-facts.json"
    facts.write_bytes(_bytes(_facts()))
    inputs = dict(fixture)
    for key in (
        "provider_ledger_pointer_path",
        "matrixport_pointer_path",
        "quantbase_pointer_path",
        "portfolio123_pointer_path",
    ):
        inputs.pop(key)
    tag_map = tmp_path / "nested-strategy-tag-map.json"
    tag_map.write_bytes(_tag_map())
    holder.update(
        {
            "fixture": fixture,
            "inputs": inputs,
            "receipt": receipt,
            "declaration": declaration,
            "facts": facts,
            "tag_map": tag_map,
            "tmp": tmp_path,
        }
    )
    return holder


@pytest.fixture
def world(plain_world: dict[str, Any]) -> dict[str, Any]:
    """The same world with the selected ledger recording its provider generation."""

    _bind_ledger(
        plain_world,
        [{"provider_id": PROVIDER.upper(), "generation_id": BOUND_GENERATION}],
    )
    return plain_world


def _build(world: dict[str, Any], **extra: Any) -> tuple[dict[str, bytes], bytes]:
    return target_series.build_layered_history_generation(
        repository_sha=target_series._trusted_layered_history_code_identity()[0],
        source_selection_mode="IMMUTABLE_GENERATIONS",
        provider_ledger_pointer_path=None,
        matrixport_pointer_path=None,
        quantbase_pointer_path=None,
        portfolio123_pointer_path=None,
        provider_admission_receipt_path=world["receipt"],
        financial_input_contract_path=world["fixture"]["contract_path"],
        **world["inputs"],
        **extra,
    )


def _validate(
    world: dict[str, Any], artifacts: dict[str, bytes], manifest: bytes
) -> dict[str, object]:
    fixture = world["fixture"]
    return target_series.validate_layered_history_generation(
        artifacts=artifacts,
        manifest_content=manifest,
        contract_path=fixture["contract_path"],
        provider_admission_receipt_path=world["receipt"],
        financial_input_contract_path=fixture["contract_path"],
        provider_ledger_manifest_path=fixture["provider_ledger_manifest_path"],
        provider_ledger_catalog_path=fixture["provider_ledger_catalog_path"],
    )


def _live_rows(artifacts: dict[str, bytes], series: str) -> list[dict[str, str]]:
    rows = csv.DictReader(io.StringIO(artifacts["segments.csv"].decode("utf-8")))
    return [
        row
        for row in rows
        if row["strategy_id"] == AI and row["mode_admission"] == "INCLUDE_SELECTED_LIVE"
    ]


def _restated(world: dict[str, Any]) -> tuple[dict[str, bytes], bytes]:
    return _build(
        world,
        strategy_basis_declaration_path=world["declaration"],
        sleeve_facts_path=world["facts"],
        nested_tag_map_path=world["tag_map"],
    )


def test_without_a_declaration_every_output_is_the_main_output(
    plain_world: dict[str, Any],
) -> None:
    world = plain_world
    artifacts, manifest = _build(world)
    assert "strategy_basis_restatement" not in json.loads(manifest)
    digests = {
        name: hashlib.sha256(content).hexdigest() for name, content in artifacts.items()
    }
    assert digests == MAIN_OFF_DIGESTS
    _validate(world, artifacts, manifest)


def test_the_declared_strategy_live_segment_is_the_sleeves_path(
    world: dict[str, Any],
) -> None:
    off_artifacts, _ = _build(world)
    artifacts, manifest = _restated(world)
    expected = strategy_basis.rows_from_sleeve_facts(_bytes(_facts()), AI)
    live = _live_rows(artifacts, AI)
    assert [(row["date"], row["return"]) for row in live] == [
        (row["date"], row["return"]) for row in expected
    ]
    assert len(live) == 4 < len(_live_rows(off_artifacts, AI))
    document = json.loads(manifest)
    (identity,) = document["strategy_basis_restatement"]["strategies"]
    assert identity["strategy_id"] == AI
    assert identity["series_sha256"] == strategy_basis.series_sha256(expected)
    assert identity["return_observations"] == len(expected)
    # every other strategy's live rows are the ledger's, byte for byte
    off_rows = list(csv.DictReader(io.StringIO(off_artifacts["segments.csv"].decode())))
    on_rows = list(csv.DictReader(io.StringIO(artifacts["segments.csv"].decode())))
    assert [r for r in on_rows if r["strategy_id"] != AI] == [
        r for r in off_rows if r["strategy_id"] != AI
    ]
    # the verifier re-derives the restated rows from the embedded declaration and facts
    _validate(world, artifacts, manifest)
    schema = json.loads(
        (
            Path(__file__).parents[2]
            / "schemas"
            / "portfolio"
            / "layered-history-generation.schema.json"
        ).read_text(encoding="utf-8")
    )
    # (the synthetic admission proof is not a schema-valid one: validate the new block)
    Draft202012Validator(
        {
            "$defs": schema["$defs"],
            **schema["properties"]["strategy_basis_restatement"],
        }
    ).validate(document["strategy_basis_restatement"])


def test_returns_and_live_views_carry_the_restated_rows(world: dict[str, Any]) -> None:
    artifacts, _ = _restated(world)
    expected = {
        row["date"]: row["return"]
        for row in strategy_basis.rows_from_sleeve_facts(_bytes(_facts()), AI)
    }
    series = "AI Factor [P123]"
    for name in ("live-only.csv", "returns.csv"):
        rows = [
            row
            for row in csv.DictReader(io.StringIO(artifacts[name].decode("utf-8")))
            if row["series_id"] == series and row["date"] in expected
        ]
        assert rows, name
        assert {row["date"]: row["return"] for row in rows} == expected


def test_a_declaration_without_an_open_sleeve_fails_closed(
    world: dict[str, Any],
) -> None:
    world["facts"].write_bytes(_bytes(_facts(open_sleeve=False)))
    with pytest.raises(target_series.TargetSeriesError, match="exactly one open"):
        _restated(world)


def test_a_declaration_for_a_strategy_without_a_ledger_series_fails_closed(
    world: dict[str, Any],
) -> None:
    world["declaration"].write_bytes(
        json.dumps(
            {
                "schema": "strategy-basis-declaration/1",
                "strategies": [{"strategy_id": "no-such", "basis": "SLEEVE_EPOCH"}],
            }
        ).encode()
    )
    with pytest.raises(target_series.TargetSeriesError, match="no provider-ledger"):
        _restated(world)


def test_declaration_and_facts_are_supplied_together(world: dict[str, Any]) -> None:
    with pytest.raises(target_series.TargetSeriesError, match="together"):
        _build(world, strategy_basis_declaration_path=world["declaration"])


def test_a_restatement_needs_the_current_admission(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        target_series, "_current_source_admission_authority", lambda **_: None
    )
    inputs = dict(world["inputs"])
    with pytest.raises(
        target_series.TargetSeriesError, match="current source admission"
    ):
        target_series.build_layered_history_generation(
            repository_sha=target_series._trusted_layered_history_code_identity()[0],
            source_selection_mode="IMMUTABLE_GENERATIONS",
            provider_ledger_pointer_path=None,
            matrixport_pointer_path=None,
            quantbase_pointer_path=None,
            portfolio123_pointer_path=None,
            strategy_basis_declaration_path=world["declaration"],
            sleeve_facts_path=world["facts"],
            **inputs,
        )


def _retie(manifest: bytes, edit: Any) -> bytes:
    document = json.loads(manifest)
    edit(document)
    document.pop("generation_id")
    document["generation_id"] = hashlib.sha256(
        target_series.canonical_json_bytes(document)
    ).hexdigest()
    return bytes(target_series.canonical_json_bytes(document))


def test_the_verifier_refuses_a_restated_manifest_whose_facts_differ(
    world: dict[str, Any],
) -> None:
    artifacts, manifest = _restated(world)
    other = _facts()
    other["sleeves"][0]["days"][-1]["nav_end_after_alloc"] = "110"
    other_bytes = _bytes(other)

    def swap(document: dict[str, Any]) -> None:
        block = document["strategy_basis_restatement"]
        block["sleeve_facts"] = other_bytes.decode()
        block["sleeve_facts_sha256"] = hashlib.sha256(other_bytes).hexdigest()

    with pytest.raises(target_series.TargetSeriesError):
        _validate(world, artifacts, _retie(manifest, swap))


def test_the_verifier_refuses_a_restated_manifest_over_unrestated_rows(
    world: dict[str, Any],
) -> None:
    off_artifacts, off_manifest = _build(world)
    _, manifest = _restated(world)
    block = json.loads(manifest)["strategy_basis_restatement"]

    def bind(document: dict[str, Any]) -> None:
        document["strategy_basis_restatement"] = block

    with pytest.raises(target_series.TargetSeriesError):
        _validate(world, off_artifacts, _retie(off_manifest, bind))


def test_the_verifier_refuses_a_restated_manifest_without_the_admission(
    world: dict[str, Any],
) -> None:
    artifacts, manifest = _restated(world)
    with pytest.raises(target_series.TargetSeriesError):
        target_series.validate_layered_history_generation(
            artifacts=artifacts,
            manifest_content=manifest,
            contract_path=world["fixture"]["contract_path"],
        )


def _write_facts(world: dict[str, Any], **bindings: object) -> None:
    facts = _facts()
    facts["bindings"] = {**facts["bindings"], **bindings}
    world["facts"].write_bytes(_bytes(facts))


def test_facts_bound_to_another_provider_generation_are_refused(
    world: dict[str, Any],
) -> None:
    _write_facts(world, parent_ledger_generation_id="f65c8ddb" + "0" * 56)
    with pytest.raises(
        target_series.TargetSeriesError, match="parent_ledger_generation_id differs"
    ):
        _restated(world)


def test_a_ledger_that_records_no_provider_generations_is_a_refusal(
    plain_world: dict[str, Any],
) -> None:
    with pytest.raises(target_series.TargetSeriesError, match="provider generations"):
        _restated(plain_world)


def test_two_generations_for_the_provider_are_a_refusal(
    world: dict[str, Any],
) -> None:
    _bind_ledger(
        world,
        [
            {"provider_id": PROVIDER, "generation_id": BOUND_GENERATION},
            {"provider_id": PROVIDER.upper(), "generation_id": "8" * 64},
        ],
    )
    with pytest.raises(
        target_series.TargetSeriesError, match="parent_ledger_generation_id differs"
    ):
        _restated(world)


def test_a_tag_map_other_than_the_one_the_facts_bind_is_refused(
    world: dict[str, Any],
) -> None:
    world["tag_map"].write_bytes(_tag_map().replace(b"T2", b"T3"))
    with pytest.raises(target_series.TargetSeriesError, match="tag_map_sha256 differs"):
        _restated(world)


def test_a_tag_map_that_does_not_name_the_facts_parent_is_refused(
    world: dict[str, Any],
) -> None:
    other = _tag_map("another-account")
    world["tag_map"].write_bytes(other)
    _write_facts(world, tag_map_sha256=_tag_map_digest(other))
    with pytest.raises(target_series.TargetSeriesError, match="tag map is refused"):
        _restated(world)


def test_an_absent_tag_map_is_a_refusal(world: dict[str, Any]) -> None:
    with pytest.raises(target_series.TargetSeriesError, match="needs the tag map"):
        _build(
            world,
            strategy_basis_declaration_path=world["declaration"],
            sleeve_facts_path=world["facts"],
        )


def test_facts_without_bindings_are_refused(world: dict[str, Any]) -> None:
    facts = _facts()
    del facts["bindings"]
    world["facts"].write_bytes(_bytes(facts))
    with pytest.raises(target_series.TargetSeriesError, match="no bindings"):
        _restated(world)


def test_the_manifest_records_the_bound_identities_and_the_verifier_checks_them(
    world: dict[str, Any],
) -> None:
    artifacts, manifest = _restated(world)
    block = json.loads(manifest)["strategy_basis_restatement"]
    assert block["parent_ledger_generation_id"] == BOUND_GENERATION
    assert block["parent_account_id"] == PARENT
    assert block["tag_map_sha256"] == _tag_map_digest(_tag_map())

    def other_generation(document: dict[str, Any]) -> None:
        document["strategy_basis_restatement"]["parent_ledger_generation_id"] = "9" * 64

    with pytest.raises(target_series.TargetSeriesError):
        _validate(world, artifacts, _retie(manifest, other_generation))
