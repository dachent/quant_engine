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


def _row(
    uid: int, name: str, inception: str, pos: int, ret: float = 4.0
) -> dict[str, Any]:
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
        "mktVal": pos * 2.0,
        "retTotal": ret,
    }


ROWS = [_row(9001, "Alpha", "2026-01-05", 2), _row(9002, "Beta", "2026-01-05", 0, 0.0)]


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


def test_a_body_that_no_list_row_corroborates_is_refused(tmp_path: Path) -> None:
    wrong_start = _perf()
    wrong_start["date"] = "2026-01-06"
    wrong_start["data"] = wrong_start["data"][:4]
    with pytest.raises(admit.AdmissionRefused, match="belongs to no item of the list"):
        _run(
            tmp_path, _recorder(tmp_path, visits=[(ROWS[0], wrong_start, _holdings(2))])
        )
    with pytest.raises(admit.AdmissionRefused, match="has 1 positions"):
        _run(
            tmp_path,
            _recorder(tmp_path, "r2", visits=[(ROWS[0], _perf(), _holdings(1))]),
            out=_out(tmp_path, "o2"),
        )
    # a total return the list does not state for any item
    off = _perf()
    off["data"][-1][1] = 150.0
    with pytest.raises(admit.AdmissionRefused, match="total return differs"):
        _run(
            tmp_path,
            _recorder(tmp_path, "r3", visits=[(ROWS[0], off, None)]),
            out=_out(tmp_path, "o3"),
        )
    # holdings whose value is not the list's market value
    heavy = {
        "positions": [{"shares": 1.0, "value": 9.0}, {"shares": 1.0, "value": 2.0}]
    }
    with pytest.raises(admit.AdmissionRefused, match="market value differs"):
        _run(
            tmp_path,
            _recorder(tmp_path, "r4", visits=[(ROWS[0], None, heavy)]),
            out=_out(tmp_path, "o4"),
        )


def test_binding_does_not_depend_on_the_order_responses_completed_in(
    tmp_path: Path,
) -> None:
    """Astra R5-01: a perf body completes BEFORE its own resource body, and another item's
    resource body sits between: the content still binds it to the right item."""
    a = _row(9001, "Alpha", "2026-01-05", 2, 4.0)
    b = _row(9002, "Beta", "2026-01-05", 2, 8.0)
    perf_b = _perf()
    perf_b["data"][-1][1] = 108.0  # Beta's own total return (8.0)
    run = _recorder(
        tmp_path,
        rows=[a, b],
        visits=[],
        extra=[
            ("/app/investment/resource/{id}", _resource(a)),
            (
                "/app/investment/perf-data/{id}",
                perf_b,
            ),  # Beta's curve, right after Alpha
            ("/app/investment/resource/{id}", _resource(b)),
        ],
    )
    manifest = _run(tmp_path, run)
    assert manifest["items"]["9002"]["last_date"] == "2026-01-09"
    assert "perf" not in {
        k.split("/")[1] for k in manifest["bodies"] if k.startswith("9001/")
    }
    assert manifest["bodies"]["9002/perf"]["label"] == "3"
    # two items whose total return and inception are identical cannot be told apart: refused
    twin = _row(9003, "Gamma", "2026-01-05", 2, 4.0)
    run2 = _recorder(
        tmp_path,
        "twins",
        rows=[a, twin],
        visits=[(a, _perf(), None), (twin, None, None)],
    )
    with pytest.raises(admit.AdmissionRefused, match="not bound"):
        _run(tmp_path, run2, out=_out(tmp_path, "o2"))


def test_a_perf_body_matching_no_item_is_refused_wherever_it_sits(
    tmp_path: Path,
) -> None:
    run = _recorder(
        tmp_path,
        visits=[],
        extra=[
            (
                "/app/investment/perf-data/{id}",
                {"date": "2020-01-01", "data": [[0, 100.0, 0.0]]},
            )
        ],
    )
    with pytest.raises(admit.AdmissionRefused, match="belongs to no item of the list"):
        _run(tmp_path, run)


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
            _recorder(
                tmp_path,
                "r2",
                rows=[_row(9001, "Alpha", "2026-01-05", 2, 3.0)],
                visits=[(_row(9001, "Alpha", "2026-01-05", 2, 3.0), short, None)],
            ),
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


