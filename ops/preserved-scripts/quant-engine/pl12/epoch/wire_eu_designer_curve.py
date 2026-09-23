"""Teach the strategy-history builder to read the EU designer model's curve.

`p123-eu` has carried `NO_EXACT_MODEL_CAPTURE_BEFORE_PROVIDER_LAUNCH` because
the retained Portfolio123 generation holds the owner's subscribed copy, whose
curve starts when the copy was created. The designer model the copy follows
publishes its own curve from 2008, and that capture is now a governed
generation, so the builder is given a way to cite it -- the same way it already
cites the Robuxio provider track record.

Only the segment strictly before the provider's stated launch is admitted. The
670 observations the model ran live before the owner funded anything are a
third layer, neither backtest nor owner history, so they are counted in the
disposition and left out of the bundle rather than relabelled.

Bytes are written directly so no newline translation can alter the file.
"""
import os
from pathlib import Path

REPOSITORY = Path(
    r'C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine'
    r'/.codex-bootstrap/mvp2-execution/repo'
)
# The vertical build holds the repository while it runs, so the same patch is
# first applied to a scratch copy and only later to the file itself.
TARGET = Path(
    os.environ.get('PATCH_TARGET')
    or REPOSITORY / 'tools' / 'portfolio_strategy_history.py'
)

READER = '''

def _p123_eu_designer_curve(
    generation_root: Path, strategy_version: str
) -> tuple[list[dict[str, str]], dict[str, object]]:
    """Read the governed designer-model generation as the EU design history.

    The generation retains the provider's own published curve for the model the
    owner subscribes to, split at the launch the provider states on its own
    page, with a manifest digest over the returns. Admitting it is a digest
    check plus a relabel of the pre-launch segment -- no return is recomputed.

    The model publishes in EUR and the owner holds it in a USD account, and the
    two series differ by about one percent a day over their overlap, which is
    implementation and currency together. That is why this is design history in
    its own layer and not a longer version of the owner's series.
    """

    manifest_path = generation_root / "generation.json"
    returns_path = generation_root / "returns.csv"
    catalog_path = generation_root / "catalog.json"
    for candidate in (manifest_path, returns_path, catalog_path):
        if candidate.is_symlink() or not candidate.is_file():
            raise StrategyHistoryError(
                f"EU designer-model artifact is not a regular file: {candidate.name}"
            )
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get("verification") != "VERIFIED":
        raise StrategyHistoryError("EU designer-model generation is not verified")
    if str(manifest.get("generation_id")) != generation_root.name:
        raise StrategyHistoryError(
            "EU designer-model generation identity differs from its directory"
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
            "EU designer-model returns differ from the generation manifest"
        )
    catalog = json.loads(catalog_path.read_bytes())
    series = catalog.get("series")
    if not isinstance(series, dict):
        raise StrategyHistoryError("EU designer-model catalog series differs")

    rows: list[dict[str, str]] = []
    model_live = 0
    reader = csv.DictReader(io.StringIO(returns_bytes.decode("utf-8")))
    for row in reader:
        layer = row.get("evidence_layer")
        if layer == "PROVIDER_MODEL_LIVE":
            model_live += 1
            continue
        if layer != "BACKTEST":
            raise StrategyHistoryError(
                "EU designer-model carries an unexpected evidence layer"
            )
        observed = _date_value(row.get("date"), "EU designer-model date")
        value = _decimal(row.get("return"), "EU designer-model return")
        if value <= -1:
            raise StrategyHistoryError("EU designer-model return is invalid")
        rows.append(
            {
                "series_id": "EU [P123]",
                "date": observed.isoformat(),
                "return": format(value.normalize(), "f"),
                "evidence_layer": "BACKTEST",
                "strategy_version": strategy_version,
            }
        )
    if len(rows) < 2:
        raise StrategyHistoryError("EU designer-model history is insufficient")
    dates = [row["date"] for row in rows]
    if dates != sorted(set(dates)):
        raise StrategyHistoryError("EU designer-model calendar is not ordered")
    provider_launch = str(series.get("provider_launch") or "")
    if not provider_launch or dates[-1] >= provider_launch:
        raise StrategyHistoryError(
            "EU designer-model prehistory reaches the provider launch"
        )
    definition: dict[str, object] = {
        "compatibility_basis": "OWNER_LAYER_RULING_DESIGNER_MODEL_IS_PRODUCT_DESIGN_HISTORY",
        "coverage_disposition": "COMPLETE_STRICTLY_BEFORE_PROVIDER_LAUNCH",
        "frequency": "DAILY_TRADING",
        "history_status": "ACCEPTED_CURRENT_SPECIFICATION_HISTORY",
        "implementation_reconciliation_status": (
            "MODEL_TO_ACCOUNT_RECONCILIATION_RETAINED_SEPARATELY"
        ),
        "method": "PROVIDER_PUBLISHED_MODEL_EQUITY_CURVE_TO_DAILY_RETURN",
        "observed_end": dates[-1],
        "observed_start": dates[0],
        "provider_currency": str(series.get("currency") or ""),
        "provider_launch": provider_launch,
        "provider_model_live_observations_not_admitted": model_live,
        "provider_revision_dates": list(series.get("provider_revision_dates") or []),
        "required_for_analysis": True,
        "return_observations": len(rows),
        "series_id": "EU [P123]",
        "source_artifacts": [
            {
                "artifact_id": "returns.csv",
                "sha256": artifact["sha256"],
                "size_bytes": artifact["size_bytes"],
                "source_id": "portfolio123-designer-model-curve-generation",
            }
        ],
        "strategy_version": strategy_version,
    }
    return rows, definition

'''

