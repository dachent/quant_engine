"""Read-only Interactive Brokers provider contracts."""

from quant_engine.ibkr.flex import (
    FlexDailyStatement,
    FlexDocument,
    FlexFetchResult,
    FlexValidationError,
    FlexWebServiceClient,
    parse_flex_file,
)
from quant_engine.ibkr.generation import (
    IbkrGenerationError,
    build_generation,
    stage_generation_candidate,
)
from quant_engine.ibkr.tws import (
    TwsExecution,
    TwsPosition,
    TwsReadSource,
    TwsReconciliation,
    TwsSnapshot,
    reconcile_tws_snapshot,
)

__version__ = "0.1.0"

__all__ = [
    "FlexDailyStatement",
    "FlexDocument",
    "FlexFetchResult",
    "FlexValidationError",
    "FlexWebServiceClient",
    "IbkrGenerationError",
    "TwsExecution",
    "TwsPosition",
    "TwsReadSource",
    "TwsReconciliation",
    "TwsSnapshot",
    "build_generation",
    "parse_flex_file",
    "reconcile_tws_snapshot",
    "stage_generation_candidate",
]
