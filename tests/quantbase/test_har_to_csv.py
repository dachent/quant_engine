"""Synthetic-fixture tests for the Quantbase HAR-to-CSV converter.

Every fixture is generated here and shaped like the response key sets observed in
the retained 2026-09-02 capture; no value is real.
"""

from __future__ import annotations

import copy
import csv
import importlib.util
import io
import json
from pathlib import Path
import sys
from typing import Any, Callable

import capture_quantbase_har as intake
import pytest
import quantbase_har_to_csv as converter


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "quantbase_generation_converter_test",
    REPOSITORY_ROOT / "tools" / "quantbase_generation.py",
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("unable to load Quantbase generation tool")
generation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generation
SPEC.loader.exec_module(generation)

ERROR = converter.QuantbaseCsvError
APPROVAL = "https://github.com/dachent/quant_engine/issues/1#issuecomment-2"
SESSION_VALUE = "SYNTHETIC_SESSION_VALUE_123456"
LONG_NAV = "1.0123456789012345678901234"
API = "https://qbapi3.com"
APP = "https://app.getquantbase.com"
VOLATIS = "Volatis Strategy"
OTHER = "Other - Fund & Co"
ACCOUNT_VALUE = "9.9876543210987654321098765"
FILE_NAMES = (
    "account_activity.csv",
    "accounts.csv",
    "dashboard_Single_Index_Leveraged_In_and_Out_pricehistory.csv",
    "dashboard_Total_holdings.csv",
    "dashboard_Total_pricehistory.csv",
    "dashboard_Volatis_Strategy_holdings.csv",
    "dashboard_Volatis_Strategy_pricehistory.csv",
    "dashboard_total_wins.csv",
    "partner_status.csv",
    "projections_all_live_funds.csv",
    "ssg_Other___Fund_and_Co_pricehistory.csv",
    "ssg_Volatis_Strategy_pricehistory.csv",
    "ssg_all_funds_stats.csv",
    "user_profile.csv",
    "volatis_strategy_rebalance_history.csv",
    "volatis_strategy_stats.csv",
)


def _bodies() -> dict[str, Any]:
    profile = dict.fromkeys(sorted(converter._PROFILE_KEYS), "x")
    profile["id"] = "11"
    profile["ob_status"] = dict.fromkeys(sorted(converter._ONBOARDING_KEYS), None)
    activity_keys = [
        key for key in converter.HEADERS["account_activity"] if key != "activity_type"
    ]
    order = dict.fromkeys(activity_keys)
    order.update(id="o-1", transaction="t-1", filled_at="2026-10-02T01:00:00Z")
    filled = dict.fromkeys(activity_keys)
    filled.update(id="t-1", fund="7", fund_title=VOLATIS, type="BUY", amount="5")
    stat = dict.fromkeys(converter.HEADERS["fund_stats"], "s")
    stat.update(id=7, title=VOLATIS, scenarios=[{"a": 1}], live=True, partner=None)
    mark = {"days_ago": 3, "fund_id": 7, "percentage": 1.5}
    wins = {
        "total": {
            "percentage": 2.5,
            "spy_percentage": 1.0,
            "spy_value_delta": 3.0,
            "value_delta": 4.0,
        },
        "biggest": mark,
        "longest": mark,
    }
    price = [
        {
            "time": "10/02/2026",
            "value": ACCOUNT_VALUE,
            "cost_basis": "1.0",
            "profit_loss": "0.5",
        }
    ]
    holding = {
        "symbol": "SYN",
        "quantity": "1",
        "value": "2",
        "latest_price": "2",
        "percentage": "100",
    }

    def fund(identifier: int, *, win: object, held: bool) -> dict[str, Any]:
        return {
            "pricehistory": price,
            "fund": {"id": identifier, "name": "n"},
            "wins": win,
            "holdings": [holding] if held else [],
            "returns": {},
        }

    def nav(title: str, identifier: int, scale: float) -> list[dict[str, Any]]:
        return [
            {
                "fund_id": identifier,
                "fund_title": title,
                "time": day,
                "value": LONG_NAV
                if title == VOLATIS and day.startswith("2026-10-02")
                else str(scale + index),
            }
            for index, day in enumerate(
                [
                    "2026-09-27T00:00:00Z",
                    "2026-10-02T00:00:00Z",
                    "2026-10-07T00:00:00Z",
                ]
            )
        ]

    return {
        "users.profile": [profile],
        "accounts.linked": {
            "data": [
                {
                    "available_balance": "10.50",
                    "available_balance_cents": 1050,
                    "funds": [{"fund_id": "7", "value": ACCOUNT_VALUE}],
                    "id": "acct-1",
                    "restricted_to_liquidation": False,
                    "settled_balance": "10.50",
                    "settled_balance_cents": 1050,
                    "type": "ACTIVE",
                }
            ]
        },
        "accounts.activity": {
            "data": [
                {"item": order, "type": "order"},
                {"item": filled, "type": "filled_txn"},
            ]
        },
        "portfolio.dashboard": {
            "data": {
                "funds_data": {
                    "Single-Index Leveraged In & Out": fund(9, win=None, held=False),
                    VOLATIS: fund(7, win=wins, held=True),
                    "Total": fund(0, win=wins, held=True),
                },
                "top_wins": {"biggest": mark, "longest": mark},
            }
        },
        "partners.status": {
            "data": {"status": "ACTIVE", "next_period_end": "2124-07-24T05:00:00Z"}
        },
        "funds.statistics": {
            "data": {
                "max_drawdown": -0.1,
                "volatility": 0.2,
                "daily_volatility": 0.01,
                "sharpe": 1.5,
                "monthly_volatility": 0.05,
            }
        },
        "funds.rebalance-history": {
            "data": {
                "2026-09-27": {"composition": [{"symbol": "AAA", "proportion": "0.5"}]},
                "2026-09-22": {
                    "composition": [
                        {"symbol": "AAA", "proportion": "0.25"},
                        {"symbol": "BBB", "proportion": "0.75"},
                    ]
                },
            }
        },
        "funds.invest": {
            "__N_SSG": True,
            "pageProps": {
                "fundPriceData": nav(VOLATIS, 7, 1.0) + nav(OTHER, 8, 50.0),
                "fundStatsData": {"data": [stat, {**stat, "id": 8, "title": OTHER}]},
            },
        },
    }


