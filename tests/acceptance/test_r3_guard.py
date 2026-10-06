"""R3 (5) V8: computed, spread and inherited keys never make a lookup table safe.

``[undefined]: true`` and ``__proto__: {undefined: true}`` make a missing payload's lookup
truthy, so the dereferencing branch runs and throws; a spread or any computed key is
unknown and so unsafe.  ACCEPTED RESIDUAL: the table definition is found by name with a
regular expression over the source text, not by a scope analysis.
"""

from __future__ import annotations

from test_instrument_guard import GLOBALS, _table

from acceptance.validate.shell import payload_dereferences_before_guard


def test_computed_spread_and_inherited_keys_do_not_guard() -> None:
    for definition in (
        "const modes = { [undefined]: true };",
        'const modes = data?.modes || { ["undefined"]: true };',
        "const modes = data?.modes || { LIVE_ONLY: 1, [String(data?.x)]: true };",
        "const modes = data?.modes || { __proto__: { undefined: true } };",
        'const modes = data?.modes || { "__proto__": { undefined: true } };',
        "const modes = data?.modes || { ...fallbackModes };",
    ):
        findings = payload_dereferences_before_guard(_table(definition), GLOBALS)
        assert [f.binding for f in findings] == ["data"], definition


def test_plain_literal_tables_still_guard() -> None:
    for definition in (
        "const modes = data?.modes || { LIVE_ONLY: [], OTHER: [1, 2] };",
        'const modes = data?.modes || { "LIVE_ONLY": 1, "OTHER-1": 2 };',
        "const modes = { LIVE_ONLY: data?.rows };",
    ):
        assert payload_dereferences_before_guard(_table(definition), GLOBALS) == [], (
            definition
        )
