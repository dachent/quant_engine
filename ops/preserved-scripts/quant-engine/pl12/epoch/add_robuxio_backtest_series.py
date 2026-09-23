"""Admit the Robuxio published track record as that product's design history.

The owner's three-layer ruling for this strategy is explicit: the Robuxio site
series is the BACKTEST layer, the Matrixport product is model-live and then the
owner's own live at the funding boundary. The site series has been captured as
a governed generation, so the only thing still holding it outside
LIVE_PLUS_BACKTEST was that nothing routed it into the backtest channel.

It is routed through the same channel as every other vendor history: this
builder reads the governed generation, verifies its returns against the
generation manifest, and emits one series that the caller then truncates
strictly before the strategy's own funding boundary via `--live-inception`.

The product version is supplied rather than minted. Every other series here
derives its version from the retained source bytes, but this strategy's version
is the Matrixport product identity the contract already asserts on the account
path, and the owner's layer ruling is what binds the published series to it.
"""
from pathlib import Path

REPO = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
path = REPO / 'tools/portfolio_strategy_history.py'
raw = path.read_bytes()
BS = chr(92).encode()

reader = b'''

def _robuxio_track_record(
    generation_root: Path, strategy_version: str
) -> tuple[list[dict[str, str]], dict[str, object]]:
    """Read the governed provider track-record generation as design history.

    The generation retains the provider's published return path with its own
    manifest digest, so admitting it is a digest check plus a relabel of the
    evidence layer -- no return is recomputed here.
    """

    manifest_path = generation_root / "generation.json"
    returns_path = generation_root / "returns.csv"
    for candidate in (manifest_path, returns_path):
        if candidate.is_symlink() or not candidate.is_file():
            raise StrategyHistoryError(
                f"Robuxio track-record artifact is not a regular file: {candidate.name}"
            )
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get("verification") != "VERIFIED":
        raise StrategyHistoryError("Robuxio track-record generation is not verified")
    if str(manifest.get("generation_id")) != generation_root.name:
        raise StrategyHistoryError(
            "Robuxio track-record generation identity differs from its directory"
        )
    artifacts = {
        str(row.get("artifact_id")): row for row in manifest.get("artifacts") or []
    }
    returns_bytes = returns_path.read_bytes()
    artifact = artifacts.get("returns.csv")
    if (
        not isinstance(artifact, dict)
        or artifact.get("sha256") != hashlib.sha256(returns_bytes).hexdigest()
        or artifact.get("size_bytes") != len(returns_bytes)
    ):
        raise StrategyHistoryError(
            "Robuxio track-record returns differ from the generation manifest"
        )

    rows: list[dict[str, str]] = []
    reader = csv.DictReader(io.StringIO(returns_bytes.decode("utf-8")))
    for row in reader:
        if row.get("evidence_layer") != "PROVIDER_TRACK_RECORD":
            raise StrategyHistoryError(
                "Robuxio track-record carries an unexpected evidence layer"
            )
        observed = _date_value(row.get("date"), "Robuxio track-record date")
        value = _decimal(row.get("return"), "Robuxio track-record return")
        if value <= -1:
            raise StrategyHistoryError("Robuxio track-record return is invalid")
        rows.append(
            {
                "series_id": "Robuxio Crypto (High Sharpe High Vol)",
                "date": observed.isoformat(),
                "return": format(value.normalize(), "f"),
                "evidence_layer": "BACKTEST",
                "strategy_version": strategy_version,
            }
        )
    if len(rows) < 2:
        raise StrategyHistoryError("Robuxio track-record history is insufficient")
    dates = [row["date"] for row in rows]
    if dates != sorted(set(dates)):
        raise StrategyHistoryError("Robuxio track-record calendar is not ordered")
    definition: dict[str, object] = {
        "compatibility_basis": "OWNER_LAYER_RULING_SITE_SERIES_IS_PRODUCT_DESIGN_HISTORY",
        "coverage_disposition": "COMPLETE_WITHIN_RETAINED_CAPTURE_BOUNDARY",
        "frequency": "DAILY_CONTINUOUS",
        "history_status": "ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
        "implementation_reconciliation_status": (
            "MODEL_TO_ACCOUNT_RECONCILIATION_RETAINED_SEPARATELY"
        ),
        "method": "PROVIDER_PUBLISHED_CUMULATIVE_RETURN_TO_DAILY_RETURN",
        "observed_end": dates[-1],
        "observed_start": dates[0],
        "required_for_analysis": True,
        "return_observations": len(rows),
        "series_id": "Robuxio Crypto (High Sharpe High Vol)",
        "source_artifacts": [
            {
                "artifact_id": "returns.csv",
                "sha256": artifact["sha256"],
                "size_bytes": artifact["size_bytes"],
                "source_id": "robuxio-published-track-record-generation",
            }
        ],
        "strategy_version": strategy_version,
    }
    return rows, definition
'''

anchor = b'''

def _p123_eu_backtest('''
assert raw.count(anchor) == 1
raw = raw.replace(anchor, reader + anchor, 1)

# Accept the generation root and the product version the contract asserts.
old_signature = b'''    portfolio123_generation_root: Path | None = None,
    live_inceptions: Mapping[str, str] | None = None,
) -> tuple[bytes, bytes]:'''
new_signature = b'''    portfolio123_generation_root: Path | None = None,
    robuxio_track_record_generation: Path | None = None,
    robuxio_strategy_version: str | None = None,
    live_inceptions: Mapping[str, str] | None = None,
) -> tuple[bytes, bytes]:'''
assert raw.count(old_signature) == 1
raw = raw.replace(old_signature, new_signature)

old_parts = b'''        *((p123_eu_part,) if p123_eu_part is not None else ()),
    )'''
new_parts = b'''        *((p123_eu_part,) if p123_eu_part is not None else ()),
        *((robuxio_part,) if robuxio_part is not None else ()),
    )'''
assert raw.count(old_parts) == 1
raw = raw.replace(old_parts, new_parts)

old_prelude = b'''    p123_dispositions: list[dict[str, object]] = []'''
new_prelude = b'''    robuxio_part: tuple[list[dict[str, str]], dict[str, object]] | None = None
    if (robuxio_track_record_generation is None) != (robuxio_strategy_version is None):
        raise StrategyHistoryError(
            "Robuxio track-record generation and product version are one input"
        )
    if robuxio_track_record_generation is not None:
        robuxio_part = _robuxio_track_record(
            robuxio_track_record_generation, str(robuxio_strategy_version)
        )
    p123_dispositions: list[dict[str, object]] = []'''
assert raw.count(old_prelude) == 1
raw = raw.replace(old_prelude, new_prelude)

old_arg = b'''    parser.add_argument("--portfolio123-generation-root", type=Path)'''
new_arg = b'''    parser.add_argument("--portfolio123-generation-root", type=Path)
    parser.add_argument("--robuxio-track-record-generation", type=Path)
    parser.add_argument("--robuxio-strategy-version")'''
assert raw.count(old_arg) == 1
raw = raw.replace(old_arg, new_arg)

old_call = b'''            portfolio123_generation_root=args.portfolio123_generation_root,'''
new_call = b'''            portfolio123_generation_root=args.portfolio123_generation_root,
            robuxio_track_record_generation=args.robuxio_track_record_generation,
            robuxio_strategy_version=args.robuxio_strategy_version,'''
assert raw.count(old_call) == 1
raw = raw.replace(old_call, new_call)

path.write_bytes(raw)
print('ok')