def _entry(
    url: str, body: object, *, method: str = "GET", status: int = 200, api: bool = True
) -> dict[str, Any]:
    headers = [{"name": "j" + "wt", "value": SESSION_VALUE}] if api else []
    return {
        "request": {"headers": headers, "method": method, "url": url},
        "response": {"content": {"text": json.dumps(body)}, "status": status},
    }


def _entries(
    bodies: dict[str, Any], omit: set[str], stats_end: str = "2026-10-07"
) -> list[dict[str, Any]]:
    entries = [
        _entry(f"{API}/api/users/?platform=web&u=synthetic", bodies["users.profile"]),
        _entry(f"{API}/api/users/123/dashboard_data/", bodies["portfolio.dashboard"]),
        _entry(f"{API}/api/users/123/accounts/", bodies["accounts.linked"]),
        _entry(f"{API}/api/users/123/accounts/activity/", bodies["accounts.activity"]),
        _entry(
            f"{API}/api/funds/stats/?end_day={stats_end}&fund=Volatis%20Strategy"
            "&start_day=2026-09-27",
            bodies["funds.statistics"],
        ),
        _entry(
            f"{API}/api/funds/rebalance_history/?title=Volatis%20Strategy",
            bodies["funds.rebalance-history"],
        ),
        _entry(
            f"{API}/api/users/123/partners/statis/status/",
            bodies.get("partners.status"),
        ),
        _entry(
            f"{APP}/_next/data/BUILD/invest.json", bodies["funds.invest"], api=False
        ),
        # Disposed noise the reviewed contract excludes.
        _entry(f"{API}/api/users/123/accounts/", {}, method="OPTIONS"),
        _entry(f"{API}/api/users/123/track-event/", {"ok": 1}, method="POST"),
        _entry(f"{APP}/_next/static/chunks/app.js", "x", api=False),
        _entry("https://static.example.test/lib.js", "x", api=False),
    ]
    if "partners.status" in omit:
        del entries[6]
    return entries