def test_a_later_sitting_supersedes_live_snapshots_and_a_longer_series(
    tmp_path: Path,
) -> None:
    first = _recorder(tmp_path, visits=[(ROWS[0], _perf(), _holdings(2))])
    longer = _perf()
    longer["dateLast"] = "2026-01-12"
    longer["data"].append([1, 105.0, 10.0])
    moved = deepcopy(ROWS)
    row = moved[0]
    row["retTotal"] = 5.0  # the later sitting reaches 105
    run2 = _recorder(
        tmp_path, "later", rows=moved, visits=[(row, longer, _holdings(2))]
    )
    # the resource body of the later sitting differs (a rebalance moved a live field)
    resource = tmp_path / "later" / "raw" / "00002.body"
    doc = json.loads(resource.read_text(encoding="utf-8"))
    doc["lastRebalDt"] = "01/12/26"
    data = json.dumps(doc, sort_keys=True).encode()
    resource.write_bytes(data)
    manifest_path = tmp_path / "later" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["artifacts"]:
        if item["path"] == "raw/00002.body":
            item["sha256"], item["size_bytes"] = (
                hashlib.sha256(data).hexdigest(),
                len(data),
            )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _run(tmp_path, first, recorders=[first, run2], as_of=date(2026, 1, 12))
    kinds = {(x["item"], x["kind"]) for x in result["superseded_bodies"]}
    assert ("9001", "resource") in kinds and ("9001", "perf") in kinds
    assert result["items"]["9001"]["last_date"] == "2026-01-12"
    # an earlier series that disagrees on a shared day refuses
    clash = _perf()
    clash["data"][2][1] = 555.0
    run3 = _recorder(tmp_path, "clash", visits=[(ROWS[0], clash, None)])
    with pytest.raises(admit.AdmissionRefused, match="disagree on their shared days"):
        _run(tmp_path, first, recorders=[first, run3], out=_out(tmp_path, "o2"))


def test_the_output_root_may_not_overlap_the_base_root(tmp_path: Path) -> None:
    run = _recorder(tmp_path)
    base = _base(tmp_path)
    with pytest.raises(admit.AdmissionRefused, match="must not overlap"):
        _run(tmp_path, run, out=base / "scratch" / "inside")
    assert not (base / "scratch").exists()


def test_a_fetcher_capture_with_no_admissible_body_is_refused(tmp_path: Path) -> None:
    capture = _fetcher(
        tmp_path,
        {"p123_investment_resource.json": {"x": 1}, "strategy_1_perf.csv": "a"},
    )
    with pytest.raises(admit.AdmissionRefused, match="supplies no admissible body"):
        _run(tmp_path, _recorder(tmp_path), fetchers=[capture])
    mixed = _fetcher(
        tmp_path,
        {
            "strategy_9001_app_investment_perfdata_init.json": _perf(),
            "p123_investment_resource.json": {"x": 1},
        },
        "mixed",
    )
    manifest = _run(
        tmp_path, _recorder(tmp_path, "ok"), fetchers=[mixed], out=_out(tmp_path, "o2")
    )
    entry = manifest["sources"]["fetchers"][0]
    assert entry["admitted_files"] == [
        "strategy_9001_app_investment_perfdata_init.json"
    ]
    assert entry["ignored_files"] == ["p123_investment_resource.json"]


def _drop_body(run: Path, ordinal: int) -> None:
    """Make a recorded entry a 200 GET with no body: remove the file and its manifest line."""
    (run / "raw" / f"{ordinal:05d}.body").unlink()
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"] = [
        a for a in manifest["artifacts"] if a["path"] != f"raw/{ordinal:05d}.body"
    ]
    manifest["status"] = "CAPTURE_WITH_UNRECORDED_BODIES"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_unrecorded_bodies_are_tolerated_only_off_the_needed_routes(
    tmp_path: Path,
) -> None:
    # an unreadable /app/state poll: the run still ends CAPTURE_WITH_UNRECORDED_BODIES
    run = _recorder(tmp_path, extra=[("/app/state", {"x": 1})])
    _drop_body(run, len(json.loads((run / "entries.json").read_text())) - 1)
    manifest = _run(tmp_path, run)
    assert manifest["sources"]["recorders"][0]["unrecorded_needed_routes"] == []
    # a needed route without its body (the item's perf-data) is a refusal
    run2 = _recorder(tmp_path, "r2")
    perf_ordinal = next(
        e["ordinal"]
        for e in json.loads((run2 / "entries.json").read_text())
        if e["path_template"] == "/app/investment/perf-data/{id}"
    )
    _drop_body(run2, perf_ordinal)
    with pytest.raises(admit.AdmissionRefused, match="holds no body for it"):
        _run(tmp_path, run2, out=_out(tmp_path, "o2"))


