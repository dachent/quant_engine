"""Strategy concepts: the producer block reads the governed contract; the view labels evidence honestly."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pytest

import portfolio_control_panel as panel
from test_shell_qc1_labels import _run

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads(
    (REPOSITORY_ROOT / "contracts" / "strategy-return-sources-v1.json").read_text(
        encoding="utf-8"
    )
)


def _rows(*strategy_ids: str) -> list[dict[str, str]]:
    return [
        {"strategy_id": item, "strategy_name": f"Name {item}"} for item in strategy_ids
    ]


def test_the_block_carries_the_contract_words_unchanged() -> None:
    entry = CONTRACT["strategies"][0]
    block = panel._strategy_concepts(_rows(entry["strategy_id"]))  # noqa: SLF001
    concept = block["rows"][0]["concept"]

    assert block["schema_version"] == 1
    assert block["source_artifact"] == "contracts/strategy-return-sources-v1.json"
    assert re.fullmatch(r"[0-9a-f]{64}", block["source_sha256"])
    assert concept["mechanism"] == entry["mechanism"]
    assert concept["persistence"] == entry["persistence"]
    assert concept["alternative_explanation"] == entry["alternative_explanation"]
    assert concept["falsification"] == entry["falsification"]
    assert concept["confidence"] == entry["confidence"]
    assert concept["source_status"] == (
        "NO_IDENTIFIED_SOURCE" if entry["no_identified_source"] else "IDENTIFIED_SOURCE"
    )


def test_a_strategy_the_contract_does_not_describe_gets_a_null_concept() -> None:
    block = panel._strategy_concepts(_rows("syn-unknown"))  # noqa: SLF001

    assert block["rows"] == [
        {
            "strategy_id": "syn-unknown",
            "strategy_name": "Name syn-unknown",
            "concept": None,
        }
    ]


def test_the_rows_follow_the_underwriting_order() -> None:
    ids = [item["strategy_id"] for item in CONTRACT["strategies"][:3]][::-1]
    block = panel._strategy_concepts(_rows(*ids))  # noqa: SLF001

    assert [row["strategy_id"] for row in block["rows"]] == ids


def test_the_schema_admits_the_block() -> None:
    schema = json.loads(
        (
            REPOSITORY_ROOT / "schemas" / "portfolio" / "control-panel-data.schema.json"
        ).read_text(encoding="utf-8")
    )
    definition = schema["$defs"]["strategy_concepts"]

    assert schema["properties"]["strategy_concepts"] == {
        "$ref": "#/$defs/strategy_concepts"
    }
    assert definition["additionalProperties"] is False
    assert set(definition["required"]) == {
        "schema_version",
        "source_artifact",
        "source_sha256",
        "rows",
    }


def _payload(**concept_fields: Any) -> dict[str, Any]:
    concept = {
        "mechanism": "Synthetic mechanism text.",
        "persistence": "Synthetic persistence text.",
        "alternative_explanation": "Synthetic alternative.",
        "falsification": ["Synthetic test one.", "Synthetic test two."],
        "confidence": "low",
        "source_status": "IDENTIFIED_SOURCE",
        **concept_fields,
    }
    return {
        "strategy_concepts": {
            "schema_version": 1,
            "source_artifact": "contracts/synthetic.json",
            "source_sha256": "a" * 64,
            "rows": [
                {
                    "strategy_id": "syn-a",
                    "strategy_name": "Synthetic A",
                    "concept": concept,
                },
                {
                    "strategy_id": "syn-b",
                    "strategy_name": "Synthetic B",
                    "concept": None,
                },
            ],
        },
        "strategies": [
            {
                "strategy_id": "syn-a",
                "economic_family": "Synthetic family",
                "design_status": "EVIDENCED",
                "design_version_ref": "synthetic-design:1",
                "systematic_weight": "0.25",
                "weight_basis": "CURRENT_CUTOFF",
                "current_value_usd": "1000",
                "mwr_member_account_ids": ["acct-1"],
            },
            {
                "strategy_id": "syn-b",
                "economic_family": "Other family",
                "design_status": "REFERENCE_ONLY",
            },
        ],
        "strategy_underwriting": {
            "rows": [
                {
                    "strategy_id": "syn-a",
                    "design_fit": {
                        "status": "AUTHORITATIVE_DESIGN_AVAILABLE",
                        "portfolio_role": "Synthetic role",
                        "conformance_status": "AUTHORITATIVE_DESIGN_AND_EMPIRICAL_PREHISTORY_COMPARABLE",
                    },
                },
                {
                    "strategy_id": "syn-b",
                    "design_fit": {"status": "INFERRED_ECONOMIC_ARCHETYPE_ONLY"},
                },
            ]
        },
    }


def _html(tmp_path: Path, payload: dict[str, Any]) -> str:
    return str(_run(tmp_path, payload, "renderConcepts")["#concepts-root"]["innerHTML"])


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def test_a_card_shows_each_field_from_the_payload(tmp_path: Path) -> None:
    html = _html(tmp_path, _payload())
    text = _plain(html)

    assert html.count('class="panel concept-card"') == 2
    for expected in (
        "Synthetic mechanism text.",
        "Synthetic persistence text.",
        "Synthetic alternative.",
        "Synthetic test one.",
        "Synthetic family",
        "Synthetic role",
        "synthetic-design:1",
        "25.0%",
    ):
        assert expected in text, expected
    assert "Authoritative design" in text
    assert 'aria-labelledby="concept-syn-a-title"' in html


def test_an_unestablished_mechanism_is_labelled_a_hypothesis(tmp_path: Path) -> None:
    text = _plain(
        _html(
            tmp_path, _payload(source_status="NO_IDENTIFIED_SOURCE", confidence="none")
        )
    )

    assert "Hypothesis, not established" in text


def test_an_inferred_archetype_is_never_authoritative(tmp_path: Path) -> None:
    html = _html(tmp_path, _payload())
    second = _plain(html.split('class="panel concept-card"')[2])

    assert "Design not authoritative" in second
    assert "Authoritative design" not in second


def test_a_missing_concept_says_so_and_a_missing_block_is_explicit(
    tmp_path: Path,
) -> None:
    text = _plain(_html(tmp_path, _payload()))
    assert "No concept record in the governed contract for this strategy" in text

    absent = _plain(_html(tmp_path, {}))
    assert "Strategy concepts are not in this snapshot" in absent


@pytest.mark.parametrize(
    "field", ["mechanism", "persistence", "alternative_explanation"]
)
def test_html_in_the_concept_text_is_escaped(tmp_path: Path, field: str) -> None:
    html = _html(tmp_path, _payload(**{field: "<script>alert(1)</script>"}))

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
