"""``strict_json_shared``: identical bytes decode once, read-only, nothing weaker."""

from __future__ import annotations

import json

import pytest

from quant_engine.core import strict_json
from quant_engine.core.strict_json import (
    StrictJSONError,
    clear_strict_json_shared,
    strict_json_loads,
    strict_json_shared,
    verify_shared_unmutated,
)


def _big_payload() -> bytes:
    rows = [{"id": i, "value": i * 0.5, "tag": "x" * 40} for i in range(20000)]
    raw = json.dumps({"rows": rows, "flag": True, "none": None}).encode("utf-8")
    assert len(raw) >= strict_json._SHARED_MIN_BYTES
    return raw


@pytest.fixture(autouse=True)
def _fresh():
    clear_strict_json_shared()
    yield
    clear_strict_json_shared()


def test_identical_bytes_decode_once_and_equal_the_strict_decode():
    raw = _big_payload()
    before = strict_json._decode_calls
    first = strict_json_shared(raw, label="t")
    second = strict_json_shared(bytes(bytearray(raw)), label="t")  # equal, new object
    assert strict_json._decode_calls - before == 1
    assert second is first
    assert first == strict_json_loads(raw, label="t")
    verify_shared_unmutated()


def test_a_caller_that_mutates_the_shared_object_is_caught():
    raw = _big_payload()
    shared = strict_json_shared(raw, label="t")
    assert isinstance(shared, dict)
    shared["rows"].append("poison")
    with pytest.raises(StrictJSONError, match="mutated"):
        verify_shared_unmutated()


def test_tampered_bytes_still_fail_after_the_clean_bytes_are_shared():
    raw = _big_payload()
    strict_json_shared(raw, label="t")
    tampered = raw[:-1] + b',"flag":false}'  # duplicate member
    for _ in range(2):  # a failure is never cached
        with pytest.raises(StrictJSONError, match="duplicate object member"):
            strict_json_shared(tampered, label="t")
    with pytest.raises(StrictJSONError):
        strict_json_shared(raw[:-1] + b',"n":NaN}', label="t")
    with pytest.raises(StrictJSONError):
        strict_json_shared(raw + b"x", label="t")


def test_small_inputs_are_fresh_mutable_objects():
    a = strict_json_shared(b'{"a":[1]}', label="t")
    b = strict_json_shared(b'{"a":[1]}', label="t")
    assert a is not b
    a["a"].append(2)
    assert b == {"a": [1]}
    verify_shared_unmutated()


def test_a_mutated_shared_object_is_evicted_and_never_served_again():
    raw = _big_payload()
    shared = strict_json_shared(raw, label="t")
    assert isinstance(shared, dict)
    shared["flag"] = False
    with pytest.raises(StrictJSONError, match="mutated"):
        verify_shared_unmutated()
    fresh = strict_json_shared(raw, label="t")
    assert fresh is not shared and fresh["flag"] is True
    verify_shared_unmutated()
