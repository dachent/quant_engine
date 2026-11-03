"""Ingest adapter and native-intake generation, proven offline.

Everything is synthetic.  Activity shapes follow the Alpaca documentation and the
driver's normalizer (UNVERIFIED against a live response: the retained legacy
export fetched zero activities, so no real rows exist locally to copy shapes
from).  The legacy byte-identity tests pin the digests of the unmodified
generation over its own synthetic source and, when the retained OneDrive tree is
present, over the real retained Alpaca generation.
"""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

import pytest

from alpaca_capture_support import (
    ACCOUNT_ID,
    account_body,
    capture_config,
    fill,
    governed,
    nontrade,
    ok,
    page,
    positions_body,
)
from quant_engine.alpaca import (
    AlpacaGenerationError,
    build_generation,
    create_plan,
    cutover_source,
    publish_candidate,
    stage_candidate,
    verify_published,
)
from quant_engine.alpaca.capture import (
    CaptureIncomplete,
    CaptureRefused,
    RawResponse,
    run_capture,
    verify_run,
)
from quant_engine.alpaca.capture import cli
from quant_engine.alpaca.capture.ingest import (
    IngestRefused,
    check_pagination,
    ingest_run,
    load_crosswalk,
    native_row,
)
from quant_engine.alpaca.capture.util import sha256_text
from quant_engine.alpaca.native_activities import (
    ACCOUNT_FILE,
    ACTIVITIES_FILE,
    BINDING_FILE,
    POSITIONS_FILE,
    SPEC_FILE,
    NativeActivitiesError,
    canonical_bytes,
    load_native_package,
    row_order,
    sha256_hex,
    summarize,
    window_dates,
)
from test_alpaca_generation import _source

INTERNAL = "account-00000000000000a1"
AFTER, UNTIL = "2024-01-01", "2024-12-31"

# Digests of the generation built by the code before native intake existed, over
# the synthetic legacy source in test_alpaca_generation (the plan id is not
# pinned: it embeds the temporary directory).
_LEGACY_BASELINE = {
    "generation_id": "31fe06455f7d6663dfe7d5c48b8dd5d9841f10ae6e604351773f9680d0b16c91",
    "catalog_sha256": "f66c1f9dc8e788f7f0319fe204a9d0c240c2b511a2ac860a6554154cb337c3fb",
}
_RETAINED_ROOT = Path(
    os.environ.get(
        "QE_RETAINED_DATA_ALPACA",
        r"C:\Users\BorisVaisman\V Capital\investing - Documents\_quant_engine\dataAlpaca",
    )
)
_RETAINED_GENERATION = (
    "7942ee08c1a3affa628c45b24f758cbe822aeb140f10845e155e042d70b593fc"
)
_RETAINED_PLAN = "56d8a79d34a6d2c1ea0637ff274c6844ccd0ddbf4e4fc19072a00d692715b03a"


def _jnls() -> dict[str, Any]:
    return {
        "id": "20240201000000009::synthetic-jnls-9",
        "activity_type": "JNLS",
        "date": "2024-02-01",
        "symbol": "SYNA",
        "qty": "1",
    }


PAGES: list[list[dict[str, Any]]] = [
    [fill(1), fill(2, side="sell", price="11")],
    [nontrade(3, "DIV", "0.42", symbol="SYNA"), nontrade(4, "CSD", "200")],
    [nontrade(5, "FEE", "-0.10"), nontrade(6, "CSW", "-25")],
    [nontrade(7, "JNL", "5"), _jnls()],
    [],
]
# -21.00 buy leg, +22.00 sell leg, 0.42 dividend, 200 deposit, -0.10 fee,
# -25 withdrawal, +5 journal.
NET_CASH = "181.32"


def _script(
    pages: list[list[dict[str, Any]]], account: bytes | None = None
) -> list[RawResponse]:
    return [
        ok(account or account_body()),
        ok(positions_body()),
        *[ok(page(rows)) for rows in pages],
    ]


def _make_run(
    tmp_path: Path,
    pages: list[list[dict[str, Any]]] | None = None,
    *,
    account: bytes | None = None,
    run_id: str = "run-1",
    **config: Any,
) -> Path:
    capture = capture_config(**config)
    evidence = tmp_path / f"gov-{run_id}"
    evidence.mkdir(exist_ok=True)
    fetcher, _, governance = governed(
        evidence, capture, _script(PAGES if pages is None else pages, account)
    )
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    return run_capture(
        config=capture,
        fetcher=fetcher,
        governance=governance,
        staging_root=staging,
        run_id=run_id,
    ).run_directory