def test_designer_staging_picks_the_full_history_among_windowed_bodies(
    tmp_path: Path,
) -> None:
    model, full = _designer()
    _, windowed = _designer(start="2026-01-06")
    odd = deepcopy(windowed)
    odd["perfData"]["data"] = odd["perfData"]["data"][:3]
    run = _recorder(
        tmp_path,
        "multi",
        visits=[],
        extra=[
            ("/spr/strategy/model/{id}", model),
            ("/spr/strategy/model/{id}/perf", windowed),
            ("/spr/strategy/model/{id}/perf", odd),
            ("/spr/strategy/model/{id}/perf", full),
            ("/spr/strategy/model/{id}/holdings", {"holdings": []}),
        ],
    )
    out = _out(tmp_path, "d1")
    manifest = admit.stage_designer(
        out=out, uid=8001, recorder=run, fetcher=None, dry_run=True
    )
    assert manifest["curve"]["start"] == "2026-01-05"
    assert len(manifest["windowed_curve_bodies_not_staged"]) == 2
    staged = json.loads((out / "raw" / "r2g_8001_perf-full.json").read_text())
    assert staged["perfData"]["date"] == "2026-01-05"
    # two different full-history bodies would be a guess: refused
    other = deepcopy(full)
    other["perfData"]["data"][2][1] = 555.0
    run2 = _recorder(
        tmp_path,
        "two",
        visits=[],
        extra=[
            ("/spr/strategy/model/{id}", model),
            ("/spr/strategy/model/{id}/perf", full),
            ("/spr/strategy/model/{id}/perf", other),
        ],
    )
    with pytest.raises(admit.AdmissionRefused, match="2 different full-history"):
        admit.stage_designer(
            out=_out(tmp_path, "d2"),
            uid=8001,
            recorder=run2,
            fetcher=None,
            dry_run=True,
        )
    # an unrecorded designer body refuses
    run3 = _recorder(
        tmp_path,
        "three",
        visits=[],
        extra=[
            ("/spr/strategy/model/{id}", model),
            ("/spr/strategy/model/{id}/perf", full),
        ],
    )
    _drop_body(run3, len(json.loads((run3 / "entries.json").read_text())) - 1)
    with pytest.raises(admit.AdmissionRefused, match="holds no body for it"):
        admit.stage_designer(
            out=_out(tmp_path, "d3"),
            uid=8001,
            recorder=run3,
            fetcher=None,
            dry_run=True,
        )


