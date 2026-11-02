"""B7: public bundles and Quantbase sources from the resolved cycle. Synthetic fixtures only."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import release_public_bundles as pub  # noqa: E402
import release_quantbase_sources as qb  # noqa: E402
from quant_engine.core.release_inputs import build_manifest  # noqa: E402


def _load(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# the committed builders' own synthetic fixtures (catalog, feeds, receipts)
_FACTOR_TESTS = _load(
    "b7_factor_fixtures",
    "tests/portfolio_analytics/test_portfolio_public_decomposition_sources.py",
)
_STYLE_TESTS = _load(
    "b7_style_fixtures",
    "tests/portfolio_analytics/test_portfolio_public_style_sources.py",
)

CUTOFF = "2026-03-17"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolved(tmp_path: Path, **extra: Any) -> Path:
    cycle = {
        "schema_version": "qe-release-resolved-cycle/1",
        "cycle_id": "cycle-synthetic",
        "cutoff": CUTOFF,
        "evaluated_on": CUTOFF,
        "providers": {"quantbase": {"generation_id": "gen-synthetic"}},
        "public_bundles": {
            "public_factors": str(tmp_path / "out-factors"),
            "sector_returns": str(tmp_path / "out-style"),
            "etf_style": str(tmp_path / "out-etf"),
        },
        **extra,
    }
    path = tmp_path / "resolved-cycle.json"
    path.write_text(json.dumps(cycle), encoding="utf-8")
    return path


# -- public bundles ----------------------------------------------------------------------
def _public_world(tmp_path: Path) -> dict[str, Any]:
    raw = tmp_path / "raw"
    raw.mkdir()
    fdir, sdir = raw / "f", raw / "s"
    fdir.mkdir()
    sdir.mkdir()
    catalog_f, sources, manifests = _FACTOR_TESTS._inputs(fdir)
    catalog_s, yahoo, yahoo_manifest, _ = _STYLE_TESTS._inputs(sdir)

    def row(path: Path) -> dict[str, str]:
        return {"path": path.relative_to(raw).as_posix(), "sha256": _sha(path)}

    spec = {
        "schema_version": pub.SOURCES_SCHEMA,
        "acquisition_run": {
            "run_started_at_utc": _FACTOR_TESTS.RUN_STARTED_AT_UTC,
            "run_ended_at_utc": _FACTOR_TESTS.RUN_ENDED_AT_UTC,
            "evaluation_date": _FACTOR_TESTS.EVALUATION_DATE,
        },
        "factor_feeds": {
            key: {"source": row(sources[key]), "manifest": row(manifests[key])}
            for key in sources
        },
        "styles": [
            {
                "bundle_key": "sector_returns",
                "source": row(yahoo),
                "manifest": row(yahoo_manifest),
                "universe_ids": ["style-fixture"],
            },
            {  # a second build with the builder's default universes (the R2 ETF-style shape)
                "bundle_key": "etf_style",
                "source": row(yahoo),
                "manifest": row(yahoo_manifest),
            },
        ],
    }
    return {"raw": raw, "catalog_f": catalog_f, "catalog_s": catalog_s, "spec": spec}


def _write_spec(tmp_path: Path, spec: dict[str, Any]) -> Path:
    path = tmp_path / "public-sources.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def _build(tmp_path: Path, world: dict[str, Any], **override: Any) -> dict[str, Any]:
    spec = override.pop("spec", world["spec"])
    return pub.build(
        override.pop("resolved", _resolved(tmp_path)),
        _write_spec(tmp_path, spec),
        world["raw"],
        world["catalog_f"],
        factors_bundle_key="public_factors",
    )


def test_public_build_end_to_end_matches_the_builders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build() wires the verified paths and the cycle's run window into the committed writers."""

    world = _public_world(tmp_path)
    calls: dict[str, Any] = {}

    def fake_factors(
        catalog: Path, srcs: Any, mans: Any, out: Path, start: str, end: str, ev: str
    ) -> dict[str, Any]:
        calls["factors"] = (catalog, dict(srcs), dict(mans), out, start, end, ev)
        return {"bundle_id": "b-f", "destination": str(out / "b-f")}

    def fake_style(
        catalog: Path, src: Path, man: Path, out: Path, universes: Any
    ) -> dict[str, Any]:
        calls.setdefault("style", []).append((catalog, src, man, out, universes))
        return {"bundle_id": "b-s", "destination": str(out / "b-s")}

    monkeypatch.setattr(pub.factors, "write_bundle", fake_factors)
    monkeypatch.setattr(pub.style, "write_bundle", fake_style)
    result = _build(tmp_path, world)
    assert (
        result["factors"]["bundle_id"] == "b-f"
        and result["styles"][0]["bundle_id"] == "b-s"
        and len(result["styles"]) == 2
    )
    catalog, srcs, mans, out, start, end, ev = calls["factors"]
    assert (
        out == tmp_path / "out-factors"
        and calls["style"][0][3] == tmp_path / "out-style"
    )
    assert set(srcs) == set(pub.factors.SOURCE_KEYS) == set(mans)
    assert (start, end, ev) == (
        _FACTOR_TESTS.RUN_STARTED_AT_UTC,
        _FACTOR_TESTS.RUN_ENDED_AT_UTC,
        _FACTOR_TESTS.EVALUATION_DATE,
    )
    assert [c[4] for c in calls["style"]] == [["style-fixture"], None]
    assert calls["style"][1][3] == tmp_path / "out-etf"


