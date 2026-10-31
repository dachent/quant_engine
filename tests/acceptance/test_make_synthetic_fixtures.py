"""The synthetic-fixture generator is deterministic, salted and ratio-preserving."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import make_synthetic_fixtures as gen

REAL = "account-0123456789abcdef"
OTHER = "account-aaaaaaaaaaaaaaaa"


def test_the_mapping_is_deterministic_salted_and_format_preserving() -> None:
    one = gen.Mapper("salt-one")
    again = gen.Mapper("salt-one")
    two = gen.Mapper("salt-two")
    assert one.account("0123456789abcdef") == again.account("0123456789abcdef")
    assert one.account("0123456789abcdef") != two.account("0123456789abcdef")
    assert re.fullmatch(r"account-[0-9a-f]{16}", one.account("0123456789abcdef"))
    assert one.account("0123456789abcdef") != REAL
    assert re.fullmatch(r"\d{4}", one.four("0042"))
    assert Decimal("0.6") <= one.scale < Decimal("1.4")
    assert one.scale != Decimal(1)


def test_money_is_scaled_exactly_by_one_factor() -> None:
    mapper = gen.Mapper("salt-one")
    a, b = Decimal(mapper.money("40000")), Decimal(mapper.money("10000"))
    assert a == 4 * b == Decimal("40000") * mapper.scale
    assert Decimal(mapper.money("12.34")) == Decimal("12.34") * mapper.scale


def test_python_scrub_rewrites_only_spec_values(tmp_path: Path) -> None:
    mapper = gen.Mapper("salt-one")
    source = tmp_path / "t.py"
    source.write_bytes(
        f'A = "{REAL}"\nB = "{OTHER}"\nC = "1234.5"\nD = "11234.5"\nE = "IBKR 1111"\n'.encode()
    )
    spec = {"ids": [REAL], "amount_tokens": ["1234.5"]}
    out = gen.scrub_python(source, mapper, spec).decode()
    lines = out.splitlines()
    assert lines[0] == f'A = "{mapper.account("0123456789abcdef")}"'
    assert lines[1] == f'B = "{OTHER}"'
    assert lines[2] == f'C = "{mapper.money("1234.5")}"'
    assert lines[3] == 'D = "11234.5"'
    assert lines[4] == f'E = "IBKR {mapper.four("1111")}"'
    assert "\r" not in out


def test_rows_keep_ratios_and_scale_amounts(tmp_path: Path) -> None:
    mapper = gen.Mapper("salt-one")
    document = {
        "provenance": {"run": "C:/Dev/q/real", "note": "x"},
        "rows": [
            {
                "account_id": REAL,
                "account_label": "Schwab \u20220042 \u2014 IRA (Alice)",
                "last_four": "0042",
                "beginning_capital": "1000",
                "return": "0.0123456789",
            }
        ],
    }
    source = tmp_path / "rows.json"
    source.write_text(json.dumps(document), encoding="ascii")
    out = json.loads(gen.synthesize_rows(source, mapper))
    row = out["rows"][0]
    assert row["account_id"] == mapper.account("0123456789abcdef")
    assert "0042" not in row["account_label"] and "Alice" not in row["account_label"]
    assert row["last_four"] == mapper.four("0042")
    assert row["beginning_capital"] == mapper.money("1000")
    assert row["return"] == "0.0123456789"
    assert "real" not in out["provenance"]["run"]
    assert "synthetic_note" in out["provenance"]