def _count(bodies: dict[str, Any], capability: str, key: str | None) -> int:
    node: Any = bodies[capability]
    if key is None:
        return len(node) if isinstance(node, list) else 1
    for part in key.split("."):
        node = node[part]
    return len(node)


def _manifest(
    bodies: dict[str, Any], repeats: dict[str, int], omit: set[str]
) -> dict[str, Any]:
    keys = {
        "users.profile": None,
        "accounts.linked": "data",
        "accounts.activity": "data",
        "portfolio.dashboard": None,
        "partners.status": None,
        "funds.statistics": None,
        "funds.rebalance-history": None,
        "funds.invest": "pageProps.fundPriceData",
    }
    capabilities = {}
    for name, key in keys.items():
        if name in omit:
            continue
        times = repeats.get(name, 1)
        capabilities[name] = {
            "expected_responses": times,
            "expected_records": _count(bodies, name, key) * times,
            "records_key": key,
        }
    return {
        "schema_version": 1,
        "provider_id": "quantbase",
        "provenance": {
            "kind": "OWNER_BROWSER_HAR_EXPORT",
            "approval_reference": APPROVAL,
        },
        "capture_files": [{"name": "owner_export.har"}],
        "session": {
            "captured_at": "2026-10-08T12:00:00+00:00",
            "expires_at": "2026-10-08T13:00:00+00:00",
        },
        "window": {"start": "2026-01-01", "end": "2026-12-31"},
        "window_fields": ["time"],
        "cutoff": "2026-10-07",
        "nav_grid": {
            "anchor": "2026-09-22",
            "step_days": 5,
            "capabilities": ["funds.invest"],
        },
        "capabilities": capabilities,
        "ignored_origins": ["https://static.example.test"],
    }


