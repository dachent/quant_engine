from __future__ import annotations


RUNTIME_BOUNDARY_UNAVAILABLE_CODE = "SCHWAB_RUNTIME_BOUNDARY_UNAVAILABLE"
RUNTIME_BOUNDARY_UNAVAILABLE_RETURN_CODE = 78


class RuntimeBoundaryUnavailableError(RuntimeError):
    """The trusted Windows broker/VFS required for this operation is absent."""

    code = RUNTIME_BOUNDARY_UNAVAILABLE_CODE

    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__(self.code)


def require_runtime_boundary(operation: str) -> None:
    """Fail before a production operation can reach a pathname-backed effect."""

    raise RuntimeBoundaryUnavailableError(operation)
