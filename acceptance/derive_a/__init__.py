"""derivation_a: standalone expected values for the WS5 acceptance oracle.

This package is an independent reference implementation. It must never import
repository production code (``tools/``, ``packages/``) and never read any report,
panel, or generation output. It reads only the upstream inputs named in
``derive.py`` plus definitions under ``contracts/``.
"""

SCHEMA = "qe-cp-derivation/1"
DERIVATION = "a"
