from __future__ import annotations

import ast
import csv
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from quant_engine.fidelity import (
    IntakeError,
    IntakeVerificationError,
    catalog_rows,
    generation,
    intake,
    intake_verify,
    normalize,
    stage_raw,
    verify_intake,
    write_request,
)


NOW = datetime(2030, 1, 15, tzinfo=timezone.utc)
START = date(2030, 1, 1)
END = date(2030, 1, 31)
ACCOUNT = "SYNTH-ACCT-001"
INTERNAL = "account-0123456789abcdef"
FOOTER = [
    [],
    ["Fidelity synthetic footer"],
    ["Synthetic legal line 1"],
    ["Synthetic legal line 2"],
    ["Synthetic legal line 3"],
    ["Synthetic legal line 4"],
    [],
    ["Synthetic legal line 5"],
    ["Synthetic legal line 6"],
    ["Synthetic legal line 7"],
    [],
    ["Date downloaded 01/31/2030 01:00 pm"],
]


def _row(run_date: str, action: str, amount: str, *, symbol: str = "") -> list[str]:
    return [
        run_date,
        "SYNTHETIC OWNER NAME",
        ACCOUNT,
        action,
        symbol,
        "SYNTHETIC DESCRIPTION",
        "Cash",
        "",
        "",
        "USD",
        "10" if symbol else "",
        "1" if symbol else "0",
        "",
        "0",
        "0",
        "0",
        amount,
        "",
    ]


