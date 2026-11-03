"""#624: a sleeve-facts file is bound, fail closed, to the run that consumes it.

SYNTHETIC cases (invented ledger, catalog, tag map); the real R2 / R2.1 files are in the
env-gated tests at the end:

* ``QE_AI624_R2_FACTS``       the R2 oracle facts (binds scratch generation f65c8ddb)
* ``QE_AI624_R22_FACTS``      branch S's rebuild (own fraction), the applicable facts
* ``QE_AI624_R21_FACTS``      branch M's pro rata rebuild (facts sha 94973dc4), a regression reference
* ``QE_AI624_R2_LEDGER``      provider-ledger.json of the R2 ledger (058f471a)
* ``QE_AI624_R21_LEDGER``     provider-ledger.json of the R2.1 ledger (8c20b436)
* ``QE_AI624_R2_IBKR_ROOT``   the dataIBKR root
* ``QE_AI624_R2_TAG_MAP``     the R2 draft tag map (IBKR parent only), used to show it is refused
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from quant_engine.core import release_input_root
import sleeve_basis_synthetic as synth

from acceptance.validate import checks, facts_bindings as fb, sleeve_basis

REPO = Path(__file__).resolve().parents[2]
REPO_TAG_MAP = (
    release_input_root(REPO)
    / "ops/mvp2-release/inputs/sleeves/nested-strategy-tag-map.json"
)


def _refused(facts: dict[str, Any], sources: Any) -> sleeve_basis.Refusal:
    with pytest.raises(sleeve_basis.Refusal) as caught:
        fb.check_bindings(facts, sources)
    return caught.value


def _sources(**changes: Any) -> fb.FactsSources:
    base = synth.sources()
    return fb.FactsSources(
        ledger=changes.get("ledger", base.ledger),
        catalog_raw=changes.get("catalog_raw", base.catalog_raw),
        tag_map_raw=changes.get("tag_map_raw", base.tag_map_raw),
    )


def test_matching_bindings_pass() -> None:
    fb.check_bindings(synth.bound_facts(), synth.sources())


def test_the_row_digest_is_the_canonical_hash_of_the_parents_window_rows() -> None:
    document = synth.catalog()
    expected = synth.bound_facts()["bindings"]["account_rows_sha256"]
    assert (
        fb.account_rows_sha256(document, synth.PARENT, synth.FIRST, synth.LAST)
        == expected
    )
    # the row outside the window, another account's row and a cash activity dated by its
    # recognition day are each decided by the rule, so moving one changes the digest
    shifted = json.loads(json.dumps(document))
    shifted["transactions"][1]["event_date"] = "2026-02-05"
    assert (
        fb.account_rows_sha256(shifted, synth.PARENT, synth.FIRST, synth.LAST)
        != expected
    )


def test_the_tag_map_digest_ignores_layout_not_content() -> None:
    compact = json.dumps(synth.TAG_MAP, separators=(",", ":")).encode()
    spaced = json.dumps(synth.TAG_MAP, indent=4, sort_keys=False).encode()
    assert fb.tag_map_sha256(compact) == fb.tag_map_sha256(spaced)
    other = json.dumps({**synth.TAG_MAP, "schema_version": 2}).encode()
    assert fb.tag_map_sha256(other) != fb.tag_map_sha256(compact)


def test_a_facts_file_of_another_generation_is_refused() -> None:
    facts = synth.bound_facts()
    facts["bindings"]["parent_ledger_generation_id"] = "gen-scratch-candidate"
    error = _refused(facts, synth.sources())
    assert error.code == fb.BINDING_LEDGER_GENERATION
    assert "gen-scratch-candidate" in error.message
    assert synth.LEDGER_GENERATION in error.message


def test_a_ledger_with_no_or_two_rows_for_the_provider_is_refused() -> None:
    twice = {
        "provider_generations": [
            {"provider_id": "IBKR", "generation_id": "a"},
            {"provider_id": "ibkr", "generation_id": "b"},
        ]
    }
    assert _refused(synth.bound_facts(), _sources(ledger=twice)).code == (
        fb.BINDING_LEDGER_GENERATION
    )
    none = {
        "provider_generations": [{"provider_id": "quantbase", "generation_id": "q"}]
    }
    assert _refused(synth.bound_facts(), _sources(ledger=none)).code == (
        fb.BINDING_LEDGER_GENERATION
    )
    assert _refused(synth.bound_facts(), _sources(ledger={})).code == (
        fb.BINDING_MISSING_SIDE
    )


def test_a_missing_comparison_side_is_a_refusal_never_not_comparable() -> None:
    assert _refused(synth.bound_facts(), None).code == fb.BINDING_MISSING_SIDE
    assert _refused(synth.bound_facts(), _sources(catalog_raw=None)).code == (
        fb.BINDING_MISSING_SIDE
    )
    assert _refused(synth.bound_facts(), _sources(tag_map_raw=None)).code == (
        fb.BINDING_MISSING_SIDE
    )
    bare = synth.bound_facts()
    del bare["bindings"]
    assert _refused(bare, synth.sources()).code == fb.BINDING_MISSING_SIDE


def test_the_catalog_must_be_a_schema_7_catalog_of_the_provider_listing_the_parent() -> (
    None
):
    for change, code in (
        ({"catalog_schema_version": 6}, fb.BINDING_CATALOG),
        ({"provider_id": "schwab"}, fb.BINDING_CATALOG),
        ({"accounts": [{"account_id": "acct-other"}]}, fb.BINDING_PARENT_ACCOUNT),
    ):
        catalog = {**synth.catalog(), **change}
        error = _refused(
            synth.bound_facts(), _sources(catalog_raw=json.dumps(catalog).encode())
        )
        assert error.code == code, change


def test_one_changed_catalog_row_breaks_the_account_rows_binding() -> None:
    catalog = synth.catalog()
    catalog["positions"][0]["quantity"] = "6"
    error = _refused(
        synth.bound_facts(), _sources(catalog_raw=json.dumps(catalog).encode())
    )
    assert error.code == fb.BINDING_ACCOUNT_ROWS
    assert synth.bound_facts()["bindings"]["account_rows_sha256"] in error.message


def test_a_tag_map_that_is_not_the_bound_one_is_refused() -> None:
    other = json.dumps({**synth.TAG_MAP, "as_of_date": "2026-10-26"}).encode()
    error = _refused(synth.bound_facts(), _sources(tag_map_raw=other))
    assert error.code == fb.BINDING_TAG_MAP


def test_a_generation_directory_is_read_only_when_it_is_what_the_ledger_says(
    tmp_path: Path,
) -> None:
    import hashlib

    raw = json.dumps(synth.catalog()).encode()
    generation = tmp_path / synth.LEDGER_GENERATION
    generation.mkdir()
    (generation / "ibkr-catalog.json").write_bytes(raw)
    manifest = {
        "generation_id": synth.LEDGER_GENERATION,
        "artifacts": [
            {
                "artifact_id": "normalized/ibkr-catalog.json",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
        ],
    }
    (generation / "generation.json").write_text(json.dumps(manifest))
    assert fb.read_generation_catalog(generation, synth.LEDGER_GENERATION) == raw
    # the root layout: <root>/generations/<id>
    root = tmp_path / "root"
    (root / "generations").mkdir(parents=True)
    generation.rename(root / "generations" / synth.LEDGER_GENERATION)
    loaded = fb.load_sources(synth.ledger(), synth.bound_facts(), root, None)
    assert loaded.catalog_raw == raw and loaded.tag_map_raw is None
    # the wrong directory, a manifest naming another generation, a changed catalog
    directory = root / "generations" / synth.LEDGER_GENERATION
    with pytest.raises(sleeve_basis.Refusal) as caught:
        fb.read_generation_catalog(directory, "gen-other")
    assert caught.value.code == fb.BINDING_CATALOG
    (directory / "ibkr-catalog.json").write_bytes(raw + b" ")
    with pytest.raises(sleeve_basis.Refusal) as caught:
        fb.read_generation_catalog(directory, synth.LEDGER_GENERATION)
    assert "differs from its generation manifest" in caught.value.message


def test_the_corporate_action_rows_are_bound_by_their_own_digest() -> None:
    expected = synth.bound_facts()["bindings"]["corporate_actions_sha256"]
    assert (
        fb.corporate_actions_sha256(
            synth.catalog(), synth.PARENT, synth.FIRST, synth.LAST
        )
        == expected
    )
    # one changed row, or a row moved into the window, changes the digest
    changed = synth.catalog()
    changed["corporate_actions"][0]["kind"] = "SD"
    error = _refused(
        synth.bound_facts(), _sources(catalog_raw=json.dumps(changed).encode())
    )
    assert error.code == fb.BINDING_CORPORATE_ACTIONS
    moved = synth.catalog()
    moved["corporate_actions"][1]["event_date"] = "2026-02-04"
    assert (
        _refused(
            synth.bound_facts(), _sources(catalog_raw=json.dumps(moved).encode())
        ).code
        == fb.BINDING_CORPORATE_ACTIONS
    )


def test_a_facts_file_without_the_corporate_action_binding_is_an_old_schema_file() -> (
    None
):
    facts = synth.bound_facts()
    del facts["bindings"]["corporate_actions_sha256"]
    error = _refused(facts, synth.sources())
    assert error.code == fb.BINDING_OLD_SCHEMA
    assert "pro rata" in error.message
    # unless it is explicitly treated as a regression reference
    fb.check_bindings(facts, synth.sources(), regression_reference=True)
    # a regression reference is still bound to everything else
    facts["bindings"]["tag_map_sha256"] = "0" * 64
    with pytest.raises(sleeve_basis.Refusal) as caught:
        fb.check_bindings(facts, synth.sources(), regression_reference=True)
    assert caught.value.code == fb.BINDING_TAG_MAP


def test_the_check_names_the_old_schema_file() -> None:
    facts = synth.bound_facts()
    del facts["bindings"]["corporate_actions_sha256"]
    findings = list(checks.check_sleeve_basis(_context(facts, synth.sources())))
    assert [f.klass for f in findings] == [checks.BLOCKER_AUTHORITY]
    assert fb.BINDING_OLD_SCHEMA in findings[0].message


# ----------------------------------------------------------- through the validator check


def _context(facts: dict[str, Any], sources: Any) -> checks.Context:
    panel = {"strategies": [{"strategy_id": synth.DECLARED}], "account_summary": []}
    return checks.Context(
        gen=SimpleNamespace(panel=panel, report={}),  # type: ignore[arg-type]
        cutoff=date(2026, 2, 6),
        cutoff_source="test",
        strategy_basis={synth.DECLARED: "SLEEVE_EPOCH"},
        sleeve_facts=facts,
        facts_sources=sources,
    )


def test_the_check_refuses_facts_that_are_not_bound_to_the_run() -> None:
    facts = synth.bound_facts()
    facts["bindings"]["parent_ledger_generation_id"] = "gen-scratch-candidate"
    findings = list(checks.check_sleeve_basis(_context(facts, synth.sources())))
    assert [f.klass for f in findings] == [checks.BLOCKER_AUTHORITY]
    assert fb.BINDING_LEDGER_GENERATION in findings[0].message
    # nothing else is derived from facts that are refused
    assert checks.check_sleeve_basis.__name__ == "check_sleeve_basis"


def test_the_check_refuses_when_the_run_holds_no_sources() -> None:
    findings = list(checks.check_sleeve_basis(_context(synth.bound_facts(), None)))
    assert len(findings) == 1
    assert fb.BINDING_MISSING_SIDE in findings[0].message


def test_bound_facts_go_on_to_the_derivation() -> None:
    findings = list(
        checks.check_sleeve_basis(_context(synth.bound_facts(), synth.sources()))
    )
    assert findings
    assert all(fb.BINDING_LEDGER_GENERATION not in f.message for f in findings)


# ------------------------------------------------------------------ the real R2 / R2.1


def _env(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value and Path(value).exists() else None


R2_FACTS, R21_FACTS = _env("QE_AI624_R2_FACTS"), _env("QE_AI624_R21_FACTS")
R22_FACTS = _env("QE_AI624_R22_FACTS")
R2_LEDGER, R21_LEDGER = _env("QE_AI624_R2_LEDGER"), _env("QE_AI624_R21_LEDGER")
IBKR_ROOT, R2_TAG_MAP = _env("QE_AI624_R2_IBKR_ROOT"), _env("QE_AI624_R2_TAG_MAP")
real = pytest.mark.skipif(
    not (
        R2_FACTS and R21_FACTS and R2_LEDGER and R21_LEDGER and IBKR_ROOT and R2_TAG_MAP
    ),
    reason="the R2 / R2.1 facts, ledgers, IBKR root and tag map are not all set",
)


def _bind(
    facts_path: Path, ledger_path: Path, tag_map: Path, *, reference: bool = False
) -> None:
    facts = json.loads(facts_path.read_bytes())
    ledger = json.loads(ledger_path.read_bytes())
    sources = fb.load_sources(ledger, facts, IBKR_ROOT, tag_map)
    fb.check_bindings(facts, sources, regression_reference=reference)


@real
def test_the_own_fraction_rebuild_binds_to_the_r21_ledger() -> None:
    """Branch S's facts: all six bindings, the corporate-action digest recomputed here."""

    assert R22_FACTS and R21_LEDGER
    _bind(R22_FACTS, R21_LEDGER, REPO_TAG_MAP)
    facts = json.loads(R22_FACTS.read_bytes())
    assert facts["bindings"]["corporate_actions_sha256"].startswith("07de8075")


