"""Compatibility entry point for the Portfolio123 quarantine profile."""

from __future__ import annotations

from collections.abc import Callable
import importlib.util
from pathlib import Path
import sys
from typing import cast, Sequence


MODULE_PATH = Path(__file__).with_name("provider_security_quarantine.py")
SPEC = importlib.util.spec_from_file_location(
    "provider_security_quarantine", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("unable to load provider security quarantine")
_MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = _MODULE
SPEC.loader.exec_module(_MODULE)
_PROVIDER_MAIN = cast(Callable[[Sequence[str] | None], int], getattr(_MODULE, "main"))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "plan":
        arguments[0] = "plan-p123"
    return _PROVIDER_MAIN(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
