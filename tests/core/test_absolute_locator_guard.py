"""A payload hashed for embedding must not name a path only one machine can resolve.

This rule was enforced in exactly one place in the repository, on the consuming side
(`portfolio_target_series.py`), and no producer checked its own output. A producer therefore
embedded absolute user-profile locators into a portable artifact and was rejected by its own
repository's guard -- discovered only because the consumer refused the producer's bytes, and
only on Windows and macOS, because the token list did not cover the Linux CI temporary root.

So the tests below cover the platforms the original guard missed, not only the one that
happened to fail.
"""

from __future__ import annotations

import pytest

from quant_engine.core.contracts import (
    ManifestValidationError,
    assert_no_absolute_locators,
)


@pytest.mark.parametrize(
    "locator",
    [
        "C:/Users/someone/AppData/Local/Temp/capture/manifest.json",
        "C:\\Users\\someone\\AppData\\Local\\Temp\\capture\\manifest.json",
        "D:/data/provider/returns.csv",
        "/Users/someone/captures/qsi.zip",
        "/home/runner/work/quant_engine/returns.csv",
        "/root/capture/manifest.json",
        "/tmp/pytest-of-runner/pytest-3/seed/manifest.json",
        "/var/folders/xx/T/capture.zip",
        "/mnt/c/Dev/worktrees/returns.csv",
        "//fileserver/share/capture.zip",
    ],
)
def test_absolute_locators_are_refused(locator: str) -> None:
    with pytest.raises(ManifestValidationError) as raised:
        assert_no_absolute_locators({"sources": [{"path": locator}]})
    assert "absolute locator" in str(raised.value)


def test_the_linux_temporary_root_is_covered() -> None:
    """The original guard had no /tmp/ token, so these tests passed on CI and failed locally.

    That is the worst possible arrangement: green exactly when the leak escapes detection.
    """
    with pytest.raises(ManifestValidationError):
        assert_no_absolute_locators(
            {
                "accepted_predecessor": {
                    "manifest_path": "/tmp/pytest-of-runner/x/m.json"
                }
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"relative_path": "output/returns.csv"},
        {"artifact_id": "raw/strategy-history-manifest.json"},
        {"filename": "manifest.json", "sha256": "a" * 64, "size_bytes": 12},
        {"segment_id": "qsi:account-live"},
        {"note": "the ratio is 3/4 and the coverage is 1/2"},
        {"schema": "portfolio-financial-mvp-source/6"},
        {"policy_id": "PORT-HWM-4", "version": "1.9.0"},
    ],
)
def test_portable_payloads_are_accepted(payload: dict[str, object]) -> None:
    assert_no_absolute_locators(payload)


def test_a_locator_nested_anywhere_is_found() -> None:
    """Checks the serialization, so it does not need to know the payload's shape."""
    payload = {
        "receipt": {
            "sources": [
                {"source_id": "qsi", "sha256": "b" * 64},
                {"source_id": "prices", "detail": {"origin": "/Users/x/prices.csv"}},
            ]
        }
    }
    with pytest.raises(ManifestValidationError):
        assert_no_absolute_locators(payload)


def test_a_locator_in_a_key_is_found() -> None:
    with pytest.raises(ManifestValidationError):
        assert_no_absolute_locators({"C:/Users/x/manifest.json": {"sha256": "c" * 64}})


def test_the_refusal_quotes_the_offending_text() -> None:
    with pytest.raises(ManifestValidationError) as raised:
        assert_no_absolute_locators({"path": "/Users/someone/captures/qsi.zip"})
    assert "qsi.zip" in str(raised.value)


def test_the_field_name_is_reported() -> None:
    with pytest.raises(ManifestValidationError) as raised:
        assert_no_absolute_locators(
            {"path": "/Users/x/y.json"}, field_name="qsi refresh receipt"
        )
    assert "qsi refresh receipt" in str(raised.value)


def test_a_sequence_payload_is_accepted() -> None:
    assert_no_absolute_locators(
        [{"relative_path": "a.csv"}, {"relative_path": "b.csv"}]
    )
