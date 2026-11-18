"""Generate the per-cycle ``base-current`` source bundle from pinned inputs.

The bundle is what release-driver points ``BASE_BUNDLE_SRC`` at::

    BUNDLE.json
    factors.csv
    <mode>/base-source/{factors.csv, targets.csv, strategy-evidence.json,
                        provider-ledger-generation.json, provider-ledger.json,
                        provider-ledger-source-proof.json,
                        source/{acquisition.json, <capture>, <capture>.manifest.json,
                                factor-derivation-proof.json, interval-authority.json}}
    spec/<mode>/base-decomposition-spec.json

Every authority and provenance file is generated here from pinned inputs and its
hash recorded in ``BUNDLE.json``; nothing is copied from a hand-written record.
Two factor constructions are available:

``ADJUSTED_RATIO``
    The R3 construction (``base-factor-r3`` builders): Decimal precision 60 over
    the adjusted-close capture, ``RF = BIL ratio - 1``, ``XS = proxy ratio - 1 - RF``
    on consecutive BIL price labels, all four prices required, no fill.  Run on the
    retained R3-BASE inputs it reproduces the committed R3 bundle byte for byte.

``TOTAL_RETURN_FRENCH_RF``
    ``tools/base_proxy_factors.py``: total returns from the raw capture
    (``(close + dividend) / previous close - 1``) and the Ken French daily RF
    through its last date, the BIL total return after it.  The capture is refused
    while the latest monthly BIL/HYG distribution is unrecorded.

Inputs that are not data but carried method (the base spec template, the
predecessor interval authority for per-series status text and entry order) and
the D plan pass (governance copy, selected targets, plan specs) are inputs of
this generator; see ``interval-authority`` below for the one-step lag that makes
the plan pass an input rather than an output.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
import sys
from typing import Any, Final, Mapping, Sequence
import zipfile

import base_proxy_factors
from quant_engine.core import check_entry_interpreter

MODES: Final = ("live_only", "live_plus_backtest")
CASH: Final = "BIL"
PROXY_TICKERS: Final = ("SPY", "IWM", "HYG")
FACTOR_COLUMNS: Final = ("date", "RF", "SPY_XS", "IWM_XS", "HYG_XS")
ADJUSTED_RATIO: Final = "ADJUSTED_RATIO"
TOTAL_RETURN_FRENCH_RF: Final = "TOTAL_RETURN_FRENCH_RF"
CONSTRUCTIONS: Final = (ADJUSTED_RATIO, TOTAL_RETURN_FRENCH_RF)
DECIMAL_PRECISION: Final = 60
ADJUSTED_CONSTRUCTION: Final = (
    "CONSECUTIVE_BIL_PRICE_LABELS_ALL_FOUR_PRICES_REQUIRED_NO_FILL; "
    "RF=BIL_RATIO-1; XS=PROXY_RATIO-1-RF"
)
# |emitted - exact| bound for a handful of 60-digit Decimal operations
ROUNDING_BOUND: Final = Fraction(4, 10**59)
CAPTURE_ARTIFACT: Final = {
    ADJUSTED_RATIO: "yahoo-base-proxies-history_adjusted.json",
    TOTAL_RETURN_FRENCH_RF: "yahoo-base-proxies-history.json",
}
# bundle file name and spec schema name of the published capture
PUBLISHED_CAPTURE: Final = {
    ADJUSTED_RATIO: ("base-proxies-adjusted.json", "yahoo-adjusted-history"),
    TOTAL_RETURN_FRENCH_RF: ("base-proxies-history.json", "yahoo-history"),
}
FRENCH_PUBLISHED: Final = "french-5-factors-daily.zip"
LEDGER_FILES: Final = (
    "provider-ledger-generation.json",
    "provider-ledger.json",
    "provider-ledger-source-proof.json",
    "strategy-evidence.json",
)


class BaseBundleError(ValueError):
    """The inputs cannot produce a trustworthy base bundle."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pin(data: bytes, path_text: str) -> dict[str, Any]:
    return {"path": path_text, "sha256": sha256(data), "size_bytes": len(data)}


def _json(data: bytes, what: str) -> Any:
    try:
        return json.loads(data)
    except ValueError as exc:
        raise BaseBundleError(f"{what} is not JSON") from exc


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BaseBundleError(f"{what} is not an object")
    return value


def _sorted_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


# --------------------------------------------------------------------------- factors


@dataclass(frozen=True)
class FactorRow:
    date: str
    previous: str
    rf: Decimal
    xs: tuple[Decimal, ...]


def adjusted_prices(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Decimal]]:
    """Ticker -> native date label -> adjusted close, as ``Decimal(repr(float))``."""

    prices: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        close = row.get("Close")
        if close is None:
            continue
        prices.setdefault(str(row["Ticker"]), {})[str(row["Date"])[:10]] = Decimal(
            repr(close)
        )
    return prices


