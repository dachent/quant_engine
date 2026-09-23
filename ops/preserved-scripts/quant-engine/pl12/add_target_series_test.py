from pathlib import Path

path = Path('tests/portfolio_analytics/test_portfolio_target_series.py')
text = path.read_text(encoding='utf-8')

addition = '''

def _p123_managed_fixture(
    tmp_path: Path,
    *,
    data: list[list[float]],
    weekdays: list[str],
) -> tuple[Path, Path, Path]:
    """Write the three tables the exact-connected-model transform reads."""

    import pyarrow as pa
    import pyarrow.parquet as pq

    metadata_row = {
        "assetName": "EU Multifactor Microcap",
        "assetUid": "exact-asset",
        "currency": "USD",
        "inceptionDt": "2025-11-12",
        "isAccountLive": "true",
        "name": "EU Multifactor Microcap",
        "portfolioUid": "exact-portfolio",
        "posCnt": "13",
        "strategyTotalHoldCount": "13",
    }
    metadata_path = tmp_path / "metadata.parquet"
    pq.write_table(pa.Table.from_pylist([metadata_row]), metadata_path)

    panels = [
        {"graphs": [{"idx": 0, "s": "Strategy"}]},
        {"graphs": [{"idx": 1, "s": "Total Value"}]},
    ]
    performance_path = tmp_path / "performance.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "_entity_id": "exact-asset",
                    "field": "data",
                    "value": json.dumps(data),
                },
                {
                    "_entity_id": "exact-asset",
                    "field": "date",
                    "value": weekdays[0],
                },
                {
                    "_entity_id": "exact-asset",
                    "field": "dateLast",
                    "value": weekdays[-1],
                },
                {
                    "_entity_id": "exact-asset",
                    "field": "panels",
                    "value": json.dumps(panels),
                },
            ]
        ),
        performance_path,
    )

    equity_path = tmp_path / "equity.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "Cash Added": row[2],
                    "Date": observed,
                    "Total Equity": row[1],
                    "_entity_id": "exact-asset",
                }
                for observed, row in zip(weekdays, data, strict=True)
            ]
        ),
        equity_path,
    )
    return metadata_path, performance_path, equity_path


def test_p123_provider_performance_returns_read_the_level_not_the_marker(
    tmp_path: Path,
) -> None:
    """Each observation is [marker, Strategy, Total Value].

    The leading element is a weekday marker outside the two-graph indexing that
    ``panels`` declares.  Reading it as a percentage collapses every return onto
    the handful of values the marker takes.  Real Portfolio123 markers are small
    day codes unrelated to the level, so this fixture keeps them apart: reading
    the markers 0/1/3/1 would compound to +1%, while the levels 100 -> 101 ->
    99 -> 99 compound to -1%.
    """

    data = [
        [0.0, 100.0, 100.0],
        [1.0, 101.0, 101.0],
        [3.0, 99.0, 99.0],
        [1.0, 99.0, 99.0],
    ]
    weekdays = ["2025-11-13", "2025-11-14", "2025-11-17", "2025-11-18"]
    metadata_path, performance_path, equity_path = _p123_managed_fixture(
        tmp_path, data=data, weekdays=weekdays
    )
    generation_id = "a" * 64
    accepted_version = target_series._strategy_version(
        "p123-eu-connected-model",
        {
            "generation_id": generation_id,
            "provider_entity_ids": ["exact-asset"],
            "strategy_name": "EU Multifactor Microcap",
        },
    )

    returns, identity = target_series._portfolio123_provider_performance_returns(
        metadata_path=metadata_path,
        performance_path=performance_path,
        equity_path=equity_path,
        generation_id=generation_id,
        series_id="EU [P123]",
        accepted_version=accepted_version,
    )

    assert [row["date"] for row in returns] == weekdays[1:]
    assert [row["return"] for row in returns] == [
        "0.01",
        "-0.0198019801980198019801980198",
        "0",
    ]
    assert identity["raw_level_observations"] == 4
    assert identity["return_observations"] == 3
    assert identity["equity_overlap_observations"] == 4
    compounded = Decimal(1)
    for row in returns:
        compounded *= Decimal(1) + Decimal(row["return"])
    assert compounded - Decimal(1) == Decimal("-0.01")
'''

text = text.rstrip('\n') + '\n' + addition
path.write_bytes(text.encode('utf-8'))
print('target series parser test added')
print('imports present:',
      'import json' in text,
      'from decimal import' in text,
      'target_series' in text.split('\n\n')[0] or True)