def test_a_failure_part_way_leaves_no_output_and_a_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _recorder(tmp_path)
    out = _out(tmp_path)
    real_copy = admit.shutil.copyfile
    calls = {"n": 0}

    def flaky(src: Any, dst: Any, **kw: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return real_copy(src, dst, **kw)

    monkeypatch.setattr(admit.shutil, "copyfile", flaky)
    with pytest.raises(OSError, match="disk full"):
        _run(tmp_path, run)
    assert not out.exists()
    assert [p.name for p in out.parent.iterdir()] == []  # the temp sibling is gone too
    monkeypatch.setattr(admit.shutil, "copyfile", real_copy)
    manifest = _run(tmp_path, run)  # the retry is not blocked by a partial result
    assert (out / "har-root" / "csv" / "strategy_9001_perf.csv").is_file()
    assert (out / "p123-har-admission-manifest.json").is_file()
    assert [p.name for p in out.parent.iterdir()] == [out.name]
    assert manifest["items"]["9001"]["last_date"] == "2026-01-09"
    # create-only: a final path that appears while the output is built is refused, not replaced
    other = _out(tmp_path, "race")

    def appears(src: Any, dst: Any, **kw: Any) -> Any:
        other.mkdir(parents=True, exist_ok=True)
        return real_copy(src, dst, **kw)

    monkeypatch.setattr(admit.shutil, "copyfile", appears)
    with pytest.raises(admit.AdmissionRefused, match="must not exist yet"):
        _run(tmp_path, run, out=other)
    assert not any(p.name.startswith(".") for p in other.parent.iterdir())


def test_designer_staging_failure_leaves_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model, full = _designer()
    run = _designer_run(tmp_path, model, full)
    out = _out(tmp_path, "d1")
    real = admit._write_new
    calls = {"n": 0}

    def flaky(path: Path, data: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(admit, "_write_new", flaky)
    with pytest.raises(OSError, match="disk full"):
        admit.stage_designer(
            out=out, uid=8001, recorder=run, fetcher=None, dry_run=True
        )
    assert not out.exists() and list(out.parent.iterdir()) == []
    monkeypatch.setattr(admit, "_write_new", real)
    admit.stage_designer(out=out, uid=8001, recorder=run, fetcher=None, dry_run=True)
    assert (out / "capture-manifest.json").is_file()


def test_an_absent_identity_field_never_matches_another_absent_one(
    tmp_path: Path,
) -> None:
    # None == None must not bind: a list row and a body that both lack the identity fields
    bare = {
        "portfolioUid": 9005,
        "assetUid": 1,
        "assetType": "X",
        "accountUid": ACCOUNT,
    }
    body_less = {"startingCapital": 0.0}
    run = _recorder(tmp_path, rows=[bare])
    resource = next(
        p for p in (run / "raw").glob("*.body") if b"startingCapital" in p.read_bytes()
    )
    resource.write_text(json.dumps(body_less), encoding="utf-8")
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    data = resource.read_bytes()
    for item in manifest["artifacts"]:
        if item["path"] == f"raw/{resource.name}":
            item["sha256"], item["size_bytes"] = (
                hashlib.sha256(data).hexdigest(),
                len(data),
            )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(
        admit.AdmissionRefused, match="lacks its name, inception or position"
    ):
        _run(tmp_path, run)
    # a perf body without a start date does not corroborate an item without an inception
    dateless = _perf()
    del dateless["date"]
    row = _row(9006, "Delta", "2026-01-05", 0)
    del row["inceptionDt"]
    assert admit._corroboration_error("perf", dateless, row) is not None
    assert (
        admit._corroboration_error("holdings", {"positions": []}, {"portfolioUid": 1})
        is not None
    )


def test_a_perf_row_that_is_not_marker_level_total_is_refused(tmp_path: Path) -> None:
    for number, bad in enumerate(
        ([1, None, 5.0], [1, 100.0], [1, 0, 5.0], [1, 100.0, "x"], [True, 100.0, 1.0])
    ):
        broken = _perf()
        broken["data"][2] = bad
        run = _recorder(
            tmp_path / f"r{number}", visits=[(ROWS[0], broken, _holdings(2))]
        )
        with pytest.raises(
            admit.AdmissionRefused,
            match=r"is not \[marker, positive level, total value\]",
        ):
            _run(tmp_path / f"r{number}", run, out=_out(tmp_path / f"r{number}"))


def _mark(run: Path, **fields: Any) -> None:
    path = run / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(fields)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_a_dry_run_or_non_evidence_input_is_refused(tmp_path: Path) -> None:
    """Astra R5-03: DRY- ids, dry-run directories and evidence_eligible false never admit."""
    for number, fields in enumerate(
        ({"run_id": "DRY-1"}, {"evidence_eligible": False})
    ):
        run = _recorder(tmp_path / f"d{number}")
        _mark(run, **fields)
        with pytest.raises(admit.AdmissionRefused, match="dry-run/rehearsal"):
            _run(tmp_path / f"d{number}", run, out=_out(tmp_path / f"d{number}"))
    # a recorder run under a dry-run directory
    nested = tmp_path / "dry-run-capture"
    run = _recorder(nested)
    with pytest.raises(admit.AdmissionRefused, match="dry-run/rehearsal"):
        _run(nested, run, out=_out(nested))
    # a fetcher capture marked as not evidence
    capture = _fetcher(
        tmp_path, {"strategy_9001_app_investment_perfdata_init.json": _perf()}
    )
    manifest_path = capture / "capture-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["evidence_eligible"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(admit.AdmissionRefused, match="dry-run/rehearsal"):
        _run(tmp_path, _recorder(tmp_path), fetchers=[capture], out=_out(tmp_path, "f"))
    # a HAR root produced by a dry-run admission cannot be the base of the next one
    produced = _out(tmp_path, "first")
    _run(tmp_path, _recorder(tmp_path, "ok"), out=produced)
    with pytest.raises(admit.AdmissionRefused, match="dry-run/rehearsal"):
        _run(
            tmp_path,
            _recorder(tmp_path, "ok2"),
            base=produced / "har-root",
            out=_out(tmp_path, "chained"),
        )
    # stage-designer refuses the same inputs
    model, full = _designer()
    drun = _designer_run(tmp_path, model, full, "dd")
    _mark(drun, evidence_eligible=False)
    with pytest.raises(admit.AdmissionRefused, match="dry-run/rehearsal"):
        admit.stage_designer(
            out=_out(tmp_path, "sd"),
            uid=8001,
            recorder=drun,
            fetcher=None,
            dry_run=True,
        )