@real
def test_the_pro_rata_rebuild_is_an_old_schema_file_unless_a_regression_reference() -> (
    None
):
    assert R21_FACTS and R21_LEDGER
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _bind(R21_FACTS, R21_LEDGER, REPO_TAG_MAP)
    assert caught.value.code == fb.BINDING_OLD_SCHEMA
    _bind(R21_FACTS, R21_LEDGER, REPO_TAG_MAP, reference=True)


@real
def test_the_r2_oracle_file_is_refused_against_both_ledgers() -> None:
    assert R2_FACTS and R2_LEDGER and R21_LEDGER and R2_TAG_MAP
    for ledger in (R2_LEDGER, R21_LEDGER):
        with pytest.raises(sleeve_basis.Refusal) as caught:
            _bind(R2_FACTS, ledger, R2_TAG_MAP)
        assert caught.value.code == fb.BINDING_LEDGER_GENERATION
        assert "f65c8ddb" in caught.value.message


@real
def test_the_own_fraction_rebuild_is_refused_against_the_r2_ledger_and_the_oracle_tag_map() -> (
    None
):
    assert R22_FACTS and R2_LEDGER and R21_LEDGER and R2_TAG_MAP
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _bind(R22_FACTS, R2_LEDGER, REPO_TAG_MAP)
    assert caught.value.code == fb.BINDING_LEDGER_GENERATION
    with pytest.raises(sleeve_basis.Refusal) as caught:
        _bind(R22_FACTS, R21_LEDGER, R2_TAG_MAP)
    assert caught.value.code == fb.BINDING_TAG_MAP


