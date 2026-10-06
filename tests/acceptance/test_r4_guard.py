"""R4 (1): the lookup-table scan tokenizes the literal instead of pattern-matching text.

Comments, strings (including ones that contain a comma, a bracket, a colon or a semicolon)
and template text are skipped.  A comment must not hide a computed key, and a property
VALUE that merely looks like ``, [label]`` must not be read as a key.
"""

from __future__ import annotations

from test_instrument_guard import GLOBALS, _table

from acceptance.validate.shell import payload_dereferences_before_guard


def _bindings(definition: str) -> list[str]:
    return [
        f.binding
        for f in payload_dereferences_before_guard(_table(definition), GLOBALS)
    ]


def test_a_comment_before_a_computed_key_does_not_hide_it() -> None:
    for definition in (
        "const modes = { /* fallback */ [undefined]: true };",
        "const modes = data?.modes || { /* x */ LIVE_ONLY: 1, /* y */ [undefined]: true };",
        "const modes = data?.modes || { // note\n  [undefined]: true };",
        "const modes = data?.modes || { LIVE_ONLY: 1, /* a, b */ ...other };",
        "const modes = data?.modes || { /* c */ __proto__: { undefined: true } };",
        "const modes = data?.modes || { /* c */ undefined: true };",
    ):
        assert _bindings(definition) == ["data"], definition


def test_a_value_string_that_looks_like_a_computed_key_is_not_a_key() -> None:
    for definition in (
        'const modes = { LIVE_ONLY: ", [label]" };',
        "const modes = data?.modes || { LIVE_ONLY: 'a, [b]: c', OTHER: \"...x\" };",
        "const modes = data?.modes || { LIVE_ONLY: `x, [${data?.y}]` };",
        'const modes = data?.modes || { LIVE_ONLY: "a; b", OTHER: 1 };',
        'const modes = data?.modes || { LIVE_ONLY: "undefined: 1, null: 2" };',
        "const modes = data?.modes || { LIVE_ONLY: 1, /* [undefined]: true */ OTHER: 2 };",
    ):
        assert _bindings(definition) == [], definition


def test_a_mixed_table_is_judged_by_its_real_keys() -> None:
    safe = 'const modes = data?.modes || { A: ", [x]", /* [undefined]: 1 */ "B": "...", C: 1 };'
    assert _bindings(safe) == []
    unsafe = (
        'const modes = data?.modes || { A: ", [x]", /* ok */ [undefined]: true, C: 1 };'
    )
    assert _bindings(unsafe) == ["data"]
    quoted = 'const modes = data?.modes || { A: ", [x]", "undefined": true };'
    assert _bindings(quoted) == ["data"]
