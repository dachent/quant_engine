"""One label for every series: layer, entity, window, basis (sprint #897/#902/#903).

The owner's three-layer rule, kept apart in code: BACKTEST, MODEL_LIVE and the
owner's own ACCOUNT_LIVE are never collapsed, and a P123 COPY_RECORD (the copy
P123 keeps of the owner following a model) is a diagnostic that is never a series.
Design: records/sprint/LAYER-DESIGN.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Mapping, Sequence

BACKTEST: Final = "BACKTEST"
MODEL_LIVE: Final = "MODEL_LIVE"
ACCOUNT_LIVE: Final = "ACCOUNT_LIVE"
COPY_RECORD: Final = "COPY_RECORD"
LAYERS: Final = (BACKTEST, MODEL_LIVE, ACCOUNT_LIVE, COPY_RECORD)
# COPY_RECORD may describe a bridge stage but can never be part of a series.
SERIES_LAYERS: Final = (BACKTEST, MODEL_LIVE, ACCOUNT_LIVE)

ENTITY_ROLES: Final = frozenset(
    {
        "SIM_TWIN",
        "MODEL_BOOK",
        "DESIGNER_MODEL",
        "PRODUCT_NAV",
        "VENDOR_SERIES",
        "VENDOR_REPLICA",
        "ACCOUNT",
        "SLEEVE",
        "COPY",
    }
)
# The only user-facing wording for a layer.
LABEL_TEXT: Final = {
    BACKTEST: "Backtest (hypothetical, before model live)",
    MODEL_LIVE: "Model live (provider record, without my trades)",
    ACCOUNT_LIVE: "My live (my account: fills, fees, timing)",
    COPY_RECORD: "P123 copy (diagnostic)",
}
FORBIDDEN_WORDS: Final = ("provider-live book", "provider live book")
MIN_HEADLINE_DAYS: Final = 365
_COST_BASES: Final = frozenset({"GROSS", "NET"})
# Roles that can legitimately carry each layer.
_ROLES_BY_LAYER: Final = {
    BACKTEST: {"SIM_TWIN", "DESIGNER_MODEL", "VENDOR_SERIES", "VENDOR_REPLICA"},
    MODEL_LIVE: {"MODEL_BOOK", "DESIGNER_MODEL", "PRODUCT_NAV", "VENDOR_SERIES"},
    ACCOUNT_LIVE: {"ACCOUNT", "SLEEVE"},
    COPY_RECORD: {"COPY"},
}


class SeriesLabelError(ValueError):
    """A series label breaks the layer rule."""


@dataclass(frozen=True)
class SeriesLabel:
    layer: str
    entity_id: str
    entity_role: str
    first: str
    last: str
    observations: int
    currency: str
    return_basis: str
    cost_basis: str = "NET"
    leverage_factor: int = 1

    def __post_init__(self) -> None:
        if self.layer not in LAYERS:
            raise SeriesLabelError(f"unknown layer {self.layer!r}")
        if self.entity_role not in ENTITY_ROLES:
            raise SeriesLabelError(f"unknown entity role {self.entity_role!r}")
        if self.entity_role not in _ROLES_BY_LAYER[self.layer]:
            raise SeriesLabelError(
                f"role {self.entity_role} cannot carry layer {self.layer}"
            )
        if not self.entity_id.strip():
            raise SeriesLabelError("a series label needs an entity id")
        if self.cost_basis not in _COST_BASES:
            raise SeriesLabelError(f"unknown cost basis {self.cost_basis!r}")
        if self.leverage_factor < 1:
            raise SeriesLabelError("leverage factor is at least 1")
        if self.observations < 1:
            raise SeriesLabelError("a labelled series has observations")
        if date.fromisoformat(self.first) > date.fromisoformat(self.last):
            raise SeriesLabelError("series window is reversed")
        if not self.currency.strip() or not self.return_basis.strip():
            raise SeriesLabelError("a series label states currency and return basis")

    @property
    def label_text(self) -> str:
        text = LABEL_TEXT[self.layer]
        if self.entity_role == "VENDOR_REPLICA" and self.leverage_factor > 1:
            text += f" - {self.leverage_factor}x replica"
        if self.cost_basis == "GROSS":
            text += " - gross of fees"
        return text

    @property
    def window_days(self) -> int:
        return (date.fromisoformat(self.last) - date.fromisoformat(self.first)).days

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "entity_id": self.entity_id,
            "entity_role": self.entity_role,
            "window": {
                "first": self.first,
                "last": self.last,
                "observations": self.observations,
            },
            "basis": {
                "return_basis": self.return_basis,
                "cost_basis": self.cost_basis,
                "currency": self.currency,
                "leverage_factor": self.leverage_factor,
            },
            "label_text": self.label_text,
        }


def label_from_dict(raw: Mapping[str, Any]) -> SeriesLabel:
    window, basis = raw["window"], raw["basis"]
    label = SeriesLabel(
        layer=raw["layer"],
        entity_id=str(raw["entity_id"]),
        entity_role=raw["entity_role"],
        first=window["first"],
        last=window["last"],
        observations=int(window["observations"]),
        currency=basis["currency"],
        return_basis=basis["return_basis"],
        cost_basis=basis["cost_basis"],
        leverage_factor=int(basis["leverage_factor"]),
    )
    if raw.get("label_text") != label.label_text:
        raise SeriesLabelError("label text differs from the fixed wording")
    return label


def require_series(label: SeriesLabel) -> SeriesLabel:
    """A COPY_RECORD is a diagnostic and never enters a series."""

    if label.layer not in SERIES_LAYERS:
        raise SeriesLabelError(f"{label.layer} can never enter a series")
    return label


def headline_allowed(segments: Sequence[SeriesLabel]) -> bool:
    """May an annualised return, CAGR or alpha be the headline of this series?

    `segments` are the labelled segments the series is stitched from. Never when
    any segment is BACKTEST (hypothetical) or a COPY_RECORD, and never when the
    stitched window (first to last date of the segments) is under one year.
    """

    if not segments or any(
        label.layer not in {MODEL_LIVE, ACCOUNT_LIVE} for label in segments
    ):
        return False
    first = min(date.fromisoformat(label.first) for label in segments)
    last = max(date.fromisoformat(label.last) for label in segments)
    return (last - first).days >= MIN_HEADLINE_DAYS


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return []


def check_block(block: Mapping[str, Any]) -> list[str]:
    """Problems with one payload block: one labelled series, fixed wording."""

    labels = block.get("series_labels")
    if not isinstance(labels, list) or len(labels) != 1:
        return ["block must carry exactly one series label"]
    try:
        require_series(label_from_dict(labels[0]))
    except (SeriesLabelError, KeyError, TypeError, ValueError) as exc:
        return [f"invalid series label: {exc}"]
    return [
        f"forbidden wording: {word}"
        for text in _strings(block)
        for word in forbidden_wording(text)
    ]


def forbidden_wording(text: str) -> list[str]:
    lowered = text.lower()
    return [word for word in FORBIDDEN_WORDS if word in lowered]