@real
def test_the_oracle_rows_are_the_r2_ledger_generations_rows() -> None:
    """Content-equal: the oracle's row digest is the catalog 610d09d6's, only the id differs."""

    assert R2_FACTS and R2_LEDGER and IBKR_ROOT
    facts = json.loads(R2_FACTS.read_bytes())
    ledger = json.loads(R2_LEDGER.read_bytes())
    generation = fb.ledger_provider_generation(ledger, "ibkr")
    catalog = json.loads(
        fb.read_generation_catalog(IBKR_ROOT / "generations" / generation, generation)
    )
    computed = fb.account_rows_sha256(
        catalog,
        facts["parent_account_id"],
        facts["window"]["first_day"],
        facts["window"]["last_day"],
    )
    assert computed == facts["bindings"]["account_rows_sha256"]


@pytest.mark.skipif(
    not REPO_TAG_MAP.is_file(),
    reason="the tag map is a release input outside Git: set QE_RELEASE_INPUTS_ROOT",
)
def test_the_committed_tag_map_is_the_one_map_of_the_r21_lineage() -> None:
    """Branch M's finding: file 10befe29, canonical digest b1cfa8e8, one map for all."""

    import hashlib

    raw = REPO_TAG_MAP.read_bytes()
    assert hashlib.sha256(raw).hexdigest().startswith("10befe29")
    assert fb.tag_map_sha256(raw).startswith("b1cfa8e8")


@real
def test_the_r2_oracle_is_an_old_schema_file_even_when_its_generation_is_corrected() -> (
    None
):
    """Content-equal to the R2 ledger's catalog, yet it lacks the corporate-action binding."""

    assert R2_FACTS and R2_LEDGER and R2_TAG_MAP
    facts = json.loads(R2_FACTS.read_bytes())
    ledger = json.loads(R2_LEDGER.read_bytes())
    facts["bindings"]["parent_ledger_generation_id"] = fb.ledger_provider_generation(
        ledger, "ibkr"
    )
    sources = fb.load_sources(ledger, facts, IBKR_ROOT, R2_TAG_MAP)
    with pytest.raises(sleeve_basis.Refusal) as caught:
        fb.check_bindings(facts, sources)
    assert caught.value.code == fb.BINDING_OLD_SCHEMA
    # explicitly a regression reference (with its own draft tag map), it binds
    fb.check_bindings(facts, sources, regression_reference=True)