def _crosswalk(
    tmp_path: Path,
    binding: str | None = None,
    account: str = INTERNAL,
    name: str = "crosswalk.json",
) -> Path:
    path = tmp_path / name
    path.write_bytes(
        json.dumps(
            {
                "provider": "ALPACA",
                "rows": [
                    {
                        "account_binding_sha256": binding or sha256_text(ACCOUNT_ID),
                        "internal_account_id": account,
                    }
                ],
                "schema_version": 1,
            }
        ).encode()
    )
    return path


def _out(tmp_path: Path) -> Path:
    root = tmp_path / "ingest"
    root.mkdir(exist_ok=True)
    return root


def _ingest(
    tmp_path: Path,
    run: Path,
    *,
    after: str = AFTER,
    until: str = UNTIL,
    crosswalk: Path | None = None,
    account: str = INTERNAL,
    package_id: str | None = None,
) -> Any:
    return ingest_run(
        run_directory=run,
        crosswalk=load_crosswalk(crosswalk or _crosswalk(tmp_path)),
        internal_account_id=account,
        expect_after=after,
        expect_until=until,
        output_root=_out(tmp_path),
        package_id=package_id,
    )


def _package(tmp_path: Path) -> Path:
    return cast(Path, _ingest(tmp_path, _make_run(tmp_path)).package_directory)


def _tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


# --- the adapter ---------------------------------------------------------


def test_verified_run_becomes_a_native_package_with_every_cash_movement(
    tmp_path: Path,
) -> None:
    run = _make_run(tmp_path)
    assert verify_run(run).ok
    result = _ingest(tmp_path, run)
    package = load_native_package(
        result.package_directory, binding_sha256=result.binding_sha256
    )
    assert result.activity_count == 8 == len(package.rows)
    assert result.net_cash_amount == NET_CASH
    summary = package.summary
    assert summary["net_cash_amount"] == NET_CASH
    assert summary["activities_without_cash_amount"] == 1
    assert set(summary["by_category"]) == {
        "dividend",
        "fee",
        "fill",
        "transfer",
        "transfer_security",
    }
    assert summary["by_category"]["fill"]["cash_amount"] == "1.00"
    assert summary["by_type"]["JNL"]["cash_amount"] == "5"
    assert summary["by_type"]["CSW"]["negative"] == 1
    # The driver's own summary agrees with the native one, to the cent.
    driver = json.loads((run / "normalized/summary.json").read_text())
    assert Decimal(driver["net_cash_amount"]) == Decimal(NET_CASH)
    fills = [row for row in package.rows if row["activity_type"] == "FILL"]
    assert {row["cash_amount_basis"] for row in fills} == {"derived_qty_x_price"}
    assert sorted(row["cash_amount"] for row in fills) == ["-21.00", "22"]
    # Provider ids survive only as digests; no account identifier is retained.
    text = b"".join(
        (package.directory / name).read_bytes() for name in package.spec["files"]
    ).decode()
    assert "synthetic-fill-1" not in text and ACCOUNT_ID not in text
    assert package.rows[0]["activity_id_sha256"] == sha256_hex(
        PAGES[0][0]["id"].encode()
    )
    assert package.account["cash"] == "100.25"
    assert package.positions is not None and len(package.positions) == 2
    assert package.spec["window"] == {"after": AFTER, "until": UNTIL}
    assert package.spec["source_run"]["manifest_sha256"] == sha256_hex(
        (run / "manifest.json").read_bytes()
    )