class Case:
    """A staged, parsed and verified intake built from mutable synthetic bodies."""

    def __init__(
        self,
        root: Path,
        mutate: Callable[[dict[str, Any]], None] | None = None,
        *,
        extra: list[dict[str, Any]] | None = None,
        repeats: dict[str, int] | None = None,
        omit: set[str] | None = None,
        stats_end: str = "2026-10-07",
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        bodies = _bodies()
        if mutate is not None:
            mutate(bodies)
        skipped = omit or set()
        entries = _entries(bodies, skipped, stats_end) + (extra or [])
        manifest = _manifest(bodies, repeats or {}, skipped)
        capture = root / "capture"
        capture.mkdir()
        (capture / "owner_export.har").write_bytes(
            json.dumps({"log": {"entries": entries}}).encode("utf-8")
        )
        manifest_path = root / "manifest.json"
        manifest_path.write_bytes(json.dumps(manifest).encode("utf-8"))
        staging = root / "staging"
        staging.mkdir()
        self.out = root / "out"
        self.out.mkdir()
        staged = intake.stage_intake(manifest_path, capture, staging)
        self.intake = Path(str(staged["intake_dir"]))
        intake.parse_intake(self.intake)

    def convert(self, mode: str = "public") -> dict[str, object]:
        return converter.convert(self.intake, self.out, mode)

    def output(self) -> Path:
        return self.out / "quantbase-har-csv" / self.intake.name


def _rows(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def _ready(tmp_path: Path, mode: str = "public") -> tuple[Case, Path]:
    case = Case(tmp_path)
    case.convert(mode)
    return case, case.output()


def test_headers_and_required_files_match_the_generation_tool() -> None:
    for family, header in converter.HEADERS.items():
        assert generation._HEADERS[family] == header
    assert converter.REQUIRED_FILES == generation._REQUIRED_FILES


def test_round_trip_produces_exactly_the_generation_layout(tmp_path: Path) -> None:
    case, target = _ready(tmp_path)
    assert {path.name for path in (target / "source" / "csv").iterdir()} == set(
        FILE_NAMES
    )
    assert {path.name for path in target.iterdir()} == {
        "source",
        "conversion-report.json",
        "native-manifest.json",
    }
    assert {path.name for path in (target / "source").iterdir()} == {
        "csv",
        "RUNBOOK_QUANTBASE.md",
    }
    for name in FILE_NAMES:
        header = _rows(target / "source" / "csv" / name)[0]
        assert header == list(generation._HEADERS[generation._family(name)])
    assert converter.verify_conversion(case.intake, case.out, "public")["state"] == (
        "VERIFIED"
    )
    report = json.loads((target / "conversion-report.json").read_bytes())
    assert report["csv_file_count"] == len(FILE_NAMES)
    assert report["csv_record_count"] == sum(
        len(_rows(target / "source" / "csv" / name)) - 1 for name in FILE_NAMES
    )
    assert report["derivation"]["total_wins_source_fund"] == VOLATIS


def test_values_come_from_raw_bodies_not_the_redacted_records(tmp_path: Path) -> None:
    case, target = _ready(tmp_path)
    records = (case.intake / "normalized" / "records.json").read_text(encoding="utf-8")
    assert LONG_NAV not in records and ACCOUNT_VALUE not in records
    assert "[REDACTED]" in records
    series = _rows(target / "source" / "csv" / "ssg_Volatis_Strategy_pricehistory.csv")
    assert LONG_NAV in {row[2] for row in series[1:]}
    activity = _rows(target / "source" / "csv" / "account_activity.csv")
    header = activity[0]
    ids = {row[header.index("id")] for row in activity[1:]}
    assert ids == {"o-1", "t-1"}
    assert {row[header.index("activity_type")] for row in activity[1:]} == {
        "order",
        "filled_txn",
    }


def test_account_level_mode_replaces_only_the_volatis_series(tmp_path: Path) -> None:
    _, public = _ready(tmp_path / "a", "public")
    _, account = _ready(tmp_path / "b", "account-level")
    name = "ssg_Volatis_Strategy_pricehistory.csv"
    other = "ssg_Other___Fund_and_Co_pricehistory.csv"
    assert (public / "source/csv" / other).read_bytes() == (
        account / "source/csv" / other
    ).read_bytes()
    rows = _rows(account / "source/csv" / name)
    assert rows[1] == [VOLATIS, "7", ACCOUNT_VALUE, "10/02/2026"]
    assert len(_rows(public / "source/csv" / name)) == 4


def test_conversion_is_deterministic_across_output_roots(tmp_path: Path) -> None:
    case = Case(tmp_path)
    second = tmp_path / "out2"
    second.mkdir()
    case.convert()
    converter.convert(case.intake, second, "public")
    left = case.output()
    right = second / "quantbase-har-csv" / case.intake.name
    names = sorted(
        p.relative_to(left).as_posix() for p in left.rglob("*") if p.is_file()
    )
    assert names == sorted(
        p.relative_to(right).as_posix() for p in right.rglob("*") if p.is_file()
    )
    # Absolute raw paths in the native manifest are the same intake in both.
    for name in names:
        assert (left / name).read_bytes() == (right / name).read_bytes()


def test_no_session_value_is_written(tmp_path: Path) -> None:
    _, target = _ready(tmp_path)
    for path in target.rglob("*"):
        if path.is_file():
            assert SESSION_VALUE.encode() not in path.read_bytes()


def test_native_manifest_drives_plan_and_build_with_no_hand_step(
    tmp_path: Path,
) -> None:
    case, target = _ready(tmp_path)
    report = json.loads((target / "conversion-report.json").read_bytes())
    manifest = json.loads((target / "native-manifest.json").read_bytes())
    assert set(manifest) == {"schema_version", "cutoff", "files"}
    assert manifest["cutoff"] == "2026-10-07"
    assert [item["role"] for item in manifest["files"]] == [
        "native-har",
        "har-intake-manifest",
    ]
    data = tmp_path / "dataQuantbase"
    data.mkdir()
    args = [
        "plan",
        str(target / "source"),
        str(data),
        "--as-of-date",
        manifest["cutoff"],
        "--quarantine-plan-id",
        "a" * 64,
        "--expected-file-count",
        str(report["csv_file_count"]),
        "--expected-record-count",
        str(report["csv_record_count"]),
        "--native-manifest",
        str(target / "native-manifest.json"),
    ]
    assert generation.main(args) == 0
    plans = list((data / "cutovers" / "plans").glob("*/plan.json"))
    assert len(plans) == 1
    catalog, built, _ = generation.build_generation(plans[0])
    assert catalog["reconciliation"]["record_count"] == report["csv_record_count"]
    assert built.generation_id


def test_conversion_is_create_only(tmp_path: Path) -> None:
    case, _ = _ready(tmp_path)
    with pytest.raises(ERROR, match="create-only"):
        case.convert()


def test_verify_refuses_tampering_and_undeclared_paths(tmp_path: Path) -> None:
    case, target = _ready(tmp_path)
    path = target / "source" / "csv" / "accounts.csv"
    original = path.read_bytes()
    path.write_bytes(original + b"x")
    with pytest.raises(ERROR, match="derived output differs"):
        converter.verify_conversion(case.intake, case.out, "public")
    path.write_bytes(original)
    (target / "source" / "csv" / "extra.csv").write_bytes(b"a\n")
    with pytest.raises(ERROR, match="undeclared"):
        converter.verify_conversion(case.intake, case.out, "public")
    (target / "source" / "csv" / "extra.csv").unlink()
    manifest = target / "native-manifest.json"
    manifest.write_bytes(manifest.read_bytes().replace(b"native-har", b"native-hax"))
    with pytest.raises(ERROR, match="derived output differs"):
        converter.verify_conversion(case.intake, case.out, "public")


def test_verify_requires_the_same_mode(tmp_path: Path) -> None:
    case, _ = _ready(tmp_path, "public")
    with pytest.raises(ERROR, match="derived output differs"):
        converter.verify_conversion(case.intake, case.out, "account-level")


def test_unverified_intake_is_refused(tmp_path: Path) -> None:
    case = Case(tmp_path)
    records = case.intake / "normalized" / "records.json"
    original = records.read_bytes()
    records.write_bytes(original + b" ")
    with pytest.raises(ERROR, match="does not verify"):
        case.convert()
    records.write_bytes(original)
    raw = case.intake / "raw" / "owner_export.har"
    raw.write_bytes(raw.read_bytes() + b" ")
    with pytest.raises(ERROR, match="does not verify"):
        case.convert()


def test_unparsed_intake_is_refused(tmp_path: Path) -> None:
    case = Case(tmp_path / "x")
    (case.intake / "parse-report.json").unlink()
    with pytest.raises(ERROR, match="does not verify"):
        case.convert()


def test_out_root_must_be_disjoint(tmp_path: Path) -> None:
    case = Case(tmp_path)
    with pytest.raises(ERROR, match="disjoint"):
        converter.convert(case.intake, case.intake / "raw", "public")
    with pytest.raises(ERROR, match="disjoint"):
        converter.convert(case.intake, REPOSITORY_ROOT / "docs", "public")
    with pytest.raises(ERROR, match="real absolute directory"):
        converter.convert(case.intake, Path("relative"), "public")


def test_mode_is_required_and_validated(tmp_path: Path) -> None:
    case = Case(tmp_path)
    with pytest.raises(ERROR, match="not recognised"):
        case.convert("both")
    with pytest.raises(SystemExit):
        converter.main(["convert", "--intake", str(case.intake), "--out-root", "x"])


def _drop(path: list[Any]) -> Callable[[dict[str, Any]], None]:
    def apply(bodies: dict[str, Any]) -> None:
        node: Any = bodies
        for part in path[:-1]:
            node = node[part]
        del node[path[-1]]

    return apply


def _add(path: list[Any], key: str) -> Callable[[dict[str, Any]], None]:
    def apply(bodies: dict[str, Any]) -> None:
        node: Any = bodies
        for part in path:
            node = node[part]
        node[key] = "surprise"

    return apply


SHAPE_MUTATIONS = {
    "profile extra key": _add(["users.profile", 0], "new_field"),
    "profile missing key": _drop(["users.profile", 0, "source"]),
    "onboarding extra key": _add(["users.profile", 0, "ob_status"], "new_field"),
    "accounts extra key": _add(["accounts.linked", "data", 0], "new_field"),
    "accounts missing key": _drop(["accounts.linked", "data", 0, "funds"]),
    "activity extra item key": _add(["accounts.activity", "data", 0, "item"], "new"),
    "activity extra entry key": _add(["accounts.activity", "data", 0], "new"),
    "dashboard extra fund key": _add(
        ["portfolio.dashboard", "data", "funds_data", VOLATIS], "new"
    ),
    "dashboard missing price key": _drop(
        [
            "portfolio.dashboard",
            "data",
            "funds_data",
            "Total",
            "pricehistory",
            0,
            "cost_basis",
        ]
    ),
    "dashboard extra top key": _add(["portfolio.dashboard", "data"], "new"),
    "stats missing key": _drop(["funds.statistics", "data", "sharpe"]),
    "stats extra key": _add(["funds.statistics", "data"], "new"),
    "rebalance extra key": _add(
        ["funds.rebalance-history", "data", "2026-09-27", "composition", 0], "new"
    ),
    "partner extra key": _add(["partners.status", "data"], "new"),
    "invest extra page key": _add(["funds.invest", "pageProps"], "new"),
    "invest extra price key": _add(
        ["funds.invest", "pageProps", "fundPriceData", 0], "new"
    ),
    "fund stats missing key": _drop(
        ["funds.invest", "pageProps", "fundStatsData", "data", 0, "partner"]
    ),
}


@pytest.mark.parametrize("name", sorted(SHAPE_MUTATIONS))
def test_unexpected_shape_fails_closed(tmp_path: Path, name: str) -> None:
    case = Case(tmp_path, SHAPE_MUTATIONS[name])
    with pytest.raises(ERROR, match="keys differ"):
        case.convert()
    assert not (case.out / "quantbase-har-csv").exists()


def test_error_names_only_keys_never_values(tmp_path: Path) -> None:
    case = Case(tmp_path, _add(["users.profile", 0], "new_field"))
    with pytest.raises(ERROR) as raised:
        case.convert()
    assert "new_field" in str(raised.value)
    assert SESSION_VALUE not in str(raised.value)


def test_conflicting_repeated_responses_fail_closed(tmp_path: Path) -> None:
    bodies = _bodies()
    changed = copy.deepcopy(bodies["accounts.linked"])
    changed["data"][0]["settled_balance"] = "11.00"
    extra = [_entry(f"{API}/api/users/123/accounts/", changed)]
    case = Case(tmp_path, extra=extra, repeats={"accounts.linked": 2})
    with pytest.raises(ERROR, match="conflicting repeated"):
        case.convert()


def test_identical_repeated_responses_are_accepted(tmp_path: Path) -> None:
    extra = [_entry(f"{API}/api/users/123/accounts/", _bodies()["accounts.linked"])]
    case = Case(tmp_path, extra=extra, repeats={"accounts.linked": 2})
    assert case.convert()["state"] == "CONVERTED"


def test_missing_required_capability_fails_closed(tmp_path: Path) -> None:
    def apply(bodies: dict[str, Any]) -> None:
        bodies["users.profile"] = []

    case = Case(tmp_path, apply)
    with pytest.raises(ERROR, match="exactly one user"):
        case.convert()


def test_partner_status_is_optional(tmp_path: Path) -> None:
    case = Case(tmp_path, omit={"partners.status"})
    assert case.convert()["csv_file_count"] == len(FILE_NAMES) - 1
    assert not (case.output() / "source/csv/partner_status.csv").exists()


def test_duplicate_fund_time_fails_closed(tmp_path: Path) -> None:
    def apply(bodies: dict[str, Any]) -> None:
        rows = bodies["funds.invest"]["pageProps"]["fundPriceData"]
        rows.append(dict(rows[0]))

    with pytest.raises(ERROR, match="repeats a fund and time"):
        Case(tmp_path, apply).convert()


def test_fund_id_conflict_fails_closed(tmp_path: Path) -> None:
    def apply(bodies: dict[str, Any]) -> None:
        bodies["funds.invest"]["pageProps"]["fundPriceData"][1]["fund_id"] = 99

    with pytest.raises(ERROR, match="more than one fund id"):
        Case(tmp_path, apply).convert()


def test_unsafe_fund_title_fails_closed(tmp_path: Path) -> None:
    def apply(bodies: dict[str, Any]) -> None:
        for row in bodies["funds.invest"]["pageProps"]["fundPriceData"]:
            if row["fund_title"] == OTHER:
                row["fund_title"] = "Bad/Title"

    with pytest.raises(ERROR, match="no safe file name"):
        Case(tmp_path, apply).convert()


def test_colliding_file_names_fail_closed(tmp_path: Path) -> None:
    def apply(bodies: dict[str, Any]) -> None:
        rows = bodies["funds.invest"]["pageProps"]["fundPriceData"]
        # "Other _ Fund & Co" and OTHER both map to the stem Other___Fund_and_Co.
        rows.extend(
            {**row, "fund_title": "Other _ Fund & Co", "fund_id": 9}
            for row in copy.deepcopy(rows)
            if row["fund_title"] == OTHER
        )

    with pytest.raises(ERROR, match="collide"):
        Case(tmp_path, apply).convert()


def test_unsupported_capability_fails_closed(tmp_path: Path) -> None:
    case = Case(tmp_path)
    document = json.loads((case.intake / "manifest.json").read_bytes())
    document["capabilities"]["funds.projections"] = {
        "expected_responses": 1,
        "expected_records": 1,
        "records_key": None,
    }
    other = tmp_path / "other"
    other.mkdir()
    capture = other / "capture"
    capture.mkdir()
    har = json.loads((case.intake / "raw" / "owner_export.har").read_bytes())
    har["log"]["entries"].append(
        _entry(f"{APP}/_next/data/BUILD/projections.json", {"a": 1}, api=False)
    )
    (capture / "owner_export.har").write_bytes(json.dumps(har).encode("utf-8"))
    manifest = other / "manifest.json"
    manifest.write_bytes(json.dumps(document).encode("utf-8"))
    (other / "staging").mkdir()
    staged = intake.stage_intake(manifest, capture, other / "staging")
    intake.parse_intake(Path(str(staged["intake_dir"])))
    (other / "out").mkdir()
    with pytest.raises(ERROR, match="no CSV conversion"):
        converter.convert(Path(str(staged["intake_dir"])), other / "out", "public")


def test_other_funds_statistics_are_refused(tmp_path: Path) -> None:
    case = Case(tmp_path)
    document = json.loads((case.intake / "manifest.json").read_bytes())
    document["capabilities"]["funds.statistics"]["expected_responses"] = 2
    document["capabilities"]["funds.statistics"]["expected_records"] = 2
    other = tmp_path / "other"
    capture = other / "capture"
    capture.mkdir(parents=True)
    har = json.loads((case.intake / "raw" / "owner_export.har").read_bytes())
    har["log"]["entries"].append(
        _entry(
            f"{API}/api/funds/stats/?end_day=2026-10-07&fund=Other&start_day=2026-09-27",
            _bodies()["funds.statistics"],
        )
    )
    (capture / "owner_export.har").write_bytes(json.dumps(har).encode("utf-8"))
    (other / "manifest.json").write_bytes(json.dumps(document).encode("utf-8"))
    (other / "staging").mkdir()
    (other / "out").mkdir()
    staged = intake.stage_intake(other / "manifest.json", capture, other / "staging")
    directory = Path(str(staged["intake_dir"]))
    intake.parse_intake(directory)
    with pytest.raises(ERROR, match="exactly the Volatis fund"):
        converter.convert(directory, other / "out", "public")


def test_cell_projection_matches_the_ledger_projection() -> None:
    assert [
        converter._cell(value)
        for value in (None, True, False, 3, 1.0, "a", {"k": "é"}, [1, "b"])
    ] == ["", "True", "False", "3", "1.0", "a", '{"k": "é"}', '[1, "b"]']


def test_csv_rows_use_crlf_like_the_retained_converter() -> None:
    assert converter._csv_bytes(("a", "b"), [[1, None]]) == b"a,b\r\n1,\r\n"
    text = converter._csv_bytes(("a",), [["x,y"], ['q"r']]).decode("utf-8")
    assert list(csv.reader(io.StringIO(text, newline=""))) == [["a"], ["x,y"], ['q"r']]


def test_a_body_that_echoes_the_session_value_is_refused(tmp_path: Path) -> None:
    def apply(bodies: dict[str, Any]) -> None:
        bodies["users.profile"][0]["source"] = SESSION_VALUE

    case = Case(tmp_path, apply)
    with pytest.raises(ERROR, match="session material"):
        case.convert()
    assert not (case.out / "quantbase-har-csv").exists()


def _later_rows(bodies: dict[str, Any]) -> None:
    """Observations dated after the 2026-10-07 cutoff in every time series."""

    prices = bodies["funds.invest"]["pageProps"]["fundPriceData"]
    prices.extend({**row, "time": "2026-10-12T00:00:00Z"} for row in list(prices)[::3])
    for fund in bodies["portfolio.dashboard"]["data"]["funds_data"].values():
        fund["pricehistory"].append({**fund["pricehistory"][0], "time": "10/12/2026"})
    items = bodies["accounts.activity"]["data"]
    items.append(
        {
            "item": {
                **items[0]["item"],
                "id": "late",
                "filled_at": "2026-10-10T01:00:00Z",
            },
            "type": "order",
        }
    )
    bodies["funds.rebalance-history"]["data"]["2026-10-12"] = {
        "composition": [{"symbol": "ZZZ", "proportion": "1"}]
    }


def test_observations_after_the_cutoff_are_excluded_and_counted(
    tmp_path: Path,
) -> None:
    case = Case(tmp_path, _later_rows)
    case.convert()
    target = case.output()
    csv_dir = target / "source" / "csv"
    for name in (
        "ssg_Volatis_Strategy_pricehistory.csv",
        "ssg_Other___Fund_and_Co_pricehistory.csv",
    ):
        assert len(_rows(csv_dir / name)) == 4  # header + the three <= cutoff
    assert all(
        row[3] <= "2026-10-07T23"
        for row in _rows(csv_dir / "ssg_Volatis_Strategy_pricehistory.csv")[1:]
    )
    assert len(_rows(csv_dir / "dashboard_Total_pricehistory.csv")) == 2
    activity = _rows(csv_dir / "account_activity.csv")
    assert "late" not in {row[activity[0].index("id")] for row in activity[1:]}
    assert len(activity) == 3
    assert (
        "2026-10-12"
        not in (csv_dir / "volatis_strategy_rebalance_history.csv").read_text()
    )
    report = json.loads((target / "conversion-report.json").read_bytes())
    excluded = report["derivation"]["excluded_after_cutoff"]
    assert excluded == {
        "account_activity": 1,
        "dashboard_pricehistory": 9,  # the fixture shares one series across 3 funds
        "rebalance_history": 1,
        "ssg_pricehistory": 2,
    }
    assert report["cutoff"] == "2026-10-07"
    assert report["capture_date"] == "2026-10-08"
    assert "accounts.csv" in report["snapshot_files"]
    assert "dashboard_Total_holdings.csv" in report["snapshot_files"]
    # The later rows stay in the retained source evidence.
    raw = (case.intake / "raw" / "owner_export.har").read_text(encoding="utf-8")
    assert "2026-10-12T00:00:00Z" in raw and "late" in raw
    assert converter.verify_conversion(case.intake, case.out, "public")["state"] == (
        "VERIFIED"
    )


def test_statistics_window_after_the_cutoff_is_refused(tmp_path: Path) -> None:
    case = Case(tmp_path, stats_end="2026-10-12")
    with pytest.raises(ERROR, match="window ends after the cutoff"):
        case.convert()


def test_a_fund_with_a_null_wins_total_is_skipped(tmp_path: Path) -> None:
    def apply(bodies: dict[str, Any]) -> None:
        funds = bodies["portfolio.dashboard"]["data"]["funds_data"]
        funds["Single-Index Leveraged In & Out"]["wins"] = {"total": None}

    case = Case(tmp_path, apply)
    case.convert()
    report = json.loads((case.output() / "conversion-report.json").read_bytes())
    assert report["derivation"]["total_wins_source_fund"] == VOLATIS


def test_two_requests_for_one_fund_are_refused(tmp_path: Path) -> None:
    extra = [
        _entry(
            f"{API}/api/funds/stats/?end_day=2026-10-07&fund=Volatis%20Strategy"
            "&start_day=2026-09-01",
            _bodies()["funds.statistics"],
        )
    ]
    case = Case(tmp_path, extra=extra, repeats={"funds.statistics": 2})
    with pytest.raises(ERROR, match="more than one request per fund"):
        case.convert()


def test_a_linked_output_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = Case(tmp_path)
    holder = case.out / "quantbase-har-csv"
    holder.mkdir()
    real = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: self.name == "quantbase-har-csv" or real(self),
    )
    with pytest.raises(ERROR, match="linked or not a directory"):
        case.convert()
    assert not any(holder.iterdir())
