"""Error taxonomy for the TuringTrader capture package."""

from __future__ import annotations


class TuringTraderError(RuntimeError):
    """Base class for all fail-closed capture errors."""


class ConfigError(TuringTraderError):
    """The deployment-local token config file is missing or malformed."""


class TransportPolicyError(TuringTraderError):
    """A request was rejected locally before any network I/O."""


class TransportError(TuringTraderError):
    """A sanitized transport failure safe for logs."""


class SchemaValidationError(TuringTraderError):
    """A response payload drifted from its versioned JSON Schema."""


class NormalizationError(TuringTraderError):
    """A schema-valid payload failed semantic normalization."""


class RawStoreError(TuringTraderError):
    """Immutable run storage was violated."""


class ReplayError(TuringTraderError):
    """Deterministic replay detected a mismatch or tamper."""
