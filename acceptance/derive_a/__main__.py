"""Entry point for ``python acceptance/derive_a`` and ``python -m derive_a``."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Run as a directory script: make ``derive_a`` importable as a package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    package = "derive_a"
else:
    package = __package__

raise SystemExit(importlib.import_module(f"{package}.cli").main())