def test_public_real_builders_run_on_verified_feeds_and_are_create_only(
    tmp_path: Path,
) -> None:
    world = _public_world(tmp_path)
    cycle = _resolved(tmp_path)
    spec = world["spec"]
    # the style fixture carries its own catalog; factor and style builders take the same flag in
    # production, so run each real writer through build() with the catalog that fits its fixture
    result = pub.factors.write_bundle(
        world["catalog_f"],
        {
            k: world["raw"] / v["source"]["path"]
            for k, v in spec["factor_feeds"].items()
        },
        {
            k: world["raw"] / v["manifest"]["path"]
            for k, v in spec["factor_feeds"].items()
        },
        tmp_path / "out-factors",
        *(
            spec["acquisition_run"][k]
            for k in ("run_started_at_utc", "run_ended_at_utc", "evaluation_date")
        ),
    )
    assert Path(result["destination"]).is_dir()
    assert cycle.is_file()
    with pytest.raises(pub.factors.PublicSourceError, match="already exists"):
        _build(
            tmp_path, world, resolved=cycle
        )  # the committed writer never replaces a bundle


def _refused(tmp_path: Path, match: str, mutate: Any = None, **override: Any) -> None:
    world = _public_world(tmp_path)
    if mutate is not None:
        spec = copy.deepcopy(world["spec"])
        mutate(spec, world)
        override["spec"] = spec
    with pytest.raises(pub.Refusal, match=match):
        _build(tmp_path, world, **override)
    assert (
        not (tmp_path / "out-factors").exists()
        and not (tmp_path / "out-style").exists()
    )


def test_public_refuses_a_changed_raw_file(tmp_path: Path) -> None:
    def mutate(spec: dict[str, Any], world: dict[str, Any]) -> None:
        (
            world["raw"] / spec["factor_feeds"]["us_factors"]["source"]["path"]
        ).write_bytes(b"{}")

    _refused(tmp_path, "differs from its pin", mutate)


@pytest.mark.parametrize(
    ("path", "match"),
    [
        ("nope.json", "is missing"),
        ("../x.json", "safe relative path"),
        ("/abs.json", "safe relative path"),
    ],
)
def test_public_refuses_missing_and_escaping_paths(
    tmp_path: Path, path: str, match: str
) -> None:
    _refused(
        tmp_path, match, lambda spec, w: spec["styles"][0]["source"].update(path=path)
    )


