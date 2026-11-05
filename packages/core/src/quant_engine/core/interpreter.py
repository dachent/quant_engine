"""The interpreter the release pipeline's numbers are reproducible on.

Every figure that is hashed into a release pin, a seed or a generation id is computed in floating point. CPython
3.12 made the built-in ``sum()`` of floats compensated, so the same inputs give different last digits on 3.12 and
later than on 3.11 (R3 pass 6e: an attribution built on 3.13 differed from the 3.11 one in five fields, for example
``0.9795932641808844`` against ``0.9795932641808839``). ``requires-python`` admits 3.11 to 3.13 for development and
CI proves only that the tests pass on 3.11; nothing else stops an operator from running a pin-feeding build with
another interpreter, so the tools and stages that compute such numbers refuse to start on one.
"""

from __future__ import annotations

from collections.abc import Sequence
import sys
from typing import Final

REQUIRED_VERSION: Final = (3, 11)


class InterpreterRefusal(RuntimeError):
    """The interpreter is not the one the pinned numbers are reproducible on."""


def require(
    version: Sequence[int],
    *,
    what: str,
    executable: str | None = None,
    required: tuple[int, int] = REQUIRED_VERSION,
) -> None:
    """Refuse unless ``version`` (major, minor, ...) is the required major and minor."""

    if tuple(version[:2]) == required:
        return
    found = ".".join(str(part) for part in version)
    where = f" ({executable})" if executable else ""
    raise InterpreterRefusal(
        f"{what} computes numbers that are hashed into release pins and is reproducible only on "
        f"Python {required[0]}.{required[1]}: found Python {found}{where}"
    )


def require_current(what: str) -> None:
    """Refuse unless the running interpreter is the required one."""

    require(tuple(sys.version_info[:3]), what=what, executable=sys.executable)