def adjusted_ratio_rows(prices: Mapping[str, Mapping[str, Decimal]]) -> list[FactorRow]:
    """ADJUSTED_RATIO on the dates where all four prices exist."""

    missing = sorted({CASH, *PROXY_TICKERS} - set(prices))
    if missing:
        raise BaseBundleError(f"capture lacks tickers: {missing}")
    dates = sorted(set.intersection(*(set(prices[t]) for t in (CASH, *PROXY_TICKERS))))
    out: list[FactorRow] = []
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        for previous, current in zip(dates, dates[1:]):
            rf = prices[CASH][current] / prices[CASH][previous] - 1
            xs = tuple(
                prices[t][current] / prices[t][previous] - 1 - rf for t in PROXY_TICKERS
            )
            out.append(FactorRow(current, previous, rf, xs))
    if not out:
        raise BaseBundleError("capture yields no factor rows")
    return out


def factors_csv(rows: Sequence[Mapping[str, str]]) -> bytes:
    lines = [",".join(FACTOR_COLUMNS)]
    lines.extend(",".join(row[c] for c in FACTOR_COLUMNS) for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _as_text_rows(rows: Sequence[FactorRow]) -> list[dict[str, str]]:
    out = []
    for row in rows:
        values = {"date": row.date, "RF": format(row.rf, "f")}
        for column, value in zip(FACTOR_COLUMNS[2:], row.xs):
            values[column] = format(value, "f")
        out.append(values)
    return out


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class ModeInputs:
    """Per-mode cycle inputs.

    With ``plan_dir`` the governance copy, selected targets and interval authority
    are cut from that D plan pass.  Without it they are carried from the
    predecessor bundle (``spec/<mode>`` and ``<mode>/base-source``): its rebound
    spec fields, its ``targets.csv`` and its interval authority verbatim (the
    authority is then still the predecessor's one-step-lag cut).
    """

    spec_template: Path
    plan_dir: Path | None = None
    authority_template: Path | None = None
    plan_manifest_text: str | None = None
    predecessor_bundle: Path | None = None


@dataclass(frozen=True)
class BundleInputs:
    construction: str
    anchor: str
    capture_dir: Path
    ledger_generation: Path
    ledger_catalog: Path
    modes: Mapping[str, ModeInputs]
    native_raw: Path | None = None
    run_record: Path | None = None
    french_zip: Path | None = None
    capture_dir_text: str | None = None
    factor_path_text: str | None = None
    driver_path_text: str = "tools/yahoo_fetch_driver.py"
    governance_name: str = "governance-evidence.json"
    extra: Mapping[str, Any] = field(default_factory=dict)


def load_config(path: Path, overrides: Mapping[str, Any] | None = None) -> BundleInputs:
    """Read the JSON config; relative paths resolve against the config's folder."""

    raw = _object(_json(path.read_bytes(), "config"), "config")
    raw.update({k: v for k, v in (overrides or {}).items() if v is not None})
    base = path.parent

    def p(value: Any) -> Path:
        candidate = Path(str(value))
        return candidate if candidate.is_absolute() else base / candidate

    def opt(key: str) -> Path | None:
        return p(raw[key]) if raw.get(key) else None

    modes_raw = _object(raw.get("modes"), "config modes")
    if set(modes_raw) != set(MODES):
        raise BaseBundleError(f"config modes must be exactly {list(MODES)}")
    predecessor = opt("predecessor_bundle")
    modes = {}
    for mode in MODES:
        m = _object(modes_raw[mode], f"config modes.{mode}")
        plan_dir = p(m["plan_dir"]) if m.get("plan_dir") else None
        if plan_dir is not None and not m.get("authority_template"):
            raise BaseBundleError(
                f"modes.{mode}: a plan_dir needs an authority_template"
            )
        if plan_dir is None and predecessor is None:
            raise BaseBundleError(
                f"modes.{mode}: give a plan_dir or a predecessor_bundle"
            )
        modes[mode] = ModeInputs(
            spec_template=p(m["spec_template"]),
            plan_dir=plan_dir,
            authority_template=p(m["authority_template"])
            if m.get("authority_template")
            else None,
            plan_manifest_text=m.get("plan_manifest_text"),
            predecessor_bundle=predecessor,
        )
    construction = str(raw.get("construction", ""))
    if construction not in CONSTRUCTIONS:
        raise BaseBundleError(f"construction must be one of {list(CONSTRUCTIONS)}")
    return BundleInputs(
        construction=construction,
        anchor=str(raw["anchor"]),
        capture_dir=p(raw["capture_dir"]),
        ledger_generation=p(raw["ledger_generation"]),
        ledger_catalog=p(raw["ledger_catalog"]),
        modes=modes,
        native_raw=opt("native_raw"),
        run_record=opt("run_record"),
        french_zip=opt("french_zip"),
        capture_dir_text=raw.get("capture_dir_text"),
        factor_path_text=raw.get("factor_path_text"),
        driver_path_text=str(
            raw.get("driver_path_text", "tools/yahoo_fetch_driver.py")
        ),
        governance_name=str(raw.get("governance_name", "governance-evidence.json")),
    )


# --------------------------------------------------------------------------- capture record


# files of the governed capture run each construction reads (names under the capture dir)
CAPTURE_FILES: Final = {
    "ADJUSTED_RATIO": (
        "artifacts/yahoo-base-proxies-history_adjusted.json",
        "artifacts/yahoo-base-proxies-history_adjusted.json.manifest.json",
    ),
    "TOTAL_RETURN_FRENCH_RF": (
        "artifacts/yahoo-base-proxies-history.json",
        "artifacts/yahoo-base-proxies-history.json.manifest.json",
    ),
}


def capture_record(inputs: BundleInputs) -> dict[str, Any]:
    """The capture run record, from the governed stage manifest and governance file.

    An optional hand-off run record (``acquisition-r3.json`` shape) supplies the
    driver hash and return code; every field it shares with the stage manifest
    must agree.
    """

    stage_bytes = (inputs.capture_dir / "MANIFEST.json").read_bytes()
    stage = _object(_json(stage_bytes, "stage manifest"), "stage manifest")
    governance = _object(
        _json(
            (inputs.capture_dir / inputs.governance_name).read_bytes(),
            "governance evidence",
        ),
        "governance evidence",
    )
    run_id = str(stage["run_id"])
    if governance.get("run_id") != run_id:
        raise BaseBundleError("governance evidence names another run")
    if stage.get("not_fetched"):
        raise BaseBundleError("stage manifest lists tickers not fetched")
    listed = _object(stage.get("files"), "stage manifest files")
    # the stage manifest vouches only for the files it lists: the capture, its manifest
    # and (where one is used) the native raw file must all be listed, or a tampered
    # price would build a bundle credited to the run
    required = set(CAPTURE_FILES[inputs.construction])
    if inputs.native_raw is not None and inputs.native_raw.is_relative_to(
        inputs.capture_dir
    ):
        required.add(inputs.native_raw.relative_to(inputs.capture_dir).as_posix())
    unlisted = sorted(required - set(listed))
    if unlisted:
        raise BaseBundleError(
            f"stage manifest does not list the capture file(s): {unlisted}"
        )
    for name, entry in listed.items():
        data = (inputs.capture_dir / name).read_bytes()
        if sha256(data) != entry["sha256"] or len(data) != entry["size_bytes"]:
            raise BaseBundleError(
                f"capture file differs from its stage manifest: {name}"
            )
    argv_logical = [
        "fetch",
        "--config",
        f"{stage['run_label']}-config.json",
        "--cutoff",
        str(stage["cutoff"]),
        "--run-id",
        run_id,
        "--governance",
        inputs.governance_name,
    ]
    record: dict[str, Any] = {
        "argv": [inputs.driver_path_text, *argv_logical],
        "cwd": f"not recorded by the governed driver run (run_id {run_id})",
        "started_at_utc": stage["fetch_started_utc"],
        "fetcher_sha256": stage["adapter_sha256"],
        "returncode": 0,
        "finished_at_utc": stage["fetch_completed_utc"],
        "approval": governance["review"],
        "run_id": run_id,
        "stage_manifest_sha256": sha256(stage_bytes),
    }
    if inputs.run_record is not None:
        given = _object(
            _json(inputs.run_record.read_bytes(), "run record"), "run record"
        )
        agree = {
            "argv_logical": argv_logical,
            "run_id": run_id,
            "started_utc": record["started_at_utc"],
            "finished_utc": record["finished_at_utc"],
            "adapter_sha256_fetch_yfinance": record["fetcher_sha256"],
            "approval": record["approval"],
            "config_effective_sha256": stage["config_effective_sha256"],
        }
        for key, expected in agree.items():
            if key not in given:
                raise BaseBundleError(f"run record lacks {key}")
            if given[key] != expected:
                raise BaseBundleError(
                    f"run record {key} differs from the stage manifest"
                )
        if "returncode" not in given:
            raise BaseBundleError("run record lacks returncode")
        record["returncode"] = given["returncode"]
        if "driver_sha256" in given:
            record["driver_sha256"] = given["driver_sha256"]
    if record["returncode"] != 0:
        raise BaseBundleError("capture run did not return 0")
    return record


def acquisition_bytes(
    record: Mapping[str, Any], outputs: list[dict[str, Any]]
) -> bytes:
    """``source/acquisition.json``: the R3 record layout (CRLF, insertion order)."""

    body = {
        "argv": record["argv"],
        "cwd": record["cwd"],
        "started_at_utc": record["started_at_utc"],
        "fetcher_sha256": record["fetcher_sha256"],
        "returncode": record["returncode"],
        "finished_at_utc": record["finished_at_utc"],
        "outputs": outputs,
    }
    return (json.dumps(body, indent=2).replace("\n", "\r\n") + "\r\n").encode("utf-8")


# --------------------------------------------------------------------------- proofs


def _native_raw_csv_check(
    raw: bytes, capture_rows: Sequence[Mapping[str, Any]]
) -> tuple[int, bool]:
    adjusted = {
        (str(r["Ticker"]), str(r["Date"])[:10]): r["Close"] for r in capture_rows
    }
    count, same = 0, True
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8"), newline="")):
        count += 1
        if row["Adj Close"] != "":
            got = adjusted.get((row["Ticker"], row["Date"][:10]))
            same = same and got is not None and float(row["Adj Close"]) == float(got)
    return count, same


def adjusted_ratio_proof(
    prices: Mapping[str, Mapping[str, Decimal]],
    rows: Sequence[FactorRow],
    native_raw: bytes,
    capture_rows: Sequence[Mapping[str, Any]],
    capture_block: dict[str, Any],
    factor_pin: dict[str, Any],
    source_pin: dict[str, Any],
    manifest_pin: dict[str, Any],
) -> dict[str, Any]:
    raw_rows, same = _native_raw_csv_check(native_raw, capture_rows)
    bil_dates = sorted(prices[CASH])
    previous_of = dict(zip(bil_dates[1:], bil_dates))
    worst = Fraction(0)
    pick = {rows[0].date, rows[len(rows) // 2].date, rows[-1].date}
    samples = []
    for row in rows:
        before = previous_of[row.date]
        f_rf = Fraction(prices[CASH][row.date]) / Fraction(prices[CASH][before]) - 1
        emitted = {"RF": format(row.rf, "f")}
        expect = {"RF": f_rf}
        worst = max(worst, abs(Fraction(row.rf) - f_rf))
        for ticker, value in zip(PROXY_TICKERS, row.xs):
            f_x = (
                Fraction(prices[ticker][row.date]) / Fraction(prices[ticker][before])
                - 1
                - f_rf
            )
            emitted[f"{ticker}_XS"] = format(value, "f")
            expect[f"{ticker}_XS"] = f_x
            worst = max(worst, abs(Fraction(value) - f_x))
        if row.date in pick:
            samples.append(
                {
                    "date": row.date,
                    "emitted": {**emitted, "date": row.date},
                    "exact_rational_expected": {k: str(v) for k, v in expect.items()},
                    "native_adjusted_closes": {
                        t: {
                            "current": str(prices[t][row.date]),
                            "previous": str(prices[t][before]),
                        }
                        for t in (CASH, *PROXY_TICKERS)
                    },
                    "previous_date": before,
                }
            )
    if worst > ROUNDING_BOUND:
        raise BaseBundleError(f"oracle error {worst} exceeds the rounding bound")
    if not same:
        raise BaseBundleError("capture Close differs from the native Adj Close")
    return {
        "adjusted_close_matches_native_adj_close_all_rows": same,
        "capture": capture_block,
        "construction": ADJUSTED_CONSTRUCTION,
        "decimal_precision": DECIMAL_PRECISION,
        "excluded_source_rows": 0,
        "factor": factor_pin,
        "factor_end": rows[-1].date,
        "factor_rows": len(rows),
        "factor_start": rows[0].date,
        "native_raw_rows": raw_rows,
        "native_raw_sha256": sha256(native_raw),
        "oracle": {
            "absolute_rounding_bound": _fraction_text(ROUNDING_BOUND),
            "algorithm": "INDEPENDENT_EXACT_FRACTION_FROM_NATIVE_ADJ_CLOSE",
            "checks": len(rows) * 4,
            "literal_samples": samples,
            "maximum_absolute_error": _fraction_text(worst),
        },
        "price_end": bil_dates[-1],
        "price_rows_per_ticker": {t: len(prices[t]) for t in (CASH, *PROXY_TICKERS)},
        "price_start": bil_dates[0],
        "prior_factor_hash_reproduction_claim": False,
        "source": source_pin,
        "source_coverage_claim": "PARTIAL",
        "source_dates_are_native_labels": True,
        "source_manifest": manifest_pin,
        "status": "PASS_SOURCE_AND_LITERAL_RATIONAL_ARITHMETIC",
    }


def french_rf_from_zip(data: bytes) -> tuple[str, dict[Any, Decimal]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != 1:
            raise BaseBundleError("French source zip must hold exactly one file")
        text = archive.read(names[0]).decode("latin-1")
    return names[0], base_proxy_factors.french_rf_from_text(text)


def total_return_construction(splice_after: str) -> str:
    return (
        "CONSECUTIVE_BIL_PRICE_LABELS_ALL_FOUR_PRICES_REQUIRED_NO_FILL; "
        "TOTAL_RETURN=(CLOSE+DIVIDEND)/PREVIOUS_CLOSE-1; "
        f"RF=FRENCH_RF_THROUGH_{splice_after}_THEN_BIL_TOTAL_RETURN; "
        "XS=PROXY_TOTAL_RETURN-RF"
    )


def total_return_proof(
    capture_rows: Sequence[Mapping[str, Any]],
    text_rows: Sequence[Mapping[str, str]],
    receipt: Mapping[str, Any],
    french_rf: Mapping[Any, Decimal],
    french_pin: dict[str, Any],
    french_member: str,
    capture_block: dict[str, Any],
    factor_pin: dict[str, Any],
    source_pin: dict[str, Any],
    manifest_pin: dict[str, Any],
) -> dict[str, Any]:
    """Independent exact-fraction check of every emitted cell from raw closes."""

    from datetime import date

    raw: dict[str, dict[str, tuple[Fraction, Fraction]]] = {}
    excluded = 0
    for row in capture_rows:
        close = row.get("Close")
        if close in (None, ""):
            excluded += 1
            continue
        dividend = row.get("Dividends")
        raw.setdefault(str(row["Ticker"]), {})[str(row["Date"])[:10]] = (
            Fraction(Decimal(str(close))),
            Fraction(Decimal(str(dividend)))
            if dividend not in (None, "")
            else Fraction(0),
        )
    bil_dates = sorted(raw[CASH])
    previous_of = dict(zip(bil_dates[1:], bil_dates))
    worst = Fraction(0)
    pick = {
        text_rows[0]["date"],
        text_rows[len(text_rows) // 2]["date"],
        text_rows[-1]["date"],
    }
    samples = []

    def total(ticker: str, current: str, before: str) -> Fraction:
        try:
            close, dividend = raw[ticker][current]
            previous_close = raw[ticker][before][0]
        except KeyError as exc:
            raise BaseBundleError(
                f"{ticker} lacks a price on {current} or {before}: no fill"
            ) from exc
        return (close + dividend) / previous_close - 1

    french_days = 0
    for row in text_rows:
        current = row["date"]
        before = previous_of[current]
        day = date.fromisoformat(current)
        if day in french_rf:
            f_rf = Fraction(french_rf[day])
            french_days += 1
        else:
            f_rf = total(CASH, current, before)
        expect = {"RF": f_rf}
        for ticker in PROXY_TICKERS:
            expect[f"{ticker}_XS"] = total(ticker, current, before) - f_rf
        for key, value in expect.items():
            worst = max(worst, abs(Fraction(Decimal(row[key])) - value))
        if current in pick:
            samples.append(
                {
                    "date": current,
                    "emitted": dict(row),
                    "exact_rational_expected": {k: str(v) for k, v in expect.items()},
                    "native_closes_and_dividends": {
                        t: {
                            "current": [str(x) for x in raw[t][current]],
                            "previous_close": str(raw[t][before][0]),
                        }
                        for t in (CASH, *PROXY_TICKERS)
                    },
                    "previous_date": before,
                    "risk_free_source": "FRENCH"
                    if day in french_rf
                    else "BIL_TOTAL_RETURN",
                }
            )
    if worst > ROUNDING_BOUND:
        raise BaseBundleError(f"oracle error {worst} exceeds the rounding bound")
    dividends = {
        t: {
            "records": sum(1 for _c, d in raw[t].values() if d > 0),
            "last_ex_date": max(
                (k for k, (_c, d) in raw[t].items() if d > 0), default=None
            ),
        }
        for t in (CASH, *PROXY_TICKERS)
    }
    return {
        "capture": capture_block,
        "construction": total_return_construction(
            str(receipt["risk_free_splice_after"])
        ),
        "decimal_precision": DECIMAL_PRECISION,
        "dividend_records": dividends,
        "excluded_source_rows": excluded,
        "factor": factor_pin,
        "factor_end": text_rows[-1]["date"],
        "factor_rows": len(text_rows),
        "factor_start": text_rows[0]["date"],
        "french_source": {**french_pin, "member": french_member},
        "oracle": {
            "absolute_rounding_bound": _fraction_text(ROUNDING_BOUND),
            "algorithm": "INDEPENDENT_EXACT_FRACTION_FROM_NATIVE_CLOSE_DIVIDEND_AND_FRENCH_RF",
            "checks": len(text_rows) * 4,
            "french_rf_rows": french_days,
            "literal_samples": samples,
            "maximum_absolute_error": _fraction_text(worst),
        },
        "price_end": bil_dates[-1],
        "price_rows_per_ticker": {t: len(raw[t]) for t in (CASH, *PROXY_TICKERS)},
        "price_start": bil_dates[0],
        "prior_factor_hash_reproduction_claim": False,
        "receipt": dict(receipt),
        "source": source_pin,
        "source_coverage_claim": "PARTIAL",
        "source_dates_are_native_labels": True,
        "source_manifest": manifest_pin,
        "status": "PASS_SOURCE_AND_LITERAL_RATIONAL_ARITHMETIC",
    }


# --------------------------------------------------------------------------- per mode


def provider_ledger_files(generation: Path, catalog: Path) -> dict[str, bytes]:
    """The four ledger files; proof and evidence cut by the repo's own exporter."""

    from portfolio_target_series import build_provider_ledger_targets

    _targets, evidence, proof, _manifest = build_provider_ledger_targets(
        generation, catalog
    )
    return {
        "provider-ledger-generation.json": generation.read_bytes(),
        "provider-ledger.json": catalog.read_bytes(),
        "provider-ledger-source-proof.json": proof,
        "strategy-evidence.json": evidence,
    }


def interval_authority(
    template_bytes: bytes,
    governance: Mapping[str, Any],
    selected: bytes,
    plan_dir: Path,
    plan_manifest_text: str | None,
) -> bytes:
    """Cut from the D plan pass: plan spec hashes, selected targets, finality.

    One-step lag: the plan pass consumes a base spec, and this file pins the plan
    pass's specs, so the authority is cut from the earlier plan pass and its own
    hash goes into the final base spec.  Per-series status text and entry order
    are carried from the predecessor authority (method, not data).
    """

    old = _object(_json(template_bytes, "authority template"), "authority template")
    plan_manifest = plan_dir / "plan-manifest.json"
    specs = {f"specs/{p.name}": p for p in (plan_dir / "specs").glob("*.json")}
    order = [str(e["relative_path"]) for e in old["authority_specs"]]
    if set(order) != set(specs):
        raise BaseBundleError(
            f"plan specs differ from the authority template: {sorted(set(order) ^ set(specs))}"
        )
    manifest_bytes = plan_manifest.read_bytes()
    body: dict[str, Any] = {
        "authority_specs": [
            {"relative_path": rel, "sha256": sha256(specs[rel].read_bytes())}
            for rel in order
        ],
        "per_series_authority": old["per_series_authority"],
        "selected_target_sha256": sha256(selected),
        "source_plan": {
            "path": plan_manifest_text or str(plan_manifest),
            "sha256": sha256(manifest_bytes),
            "size_bytes": len(manifest_bytes),
        },
        "target_interval_policy": _copy(old["target_interval_policy"]),
    }
    policies = governance["target_source"]["interval_policy"]["series_policies"]
    for series, policy in body["target_interval_policy"]["series_policies"].items():
        policy["finalized_through"] = policies[series]["finalized_through"]
        policy["source_vintage"] = policies[series]["source_vintage"]
    newline = b"\r\n" if b"\r\n" in template_bytes else b"\n"
    text = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
    if newline == b"\r\n":
        text = text.replace(b"\n", b"\r\n")
    return text + newline if template_bytes.endswith(newline) else text


def base_spec(
    template: Mapping[str, Any],
    governance: Mapping[str, Any],
    selected: bytes,
    factors: bytes,
    anchor: str,
    base_files: Mapping[str, bytes],
    construction: str,
) -> dict[str, Any]:
    spec: dict[str, Any] = _copy(template)
    spec["as_of_date"] = governance["as_of_date"]
    spec["series_composition"] = _copy(governance["series_composition"])
    spec["target_source"] = _copy(governance["target_source"])
    spec["target_source"]["expected_sha256"] = sha256(selected)
    spec["target_source"]["expected_size_bytes"] = len(selected)
    source = spec["factor_source"]
    source["expected_sha256"], source["expected_size_bytes"] = (
        sha256(factors),
        len(factors),
    )
    source["interval_policy"]["available_through_anchor"] = anchor
    source["interval_policy"]["source_vintage"] = sha256(factors)
    if construction == TOTAL_RETURN_FRENCH_RF:
        spec["provenance_sources"] = _total_return_provenance(
            spec["provenance_sources"]
        )
    for item in spec["provenance_sources"]:
        data = base_files.get(str(item["relative_path"]))
        if data is None:
            raise BaseBundleError(
                f"no bundle file for provenance {item['relative_path']}"
            )
        item["expected_sha256"], item["expected_size_bytes"] = sha256(data), len(data)
    return spec


def _total_return_provenance(
    items: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Swap the adjusted capture slots for the raw capture; add the French source."""

    old_name, _schema = PUBLISHED_CAPTURE[ADJUSTED_RATIO]
    new_name, new_schema = PUBLISHED_CAPTURE[TOTAL_RETURN_FRENCH_RF]
    out: list[dict[str, Any]] = []
    for raw_item in items:
        item = _copy(raw_item)
        rel = str(item["relative_path"])
        if rel == f"source/{old_name}":
            item.update(
                artifact_id=f"current-base-proxy/{new_name}",
                relative_path=f"source/{new_name}",
                schema_name=new_schema,
            )
        elif rel == f"source/{old_name}.manifest.json":
            item.update(
                artifact_id=f"current-base-proxy/{new_name}.manifest.json",
                relative_path=f"source/{new_name}.manifest.json",
            )
        out.append(item)
        if rel == f"source/{old_name}.manifest.json":
            out.append(
                {
                    "artifact_id": f"current-base-proxy/{FRENCH_PUBLISHED}",
                    "expected_sha256": "",
                    "expected_size_bytes": 0,
                    "relative_path": f"source/{FRENCH_PUBLISHED}",
                    "schema_name": "ken-french-daily-factor-source",
                    "schema_version": "1",
                }
            )
    return out


def mode_cycle_inputs(
    mode: str, m: ModeInputs
) -> tuple[dict[str, Any], bytes, bytes, dict[str, Any]]:
    """Governance fields, selected targets, interval authority and their pins."""

    pins: dict[str, Any] = {}
    if m.plan_dir is not None:
        if m.authority_template is None:
            raise BaseBundleError(f"{mode}: a plan_dir needs an authority_template")
        governance_path = m.plan_dir / "inputs/governance/base-decomposition-spec.json"
        selected_path = m.plan_dir / "inputs/history/selected-targets.csv"
        governance_bytes = governance_path.read_bytes()
        governance = _object(
            _json(governance_bytes, "plan governance copy"), "governance"
        )
        selected = selected_path.read_bytes()
        template = m.authority_template.read_bytes()
        authority = interval_authority(
            template, governance, selected, m.plan_dir, m.plan_manifest_text
        )
        manifest = (m.plan_dir / "plan-manifest.json").read_bytes()
        pins[f"{mode}.plan_manifest"] = pin(
            manifest, m.plan_manifest_text or str(m.plan_dir / "plan-manifest.json")
        )
        pins[f"{mode}.plan_governance"] = pin(governance_bytes, str(governance_path))
        pins[f"{mode}.authority_template"] = pin(template, str(m.authority_template))
        return governance, selected, authority, pins
    if m.predecessor_bundle is None:
        raise BaseBundleError(f"{mode}: give a plan_dir or a predecessor_bundle")
    spec_path = m.predecessor_bundle / "spec" / mode / "base-decomposition-spec.json"
    source = m.predecessor_bundle / mode / "base-source"
    spec_bytes = spec_path.read_bytes()
    spec = _object(_json(spec_bytes, "predecessor spec"), "predecessor spec")
    selected = (source / "targets.csv").read_bytes()
    authority = (source / "source/interval-authority.json").read_bytes()
    pinned = _object(_json(authority, "predecessor authority"), "predecessor authority")
    if pinned.get("selected_target_sha256") != sha256(selected):
        raise BaseBundleError(f"{mode}: predecessor authority pins other targets")
    if spec["target_source"].get("expected_sha256") != sha256(selected):
        raise BaseBundleError(f"{mode}: predecessor spec pins other targets")
    governance = {
        "as_of_date": spec["as_of_date"],
        "series_composition": spec["series_composition"],
        "target_source": spec["target_source"],
    }
    pins[f"{mode}.predecessor_spec"] = pin(spec_bytes, str(spec_path))
    pins[f"{mode}.predecessor_targets"] = pin(selected, str(source / "targets.csv"))
    pins[f"{mode}.predecessor_interval_authority"] = pin(
        authority, str(source / "source/interval-authority.json")
    )
    return governance, selected, authority, pins


# --------------------------------------------------------------------------- build


def build_bundle(inputs: BundleInputs, out: Path) -> dict[str, Any]:
    """Write the bundle under ``out`` (must not exist); return the BUNDLE.json body."""

    if out.exists():
        raise BaseBundleError(f"output exists: {out}")
    construction = inputs.construction
    capture_dir_text = inputs.capture_dir_text or str(inputs.capture_dir)
    artifact = CAPTURE_ARTIFACT[construction]
    capture_path = inputs.capture_dir / "artifacts" / artifact
    manifest_path = capture_path.with_name(artifact + ".manifest.json")
    capture = capture_path.read_bytes()
    capture_manifest = manifest_path.read_bytes()
    payload = _json(capture, "capture")
    capture_rows: list[Mapping[str, Any]] = (
        payload["rows"] if isinstance(payload, dict) else payload
    )
    record = capture_record(inputs)
    source_pin = pin(capture, f"{capture_dir_text}\\artifacts\\{artifact}")
    manifest_pin = pin(
        capture_manifest, f"{capture_dir_text}\\artifacts\\{artifact}.manifest.json"
    )
    capture_block = {**record, "outputs": [source_pin, manifest_pin]}
    # bundle-relative by default, so the bytes do not depend on where it is built
    factor_text = inputs.factor_path_text or "factors.csv"
    published_name, _schema = PUBLISHED_CAPTURE[construction]
    source_files: dict[str, bytes] = {
        published_name: capture,
        f"{published_name}.manifest.json": capture_manifest,
        "acquisition.json": acquisition_bytes(record, [source_pin, manifest_pin]),
    }
    inputs_pins: dict[str, Any] = {
        "capture": source_pin,
        "capture_manifest": manifest_pin,
        "stage_manifest_sha256": record["stage_manifest_sha256"],
    }
    if construction == ADJUSTED_RATIO:
        if inputs.native_raw is None:
            raise BaseBundleError("ADJUSTED_RATIO needs the native raw csv")
        prices = adjusted_prices(capture_rows)
        rows = adjusted_ratio_rows(prices)
        if rows[-1].date != inputs.anchor:
            raise BaseBundleError(
                f"factors end {rows[-1].date}, anchor is {inputs.anchor}"
            )
        factors = factors_csv(_as_text_rows(rows))
        native_raw = inputs.native_raw.read_bytes()
        inputs_pins["native_raw"] = pin(native_raw, str(inputs.native_raw))
        proof = adjusted_ratio_proof(
            prices,
            rows,
            native_raw,
            capture_rows,
            capture_block,
            pin(factors, factor_text),
            source_pin,
            manifest_pin,
        )
    else:
        if inputs.french_zip is None:
            raise BaseBundleError("TOTAL_RETURN_FRENCH_RF needs the French source zip")
        french = inputs.french_zip.read_bytes()
        member, french_rf = french_rf_from_zip(french)
        try:
            text_rows, receipt = base_proxy_factors.build_factors(
                capture_rows, french_rf
            )
        except base_proxy_factors.BaseProxyError as exc:
            raise BaseBundleError(str(exc)) from exc
        if not text_rows or text_rows[-1]["date"] != inputs.anchor:
            last = text_rows[-1]["date"] if text_rows else None
            raise BaseBundleError(f"factors end {last}, anchor is {inputs.anchor}")
        factors = factors_csv(text_rows)
        french_pin = pin(french, str(inputs.french_zip))
        inputs_pins["french_zip"] = french_pin
        source_files[FRENCH_PUBLISHED] = french
        proof = total_return_proof(
            capture_rows,
            text_rows,
            receipt,
            french_rf,
            french_pin,
            member,
            capture_block,
            pin(factors, factor_text),
            source_pin,
            manifest_pin,
        )
    source_files["factor-derivation-proof.json"] = _sorted_json(proof)
    ledger = provider_ledger_files(inputs.ledger_generation, inputs.ledger_catalog)
    inputs_pins["ledger_generation"] = pin(
        ledger["provider-ledger-generation.json"], str(inputs.ledger_generation)
    )
    inputs_pins["ledger_catalog"] = pin(
        ledger["provider-ledger.json"], str(inputs.ledger_catalog)
    )

    written: dict[str, bytes] = {"factors.csv": factors}
    summary: dict[str, Any] = {
        "factor_rows": len(factors.splitlines()) - 1,
        "factors.csv": [sha256(factors), len(factors)],
    }
    for mode in MODES:
        m = inputs.modes[mode]
        governance, selected, authority, cycle_pins = mode_cycle_inputs(mode, m)
        inputs_pins.update(cycle_pins)
        base_files: dict[str, bytes] = {
            "factors.csv": factors,
            "targets.csv": selected,
            **ledger,
            **{f"source/{k}": v for k, v in source_files.items()},
            "source/interval-authority.json": authority,
        }
        template = _object(_json(m.spec_template.read_bytes(), "spec template"), "spec")
        spec = base_spec(
            template,
            governance,
            selected,
            factors,
            inputs.anchor,
            base_files,
            construction,
        )
        spec_bytes = _sorted_json(spec)
        for rel, data in base_files.items():
            written[f"{mode}/base-source/{rel}"] = data
        written[f"spec/{mode}/base-decomposition-spec.json"] = spec_bytes
        summary[mode] = {
            "interval-authority": sha256(authority),
            "provenance": {
                str(i["artifact_id"]): str(i["expected_sha256"])[:12]
                for i in spec["provenance_sources"]
            },
            "spec": sha256(spec_bytes),
            "targets.csv": sha256(selected),
        }
        inputs_pins[f"{mode}.spec_template"] = pin(
            m.spec_template.read_bytes(), str(m.spec_template)
        )
    summary["construction"] = construction
    summary["inputs"] = inputs_pins
    summary["files"] = {
        rel: {"sha256": sha256(data), "size_bytes": len(data)}
        for rel, data in sorted(written.items())
    }
    for rel, data in written.items():
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (out / "BUNDLE.json").write_bytes(
        (json.dumps(summary, indent=1, sort_keys=True) + "\n").encode("utf-8")
    )
    return summary


LEGACY_KEYS: Final = ("factor_rows", "factors.csv", *MODES)


def compare_reference(out: Path, reference: Path) -> list[str]:
    """Differences between a built bundle and a reference bundle directory."""

    problems: list[str] = []
    ref_summary = _object(
        _json((reference / "BUNDLE.json").read_bytes(), "reference"), "ref"
    )
    new_summary = _object(_json((out / "BUNDLE.json").read_bytes(), "bundle"), "bundle")
    for key in LEGACY_KEYS:
        if ref_summary.get(key) != new_summary.get(key):
            problems.append(f"BUNDLE.json {key} differs")
    ref_files = sorted(
        p.relative_to(reference).as_posix()
        for p in reference.rglob("*")
        if p.is_file() and p.name != "BUNDLE.json"
    )
    new_files = sorted(_object(new_summary["files"], "files"))
    if ref_files != new_files:
        problems.append(f"file sets differ: {sorted(set(ref_files) ^ set(new_files))}")
    for rel in ref_files:
        if (
            rel in new_files
            and (reference / rel).read_bytes() != (out / rel).read_bytes()
        ):
            problems.append(f"bytes differ: {rel}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--config", type=Path, required=True, help="bundle input config"
    )
    parser.add_argument("--out", type=Path, required=True, help="new bundle directory")
    parser.add_argument("--construction", choices=CONSTRUCTIONS)
    parser.add_argument("--capture-dir", help="governed capture stage directory")
    parser.add_argument("--french-zip", help="Ken French 5-factor daily source zip")
    parser.add_argument(
        "--factor-path-text",
        help="factor path recorded in the proof (default: factors.csv)",
    )
    parser.add_argument(
        "--compare", type=Path, help="reference bundle to compare against"
    )
    args = parser.parse_args(argv)
    refused = check_entry_interpreter("the base bundle builder", "base-bundle-builder")
    if refused is not None:
        return refused
    try:
        inputs = load_config(
            args.config,
            {
                "construction": args.construction,
                "capture_dir": args.capture_dir,
                "french_zip": args.french_zip,
                "factor_path_text": args.factor_path_text,
            },
        )
        summary = build_bundle(inputs, args.out)
    except (BaseBundleError, OSError, KeyError) as exc:
        print(f"base bundle refused: {exc}", file=sys.stderr)
        return 2
    bundle = (args.out / "BUNDLE.json").read_bytes()
    print(f"BUNDLE.json {sha256(bundle)} factors.csv {summary['factors.csv'][0]}")
    if args.compare is not None:
        problems = compare_reference(args.out, args.compare)
        for problem in problems:
            print(problem)
        print("reference reproduced byte for byte" if not problems else "DIFFERS")
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
