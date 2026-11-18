"""#1010: each lens source row carries the provider's own publication stamp."""

from __future__ import annotations

from typing import Any

import portfolio_control_panel as panel


def _manifest(stamps: dict[str, str | None]) -> dict[str, Any]:
    return {
        "sources": [
            {
                "source_key": key,
                "response": {
                    "captured_at_utc": "2026-11-05T12:00:00Z",
                    "last_modified": stamp,
                },
            }
            for key, stamp in stamps.items()
        ]
    }


def test_lens_takes_the_latest_provider_last_modified() -> None:
    manifest = _manifest(
        {
            "us_factors": "Tue, 27 Oct 2026 15:18:47 GMT",
            "us_momentum": "Mon, 26 Oct 2026 20:51:47 GMT",
            "europe_factors": "Mon, 26 Oct 2026 20:51:50 GMT",
        }
    )
    got = panel._lens_provider_publication(manifest, "us-economic")
    assert got == {
        "captured_at_utc": "2026-11-05T12:00:00Z",
        "last_modified": "Tue, 27 Oct 2026 15:18:47 GMT",
    }


def test_missing_stamp_stays_missing_and_unlagged_lens_has_none() -> None:
    manifest = _manifest({"us_factors": "Tue, 27 Oct 2026 15:18:47 GMT", "us_momentum": None})
    got = panel._lens_provider_publication(manifest, "us-economic")
    assert got is not None and got["last_modified"] is None
    assert panel._lens_provider_publication(manifest, "us-equity-style") is None