def test_public_refuses_wrong_feed_set_window_and_cycle(tmp_path: Path) -> None:
    _refused(
        tmp_path,
        "factor_feeds must be exactly",
        lambda s, w: s["factor_feeds"].pop("us_momentum"),
    )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda s: s["acquisition_run"].update(
                run_ended_at_utc="2026-03-17T11:00:00Z"
            ),
            "ends before",
        ),
        (
            lambda s: s["acquisition_run"].update(evaluation_date="2026-03-18"),
            "after the cycle",
        ),
        (
            lambda s: s["acquisition_run"].update(evaluation_date="17/03/2026"),
            "malformed",
        ),
        (lambda s: s["styles"][0].update(universe_ids=[]), "universe_ids"),
        (lambda s: s.update(schema_version="other/1"), "schema_version"),
        (lambda s: s.update(extra=1), "keys differ"),
    ],
)
def test_public_refuses_inconsistent_manifest(
    tmp_path: Path, mutate: Any, match: str
) -> None:
    _refused(tmp_path, match, lambda spec, w: mutate(spec))


def test_public_refuses_cycle_without_bundle_root_or_schema(tmp_path: Path) -> None:
    world = _public_world(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {"schema_version": "qe-release-resolved-cycle/1", "evaluated_on": CUTOFF}
        )
    )
    with pytest.raises(pub.Refusal, match="public_bundles"):
        _build(tmp_path, world, resolved=bad)
    bad.write_text(json.dumps({"schema_version": "old"}))
    with pytest.raises(pub.Refusal, match="schema_version"):
        _build(tmp_path, world, resolved=bad)


# -- Quantbase sources ---------------------------------------------------------------------
FILES = {
    "accounts.csv": b"id,name\n1,synthetic\n",
    "account_activity.csv": b"id,amount\n1,10\n",
    "dashboard_One_pricehistory.csv": b"d,p\n2026-01-02,1\n",
    "dashboard_Two_pricehistory.csv": b"d,p\n2026-01-02,2\n",
}


def _policy(files: dict[str, bytes] = FILES) -> dict[str, Any]:
    sha = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    return {
        "quantbase-fund-facts": {
            "sources": {
                "accounts": {"file": "accounts.csv", "sha256": sha["accounts.csv"]},
                "account_activity": {
                    "file": "account_activity.csv",
                    "sha256": sha["account_activity.csv"],
                },
                "dashboards": {
                    "one": {
                        "file": "dashboard_One_pricehistory.csv",
                        "sha256": sha["dashboard_One_pricehistory.csv"],
                    },
                    "two": {
                        "file": "dashboard_Two_pricehistory.csv",
                        "sha256": sha["dashboard_Two_pricehistory.csv"],
                    },
                },
            }
        }
    }


def _candidates(tmp_path: Path) -> tuple[Path, Path]:
    directory = tmp_path / "capture"
    (directory / "nested").mkdir(parents=True, exist_ok=True)
    (directory / "accounts.csv").write_bytes(FILES["accounts.csv"])
    (directory / "nested" / "account_activity.csv").write_bytes(
        FILES["account_activity.csv"]
    )
    archive = tmp_path / "old.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(
            "x/dashboard_One_pricehistory.csv", FILES["dashboard_One_pricehistory.csv"]
        )
        handle.writestr(
            "y/dashboard_Two_pricehistory.csv", b"stale bytes"
        )  # same name, wrong bytes
        handle.writestr(
            "z/dashboard_Two_pricehistory.csv", FILES["dashboard_Two_pricehistory.csv"]
        )
    return directory, archive


