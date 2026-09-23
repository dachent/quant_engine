"""Offline tests for the workstation-only main push guard."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY_ROOT / "tools" / "check_push_policy.py"
SPEC = importlib.util.spec_from_file_location("check_push_policy", MODULE_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("unable to load push policy")
push_policy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = push_policy
SPEC.loader.exec_module(push_policy)

OID_A = "a" * 40
OID_B = "b" * 40
ZERO_OID = "0" * 40


def _run(payload: str) -> tuple[int, str]:
    error_stream = io.StringIO()
    result = push_policy.run(io.StringIO(payload), error_stream)
    return result, error_stream.getvalue()


def test_feature_branch_update_is_allowed() -> None:
    code, diagnostic = _run(
        f"refs/heads/feat/example {OID_A} refs/heads/feat/example {ZERO_OID}\n"
    )
    assert code == 0
    assert diagnostic == ""


def test_direct_main_update_is_denied() -> None:
    code, diagnostic = _run(f"refs/heads/main {OID_A} refs/heads/main {OID_B}\n")
    assert code == 1
    assert diagnostic == "QE_MAIN_UPDATE_DENIED\n"


def test_forced_main_update_is_denied_without_needing_force_inference() -> None:
    code, diagnostic = _run(f"refs/heads/recovery {OID_B} refs/heads/main {OID_A}\n")
    assert code == 1
    assert diagnostic == "QE_MAIN_UPDATE_DENIED\n"


def test_main_deletion_is_denied() -> None:
    code, diagnostic = _run(f"(delete) {ZERO_OID} refs/heads/main {OID_A}\n")
    assert code == 1
    assert diagnostic == "QE_MAIN_DELETE_DENIED\n"


def test_any_main_update_denies_a_multi_ref_push() -> None:
    code, diagnostic = _run(
        "".join(
            (
                f"refs/heads/feat/example {OID_A} refs/heads/feat/example {ZERO_OID}\n",
                f"refs/heads/main {OID_B} refs/heads/main {OID_A}\n",
            )
        )
    )
    assert code == 1
    assert diagnostic == "QE_MAIN_UPDATE_DENIED\n"


def test_malformed_input_fails_closed_without_echoing_it() -> None:
    secret_bearing_input = "https://token-value@example.invalid malformed"
    code, diagnostic = _run(secret_bearing_input)
    assert code == 2
    assert diagnostic == "PUSH_POLICY_INPUT_INVALID\n"
    assert "token-value" not in diagnostic


def test_empty_push_set_is_allowed() -> None:
    assert _run("") == (0, "")


def test_hook_does_not_forward_remote_name_or_url() -> None:
    hook = (REPOSITORY_ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8")
    assert '"$1"' not in hook
    assert '"$2"' not in hook
    assert "check_push_policy.py" in hook
    assert ".venv/Scripts/python.exe" in hook
    assert ".venv/bin/python" in hook
    assert "C:/Tools/quant-engine/.venv/Scripts/python.exe" in hook
    assert "QE_PUSH_RUNTIME_UNAVAILABLE" in hook
    assert "UV_PYTHON=" not in hook


def test_hook_line_endings_are_pinned_to_lf() -> None:
    attributes = (REPOSITORY_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "/.githooks/* text eol=lf" in attributes.splitlines()
