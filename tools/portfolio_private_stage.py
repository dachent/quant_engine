"""Validate private report/panel publication roots against governed production roots."""

from __future__ import annotations

from pathlib import Path

from portfolio_pipeline_materialize import PRODUCTION_ROOTS


class PrivateStageError(ValueError):
    """A private output target is absent, misbound, or overlaps production."""


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_private_stage_target(
    target: Path,
    declared_private_stage_root: Path,
    portfolio_root: Path,
) -> Path:
    """Return an exact private target only when it is disjoint from production."""

    resolved_target = target.resolve()
    declared = declared_private_stage_root.resolve()
    if resolved_target != declared:
        raise PrivateStageError("publication target differs from private stage root")
    for relative in PRODUCTION_ROOTS.values():
        production = (portfolio_root.resolve() / relative).resolve()
        if _contains(resolved_target, production) or _contains(
            production, resolved_target
        ):
            raise PrivateStageError("private stage target overlaps production root")
    return resolved_target
