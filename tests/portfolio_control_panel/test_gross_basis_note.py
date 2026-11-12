"""#906: the systematic capital headline states a gross-counted strategy's basis."""

from __future__ import annotations

from pathlib import Path

_APP = (
    Path(__file__).resolve().parents[2] / "apps" / "portfolio-control-panel" / "app.js"
)


def test_the_systematic_headline_carries_the_gross_loan_equity_note() -> None:
    source = _APP.read_text(encoding="utf-8")
    assert "function grossBasisNote()" in source
    # the note reads the disclosure's own gross, liability and net-equivalent figures
    for field in ("gross_sleeve_nav_usd", "liability_line", "net_equivalent_usd"):
        assert field in source.split("function grossBasisNote()")[1][:900]
    assert "${grossBasisNote()}" in source
    systematic = [
        line for line in source.splitlines() if 'capitalAmount("systematic")' in line
    ]
    assert any("grossBasisNote()" in line for line in systematic)