def _policy_file(tmp_path: Path, policy: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "policy.json"
    path.write_bytes(json.dumps(policy or _policy()).encode())
    return path


def _qb_main(tmp_path: Path, *extra: str, out: str = "qb-sources") -> int:
    directory, archive = (
        _candidates(tmp_path)
        if not (tmp_path / "capture").exists()
        else (tmp_path / "capture", tmp_path / "old.zip")
    )
    return qb.main(
        [
            "--resolved-cycle", str(_resolved(tmp_path)),
            "--policy-manifest", str(_policy_file(tmp_path)),
            "--source-dir", str(directory),
            "--source-zip", str(archive),
            "--output-dir", str(tmp_path / out),
            *extra,
        ]
    )  # fmt: skip


def test_qb_assembles_by_hash_from_dirs_and_zips(tmp_path: Path) -> None:
    assert _qb_main(tmp_path) == 0
    out = tmp_path / "qb-sources"
    assert {p.name for p in out.iterdir()} == set(FILES)
    for name, data in FILES.items():
        assert (out / name).read_bytes() == data  # byte-identical to the pinned bytes
    receipt = json.loads((tmp_path / "qb-sources.receipt.json").read_text())
    assert receipt["quantbase_generation_id"] == "gen-synthetic"
    assert (
        receipt["files"]["dashboard_Two_pricehistory.csv"]["source"]
        == "zip:z/dashboard_Two_pricehistory.csv"
    )
    assert not (tmp_path / "qb-sources.partial").exists()


def test_qb_refuses_existing_output_missing_and_mismatched(tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    assert _qb_main(tmp_path, out="taken") == 1
    directory, archive = _candidates(tmp_path)
    (directory / "accounts.csv").write_bytes(
        b"changed"
    )  # the only candidate has other bytes
    assert _qb_main(tmp_path, out="x1") == 1
    assert not (tmp_path / "x1").exists() and not (tmp_path / "x1.partial").exists()
    (directory / "accounts.csv").unlink()
    assert _qb_main(tmp_path, out="x2") == 1  # no candidate at all
    assert (
        not (tmp_path / "x2").exists() and not (tmp_path / "x2.receipt.json").exists()
    )


def test_qb_refuses_bad_cycle_and_policy(tmp_path: Path) -> None:
    directory, archive = _candidates(tmp_path)
    bad_cycle = tmp_path / "c.json"
    bad_cycle.write_text(
        json.dumps({"schema_version": "qe-release-resolved-cycle/1", "providers": {}})
    )
    policy = _policy_file(tmp_path)

    def run(cycle: Path, pol: Path, out: str) -> int:
        return qb.main(["--resolved-cycle", str(cycle), "--policy-manifest", str(pol),
                        "--source-dir", str(directory), "--source-zip", str(archive),
                        "--output-dir", str(tmp_path / out)])  # fmt: skip

    assert run(bad_cycle, policy, "o1") == 1
    broken = copy.deepcopy(_policy())
    broken["quantbase-fund-facts"]["sources"]["dashboards"]["two"]["file"] = (
        "dashboard_One_pricehistory.csv"
    )
    pol2 = tmp_path / "p2.json"
    pol2.write_bytes(json.dumps(broken).encode())
    assert run(_resolved(tmp_path), pol2, "o2") == 1  # two pins, one file name
    pol3 = tmp_path / "p3.json"
    pol3.write_bytes(b"{}")
    assert run(_resolved(tmp_path), pol3, "o3") == 1
    assert (
        qb.main(
            [
                "--resolved-cycle",
                str(_resolved(tmp_path)),
                "--output-dir",
                str(tmp_path / "o4"),
            ]
        )
        == 1
    )


def test_qb_policy_from_pinned_release_inputs_and_cutoff(tmp_path: Path) -> None:
    root = tmp_path / "inputs"
    target = root / qb.POLICY_LOGICAL
    target.parent.mkdir(parents=True)
    target.write_bytes(json.dumps(_policy()).encode())
    manifest, pin = build_manifest(root, [qb.POLICY_LOGICAL], cutoff=CUTOFF)
    directory, archive = _candidates(tmp_path)

    def run(cutoff_cycle: Path, out: str, sha: str) -> int:
        return qb.main(["--resolved-cycle", str(cutoff_cycle), "--release-inputs-root", str(root),
                        "--release-inputs-manifest-sha256", sha,
                        "--source-dir", str(directory), "--source-zip", str(archive),
                        "--output-dir", str(tmp_path / out)])  # fmt: skip

    assert manifest.is_file()
    assert run(_resolved(tmp_path), "good", pin) == 0
    assert run(_resolved(tmp_path), "badpin", "0" * 64) == 1
    other = tmp_path / "other.json"
    cycle = json.loads(_resolved(tmp_path).read_text())
    cycle["cutoff"] = "2026-03-24"
    other.write_text(json.dumps(cycle))
    assert run(other, "badcutoff", pin) == 1
    assert not (tmp_path / "badcutoff").exists()