def test_package_is_deterministic_and_binding_is_written_last(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    first = _ingest(tmp_path, run, package_id="one")
    second = _ingest(tmp_path, run, package_id="two")
    assert first.binding_sha256 == second.binding_sha256
    for name in (
        ACTIVITIES_FILE,
        ACCOUNT_FILE,
        POSITIONS_FILE,
        SPEC_FILE,
        BINDING_FILE,
    ):
        assert (first.package_directory / name).read_bytes() == (
            second.package_directory / name
        ).read_bytes()
    spec_digest = sha256_hex((first.package_directory / SPEC_FILE).read_bytes())
    binding = json.loads((first.package_directory / BINDING_FILE).read_text())
    assert binding["spec_sha256"] == spec_digest


def test_positions_are_optional(tmp_path: Path) -> None:
    capture = capture_config(include_positions=False)
    fetcher, _, governance = governed(
        tmp_path,
        capture,
        [ok(account_body()), ok(page([nontrade(1, "FEE", "-0.10")]))],
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    run = run_capture(
        config=capture,
        fetcher=fetcher,
        governance=governance,
        staging_root=staging,
        run_id="run-nopos",
    ).run_directory
    package = load_native_package(_ingest(tmp_path, run).package_directory)
    assert package.positions is None
    assert POSITIONS_FILE not in package.spec["files"]


def test_output_is_create_only(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    _ingest(tmp_path, run, package_id="same")
    with pytest.raises(IngestRefused, match="create-only"):
        _ingest(tmp_path, run, package_id="same")
    with pytest.raises(IngestRefused, match="plain identifier"):
        _ingest(tmp_path, run, package_id="../escape")
    with pytest.raises(IngestRefused, match="output root"):
        ingest_run(
            run_directory=run,
            crosswalk=load_crosswalk(_crosswalk(tmp_path)),
            internal_account_id=INTERNAL,
            expect_after=AFTER,
            expect_until=UNTIL,
            output_root="relative",
        )


# --- refusals: the run ---------------------------------------------------


def test_refuses_an_unverified_run_and_writes_nothing(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    raw = sorted((run / "raw").iterdir())[2]
    raw.write_bytes(raw.read_bytes() + b" ")
    assert not verify_run(run).ok
    with pytest.raises(IngestRefused, match="does not verify"):
        _ingest(tmp_path, run)
    assert _tree(_out(tmp_path)) == []


def test_refuses_a_run_with_no_manifest(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    (run / "MANIFEST.sha256").unlink()
    with pytest.raises(IngestRefused, match="manifest_missing"):
        _ingest(tmp_path, run)


def test_refuses_an_incomplete_run(tmp_path: Path) -> None:
    capture = capture_config()
    fetcher, _, governance = governed(
        tmp_path,
        capture,
        [
            ok(account_body()),
            ok(positions_body()),
            ok(page(PAGES[0])),
            RawResponse(status=429, body=b"{}", headers={"retry-after": "5"}),
        ],
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(CaptureIncomplete):
        run_capture(
            config=capture,
            fetcher=fetcher,
            governance=governance,
            staging_root=staging,
            run_id="partial",
        )
    with pytest.raises(IngestRefused, match="run_incomplete"):
        _ingest(tmp_path, staging / "partial")
    assert _tree(_out(tmp_path)) == []


def test_refuses_a_run_whose_window_differs_from_the_declared_window(
    tmp_path: Path,
) -> None:
    run = _make_run(tmp_path)
    with pytest.raises(IngestRefused, match="differs from the declared window"):
        _ingest(tmp_path, run, after="2023-12-31")
    with pytest.raises(IngestRefused, match="differs from the declared window"):
        _ingest(tmp_path, run, until="2025-01-01")


def test_refuses_an_unbounded_run_window(tmp_path: Path) -> None:
    run = _make_run(tmp_path, after=None)
    with pytest.raises(IngestRefused, match="not bounded"):
        _ingest(tmp_path, run, after="None")
    run_two = _make_run(tmp_path, run_id="run-2", until=None)
    with pytest.raises(IngestRefused, match="not bounded"):
        _ingest(tmp_path, run_two)


def test_refuses_an_activity_outside_the_declared_window(tmp_path: Path) -> None:
    early = [
        [fill(1, transaction_time="2023-12-31T23:59:59Z"), fill(2)],
        [],
    ]
    run = _make_run(tmp_path, early)
    assert verify_run(run).ok  # the driver does not filter by window
    with pytest.raises(IngestRefused, match="outside the declared window"):
        _ingest(tmp_path, run)
    late = [[nontrade(1, "FEE", "-1", date="2025-01-01")], []]
    page_size_one = _make_run(tmp_path, late, run_id="late", page_size=1)
    with pytest.raises(IngestRefused, match="outside the declared window"):
        _ingest(tmp_path, page_size_one)
    assert _tree(_out(tmp_path)) == []


def test_window_edges_are_inclusive_for_dates_and_instants(tmp_path: Path) -> None:
    rows = [
        nontrade(1, "FEE", "-1", date="2024-01-01"),
        nontrade(2, "FEE", "-2", date="2024-12-31"),
    ]
    run = _make_run(tmp_path, [rows, []])
    assert _ingest(tmp_path, run).activity_count == 2


def test_duplicate_activity_ids_are_refused(tmp_path: Path) -> None:
    duplicate = [[fill(1), fill(1)], []]
    capture = capture_config()
    fetcher, _, governance = governed(tmp_path, capture, _script(duplicate))
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(CaptureIncomplete):
        run_capture(
            config=capture,
            fetcher=fetcher,
            governance=governance,
            staging_root=staging,
            run_id="dup",
        )
    with pytest.raises(IngestRefused, match="run_incomplete"):
        _ingest(tmp_path, staging / "dup")


def test_a_duplicate_across_pages_is_refused(tmp_path: Path) -> None:
    across = [[fill(1), fill(2)], [fill(2), fill(3)], []]
    capture = capture_config()
    fetcher, _, governance = governed(tmp_path, capture, _script(across))
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(CaptureIncomplete):
        run_capture(
            config=capture,
            fetcher=fetcher,
            governance=governance,
            staging_root=staging,
            run_id="dup2",
        )
    with pytest.raises(IngestRefused):
        _ingest(tmp_path, staging / "dup2")


def test_refuses_a_non_usd_account(tmp_path: Path) -> None:
    run = _make_run(tmp_path, account=account_body(currency="EUR"))
    assert verify_run(run).ok
    with pytest.raises(IngestRefused, match="USD"):
        _ingest(tmp_path, run)


def test_refuses_a_tampered_request_manifest_with_a_page_gap(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    lines = (run / "request_manifest.jsonl").read_text().splitlines()
    kept = [line for index, line in enumerate(lines) if index != 3]
    (run / "request_manifest.jsonl").write_text("\n".join(kept) + "\n")
    with pytest.raises(IngestRefused, match="does not verify"):
        _ingest(tmp_path, run)


def _record(rows: int, cursor: str | None) -> dict[str, Any]:
    params: dict[str, str] = {"direction": "asc", "page_size": "2"}
    if cursor is not None:
        params["page_token"] = cursor
    return {"report": "activities", "safe_params": params, "row_count": rows}


def test_pagination_replay_accepts_a_continuous_terminated_chain() -> None:
    check_pagination([_record(2, None), _record(2, "a"), _record(1, "b")], 2)
    check_pagination([_record(2, None), _record(0, "a")], 2)
    check_pagination([_record(0, None)], 2)


@pytest.mark.parametrize(
    ("chain", "message"),
    [
        ([], "no activities page"),
        ([_record(2, "a")], "first page carries a cursor"),
        ([_record(1, None), _record(1, "a")], "follows a short page"),
        ([_record(2, None), _record(2, None)], "missing or repeated"),
        ([_record(2, None), _record(2, "a"), _record(1, "a")], "missing or repeated"),
        ([_record(2, None), _record(2, "a")], "unterminated"),
    ],
)
def test_pagination_replay_refuses_gaps(
    chain: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(IngestRefused, match=message):
        check_pagination(chain, 2)


# --- refusals: the crosswalk ---------------------------------------------


def test_refuses_a_run_bound_to_a_different_account_than_the_crosswalk(
    tmp_path: Path,
) -> None:
    run = _make_run(tmp_path)
    other = _crosswalk(
        tmp_path, binding=sha256_text("SOME-OTHER-ACCOUNT"), name="x.json"
    )
    with pytest.raises(IngestRefused, match="differs from the crosswalk"):
        _ingest(tmp_path, run, crosswalk=other)
    unknown = _crosswalk(tmp_path, account="account-00000000000000b2", name="y.json")
    with pytest.raises(IngestRefused, match="differs from the crosswalk"):
        _ingest(tmp_path, run, crosswalk=unknown)
    assert _tree(_out(tmp_path)) == []


def test_refuses_a_run_whose_account_snapshot_binding_was_altered(
    tmp_path: Path,
) -> None:
    run = _make_run(tmp_path)
    target = run / "normalized/account.json"
    target.write_bytes(
        target.read_bytes().replace(sha256_text(ACCOUNT_ID).encode(), b"0" * 64)
    )
    with pytest.raises(IngestRefused, match="does not verify"):
        _ingest(tmp_path, run)


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        {"provider": "ALPACA", "rows": [], "schema_version": 1},
        {"provider": "SCHWAB", "rows": [{}], "schema_version": 1},
        {
            "provider": "ALPACA",
            "rows": [{"internal_account_id": INTERNAL}],
            "schema_version": 1,
        },
        {
            "provider": "ALPACA",
            "rows": [
                {"internal_account_id": INTERNAL, "account_binding_sha256": "ab" * 32},
                {"internal_account_id": INTERNAL, "account_binding_sha256": "cd" * 32},
            ],
            "schema_version": 1,
        },
        {
            "provider": "ALPACA",
            "rows": [
                {"internal_account_id": INTERNAL, "account_binding_sha256": "ab" * 32},
                {
                    "internal_account_id": "account-00000000000000b2",
                    "account_binding_sha256": "ab" * 32,
                },
            ],
            "schema_version": 1,
        },
        {
            "provider": "ALPACA",
            "rows": [
                {"internal_account_id": "acct-1", "account_binding_sha256": "ab" * 32}
            ],
            "schema_version": 1,
        },
    ],
)
def test_malformed_crosswalks_are_refused(tmp_path: Path, document: Any) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        document if isinstance(document, str) else json.dumps(document),
        encoding="utf-8",
    )
    with pytest.raises(IngestRefused):
        load_crosswalk(path)
    with pytest.raises(IngestRefused):
        load_crosswalk(tmp_path / "absent.json")


# --- refusals: the reader (a package altered with consistent hashes) -----


def _repack(source: Path, destination: Path, mutate: Any) -> Path:
    """Rewrite a package with a consistent spec and binding after ``mutate``."""

    package = load_native_package(source)
    rows = copy.deepcopy(package.rows)
    spec = copy.deepcopy(package.spec)
    mutate(rows, spec)
    destination.mkdir()
    blobs = {
        ACTIVITIES_FILE: b"".join(canonical_bytes(row) for row in rows),
        ACCOUNT_FILE: (source / ACCOUNT_FILE).read_bytes(),
        POSITIONS_FILE: (source / POSITIONS_FILE).read_bytes(),
    }
    for name, data in blobs.items():
        spec["files"][name] = {"bytes": len(data), "sha256": sha256_hex(data)}
        (destination / name).write_bytes(data)
    spec["files"][ACTIVITIES_FILE]["row_count"] = len(rows)
    spec_bytes = canonical_bytes(spec)
    (destination / SPEC_FILE).write_bytes(spec_bytes)
    binding = copy.deepcopy(package.binding)
    binding["spec_sha256"] = sha256_hex(spec_bytes)
    (destination / BINDING_FILE).write_bytes(canonical_bytes(binding))
    return destination


def _keep_summary(rows: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    spec["summary"] = summarize(sorted(rows, key=row_order))


def test_repack_control_loads(tmp_path: Path) -> None:
    source = _package(tmp_path)
    out = _repack(source, tmp_path / "control", lambda rows, spec: None)
    assert (
        load_native_package(out).binding_sha256
        == load_native_package(source).binding_sha256
    )


def _dup(rows: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    rows.append(copy.deepcopy(rows[0]))
    rows.sort(key=row_order)
    _keep_summary(rows, spec)


def _eur(rows: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    rows[0]["currency"] = "EUR"


def _outside(rows: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    target = next(row for row in rows if row["activity_type"] == "FEE")
    target["event_date"] = "2025-06-01"
    rows.sort(key=row_order)
    _keep_summary(rows, spec)


def _unsorted(rows: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    rows.reverse()


def _bad_summary(rows: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    spec["summary"]["net_cash_amount"] = "0"


def _bad_sign(rows: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    target = next(row for row in rows if row["activity_type"] == "CSD")
    target["cash_amount"] = "-200"
    _keep_summary(rows, spec)


def _bad_fill_leg(rows: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    target = next(row for row in rows if row["activity_type"] == "FILL")
    target["cash_amount"] = "-1"
    _keep_summary(rows, spec)


def _extra_key(rows: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    rows[0]["note"] = "x"


def _unknown_type(rows: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    rows[0]["activity_type"] = "ZZZ"


def _cash_without_basis(rows: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    target = next(row for row in rows if row["activity_type"] == "FEE")
    target["cash_amount_basis"] = None
    _keep_summary(rows, spec)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_dup, "duplicate activity digest"),
        (_eur, "USD"),
        (_outside, "outside the declared window"),
        (_unsorted, "canonical order"),
        (_bad_summary, "summary differs"),
        (_bad_sign, "opposite sign"),
        (_bad_fill_leg, "differs from quantity times price"),
        (_extra_key, "exact native key set"),
        (_unknown_type, "not recognized"),
        (_cash_without_basis, "basis disagree"),
    ],
)
def test_reader_refuses_altered_packages_even_with_consistent_hashes(
    tmp_path: Path, mutate: Any, message: str
) -> None:
    source = _package(tmp_path)
    altered = _repack(source, tmp_path / "altered", mutate)
    with pytest.raises(NativeActivitiesError, match=message):
        load_native_package(altered)


def test_reader_refuses_hash_drift_extra_files_and_wrong_pin(tmp_path: Path) -> None:
    source = _package(tmp_path)
    good = load_native_package(source)
    with pytest.raises(NativeActivitiesError, match="pinned digest"):
        load_native_package(source, binding_sha256="0" * 64)
    (source / "stray.txt").write_text("x")
    with pytest.raises(NativeActivitiesError, match="missing or extra"):
        load_native_package(source)
    (source / "stray.txt").unlink()
    (source / ACCOUNT_FILE).write_bytes(
        (source / ACCOUNT_FILE).read_bytes()[:-2] + b"\n"
    )
    with pytest.raises(NativeActivitiesError):
        load_native_package(source, binding_sha256=good.binding_sha256)
    with pytest.raises(NativeActivitiesError, match="absolute directory"):
        load_native_package("relative")


def test_native_row_drops_ids_and_free_text() -> None:
    row = native_row(
        {
            "activity_id": "ID-1",
            "activity_type": "FEE",
            "category": "fee",
            "cash_amount": "-1",
            "cash_amount_basis": "reported",
            "description": "free text",
            "fill_kind": None,
            "net_amount": "-1",
            "occurred_at": None,
            "occurred_date": "2024-02-01",
            "order_id": "ORD-1",
            "per_share_amount": None,
            "price": None,
            "quantity": None,
            "side": None,
            "status": None,
            "symbol": None,
        }
    )
    assert "description" not in row and "net_amount" not in row
    assert row["activity_id_sha256"] == hashlib.sha256(b"ID-1").hexdigest()
    assert row["order_id_sha256"] == hashlib.sha256(b"ORD-1").hexdigest()


# --- the CLI -------------------------------------------------------------


def test_cli_ingest_prints_hashes_and_counts_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _make_run(tmp_path)
    code = cli.main(
        [
            "ingest",
            "--run-dir",
            str(run),
            "--crosswalk",
            str(_crosswalk(tmp_path)),
            "--internal-account-id",
            INTERNAL,
            "--after",
            AFTER,
            "--until",
            UNTIL,
            "--output-root",
            str(_out(tmp_path)),
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    value = json.loads(output)
    assert value["activity_count"] == 8 and len(value["binding_sha256"]) == 64
    assert NET_CASH not in output and ACCOUNT_ID not in output


def test_cli_ingest_refusal_is_a_sanitized_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _make_run(tmp_path)
    code = cli.main(
        [
            "ingest",
            "--run-dir",
            str(run),
            "--crosswalk",
            str(_crosswalk(tmp_path, binding="ab" * 32)),
            "--internal-account-id",
            INTERNAL,
            "--after",
            AFTER,
            "--until",
            UNTIL,
            "--output-root",
            str(_out(tmp_path)),
        ]
    )
    assert code == 2
    assert "differs from the crosswalk" in capsys.readouterr().out
    assert issubclass(IngestRefused, CaptureRefused)


# --- the frozen generation consumes the package --------------------------


def _legacy_plan(tmp_path: Path, native: Path | None = None) -> tuple[Path, Path, Path]:
    source, data, tracker = _source(tmp_path)
    result = create_plan(
        source,
        data,
        tracker,
        internal_account_id="account-0000000000000001",
        as_of_date=date(2026, 7, 17),
        native_activities=native,
    )
    return Path(str(result["plan_path"])), source, data


def test_generation_consumes_the_package_as_ledger_transactions(tmp_path: Path) -> None:
    package = _package(tmp_path)
    plan, _, _ = _legacy_plan(tmp_path, package)
    catalog, manifest, data = build_generation(plan)
    cat = cast(dict[str, Any], catalog)
    transactions = cat["transactions"]
    native = [t for t in transactions if t["kind"].startswith("ACTIVITY_")]
    legacy = [t for t in transactions if t["kind"].startswith("ORDER_FILL_")]
    assert len(native) == 8 and len(legacy) == 1
    assert {t["account_id"] for t in native} == {INTERNAL}
    assert {t["account_id"] for t in legacy} == {"account-0000000000000001"}
    cash = sum(
        Decimal(t["amount"]["value"])
        for t in native
        if t["amount"]["status"] == "AVAILABLE"
    )
    assert cash == Decimal(NET_CASH)
    assert sum(t["amount"]["status"] == "UNAVAILABLE" for t in native) == 1
    assert {t["kind"] for t in native} == {
        "ACTIVITY_FILL",
        "ACTIVITY_DIV",
        "ACTIVITY_CSD",
        "ACTIVITY_CSW",
        "ACTIVITY_FEE",
        "ACTIVITY_JNL",
        "ACTIVITY_JNLS",
    }
    block = cat["native_activities"]
    assert block["summary"]["net_cash_amount"] == NET_CASH
    assert block["account_snapshot"]["cash"] == "100.25"
    assert block["window"] == {"after": AFTER, "until": UNTIL}
    assert [a["account_id"] for a in cat["accounts"]] == [
        "account-0000000000000001",
        INTERNAL,
    ]
    assert cat["reconciliation"]["native_activity_transaction_count"] == 8
    assert cat["reconciliation"]["ledger_transaction_count"] == 9
    assert "native capture window" in cat["coverage"]["out_of_boundary"][0]
    assert "account_activities" in manifest.coverage.fact_types
    assert "positions_snapshot" in manifest.coverage.fact_types
    assert cat["coverage"]["observed_start"] == "2024-01-02"
    artifact_ids = {a["artifact_id"] for a in cat["source_artifacts"]}
    assert {f"native/{ACTIVITIES_FILE}", f"native/{BINDING_FILE}"} <= artifact_ids
    assert ACCOUNT_ID not in data.decode()
    # Deterministic.
    assert build_generation(plan)[2] == data


def test_legacy_content_is_unchanged_by_native_intake(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    legacy_plan, _, _ = _legacy_plan(tmp_path / "a")
    native_plan, _, _ = _legacy_plan(tmp_path / "b", _package(tmp_path / "b"))
    legacy = cast(dict[str, Any], build_generation(legacy_plan)[0])
    combined = cast(dict[str, Any], build_generation(native_plan)[0])
    for key in (
        "orders",
        "nav_observations",
        "market_bars",
        "account_snapshots",
        "watchlists",
        "provenance",
        "filled_side_counts",
    ):
        assert legacy[key] == combined[key]
    legacy_ids = [
        t for t in combined["transactions"] if t["kind"].startswith("ORDER_FILL_")
    ]
    assert legacy_ids == legacy["transactions"]
    assert "native_activities" not in legacy


def test_legacy_only_generation_is_byte_identical_to_the_pre_native_baseline(
    tmp_path: Path,
) -> None:
    plan, _, _ = _legacy_plan(tmp_path)
    catalog, manifest, data = build_generation(plan)
    assert manifest.generation_id == _LEGACY_BASELINE["generation_id"]
    assert hashlib.sha256(data).hexdigest() == _LEGACY_BASELINE["catalog_sha256"]
    assert "native_activities" not in cast(dict[str, Any], catalog)
    plan_value = json.loads(plan.read_text(encoding="utf-8"))
    assert "native_activities" not in plan_value
    assert "native_activities" not in json.dumps(cast(dict[str, Any], catalog))


@pytest.mark.skipif(
    not (_RETAINED_ROOT / "CURRENT.json").is_file(),
    reason="the retained dataAlpaca tree is not on this machine",
)
def test_retained_alpaca_generation_rebuilds_byte_identically() -> None:
    plan = _RETAINED_ROOT / "cutovers" / "plans" / _RETAINED_PLAN / "plan.json"
    authority = (
        _RETAINED_ROOT / "source-runs" / "legacy-20260717-56d8a79d34a6" / "artifacts"
    )
    retained = (
        _RETAINED_ROOT / "generations" / _RETAINED_GENERATION / "alpaca-generation.json"
    )
    if not (plan.is_file() and authority.is_dir() and retained.is_file()):
        pytest.skip("the retained plan, authority tree, or catalog is not present")
    _, manifest, data = build_generation(plan, input_root=authority)
    assert manifest.generation_id == _RETAINED_GENERATION
    assert data == retained.read_bytes()


def test_native_generation_stages_publishes_verifies_and_cuts_over(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    plan, source, data_root = _legacy_plan(tmp_path, package)
    staged = stage_candidate(plan)
    generation_id = str(staged["generation_id"])
    published = publish_candidate(plan, generation_id)
    assert published["state"] == "PUBLISHED"
    assert verify_published(plan, generation_id)["state"] == "VERIFIED_PUBLISHED"
    published_catalog = json.loads(
        (
            data_root / "generations" / generation_id / "alpaca-generation.json"
        ).read_text()
    )
    assert published_catalog["native_activities"]["binding_sha256"] == (
        load_native_package(package).binding_sha256
    )
    result = cutover_source(plan, generation_id)
    assert result["state"] == "SINGLE_AUTHORITY_CUTOVER_COMPLETE"
    assert not source.exists()
    assert verify_published(plan, generation_id)["state"] == "VERIFIED_PUBLISHED"


def test_generation_refuses_a_package_changed_after_planning(tmp_path: Path) -> None:
    package = _package(tmp_path)
    plan, _, _ = _legacy_plan(tmp_path, package)
    activities = package / ACTIVITIES_FILE
    activities.write_bytes(activities.read_bytes().replace(b"-0.10", b"-0.11"))
    with pytest.raises(AlpacaGenerationError, match="differs"):
        build_generation(plan)


def test_generation_refuses_a_package_with_a_different_binding(tmp_path: Path) -> None:
    package = _package(tmp_path)
    plan, _, _ = _legacy_plan(tmp_path, package)
    altered = _repack(package, tmp_path / "swapped", lambda rows, spec: None)
    plan_value = json.loads(plan.read_text(encoding="utf-8"))
    plan_value["native_activities"]["directory"] = str(altered)
    # Same content repacked is the same binding, so alter one cash leg instead.
    other = _repack(package, tmp_path / "other", _drop_last)
    plan_value["native_activities"]["directory"] = str(other)
    tampered = plan.with_name("tampered.json")
    tampered.write_text(json.dumps(plan_value), encoding="utf-8")
    with pytest.raises(AlpacaGenerationError):
        build_generation(tampered)


def _drop_last(rows: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    rows.pop()
    _keep_summary(rows, spec)


def test_generation_plan_refuses_an_invalid_package(tmp_path: Path) -> None:
    broken = _package(tmp_path)
    (broken / SPEC_FILE).write_bytes(b"{}\n")
    with pytest.raises(AlpacaGenerationError):
        _legacy_plan(tmp_path, broken)


def test_tool_plan_accepts_native_activities(tmp_path: Path) -> None:
    from importlib import util

    spec = util.spec_from_file_location(
        "alpaca_generation_tool",
        Path(__file__).resolve().parents[2] / "tools" / "alpaca_generation.py",
    )
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parser = module._parser()
    package = _package(tmp_path)
    args = parser.parse_args(
        [
            "plan",
            "s",
            "d",
            "t",
            "--internal-account-id",
            "account-0000000000000001",
            "--as-of-date",
            "2026-07-17",
            "--native-activities",
            str(package),
        ]
    )
    assert args.native_activities == package
    bare = parser.parse_args(
        [
            "plan",
            "s",
            "d",
            "t",
            "--internal-account-id",
            "account-0000000000000001",
            "--as-of-date",
            "2026-07-17",
        ]
    )
    assert bare.native_activities is None


def test_refuses_a_run_filtered_by_activity_type(tmp_path: Path) -> None:
    run = _make_run(tmp_path, [[nontrade(1, "FEE", "-1")], []], activity_types=["FEE"])
    assert verify_run(run).ok
    with pytest.raises(IngestRefused, match="filtered by activity type"):
        _ingest(tmp_path, run)
    assert _tree(_out(tmp_path)) == []


def test_inverted_same_day_timestamp_window_is_refused() -> None:
    with pytest.raises(NativeActivitiesError, match="after its end"):
        window_dates("2024-01-01T18:00:00Z", "2024-01-01T09:00:00Z")


def test_generation_refuses_native_events_after_the_as_of_date(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    source, data, tracker = _source(tmp_path)
    with pytest.raises(AlpacaGenerationError, match="beyond the generation as-of"):
        create_plan(
            source,
            data,
            tracker,
            internal_account_id="account-0000000000000001",
            as_of_date=date(2024, 6, 30),
            native_activities=package,
        )