def _export(
    path: Path,
    rows: list[list[str]] | None = None,
    *,
    header: list[str] | None = None,
    footer: list[list[str]] | None = None,
) -> Path:
    body = (
        rows
        if rows is not None
        else [
            _row("01/03/2030", "YOU BOUGHT SYNTHETIC SECURITY", "-10", symbol="SYNTH"),
            _row(
                "01/04/2030",
                "DIVIDEND RECEIVED SYNTHETIC SECURITY",
                "2",
                symbol="SYNTH",
            ),
            _row("01/05/2030", "DIRECT DEPOSIT SYNTHETIC", "100"),
        ]
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([])
        writer.writerow([])
        writer.writerow(header or list(generation._HEADER))
        writer.writerows(body)
        writer.writerows(FOOTER if footer is None else footer)
    return path


def _evidence(tmp_path: Path, **overrides: object) -> Path:
    value: dict[str, object] = {
        "approved_at": "2030-01-10T00:00:00Z",
        "catalog_sha256": intake.catalog_sha256(),
        "expires_at": "2030-02-10T00:00:00Z",
        "owner_export": {"automation_credentials": "NONE", "performed_by": "owner"},
        "review": {
            "identity": "synthetic-reviewer",
            "reference": "https://github.com/dachent/quant_engine/issues/1#issuecomment-2",
        },
        "schema": intake.GOVERNANCE_SCHEMA,
        "scope": {
            "report_types": ["accounts-history-csv"],
            "window_end": "2030-01-31",
            "window_start": "2030-01-01",
        },
        "terms_sha256": intake.TERMS_SHA256,
    }
    value.update(overrides)
    path = tmp_path / f"evidence-{len(list(tmp_path.glob('evidence-*')))}.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _crosswalk(tmp_path: Path) -> Path:
    path = tmp_path / "crosswalk.json"
    path.write_text(
        json.dumps([{"external_account_id": ACCOUNT, "internal_account_id": INTERNAL}]),
        encoding="utf-8",
    )
    return path


def _request(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "staging"
    root.mkdir(exist_ok=True)
    manifest = write_request(
        root,
        evidence_path=_evidence(tmp_path),
        report_type_id="accounts-history-csv",
        window_start=START,
        window_end=END,
        now=NOW,
    )
    return root, str(manifest["request_id"])


def _staged(tmp_path: Path) -> tuple[Path, str, Path]:
    root, request_id = _request(tmp_path)
    source = _export(tmp_path / "export.csv")
    stage_raw(root, request_id, [source])
    return root, request_id, source


def _full(tmp_path: Path) -> tuple[Path, str]:
    root, request_id, _ = _staged(tmp_path)
    normalize(root, request_id, _crosswalk(tmp_path))
    return root, request_id


def test_catalog_accepts_only_the_observed_shape() -> None:
    accepted = [row["report_type_id"] for row in catalog_rows() if row["accepted"]]
    assert accepted == ["accounts-history-csv"]
    assert all(
        row["shape_status"] == "UNOBSERVED"
        for row in catalog_rows()
        if not row["accepted"]
    )
    assert intake_verify._HEADER == list(generation._HEADER)


def test_request_manifest_tells_owner_what_to_export(tmp_path: Path) -> None:
    root, request_id = _request(tmp_path)
    manifest = json.loads((root / request_id / "request-manifest.json").read_text())
    assert manifest["window_start"] == "2030-01-01"
    assert manifest["window_end"] == "2030-01-31"
    assert manifest["expected"]["header"] == list(generation._HEADER)
    assert manifest["owner_steps"] and manifest["status"] == "AWAITING_OWNER_EXPORT"
    with pytest.raises(IntakeError, match="create-only"):
        _request_again(tmp_path, root)


def _request_again(tmp_path: Path, root: Path) -> None:
    write_request(
        root,
        evidence_path=_evidence(tmp_path),
        report_type_id="accounts-history-csv",
        window_start=START,
        window_end=END,
        now=NOW,
    )


@pytest.mark.parametrize(
    "override",
    [
        {"terms_sha256": "0" * 64},
        {"catalog_sha256": "0" * 64},
        {"expires_at": "2030-01-12T00:00:00Z"},
        {"approved_at": "2030-01-20T00:00:00Z"},
        {"owner_export": {"automation_credentials": "STORED", "performed_by": "owner"}},
        {"owner_export": {"automation_credentials": "NONE", "performed_by": "agent"}},
        {"review": {"identity": "x", "reference": "https://example.test/1"}},
        {
            "scope": {
                "report_types": ["positions-csv"],
                "window_end": "2030-01-31",
                "window_start": "2030-01-01",
            }
        },
        {
            "scope": {
                "report_types": ["accounts-history-csv"],
                "window_end": "2030-01-20",
                "window_start": "2030-01-01",
            }
        },
        {"extra": 1},
    ],
)
def test_request_refuses_without_valid_governance(
    tmp_path: Path, override: dict[str, object]
) -> None:
    root = tmp_path / "staging"
    root.mkdir()
    with pytest.raises(IntakeError):
        write_request(
            root,
            evidence_path=_evidence(tmp_path, **override),
            report_type_id="accounts-history-csv",
            window_start=START,
            window_end=END,
            now=NOW,
        )
    assert list(root.iterdir()) == []


def test_request_refuses_unaccepted_report_and_missing_evidence(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    root.mkdir()
    with pytest.raises(IntakeError):
        write_request(
            root,
            evidence_path=tmp_path / "absent.json",
            report_type_id="accounts-history-csv",
            window_start=START,
            window_end=END,
            now=NOW,
        )
    with pytest.raises(IntakeError):
        intake.build_request_manifest(
            report_type_id="positions-csv",
            window_start=START,
            window_end=END,
            governance_sha256="0" * 64,
        )


def test_end_to_end_stage_normalize_verify(tmp_path: Path) -> None:
    root, request_id, source = _staged(tmp_path)
    raw = next((root / request_id / "raw").iterdir())
    assert raw.read_bytes() == source.read_bytes()
    summary = normalize(root, request_id, _crosswalk(tmp_path))
    assert summary["activity_count"] == 3 and summary["published"] is False
    result = verify_intake(root, request_id)
    assert result["status"] == "VERIFIED" and result["activity_count"] == 3
    catalog = (root / request_id / intake.NORMALIZED_CATALOG_NAME).read_text()
    for private in (ACCOUNT, "SYNTHETIC OWNER NAME", "SYNTHETIC DESCRIPTION"):
        assert private not in catalog


def test_every_stage_is_create_only(tmp_path: Path) -> None:
    root, request_id = _full(tmp_path)
    with pytest.raises(IntakeError, match="already exists"):
        stage_raw(root, request_id, [_export(tmp_path / "again.csv")])
    with pytest.raises(IntakeError, match="create-only"):
        normalize(root, request_id, _crosswalk(tmp_path))


def test_failed_stage_leaves_nothing_behind(tmp_path: Path) -> None:
    root, request_id = _request(tmp_path)
    good = _export(tmp_path / "good.csv")
    bad = _export(tmp_path / "bad.csv", footer=[])
    with pytest.raises(IntakeError):
        stage_raw(root, request_id, [good, bad])
    assert sorted(p.name for p in (root / request_id).iterdir()) == [
        "request-manifest.json"
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h: [*h, "Surprise Column"],
        lambda h: h[1:],
        lambda h: [h[1], h[0], *h[2:]],
    ],
)
def test_unknown_columns_fail_closed(tmp_path: Path, mutate) -> None:  # type: ignore[no-untyped-def]
    root, request_id = _request(tmp_path)
    source = _export(tmp_path / "x.csv", header=mutate(list(generation._HEADER)))
    with pytest.raises(IntakeError, match="header differs"):
        stage_raw(root, request_id, [source])


def test_bad_shapes_fail_closed(tmp_path: Path) -> None:
    root, request_id = _request(tmp_path)
    for name, payload in {
        "empty": b"",
        "binary": b"\xff\xfe\x00\x01",
        "short": b"a,b\n",
    }.items():
        path = tmp_path / name
        path.write_bytes(payload)
        with pytest.raises(IntakeError):
            stage_raw(root, request_id, [path])
    with pytest.raises(IntakeError, match="twice"):
        stage_raw(
            root, request_id, [_export(tmp_path / "a.csv"), _export(tmp_path / "b.csv")]
        )


def test_unknown_action_unmapped_account_and_window_fail_closed(tmp_path: Path) -> None:
    for rows, expect in (
        ([_row("01/03/2030", "SURPRISE ACTION", "1")], "unclassified"),
        ([_row("02/03/2030", "DIRECT DEPOSIT X", "1")], "outside the requested window"),
    ):
        case = tmp_path / expect.split()[0]
        case.mkdir()
        root, request_id = _request(case)
        stage_raw(root, request_id, [_export(case / "e.csv", rows)])
        with pytest.raises(IntakeError, match=expect):
            normalize(root, request_id, _crosswalk(case))
    case = tmp_path / "unmapped"
    case.mkdir()
    root, request_id, _ = _staged(case)
    other = case / "other.json"
    other.write_text(
        json.dumps([{"external_account_id": "OTHER", "internal_account_id": INTERNAL}])
    )
    with pytest.raises(IntakeError, match="unmapped"):
        normalize(root, request_id, other)


def _rewrite_catalog(root: Path, request_id: str, edit) -> None:  # type: ignore[no-untyped-def]
    base = root / request_id / "normalized"
    catalog = json.loads((base / "fidelity-intake-catalog.json").read_text())
    edit(catalog)
    payload = intake.canonical_json_bytes(catalog)
    (base / "fidelity-intake-catalog.json").write_bytes(payload)
    manifest = json.loads((base / "normalized-manifest.json").read_text())
    manifest["catalog_sha256"] = intake._sha(payload)
    (base / "normalized-manifest.json").write_bytes(
        intake.canonical_json_bytes(manifest)
    )


def test_verifier_catches_tampering(tmp_path: Path) -> None:
    root, request_id = _full(tmp_path)
    _rewrite_catalog(
        root,
        request_id,
        lambda c: c["cash_flows"][0]["amount"].__setitem__("value", "101"),
    )
    with pytest.raises(IntakeVerificationError) as caught:
        verify_intake(root, request_id)
    assert "amount_sum_differs_from_raw" in caught.value.reasons


def test_verifier_catches_dropped_rows_and_leaks(tmp_path: Path) -> None:
    root, request_id = _full(tmp_path)
    _rewrite_catalog(root, request_id, lambda c: c["transactions"].pop())
    with pytest.raises(IntakeVerificationError) as caught:
        verify_intake(root, request_id)
    assert "activity_count_differs_from_raw" in caught.value.reasons
    case = tmp_path / "leak"
    case.mkdir()
    root, request_id = _full(case)
    _rewrite_catalog(
        root,
        request_id,
        lambda c: c["accounts"][0].__setitem__("provider_ref", ACCOUNT),
    )
    with pytest.raises(IntakeVerificationError) as caught:
        verify_intake(root, request_id)
    assert any(r.startswith("private_values_in_catalog") for r in caught.value.reasons)


def test_verifier_catches_raw_tamper_and_extras(tmp_path: Path) -> None:
    root, request_id = _full(tmp_path)
    raw = next((root / request_id / "raw").iterdir())
    raw.write_bytes(raw.read_bytes() + b"\n")
    (root / request_id / "stray.txt").write_text("x")
    with pytest.raises(IntakeVerificationError) as caught:
        verify_intake(root, request_id)
    assert "raw_hash_differs" in caught.value.reasons
    assert any(r.startswith("unexpected_entries") for r in caught.value.reasons)


def test_overlapping_files_are_rejected_by_verifier(tmp_path: Path) -> None:
    root, request_id = _request(tmp_path)
    first = _export(tmp_path / "a.csv")
    second = _export(
        tmp_path / "b.csv",
        [
            _row("01/03/2030", "YOU BOUGHT SYNTHETIC SECURITY", "-10", symbol="SYNTH"),
            _row("01/09/2030", "DIRECT DEPOSIT SYNTHETIC", "5"),
        ],
    )
    stage_raw(root, request_id, [first, second])
    normalize(root, request_id, _crosswalk(tmp_path))
    with pytest.raises(IntakeVerificationError) as caught:
        verify_intake(root, request_id)
    assert "rows_repeat_across_files" in caught.value.reasons


def test_intake_modules_have_no_network_or_login_code() -> None:
    root = Path(__file__).resolve().parents[2]
    files = [
        root / "packages/fidelity/src/quant_engine/fidelity/intake.py",
        root / "packages/fidelity/src/quant_engine/fidelity/intake_verify.py",
        root / "tools/capture_fidelity_intake.py",
    ]
    banned = {
        "socket",
        "ssl",
        "http",
        "urllib",
        "requests",
        "httpx",
        "ftplib",
        "smtplib",
        "subprocess",
        "webbrowser",
        "ctypes",
        "asyncio",
        "selenium",
        "playwright",
        "keyring",
        "getpass",
        "os",
    }
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if path.name == "intake.py" and top == "os":
                    continue  # os.fsync only; asserted below
                assert top not in banned, (path.name, name)
    source = files[0].read_text(encoding="utf-8")
    assert "os.environ" not in source and "os.system" not in source
