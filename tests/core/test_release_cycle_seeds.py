"""The per-cycle seeds come from the pinned release-inputs root and the cycle's baseline, never by hand.

Synthetic fixtures only: a throwaway root holding the template, a return-source CSV, and series
dictionaries standing in for the baseline's ACCOUNT_LIVE rows.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quant_engine.core import ReleaseInputsError
from quant_engine.core.release_inputs import build_manifest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import release_cycle_seeds as seeds  # noqa: E402

CUTOFF = "2000-01-14"
AVAILABLE = "DESCRIPTIVE_EXACT_DATE_VERSION_UNRESOLVED"
CSV = b"series_id,date,return,evidence_layer,version_ref,source_artifact_id\n"
MANIFEST = b'{"verification": "VERIFIED", "as_of_date": "2000-01-14"}\n'


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _template() -> dict:
    row = {
        "strategy_id": "alpha",
        "status": AVAILABLE,
        "account_series_id": "account:alpha",
        "reference_series_id": "provider:alpha",
        "periods_per_year": 252,
        "observed_start": "1999-01-01",
        "observed_end": "1999-02-01",
        "expected_legacy_cumulative_gap": None,
    }
    other = {"strategy_id": "beta", "status": "UNAVAILABLE", "periods_per_year": 252}
    return {
        "schema_version": 2,
        "as_of_date": CUTOFF,
        "comparisons": [dict(row), dict(other)],
        "benchmark_comparisons": [
            {
                **row,
                "reference_series_id": "benchmark:b",
                "observed_start": None,
                "observed_end": None,
            },
            dict(other),
        ],
        "return_source": {
            "root_key": "data",
            "relative": "returns/attribution-returns.csv",
            "expected_sha256": _sha(CSV),
            "source_manifest_relative": "returns/source-manifest.json",
            "source_manifest_expected_sha256": _sha(MANIFEST),
        },
    }


@pytest.fixture
def world(tmp_path: Path) -> dict:
    data = tmp_path / "data"
    (data / "returns").mkdir(parents=True)
    (data / "returns" / "attribution-returns.csv").write_bytes(CSV)
    (data / "returns" / "source-manifest.json").write_bytes(MANIFEST)
    inputs = tmp_path / "inputs"
    files = {
        seeds.TEMPLATE_SOURCE: json.dumps(_template()).encode(),
    }
    for relative, body in files.items():
        (inputs / relative).parent.mkdir(parents=True, exist_ok=True)
        (inputs / relative).write_bytes(body)
    _, pin = build_manifest(inputs, list(files), cutoff=CUTOFF)
    return {
        "repo": tmp_path / "repo",
        "inputs": inputs,
        "pin": pin,
        "roots": {"data": data.resolve()},
        "cycle": tmp_path / "cycle",
        "files": files,
    }


def _stage(world: dict, **override) -> dict:
    kwargs = dict(
        repo_root=world["repo"],
        root=world["inputs"],
        manifest_sha256=world["pin"],
        cutoff=CUTOFF,
        roots=world["roots"],
        cycle=world["cycle"],
    )
    kwargs.update(override)
    return seeds.stage(**kwargs)


def test_stage_resolves_the_template_and_stages_no_implementation_file(
    world: dict,
) -> None:
    result = _stage(world)
    assert set(result) == {seeds.TEMPLATE_NAME}
    assert not (world["cycle"] / "in").exists()
    spec = json.loads((world["cycle"] / seeds.TEMPLATE_NAME).read_bytes())
    source = spec["return_source"]
    assert (
        source["path"]
        == (world["roots"]["data"] / "returns/attribution-returns.csv").as_posix()
    )
    assert source["expected_sha256"] == _sha(CSV)
    assert set(source) == {
        "expected_sha256",
        "path",
        "source_manifest_expected_sha256",
        "source_manifest_path",
    }
    assert all(value.startswith("written ") for value in result.values())
    assert set(_stage(world).values()) == {
        v.replace("written", "unchanged") for v in result.values()
    }


def test_stage_is_create_only(world: dict) -> None:
    _stage(world)
    target = world["cycle"] / seeds.TEMPLATE_NAME
    target.write_bytes(b"{}\n")
    with pytest.raises(seeds.SeedError, match="create-only"):
        _stage(world)


def test_stage_refuses_a_root_of_another_cutoff_and_a_tampered_seed(
    world: dict,
) -> None:
    with pytest.raises(ReleaseInputsError, match="this run is for"):
        _stage(world, cutoff="2000-02-04")
    (world["inputs"] / seeds.TEMPLATE_SOURCE).write_text("changed", encoding="utf-8")
    with pytest.raises(
        ReleaseInputsError,
        match="differs from the manifest|changed after it was verified",
    ):
        _stage(world)


def test_stage_refuses_a_return_source_whose_bytes_changed(world: dict) -> None:
    (world["roots"]["data"] / "returns/attribution-returns.csv").write_bytes(
        CSV + b"x\n"
    )
    with pytest.raises(seeds.SeedError, match="return source"):
        _stage(world)


def test_stage_refuses_an_undeclared_root_key_and_an_escaping_path(world: dict) -> None:
    with pytest.raises(seeds.SeedError, match="root key 'data' is not declared"):
        _stage(world, roots={})
    template = _template()
    template["return_source"]["relative"] = "../x.csv"
    with pytest.raises(seeds.SeedError, match="safe relative path"):
        seeds.resolve_template(template, world["roots"])


def _days(start: str, count: int) -> dict[date, Decimal]:
    first = date.fromisoformat(start)
    return {first + timedelta(days=i): Decimal("0.001") for i in range(count)}


def test_windows_are_the_first_and_last_shared_date_up_to_the_cutoff() -> None:
    account = {"alpha": _days("2000-01-03", 20)}
    references = {
        "provider:alpha": _days("2000-01-05", 4),
        "benchmark:b": _days("2000-01-01", 30),
    }
    spec, report = seeds.derive_windows(_template(), account, references, CUTOFF)
    row = spec["comparisons"][0]
    assert (row["observed_start"], row["observed_end"]) == ("2000-01-05", "2000-01-08")
    bench = spec["benchmark_comparisons"][0]
    # the benchmark runs past the cutoff; the window stops at it
    assert (bench["observed_start"], bench["observed_end"]) == ("2000-01-03", CUTOFF)
    assert spec["as_of_date"] == CUTOFF
    assert spec["comparisons"][1] == _template()["comparisons"][1]
    assert [r["strategy_id"] for r in report] == ["alpha", "alpha"]


def test_a_coarser_reference_takes_its_own_dates_inside_the_account_history() -> None:
    template = _template()
    template["comparisons"][0]["periods_per_year"] = 52
    reference = {
        date(2000, 1, 7): Decimal("0.01"),
        date(2000, 1, 14): Decimal("0.01"),
        date(2000, 1, 21): Decimal("0"),
    }
    spec, _ = seeds.derive_windows(
        template,
        {"alpha": _days("2000-01-03", 20)},
        {"provider:alpha": reference, "benchmark:b": _days("2000-01-03", 5)},
        CUTOFF,
    )
    row = spec["comparisons"][0]
    assert (row["observed_start"], row["observed_end"]) == ("2000-01-07", "2000-01-14")


def test_an_available_row_with_nothing_to_compare_refuses() -> None:
    references = {
        "provider:alpha": _days("2001-01-01", 3),
        "benchmark:b": _days("2000-01-03", 3),
    }
    with pytest.raises(seeds.SeedError, match="share 0 observation"):
        seeds.derive_windows(
            _template(), {"alpha": _days("2000-01-03", 5)}, references, CUTOFF
        )
    with pytest.raises(seeds.SeedError, match="holds no ACCOUNT_LIVE series"):
        seeds.derive_windows(_template(), {}, references, CUTOFF)
    with pytest.raises(seeds.SeedError, match="not in the return source"):
        seeds.derive_windows(_template(), {"alpha": _days("2000-01-03", 5)}, {}, CUTOFF)


def test_a_single_shared_observation_is_not_a_comparison() -> None:
    references = {
        "provider:alpha": {date(2000, 1, 5): Decimal("0.01")},
        "benchmark:b": _days("2000-01-03", 5),
    }
    with pytest.raises(seeds.SeedError, match="share 1 observation"):
        seeds.derive_windows(
            _template(), {"alpha": _days("2000-01-03", 20)}, references, CUTOFF
        )


def test_retained_history_is_used_only_when_the_consumer_authenticates_it() -> None:
    references = {
        "provider:alpha": _days("2000-01-05", 6),
        "benchmark:b": _days("2000-01-03", 5),
    }

    def accepted(strategy: str, account_id: str) -> dict[date, Decimal]:
        assert (strategy, account_id) == ("alpha", "account:alpha")
        return _days("2000-01-04", 10)

    spec, _ = seeds.derive_windows(_template(), {}, references, CUTOFF, accepted)
    assert spec["comparisons"][0]["observed_start"] == "2000-01-05"

    def refused(strategy: str, account_id: str) -> dict[date, Decimal]:
        raise seeds.attribution.AttributionValidationError(
            "retained account history identity differs"
        )

    with pytest.raises(seeds.SeedError, match="retained account history refused"):
        seeds.derive_windows(_template(), {}, references, CUTOFF, refused)


def test_stage_refuses_a_source_manifest_that_is_not_verified(world: dict) -> None:
    manifest = world["roots"]["data"] / "returns/source-manifest.json"
    for body in (
        b'{"verification": "PENDING", "as_of_date": "2000-01-14"}\n',
        b'{"verification": "VERIFIED"}\n',
        b"[]\n",
    ):
        manifest.write_bytes(body)
        template = _template()
        template["return_source"]["source_manifest_expected_sha256"] = _sha(body)
        with pytest.raises(seeds.SeedError, match="return source manifest"):
            seeds.resolve_template(template, world["roots"])


def test_stage_refuses_a_path_that_leaves_the_root_through_a_link(
    world: dict, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.csv").write_bytes(CSV)
    link = world["roots"]["data"] / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    template = _template()
    template["return_source"]["relative"] = "linked/x.csv"
    with pytest.raises(Exception, match="linked|symlink|link"):
        seeds.resolve_template(template, world["roots"])
    for relative in ("C:/x.csv", "a\\b.csv"):
        template["return_source"]["relative"] = relative
        with pytest.raises(seeds.SeedError, match="safe relative path"):
            seeds.resolve_template(template, world["roots"])


def test_an_attribution_validation_error_is_a_cli_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(**_: object) -> None:
        raise seeds.attribution.AttributionValidationError("return source is invalid")

    monkeypatch.setattr(seeds, "derive_attribution_spec", broken)
    code = seeds.main(
        [
            "derive-attribution-spec",
            "--template",
            str(tmp_path / "t.json"),
            "--baseline-root",
            str(tmp_path),
            "--baseline-generation-id",
            "0" * 64,
            "--cutoff",
            CUTOFF,
            "--out",
            str(tmp_path / "out.json"),
        ]
    )
    assert code == 1
    assert (
        "release-cycle-seeds: FAIL: return source is invalid" in capsys.readouterr().err
    )


def test_the_cli_reports_a_refusal_without_a_traceback(
    world: dict, capsys: pytest.CaptureFixture
) -> None:
    code = seeds.main(
        [
            "stage",
            "--release-inputs-root",
            str(world["inputs"]),
            "--release-inputs-manifest-sha256",
            world["pin"],
            "--cutoff",
            "2000-02-04",
            "--root",
            f"data={world['roots']['data']}",
            "--cycle",
            str(world["cycle"]),
            "--repo-root",
            str(world["repo"]),
        ]
    )
    assert code == 1
    assert "release-cycle-seeds: FAIL" in capsys.readouterr().err
