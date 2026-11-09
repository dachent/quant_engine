"""Recorder and fetcher bodies are admitted into a HAR root only with a checked id binding.

Every body here is a key-structure skeleton with invented ids and values."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tools import p123_har_admit as admit

AS_OF = date(2026, 1, 9)
ACCOUNT = 7001
LIST_TEMPLATE = "/app/investment/resource"


def _row(uid: int, name: str, inception: str, pos: int) -> dict[str, Any]:
    return {
        "portfolioUid": uid,
        "assetUid": uid + 100,
        "assetType": "LIVE_BOOK",
        "accountUid": ACCOUNT,
        "name": name,
        "assetName": name,
        "inceptionDt": inception,
        "posCnt": pos,
        "isAccountLive": True,
        "mktVal": 1.5,
    }


ROWS = [_row(9001, "Alpha", "2026-01-05", 2), _row(9002, "Beta", "2026-01-05", 0)]


def _perf() -> dict[str, Any]:
    return {
        "date": "2026-01-05",
        "dateLast": "2026-01-09",
        "data": [
            [0, 100.0, 0.0],
            [1, 101.0, 10.0],
            [1, 102.0, 10.0],
            [3, 103.0, 10.0],
            [1, 104.0, 10.0],
        ],
        "panels": [
            {"graphs": [{"idx": 0, "s": "Strategy"}, {"idx": 1, "s": "Total Value"}]}
        ],
    }


def _resource(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row["name"],
        "inceptionDt": row["inceptionDt"],
        "posCnt": row["posCnt"],
        "startingCapital": 0.0,
    }


def _holdings(count: int) -> dict[str, Any]:
    return {"positions": [{"shares": 1.0, "value": 2.0} for _ in range(count)]}


def _recorder(
    tmp: Path,
    name: str = "run",
    *,
    rows: list[dict[str, Any]] | None = None,
    visits: list[tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]]
    | None = None,
    account: int = ACCOUNT,
    extra: list[tuple[str, Any]] | None = None,
) -> Path:
    """A recorder run: the list, the account body, then one visit per item."""
    rows = ROWS if rows is None else rows
    if visits is None:
        visits = [(ROWS[0], _perf(), _holdings(2)), (ROWS[1], None, _holdings(0))]
    run = tmp / name
    (run / "raw").mkdir(parents=True)
    sequence: list[tuple[str, Any]] = [
        (LIST_TEMPLATE, rows),
        ("/app/account/resource/{id}", {"id": account}),
    ]
    for row, perf, holdings in visits:
        sequence.append(("/app/investment/resource/{id}", _resource(row)))
        if perf is not None:
            sequence.append(("/app/investment/perf-data/{id}", perf))
        if holdings is not None:
            sequence.append(("/app/investment/holdings/{id}", holdings))
    sequence.extend(extra or [])
    entries, artifacts = [], []
    for ordinal, (template, body) in enumerate(sequence):
        data = json.dumps(body, sort_keys=True).encode()
        relative = f"raw/{ordinal:05d}.body"
        (run / relative).write_bytes(data)
        entries.append(
            {
                "ordinal": ordinal,
                "method": "GET",
                "status": 200,
                "path_template": template,
            }
        )
        artifacts.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        )
    entries_bytes = json.dumps(entries).encode()
    (run / "entries.json").write_bytes(entries_bytes)
    artifacts.append(
        {
            "path": "entries.json",
            "sha256": hashlib.sha256(entries_bytes).hexdigest(),
            "size_bytes": len(entries_bytes),
        }
    )
    (run / "manifest.json").write_text(
        json.dumps({"artifacts": artifacts}), encoding="utf-8"
    )
    return run


def _base(tmp: Path) -> Path:
    base = tmp / "base"
    (base / "csv").mkdir(parents=True)
    (base / "csv" / "strategy_7001_app_investment_resource.csv").write_text(
        "old\n", encoding="utf-8"
    )
    (base / "csv" / "strategy_1_app_state.csv").write_text("keep\n", encoding="utf-8")
    return base


def _out(tmp: Path, name: str = "out") -> Path:
    return tmp / "scratch" / name


def _run(tmp: Path, recorder: Path, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = dict(
        base=_base(tmp) if not (tmp / "base").exists() else tmp / "base",
        out=_out(tmp),
        recorders=[recorder],
        fetchers=[],
        as_of=AS_OF,
        account_ids=[ACCOUNT],
        dry_run=True,
    )
    args.update(kw)
    return admit.admit(**args)


def test_admission_binds_by_the_list_and_records_every_hash(tmp_path: Path) -> None:
    manifest = _run(tmp_path, _recorder(tmp_path))
    root = _out(tmp_path) / "har-root" / "csv"
    assert (root / "strategy_9001_app_investment_perfdata_9001.csv").is_file()
    assert (root / "strategy_9001_app_investment_resource_9001.csv").is_file()
    assert (root / "strategy_9001_app_investment_holdings_9001.csv").is_file()
    assert (root / "strategy_9002_app_investment_holdings_9002.csv").is_file()
    assert not (root / "strategy_9002_app_investment_perfdata_9002.csv").exists()
    equity = (root / "strategy_9001_perf.csv").read_text(encoding="utf-8").splitlines()
    assert equity == [
        "Date,Total Equity,Cash Added",
        "2026-01-06,101.0,10.0",
        "2026-01-07,102.0,10.0",
        "2026-01-09,104.0,10.0",
    ]
    assert manifest["evidence_eligible"] is False
    assert manifest["items"]["9001"]["last_date"] == "2026-01-09"
    assert manifest["items"]["9002"]["last_date"] is None
    for relative, entry in manifest["outputs"].items():
        assert (
            hashlib.sha256(
                (_out(tmp_path) / "har-root" / relative).read_bytes()
            ).hexdigest()
            == entry["sha256"]
        )
    layout = {e["relative_path"]: e["origin"] for e in manifest["final_root_layout"]}
    assert layout["csv/strategy_1_app_state.csv"] == "base"
    assert layout["csv/strategy_9001_perf.csv"] == "admitted"
    assert list(manifest["replaced_base_files"]) == [
        "csv/strategy_7001_app_investment_resource.csv"
    ]
    written = json.loads(
        (_out(tmp_path) / "p123-har-admission-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert written["bodies"]["9001/perf"]["source"] == "recorder"
    assert (
        "list row on name+inceptionDt+posCnt"
        in written["items"]["9001"]["binding"]["resource"]
    )


def test_a_resource_body_matching_two_list_rows_or_none_is_refused(
    tmp_path: Path,
) -> None:
    twin = deepcopy(ROWS[0])
    twin["portfolioUid"] = 9003
    twin["assetUid"] = 9103
    with pytest.raises(admit.AdmissionRefused, match="matches 2 list rows"):
        _run(tmp_path, _recorder(tmp_path, rows=[*ROWS, twin]))
    stranger = _row(9009, "Gamma", "2026-01-05", 2)
    run = _recorder(tmp_path, "r2", visits=[(stranger, None, None)])
    with pytest.raises(admit.AdmissionRefused, match="matches 0 list rows"):
        _run(tmp_path, run, out=_out(tmp_path, "o2"))


def test_a_body_that_the_item_does_not_corroborate_is_refused(tmp_path: Path) -> None:
    wrong_start = _perf()
    wrong_start["date"] = "2026-01-06"
    wrong_start["data"] = wrong_start["data"][:4]
    with pytest.raises(admit.AdmissionRefused, match="not the inception"):
        _run(
            tmp_path, _recorder(tmp_path, visits=[(ROWS[0], wrong_start, _holdings(2))])
        )
    with pytest.raises(admit.AdmissionRefused, match="has 1 positions"):
        _run(
            tmp_path,
            _recorder(tmp_path, "r2", visits=[(ROWS[0], _perf(), _holdings(1))]),
            out=_out(tmp_path, "o2"),
        )


def test_a_perf_body_with_no_item_before_it_is_refused(tmp_path: Path) -> None:
    run = _recorder(
        tmp_path, visits=[], extra=[("/app/investment/perf-data/{id}", _perf())]
    )
    with pytest.raises(admit.AdmissionRefused, match="no item resource body"):
        _run(tmp_path, run)
    far = [("/app/account/resource/{id}", {"id": ACCOUNT})] * (admit.WINDOW + 1)
    run = _recorder(
        tmp_path,
        "far",
        visits=[(ROWS[0], None, None)],
        extra=[*far, ("/app/investment/perf-data/{id}", _perf())],
    )
    with pytest.raises(admit.AdmissionRefused, match="no item resource body"):
        _run(tmp_path, run, out=_out(tmp_path, "o2"))


def test_two_different_bodies_for_one_item_are_refused(tmp_path: Path) -> None:
    other = _perf()
    other["data"][1][1] = 101.5
    run = _recorder(tmp_path, visits=[(ROWS[0], _perf(), None), (ROWS[0], other, None)])
    with pytest.raises(admit.AdmissionRefused, match="two different perf bodies"):
        _run(tmp_path, run)


def test_a_series_that_stops_before_the_as_of_or_breaks_the_calendar_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(admit.AdmissionRefused, match="before the as-of"):
        _run(tmp_path, _recorder(tmp_path), as_of=date(2026, 1, 12))
    short = _perf()
    short["data"] = short["data"][:4]
    with pytest.raises(
        admit.AdmissionRefused, match="differ from the weekday calendar"
    ):
        _run(
            tmp_path,
            _recorder(tmp_path, "r2", visits=[(ROWS[0], short, None)]),
            out=_out(tmp_path, "o2"),
        )


def test_the_account_must_be_one_the_run_states(tmp_path: Path) -> None:
    run = _recorder(tmp_path)
    with pytest.raises(admit.AdmissionRefused, match="not an account the runs state"):
        _run(tmp_path, run, account_ids=[7002])
    with pytest.raises(admit.AdmissionRefused, match="name the account"):
        _run(tmp_path, run, account_ids=[], out=_out(tmp_path, "o2"))


def _fetcher(tmp: Path, files: dict[str, Any], name: str = "fetch") -> Path:
    capture = tmp / name
    (capture / "raw").mkdir(parents=True)
    artifacts = []
    for fname, body in files.items():
        data = json.dumps(body, sort_keys=True).encode()
        (capture / "raw" / fname).write_bytes(data)
        artifacts.append(
            {
                "relative_path": f"raw/{fname}",
                "sha256": hashlib.sha256(data).hexdigest(),
                "observed_length": len(data),
            }
        )
    (capture / "capture-manifest.json").write_text(
        json.dumps({"artifacts": artifacts}), encoding="utf-8"
    )
    return capture


def test_a_fetcher_body_must_equal_the_recorder_body_of_the_same_item(
    tmp_path: Path,
) -> None:
    run = _recorder(tmp_path)
    same = _fetcher(
        tmp_path, {"strategy_9001_app_investment_perfdata_init.json": _perf()}
    )
    manifest = _run(tmp_path, run, fetchers=[same])
    assert manifest["sources"]["fetchers"][0]["bodies"] == 1
    other = _perf()
    other["data"][2][1] = 999.0
    differs = _fetcher(
        tmp_path, {"strategy_9001_app_investment_perfdata_init.json": other}, "fetch2"
    )
    with pytest.raises(admit.AdmissionRefused, match="differs from the recorder body"):
        _run(tmp_path, run, fetchers=[differs], out=_out(tmp_path, "o2"))
    unknown = _fetcher(
        tmp_path, {"strategy_9999_app_investment_perfdata_init.json": _perf()}, "fetch3"
    )
    with pytest.raises(admit.AdmissionRefused, match="does not hold"):
        _run(tmp_path, run, fetchers=[unknown], out=_out(tmp_path, "o3"))


def test_a_fetcher_artifact_that_differs_from_its_manifest_is_refused(
    tmp_path: Path,
) -> None:
    capture = _fetcher(
        tmp_path, {"strategy_9001_app_investment_perfdata_init.json": _perf()}
    )
    (capture / "raw" / "strategy_9001_app_investment_perfdata_init.json").write_bytes(
        b"{}"
    )
    with pytest.raises(admit.AdmissionRefused, match="differs from its manifest"):
        _run(tmp_path, _recorder(tmp_path), fetchers=[capture])


def test_a_recorder_run_with_a_stray_or_altered_body_is_refused(tmp_path: Path) -> None:
    run = _recorder(tmp_path)
    (run / "raw" / "99999.body").write_bytes(b"{}")
    with pytest.raises(admit.AdmissionRefused, match="its manifest does not list"):
        _run(tmp_path, run)
    run2 = _recorder(tmp_path, "r2")
    (run2 / "raw" / "00003.body").write_bytes(b"{}")
    with pytest.raises(admit.AdmissionRefused, match="differs from its manifest"):
        _run(tmp_path, run2, out=_out(tmp_path, "o2"))


def test_a_second_sitting_adds_items_but_must_name_the_same_items(
    tmp_path: Path,
) -> None:
    first = _recorder(tmp_path, visits=[(ROWS[0], _perf(), _holdings(2))])
    second = _recorder(tmp_path, "second", visits=[(ROWS[1], None, _holdings(0))])
    manifest = _run(tmp_path, first, recorders=[first, second])
    assert sorted(manifest["items"]) == ["9001", "9002"]
    changed = deepcopy(ROWS)
    changed[1]["name"] = "Renamed"
    third = _recorder(
        tmp_path, "third", rows=changed, visits=[(changed[1], None, None)]
    )
    with pytest.raises(admit.AdmissionRefused, match="names different items"):
        _run(tmp_path, first, recorders=[first, third], out=_out(tmp_path, "o2"))
    live_only = deepcopy(ROWS)
    live_only[0]["mktVal"] = (
        99.0  # a live column moves between sittings: allowed, recorded
    )
    fourth = _recorder(
        tmp_path, "fourth", rows=live_only, visits=[(live_only[1], None, _holdings(0))]
    )
    manifest = _run(
        tmp_path, first, recorders=[first, fourth], out=_out(tmp_path, "o3")
    )
    assert len(manifest["sources"]["investment_lists_of_other_runs"]) == 1


def test_output_root_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _recorder(tmp_path)
    with pytest.raises(admit.AdmissionRefused, match="scratch folder only"):
        _run(tmp_path, run, out=tmp_path / "governed" / "out")
    with pytest.raises(admit.AdmissionRefused, match="refuses a scratch"):
        _run(tmp_path, run, out=_out(tmp_path, "o2"), dry_run=False)
    # pytest's own tmp_path sits under a temp folder; only "scratch" is refused for the rest
    monkeypatch.setattr(admit, "_TEMP_PARTS", {"scratch"})
    manifest = _run(tmp_path, run, out=tmp_path / "governed" / "real", dry_run=False)
    assert manifest["evidence_eligible"] is True
    with pytest.raises(admit.AdmissionRefused, match="must not exist"):
        _run(tmp_path, run, out=tmp_path / "governed" / "real", dry_run=False)
    with pytest.raises(admit.AdmissionRefused, match="absolute"):
        _run(tmp_path, run, out=Path("relative/scratch/x"))


def test_an_admitted_file_may_not_replace_a_base_data_file(tmp_path: Path) -> None:
    base = _base(tmp_path)
    (base / "csv" / "strategy_9001_perf.csv").write_text("x\n", encoding="utf-8")
    with pytest.raises(admit.AdmissionRefused, match="would replace a base file"):
        _run(tmp_path, _recorder(tmp_path))


def _designer(start: str = "2026-01-05") -> tuple[dict[str, Any], dict[str, Any]]:
    model = {
        "portUid": 8001,
        "name": "Synthetic",
        "inceptionDt": "01/05/2026",
        "endDt": "01/09/2026",
        "launchDt": "01/07/2026",
    }
    perf = {
        "perfData": {
            "date": start,
            "data": [
                [0, 100.0, 100.0],
                [1, 101.0, 100.0],
                [1, 102.0, 100.0],
                [1, 103.0, 100.0],
                [1, 104.0, 100.0],
                [1, None, None],
            ],
            "plotLines": [{"dt": "2026-01-07"}],
        }
    }
    return model, perf


def _designer_run(
    tmp: Path, model: dict[str, Any], perf: dict[str, Any], name: str = "drun"
) -> Path:
    return _recorder(
        tmp,
        name,
        visits=[],
        extra=[
            ("/spr/strategy/model/{id}", model),
            ("/spr/strategy/model/{id}/perf", perf),
            ("/spr/strategy/model/{id}/holdings", {"holdings": []}),
        ],
    )


def test_designer_staging_refuses_a_windowed_curve_and_stages_the_full_one(
    tmp_path: Path,
) -> None:
    model, full = _designer()
    out = _out(tmp_path, "d1")
    manifest = admit.stage_designer(
        out=out,
        uid=8001,
        recorder=_designer_run(tmp_path, model, full),
        fetcher=None,
        dry_run=True,
    )
    assert manifest["curve"] == {"start": "2026-01-05", "end": "2026-01-09"}
    names = sorted(p.name for p in (out / "raw").iterdir())
    assert names == [
        "r2g_8001_dm_true.json",
        "r2g_8001_holdings.json",
        "r2g_8001_perf-full.json",
    ]
    staged = json.loads((out / "capture-manifest.json").read_text(encoding="utf-8"))
    for item in staged["artifacts"]:
        assert (
            hashlib.sha256((out / item["relative_path"]).read_bytes()).hexdigest()
            == item["sha256"]
        )
    _, windowed = _designer(start="2026-01-06")
    with pytest.raises(admit.AdmissionRefused, match="windowed curve"):
        admit.stage_designer(
            out=_out(tmp_path, "d2"),
            uid=8001,
            recorder=_designer_run(tmp_path, model, windowed, "w"),
            fetcher=None,
            dry_run=True,
        )
    stale = deepcopy(model)
    stale["endDt"] = "01/12/2026"
    with pytest.raises(admit.AdmissionRefused, match="model states endDt"):
        admit.stage_designer(
            out=_out(tmp_path, "d3"),
            uid=8001,
            recorder=_designer_run(tmp_path, stale, full, "s"),
            fetcher=None,
            dry_run=True,
        )
    moved = deepcopy(model)
    moved["launchDt"] = "01/08/2026"
    with pytest.raises(admit.AdmissionRefused, match="launch line"):
        admit.stage_designer(
            out=_out(tmp_path, "d4"),
            uid=8001,
            recorder=_designer_run(tmp_path, moved, full, "m"),
            fetcher=None,
            dry_run=True,
        )
    with pytest.raises(admit.AdmissionRefused, match="missing"):
        admit.stage_designer(
            out=_out(tmp_path, "d5"),
            uid=8002,
            recorder=_designer_run(tmp_path, model, full, "x"),
            fetcher=None,
            dry_run=True,
        )


def test_designer_staging_from_a_fetcher_capture(tmp_path: Path) -> None:
    model, full = _designer()
    capture = _fetcher(
        tmp_path, {"r2g_8001_dm_true.json": model, "r2g_8001_perf-full.json": full}
    )
    manifest = admit.stage_designer(
        out=_out(tmp_path, "f1"), uid=8001, recorder=None, fetcher=capture, dry_run=True
    )
    assert manifest["source"]["bodies"] == 2
    with pytest.raises(admit.AdmissionRefused, match="exactly one"):
        admit.stage_designer(
            out=_out(tmp_path, "f2"),
            uid=8001,
            recorder=None,
            fetcher=None,
            dry_run=True,
        )


def test_the_cli_exits_2_with_the_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _recorder(tmp_path)
    argv = [
        "admit",
        "--base",
        str(_base(tmp_path)),
        "--out",
        str(tmp_path / "governed" / "o"),
        "--recorder",
        str(run),
        "--as-of",
        "2026-01-09",
        "--account-id",
        str(ACCOUNT),
        "--dry-run",
    ]
    assert admit.main(argv) == 2
    assert "scratch folder only" in capsys.readouterr().err
    argv[argv.index("--out") + 1] = str(_out(tmp_path, "cli"))
    assert admit.main(argv) == 0
    assert "admitted" in capsys.readouterr().out
