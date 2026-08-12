from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "restricted_evidence_summary.py"
SPEC = importlib.util.spec_from_file_location(
    "restricted_evidence_summary", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("unable to load restricted evidence summary tool")
summary = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = summary
SPEC.loader.exec_module(summary)


def _private_fixture(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    restricted = (
        "synthetic-account-identifier-8842",
        "synthetic-restricted-session-value",
        "synthetic-private-catalog-row",
    )
    path = tmp_path / "private.json"
    path.write_text(
        json.dumps(
            {
                "accounts": [{"external_id": restricted[0]}],
                "session": {"opaque_state": restricted[1]},
                "catalog": [{"private_name": restricted[2]}],
                "generation": {"artifacts": [1, 2, 3]},
            }
        ),
        encoding="utf-8",
    )
    return path, restricted


def test_summary_emits_only_declared_aggregate_and_hash(tmp_path: Path) -> None:
    path, restricted = _private_fixture(tmp_path)

    result = summary.summarize(
        path,
        (
            "artifact_count:/generation/artifacts:count",
            "catalog_digest:/catalog:hash",
            "account_shape:/accounts/0:type",
        ),
    )
    rendered = json.dumps(result, sort_keys=True)

    assert result["state"] == "SANITIZED_AGGREGATE_ONLY"
    assert result["queries"]["artifact_count"]["count"] == 3
    assert all(value not in rendered for value in restricted)
    assert str(path) not in rendered


@pytest.mark.parametrize(
    ("query", "code"),
    (
        ("missing:/unknown:count", "QUERY_FIELD_UNKNOWN"),
        ("raw:/accounts/0/external_id:value", "QUERY_OPERATION_INVALID"),
        ("bad label:/accounts:count", "QUERY_LABEL_INVALID"),
    ),
)
def test_unknown_or_raw_queries_fail_without_values(
    tmp_path: Path, query: str, code: str
) -> None:
    path, restricted = _private_fixture(tmp_path)
    stdout = io.StringIO()
    stderr = io.StringIO()

    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = summary.main(("--input", str(path), "--query", query))

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert code in stderr.getvalue()
    assert all(value not in stderr.getvalue() for value in restricted)
    assert str(path) not in stderr.getvalue()


def test_query_is_required_before_input_is_read(tmp_path: Path) -> None:
    path, restricted = _private_fixture(tmp_path)

    with pytest.raises(summary.EvidenceSummaryError, match="QUERY_REQUIRED"):
        summary.summarize(path, ())

    assert all(value not in "QUERY_REQUIRED" for value in restricted)


def test_duplicate_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"generation":{"items":[]},"generation":{"items":[1]}}')

    with pytest.raises(summary.EvidenceSummaryError, match="INPUT_JSON_DUPLICATE_KEY"):
        summary.summarize(path, ("items:/generation/items:count",))


def test_decoder_depth_failure_has_stable_value_free_code(tmp_path: Path) -> None:
    path = tmp_path / "deep.json"
    path.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
    stderr = io.StringIO()

    with redirect_stderr(stderr):
        exit_code = summary.main(("--input", str(path), "--query", "shape:/0:type"))

    assert exit_code == 1
    assert "INPUT_JSON_INVALID" in stderr.getvalue()
    assert str(path) not in stderr.getvalue()


def test_linked_parent_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "private.json").write_text('{"items":[]}', encoding="utf-8")
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory links are unavailable")

    with pytest.raises(summary.EvidenceSummaryError, match="INPUT_LINK_FORBIDDEN"):
        summary.summarize(linked / "private.json", ("items:/items:count",))