EDITS = (
    (
        'reader placed before the existing EU prehistory helper',
        '\n\ndef _p123_eu_backtest(\n',
        READER + '\ndef _p123_eu_backtest(\n',
    ),
    (
        'build_bundle signature',
        '    robuxio_track_record_generation: Path | None = None,\n'
        '    robuxio_strategy_version: str | None = None,\n',
        '    robuxio_track_record_generation: Path | None = None,\n'
        '    robuxio_strategy_version: str | None = None,\n'
        '    p123_eu_designer_model_generation: Path | None = None,\n'
        '    p123_eu_strategy_version: str | None = None,\n',
    ),
    (
        'designer part built beside the Robuxio part',
        '    p123_dispositions: list[dict[str, object]] = []\n',
        '    p123_eu_designer_part: tuple[list[dict[str, str]], dict[str, object]] | None = (\n'
        '        None\n'
        '    )\n'
        '    if (p123_eu_designer_model_generation is None) != (\n'
        '        p123_eu_strategy_version is None\n'
        '    ):\n'
        '        raise StrategyHistoryError(\n'
        '            "EU designer-model generation and strategy version are one input"\n'
        '        )\n'
        '    if p123_eu_designer_model_generation is not None:\n'
        '        p123_eu_designer_part = _p123_eu_designer_curve(\n'
        '            p123_eu_designer_model_generation, str(p123_eu_strategy_version)\n'
        '        )\n'
        '    p123_dispositions: list[dict[str, object]] = []\n',
    ),
    (
        'designer part preferred over the copy-derived prehistory',
        '    if portfolio123_generation_root is not None:\n'
        '        p123_dispositions, p123_eu_part = _p123_history_dispositions(\n'
        '            portfolio123_generation_root\n'
        '        )\n',
        '    if portfolio123_generation_root is not None:\n'
        '        p123_dispositions, p123_eu_part = _p123_history_dispositions(\n'
        '            portfolio123_generation_root\n'
        '        )\n'
        '    if p123_eu_designer_part is not None:\n'
        '        # The copy-derived prehistory only ever existed as a fallback for a\n'
        '        # capture that had not been taken. Now that the designer model the\n'
        '        # copy follows is a governed generation, it is the authority for the\n'
        '        # design layer and the fallback is not used alongside it.\n'
        '        p123_eu_part = p123_eu_designer_part\n',
    ),
    (
        'CLI arguments',
        '    parser.add_argument("--robuxio-strategy-version")\n',
        '    parser.add_argument("--robuxio-strategy-version")\n'
        '    parser.add_argument("--p123-eu-designer-model-generation", type=Path)\n'
        '    parser.add_argument("--p123-eu-strategy-version")\n',
    ),
    (
        'main() pass-through',
        '            robuxio_strategy_version=args.robuxio_strategy_version,\n',
        '            robuxio_strategy_version=args.robuxio_strategy_version,\n'
        '            p123_eu_designer_model_generation=(\n'
        '                args.p123_eu_designer_model_generation\n'
        '            ),\n'
        '            p123_eu_strategy_version=args.p123_eu_strategy_version,\n',
    ),
)


def main():
    content = TARGET.read_bytes().decode('utf-8')
    if '\r\n' in content:
        raise SystemExit('refusing to edit a file that already carries CRLF')
    for label, anchor, replacement in EDITS:
        occurrences = content.count(anchor)
        if occurrences != 1:
            raise SystemExit('anchor is not unique (%d): %s' % (occurrences, label))
        content = content.replace(anchor, replacement, 1)
        print('applied: %s' % label)
    TARGET.write_bytes(content.encode('utf-8'))
    print('wrote %s (%d bytes)' % (TARGET.name, len(content.encode('utf-8'))))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
